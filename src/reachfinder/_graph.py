"""Построение и объединение графов trailmark.

Проект и каждая зависимость парсятся по отдельности (корень парсинга —
каталог исходников пакета). Чтобы при объединении графов идентификаторы
узлов из разных пакетов не пересекались (например, ``index.js`` из двух
npm-пакетов), каждому подграфу присваивается уникальный префикс.
"""

from __future__ import annotations

import dataclasses
import os

from trailmark.models import CodeEdge, CodeGraph, CodeUnit, EdgeKind, NodeKind

__all__ = [
    "CodeEdge",
    "CodeGraph",
    "CodeUnit",
    "EdgeKind",
    "NodeKind",
    "merge_graphs",
    "normalize_path",
    "parse_graph",
    "prefix_graph",
    "read_attribute",
]


def parse_graph(path: str, language: str) -> CodeGraph:
    """Построить граф кода каталога через trailmark."""
    from trailmark import parse_directory

    return parse_directory(path, language=language)


def prefix_graph(graph: CodeGraph, prefix: str) -> CodeGraph:
    """Переписать идентификаторы узлов графа с уникальным префиксом.

    Переписываются id узлов, концы рёбер и ключи ``annotations``/``entrypoints``/
    ``subgraphs``. Имена узлов (``name``) и пути файлов не меняются — на них
    опираются поиск целевой функции и склейка границ.
    """
    mapping = {old_id: f"{prefix}::{old_id}" for old_id in graph.nodes}

    graph.nodes = {
        mapping[node.id]: dataclasses.replace(node, id=mapping[node.id])
        for node in graph.nodes.values()
    }
    graph.edges = [
        dataclasses.replace(
            edge,
            source_id=mapping.get(edge.source_id, edge.source_id),
            target_id=mapping.get(edge.target_id, edge.target_id),
        )
        for edge in graph.edges
    ]
    graph.annotations = {
        mapping.get(node_id, node_id): anns for node_id, anns in graph.annotations.items()
    }
    graph.entrypoints = {
        mapping.get(node_id, node_id): tag for node_id, tag in graph.entrypoints.items()
    }
    graph.subgraphs = {
        name: {mapping.get(node_id, node_id) for node_id in node_ids}
        for name, node_ids in graph.subgraphs.items()
    }
    return graph


def merge_graphs(graphs: list[CodeGraph], language: str) -> CodeGraph:
    """Объединить несколько графов в один (без копирования метаданных root)."""
    merged = CodeGraph(language=language, root_path="")
    for graph in graphs:
        merged.merge(graph)
    return merged


def read_attribute(node: CodeUnit, key: str) -> str:
    """Прочитать строковый атрибут узла из кортежа ``attributes``."""
    for attr_key, attr_value in node.attributes:
        if attr_key == key:
            return str(attr_value)
    return ""


def normalize_path(path: str) -> str:
    """Нормализовать путь для сравнения префиксов (регистронезависимо на Windows)."""
    return os.path.normcase(os.path.abspath(path))


def is_under_any(path: str, normalized_roots: list[str]) -> bool:
    """Принадлежит ли ``path`` одному из заранее нормализованных корней.

    Корни нормализуются один раз на старте (см. :func:`normalize_path`), чтобы
    не повторять ``abspath``/``normcase`` на каждый узел графа.
    """
    norm_path = normalize_path(path)
    for root in normalized_roots:
        try:
            if os.path.commonpath([norm_path, root]) == root:
                return True
        except ValueError:
            continue
    return False
