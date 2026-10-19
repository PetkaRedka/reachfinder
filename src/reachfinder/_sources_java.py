"""Поставщик исходников для экосистемы Java (Maven).

Полный closure определяется командой mvn dependency:list, а исходники
конкретного артефакта скачиваются как -sources.jar через
mvn dependency:get и распаковываются в кэш.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import ClassVar

from reachfinder._languages import JAVA_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._sources import (
    SourceError,
    SourceProvider,
    cached_scip_index,
    extract_archive,
    resolve_tool,
)

# Версия компиляторного плагина scip-javac (Maven Central).
_SCIP_JAVAC_VERSION = "0.13.1"

# Каталоги, которые не должны попадать в компиляцию (в них нет исходников
# проекта или лежат копии зависимостей).
_SCIP_EXCLUDED_DIRS = {
    ".reachfinder_cache",
    ".git",
    ".idea",
    "build",
    "node_modules",
    "out",
    "target",
}

# Временный POM для разрешения classpath плагина scip-javac и его транзитивов.
_SCIP_JAVAC_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>reachfinder</groupId>
  <artifactId>scip-javac-classpath</artifactId>
  <version>1.0</version>
  <dependencies>
    <dependency>
      <groupId>org.scip-code</groupId>
      <artifactId>scip-javac</artifactId>
      <version>{version}</version>
    </dependency>
  </dependencies>
</project>
"""


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

    def build_scip_index(
        self,
        project_root: str,
        coordinates: list[DependencyCoordinate],
        cache_root: str,
    ) -> str | None:
        """Построить SCIP-индекс Java вручную: javac + scip-javac.

        Готовый scip-java на Windows не работает: он запускает mvn
        (.cmd, который ProcessBuilder не находит), а его агрегатор
        падает на POSIX-правах. Поэтому плагин scip-javac подключается
        напрямую к javac, а полученные шарды конкатенируются (SCIP-индекс —
        protobuf с repeated-полем documents). Работает на всех ОС.
        """
        javac = self._javac()
        if javac is None:
            return None

        def build(out_path: Path) -> bool:
            sources = _java_sources(Path(project_root))
            if not sources:
                return False
            work = out_path.parent / f"{out_path.stem}.work"
            shutil.rmtree(work, ignore_errors=True)
            for sub in ("classes", "generated", "headers", "shards"):
                (work / sub).mkdir(parents=True, exist_ok=True)
            try:
                plugin_cp = self._scip_javac_classpath(out_path.parent / "tools")
                if plugin_cp is None:
                    return False
                project_cp = self._project_classpath(Path(project_root), work)
                if project_cp is None:
                    return False
                argsfile = work / "javac.args"
                _write_javac_args(argsfile, project_root, work, plugin_cp, project_cp, sources)
                proc = self.run([javac, f"@{argsfile}"], project_root, timeout=3600)
                if proc.returncode != 0:
                    return False
                _concat_scip_shards(work / "shards", out_path)
                return out_path.is_file()
            finally:
                shutil.rmtree(work, ignore_errors=True)

        return cached_scip_index(cache_root, project_root, build)

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
            extract_archive(jar, extract_root)
            return self._cache.install(coord, extract_root)

    # ------------------------------------------------------------------

    @staticmethod
    def _maven_repo_root() -> Path:
        """Корень локального репозитория Maven (~/.m2/repository)."""
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

    # ------------------------------------------------------------------
    # SCIP-индексация
    # ------------------------------------------------------------------

    @staticmethod
    def _javac() -> str | None:
        """Найти javac (из JAVA_HOME или PATH)."""
        home = os.getenv("JAVA_HOME")
        if home:
            name = "javac.exe" if os.name == "nt" else "javac"
            candidate = Path(home) / "bin" / name
            if candidate.is_file():
                return os.fspath(candidate)
        return resolve_tool("javac")

    def _scip_javac_classpath(self, tools: Path) -> str | None:
        """Classpath плагина scip-javac (кэшируется между запусками)."""
        version = os.getenv("REACHFINDER_SCIP_JAVAC_VERSION", _SCIP_JAVAC_VERSION)
        tools.mkdir(parents=True, exist_ok=True)
        cp_file = tools / f"classpath-{version}.txt"
        if cp_file.is_file():
            return cp_file.read_text(encoding="utf-8").strip()
        pom = tools / "pom.xml"
        pom.write_text(
            _SCIP_JAVAC_POM.format(version=version),
            encoding="utf-8",
        )
        proc = self.run(
            ["mvn", "-q", "dependency:build-classpath", f"-Dmdep.outputFile={cp_file}"],
            os.fspath(tools),
            timeout=1800,
        )
        if proc.returncode != 0 or not cp_file.is_file():
            return None
        return cp_file.read_text(encoding="utf-8").strip()

    def _project_classpath(self, project_root: Path, work: Path) -> str | None:
        """Classpath зависимостей проекта из Maven."""
        cp_file = work / "project-classpath.txt"
        proc = self.run(
            [
                "mvn",
                "-q",
                "dependency:build-classpath",
                f"-Dmdep.outputFile={cp_file}",
                "-DincludeScope=compile",
            ],
            os.fspath(project_root),
            timeout=1800,
        )
        if proc.returncode != 0:
            return None
        return cp_file.read_text(encoding="utf-8").strip() if cp_file.is_file() else ""


