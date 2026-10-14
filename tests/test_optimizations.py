"""Тесты кэша графов, защиты от zip-slip и вспомогательных утилит (без сети)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from reachfinder._graph_cache import fingerprint, graph_cache_path, load_graph, save_graph
from reachfinder._reachability import _has_source_files, _package_short_name
from reachfinder._sources import SourceError, _is_safe_member, extract_archive


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
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    first = fingerprint(str(tmp_path))
    assert fingerprint(str(tmp_path)) == first

    (tmp_path / "a.py").write_text("x = 2", encoding="utf-8")
    assert fingerprint(str(tmp_path)) != first


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
