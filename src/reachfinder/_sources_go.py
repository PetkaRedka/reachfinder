"""Поставщик исходников для экосистемы Go (go modules).

Полный closure модулей определяется командой ``go list -m all`` с шаблоном,
выдающим путь, версию и каталог локальной замены (``replace``). Для обычных
модулей исходники скачиваются ``go mod download -json`` в модульный кэш Go;
для модулей, заменённых на локальный каталог, используется сам этот каталог.
Исходники копируются в локальный кэш reachfinder (модульный кэш Go трогать
нельзя).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

from reachfinder._cache import PkgCache
from reachfinder._languages import GO_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._sources import SourceError, SourceProvider

# Путь | версия | каталог локальной замены (может быть пустым) | признак косвенной зависимости.
_LIST_TEMPLATE = "{{.Path}}|{{.Version}}|{{with .Replace}}{{.Dir}}{{end}}|{{.Indirect}}"


class GoSourceProvider(SourceProvider):
    """Получение исходников Go-модулей через ``go``."""

    ecosystem: ClassVar[str] = GO_ECOSYSTEM
    trailmark_language: ClassVar[str] = "go"

    def __init__(self, cache: PkgCache) -> None:
        super().__init__(cache)
        self._project_root: str = ""
        self._local_modules: dict[str, str] = {}

    # ------------------------------------------------------------------

    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        self._project_root = project_root
        if not Path(project_root, "go.mod").is_file():
            raise SourceError(f"Не найден go.mod в {project_root}")

        proc = self.run(["go", "list", "-m", "-f", _LIST_TEMPLATE, "all"], project_root)
        out = self.require_success(proc, "получить список Go-модулей")

        coords: list[DependencyCoordinate] = []
        roots: list[str] = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 4:
                continue
            path, version, replace_dir, indirect = parts[0], parts[1], parts[2], parts[3]
            if not path:
                continue
            # Главный модуль идёт без версии — пропускаем.
            if not version:
                continue
            if replace_dir:
                self._local_modules[path] = replace_dir
            coords.append(
                DependencyCoordinate(ecosystem=GO_ECOSYSTEM, name=path, version=version)
            )
            # Прямые зависимости — те, что не помечены как косвенные.
            if indirect.strip().lower() == "false":
                roots.append(path)

        edges = self._module_edges(project_root)
        return ClosureGraph(coords=coords, edges=edges, roots=roots)

    def _module_edges(self, project_root: str) -> dict[str, list[str]]:
        """Рёбра графа модулей из ``go mod graph`` (путь → прямые зависимости)."""
        proc = self.run(["go", "mod", "graph"], project_root)
        if proc.returncode != 0:
            return {}

        edges: dict[str, list[str]] = {}
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            src, dst = _module_path(parts[0]), _module_path(parts[1])
            if not src or not dst:
                continue
            deps = edges.setdefault(src, [])
            if dst not in deps:
                deps.append(dst)
        return edges

    def fetch(self, coord: DependencyCoordinate) -> Path:
        if self._cache.has(coord):
            return self._cache.source_dir(coord)

        # Модуль, заменённый на локальный каталог, копируем напрямую.
        local_dir = self._local_modules.get(coord.name)
        if local_dir:
            source = Path(os.path.normpath(local_dir))
            if not source.is_dir():
                raise SourceError(f"Каталог локальной замены не найден: {local_dir}")
            return self._cache.install(coord, source, move=False)

        spec = f"{coord.name}@{coord.version}"
        proc = self.run(["go", "mod", "download", "-json", spec], self._project_root)
        out = self.require_success(proc, f"скачать модуль {spec}")
        try:
            info = json.loads(out)
        except json.JSONDecodeError as exc:
            raise SourceError(f"Некорректный вывод `go mod download` для {spec}") from exc

        module_dir = info.get("Dir")
        if not module_dir or not Path(module_dir).is_dir():
            raise SourceError(f"Не найден каталог модуля {spec} в модульном кэше Go")

        # Копируем, чтобы не трогать общий модульный кэш Go.
        return self._cache.install(coord, Path(os.path.normpath(module_dir)), move=False)


def _module_path(ref: str) -> str:
    """Выделить путь модуля из ссылки ``path@version`` (версия отбрасывается)."""
    return ref.rsplit("@", 1)[0].strip()
