"""Поиск целевой функции/метода в графе по имени и каталогу пакета."""

from __future__ import annotations

from reachfinder._graph import CodeGraph, CodeUnit, NodeKind, is_under_any, normalize_path
from reachfinder._models import NodeRef


def find_target_nodes(graph: CodeGraph, function: str, package_source_dir: str) -> list[CodeUnit]:
    """Найти узлы-кандидаты целевой функции внутри каталога исходников пакета.

    Возвращает функции/методы, чей ``file_path`` лежит под ``package_source_dir``
    и чьё имя/квалифицированное имя совпадает с ``function`` (см. ``_matches``).
    """
    # Нормализуем корень один раз, чтобы не вызывать abspath/normcase на каждый узел.
    norm_root = [normalize_path(package_source_dir)]
    matches: list[CodeUnit] = []
    for node in graph.nodes.values():
        if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue
        if node.location is None or not is_under_any(node.location.file_path, norm_root):
            continue
        if _matches(function, node):
            matches.append(node)
    return matches


def _matches(function: str, node: CodeUnit) -> bool:
    """Совпадает ли имя узла с запрошенным именем функции.

    Поддерживаются формы:

    * ``get`` — имя функции или метода;
    * ``Class.method`` — метод конкретного класса;
    * ``pkg.mod.func`` — квалифицированное имя (по последнему сегменту и
      совпадению остатка с идентификатором узла).
    """
    if node.name == function:
        return True
    if node.id.endswith(f":{function}"):
        return True
    if node.id.endswith(f".{function}"):
        return True

    if "." in function:
        prefix, _, last = function.rpartition(".")
        if node.name == last and (prefix in node.id or prefix in node.name):
            return True
    return False


def to_node_ref(node: CodeUnit) -> NodeRef:
    """Построить лёгкую ссылку на узел для отчёта."""
    location = node.location
    return NodeRef(
        node_id=node.id,
        name=node.name,
        kind=node.kind.value if node.kind is not None else "",
        file_path=location.file_path if location is not None else "",
        start_line=location.start_line if location is not None else 0,
        end_line=location.end_line if location is not None else 0,
    )
