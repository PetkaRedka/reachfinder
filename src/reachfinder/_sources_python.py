"""Поставщик исходников для экосистемы Python (pip).

Версии берутся из локфайлов проекта в порядке приоритета:

1. ``uv.lock`` / ``poetry.lock`` / ``Pipfile.lock`` — полный closure уже
   зафиксирован, дополнительное разрешение зависимостей не требуется;
2. ``requirements.txt`` / ``pyproject.toml`` — только верхнеуровневые
   зависимости; полный closure вычисляется через ``pip install --dry-run``.

Исходники одного пакета скачиваются командой ``pip download --no-deps``
(предпочтительно sdist, иначе wheel) и распаковываются в кэш.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import ClassVar

from reachfinder._languages import PYTHON_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._sources import SourceError, SourceProvider, extract_archive

# Архивы, которые pip download может оставить после загрузки.
_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".zip")


class PythonSourceProvider(SourceProvider):
    """Получение исходников Python-зависимостей через pip."""

    ecosystem: ClassVar[str] = PYTHON_ECOSYSTEM
    trailmark_language: ClassVar[str] = "python"

    # ------------------------------------------------------------------
    # Определение closure
    # ------------------------------------------------------------------

    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        root = Path(project_root)

        for name in ("uv.lock", "poetry.lock"):
            path = root / name
            if path.is_file():
                return _toml_lock_graph(path, root)

        pipfile = root / "Pipfile.lock"
        if pipfile.is_file():
            return ClosureGraph.flat(self._parse_pipfile_lock(pipfile))

        # requirements.txt (одиночный или каталог) и pyproject.toml —
        # только верхнеуровневые зависимости, раскрываем через pip.
        specs = self._collect_top_level_specs(root)
        if specs:
            return _pip_graph(self._pip_report(project_root, specs))

        raise SourceError(
            f"Не найден файл зависимостей Python в {project_root}. Ожидались uv.lock, "
            "poetry.lock, Pipfile.lock, requirements*.txt или pyproject.toml."
        )

    @staticmethod
    def _parse_lock_toml(path: Path) -> list[DependencyCoordinate]:
        """Разобрать ``[[package]] name/version`` из uv.lock или poetry.lock."""
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

        coords: list[DependencyCoordinate] = []
        for package in data.get("package", []):
            if not isinstance(package, dict):
                continue
            name = package.get("name")
            version = package.get("version")
            if not name or not version:
                continue
            if not _is_registry_package(package):
                continue
            coords.append(
                DependencyCoordinate(
                    ecosystem=PYTHON_ECOSYSTEM,
                    name=str(name),
                    version=str(version),
                )
            )
        return coords

    @staticmethod
    def _parse_pipfile_lock(path: Path) -> list[DependencyCoordinate]:
        """Разобрать closure из Pipfile.lock (секции default и develop)."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)

        coords: list[DependencyCoordinate] = []
        seen: set[str] = set()
        for section in ("default", "develop"):
            for name, spec in (data.get(section) or {}).items():
                if not isinstance(spec, dict):
                    continue
                version = _parse_pipfile_version(str(spec.get("version", "")))
                if not version or name in seen:
                    continue
                seen.add(name)
                coords.append(
                    DependencyCoordinate(
                        ecosystem=PYTHON_ECOSYSTEM,
                        name=name,
                        version=version,
                    )
                )
        return coords

    @staticmethod
    def _collect_top_level_specs(root: Path) -> list[str]:
        """Собрать верхнеуровневые спецификаторы зависимостей из manifest-файлов."""
        specs: list[str] = []

        req_path = root / "requirements.txt"
        req_dir = root / "requirements"
        candidates: list[Path] = []
        if req_path.is_file():
            candidates.append(req_path)
        if req_dir.is_dir():
            candidates.extend(sorted(req_dir.glob("*.txt")))
        for path in candidates:
            specs.extend(_parse_requirements(path))

        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            with open(pyproject, "rb") as fh:
                data = tomllib.load(fh)
            for dep in data.get("project", {}).get("dependencies", []):
                if isinstance(dep, str):
                    specs.append(dep)
            for dep in (data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}):
                # Пропускаем служебный ключ python и локальные зависимости.
                if dep.lower() == "python":
                    continue
                if isinstance(dep, str):
                    specs.append(dep)

        return specs

    def _pip_report(self, project_root: str, specs: list[str]) -> dict:
        """Разрешить полный closure через ``pip install --dry-run --report``."""
        with tempfile.TemporaryDirectory() as tmp:
            report_path = os.path.join(tmp, "report.json")
            cmd = [
                *_pip_command(),
                "install",
                "--dry-run",
                "--ignore-installed",
                "--disable-pip-version-check",
                "--report",
                report_path,
                *specs,
            ]
            proc = self.run(cmd, project_root)
            self.require_success(proc, "разрешить зависимости Python через pip")

            with open(report_path, encoding="utf-8") as fh:
                return json.load(fh)

    # ------------------------------------------------------------------
    # Получение исходников
    # ------------------------------------------------------------------

    def fetch(self, coord: DependencyCoordinate) -> Path:
        if self._cache.has(coord):
            return self._cache.source_dir(coord)

        spec = f"{coord.name}=={coord.version}"
        with tempfile.TemporaryDirectory() as tmp:
            archive = self._download_dist(spec, Path(tmp))
            extract_root = Path(tmp) / "extracted"
            extract_archive(archive, extract_root)
            source_dir = _top_source_dir(extract_root)
            return self._cache.install(coord, source_dir)

    def _download_dist(self, spec: str, dest: Path) -> Path:
        """Скачать sdist (иначе wheel) пакета и вернуть путь к архиву."""
        # Предпочитаем sdist — он содержит только исходники.
        for force_source in (True, False):
            cmd = [
                *_pip_command(),
                "download",
                "--no-deps",
                "--disable-pip-version-check",
                "--dest",
                os.fspath(dest),
            ]
            if force_source:
                cmd.append("--no-binary")
                cmd.append(":all:")
            cmd.append(spec)

            proc = self.run(cmd, os.fspath(dest))
            archive = _find_archive(dest)
            if proc.returncode == 0 and archive is not None:
                return archive

        raise SourceError(f"Не удалось скачать исходники пакета: {spec}")