def _java_sources(root: Path) -> list[Path]:
    """Исходники Java проекта (без каталогов зависимостей и сборки).

    Сгенерированные исходники (target/generated-sources) нужны для
    компиляции проектов с ANTLR/аннотационными процессорами, поэтому они
    включаются, в отличие от остального target.
    """
    sources: list[Path] = []
    for path in root.rglob("*.java"):
        relative = path.relative_to(root)
        posix = relative.as_posix()
        # Тестовые исходники не нужны для достижимости и часто требуют test-scope
        # зависимостей, которых нет в compile-classpath.
        if posix.startswith("src/test/") or "/src/test/" in posix:
            continue
        if relative.parts[:2] == ("target", "generated-sources"):
            sources.append(path)
            continue
        if any(part in _SCIP_EXCLUDED_DIRS for part in relative.parts[:-1]):
            continue
        sources.append(path)
    return sorted(sources)


def _write_javac_args(
    argsfile: Path,
    project_root: str,
    work: Path,
    plugin_classpath: str,
    project_classpath: str,
    sources: list[Path],
) -> None:
    """Записать @argfile для javac с forward-slash путями.

    Это обходит баг scip-java на Windows: в его @argfile обратные слэши
    интерпретируются javac как escape-символы и пути искажаются.
    """
    classpath = os.pathsep.join(
        path for path in (plugin_classpath, project_classpath) if path
    )
    targetroot = work / "shards"
    arguments = [
        "-encoding",
        "utf8",
        "-nowarn",
        "-d",
        _forward(work / "classes"),
        "-s",
        _forward(work / "generated"),
        "-h",
        _forward(work / "headers"),
        "-classpath",
        _forward_classpath(classpath),
        f"-Xplugin:scip -sourceroot:{_forward(project_root)} -targetroot:{_forward(targetroot)}",
        *(_forward(source) for source in sources),
    ]
    argsfile.write_text(
        "\n".join(_quote(arg) for arg in arguments),
        encoding=_argsfile_encoding(),
        errors="replace",
    )


def _concat_scip_shards(shards_root: Path, out_path: Path) -> None:
    """Объединить шарды scip-javac в один индекс.

    Каждый шард — самостоятельное protobuf-сообщение scip.Index, у которого
    documents — repeated-поле. Конкатенация таких сообщений даёт валидный
    индекс со всеми документами.
    """
    data = b"".join(path.read_bytes() for path in sorted(shards_root.rglob("*.scip")))
    out_path.write_bytes(data)


def _forward(path: Path | str) -> str:
    """Абсолютный путь с прямыми слэшами (совместим с javac на Windows)."""
    return os.path.abspath(os.fspath(path)).replace("\\", "/")


def _argsfile_encoding() -> str:
    """Кодировка, в которой javac читает @argfile.

    На Windows это системная ANSI-кодепейдж (file.encoding JVM), а не UTF-8.
    Если записать файл в UTF-8, кириллица в путях искажается и javac не
    находит исходники.
    """
    if os.name == "nt":
        try:
            import ctypes

            return f"cp{ctypes.windll.kernel32.GetACP()}"  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    return "utf-8"


def _forward_classpath(classpath: str) -> str:
    """Переписать classpath на прямые слэши, сохранив разделитель ОС."""
    return os.pathsep.join(_forward(part) for part in classpath.split(os.pathsep) if part)


def _quote(argument: str) -> str:
    """Заключить аргумент в кавычки, если в нём есть пробелы."""
    return f'"{argument}"' if any(char.isspace() for char in argument) else argument


def _read_lines(path: str) -> list[str]:
    """Прочитать строки файла, сохраняя ведущие отступы (нужны для дерева)."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return [line.rstrip("\r\n") for line in fh if line.strip()]
    except OSError:
        return []


def _parse_maven_line(line: str) -> DependencyCoordinate | None:
    """Разобрать строку groupId:artifactId:packaging:version[:scope]."""
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
    r"""Построить граф зависимостей из текстового дерева mvn dependency:tree.

    Уровень вложенности определяется длиной префикса до координаты (каждый
    уровень дерева Maven занимает 3 символа: +- , |  , \- ).
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
