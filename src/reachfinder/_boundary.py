"""Склейка нерешённых вызовов (proxy-узлов) с определениями в зависимостях.

trailmark оставляет вызовы вида requests.get(...) и вызовы через импорт
from requests import get; get() нерешёнными — они превращаются в
proxy-узлы proxy.unresolved:<символ>. Этот модуль связывает такие узлы с
реальными функциями/методами зависимостей, чтобы обход графа вызовов мог
пройти сквозь границу «проект → зависимость».
"""

from __future__ import annotations

import os
import re
from collections import defaultdict

from trailmark.models import CodeEdge, EdgeConfidence, EdgeKind, NodeKind

from reachfinder._graph import CodeGraph, CodeUnit, read_attribute
from reachfinder._scip import ScipIndex, parse_descriptor_symbol, parse_symbol

# Версия Go-модуля в пути (.../v2) и суффикс gopkg.in (yaml.v3).
_GO_VERSION_RE = re.compile(r"^v\d+$")
_PKG_VERSION_SUFFIX_RE = re.compile(r"\.v\d+$")

# Максимум локальных реализаций интерфейсного метода в одном пакете/модуле, при
# котором допускается склейка со всеми одноимёнными методами (иначе граф взрывается).
_MAX_INTERFACE_IMPLS = 8

# Префиксы корней исходников, которые отбрасываются при вычислении пути класса
# из пути модуля trailmark (src.main.java.com.example.A → com/example/A).
_SOURCE_ROOT_PREFIXES = (
    "src.main.java.",
    "src.test.java.",
    "src.main.kotlin.",
    "src.test.kotlin.",
    "src.",
)


def link_boundaries(graph: CodeGraph, scip: ScipIndex | None = None) -> int:
    """Связать proxy-узлы нерешённых вызовов с определениями в графе.

    Приоритет — резолв по SCIP-индексу (точные символы, кросс-пакетные вызовы).
    Если для узла SCIP ничего не дал, включается name-based резолвер:
    trailmark оставляет нерешёнными межпакетные вызовы проекта и обращения к
    зависимостям, а также вызовы на переменных/интерфейсах.

    :param graph: объединённый граф (проект + зависимости, с префиксами узлов).
    :param scip: прочитанный SCIP-индекс (или None, если недоступен).
    :returns: количество добавленных рёбер склейки.
    """
    name_index: dict[str, list[CodeUnit]] = defaultdict(list)
    for node in graph.nodes.values():
        if node.kind in (NodeKind.FUNCTION, NodeKind.METHOD) and node.location is not None:
            name_index[node.name].append(node)

    resolver = _ScipResolver(graph, scip) if scip is not None else None

    added = 0
    for edge in list(graph.edges):
        if edge.kind != EdgeKind.CALLS:
            continue
        proxy = graph.nodes.get(edge.target_id)
        if proxy is None or proxy.kind != NodeKind.PROXY:
            continue

        targets: list[str] = []
        if resolver is not None and proxy.location is not None:
            targets = resolver.resolve(
                proxy.location.file_path,
                proxy.location.start_line,
                proxy.name,
            )
        if not targets:
            raw_symbol = read_attribute(proxy, "raw_symbol") or proxy.name
            prefix = _package_prefix(edge.target_id)
            targets = [node.id for node in _resolve(raw_symbol, name_index, prefix)]

        for target in targets:
            graph.edges.append(
                CodeEdge(
                    source_id=edge.target_id,
                    target_id=target,
                    kind=EdgeKind.CALLS,
                    confidence=EdgeConfidence.INFERRED,
                )
            )
            added += 1
    if resolver is not None:
        added += _link_module_toplevel(graph, resolver)
    return added


