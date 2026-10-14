"""Поставщик исходников для экосистем C/C++ (Conan).

Closure определяется командой ``conan graph info --format=json`` (полный граф)
с фоллбэком на список требований из ``conanfile.txt``. Исходники получаются
как:

1. ``conan download <ref> --only-recipe`` — рецепт попадает в кэш;
2. ``conan cache path <ref>`` — папка рецепта в кэше;
3. ``conan source <папка>`` — выполняется ``source()`` рецепта (скачивание
   вышестоящих исходников);
4. ``conan cache path <ref> --folder=source`` — каталог исходников, который
   копируется в кэш reachfinder.

C/C++ — экосистема без единого стандарта зависимостей, поэтому этот
поставщик best-effort: транзитивные зависимости учитываются при наличии
рабочего профиля Conan и разрешимого графа.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import ClassVar

from reachfinder._cache import PkgCache
from reachfinder._languages import CPP_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._sources import SourceError, SourceProvider


class CppSourceProvider(SourceProvider):
    """Получение исходников C/C++-зависимостей через Conan."""

    ecosystem: ClassVar[str] = CPP_ECOSYSTEM
    trailmark_language: ClassVar[str] = "c,cpp"

    def __init__(self, cache: PkgCache) -> None:
        super().__init__(cache)
        self._project_root: str = ""

    # ------------------------------------------------------------------

    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        self._project_root = project_root
        root = Path(project_root)
        conanfile = root / "conanfile.txt"
        if not conanfile.is_file() and not (root / "conanfile.py").is_file():
            raise SourceError(f"Не найден conanfile.txt/conanfile.py в {project_root}")

        # Пытаемся получить полный граф (включая транзитивные зависимости).
        proc = self.run(["conan", "graph", "info", ".", "--format=json"], project_root)
        if proc.returncode == 0:
            graph = _parse_graph_json(proc.stdout)
            if graph.coords:
                return graph

        # Фоллбэк: только верхнеуровневые требования из conanfile.txt.
        if conanfile.is_file():
            return ClosureGraph.flat(self._parse_conanfile_txt(conanfile))
        return ClosureGraph.flat([])

    @staticmethod
    def _parse_conanfile_txt(path: Path) -> list[DependencyCoordinate]:
        coords: list[DependencyCoordinate] = []
        in_requires = False
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    in_requires = line == "[requires]"
                    continue
                if in_requires and "/" in line:
                    ref = line.split("#", 1)[0].strip()
                    name, _, version = ref.rpartition("/")
                    if name and version:
                        coords.append(
                            DependencyCoordinate(
                                ecosystem=CPP_ECOSYSTEM, name=name, version=version
                            )
                        )
        return coords

    # ------------------------------------------------------------------

    def fetch(self, coord: DependencyCoordinate) -> Path:
        if self._cache.has(coord):
            return self._cache.source_dir(coord)

        ref = f"{coord.name}/{coord.version}"
        cwd = self._project_root or os.path.expanduser("~")
        remote = os.getenv("CONAN_REMOTE", "conancenter")

        # 1. Скачать рецепт в кэш Conan (ошибку игнорируем — рецепт может быть уже там).
        self.run(["conan", "download", ref, "--only-recipe", f"-r={remote}"], cwd)

        # 2. Папка рецепта в кэше.
        recipe_dir = self._last_line(
            self.require_success(
                self.run(["conan", "cache", "path", ref], cwd),
                f"найти рецепт {ref}",
            )
        )

        # 3. Выполнить source() рецепта (скачивает/подготавливает исходники).
        # У рецептов без source() (только exports_sources) это может быть нечего делать.
        self.run(
            [
                "conan",
                "source",
                recipe_dir,
                f"--name={coord.name}",
                f"--version={coord.version}",
            ],
            cwd,
        )

        # 4. Каталог исходников в кэше: проверяем несколько известных мест.
        source_dir = self._locate_sources(ref, recipe_dir, cwd)
        if source_dir is None:
            raise SourceError(f"Не найден каталог исходников Conan для {ref}")

        # Копируем в кэш reachfinder (кэш Conan трогать нельзя).
        with tempfile.TemporaryDirectory() as tmp:
            copy_dir = Path(tmp) / "source"
            shutil.copytree(source_dir, copy_dir)
            return self._cache.install(coord, copy_dir)

    def _locate_sources(self, ref: str, recipe_dir: str, cwd: str) -> Path | None:
        """Найти каталог исходников в кэше Conan.

        Порядок: ``<recipe_dir>/src`` (source() распаковал сюда), затем
        ``--folder=source`` (source() через отдельную папку) и, наконец,
        ``--folder=export_source`` (исходники из ``exports_sources``).
        """
        candidates = [Path(recipe_dir) / "src"]
        for folder in ("source", "export_source"):
            proc = self.run(["conan", "cache", "path", ref, f"--folder={folder}"], cwd)
            if proc.returncode == 0:
                candidates.append(Path(self._last_line(proc.stdout)))
        for candidate in candidates:
            if candidate.is_dir() and any(candidate.iterdir()):
                return candidate
        return None

    @staticmethod
    def _last_line(text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else ""


def _ref_name(ref: str) -> str:
    """Имя пакета из ссылки Conan ``name/version#revision`` (без версии/ревизии)."""
    if not ref:
        return ""
    name, _, _ = ref.split("#", 1)[0].rpartition("/")
    return name