# ---------------------------------------------------------------------------
# Утилиты разбора
# ---------------------------------------------------------------------------

# Ключи source, означающие не-реестровую зависимость (локальный путь/git/url).
_NON_REGISTRY_KEYS = frozenset({"path", "git", "editable", "directory", "url", "virtual"})


def _toml_lock_graph(path: Path, project_root: Path) -> ClosureGraph:
    """Построить граф зависимостей из uv.lock/poetry.lock (координаты + рёбра)."""
    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    coords: list[DependencyCoordinate] = []
    edges: dict[str, list[str]] = {}
    roots: list[str] = []

    for package in data.get("package", []):
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if not name:
            continue
        deps = _package_dependency_names(package)
        if not _is_registry_package(package):
            # Корневой пакет воркспейса (virtual/editable/path): его зависимости
            # и есть прямые зависимости проекта (глубина 1).
            roots.extend(dep for dep in deps if dep not in roots)
            continue
        if deps:
            edges.setdefault(str(name), deps)
        if not version:
            continue
        coords.append(
            DependencyCoordinate(
                ecosystem=PYTHON_ECOSYSTEM, name=str(name), version=str(version)
            )
        )

    if not roots and path.name == "poetry.lock":
        # poetry.lock не содержит корневого пакета — прямые зависимости берём
        # из pyproject.toml.
        roots = _pyproject_dep_names(project_root)

    return ClosureGraph(coords=coords, edges=edges, roots=roots)


def _package_dependency_names(package: dict) -> list[str]:
    """Имена прямых зависимостей пакета (uv.lock — список, poetry.lock — таблица)."""
    names: list[str] = []
    for key in ("dependencies", "dev-dependencies"):
        value = package.get(key)
        if isinstance(value, list):  # uv.lock: dependencies = [{ name = "..." }]
            for item in value:
                if isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]))
        elif isinstance(value, dict):  # poetry.lock: [package.dependencies]
            names.extend(str(item) for item in value)
    return names