def _link_module_toplevel(graph: CodeGraph, resolver: _ScipResolver) -> int:
    """Связать top-level ссылки модулей с определениями (SCIP)"""
    normalize = os.path.normcase
    function_ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    modules: list[CodeUnit] = []
    for node in graph.nodes.values():
        if node.location is None:
            continue
        if node.kind in (NodeKind.FUNCTION, NodeKind.METHOD):
            abs_file = normalize(os.path.abspath(node.location.file_path))
            function_ranges[abs_file].append(
                (node.location.start_line, node.location.end_line)
            )
        elif node.kind == NodeKind.MODULE:
            modules.append(node)

    existing = {
        (edge.source_id, edge.target_id)
        for edge in graph.edges
        if edge.kind == EdgeKind.CALLS
    }
    added = 0
    for module in modules:
        if module.location is None:
            continue
        file_path = module.location.file_path
        abs_file = normalize(os.path.abspath(file_path))
        ranges = function_ranges.get(abs_file, ())
        for line in resolver.lines(file_path):
            if any(start <= line <= end for start, end in ranges):
                continue
            for target in resolver.resolve(file_path, line):
                key = (module.id, target)
                if target == module.id or key in existing:
                    continue
                existing.add(key)
                graph.edges.append(
                    CodeEdge(
                        source_id=module.id,
                        target_id=target,
                        kind=EdgeKind.CALLS,
                        confidence=EdgeConfidence.INFERRED,
                    )
                )
                added += 1
    return added


class _ScipResolver:
    """Резолв proxy-вызовов по SCIP-индексу: точный символ → узел графа.

    Определения проекта сопоставляются по (файл, строка), а цели в
    зависимостях — по (модуль, квалифицированное имя) (пути у индексора и
    trailmark разные: модульный кэш Go и кэш reachfinder).
    """

    def __init__(self, graph: CodeGraph, scip: ScipIndex) -> None:
        normalize = os.path.normcase
        root = os.path.abspath(scip.root)

        node_by_absline: dict[tuple[str, int], str] = {}
        rel_to_abs: dict[str, str] = {}
        self._dep_by_qual: dict[tuple[str, str], str] = {}
        self._dep_by_class: dict[tuple[str, str], str] = {}
        # Для C/C++ (scip-clang) в символе нет имени пакета (.), поэтому
        # дополнительно держим карту «квалифицированное имя → узел». Неоднозначные
        # имена (одинаковые в разных пакетах) не используем.
        self._dep_by_name: dict[str, str] = {}
        self._ambiguous_names: set[str] = set()
        for node_id, node in graph.nodes.items():
            if "::" in node_id:
                prefix, rest = node_id.split("::", 1)
                if ":" in rest:
                    module = prefix.rsplit("@", 1)[0]
                    full_qual = rest.split(":", 1)[1]
                    self._dep_by_qual[(module, full_qual)] = node_id
                    if full_qual in self._dep_by_name and self._dep_by_name[full_qual] != node_id:
                        self._ambiguous_names.add(full_qual)
                    else:
                        self._dep_by_name[full_qual] = node_id
                module_path, sep, qual = rest.partition(":")
                if sep and qual:
                    class_path = _class_path(module_path)
                    if class_path:
                        member = qual.rsplit(".", 1)[-1]
                        self._dep_by_class.setdefault((class_path, member), node_id)
            if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD) or node.location is None:
                continue
            abs_file = normalize(os.path.abspath(node.location.file_path))
            node_by_absline[(abs_file, node.location.start_line)] = node_id
            rel = os.path.relpath(os.path.abspath(node.location.file_path), root).replace("\\", "/")
            if not rel.startswith(".."):
                rel_to_abs.setdefault(rel, abs_file)

        self._def_node: dict[str, str] = {}
        for symbol, (rel, line0) in scip.defs.items():
            abs_file = rel_to_abs.get(rel)
            if abs_file is None:
                continue
            node_id = node_by_absline.get((abs_file, line0 + 1))
            if node_id is not None:
                self._def_node[symbol] = node_id

        for name in self._ambiguous_names:
            self._dep_by_name.pop(name, None)

        self._refs_abs: dict[tuple[str, int], set[str]] = defaultdict(set)
        for (rel, line0), symbols in scip.refs.items():
            abs_file = rel_to_abs.get(rel)
            if abs_file is None:
                continue
            self._refs_abs[(abs_file, line0 + 1)] |= symbols
        self._lines_by_file: dict[str, list[int]] = defaultdict(list)
        for abs_file, line in self._refs_abs:
            self._lines_by_file[abs_file].append(line)

    def lines(self, file_path: str) -> list[int]:
        """Строки файла, на которых есть SCIP-ссылки."""
        abs_file = os.path.normcase(os.path.abspath(file_path))
        return self._lines_by_file.get(abs_file, [])

    def resolve(self, file_path: str, line: int, hint: str = "") -> list[str]:
        """Найти узлы-цели вызова в file_path на строке line (1-базной).

        hint — имя нерешённого вызова (например, DemoLibA.Top.Call).
        На одной строке может быть несколько вызовов, поэтому кандидаты из
        индекса фильтруются по суффиксу имени: это отсекает ложные дуги
        Console.WriteLine → Top.Call, возникающие при резолве только по строке.
        """
        abs_file = os.path.normcase(os.path.abspath(file_path))
        candidates: dict[str, str] = {}
        for symbol in self._refs_abs.get((abs_file, line), set()):
            node_id = self._def_node.get(symbol)
            if node_id is not None:
                candidates.setdefault(node_id, _symbol_hint(symbol))
                continue
            parsed = parse_symbol(symbol)
            if parsed is not None:
                dep_node = self._dep_by_qual.get(parsed)
                if dep_node is not None:
                    candidates.setdefault(dep_node, parsed[1])
                    continue
                # scip-clang не несёт имени пакета (.): резолвим по имени.
                if parsed[0] == ".":
                    dep_node = self._dep_by_name.get(parsed[1])
                    if dep_node is not None:
                        candidates.setdefault(dep_node, parsed[1])
                        continue
            relative = parse_descriptor_symbol(symbol)
            if relative is not None:
                dep_node = self._dep_by_class.get(relative)
                if dep_node is not None:
                    dotted = f"{relative[0].replace('/', '.')}.{relative[1]}"
                    candidates.setdefault(dep_node, dotted)
        return _filter_by_hint(candidates, hint)


