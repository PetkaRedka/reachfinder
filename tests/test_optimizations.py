"""Тесты кэша графов, защиты от zip-slip и вспомогательных утилит (без сети)."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from reachfinder._graph_cache import fingerprint, graph_cache_path, load_graph, save_graph
from reachfinder._reachability import _has_source_files, _package_short_name
from reachfinder._sources import SourceError, _is_safe_member, extract_archive
from reachfinder._sources_csharp import _github_slug, _repository_info


def test_github_slug_normalization() -> None:
    assert _github_slug("git+https://github.com/owner/repo.git") == "owner/repo"
    assert _github_slug("https://github.com/owner/repo") == "owner/repo"
    assert _github_slug("git@github.com:owner/repo.git") == "owner/repo"
    assert _github_slug("https://gitlab.com/owner/repo") is None


def test_repository_info_from_nuspec(tmp_path: Path) -> None:
    nuspec = tmp_path / "package.nuspec"
    nuspec.write_text(
        '<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/2012/06/nuspec.xsd">'
        "<metadata><repository type=\"git\" url=\"https://github.com/o/r.git\" "
        'commit="abc123"/></metadata></package>',
        encoding="utf-8",
    )

    assert _repository_info(nuspec) == ("o/r", "abc123")


def test_is_safe_member_rejects_traversal() -> None:
    assert not _is_safe_member("../evil.py")
    assert not _is_safe_member("a/../../evil.py")
    assert not _is_safe_member("/etc/passwd")
    assert not _is_safe_member("C:/Windows/system32/x.dll")
    assert not _is_safe_member("")
    assert _is_safe_member("pkg/module.py")


def test_extract_archive_blocks_zip_slip(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "boom")

    with pytest.raises(SourceError, match="Опасный путь"):
        extract_archive(archive, tmp_path / "out")


def test_graph_cache_roundtrip(tmp_path: Path) -> None:
    from reachfinder._graph import CodeGraph

    graph = CodeGraph(language="python", root_path="")
    path = graph_cache_path(str(tmp_path), "python", "k1")

    save_graph(path, graph)
    loaded = load_graph(path)

    assert loaded is not None
    assert loaded.language == "python"


def test_graph_cache_corrupt_returns_none(tmp_path: Path) -> None:
    path = graph_cache_path(str(tmp_path), "python", "k1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not-a-pickle")

    assert load_graph(path) is None


def test_fingerprint_stable_and_sensitive(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1", encoding="utf-8")
    first = fingerprint(str(tmp_path))
    assert fingerprint(str(tmp_path)) == first

    # Отпечаток учитывает mtime, поэтому сдвигаем его явно: на файловых
    # системах с грубым разрешением две записи подряд могут дать одинаковый mtime.
    path.write_text("x = 2", encoding="utf-8")
    stamp = path.stat().st_mtime_ns
    os.utime(path, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
    assert fingerprint(str(tmp_path)) != first


def test_fingerprint_depends_on_location(tmp_path: Path) -> None:
    # Граф хранит абсолютные пути: копия с тем же содержимым в другом каталоге
    # должна получать собственный ключ кэша.
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory in (first, second):
        directory.mkdir()
        (directory / "a.py").write_text("x = 1", encoding="utf-8")

    assert fingerprint(str(first)) != fingerprint(str(second))


def test_fingerprint_excludes_directory(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    cache = tmp_path / ".cache"
    cache.mkdir()
    base = fingerprint(str(tmp_path), {str(cache)})

    (cache / "b.txt").write_text("изменилось", encoding="utf-8")
    assert fingerprint(str(tmp_path), {str(cache)}) == base


def test_has_source_files(tmp_path: Path) -> None:
    assert not _has_source_files(str(tmp_path), "python")
    (tmp_path / "mod.py").write_text("def f(): pass", encoding="utf-8")
    assert _has_source_files(str(tmp_path), "python")


def test_package_short_name() -> None:
    assert _package_short_name("com.example:c") == "c"
    assert _package_short_name("example.com/c") == "c"
    assert _package_short_name("DemoLibC") == "demolibc"