def _ref_version(ref: str) -> str:
    """Версия из ссылки Conan ``name/version#revision``."""
    if not ref:
        return ""
    _, _, version = ref.split("#", 1)[0].rpartition("/")
    return version


def _parse_graph_json(stdout: str) -> ClosureGraph:
    """Построить граф зависимостей из JSON ``conan graph info``.

    Узлы графа нумерованы (``"0"`` — потребитель/conanfile). Поле
    ``dependencies`` узла содержит **транзитивное** замыкание, поэтому прямые
    рёбра вычисляются: прямые зависимость — те, что не достигаются через другие
    зависимости того же узла. Корневой узел (без ``ref``) даёт корни.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return ClosureGraph.flat([])

    nodes = (data.get("graph", {}).get("nodes") or {})

    id_name: dict[str, str] = {}
    id_version: dict[str, str] = {}
    id_children: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        ref = node.get("ref") or ""
        id_name[node_id] = _ref_name(ref)
        id_version[node_id] = _ref_version(ref)
        id_children[node_id] = list((node.get("dependencies") or {}).keys())

    coords: list[DependencyCoordinate] = []
    seen: set[str] = set()
    for node_id, name in id_name.items():
        version = id_version[node_id]
        if name and version and name not in seen:
            seen.add(name)
            coords.append(
                DependencyCoordinate(ecosystem=CPP_ECOSYSTEM, name=name, version=version)
            )

    # Имена, достижимые из каждого узла (транзитивное замыкание).
    id_closure: dict[str, set[str]] = {
        node_id: {id_name.get(child, "") for child in children} - {""}
        for node_id, children in id_children.items()
    }

    edges: dict[str, list[str]] = {}
    roots: list[str] = []
    for node_id, children in id_children.items():
        if not children:
            continue
        direct: list[str] = []
        for child in children:
            child_name = id_name.get(child, "")
            if not child_name:
                continue
            reachable_through_others: set[str] = set()
            for other in children:
                if other == child:
                    continue
                reachable_through_others |= id_closure.get(other, set())
                reachable_through_others.add(id_name.get(other, ""))
            if child_name not in reachable_through_others and child_name not in direct:
                direct.append(child_name)

        name = id_name.get(node_id, "")
        if name and direct:
            edges.setdefault(name, direct)
        elif not name:
            # Корневой узел (потребитель) — его прямые зависимости и есть корни.
            roots.extend(dep for dep in direct if dep not in roots)

    return ClosureGraph(coords=coords, edges=edges, roots=roots)