def _class_path(module_path: str) -> str:
    """Путь класса из пути модуля trailmark (src.main.java.com.x.A → com/x/A)."""
    normalized = module_path
    for prefix in _SOURCE_ROOT_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized.replace(".", "/")


def _symbol_hint(symbol: str) -> str:
    """Квалифицированное имя символа (для фильтрации кандидатов по имени вызова)."""
    parsed = parse_symbol(symbol)
    if parsed is not None:
        return parsed[1]
    relative = parse_descriptor_symbol(symbol)
    if relative is not None:
        return f"{relative[0].replace('/', '.')}.{relative[1]}"
    return ""


def _filter_by_hint(candidates: dict[str, str], hint: str) -> list[str]:
    """Оставить кандидатов, чьё имя совпадает с именем вызова по суффиксу."""
    if not hint or not candidates:
        return list(candidates)
    hint_parts = _name_parts(hint)
    if not hint_parts:
        return list(candidates)
    matched = [
        node_id
        for node_id, qualname in candidates.items()
        if _is_component_suffix(_name_parts(qualname), hint_parts)
        or _is_component_suffix(hint_parts, _name_parts(qualname))
        or _same_last_component(_name_parts(qualname), hint_parts)
    ]
    # Строгий фильтр: при несовпадении имён лучше потерять дугу, чем добавить
    # ложную (на одной строке может быть несколько разных вызовов).
    return matched


def _same_last_component(left: list[str], right: list[str]) -> bool:
    """Совпадает ли последний компонент имён."""
    return bool(left) and bool(right) and left[-1] == right[-1]


def _name_parts(text: str) -> list[str]:
    """Разбить квалифицированное имя на компоненты (A#b(). → ["A", "b"])."""
    normalized = text.replace("::", ".").replace("#", ".").replace("/", ".")
    normalized = normalized.replace(":", ".").replace("().", "").replace("()", "")
    return [part for part in normalized.strip(".").split(".") if part and part != "local 0"]


def _is_component_suffix(shorter: list[str], longer: list[str]) -> bool:
    """Является ли shorter суффиксом longer на уровне компонентов."""
    return bool(shorter) and len(shorter) <= len(longer) and longer[-len(shorter) :] == shorter


def _package_prefix(node_id: str) -> str:
    """Префикс пакета/модуля в идентификаторе узла (project, pkg@1.0)."""
    return node_id.split("::", 1)[0] if "::" in node_id else ""


