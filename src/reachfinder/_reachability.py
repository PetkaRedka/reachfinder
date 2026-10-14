"""Оркестрация анализа достижимости: от локфайлов до итогового отчёта.

Полный конвейер:

1. определить язык/экосистему;
2. получить полный closure зависимостей из локфайлов;
3. скачать исходники всех зависимостей в кэш;
4. построить графы проекта и каждой зависимости, склеить их;
5. связать нерешённые вызовы с определениями в зависимостях;
6. найти целевую функцию, вычислить пути от точек входа и диагноз.
"""

from __future__ import annotations

import os

from trailmark.analysis.entrypoints import detect_entrypoints
from trailmark.parse import detect_languages
from trailmark.storage.graph_store import GraphStore

from reachfinder._boundary import link_boundaries
from reachfinder._cache import PkgCache
from reachfinder._graph import (
    CodeGraph,
    CodeUnit,
    EdgeKind,
    NodeKind,
    merge_graphs,
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
from reachfinder._sources import SourceError, get_provider
from reachfinder._target import find_target_nodes, to_node_ref

_PROJECT_PREFIX = "project"
_MAX_REFS = 20
_MAX_MISSING = 10
# Ограничение числа перебираемых маршрутов (защита от взрыва путей на плотных графах).
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
    function: str,
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

    :param package_depth: ограничить closure глубиной от прямых зависимостей
        (``None``/``0`` — без ограничения, ``1`` — только прямые зависимости).
    :param offline: использовать только уже скачанные исходники из кэша.
    :param jobs: число процессов для параллельного парсинга (``None``/``0`` — авто).
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
    project_entrypoint_ids = [
        *project_graph.entrypoints,
        *_conventional_entrypoints(project_graph, set(project_graph.entrypoints)),
    ]
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

    _log(logger, "Склейка нерешённых вызовов с определениями графа...")
    linked = link_boundaries(merged)
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

    target_nodes = find_target_nodes(merged, function, target_source_dir)
    if not target_nodes:
        _log(logger, "  Целевая функция не найдена в пакете.")
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
            function,
            [],
            [],
            coords,
            diagnosis=diagnosis,
            stats=stats,
            entrypoints=entrypoint_refs,
            missing_packages=list(failed),
            unlinked_boundaries=unlinked,
        )

    store = GraphStore(merged)
    entrypoint_ids = [f"{_PROJECT_PREFIX}::{ep_id}" for ep_id in project_entrypoint_ids]

    target_ids = {node.id for node in target_nodes}
    callers = _direct_callers(merged, target_ids)
    ancestors: set[str] = set()
    for target in target_nodes:
        ancestors |= store.ancestors_of(target.id)
    project_reachers = _project_reachers(merged, ancestors)

    entrypoint_paths: list[EntryPointPath] = []
    for target in target_nodes:
        if len(entrypoint_paths) >= _MAX_PATHS:
            break
        target_ancestors = store.ancestors_of(target.id)
        for ep_id in entrypoint_ids:
            if len(entrypoint_paths) >= _MAX_PATHS:
                break
            if ep_id not in target_ancestors:
                continue
            ep_node = merged.nodes.get(ep_id)
            for raw_path in store.paths_between(ep_id, target.id, max_depth=max_depth):
                entrypoint_paths.append(
                    EntryPointPath(
                        entrypoint=to_node_ref(ep_node) if ep_node else _empty_ref(ep_id),
                        path=[_ref_for(merged, node_id) for node_id in raw_path],
                    )
                )
                if len(entrypoint_paths) >= _MAX_PATHS:
                    break

    reachable = bool(entrypoint_paths)
    diagnosis = _diagnose(reachable, ancestors, project_reachers)
    stats["callers"] = len(callers)
    _log(logger, f"  Найдено маршрутов: {len(entrypoint_paths)} (диагноз: {diagnosis})")

    return _report(
        root,
        trailmark_lang,
        ecosystem,
        target_coord,
        function,
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
    """Дополнить точки входа языковой конвенцией: имя ``main`` без учёта регистра."""
    result: list[str] = []
    for node in graph.nodes.values():
        if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue
        if node.id in detected:
            continue
        if node.name.lower() == "main":
            result.append(node.id)
    return result


def _find_coordinate(
    coords: list[DependencyCoordinate],
    package: str,
    ecosystem: str,
) -> DependencyCoordinate:
    """Найти координату целевого пакета по имени (регистронезависимо)."""
    needle = package.strip().lower()
    for coord in coords:
        name = coord.name.lower()
        if name == needle:
            return coord
        # Для Maven/NuGet допускаем ввод только имени артефакта (без group).
        if ":" in coord.name and coord.name.rsplit(":", 1)[-1].lower() == needle:
            return coord
    raise SourceError(
        f"Пакет '{package}' не найден в closure зависимостей экосистемы '{ecosystem}'."
    )


def _graph_prefix(coord: DependencyCoordinate) -> str:
    """Уникальный префикс идентификаторов узлов графа зависимости."""
    return f"{coord.name}@{coord.version}"


def _direct_callers(graph: CodeGraph, target_ids: set[str]) -> list[NodeRef]:
    """Функции, вызывающие цель напрямую (в т. ч. через proxy-узлы).

    Рёбра к цели из других пакетов идут через proxy-узел
    (``proxy.unresolved:*``); чтобы отчёт был осмысленным, вместо самого
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

    Проектные узлы определяются по префиксу идентификатора (``project::``),
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
) -> ReachabilityReport:
    return ReachabilityReport(
        project=os.path.basename(root.rstrip(os.sep)) or root,
        language=language,
        ecosystem=ecosystem,
        package=coord.name,
        function=function,
        reachable=bool(entrypoint_paths),
        diagnosis=diagnosis,
        target_nodes=[to_node_ref(node) for node in target_nodes],
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
    """Имена пакетов, достижимых от корней за не более чем ``max_depth`` шагов."""
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
    """Проверить, что все зависимости уже есть в кэше (для ``--offline``)."""
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
    """Распарсить несколько графов в процессах (``None`` при ошибке парсинга)."""
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
    """Распарсить один граф (``None`` при ошибке) — последовательный путь."""
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
    """Короткое имя пакета (последний компонент после ``:`` или ``/``)."""
    text = name.strip().lower()
    for separator in (":", "/"):
        text = text.rsplit(separator, 1)[-1]
    return text
