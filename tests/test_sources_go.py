"""Тесты разбора vendored Go-модулей (без сети)."""

from __future__ import annotations

from pathlib import Path

from reachfinder._sources_go import _parse_vendor_modules, _vendor_dir

VENDOR_MODULES = """\
# example.com/a v1.0.0
## explicit; go 1.21
example.com/a
example.com/a/internal

# example.com/b v1.2.3
## explicit
example.com/b

# example.com/c v0.1.0
## explicit; go 1.20
example.com/c

# example.com/local v1.0.0 => ./local
## explicit
example.com/local
"""


def test_parse_vendor_modules_coords_and_roots(tmp_path: Path) -> None:
    path = tmp_path / "modules.txt"
    path.write_text(VENDOR_MODULES, encoding="utf-8")

    graph = _parse_vendor_modules(path)

    assert [coord.name for coord in graph.coords] == [
        "example.com/a",
        "example.com/b",
        "example.com/c",
        "example.com/local",
    ]
    assert graph.coords[1].version == "v1.2.3"
    # ``## explicit`` означает прямую зависимость проекта.
    assert graph.roots == [
        "example.com/a",
        "example.com/b",
        "example.com/c",
        "example.com/local",
    ]
    # Из modules.txt рёбра не извлекаются.
    assert not graph.edges_known


def test_parse_vendor_modules_skips_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "modules.txt"
    path.write_text(
        "# example.com/a v1.0.0\n## explicit\nexample.com/a\n"
        "# example.com/a v1.0.0\n## explicit\nexample.com/a\n",
        encoding="utf-8",
    )

    graph = _parse_vendor_modules(path)

    assert [coord.name for coord in graph.coords] == ["example.com/a"]
    assert graph.roots == ["example.com/a"]


def test_vendor_dir_detected_only_with_modules_txt(tmp_path: Path) -> None:
    assert _vendor_dir(tmp_path) is None

    vendor = tmp_path / "vendor"
    vendor.mkdir()
    assert _vendor_dir(tmp_path) is None

    (vendor / "modules.txt").write_text("", encoding="utf-8")
    assert _vendor_dir(tmp_path) == vendor
