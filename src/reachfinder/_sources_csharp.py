"""Поставщик исходников для экосистемы .NET (NuGet).

Closure определяется по obj/project.assets.json (результат
dotnet restore) или packages.lock.json. Исходники пакета берутся из
глобального кэша NuGet (~/.nuget/packages, куда dotnet restore
распаковывает .nupkg), а при его отсутствии скачиваются .nupkg
с nuget.org и распаковываются в кэш.

Ограничение: многие пакеты NuGet публикуют только скомпилированные DLL,
поэтому для них исходников (.cs) может не оказаться — такие пакеты не
дадут узлов в графе. Источники, упакованные в .nupkg (src/, embedded
source), обрабатываются.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar

from reachfinder._languages import DOTNET_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._sources import (
    SourceError,
    SourceProvider,
    cached_scip_index,
    download_url,
    extract_archive,
    resolve_tool,
)

_NUGET_FLAT_URL = "https://api.nuget.org/v3-flatcontainer/{id}/{version}/{id}.{version}.nupkg"

# Каталоги, которые не могут содержать локфайлы проекта и только замедляют обход.
_SKIP_DIRS = {".git", ".reachfinder_cache", "node_modules", "bin"}


class CSharpSourceProvider(SourceProvider):
    """Получение исходников NuGet-пакетов (.NET)."""

    ecosystem: ClassVar[str] = DOTNET_ECOSYSTEM
    trailmark_language: ClassVar[str] = "c_sharp"

    # ------------------------------------------------------------------

    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        root = Path(project_root)

        assets = _find_files(root, "obj", "project.assets.json")
        locks = _find_files(root, "", "packages.lock.json")
        if not assets and not locks:
            if not any(root.glob("*.csproj")) and not any(root.glob("*.sln")):
                raise SourceError(f"Не найден .csproj/.sln в {project_root}")
            proc = self.run(["dotnet", "restore"], project_root, timeout=1800)
            self.require_success(proc, "восстановить зависимости .NET")
            assets = _find_files(root, "obj", "project.assets.json")
            locks = _find_files(root, "", "packages.lock.json")

        # В решении (.sln) dotnet restore пишет project.assets.json
        # в obj каждого проекта, а не в корень. Объединяем closure всех
        # найденных проектов, иначе зависимости не видны.
        if assets:
            return ClosureGraph.merged([_parse_project_assets(path) for path in assets])
        if locks:
            return ClosureGraph.merged([_parse_packages_lock(path) for path in locks])
        raise SourceError(
            f"Не удалось найти локфайлы .NET (obj/project.assets.json, "
            f"packages.lock.json) в {project_root}"
        )

    # ------------------------------------------------------------------

    def build_scip_index(
        self,
        project_root: str,
        coordinates: list[DependencyCoordinate],
        cache_root: str,
    ) -> str | None:
        """Построить SCIP-индекс .NET (scip-dotnet).

        scip-dotnet индексирует решение (.sln) или проект (.csproj).
        Восстановление зависимостей к этому моменту уже выполнено в
        :meth:`resolve_closure_graph`, поэтому передаётся
        --skip-dotnet-restore. Если инструмент недоступен, возвращается
        None (name-based резолв).
        """
        tool = resolve_tool("scip-dotnet", (os.path.expanduser("~/.dotnet/tools"),))
        target = _solution_or_project(Path(project_root))
        if tool is None or target is None:
            return None

        def build(out_path: Path) -> bool:
            proc = self.run(
                [
                    tool,
                    "index",
                    os.fspath(target),
                    "--output",
                    os.fspath(out_path),
                    "--working-directory",
                    project_root,
                    "--skip-dotnet-restore",
                ],
                project_root,
                timeout=3600,
            )
            return proc.returncode == 0 and out_path.is_file()

        return cached_scip_index(cache_root, project_root, build)

    def fetch(self, coord: DependencyCoordinate) -> Path:
        if self._cache.has(coord):
            return self._cache.source_dir(coord)

        # Глобальный кэш NuGet покрывает локальные/приватные фиды и офлайн-режим.
        cached = self._global_packages_dir(coord)
        if cached is not None:
            return self._cache.install(coord, cached, move=False)

        return self._download_from_nuget(coord)

    def fetch_extra_sources(self, coord: DependencyCoordinate) -> Path | None:
        """Скачать исходники из git-репозитория, если в .nupkg их нет.

        Большинство пакетов NuGet публикуют только DLL. В .nuspec обычно есть
        <repository url=... commit=...> — по нему можно скачать исходники
        нужной версии. Метод используется только для **целевого** пакета, чтобы
        не тянуть десятки репозиториев для всего closure.
        """
        repo_dir = Path(self._cache.root) / "dotnet" / "repo" / self._cache.key_for(coord)
        if repo_dir.is_dir():
            return _package_project_dir(repo_dir, coord.name) or repo_dir

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            nuspec = self._nuspec_path(coord, tmp_dir)
            if nuspec is None:
                return None
            repository = _repository_info(nuspec)
            if repository is None:
                return None
            slug, commit = repository
            refs = [commit] if commit else []
            refs.extend([f"v{coord.version}", coord.version])
            for ref in refs:
                archive = tmp_dir / "source.tar.gz"
                archive.unlink(missing_ok=True)
                try:
                    download_url(f"https://github.com/{slug}/archive/{ref}.tar.gz", archive)
                    extract_root = tmp_dir / "source"
                    shutil.rmtree(extract_root, ignore_errors=True)
                    extract_archive(archive, extract_root)
                except SourceError:
                    continue
                roots = [path for path in extract_root.iterdir() if path.is_dir()]
                source = roots[0] if len(roots) == 1 else extract_root
                repo_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, repo_dir)
                return _package_project_dir(repo_dir, coord.name) or repo_dir
        return None

    def _nuspec_path(self, coord: DependencyCoordinate, tmp_dir: Path) -> Path | None:
        """Найти .nuspec в кэше, глобальном кэше NuGet или скачанном .nupkg."""
        candidates: list[Path] = []
        cached = self._cache.source_dir(coord)
        if cached.is_dir():
            candidates = list(cached.glob("*.nuspec"))
        if not candidates:
            global_dir = self._global_packages_dir(coord)
            if global_dir is not None:
                candidates = list(global_dir.glob("*.nuspec"))
        if candidates:
            return candidates[0]

        ident = coord.name.lower()
        version = coord.version.lower()
        nupkg = tmp_dir / f"{ident}.{version}.nupkg"
        extract_root = tmp_dir / "nupkg"
        try:
            download_url(_NUGET_FLAT_URL.format(id=ident, version=version), nupkg)
            extract_archive(nupkg, extract_root)
        except SourceError:
            return None
        candidates = list(extract_root.glob("*.nuspec"))
        return candidates[0] if candidates else None

    @staticmethod
    def _global_packages_dir(coord: DependencyCoordinate) -> Path | None:
        """Каталог пакета в глобальном кэше NuGet (~/.nuget/packages)."""
        override = os.getenv("NUGET_PACKAGES")
        root = Path(override) if override else Path.home() / ".nuget" / "packages"
        candidate = root / coord.name.lower() / coord.version.lower()
        return candidate if candidate.is_dir() else None

    def _download_from_nuget(self, coord: DependencyCoordinate) -> Path:
        """Скачать .nupkg с nuget.org и распаковать в кэш reachfinder."""
        ident = coord.name.lower()
        version = coord.version.lower()
        url = _NUGET_FLAT_URL.format(id=ident, version=version)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            nupkg = tmp_dir / f"{ident}.{version}.nupkg"
            download_url(url, nupkg)
            extract_root = tmp_dir / "extracted"
            extract_archive(nupkg, extract_root)
            return self._cache.install(coord, extract_root)

    # Утилита оставлена для будущего использования (диагностика пакетов без .cs).
    @staticmethod
    def _contains_source(extract_root: Path) -> bool:
        return any(extract_root.rglob("*.cs"))


# ---------------------------------------------------------------------------
# Разбор локфайлов .NET в граф зависимостей
# ---------------------------------------------------------------------------


def _solution_or_project(root: Path) -> Path | None:
    """Найти .sln (предпочтительно) или .csproj для индексации."""
    solutions = sorted(root.glob("*.sln"))
    if solutions:
        return solutions[0]
    projects = sorted(root.glob("*.csproj")) + sorted(root.glob("*/*.csproj"))
    return projects[0] if projects else None


def _package_project_dir(repo_root: Path, package: str) -> Path | None:
    """Каталог проекта пакета внутри git-репозитория.

    В репозитории обычно лежат не только сам пакет, но и тесты, примеры и
    документация. Если брать весь репозиторий как исходники пакета, в граф
    попадает тестовый код и отчёт завышает достижимость. Здесь выбирается
    .csproj, чей PackageId (или имя файла) совпадает с полным именем
    пакета либо с его последним компонентом.
    """
    desired = package.lower()
    short = desired.rsplit(".", 1)[-1]
    candidates: list[tuple[int, Path]] = []
    for project in repo_root.rglob("*.csproj"):
        parts = {part.lower() for part in project.parts}
        if parts & {"test", "tests", "samples", "examples", "benchmarks"}:
            continue
        ident = _project_package_id(project) or project.stem.lower()
        if ident == desired:
            candidates.append((0, project.parent))
        elif ident == short:
            candidates.append((1, project.parent))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], len(item[1].parts), os.fspath(item[1])))
    return candidates[0][1]


def _project_package_id(project: Path) -> str:
    """Прочитать PackageId из .csproj (пустая строка, если не задан)."""
    try:
        root = ET.parse(project).getroot()  # noqa: S314 — читаем только метаданные проекта
    except (OSError, ET.ParseError):
        return ""
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "PackageId" and element.text:
            value = element.text.strip().lower()
            if value:
                return value
    return ""


def _find_files(root: Path, subdir: str, filename: str) -> list[Path]:
    """Найти файлы с именем filename (опционально — только в каталоге subdir).

    Обход пропускает каталоги, где локфайлов быть не может (.git,
    node_modules и т. п.), чтобы не тратить время на большие репозитории.
    """
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        if filename not in filenames:
            continue
        path = Path(dirpath)
        if subdir and path.name != subdir:
            continue
        results.append(path / filename)
    return sorted(results)


def _repository_info(nuspec: Path) -> tuple[str, str] | None:
    """Извлечь (owner/repo, commit) из .nuspec (GitHub)."""
    try:
        root = ET.parse(nuspec).getroot()  # noqa: S314 — читаем только метаданные nuspec
    except (OSError, ET.ParseError):
        return None
    url = ""
    commit = ""
    project_url = ""
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "repository":
            url = element.attrib.get("url", "")
            commit = element.attrib.get("commit", "")
        elif tag == "projectUrl":
            project_url = (element.text or "").strip()
    slug = _github_slug(url or project_url)
    return (slug, commit) if slug else None


def _github_slug(url: str) -> str | None:
    """Нормализовать URL GitHub-репозитория в owner/repo."""
    normalized = url.strip()
    if normalized.startswith("git+"):
        normalized = normalized[4:]
    if normalized.startswith("git@github.com:"):
        slug = normalized.split(":", 1)[1]
    elif "github.com/" in normalized:
        slug = normalized.split("github.com/", 1)[1]
    else:
        return None
    slug = slug.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    return slug if slug.count("/") >= 1 else None


def _parse_project_assets(path: Path) -> ClosureGraph:
    """Построить граф зависимостей из obj/project.assets.json.

    Координаты берутся из секции libraries, рёбра — из targets
    (у каждого пакета есть словарь dependencies), корни — из
    project.frameworks.*.dependencies.
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
    """Построить граф зависимостей из packages.lock.json.

    Прямые зависимости помечены флагом type: Direct, у каждой записи может
    быть словарь dependencies (рёбра).
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
