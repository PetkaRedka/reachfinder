"""Поставщик исходников для экосистемы Java (Maven).

Полный closure определяется командой ``mvn dependency:list``, а исходники
конкретного артефакта скачиваются как ``-sources.jar`` через
``mvn dependency:get`` и распаковываются в кэш.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import ClassVar

from reachfinder._languages import JAVA_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._sources import SourceError, SourceProvider


class JavaSourceProvider(SourceProvider):
    """Получение исходников Java-артефактов через Maven."""

    ecosystem: ClassVar[str] = JAVA_ECOSYSTEM
    trailmark_language: ClassVar[str] = "java"

    # ------------------------------------------------------------------

    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        if not Path(project_root, "pom.xml").is_file():
            raise SourceError(f"Не найден pom.xml в {project_root}")

        # Дерево зависимостей даёт и координаты, и рёбра (глубину).
        with tempfile.TemporaryDirectory() as tmp:
            tree_file = os.path.join(tmp, "tree.txt")
            proc = self.run(
                [
                    "mvn",
                    "-q",
                    "dependency:tree",
                    "-DoutputType=text",
                    "-Dscope=runtime",
                    f"-DoutputFile={tree_file}",
                ],
                project_root,
                timeout=1200,
            )
            tree_graph = _parse_dependency_tree(
                _read_lines(tree_file) if proc.returncode == 0 else []
            )
        if tree_graph.coords:
            return tree_graph

        # Фоллбэк: плоский список без рёбер (глубина не вычислима).
        with tempfile.TemporaryDirectory() as tmp:
            output_file = os.path.join(tmp, "deps.txt")
            proc = self.run(
                [
                    "mvn",
                    "-q",
                    "dependency:list",
                    "-DincludeScope=runtime",
                    f"-DoutputFile={output_file}",
                ],
                project_root,
                timeout=1200,
            )
            self.require_success(proc, "получить список зависимостей Maven")
            lines = _read_lines(output_file)

        coords: list[DependencyCoordinate] = []
        for line in lines:
            coord = _parse_maven_line(line)
            if coord is not None:
                coords.append(coord)
        return ClosureGraph.flat(coords)

    def fetch(self, coord: DependencyCoordinate) -> Path:
        if self._cache.has(coord):
            return self._cache.source_dir(coord)

        group, artifact = coord.name.split(":", 1)
        proc = self.run(
            [
                "mvn",
                "-q",
                "dependency:get",
                "-Dtransitive=false",
                f"-Dartifact={group}:{artifact}:{coord.version}:jar:sources",
            ],
            self._maven_repo_root_parent(),
            timeout=1200,
        )
        self.require_success(proc, f"скачать исходники {coord.name}:{coord.version}")

        jar = self._sources_jar(group, artifact, coord.version)
        if not jar.is_file():
            raise SourceError(f"Не найден sources.jar для {coord.name}:{coord.version}")

        with tempfile.TemporaryDirectory() as tmp:
            extract_root = Path(tmp)
            with zipfile.ZipFile(jar) as zf:
                zf.extractall(extract_root)
            return self._cache.install(coord, extract_root)

    # ------------------------------------------------------------------

    @staticmethod
    def _maven_repo_root() -> Path:
        """Корень локального репозитория Maven (``~/.m2/repository``)."""
        override = os.getenv("MAVEN_REPO_LOCAL")
        if override:
            return Path(override)
        return Path.home() / ".m2" / "repository"

    @classmethod
    def _maven_repo_root_parent(cls) -> str:
        # dependency:get требует существующий каталог; используем корень репозитория.
        root = cls._maven_repo_root()
        root.mkdir(parents=True, exist_ok=True)
        return os.fspath(root)

    @classmethod
    def _sources_jar(cls, group: str, artifact: str, version: str) -> Path:
        group_path = group.replace(".", os.sep)
        return (
            cls._maven_repo_root()
            / group_path
            / artifact
            / version
            / f"{artifact}-{version}-sources.jar"
        )


def _read_lines(path: str) -> list[str]:
    """Прочитать строки файла, сохраняя ведущие отступы (нужны для дерева)."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return [line.rstrip("\r\n") for line in fh if line.strip()]
    except OSError:
        return []


def _parse_maven_line(line: str) -> DependencyCoordinate | None:
    """Разобрать строку ``groupId:artifactId:packaging:version[:scope]``."""
    parts = line.strip().split(":")
    if len(parts) < 4:
        return None
    group, artifact, version = parts[0], parts[1], parts[3]
    if not group or not artifact or not version:
        return None
    return DependencyCoordinate(
        ecosystem=JAVA_ECOSYSTEM,
        name=f"{group}:{artifact}",
        version=version,
    )


# Цветовые ANSI-последовательности в выводе Maven.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Координата вида group:artifact:packaging:version внутри строки дерева.
_COORD_RE = re.compile(r"([\w.\-]+):([\w.\-]+):(\w+):([\w.\-]+)")


def _parse_dependency_tree(lines: list[str]) -> ClosureGraph:
    r"""Построить граф зависимостей из текстового дерева ``mvn dependency:tree``.

    Уровень вложенности определяется длиной префикса до координаты (каждый
    уровень дерева Maven занимает 3 символа: ``+- ``, ``|  ``, ``\- ``).
    """
    coords: list[DependencyCoordinate] = []
    edges: dict[str, list[str]] = {}
    roots: list[str] = []
    seen: set[str] = set()
    parent_by_level: dict[int, str] = {}

    for raw in lines:
        line = _ANSI_RE.sub("", raw)
        match = _COORD_RE.search(line)
        if match is None:
            continue
        level = len(line[: match.start()]) // 3
        if level == 0:
            continue  # сам проект, не зависимость
        group, artifact, version = match.group(1), match.group(2), match.group(4)
        name = f"{group}:{artifact}"
        if name not in seen:
            seen.add(name)
            coords.append(
                DependencyCoordinate(ecosystem=JAVA_ECOSYSTEM, name=name, version=version)
            )
        if level == 1:
            if name not in roots:
                roots.append(name)
        else:
            parent = parent_by_level.get(level - 1)
            if parent:
                deps = edges.setdefault(parent, [])
                if name not in deps:
                    deps.append(name)
        parent_by_level[level] = name

    return ClosureGraph(coords=coords, edges=edges, roots=roots)
