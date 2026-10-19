"""Тесты поиска целевой функции в графе."""

from __future__ import annotations

from trailmark.models import CodeUnit, NodeKind

from reachfinder._target import find_package_nodes, find_target_nodes, to_node_ref


def test_find_function_by_name(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("pkg:vuln", "vuln", NodeKind.FUNCTION, file_path="/dep/pkg/__init__.py"),
            make_unit("other:helper", "helper", NodeKind.FUNCTION, file_path="/project/helper.py"),
        ]
    )

    nodes = find_target_nodes(graph, "vuln", "/dep")

    assert [node.id for node in nodes] == ["pkg:vuln"]


def test_find_method_by_class_method(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit(
                "pkg:Auth.verify",
                "verify",
                NodeKind.METHOD,
                file_path="/dep/pkg/auth.py",
            ),
        ]
    )

    nodes = find_target_nodes(graph, "Auth.verify", "/dep")

    assert [node.id for node in nodes] == ["pkg:Auth.verify"]


def test_ignores_nodes_outside_package(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("pkg:vuln", "vuln", NodeKind.FUNCTION, file_path="/dep/pkg/__init__.py"),
            make_unit("project:vuln", "vuln", NodeKind.FUNCTION, file_path="/project/vuln.py"),
        ]
    )

    nodes = find_target_nodes(graph, "vuln", "/dep")

    assert [node.id for node in nodes] == ["pkg:vuln"]


def test_find_package_nodes_returns_all_package_functions(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("pkg:two", "two", NodeKind.FUNCTION, file_path="/dep/pkg/b.py", start_line=5),
            make_unit("pkg:one", "one", NodeKind.FUNCTION, file_path="/dep/pkg/a.py", start_line=1),
            make_unit(
                "pkg:Auth.verify",
                "verify",
                NodeKind.METHOD,
                file_path="/dep/pkg/auth.py",
                start_line=2,
            ),
            make_unit("project:main", "main", NodeKind.FUNCTION, file_path="/project/main.py"),
        ]
    )

    nodes = find_package_nodes(graph, "/dep")

    # Все функции и методы пакета, отсортированные по файлу и строке.
    assert [node.id for node in nodes] == ["pkg:one", "pkg:Auth.verify", "pkg:two"]


def test_find_package_nodes_skips_non_callable_nodes(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("pkg:Const", "Const", NodeKind.CLASS, file_path="/dep/pkg/a.py"),
            make_unit("pkg:mod", "mod", NodeKind.MODULE, file_path="/dep/pkg/a.py"),
        ]
    )

    assert find_package_nodes(graph, "/dep") == []


def test_to_node_ref(make_unit) -> None:
    unit: CodeUnit = make_unit(
        "pkg:vuln",
        "vuln",
        NodeKind.FUNCTION,
        file_path="/dep/pkg/__init__.py",
        start_line=3,
        end_line=5,
    )

    ref = to_node_ref(unit)

    assert ref.node_id == "pkg:vuln"
    assert ref.name == "vuln"
    assert ref.kind == "function"
    assert ref.start_line == 3
