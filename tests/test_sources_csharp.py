"""Тесты вспомогательных функций .NET-провайдера (без сети)."""

from __future__ import annotations

from pathlib import Path

from reachfinder._sources_csharp import _find_files, _package_project_dir


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_package_project_dir_by_short_name(tmp_path: Path) -> None:
    project = tmp_path / "src" / "CommandLineUtils" / "CommandLineUtils.csproj"
    _write(project)
    _write(tmp_path / "test" / "CommandLineUtils.Tests" / "CommandLineUtils.Tests.csproj")
    _write(tmp_path / "docs" / "samples" / "Demo" / "Demo.csproj")

    found = _package_project_dir(tmp_path, "McMaster.Extensions.CommandLineUtils")

    assert found == project.parent


def test_package_project_dir_by_package_id(tmp_path: Path) -> None:
    project = tmp_path / "src" / "Lib" / "Lib.csproj"
    _write(
        project,
        "<Project><PropertyGroup><PackageId>My.Package</PackageId>"
        "</PropertyGroup></Project>",
    )

    found = _package_project_dir(tmp_path, "My.Package")

    assert found == project.parent


def test_package_project_dir_falls_back_to_none(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "Other" / "Other.csproj")

    assert _package_project_dir(tmp_path, "Missing.Package") is None


def test_find_files_finds_nested_assets_and_skips_noise(tmp_path: Path) -> None:
    first = tmp_path / "src" / "A" / "obj" / "project.assets.json"
    second = tmp_path / "test" / "B" / "obj" / "project.assets.json"
    _write(first)
    _write(second)
    _write(tmp_path / "node_modules" / "pkg" / "obj" / "project.assets.json")

    found = _find_files(tmp_path, "obj", "project.assets.json")

    assert found == [first, second]
