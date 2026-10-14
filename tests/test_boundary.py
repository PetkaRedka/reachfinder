"""Тесты склейки нерешённых вызовов (proxy-узлов) с зависимостями."""

from __future__ import annotations

import pytest
from trailmark.models import CodeEdge, EdgeKind, NodeKind

from reachfinder._boundary import _symbol_parts, link_boundaries


def _proxy_edge(graph, source_id: str, proxy_id: str) -> None:
    graph.edges.append(CodeEdge(source_id=source_id, target_id=proxy_id, kind=EdgeKind.CALLS))


def test_links_unique_symbol(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("project::app:main", "main", NodeKind.FUNCTION, file_path="/project/app.py"),
            make_unit(
                "project::proxy.unresolved:pkg.do_work",
                "pkg.do_work",
                NodeKind.PROXY,
                file_path="/project/app.py",
                attributes=(("raw_symbol", "pkg.do_work"),),
            ),
            make_unit(
                "pkg@1.0::pkg:do_work",
                "do_work",
                NodeKind.FUNCTION,
                file_path="/dep/pkg/__init__.py",
            ),
        ]
    )
    _proxy_edge(graph, "project::app:main", "project::proxy.unresolved:pkg.do_work")

    added = link_boundaries(graph)

    assert added == 1
    assert any(
        edge.source_id == "project::proxy.unresolved:pkg.do_work"
        and edge.target_id == "pkg@1.0::pkg:do_work"
        for edge in graph.edges
    )


def test_ambiguous_symbol_not_linked(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("project::app:main", "main", NodeKind.FUNCTION, file_path="/project/app.py"),
            make_unit(
                "project::proxy.unresolved:x.run",
                "x.run",
                NodeKind.PROXY,
                file_path="/project/app.py",
                attributes=(("raw_symbol", "x.run"),),
            ),
            make_unit("a@1::a:run", "run", NodeKind.FUNCTION, file_path="/dep/a/__init__.py"),
            make_unit("b@1::b:run", "run", NodeKind.FUNCTION, file_path="/dep/b/__init__.py"),
        ]
    )
    _proxy_edge(graph, "project::app:main", "project::proxy.unresolved:x.run")

    added = link_boundaries(graph)

    # Приёмник 'x' не совпадает ни с одним каталогом — склейка не выполняется.
    assert added == 0


def test_non_proxy_target_ignored(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("project::app:main", "main", NodeKind.FUNCTION, file_path="/project/app.py"),
            make_unit(
                "project::app:helper",
                "helper",
                NodeKind.FUNCTION,
                file_path="/project/app.py",
            ),
        ]
    )
    _proxy_edge(graph, "project::app:main", "project::app:helper")

    added = link_boundaries(graph)

    assert added == 0


def test_disambiguates_method_by_class(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("project::app:Main", "Main", NodeKind.METHOD, file_path="/project/Main.java"),
            make_unit(
                "project::proxy.unresolved:StringUtils.isEmpty",
                "StringUtils.isEmpty",
                NodeKind.PROXY,
                file_path="/project/Main.java",
                attributes=(("raw_symbol", "StringUtils.isEmpty"),),
            ),
            make_unit(
                "commons::StringUtils.isEmpty",
                "isEmpty",
                NodeKind.METHOD,
                file_path="/dep/org/apache/commons/lang3/StringUtils.java",
            ),
            make_unit(
                "other::CollectionUtils.isEmpty",
                "isEmpty",
                NodeKind.METHOD,
                file_path="/dep/org/apache/commons/collections/CollectionUtils.java",
            ),
        ]
    )
    _proxy_edge(graph, "project::app:Main", "project::proxy.unresolved:StringUtils.isEmpty")

    added = link_boundaries(graph)

    assert added == 1
    assert any(edge.target_id == "commons::StringUtils.isEmpty" for edge in graph.edges)


def test_disambiguates_namespaced_receiver(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit(
                "project::app:Main",
                "Main",
                NodeKind.METHOD,
                file_path="/project/Program.cs",
            ),
            make_unit(
                "project::proxy.unresolved:DemoLibA.Top.Call",
                "DemoLibA.Top.Call",
                NodeKind.PROXY,
                file_path="/project/Program.cs",
                attributes=(("raw_symbol", "DemoLibA.Top.Call"),),
            ),
            make_unit("a::Top.Call", "Call", NodeKind.METHOD, file_path="/dep/a/Top.cs"),
            make_unit("b::Mid.Call", "Call", NodeKind.METHOD, file_path="/dep/b/Mid.cs"),
        ]
    )
    _proxy_edge(graph, "project::app:Main", "project::proxy.unresolved:DemoLibA.Top.Call")

    added = link_boundaries(graph)

    # Приёмник DemoLibA.Top -> последний компонент Top -> файл Top.cs.
    assert added == 1
    assert any(edge.target_id == "a::Top.Call" for edge in graph.edges)


@pytest.mark.parametrize(
    ("raw", "member", "receiver"),
    [
        ("requests.get", "get", "requests"),
        ("Class.method", "method", "Class"),
        ("std::vector::push_back", "push_back", "std::vector"),
        ("index:leftPad", "leftPad", ""),
        ("myapp.main:get", "get", ""),
    ],
)
def test_symbol_parts(raw: str, member: str, receiver: str) -> None:
    assert _symbol_parts(raw) == (member, receiver)
