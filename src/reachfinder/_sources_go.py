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
        self._vendor_dir: Path | None = None

    # ------------------------------------------------------------------

    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        self._project_root = project_root
        root = Path(project_root)
        if not (root / "go.mod").is_file():
            raise SourceError(f"Не найден go.mod в {project_root}")

        self._vendor_dir = _vendor_dir(root)

        # ``-mod=readonly`` обходит vendor-режим: без него Go отказывается
        # выполнять ``go list -m all`` при наличии каталога ``vendor/``.
        # В vendored-проекте сначала пробуем модульный граф без сети (GOPROXY=off),
        # а при неудаче берём состав зависимостей из ``vendor/modules.txt``.
        cmd = ["go", "list", "-mod=readonly", "-m", "-f", _LIST_TEMPLATE, "all"]
        extra_env = {"GOPROXY": "off"} if self._vendor_dir is not None else None
        proc = self.run(cmd, project_root, extra_env=extra_env)
        if proc.returncode != 0 and self._vendor_dir is not None:
            vendor_graph = _parse_vendor_modules(self._vendor_dir / "modules.txt")
            if vendor_graph.coords:
                return vendor_graph
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

        # В vendored-проекте исходники уже лежат в vendor/<module> — не нужен
        # ни модульный кэш, ни сеть.
        vendor_source = self._vendor_source(coord)
        if vendor_source is not None:
            return self._cache.install(coord, vendor_source, move=False)

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

    def _vendor_source(self, coord: DependencyCoordinate) -> Path | None:
        """Каталог модуля в ``vendor/`` (``None``, если его там нет)."""
        if self._vendor_dir is None:
            return None
        candidate = self._vendor_dir / coord.name
        return candidate if candidate.is_dir() else None

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


def _vendor_dir(root: Path) -> Path | None:
    """Каталог ``vendor`` с манифестом ``modules.txt`` (иначе ``None``)."""
    vendor = root / "vendor"
    return vendor if (vendor / "modules.txt").is_file() else None


def _parse_vendor_modules(path: Path) -> ClosureGraph:
    """Построить closure из ``vendor/modules.txt``.

    Формат (Go 1.14+): заголовки ``# <module> <version> [=> ...]`` и строки
    ``## explicit`` у модулей, перечисленных в ``go.mod``. Пакетные строки
    игнорируются. Рёбра между модулями из этого файла не извлекаются, поэтому
    ``--package-depth`` для vendored-проектов может не применяться.
    """
    coords: list[DependencyCoordinate] = []
    roots: list[str] = []
    seen: set[str] = set()
    current: DependencyCoordinate | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            if current is not None and "explicit" in line and current.name not in roots:
                roots.append(current.name)
            continue
        if not line.startswith("# "):
            continue
        body = line[2:].strip().split("=>", 1)[0].strip()
        parts = body.split()
        current = None
        if len(parts) < 2:
            continue
        name, version = parts[0], parts[1]
        if name in seen:
            continue
        seen.add(name)
        current = DependencyCoordinate(ecosystem=GO_ECOSYSTEM, name=name, version=version)
        coords.append(current)
    return ClosureGraph(coords=coords, edges={}, roots=roots)

def _module_path(ref: str) -> str:
    """Выделить путь модуля из ссылки path@version (версия отбрасывается)."""
    return ref.rsplit("@", 1)[0].strip()
