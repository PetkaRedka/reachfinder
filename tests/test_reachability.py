"""Интеграционный тест графового конвейера (без сети, на локальных файлах)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from trailmark.analysis.entrypoints import detect_entrypoints
from trailmark.models import (
    CodeEdge,
    CodeGraph,
    CodeUnit,
    EdgeConfidence,
    EdgeKind,
    NodeKind,
)
from trailmark.storage.graph_store import GraphStore

from reachfinder._boundary import link_boundaries
from reachfinder._graph import merge_graphs, parse_graph, prefix_graph
from reachfinder._models import NodeRef
from reachfinder._reachability import (
    _conventional_entrypoints,
    _diagnose,
    _forward_calls_adjacency,
    _forward_reachable,
    _package_entrypoints,
    _reconstruct_forward_path,
    _reconstruct_path,
    _reverse_calls_adjacency,
    _reverse_reachable,
    _reverse_shortest_tree,
)
from reachfinder._target import find_target_nodes


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build(tmp_path: Path) -> tuple[GraphStore, list[str], str]:
    project = tmp_path / "project"
    dep = tmp_path / "dep"
    _write(project / "app.py", "def main():\n    return pkg.do_work()\n")
    _write(
        dep / "pkg" / "__init__.py",
        "def do_work():\n    return vuln()\n\n\ndef vuln():\n    return 'boom'\n",
    )

    project_graph = parse_graph(str(project), "python")
    project_graph.entrypoints.update(detect_entrypoints(project_graph, str(project)))
    entrypoint_ids = list(project_graph.entrypoints)

    dep_graph = parse_graph(str(dep), "python")
    merged = merge_graphs(
        [prefix_graph(project_graph, "project"), prefix_graph(dep_graph, "pkg@1.0")],
        "python",
    )
    link_boundaries(merged)

    return GraphStore(merged), [f"project::{ep_id}" for ep_id in entrypoint_ids], str(dep)


def test_entrypoint_detected(tmp_path: Path) -> None:
    _, entrypoints, _ = _build(tmp_path)
    assert entrypoints, "main() должен быть обнаружен как точка входа"


def test_vulnerable_function_reachable(tmp_path: Path) -> None:
    store, entrypoints, dep_root = _build(tmp_path)

    targets = find_target_nodes(store._graph, "vuln", dep_root)  # noqa: SLF001
    assert len(targets) == 1

    ancestors = store.ancestors_of(targets[0].id)
    assert set(entrypoints) & ancestors, "точка входа должна достигать цели"

    paths = [
        path for ep_id in entrypoints for path in store.paths_between(ep_id, targets[0].id)
    ]
    assert paths, "должен существовать путь от точки входа до цели"
    # Путь идёт через proxy-узел нерешённого вызова pkg.do_work.
    assert any("proxy.unresolved:pkg.do_work" in node_id for node_id in paths[0])


def test_unrelated_function_not_reachable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    dep = tmp_path / "dep"
    _write(project / "app.py", "def main():\n    return 'hi'\n")
    _write(dep / "pkg" / "__init__.py", "def vuln():\n    return 'boom'\n")

    project_graph = parse_graph(str(project), "python")
    project_graph.entrypoints.update(detect_entrypoints(project_graph, str(project)))
    entrypoints = [f"project::{ep_id}" for ep_id in project_graph.entrypoints]

    dep_graph = parse_graph(str(dep), "python")
    merged = merge_graphs(
        [prefix_graph(project_graph, "project"), prefix_graph(dep_graph, "pkg@1.0")],
        "python",
    )
    link_boundaries(merged)
    store = GraphStore(merged)

    targets = find_target_nodes(merged, "vuln", str(dep))
    assert targets

    ancestors = store.ancestors_of(targets[0].id)
    assert not (set(entrypoints) & ancestors), "нет вызова — не должно быть достижимости"


def test_conventional_entrypoints_detect_csharp_main(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit("Program:Program.Main", "Main", NodeKind.METHOD, file_path="Program.cs"),
            make_unit("app:helper", "helper", NodeKind.FUNCTION, file_path="helper.py"),
        ]
    )

    assert _conventional_entrypoints(graph, set()) == ["Program:Program.Main"]


def test_conventional_entrypoints_skip_already_detected(make_unit, make_graph) -> None:
    graph = make_graph(
        [make_unit("app:main", "main", NodeKind.FUNCTION, file_path="app.py")]
    )

    assert _conventional_entrypoints(graph, {"app:main"}) == []


def test_package_entrypoints_js(tmp_path: Path, make_unit, make_graph) -> None:
    (tmp_path / "package.json").write_text(
        '{"bin": {"cli": "./bin/cli.js"}, "main": "./lib"}',
        encoding="utf-8",
    )
    graph = make_graph(
        [
            make_unit(
                "bin.cli",
                "bin.cli",
                NodeKind.MODULE,
                file_path=str(tmp_path / "bin" / "cli.js"),
            ),
            make_unit(
                "lib.index",
                "lib.index",
                NodeKind.MODULE,
                file_path=str(tmp_path / "lib" / "index.js"),
            ),
            make_unit(
                "app:main",
                "main",
                NodeKind.FUNCTION,
                file_path=str(tmp_path / "app.py"),
            ),
        ]
    )

    assert set(_package_entrypoints(graph, str(tmp_path), "javascript")) == {
        "bin.cli",
        "lib.index",
    }
    assert _package_entrypoints(graph, str(tmp_path), "python") == []


@pytest.mark.parametrize(
    ("reachable", "has_ancestors", "has_project", "expected"),
    [
        (True, True, True, "reachable"),
        (False, False, False, "no_callers"),
        (False, True, False, "deps_only"),
        (False, True, True, "project_not_entrypoint"),
    ],
)
def test_diagnose(
    reachable: bool,
    has_ancestors: bool,
    has_project: bool,
    expected: str,
) -> None:
    ancestors = {"x"} if has_ancestors else set()
    project = [NodeRef(node_id="p", name="p")] if has_project else []

    assert _diagnose(reachable, ancestors, project) == expected


def _add_calls(
    make_graph: Callable[..., CodeGraph],
    units: list[CodeUnit],
    edges: list[tuple[str, str]],
) -> CodeGraph:
    """Собрать граф с рёбрами CALLS для тестов обхода."""
    graph = make_graph(units)
    for src, dst in edges:
        graph.edges.append(
            CodeEdge(
                source_id=src,
                target_id=dst,
                kind=EdgeKind.CALLS,
                confidence=EdgeConfidence.INFERRED,
            )
        )
    return graph


def test_reverse_shortest_tree_gives_shortest_witness(make_unit, make_graph) -> None:
    graph = _add_calls(
        make_graph,
        [
            make_unit("project:main", "main"),
            make_unit("project:a", "a"),
            make_unit("project:b", "b"),
            make_unit("dep:vuln", "vuln"),
        ],
        [
            ("project:main", "project:a"),
            ("project:a", "dep:vuln"),
            ("project:main", "project:b"),
        ],
    )

    reverse = _reverse_calls_adjacency(graph)
    next_hop = _reverse_shortest_tree(reverse, "dep:vuln", max_depth=20)

    assert set(next_hop) == {"dep:vuln", "project:a", "project:main"}
    assert next_hop["dep:vuln"] is None
    assert _reconstruct_path(next_hop, "project:main") == [
        "project:main",
        "project:a",
        "dep:vuln",
    ]


def test_reverse_shortest_tree_respects_depth(make_unit, make_graph) -> None:
    graph = _add_calls(
        make_graph,
        [
            make_unit("project:main", "main"),
            make_unit("project:a", "a"),
            make_unit("dep:vuln", "vuln"),
        ],
        [("project:main", "project:a"), ("project:a", "dep:vuln")],
    )

    reverse = _reverse_calls_adjacency(graph)
    next_hop = _reverse_shortest_tree(reverse, "dep:vuln", max_depth=1)

    # За один шаг от цели достижим только project:a, project:main — уже нет.
    assert set(next_hop) == {"dep:vuln", "project:a"}


def test_reverse_calls_adjacency_ignores_non_call_edges(make_unit, make_graph) -> None:
    graph = make_graph([make_unit("a", "a"), make_unit("b", "b")])
    graph.edges.append(
        CodeEdge(
            source_id="a",
            target_id="b",
            kind=EdgeKind.IMPORTS,
            confidence=EdgeConfidence.INFERRED,
        )
    )

    assert _reverse_calls_adjacency(graph) == {}


def test_forward_calls_adjacency_ignores_non_call_edges(make_unit, make_graph) -> None:
    graph = make_graph([make_unit("a", "a"), make_unit("b", "b")])
    graph.edges.append(
        CodeEdge(
            source_id="a",
            target_id="b",
            kind=EdgeKind.IMPORTS,
            confidence=EdgeConfidence.INFERRED,
        )
    )

    assert _forward_calls_adjacency(graph) == {}


def test_forward_reachable_finds_package_targets(make_unit, make_graph) -> None:
    graph = _add_calls(
        make_graph,
        [
            make_unit("project:main", "main"),
            make_unit("project:a", "a"),
            make_unit("dep:one", "one"),
            make_unit("dep:two", "two"),
        ],
        [
            ("project:main", "project:a"),
            ("project:a", "dep:one"),
            ("project:a", "dep:two"),
        ],
    )

    forward = _forward_calls_adjacency(graph)
    depths, parents = _forward_reachable(forward, ["project:main"], max_depth=20)

    assert set(depths) == {"project:main", "project:a", "dep:one", "dep:two"}
    assert depths["dep:one"] == 2
    assert depths["dep:two"] == 2
    assert _reconstruct_forward_path(parents, "dep:one") == [
        "project:main",
        "project:a",
        "dep:one",
    ]


def test_forward_reachable_respects_depth(make_unit, make_graph) -> None:
    graph = _add_calls(
        make_graph,
        [
            make_unit("project:main", "main"),
            make_unit("dep:one", "one"),
            make_unit("dep:two", "two"),
        ],
        [("project:main", "dep:one"), ("dep:one", "dep:two")],
    )

    forward = _forward_calls_adjacency(graph)
    depths, _parents = _forward_reachable(forward, ["project:main"], max_depth=1)

    assert set(depths) == {"project:main", "dep:one"}


def test_forward_reachable_multiple_entrypoints(make_unit, make_graph) -> None:
    graph = _add_calls(
        make_graph,
        [
            make_unit("project:main", "main"),
            make_unit("project:worker", "worker"),
            make_unit("dep:one", "one"),
        ],
        [
            ("project:main", "dep:one"),
            ("project:worker", "dep:one"),
        ],
    )

    forward = _forward_calls_adjacency(graph)
    depths, parents = _forward_reachable(forward, ["project:main", "project:worker"], 20)

    assert depths["dep:one"] == 1
    assert parents["dep:one"] in {"project:main", "project:worker"}
    assert parents["project:main"] is None
    assert parents["project:worker"] is None


def test_reverse_reachable_collects_ancestors(make_unit, make_graph) -> None:
    graph = _add_calls(
        make_graph,
        [
            make_unit("project:main", "main"),
            make_unit("project:a", "a"),
            make_unit("dep:one", "one"),
            make_unit("dep:two", "two"),
        ],
        [
            ("project:main", "project:a"),
            ("project:a", "dep:one"),
            ("dep:two", "dep:one"),
        ],
    )

    reverse = _reverse_calls_adjacency(graph)
    reachers = _reverse_reachable(reverse, {"dep:one"}, max_depth=20)

    assert reachers == {"dep:one", "project:a", "project:main", "dep:two"}


def test_reverse_reachable_respects_depth(make_unit, make_graph) -> None:
    graph = _add_calls(
        make_graph,
        [
            make_unit("project:main", "main"),
            make_unit("project:a", "a"),
            make_unit("dep:one", "one"),
        ],
        [("project:main", "project:a"), ("project:a", "dep:one")],
    )

    reverse = _reverse_calls_adjacency(graph)
    reachers = _reverse_reachable(reverse, {"dep:one"}, max_depth=1)

    assert reachers == {"dep:one", "project:a"}
