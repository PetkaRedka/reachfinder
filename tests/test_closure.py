"""Тесты графа зависимостей, ограничения по глубине и офлайн-режима (без сети)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reachfinder._cache import PkgCache
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._reachability import _limit_closure, _names_within_depth, _require_cached
from reachfinder._sources import SourceError
from reachfinder._sources_cpp import _parse_graph_json
from reachfinder._sources_go import _module_path
from reachfinder._sources_java import _parse_dependency_tree
from reachfinder._sources_python import _pip_graph, _requirement_name, _toml_lock_graph

# ---------------------------------------------------------------------------
# Ограничение по глубине
# ---------------------------------------------------------------------------


def _coord(name: str) -> DependencyCoordinate:
    return DependencyCoordinate(ecosystem="python", name=name, version="1.0")


def _chain_graph() -> ClosureGraph:
    """Цепочка app -> a -> b -> c (a — прямая зависимость проекта)."""
    return ClosureGraph(
        coords=[_coord("a"), _coord("b"), _coord("c")],
        edges={"a": ["b"], "b": ["c"]},
        roots=["a"],
    )


def test_names_within_depth_one() -> None:
    assert _names_within_depth(_chain_graph(), 1) == {"a"}


def test_names_within_depth_two() -> None:
    assert _names_within_depth(_chain_graph(), 2) == {"a", "b"}


def test_names_within_depth_unbounded_chain() -> None:
    assert _names_within_depth(_chain_graph(), 5) == {"a", "b", "c"}


def test_limit_closure_none_keeps_all() -> None:
    graph = _chain_graph()
    assert _limit_closure(graph, None, None) == graph.coords


def test_limit_closure_zero_keeps_all() -> None:
    graph = _chain_graph()
    assert _limit_closure(graph, 0, None) == graph.coords


def test_limit_closure_depth_one() -> None:
    names = [coord.name for coord in _limit_closure(_chain_graph(), 1, None)]
    assert names == ["a"]


def test_limit_closure_depth_two() -> None:
    names = [coord.name for coord in _limit_closure(_chain_graph(), 2, None)]
    assert names == ["a", "b"]


def test_limit_closure_unknown_edges_keeps_all() -> None:
    graph = ClosureGraph.flat([_coord("a"), _coord("b")])
    assert _limit_closure(graph, 1, None) == graph.coords


def test_flat_graph_has_no_edges() -> None:
    graph = ClosureGraph.flat([_coord("a")])
    assert not graph.edges_known
    assert graph.roots == ["a"]


# ---------------------------------------------------------------------------
# Разбор локфайлов в граф (Python)
# ---------------------------------------------------------------------------

UV_LOCK = """\
version = 1

[[package]]
name = "app"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "pkga" }]

[[package]]
name = "pkga"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "pkgb" }]

[[package]]
name = "pkgb"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "pkgc" }]

