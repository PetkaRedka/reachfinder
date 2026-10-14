"""Тесты файлового кэша исходников."""

from __future__ import annotations

from reachfinder._cache import PkgCache
from reachfinder._models import DependencyCoordinate

COORD = DependencyCoordinate(ecosystem="python", name="requests", version="2.25.1")


def test_install_move_and_read_meta(tmp_path) -> None:
    cache = PkgCache(str(tmp_path))
    source = tmp_path / "src"
    source.mkdir()
    (source / "x.py").write_text("print('x')", encoding="utf-8")

    dest = cache.install(COORD, source)

    assert dest.is_dir()
    assert (dest / "x.py").is_file()
    assert cache.has(COORD)
    assert not source.exists()  # исходники перемещены, а не скопированы

    meta = cache.read_meta(COORD)
    assert meta is not None
    assert meta["name"] == "requests"
    assert meta["version"] == "2.25.1"


def test_install_copy(tmp_path) -> None:
    cache = PkgCache(str(tmp_path))
    source = tmp_path / "src"
    source.mkdir()
    (source / "x.py").write_text("print('x')", encoding="utf-8")

    dest = cache.install(COORD, source, move=False)

    assert dest.is_dir()
    assert source.exists()  # при copy исходники остаются на месте


def test_install_is_idempotent(tmp_path) -> None:
    cache = PkgCache(str(tmp_path))
    source = tmp_path / "src"
    source.mkdir()
    (source / "x.py").write_text("print('x')", encoding="utf-8")

    first = cache.install(COORD, source)
    # Повторный вызов не должен создавать дубликат.
    second = cache.install(COORD, tmp_path / "other")
    assert first == second


def test_key_is_stable_per_purl(tmp_path) -> None:
    cache = PkgCache(str(tmp_path))
    same = DependencyCoordinate(ecosystem="python", name="requests", version="2.25.1")
    other = DependencyCoordinate(ecosystem="python", name="requests", version="2.26.0")
    assert cache.key_for(COORD) == cache.key_for(same)
    assert cache.key_for(COORD) != cache.key_for(other)
