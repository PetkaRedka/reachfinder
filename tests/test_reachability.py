"""Интеграционный тест графового конвейера (без сети, на локальных файлах)."""

from __future__ import annotations

from pathlib import Path

import pytest
from trailmark.analysis.entrypoints import detect_entrypoints
from trailmark.models import NodeKind
from trailmark.storage.graph_store import GraphStore

from reachfinder._boundary import link_boundaries
from reachfinder._graph import merge_graphs, parse_graph, prefix_graph
from reachfinder._models import NodeRef
from reachfinder._reachability import _conventional_entrypoints, _diagnose
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
