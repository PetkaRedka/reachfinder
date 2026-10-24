"""Тесты сопоставления имени пакета из --package с closure."""

from __future__ import annotations

import pytest

from reachfinder._models import DependencyCoordinate
from reachfinder._names import find_coordinate
from reachfinder._sources import SourceError


def _coord(ecosystem: str, name: str, version: str = "1.0") -> DependencyCoordinate:
    return DependencyCoordinate(ecosystem=ecosystem, name=name, version=version)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def test_go_module_without_major_suffix() -> None:
    coords = [_coord("go", "github.com/bmatcuk/doublestar/v4", "v4.6.1")]

    found = find_coordinate(coords, "github.com/bmatcuk/doublestar", "go")

    assert found.name == "github.com/bmatcuk/doublestar/v4"


def test_go_module_short_name() -> None:
    coords = [_coord("go", "github.com/bmatcuk/doublestar/v4", "v4.6.1")]

    assert find_coordinate(coords, "doublestar", "go").name.endswith("/v4")


def test_go_gopkg_major_suffix() -> None:
    coords = [_coord("go", "gopkg.in/yaml.v3", "v3.0.1")]

    assert find_coordinate(coords, "gopkg.in/yaml", "go").name == "gopkg.in/yaml.v3"


def test_go_query_with_version() -> None:
    coords = [_coord("go", "github.com/a/b", "v1.2.3")]

    assert find_coordinate(coords, "github.com/a/b@v1.2.3", "go").name == "github.com/a/b"


def test_go_ambiguous_short_name() -> None:
    coords = [
        _coord("go", "example.com/a/foo", "v1.0.0"),
        _coord("go", "example.com/b/foo", "v1.0.0"),
    ]

    with pytest.raises(SourceError, match="неоднозначен"):
        find_coordinate(coords, "foo", "go")


def test_go_major_versions_are_ambiguous_without_suffix() -> None:
    coords = [
        _coord("go", "example.com/foo/v2", "v2.1.0"),
        _coord("go", "example.com/foo/v3", "v3.1.0"),
    ]

    with pytest.raises(SourceError, match="неоднозначен"):
        find_coordinate(coords, "example.com/foo", "go")


def test_go_exact_versioned_path_wins() -> None:
    coords = [
        _coord("go", "example.com/foo", "v1.0.0"),
        _coord("go", "example.com/foo/v2", "v2.1.0"),
    ]

    assert find_coordinate(coords, "example.com/foo/v2", "go").version == "v2.1.0"


# ---------------------------------------------------------------------------
# Java / Maven
# ---------------------------------------------------------------------------


def test_java_artifact_only() -> None:
    coords = [_coord("java", "info.picocli:picocli", "4.7.6")]

    assert find_coordinate(coords, "picocli", "java").name == "info.picocli:picocli"


def test_java_full_coordinate_with_version() -> None:
    coords = [_coord("java", "com.example:c", "1.0")]

    assert find_coordinate(coords, "com.example:c:1.0", "java").name == "com.example:c"


def test_java_ambiguous_artifact() -> None:
    coords = [
        _coord("java", "com.a:c", "1.0"),
        _coord("java", "com.b:c", "1.0"),
    ]

    with pytest.raises(SourceError, match="неоднозначен"):
        find_coordinate(coords, "c", "java")


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def test_python_canonical_name() -> None:
    coords = [_coord("python", "python-dateutil", "2.8.2")]

    assert find_coordinate(coords, "python_dateutil", "python").name == "python-dateutil"


def test_python_specifier_and_extras() -> None:
    coords = [_coord("python", "requests", "2.31.0")]

    assert find_coordinate(coords, "requests[security]>=2", "python").name == "requests"


def test_python_case_insensitive() -> None:
    coords = [_coord("python", "PyYAML", "6.0.1")]

    assert find_coordinate(coords, "pyyaml", "python").name == "PyYAML"


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------


def test_js_name_with_version() -> None:
    coords = [_coord("js", "left-pad", "1.3.0")]

    assert find_coordinate(coords, "left-pad@1.3.0", "js").name == "left-pad"


def test_js_scoped_package_with_version() -> None:
    coords = [_coord("js", "@scope/pkg", "1.0.0")]

    assert find_coordinate(coords, "@scope/pkg@1.0.0", "js").name == "@scope/pkg"


# ---------------------------------------------------------------------------
# .NET / NuGet
# ---------------------------------------------------------------------------


def test_dotnet_name_with_version() -> None:
    coords = [_coord("dotnet", "Newtonsoft.Json", "13.0.3")]

    assert find_coordinate(coords, "newtonsoft.json/13.0.3", "dotnet").name == "Newtonsoft.Json"


# ---------------------------------------------------------------------------
# C/C++ / Conan
# ---------------------------------------------------------------------------


def test_cpp_name_with_version_and_revision() -> None:
    coords = [_coord("cpp", "zlib", "1.3.1")]

    assert find_coordinate(coords, "zlib/1.3.1#rev1", "cpp").name == "zlib"


# ---------------------------------------------------------------------------
# Ошибки и подсказки
# ---------------------------------------------------------------------------


def test_not_found_suggests_close_name() -> None:
    coords = [_coord("go", "github.com/bmatcuk/doublestar/v4", "v4.6.1")]

    with pytest.raises(SourceError, match="Возможно, вы имели в виду"):
        find_coordinate(coords, "github.com/bmatcuk/doublestarr", "go")


def test_empty_name() -> None:
    with pytest.raises(SourceError, match="Пустое имя"):
        find_coordinate([], "   ", "go")
