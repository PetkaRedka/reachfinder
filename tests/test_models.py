"""Тесты моделей данных."""

from __future__ import annotations

from reachfinder._models import (
    DependencyCoordinate,
    EntryPointPath,
    NodeRef,
    ReachabilityReport,
    make_purl,
)


def test_make_purl_plain_name() -> None:
    assert make_purl("pypi", "requests", "2.25.1") == "pkg:pypi/requests@2.25.1"


def test_make_purl_encodes_slashes() -> None:
    # Пути Go-модулей содержат '/' и кодируются в purl.
    purl = make_purl("golang", "example.com/foo/bar", "v1.2.3")
    assert "example.com%2Ffoo%2Fbar" in purl


def test_coordinate_roundtrip() -> None:
    coord = DependencyCoordinate(ecosystem="python", name="requests", version="2.25.1")
    restored = DependencyCoordinate.from_dict(coord.to_dict())
    assert restored == coord
    assert restored.purl == coord.purl


def test_reachability_report_roundtrip() -> None:
    report = ReachabilityReport(
        project="demo",
        language="python",
        ecosystem="python",
        package="requests",
        function="get",
        reachable=True,
        target_nodes=[NodeRef(node_id="requests.api:get", name="get", kind="function")],
        entrypoint_paths=[
            EntryPointPath(
                entrypoint=NodeRef(node_id="app:main", name="main"),
                path=[
                    NodeRef(node_id="app:main", name="main"),
                    NodeRef(node_id="requests.api:get", name="get"),
                ],
            )
        ],
        dependencies=[DependencyCoordinate(ecosystem="python", name="requests", version="2.25.1")],
        diagnosis="reachable",
        stats={"packages": 3, "graph_nodes": 10, "graph_edges": 12},
        entrypoints=[NodeRef(node_id="app:main", name="main")],
        callers=[NodeRef(node_id="app:helper", name="helper")],
        project_reachers=[NodeRef(node_id="app:helper", name="helper")],
    )

    restored = ReachabilityReport.from_dict(report.to_dict())
    assert restored.reachable is True
    assert restored.diagnosis == "reachable"
    assert restored.target_nodes[0].name == "get"
    assert len(restored.entrypoint_paths) == 1
    assert restored.dependencies[0].name == "requests"
    assert restored.stats["packages"] == 3
    assert restored.entrypoints[0].name == "main"
    assert restored.callers[0].name == "helper"
    assert restored.project_reachers[0].name == "helper"