def _resolve(
    raw_symbol: str,
    name_index: dict[str, list[CodeUnit]],
    prefix: str = "",
) -> list[CodeUnit]:
    """Подобрать узлы-цели для нерешённого символа (best-effort).

    Приёмник может быть пакетом/классом/именем файла — тогда среди одноимённых
    определений выбирается подходящее. Если приёмник — переменная (интерфейсный
    вызов, тип статически неизвестен), допускается ограниченная переоценка:
    связываем со всеми одноимёнными методами того же пакета/модуля, что и
    вызов, если их немного (_MAX_INTERFACE_IMPLS). Так реализация
    интерфейса находится, а массовые одноимённые методы из зависимостей граф не
    взрывают.
    """
    member, receiver = _symbol_parts(raw_symbol)
    if not member:
        return []

    candidates = name_index.get(member, [])
    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    # Символ с точкой — пытаемся разобрать по приёмнику (пакет/класс/имя файла).
    if receiver:
        filtered = [node for node in candidates if _receiver_matches(node, receiver)]
        if filtered:
            functions = [node for node in filtered if node.kind == NodeKind.FUNCTION]
            if len(functions) == 1:
                return functions
            if len(filtered) == 1:
                return filtered
            return []

    # Интерфейсный dispatch: локальные (в своём пакете) реализации метода.
    methods = [
        node
        for node in candidates
        if node.kind == NodeKind.METHOD and _package_prefix(node.id) == prefix
    ]
    if 0 < len(methods) <= _MAX_INTERFACE_IMPLS:
        return methods
    return []


def _symbol_parts(raw_symbol: str) -> tuple[str, str]:
    """Выделить имя члена и приёмник из нерешённого символа.

    Префикс модуля вызывающего (myapp.main:get) отбрасывается — он не
    несёт информации об определении вызываемой функции. Разделитель ::
    (C++/C#) имеет приоритет над ..
    """
    symbol = raw_symbol.strip()
    if "::" not in symbol and ":" in symbol:
        symbol = symbol.rsplit(":", 1)[-1].strip()
    for sep in ("::", "."):
        if sep in symbol:
            receiver, _, member = symbol.rpartition(sep)
            return member, receiver
    return symbol, ""


def _receiver_matches(node: CodeUnit, receiver: str) -> bool:
    """Совпадает ли приёмник с путём/именем пакета определения.

    Учитываются полное имя приёмника и его последний компонент (DemoLib.Top
    → Top), сегменты каталогов (имя пакета) и имя файла без расширения —
    для Java/C# это имя класса (Vuln.cs → Vuln). Дополнительно
    сравнивается префикс идентификатора узла: каталог кэша npm-пакета не
    содержит имени пакета (js/src/<hash>/index.js), а идентификатор —
    содержит (chokidar@3.5.3::index:watch).
    """
    if node.location is None:
        return False
    path = node.location.file_path.replace("\\", "/")
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    segments = [segment.lower() for segment in path.split("/")]
    packages = _node_package_names(node.id)
    for candidate in _receiver_candidates(receiver):
        if candidate == stem or candidate in packages:
            return True
        for low in segments:
            if low == candidate:
                return True
            # Каталог sdist имеет вид <имя>-<версия>, модульного кэша — <имя>@<версия>.
            if low.startswith(f"{candidate}-") or low.startswith(f"{candidate}@"):
                return True
    return False


def _node_package_names(node_id: str) -> set[str]:
    """Имена пакета из идентификатора узла (chokidar@3.5.3::x → chokidar).

    Возвращаются полное имя и его компоненты: для Go-модуля
    github.com/gdamore/tcell/v2@v2.1.0 это позволяет сопоставить вызов
    tcell.NewScreen с пакетом, а для Maven — artifact из
    group:artifact. Версионные компоненты (v2, yaml.v3) отбрасываются.
    """
    if "::" not in node_id:
        return set()
    prefix = node_id.split("::", 1)[0].lower()
    name = prefix.rsplit("@", 1)[0]
    names = {name}
    for separator in ("/", ":"):
        names.update(part for part in name.split(separator) if part)
    for part in list(names):
        stripped = _PKG_VERSION_SUFFIX_RE.sub("", part)
        if stripped:
            names.add(stripped)
    return {part for part in names if not _GO_VERSION_RE.match(part)}


def _receiver_candidates(receiver: str) -> list[str]:
    """Варианты имени приёмника: полное имя и последний компонент."""
    text = receiver.strip()
    candidates = [text.lower()]
    for separator in ("::", "."):
        if separator in text:
            candidates.append(text.rsplit(separator, 1)[-1].lower())
    return candidates