def _pyproject_dep_names(root: Path) -> list[str]:
    """Имена прямых зависимостей проекта из pyproject.toml."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)

    names: list[str] = []
    for spec in data.get("project", {}).get("dependencies", []):
        if isinstance(spec, str) and (name := _requirement_name(spec)):
            names.append(name)
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    for dep in poetry:
        if str(dep).lower() == "python":
            continue
        names.append(str(dep))
    return names


def _pip_graph(report: dict) -> ClosureGraph:
    """Построить граф зависимостей из отчёта ``pip install --dry-run --report``.

    Корневые зависимости помечены в отчёте флагом ``requested``, а рёбра берутся
    из ``metadata.requires_dist`` каждого пакета.
    """
    coords: list[DependencyCoordinate] = []
    edges: dict[str, list[str]] = {}
    roots: list[str] = []
    seen: set[str] = set()

    for entry in report.get("install", []):
        metadata = entry.get("metadata") or {}
        name = metadata.get("name")
        version = metadata.get("version")
        if not name or not version or name in seen:
            continue
        seen.add(str(name))
        coords.append(
            DependencyCoordinate(
                ecosystem=PYTHON_ECOSYSTEM, name=str(name), version=str(version)
            )
        )
        if entry.get("requested"):
            roots.append(str(name))
        deps = [
            dep
            for dep in (
                _requirement_name(spec) for spec in (metadata.get("requires_dist") or [])
            )
            if dep and dep != name
        ]
        if deps:
            edges[str(name)] = deps

    return ClosureGraph(coords=coords, edges=edges, roots=roots)


def _requirement_name(spec: str) -> str:
    """Извлечь имя пакета из PEP 508-спецификатора (``requests (>=2); extra=='x'``)."""
    if not isinstance(spec, str) or not spec.strip():
        return ""
    return re.split(r"[<>=!~;\s\[(]", spec.strip(), maxsplit=1)[0].strip()


def _is_registry_package(package: dict) -> bool:
    """Пакет доступен в реестре (не локальный path/editable/git/url)."""
    source = package.get("source")
    if source is None or not isinstance(source, dict):
        return True
    if "registry" in source:
        return True
    if any(key in source for key in _NON_REGISTRY_KEYS):
        return False
    # poetry.lock помечает не-реестровые зависимости через source.type.
    return source.get("type") not in ("git", "path", "url", "directory", "file", "editable")


def _parse_pipfile_version(raw: str) -> str:
    """Выделить версию из строки вида ``==2.25.1``."""
    match = re.search(r"(\d[^\s]*)", raw)
    return match.group(1) if match else ""


def _parse_requirements(path: Path) -> list[str]:
    """Разобрать requirements-файл в список спецификаторов.

    Учитываются комментарии, ``-r``/``--requirement``-включения, ``-e``
    (editable, пропускаются) и PEP 508-строки с URL (оставляем имя).
    """
    specs: list[str] = []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("-r ", "--requirement ")):
                include = line.split(maxsplit=1)[1].strip()
                include_path = (path.parent / include).resolve()
                if include_path.is_file():
                    specs.extend(_parse_requirements(include_path))
                continue
            if line.startswith(("-e ", "--editable ")):
                continue
            # Отбрасываем hash-маркеры и URL-зависимости (скачиваются отдельно).
            spec = line.split(" #", 1)[0].strip()
            spec = spec.split(" ;", 1)[0].strip()
            if " @ " in spec:
                spec = spec.split(" @ ", 1)[0].strip()
            if spec and "://" not in spec:
                specs.append(spec)
    return specs


def _find_archive(dest: Path) -> Path | None:
    """Найти первый архив, оставленный pip download в каталоге."""
    for entry in sorted(dest.iterdir()):
        if entry.is_file() and entry.name.lower().endswith(_ARCHIVE_SUFFIXES):
            return entry
    return None


def _pip_command() -> list[str]:
    """Вернуть префикс команды pip (учёт uv-окружений без модуля pip).

    В окружениях, созданных ``uv venv``, модуль ``pip`` по умолчанию не
    устанавливается. Предпочитаем ``python -m pip`` текущего интерпретатора,
    иначе — ``pip`` из PATH, иначе — понятная ошибка.
    """
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip"]
    pip = shutil.which("pip")
    if pip:
        return [pip]
    raise SourceError(
        "pip не найден: установите pip (например, `python -m ensurepip`) "
        "или используйте окружение Python с pip."
    )


def _top_source_dir(extract_root: Path) -> Path:
    """Вернуть каталог с исходниками пакета из корня распаковки.

    sdist раскрывается в единственный каталог ``<имя>-<версия>/`` — его и
    возвращаем, чтобы модульные идентификаторы trailmark были консистентны.
    wheel раскрывается в корень с несколькими элементами (пакет/модуль +
    ``<имя>-<версия>.dist-info/``) — тогда возвращаем сам корень.
    """
    entries = list(extract_root.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_root