[[package]]
name = "pkgc"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
"""


def test_toml_lock_graph(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(UV_LOCK, encoding="utf-8")

    graph = _toml_lock_graph(lock, tmp_path)

    assert {coord.name for coord in graph.coords} == {"pkga", "pkgb", "pkgc"}
    assert graph.roots == ["pkga"]  # корневой проект исключён, его зависимости — корни
    assert graph.edges == {"pkga": ["pkgb"], "pkgb": ["pkgc"]}


def test_toml_lock_graph_depth_filter(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(UV_LOCK, encoding="utf-8")

    graph = _toml_lock_graph(lock, tmp_path)

    assert [c.name for c in _limit_closure(graph, 1, None)] == ["pkga"]
    assert [c.name for c in _limit_closure(graph, 3, None)] == ["pkga", "pkgb", "pkgc"]


PIP_REPORT = {
    "install": [
        {
            "requested": True,
            "metadata": {
                "name": "pkga",
                "version": "1.0",
                "requires_dist": ["pkgb (>=1)"],
            },
        },
        {
            "metadata": {
                "name": "pkgb",
                "version": "1.0",
                "requires_dist": ["pkgc>=1 ; python_version >= '3'"],
            },
        },
        {"metadata": {"name": "pkgc", "version": "1.0"}},
    ]
}


def test_pip_graph_edges_and_roots() -> None:
    graph = _pip_graph(PIP_REPORT)

    assert {coord.name for coord in graph.coords} == {"pkga", "pkgb", "pkgc"}
    assert graph.roots == ["pkga"]
    assert graph.edges == {"pkga": ["pkgb"], "pkgb": ["pkgc"]}


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("requests (>=2)", "requests"),
        ("pkgc>=1 ; python_version >= '3'", "pkgc"),
        ("urllib3[secure]>=1.26", "urllib3"),
        ("", ""),
    ],
)
def test_requirement_name(spec: str, expected: str) -> None:
    assert _requirement_name(spec) == expected


# ---------------------------------------------------------------------------
# Дерево Maven
# ---------------------------------------------------------------------------

MAVEN_TREE = [
    "com.example:app:jar:1.0",
    "+- com.example:a:jar:1.0:compile",
    "|  \\- com.example:b:jar:1.0:compile",
    "|     \\- com.example:c:jar:1.0:compile",
]


def test_parse_dependency_tree() -> None:
    graph = _parse_dependency_tree(MAVEN_TREE)

    assert {coord.name for coord in graph.coords} == {
        "com.example:a",
        "com.example:b",
        "com.example:c",
    }
    assert graph.roots == ["com.example:a"]
    assert graph.edges == {
        "com.example:a": ["com.example:b"],
        "com.example:b": ["com.example:c"],
    }


# ---------------------------------------------------------------------------
# Граф Conan (транзитивное замыкание → прямые рёбра)
# ---------------------------------------------------------------------------

CONAN_GRAPH = {
    "graph": {
        "nodes": {
            "0": {
                "ref": "conanfile",
                "dependencies": {
                    "1": {"ref": "demoa/1.0"},
                    "2": {"ref": "demob/1.0"},
                    "3": {"ref": "democ/1.0"},
                },
            },
            "1": {
                "ref": "demoa/1.0#rev1",
                "dependencies": {"2": {"ref": "demob/1.0"}, "3": {"ref": "democ/1.0"}},
            },
            "2": {"ref": "demob/1.0#rev2", "dependencies": {"3": {"ref": "democ/1.0"}}},
            "3": {"ref": "democ/1.0#rev3", "dependencies": {}},
        }
    }
}


def test_parse_conan_graph_direct_edges() -> None:
    graph = _parse_graph_json(json.dumps(CONAN_GRAPH))

    assert {coord.name for coord in graph.coords} == {"demoa", "demob", "democ"}
    assert graph.roots == ["demoa"]
    assert graph.edges == {"demoa": ["demob"], "demob": ["democ"]}


def test_parse_conan_graph_depth_filter() -> None:
    graph = _parse_graph_json(json.dumps(CONAN_GRAPH))
    assert [c.name for c in _limit_closure(graph, 1, None)] == ["demoa"]
    assert [c.name for c in _limit_closure(graph, 2, None)] == ["demoa", "demob"]


# ---------------------------------------------------------------------------
# Пути Go-модулей
# ---------------------------------------------------------------------------


def test_module_path_strips_version() -> None:
    assert _module_path("example.com/a@v0.0.0") == "example.com/a"


def test_module_path_keeps_main_module() -> None:
    assert _module_path("example.com/demo") == "example.com/demo"


# ---------------------------------------------------------------------------
# Офлайн-режим
# ---------------------------------------------------------------------------


def test_require_cached_reports_missing(tmp_path: Path) -> None:
    cache = PkgCache(str(tmp_path / "cache"))
    coords = [_coord("a"), _coord("b")]

    with pytest.raises(SourceError, match="Офлайн-режим"):
        _require_cached(coords, cache)


def test_require_cached_passes_when_cached(tmp_path: Path) -> None:
    cache = PkgCache(str(tmp_path / "cache"))
    coord = _coord("a")
    cache.source_dir(coord).mkdir(parents=True)

    _require_cached([coord], cache)  # не должно поднять ошибку
