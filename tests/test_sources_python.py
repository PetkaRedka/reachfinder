"""Тесты разбора локфайлов и утилит поставщика Python (без сети)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from reachfinder._sources import extract_archive
from reachfinder._sources_python import (
    PythonSourceProvider,
    _parse_pipfile_version,
    _parse_requirements,
    _top_source_dir,
)

UV_LOCK = """\
version = 1
requires-python = ">=3.12"

[[package]]
name = "requests"
version = "2.25.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "urllib3"
version = "1.26.5"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "local-lib"
version = "0.1.0"
source = { path = "." }
"""

PIPFILE_LOCK = """\
{
  "default": {
    "requests": {"version": "==2.25.1"},
    "urllib3": {"version": "==1.26.5"}
  },
  "develop": {}
}
"""


def test_parse_uv_lock(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(UV_LOCK, encoding="utf-8")

    coords = PythonSourceProvider._parse_lock_toml(lock)

    names = {coord.name for coord in coords}
    assert names == {"requests", "urllib3"}  # local-lib (path) исключается


def test_parse_pipfile_lock(tmp_path: Path) -> None:
    lock = tmp_path / "Pipfile.lock"
    lock.write_text(PIPFILE_LOCK, encoding="utf-8")

    coords = PythonSourceProvider._parse_pipfile_lock(lock)

    assert {(c.name, c.version) for c in coords} == {("requests", "2.25.1"), ("urllib3", "1.26.5")}


def test_parse_requirements(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# комментарий\nrequests==2.25.1\nurllib3>=1.26.0\n-e ./local\n",
        encoding="utf-8",
    )

    specs = _parse_requirements(req)

    assert specs == ["requests==2.25.1", "urllib3>=1.26.0"]


def test_parse_pipfile_version() -> None:
    assert _parse_pipfile_version("==2.25.1") == "2.25.1"
    assert _parse_pipfile_version("*") == ""


def test_top_source_dir_single_subdir(tmp_path: Path) -> None:
    root = tmp_path / "extracted"
    (root / "pkg-1.0").mkdir(parents=True)
    (root / "pkg-1.0" / "x.py").write_text("x", encoding="utf-8")
    assert _top_source_dir(root).name == "pkg-1.0"


def test_top_source_dir_multiple_entries(tmp_path: Path) -> None:
    root = tmp_path / "extracted"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg-1.0.dist-info").mkdir(parents=True)
    assert _top_source_dir(root) == root


def test_top_source_dir_wheel_with_single_module(tmp_path: Path) -> None:
    # wheel с одиночным модулем: <модуль>.py + <имя>.dist-info/ → корень, не dist-info.
    root = tmp_path / "extracted"
    (root / "pkgc-1.0.dist-info").mkdir(parents=True)
    (root / "pkgc.py").write_text("def vuln(): pass", encoding="utf-8")
    assert _top_source_dir(root) == root


def test_extract_archive_whl(tmp_path: Path) -> None:
    wheel = tmp_path / "pkgc-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("pkgc.py", "def vuln(): pass\n")
    dest = tmp_path / "out"
    extract_archive(wheel, dest)
    assert (dest / "pkgc.py").is_file()
