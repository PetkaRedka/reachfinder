"""Поставщик исходников для экосистемы Go (go modules).

Полный closure модулей определяется командой go list -m all с шаблоном,
выдающим путь, версию и каталог локальной замены (replace). Для обычных
модулей исходники скачиваются go mod download -json в модульный кэш Go;
для модулей, заменённых на локальный каталог, используется сам этот каталог.
Исходники копируются в локальный кэш reachfinder (модульный кэш Go трогать
нельзя).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import ClassVar

from reachfinder._cache import PkgCache
from reachfinder._languages import GO_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._scip import load_index, referenced_modules
from reachfinder._sources import SourceError, SourceProvider, cached_scip_index

# Путь | версия | каталог локальной замены (может быть пустым) | признак косвенной зависимости.
_LIST_TEMPLATE = "{{.Path}}|{{.Version}}|{{with .Replace}}{{.Dir}}{{end}}|{{.Indirect}}"


class GoSourceProvider(SourceProvider):
    """Получение исходников Go-модулей через go."""

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
        """Рёбра графа модулей из go mod graph (путь → прямые зависимости)."""
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

    def build_scip_index(
        self,
        project_root: str,
        coordinates: list[DependencyCoordinate],
        cache_root: str,
    ) -> str | None:
        """Построить SCIP-индекс Go (scip-go), включая нужные зависимости.

        Двухфазно: сначала индексируется только проект (быстро), затем — лишь те
        модули, на символы которых проект реально ссылается (иначе индексирование
        всего графа модулей непомерно долго). Индекс кэшируется по отпечатку проекта.
        """
        if shutil.which("scip-go") is None:
            return None

        def build(out_path: Path) -> bool:
            base_path = out_path.with_suffix(".base.scip")
            if not self._run_scip_go(project_root, [], base_path):
                return False
            names = {coord.name for coord in coordinates}
            try:
                referenced = referenced_modules(
                    load_index(os.fspath(base_path), project_root), names
                )
            except (OSError, ValueError):
                referenced = []
            patterns = [f"{name}/..." for name in referenced]
            if patterns and self._run_scip_go(project_root, patterns, out_path):
                return True
            # Фаза 2 не удалась — используем базовый индекс (только проект).
            shutil.copyfile(base_path, out_path)
            return True

        return cached_scip_index(cache_root, project_root, build)

    def _run_scip_go(self, project_root: str, patterns: list[str], out_path: Path) -> bool:
        """Запустить scip-go index; вернуть успех."""
        if out_path.is_file():
            out_path.unlink()
        cmd = ["scip-go", "index", "./...", *patterns, "-o", os.fspath(out_path)]
        proc = self.run(cmd, project_root, timeout=3600)
        return proc.returncode == 0 and out_path.is_file()


def _module_path(ref: str) -> str:
    """Выделить путь модуля из ссылки path@version (версия отбрасывается)."""
    return ref.rsplit("@", 1)[0].strip()
