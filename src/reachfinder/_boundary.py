"""Склейка нерешённых вызовов (proxy-узлов) с определениями в зависимостях.

trailmark оставляет вызовы вида ``requests.get(...)`` и вызовы через импорт
``from requests import get; get()`` нерешёнными — они превращаются в
proxy-узлы ``proxy.unresolved:<символ>``. Этот модуль связывает такие узлы с
реальными функциями/методами зависимостей, чтобы обход графа вызовов мог
пройти сквозь границу «проект → зависимость».
"""

from __future__ import annotations

from collections import defaultdict

from trailmark.models import CodeEdge, EdgeConfidence, EdgeKind, NodeKind

from reachfinder._graph import CodeGraph, CodeUnit, read_attribute

# Максимум локальных реализаций интерфейсного метода в одном пакете/модуле, при
# котором допускается склейка со всеми одноимёнными методами (иначе граф взрывается).
_MAX_INTERFACE_IMPLS = 8


def link_boundaries(graph: CodeGraph) -> int:
    """Связать proxy-узлы нерешённых вызовов с определениями в графе.

    trailmark оставляет нерешёнными вызовы за пределами текущего пакета:
    обращения к зависимостям, а также межпакетные вызовы внутри проекта
    (например, `pkg.Func()` из другого каталога). Индексируются определения
    из **всего** объединённого графа (и проект, и зависимости), иначе внутренние
    межпакетные вызовы проекта теряются и достижимость не находится.

    :param graph: объединённый граф (проект + зависимости, с префиксами узлов).
    :returns: количество добавленных рёбер склейки.
    """
    name_index: dict[str, list[CodeUnit]] = defaultdict(list)
    for node in graph.nodes.values():
        if node.kind in (NodeKind.FUNCTION, NodeKind.METHOD) and node.location is not None:
            name_index[node.name].append(node)

    added = 0
    for edge in list(graph.edges):
        if edge.kind != EdgeKind.CALLS:
            continue
        proxy = graph.nodes.get(edge.target_id)
        if proxy is None or proxy.kind != NodeKind.PROXY:
            continue

        raw_symbol = read_attribute(proxy, "raw_symbol") or proxy.name
        prefix = _package_prefix(edge.target_id)
        for resolved in _resolve(raw_symbol, name_index, prefix):
            graph.edges.append(
                CodeEdge(
                    source_id=edge.target_id,
                    target_id=resolved.id,
                    kind=EdgeKind.CALLS,
                    confidence=EdgeConfidence.INFERRED,
                )
            )
            added += 1
    return added


def _package_prefix(node_id: str) -> str:
    """Префикс пакета/модуля в идентификаторе узла (``project``, ``pkg@1.0``)."""
    return node_id.split("::", 1)[0] if "::" in node_id else ""


def _resolve(
    raw_symbol: str,
    name_index: dict[str, list[CodeUnit]],
    prefix: str = "",
) -> list[CodeUnit]:
    """Подобрать узлы-цели для нерешённого символа (best-effort).

    Приёмник может быть пакетом/классом/именем файла — тогда среди одноимённых
    определений выбирается подходящее. Если приёмник — переменная (интерфейсный
    вызов, тип статически неизвестен), допускается **ограниченная** переоценка:
    связываем со всеми одноимёнными методами **того же пакета/модуля**, что и
    вызов, если их немного (``_MAX_INTERFACE_IMPLS``). Так реализация
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

    Префикс модуля вызывающего (``myapp.main:get``) отбрасывается — он не
    несёт информации об определении вызываемой функции. Разделитель ``::``
    (C++/C#) имеет приоритет над ``.``.
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
    """Совпадает ли приёмник с путём/именем файла определения.

    Учитываются полное имя приёмника и его последний компонент (``DemoLib.Top``
    → ``Top``), а также сегменты каталогов (имя пакета) и имя файла без
    расширения — для Java/C# это имя класса (``Vuln.cs`` → ``Vuln``).
    """
    if node.location is None:
        return False
    path = node.location.file_path.replace("\\", "/")
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    segments = [segment.lower() for segment in path.split("/")]
    for candidate in _receiver_candidates(receiver):
        if candidate == stem:
            return True
        for low in segments:
            if low == candidate:
                return True
            # Каталог sdist имеет вид <имя>-<версия>, модульного кэша — <имя>@<версия>.
            if low.startswith(f"{candidate}-") or low.startswith(f"{candidate}@"):
                return True
    return False


def _receiver_candidates(receiver: str) -> list[str]:
    """Варианты имени приёмника: полное имя и последний компонент."""
    text = receiver.strip()
    candidates = [text.lower()]
    for separator in ("::", "."):
        if separator in text:
            candidates.append(text.rsplit(separator, 1)[-1].lower())
    return candidates
