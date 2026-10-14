"""Общие фикстуры тестов reachfinder."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from trailmark.models import CodeGraph, CodeUnit, NodeKind, SourceLocation


@pytest.fixture
def make_unit() -> Callable[..., CodeUnit]:
    """Фабрика узлов графа для синтетических тестов."""

    def _make(
        node_id: str,
        name: str,
        kind: NodeKind = NodeKind.FUNCTION,
        *,
        file_path: str = "x.py",
        start_line: int = 1,
        end_line: int = 1,
        attributes: tuple[tuple[str, str], ...] = (),
    ) -> CodeUnit:
        return CodeUnit(
            id=node_id,
            name=name,
            kind=kind,
            location=SourceLocation(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
            ),
            attributes=attributes,
        )

    return _make


@pytest.fixture
def make_graph(make_unit: Callable[..., CodeUnit]) -> Callable[..., CodeGraph]:
    """Фабрика пустого графа с уже добавленными узлами."""

    def _make(units: list[CodeUnit] | None = None) -> CodeGraph:
        graph = CodeGraph(language="python", root_path="")
        for unit in units or []:
            graph.nodes[unit.id] = unit
        return graph

    return _make
