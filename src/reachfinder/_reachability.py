"""Оркестрация анализа достижимости: от локфайлов до итогового отчёта.

Полный конвейер:

1. определить язык/экосистему;
2. получить полный closure зависимостей из локфайлов;
3. скачать исходники всех зависимостей в кэш;
4. построить графы проекта и каждой зависимости, склеить их;
5. связать нерешённые вызовы с определениями в зависимостях;
6. найти целевые узлы пакета (функцию или весь пакет), вычислить пути
   от точек входа и диагноз.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict, deque

from trailmark.analysis.entrypoints import detect_entrypoints
from trailmark.parse import detect_languages

from reachfinder._boundary import link_boundaries
from reachfinder._cache import PkgCache
from reachfinder._graph import (
    CodeGraph,
    CodeUnit,
    EdgeKind,
    NodeKind,
    merge_graphs,
    normalize_path,
    parse_graph,
    prefix_graph,
    read_attribute,
)
from reachfinder._graph_cache import fingerprint, graph_cache_path, load_graph, save_graph
from reachfinder._languages import ECOSYSTEM_BY_LANGUAGE, ecosystem_for_language
from reachfinder._logger import Logger
from reachfinder._models import (
    ClosureGraph,
    DependencyCoordinate,
    EntryPointPath,
    NodeRef,
    ReachabilityReport,
)
from reachfinder._names import find_coordinate
from reachfinder._scip import ScipIndex, load_index
from reachfinder._sources import SourceError, SourceProvider, get_provider
from reachfinder._target import find_package_nodes, find_target_nodes, to_node_ref

_PROJECT_PREFIX = "project"
_MAX_REFS = 20
_MAX_MISSING = 10
# Максимум маршрутов-свидетелей в отчёте (по одному на достигaющую цель точку входа).
_MAX_PATHS = 200

# Расширения исходников по языку trailmark (для диагностики «нет исходников»).
_SOURCE_EXTENSIONS: dict[str, set[str]] = {
    "python": {".py"},
    "go": {".go"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx"},
    "java": {".java"},
    "c": {".c", ".h"},
    "cpp": {".c", ".h", ".cc", ".cxx", ".cpp", ".hpp"},
    "c_sharp": {".cs"},
}


def compute_reachability(
    project_root: str,
    package: str,
    function: str | None = None,
    *,
    language: str = "auto",
    cache_dir: str | None = None,
    max_depth: int = 20,
    package_depth: int | None = None,
    offline: bool = False,
    jobs: int | None = None,
    timeout: int | None = None,
    logger: Logger | None = None,
) -> ReachabilityReport:
    """Вычислить достижимость функции пакета из точек входа проекта.

    Если function не задана (None/пустая строка), проверяется достижимость
    **любого кода пакета**: находятся все функции/методы в исходниках пакета и
    проверяется, достигает ли их хотя бы одна точка входа проекта.

    :param package_depth: ограничить closure глубиной от прямых зависимостей
        (None/0 — без ограничения, 1 — только прямые зависимости).
    :param offline: использовать только уже скачанные исходники из кэша.
    :param jobs: число процессов для параллельного парсинга (None/0 — авто).
    :param timeout: таймаут команд пакетных менеджеров в секундах (по умолчанию 600).
    :raises SourceError: при невозможности определить или скачать зависимости.
    :raises ValueError: при некорректных аргументах (язык, пакет не найден).
    """
    root = os.path.abspath(project_root)
    trailmark_lang = resolve_language(root, language)
    ecosystem = ecosystem_for_language(trailmark_lang)

    cache = PkgCache(cache_dir or os.path.join(root, ".reachfinder_cache"))
    provider = get_provider(ecosystem, cache)
    if timeout is not None:
        provider.default_timeout = timeout

    _log(logger, f"Определение зависимостей ({ecosystem})...")
    closure = provider.resolve_closure_graph(root)
    coords = _limit_closure(closure, package_depth, logger)
    _log(logger, f"  Зависимостей в closure: {len(coords)}")

    target_coord = _find_coordinate(coords, package, ecosystem)
    _log(logger, f"Целевой пакет: {target_coord.name}@{target_coord.version}")

    if offline:
        _require_cached(coords, cache)

    _log(logger, "Скачивание исходников зависимостей...")
    source_dirs: dict[str, str] = {}
    failed: dict[str, str] = {}
    for coord in coords:
        try:
            source_dirs[coord.name] = os.fspath(provider.fetch(coord))
            _log(logger, f"  {coord.name}@{coord.version}")
        except SourceError as exc:
            failed[coord.name] = str(exc)
            _log(logger, f"  {coord.name}@{coord.version} — ОШИБКА: {exc}")

    if target_coord.name in failed:
        raise SourceError(
            f"Не удалось скачать целевой пакет {target_coord.name}@{target_coord.version}: "
            f"{failed[target_coord.name]}"
        )
    if failed:
        _log(logger, f"  Не скачано пакетов: {len(failed)} — исключены из анализа.")
    target_source_dir = source_dirs[target_coord.name]
    if not _has_source_files(target_source_dir, trailmark_lang):
        _log(logger, f"  В пакете {target_coord.name} нет исходников — пробуем git-репозиторий...")
        try:
            extra = provider.fetch_extra_sources(target_coord)
        except SourceError as exc:
            extra = None
            _log(logger, f"  Не удалось получить исходники из репозитория: {exc}")
        if extra is not None:
            target_source_dir = os.fspath(extra)
            source_dirs[target_coord.name] = target_source_dir
            _log(logger, f"  Исходники взяты из репозитория: {target_source_dir}")

    active_coords = [coord for coord in coords if coord.name not in failed]

    _log(logger, "Построение графов trailmark...")
    parse_jobs: list[tuple[str, str, str]] = [
        (root, trailmark_lang, f"project-{fingerprint(root, {cache.root})}")
    ]
    for coord in active_coords:
        source_path = source_dirs[coord.name]
        parse_jobs.append(
            (
                source_path,
                provider.trailmark_language,
                f"{cache.key_for(coord)}-{fingerprint(source_path)}",
            )
        )

    parsed = _parse_with_cache(parse_jobs, ecosystem, cache.root, jobs, logger)
    project_graph = parsed[0]
    if project_graph is None:
        raise SourceError(f"Не удалось распарсить граф проекта {root}.")

    project_graph.entrypoints.update(detect_entrypoints(project_graph, root))
    project_entrypoint_ids = list(
        dict.fromkeys(
            [
                *project_graph.entrypoints,
                *_conventional_entrypoints(project_graph, set(project_graph.entrypoints)),
                *_package_entrypoints(project_graph, root, trailmark_lang),
            ]
        )
    )
    entrypoint_refs = [
        to_node_ref(project_graph.nodes[node_id])
        for node_id in project_entrypoint_ids
        if node_id in project_graph.nodes
    ]

    graphs: list[CodeGraph] = [prefix_graph(project_graph, _PROJECT_PREFIX)]
    for coord, dep_graph in zip(active_coords, parsed[1:], strict=True):
        if dep_graph is None:
            continue
        graphs.append(prefix_graph(dep_graph, _graph_prefix(coord)))

    merged = merge_graphs(graphs, trailmark_lang)
    _log(logger, f"  Узлов: {len(merged.nodes)}, рёбер: {len(merged.edges)}")

    scip = _build_scip(provider, root, coords, cache.root, logger)
    _log(logger, "Склейка нерешённых вызовов с определениями графа...")
    linked = link_boundaries(merged, scip)
    _log(logger, f"  Добавлено рёбер склейки: {linked}")

    unlinked = _unlinked_boundary_symbols(merged, target_coord)

    stats = {
        "packages": len(coords),
        "graph_nodes": len(merged.nodes),
        "graph_edges": len(merged.edges),
        "boundary_links": linked,
        "entrypoints": len(project_entrypoint_ids),
        "failed_downloads": len(failed),
    }

    target_nodes = (
        find_target_nodes(merged, function, target_source_dir)
        if function
        else find_package_nodes(merged, target_source_dir)
    )
    mode = "function" if function else "package"
    if not target_nodes:
        _log(logger, "  Целевой код не найден в пакете.")
        diagnosis = (
            "no_source"
            if not _has_source_files(target_source_dir, trailmark_lang)
            else "not_found"
        )
        stats["callers"] = 0
        return _report(
            root,
            trailmark_lang,
            ecosystem,
            target_coord,
            function or "",
            [],
            [],
            coords,
            diagnosis=diagnosis,
            stats=stats,
            entrypoints=entrypoint_refs,
            missing_packages=list(failed),
            unlinked_boundaries=unlinked,
            mode=mode,
        )

    entrypoint_ids = [f"{_PROJECT_PREFIX}::{ep_id}" for ep_id in project_entrypoint_ids]

    target_ids = {node.id for node in target_nodes}
    callers = _direct_callers(merged, target_ids)

    ancestors: set[str] = set()
    entrypoint_paths: list[EntryPointPath] = []
    reachable_targets: list[CodeUnit] = []
    project_reachers: list[NodeRef] = []

    if function:
        # Обратный BFS от цели даёт дерево кратчайших путей и множество предков за
        # один линейный проход. Перечисление *всех* путей (как all_simple_paths)
        # на плотных графах (проект + зависимости + склейка границ) взрывается, а для
        # доказательства достижимости достаточно одного пути-свидетеля на точку входа.
        reverse_adjacency = _reverse_calls_adjacency(merged)
        for target in target_nodes:
            next_hop = _reverse_shortest_tree(reverse_adjacency, target.id, max_depth)
            ancestors |= set(next_hop) - {target.id}
            if len(entrypoint_paths) >= _MAX_PATHS:
                continue
            for ep_id in entrypoint_ids:
                if len(entrypoint_paths) >= _MAX_PATHS:
                    break
                if ep_id not in next_hop:
                    continue
                ep_node = merged.nodes.get(ep_id)
                entrypoint_paths.append(
                    EntryPointPath(
                        entrypoint=to_node_ref(ep_node) if ep_node else _empty_ref(ep_id),
                        path=[
                            _ref_for(merged, node_id)
                            for node_id in _reconstruct_path(next_hop, ep_id)
                        ],
                    )
                )
        project_reachers = _project_reachers(merged, ancestors)
    else:
        # Режим «любой код пакета»: один прямой BFS от всех точек входа находит
        # все достижимые функции пакета и кратчайший маршрут-свидетель для каждой.
        forward_adjacency = _forward_calls_adjacency(merged)
        depths, parents = _forward_reachable(forward_adjacency, entrypoint_ids, max_depth)
        reachable_targets = [target for target in target_nodes if target.id in depths]
        for target in reachable_targets[:_MAX_PATHS]:
            raw_path = _reconstruct_forward_path(parents, target.id)
            ep_id = raw_path[0]
            ep_node = merged.nodes.get(ep_id)
            entrypoint_paths.append(
                EntryPointPath(
                    entrypoint=to_node_ref(ep_node) if ep_node else _empty_ref(ep_id),
                    path=[_ref_for(merged, node_id) for node_id in raw_path],
                )
            )
        if not reachable_targets:
            reverse_adjacency = _reverse_calls_adjacency(merged)
            ancestors = _reverse_reachable(reverse_adjacency, target_ids, max_depth) - target_ids
            project_reachers = _project_reachers(merged, ancestors)
        stats["package_functions"] = len(target_nodes)
        stats["reachable_functions"] = len(reachable_targets)

    reachable = bool(entrypoint_paths)
    diagnosis = _diagnose(reachable, ancestors, project_reachers)
    stats["callers"] = len(callers)
    _log(logger, f"  Найдено маршрутов: {len(entrypoint_paths)} (диагноз: {diagnosis})")

    return _report(
        root,
        trailmark_lang,
        ecosystem,
        target_coord,
        function or "",
        target_nodes,
        entrypoint_paths,
        coords,
        diagnosis=diagnosis,
        stats=stats,
        entrypoints=entrypoint_refs,
        callers=callers,
        project_reachers=project_reachers,
        missing_packages=list(failed),
        unlinked_boundaries=unlinked,
        mode=mode,
        reachable_targets=reachable_targets,
    )


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def resolve_language(root: str, language: str) -> str:
    """Определить язык trailmark (auto — по содержимому каталога)."""
    if language and language != "auto":
        from reachfinder._languages import normalize_language

        lang = normalize_language(language)
        if lang not in ECOSYSTEM_BY_LANGUAGE:
            supported = ", ".join(sorted(ECOSYSTEM_BY_LANGUAGE))
            raise ValueError(f"Неподдерживаемый язык '{language}' (доступны: {supported})")
        return lang

    detected = [lang for lang in detect_languages(root) if lang in ECOSYSTEM_BY_LANGUAGE]
    if not detected:
        raise ValueError(f"Не удалось определить язык проекта в {root}")
    if len(detected) > 1:
        langs = ", ".join(detected)
        raise ValueError(
            f"В проекте найдено несколько языков ({langs}); укажите --language явно."
        )
    return detected[0]


def _conventional_entrypoints(graph: CodeGraph, detected: set[str]) -> list[str]:
    """Дополнить точки входа языковой конвенцией: имя main без учёта регистра."""
    result: list[str] = []
    for node in graph.nodes.values():
        if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue
        if node.id in detected:
            continue
        if node.name.lower() == "main":
            result.append(node.id)
    return result


def _package_entrypoints(graph: CodeGraph, root: str, language: str) -> list[str]:
    """Точки входа JS/TS из package.json: поля bin и main.

    Для CLI-проектов (nodemon, eslint) единственный реальный вход —
    исполняемый файл из bin; trailmark не считает его точкой входа.
    """
    if language not in ("javascript", "typescript"):
        return []
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    entries: list[str] = []
    bin_field = data.get("bin")
    if isinstance(bin_field, str):
        entries.append(bin_field)
    elif isinstance(bin_field, dict):
        entries.extend(value for value in bin_field.values() if isinstance(value, str))
    main = data.get("main")
    if isinstance(main, str):
        entries.append(main)
    if not entries:
        return []

    candidates: set[str] = set()
    for entry in entries:
        base = os.path.abspath(os.path.join(root, entry))
        candidates.add(normalize_path(base))
        for suffix in (".js", ".ts", ".tsx", ".mjs", ".cjs"):
            candidates.add(normalize_path(base + suffix))
        for name in ("index.js", "index.ts", "index.tsx"):
            candidates.add(normalize_path(os.path.join(base, name)))

    result: list[str] = []
    for node in graph.nodes.values():
        if node.kind != NodeKind.MODULE or node.location is None:
            continue
        if normalize_path(node.location.file_path) in candidates:
            result.append(node.id)
    return result


def _find_coordinate(
    coords: list[DependencyCoordinate],
    package: str,
    ecosystem: str,
) -> DependencyCoordinate:
    """Найти координату целевого пакета по имени (см. :mod:`reachfinder._names`)."""
    return find_coordinate(coords, package, ecosystem)


def _graph_prefix(coord: DependencyCoordinate) -> str:
    """Уникальный префикс идентификаторов узлов графа зависимости."""
    return f"{coord.name}@{coord.version}"


def _direct_callers(graph: CodeGraph, target_ids: set[str]) -> list[NodeRef]:
    """Функции, вызывающие цель напрямую (в т. ч. через proxy-узлы).

    Рёбра к цели из других пакетов идут через proxy-узел
    (proxy.unresolved:*); чтобы отчёт был осмысленным, вместо самого
    proxy показываем функцию, которая его вызывает.
    """
    leads_to_target = set(target_ids)
    for edge in graph.edges:
        if edge.kind != EdgeKind.CALLS or edge.target_id not in target_ids:
            continue
        node = graph.nodes.get(edge.source_id)
        if node is not None and node.kind == NodeKind.PROXY:
            leads_to_target.add(edge.source_id)

    refs: list[NodeRef] = []
    seen: set[str] = set()
    for edge in graph.edges:
        if edge.kind != EdgeKind.CALLS or edge.target_id not in leads_to_target:
            continue
        node = graph.nodes.get(edge.source_id)
        if node is None or node.kind == NodeKind.PROXY or node.id in seen:
            continue
        seen.add(node.id)
        refs.append(to_node_ref(node))
    refs.sort(key=lambda ref: (ref.file_path, ref.start_line, ref.name))
    return refs


def _project_reachers(graph: CodeGraph, ancestors: set[str]) -> list[NodeRef]:
    """Проектные функции, транзитивно достигающие цели.

    Проектные узлы определяются по префиксу идентификатора (project::),
    а не по пути файла: кэш исходников лежит внутри каталога проекта.
    """
    prefix = f"{_PROJECT_PREFIX}::"
    refs: list[NodeRef] = []
    for node_id in ancestors:
        if not node_id.startswith(prefix):
            continue
        node = graph.nodes.get(node_id)
        if node is None or node.kind == NodeKind.PROXY:
            continue
        refs.append(to_node_ref(node))
    refs.sort(key=lambda ref: (ref.file_path, ref.start_line, ref.name))
    return refs


def _diagnose(reachable: bool, ancestors: set[str], project_reachers: list[NodeRef]) -> str:
    """Определить код причины недостижимости."""
    if reachable:
        return "reachable"
    if not ancestors:
        return "no_callers"
    if not project_reachers:
        return "deps_only"
    return "project_not_entrypoint"


def _forward_calls_adjacency(graph: CodeGraph) -> dict[str, list[str]]:
    """Прямая смежность по рёбрам CALLS (вызывающий -> вызываемые узлы)."""
    forward: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == EdgeKind.CALLS:
            forward[edge.source_id].append(edge.target_id)
    return forward


def _forward_reachable(
    forward_adjacency: dict[str, list[str]],
    starts: list[str],
    max_depth: int,
) -> tuple[dict[str, int], dict[str, str | None]]:
    """Прямой BFS от всех точек входа: глубины и дерево кратчайших путей.

    Возвращает две согласованные структуры:

    * depths — узел -> расстояние от ближайшей точки входа;
    * parents — узел -> предыдущий узел на кратчайшем маршруте
      (для самих точек входа — None).

    Уже посещённые узлы не перезаписываются, поэтому дерево содержит
    кратчайшие маршруты. Обход линейный (O(V + E)).
    """
    depths: dict[str, int] = {}
    parents: dict[str, str | None] = {}
    queue: deque[str] = deque()
    for start in starts:
        if start in depths:
            continue
        depths[start] = 0
        parents[start] = None
        queue.append(start)

    while queue:
        node = queue.popleft()
        node_depth = depths[node]
        if node_depth >= max_depth:
            continue
        for target in forward_adjacency.get(node, ()):
            if target in depths:
                continue
            depths[target] = node_depth + 1
            parents[target] = node
            queue.append(target)
    return depths, parents


def _reconstruct_forward_path(parents: dict[str, str | None], target: str) -> list[str]:
    """Восстановить путь от точки входа до target по дереву parents."""
    path = [target]
    current = parents.get(target)
    while current is not None:
        path.append(current)
        current = parents.get(current)
    path.reverse()
    return path


def _reverse_calls_adjacency(graph: CodeGraph) -> dict[str, list[str]]:
    """Обратная смежность по рёбрам CALLS (цель -> вызывающие узлы)."""
    reverse: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == EdgeKind.CALLS:
            reverse[edge.target_id].append(edge.source_id)
    return reverse


def _reverse_shortest_tree(
    reverse_adjacency: dict[str, list[str]],
    target: str,
    max_depth: int,
) -> dict[str, str | None]:
    """Кратчайшие пути к target: обратный BFS, отдающий next_hop.

    Возвращает отображение «узел -> следующий узел на кратчайшем пути к цели»
    (для самой цели — None). Ключи — все узлы, достигающие цели не дальше
    max_depth шагов. Обход линейный (O(V + E)) и не перечисляет пути,
    поэтому не взрывается на плотных графах.
    """
    next_hop: dict[str, str | None] = {target: None}
    depth = {target: 0}
    queue: deque[str] = deque([target])
    while queue:
        node = queue.popleft()
        node_depth = depth[node]
        if node_depth >= max_depth:
            continue
        for source in reverse_adjacency.get(node, ()):
            if source in next_hop:
                continue
            next_hop[source] = node
            depth[source] = node_depth + 1
            queue.append(source)
    return next_hop


def _reconstruct_path(next_hop: dict[str, str | None], entry: str) -> list[str]:
    """Восстановить путь к цели по дереву next_hop начиная с entry."""
    path = [entry]
    current = next_hop[entry]
    while current is not None:
        path.append(current)
        current = next_hop[current]
    return path


def _reverse_reachable(
    reverse_adjacency: dict[str, list[str]],
    starts: set[str],
    max_depth: int,
) -> set[str]:
    """Все узлы, транзитивно достигающие starts не дальше max_depth шагов.

    Обратный BFS без построения путей: используется для диагностики
    недостижимости в режиме «любой код пакета», когда нужно понять, кто
    вообще вызывает целевые функции.
    """
    visited = set(starts)
    queue: deque[tuple[str, int]] = deque((start, 0) for start in starts)
    while queue:
        node, node_depth = queue.popleft()
        if node_depth >= max_depth:
            continue
        for source in reverse_adjacency.get(node, ()):
            if source in visited:
                continue
            visited.add(source)
            queue.append((source, node_depth + 1))
    return visited


def _report(
    root: str,
    language: str,
    ecosystem: str,
    coord: DependencyCoordinate,
    function: str,
    target_nodes: list[CodeUnit],
    entrypoint_paths: list[EntryPointPath],
    dependencies: list[DependencyCoordinate],
    *,
    diagnosis: str = "reachable",
    stats: dict[str, int] | None = None,
    entrypoints: list[NodeRef] | None = None,
    callers: list[NodeRef] | None = None,
    project_reachers: list[NodeRef] | None = None,
    missing_packages: list[str] | None = None,
    unlinked_boundaries: list[str] | None = None,
    mode: str = "function",
    reachable_targets: list[CodeUnit] | None = None,
) -> ReachabilityReport:
    return ReachabilityReport(
        project=os.path.basename(root.rstrip(os.sep)) or root,
        language=language,
        ecosystem=ecosystem,
        package=coord.name,
        function=function,
        mode=mode,
        reachable=bool(entrypoint_paths),
        diagnosis=diagnosis,
        target_nodes=[to_node_ref(node) for node in target_nodes],
        reachable_targets=[to_node_ref(node) for node in (reachable_targets or [])],
        entrypoint_paths=entrypoint_paths,
        dependencies=dependencies,
        stats=stats or {},
        entrypoints=(entrypoints or [])[:_MAX_REFS],
        callers=(callers or [])[:_MAX_REFS],
        project_reachers=(project_reachers or [])[:_MAX_REFS],
        missing_packages=list(missing_packages or []),
        unlinked_boundaries=list(unlinked_boundaries or []),
    )


def _ref_for(graph: CodeGraph, node_id: str) -> NodeRef:
    node = graph.nodes.get(node_id)
    if node is not None:
        return to_node_ref(node)
    return _empty_ref(node_id)


def _empty_ref(node_id: str) -> NodeRef:
    return NodeRef(node_id=node_id, name=node_id)


def _log(logger: Logger | None, message: str) -> None:
    if logger is not None:
        logger.print_console(message)


def _build_scip(
    provider: SourceProvider,
    root: str,
    coords: list[DependencyCoordinate],
    cache_root: str,
    logger: Logger | None,
) -> ScipIndex | None:
    """Построить и прочитать SCIP-индекс (None, если не удалось)."""
    try:
        path = provider.build_scip_index(root, coords, cache_root)
    except SourceError as exc:
        _log(logger, f"  ПРЕДУПРЕЖДЕНИЕ: SCIP-индекс не построен: {exc}")
        return None
    if path is None:
        return None
    try:
        index = load_index(path, root)
    except (OSError, ValueError) as exc:
        _log(logger, f"  ПРЕДУПРЕЖДЕНИЕ: SCIP-индекс не прочитан: {exc}")
        return None
    _log(logger, f"  SCIP: {len(index.defs)} определений, {len(index.refs)} строк ссылок")
    if not index.refs:
        _log(logger, "  SCIP: индекс не содержит ссылок — используется name-based резолв")
        return None
    return index


def _limit_closure(
    closure: ClosureGraph,
    package_depth: int | None,
    logger: Logger | None,
) -> list[DependencyCoordinate]:
    """Ограничить closure по глубине от прямых зависимостей проекта.

    Если рёбра графа неизвестны для экосистемы, ограничение не применяется
    (скачивается весь closure), о чем сообщается в лог.
    """
    coords = closure.coords
    if package_depth is None or package_depth <= 0:
        return coords
    if not closure.edges_known or not closure.roots:
        _log(
            logger,
            "  Ограничение по глубине недоступно для этой экосистемы; "
            "берутся все пакеты.",
        )
        return coords

    allowed = _names_within_depth(closure, package_depth)
    limited = [coord for coord in coords if coord.name in allowed]
    _log(
        logger,
        f"  Ограничение глубины {package_depth}: {len(limited)} из {len(coords)} пакетов.",
    )
    return limited


def _names_within_depth(closure: ClosureGraph, max_depth: int) -> set[str]:
    """Имена пакетов, достижимых от корней за не более чем max_depth шагов."""
    allowed: set[str] = set()
    frontier = list(closure.roots)
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        next_frontier: list[str] = []
        for name in frontier:
            if name in allowed:
                continue
            allowed.add(name)
            next_frontier.extend(closure.edges.get(name, []))
        frontier = next_frontier
    return allowed


def _require_cached(coords: list[DependencyCoordinate], cache: PkgCache) -> None:
    """Проверить, что все зависимости уже есть в кэше (для --offline)."""
    missing = [coord for coord in coords if not cache.has(coord)]
    if not missing:
        return
    preview = ", ".join(f"{coord.name}@{coord.version}" for coord in missing[:_MAX_MISSING])
    remainder = len(missing) - _MAX_MISSING
    suffix = f" и ещё {remainder}" if remainder > 0 else ""
    raise SourceError(
        f"Офлайн-режим: в кэше отсутствуют {len(missing)} пакет(ов): {preview}{suffix}. "
        "Запустите без --offline, чтобы скачать их."
    )


def _parse_with_cache(
    jobs: list[tuple[str, str, str]],
    ecosystem: str,
    cache_root: str,
    jobs_count: int | None,
    logger: Logger | None,
) -> list[CodeGraph | None]:
    """Распарсить графы, переиспользуя кэш; недостающие — параллельно."""
    result: list[CodeGraph | None] = [None] * len(jobs)
    todo: list[int] = []
    for index, (_source_path, _language, key) in enumerate(jobs):
        cached = load_graph(graph_cache_path(cache_root, ecosystem, key))
        if cached is not None:
            result[index] = cached
        else:
            todo.append(index)

    if todo:
        _log(logger, f"  Парсинг {len(todo)} из {len(jobs)} графов...")
        todo_jobs = [(jobs[index][0], jobs[index][1]) for index in todo]
        parsed = _parse_parallel(todo_jobs, jobs_count, logger)
        for index, graph in zip(todo, parsed, strict=True):
            if graph is not None:
                result[index] = graph
                save_graph(graph_cache_path(cache_root, ecosystem, jobs[index][2]), graph)
    return result


def _parse_parallel(
    jobs: list[tuple[str, str]],
    jobs_count: int | None,
    logger: Logger | None,
) -> list[CodeGraph | None]:
    """Распарсить несколько графов в процессах (None при ошибке парсинга)."""
    if len(jobs) == 1:
        return [_parse_one(jobs[0][0], jobs[0][1], logger)]

    from concurrent.futures import ProcessPoolExecutor

    workers = jobs_count or min(len(jobs), os.cpu_count() or 1)
    results: list[CodeGraph | None] = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [(path, executor.submit(parse_graph, path, language)) for path, language in jobs]
        for path, future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 — один граф не распарсился, остальные продолжаем
                _log(
                    logger,
                    f"  ПРЕДУПРЕЖДЕНИЕ: не удалось распарсить {path}: "
                    f"{type(exc).__name__}: {exc}",
                )
                results.append(None)
    return results


def _parse_one(path: str, language: str, logger: Logger | None) -> CodeGraph | None:
    """Распарсить один граф (None при ошибке) — последовательный путь."""
    try:
        return parse_graph(path, language)
    except Exception as exc:  # noqa: BLE001
        _log(logger, f"  ПРЕДУПРЕЖДЕНИЕ: не удалось распарсить {path}: {type(exc).__name__}: {exc}")
        return None


def _has_source_files(directory: str, language: str) -> bool:
    """Есть ли в каталоге исходники на целевом языке."""
    extensions = _SOURCE_EXTENSIONS.get(language)
    if extensions is None:
        return True  # неизвестный язык — не считаем «нет исходников»
    for _root_dir, _dirs, filenames in os.walk(directory):
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() in extensions:
                return True
    return False


def _unlinked_boundary_symbols(graph: CodeGraph, coord: DependencyCoordinate) -> list[str]:
    """Нераскрытые proxy-символы, ссылающиеся на целевой пакет."""
    needle = _package_short_name(coord.name)
    outgoing = {edge.source_id for edge in graph.edges if edge.kind == EdgeKind.CALLS}
    symbols: set[str] = set()
    for node in graph.nodes.values():
        if node.kind != NodeKind.PROXY or node.id in outgoing:
            continue
        raw = read_attribute(node, "raw_symbol") or node.name
        if needle and needle in raw.lower():
            symbols.add(raw)
    return sorted(symbols)


def _package_short_name(name: str) -> str:
    """Короткое имя пакета (последний компонент после : или /)."""
    text = name.strip().lower()
    for separator in (":", "/"):
        text = text.rsplit(separator, 1)[-1]
    return text
