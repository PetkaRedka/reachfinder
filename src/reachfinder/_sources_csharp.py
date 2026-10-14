"""Поставщик исходников для экосистемы .NET (NuGet).

Closure определяется по ``obj/project.assets.json`` (результат
``dotnet restore``) или ``packages.lock.json``. Исходники пакета берутся из
глобального кэша NuGet (``~/.nuget/packages``, куда ``dotnet restore``
распаковывает ``.nupkg``), а при его отсутствии скачиваются ``.nupkg``
с nuget.org и распаковываются в кэш.

Ограничение: многие пакеты NuGet публикуют только скомпилированные DLL,
поэтому для них исходников (``.cs``) может не оказаться — такие пакеты не
дадут узлов в графе. Источники, упакованные в ``.nupkg`` (``src/``, embedded
source), обрабатываются.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import ClassVar

from reachfinder._languages import DOTNET_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._sources import SourceError, SourceProvider, download_url

_NUGET_FLAT_URL = "https://api.nuget.org/v3-flatcontainer/{id}/{version}/{id}.{version}.nupkg"


class CSharpSourceProvider(SourceProvider):
    """Получение исходников NuGet-пакетов (.NET)."""

    ecosystem: ClassVar[str] = DOTNET_ECOSYSTEM
    trailmark_language: ClassVar[str] = "c_sharp"

    # ------------------------------------------------------------------

    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        root = Path(project_root)

        assets = root / "obj" / "project.assets.json"
        lock = root / "packages.lock.json"
        if not assets.is_file() and not lock.is_file():
            if not any(root.glob("*.csproj")) and not any(root.glob("*.sln")):
                raise SourceError(f"Не найден .csproj/.sln в {project_root}")
            proc = self.run(["dotnet", "restore"], project_root, timeout=1800)
            self.require_success(proc, "восстановить зависимости .NET")

        if assets.is_file():
            return _parse_project_assets(assets)
        return _parse_packages_lock(lock)

    # ------------------------------------------------------------------

    def fetch(self, coord: DependencyCoordinate) -> Path:
        if self._cache.has(coord):
            return self._cache.source_dir(coord)

        # Глобальный кэш NuGet покрывает локальные/приватные фиды и офлайн-режим.
        cached = self._global_packages_dir(coord)
        if cached is not None:
            return self._cache.install(coord, cached, move=False)

        return self._download_from_nuget(coord)

    @staticmethod
    def _global_packages_dir(coord: DependencyCoordinate) -> Path | None:
        """Каталог пакета в глобальном кэше NuGet (``~/.nuget/packages``)."""
        override = os.getenv("NUGET_PACKAGES")
        root = Path(override) if override else Path.home() / ".nuget" / "packages"
        candidate = root / coord.name.lower() / coord.version.lower()
        return candidate if candidate.is_dir() else None

    def _download_from_nuget(self, coord: DependencyCoordinate) -> Path:
        """Скачать ``.nupkg`` с nuget.org и распаковать в кэш reachfinder."""
        ident = coord.name.lower()
        version = coord.version.lower()
        url = _NUGET_FLAT_URL.format(id=ident, version=version)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            nupkg = tmp_dir / f"{ident}.{version}.nupkg"
            download_url(url, nupkg)
            extract_root = tmp_dir / "extracted"
            with zipfile.ZipFile(nupkg) as zf:
                zf.extractall(extract_root)
            return self._cache.install(coord, extract_root)

    # Утилита оставлена для будущего использования (диагностика пакетов без .cs).
    @staticmethod
    def _contains_source(extract_root: Path) -> bool:
        return any(extract_root.rglob("*.cs"))


# ---------------------------------------------------------------------------
# Разбор локфайлов .NET в граф зависимостей
# ---------------------------------------------------------------------------


def _parse_project_assets(path: Path) -> ClosureGraph:
    """Построить граф зависимостей из ``obj/project.assets.json``.

    Координаты берутся из секции ``libraries``, рёбра — из ``targets``
    (у каждого пакета есть словарь ``dependencies``), корни — из
    ``project.frameworks.*.dependencies``.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    coords: list[DependencyCoordinate] = []
    for key, entry in (data.get("libraries") or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "package":
            continue
        name, _, version = key.rpartition("/")
        if name and version:
            coords.append(
                DependencyCoordinate(ecosystem=DOTNET_ECOSYSTEM, name=name, version=version)
            )

    edges: dict[str, list[str]] = {}
    for target in (data.get("targets") or {}).values():
        for key, entry in (target or {}).items():
            if not isinstance(entry, dict):
                continue
            name, _, _ = key.rpartition("/")
            deps = entry.get("dependencies")
            if name and isinstance(deps, dict) and deps:
                edges.setdefault(name, list(deps))

    roots: list[str] = []
    frameworks = (data.get("project") or {}).get("frameworks") or {}
    for framework in frameworks.values():
        if isinstance(framework, dict):
            roots.extend(name for name in (framework.get("dependencies") or {}))

    return ClosureGraph(coords=coords, edges=edges, roots=roots)


def _parse_packages_lock(path: Path) -> ClosureGraph:
    """Построить граф зависимостей из ``packages.lock.json``.

    Прямые зависимости помечены флагом ``type: Direct``, у каждой записи может
    быть словарь ``dependencies`` (рёбра).
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    coords: list[DependencyCoordinate] = []
    edges: dict[str, list[str]] = {}
    roots: list[str] = []
    seen: set[str] = set()

    for framework_deps in (data.get("dependencies") or {}).values():
        for name, entry in (framework_deps or {}).items():
            if not isinstance(entry, dict):
                continue
            version = entry.get("resolved") or entry.get("version")
            if version and name not in seen:
                seen.add(name)
                coords.append(
                    DependencyCoordinate(
                        ecosystem=DOTNET_ECOSYSTEM, name=name, version=str(version)
                    )
                )
            if entry.get("type") == "Direct" and name not in roots:
                roots.append(name)
            deps = entry.get("dependencies")
            if isinstance(deps, dict) and deps:
                edges.setdefault(name, list(deps))

    return ClosureGraph(coords=coords, edges=edges, roots=roots)
