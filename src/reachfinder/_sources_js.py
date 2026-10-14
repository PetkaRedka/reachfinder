"""Поставщик исходников для экосистем JavaScript/TypeScript (npm).

Closure берётся из ``package-lock.json``. Реестровые пакеты разрешаются по
локфайлу (в closure уже есть все транзитивы), а локальные пакеты (``file:``
или ссылки) npm не раскрывает в локфайле — поэтому такие зависимости
обходятся рекурсивно по их ``package.json``. При отсутствии npm-локфайла
поддерживаются ``pnpm-lock.yaml``/``yarn.lock`` (реестровые пакеты).

Исходники реестрового пакета скачиваются командой ``npm pack <имя>@<версия>``;
локальный пакет копируется из своего каталога.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from reachfinder._cache import PkgCache
from reachfinder._languages import JS_ECOSYSTEM
from reachfinder._models import ClosureGraph, DependencyCoordinate
from reachfinder._sources import SourceError, SourceProvider, extract_archive

_FILE_PREFIX = "file:"


class JsSourceProvider(SourceProvider):
    """Получение исходников npm-пакетов."""

    ecosystem: ClassVar[str] = JS_ECOSYSTEM
    trailmark_language: ClassVar[str] = "javascript"

    def __init__(self, cache: PkgCache) -> None:
        super().__init__(cache)
        #: локальные пакеты (``file:``/link): имя → абсолютный каталог.
        self._local_packages: dict[str, str] = {}

    # ------------------------------------------------------------------

    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        root = Path(project_root)

        npm_lock = root / "package-lock.json"
        if npm_lock.is_file():
            return self._resolve_npm(root, npm_lock)

        pnpm_lock = root / "pnpm-lock.yaml"
        if pnpm_lock.is_file():
            return ClosureGraph.flat(self._parse_pnpm_lock(pnpm_lock))

        yarn_lock = root / "yarn.lock"
        if yarn_lock.is_file():
            return ClosureGraph.flat(self._parse_yarn_lock(yarn_lock))

        raise SourceError(
            f"Не найден файл зависимостей JS/TS в {project_root}. Ожидались "
            "package-lock.json, pnpm-lock.yaml или yarn.lock."
        )

    def _resolve_npm(self, root: Path, lock_path: Path) -> ClosureGraph:
        """Разобрать npm-локфайл, рекурсивно раскрывая локальные пакеты."""
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(f"Некорректный {lock_path.name}: {exc}") from exc

        # Реестровые пакеты и их рёбра из секции packages (ключи node_modules/<имя>).
        registry: dict[str, str] = {}
        registry_edges: dict[str, list[str]] = {}
        for key, entry in (data.get("packages") or {}).items():
            if not key.startswith("node_modules/") or not isinstance(entry, dict):
                continue
            if entry.get("link"):
                continue
            version = entry.get("version")
            resolved = str(entry.get("resolved", ""))
            if version and not resolved.startswith(_FILE_PREFIX):
                name = key.rsplit("node_modules/", 1)[-1]
                registry.setdefault(name, str(version))
                deps = entry.get("dependencies")
                if isinstance(deps, dict) and deps:
                    registry_edges.setdefault(name, list(deps))

        # Прямые зависимости проекта — корни графа.
        root_deps = _read_package_json(root).get("dependencies") or {}
        roots = list(root_deps)

        coords: list[DependencyCoordinate] = []
        edges: dict[str, list[str]] = {}
        seen: set[str] = set()
        queue: list[tuple[str, object, Path]] = [
            (name, spec, root) for name, spec in root_deps.items()
        ]
        while queue:
            name, spec, base = queue.pop(0)
            if not isinstance(name, str) or name in seen:
                continue
            seen.add(name)
            if isinstance(spec, str) and spec.startswith(_FILE_PREFIX):
                local = (base / spec[len(_FILE_PREFIX) :]).resolve()
                if not local.is_dir():
                    continue
                self._local_packages[name] = os.fspath(local)
                package = _read_package_json(local)
                version = str(package.get("version", "0.0.0"))
                coords.append(
                    DependencyCoordinate(ecosystem=JS_ECOSYSTEM, name=name, version=version)
                )
                deps = package.get("dependencies") or {}
                edges[name] = [dep for dep in deps if isinstance(dep, str)]
                for dep_name, dep_spec in deps.items():
                    queue.append((dep_name, dep_spec, local))
            elif name in registry:
                coords.append(
                    DependencyCoordinate(ecosystem=JS_ECOSYSTEM, name=name, version=registry[name])
                )
                deps = registry_edges.get(name, [])
                edges[name] = list(deps)
                for dep_name in deps:
                    queue.append((dep_name, None, root))

        return ClosureGraph(coords=coords, edges=edges, roots=roots)

    @staticmethod
    def _parse_pnpm_lock(path: Path) -> list[DependencyCoordinate]:
        # pnpm-lock.yaml: секция `packages:` с ключами вида `/name@1.2.3`.
        coords: list[DependencyCoordinate] = []
        in_packages = False
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if line.startswith("packages:"):
                    in_packages = True
                    continue
                if in_packages:
                    if line and not line[0].isspace():
                        break
                    key = line.strip().strip(":")
                    if not key or not key.startswith("/"):
                        continue
                    body = key.lstrip("/").rsplit("(", 1)[0].strip()
                    name, _, version = body.partition("@")
                    if name and version:
                        coords.append(
                            DependencyCoordinate(
                                ecosystem=JS_ECOSYSTEM, name=name, version=version
                            )
                        )
        return coords

    @staticmethod
    def _parse_yarn_lock(path: Path) -> list[DependencyCoordinate]:
        # yarn.lock: записи вида `"name@^1.0.0", "name@1.2.3":` и `version "1.2.3"`.
        coords: list[DependencyCoordinate] = []
        pending: list[str] = []
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line.strip():
                    pending = []
                    continue
                if line.startswith(" "):
                    stripped = line.strip()
                    if stripped.startswith("version "):
                        version = (
                            stripped.split('"', 2)[1] if '"' in stripped else stripped[8:]
                        )
                        for key in pending:
                            name = key.strip('",').split("@", 1)[0]
                            if name:
                                coords.append(
                                    DependencyCoordinate(
                                        ecosystem=JS_ECOSYSTEM, name=name, version=version
                                    )
                                )
                    continue
                pending = [part.strip() for part in line.rstrip(":").split(",")]
        return coords

    # ------------------------------------------------------------------

    def fetch(self, coord: DependencyCoordinate) -> Path:
        if self._cache.has(coord):
            return self._cache.source_dir(coord)

        local_dir = self._local_packages.get(coord.name)
        if local_dir:
            return self._cache.install(coord, Path(local_dir), move=False)

        spec = f"{coord.name}@{coord.version}"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            proc = self.run(["npm", "pack", spec, "--silent"], os.fspath(tmp_dir))
            out = self.require_success(proc, f"скачать пакет {spec}")
            archive_name = out.strip().splitlines()[-1].strip()
            archive = tmp_dir / archive_name
            if not archive.is_file():
                raise SourceError(f"npm pack не оставил архив для {spec}")

            extract_root = tmp_dir / "extracted"
            extract_archive(archive, extract_root)
            package_dir = extract_root / "package"
            source_dir = package_dir if package_dir.is_dir() else extract_root
            return self._cache.install(coord, source_dir)


def _read_package_json(directory: Path) -> dict[str, Any]:
    """Прочитать ``package.json`` каталога (пустой словарь при ошибке)."""
    path = directory / "package.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
