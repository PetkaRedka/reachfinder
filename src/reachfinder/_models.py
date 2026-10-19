"""Модели данных инструмента reachfinder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Координата зависимости
# ---------------------------------------------------------------------------

def make_purl(ecosystem: str, name: str, version: str) -> str:
    """Собрать package-url (purl) для координаты зависимости.

    Имена, содержащие / (пути Go-модулей, Maven-координаты group:artifact),
    кодируются по правилам purl, чтобы кэш-ключ оставался безопасным именем файла.
    """
    encoded_name = quote(name, safe="")
    return f"pkg:{ecosystem}/{encoded_name}@{quote(version, safe='')}"


@dataclass(frozen=True)
class DependencyCoordinate:
    """Одна зависимость (или транзитивная) с точной версией из локфайла."""

    ecosystem: str
    name: str
    version: str

    @property
    def purl(self) -> str:
        """Package-url координаты (стабильный ключ кэша)."""
        return make_purl(self.ecosystem, self.name, self.version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "name": self.name,
            "version": self.version,
            "purl": self.purl,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DependencyCoordinate:
        return cls(
            ecosystem=str(data.get("ecosystem", "")),
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
        )


# ---------------------------------------------------------------------------
# Граф зависимостей
# ---------------------------------------------------------------------------


@dataclass
class ClosureGraph:
    """Граф зависимостей проекта: координаты и рёбра «пакет → прямые зависимости».

    Нужен для ограничения closure по глубине (флаг --package-depth):

    - coords — полный список зависимостей (включая транзитивные);
    - edges — имя пакета → имена его прямых зависимостей (по имени в
      рамках экосистемы);
    - roots — имена прямых зависимостей проекта (глубина 1).

    Если экосистема не позволяет получить рёбра графа, edges остаётся
    пустым, и ограничение по глубине не применяется (см. :meth:`flat`).
    """

    coords: list[DependencyCoordinate] = field(default_factory=list)
    edges: dict[str, list[str]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)

    @property
    def edges_known(self) -> bool:
        """Известны ли рёбра графа (можно ли вычислять глубину)."""
        return bool(self.edges)

    @classmethod
    def flat(cls, coords: list[DependencyCoordinate]) -> ClosureGraph:
        """Closure без известных рёбер: глубина не вычислима."""
        return cls(coords=coords, edges={}, roots=[coord.name for coord in coords])

    @classmethod
    def merged(cls, graphs: list[ClosureGraph]) -> ClosureGraph:
        """Объединить closure нескольких проектов (например, модулей решения).

        Координаты дедуплицируются по имени (первая встреченная версия
        сохраняется), рёбра и корни объединяются без дублей. Порядок входных
        графов должен быть детерминированным — от него зависит результат.
        """
        coords: list[DependencyCoordinate] = []
        edges: dict[str, list[str]] = {}
        roots: list[str] = []
        seen_coords: set[str] = set()
        seen_roots: set[str] = set()
        for graph in graphs:
            for coord in graph.coords:
                if coord.name in seen_coords:
                    continue
                seen_coords.add(coord.name)
                coords.append(coord)
            for name, deps in graph.edges.items():
                existing = edges.setdefault(name, [])
                for dep in deps:
                    if dep not in existing:
                        existing.append(dep)
            for name in graph.roots:
                if name not in seen_roots:
                    seen_roots.add(name)
                    roots.append(name)
        return cls(coords=coords, edges=edges, roots=roots)


# ---------------------------------------------------------------------------
# Ссылка на узел графа
# ---------------------------------------------------------------------------

@dataclass
class NodeRef:
    """Лёгкая ссылка на узел графа вызовов (для отчёта и цепочек)."""

    node_id: str
    name: str
    kind: str = ""
    file_path: str = ""
    start_line: int = 0
    end_line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "kind": self.kind,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeRef:
        return cls(
            node_id=str(data.get("node_id", "")),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            file_path=str(data.get("file_path", "")),
            start_line=int(data.get("start_line", 0)),
            end_line=int(data.get("end_line", 0)),
        )


# ---------------------------------------------------------------------------
# Маршрут достижимости
# ---------------------------------------------------------------------------

@dataclass
class EntryPointPath:
    """Путь вызовов от одной точки входа проекта до целевой функции."""

    entrypoint: NodeRef
    path: list[NodeRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entrypoint": self.entrypoint.to_dict(),
            "path": [hop.to_dict() for hop in self.path],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntryPointPath:
        entrypoint = data.get("entrypoint", {})
        return cls(
            entrypoint=NodeRef.from_dict(entrypoint) if isinstance(entrypoint, dict) else NodeRef(
                node_id="", name=""
            ),
            path=[
                NodeRef.from_dict(hop) for hop in data.get("path", []) if isinstance(hop, dict)
            ],
        )


# ---------------------------------------------------------------------------
# Итоговый отчёт
# ---------------------------------------------------------------------------

@dataclass
class ReachabilityReport:
    """Результат анализа достижимости целевой функции из точек входа проекта."""

    project: str
    language: str
    ecosystem: str
    package: str
    function: str
    #: Режим: function (конкретная функция) или package (любой код пакета).
    mode: str = "function"
    reachable: bool = False
    #: Код диагноза: reachable | not_found | no_source |
    #: no_callers | deps_only | project_not_entrypoint.
    diagnosis: str = "reachable"
    target_nodes: list[NodeRef] = field(default_factory=list)
    #: Функции пакета, достижимые из точек входа (для режима package).
    reachable_targets: list[NodeRef] = field(default_factory=list)
    entrypoint_paths: list[EntryPointPath] = field(default_factory=list)
    dependencies: list[DependencyCoordinate] = field(default_factory=list)
    #: Статистика конвейера: packages, graph_nodes, graph_edges, boundary_links,
    #: entrypoints, callers.
    stats: dict[str, int] = field(default_factory=dict)
    #: Точки входа проекта (для наглядности).
    entrypoints: list[NodeRef] = field(default_factory=list)
    #: Прямые вызывающие целевой функции (в построенном графе).
    callers: list[NodeRef] = field(default_factory=list)
    #: Проектные функции, транзитивно достигающие цели (но не точки входа).
    project_reachers: list[NodeRef] = field(default_factory=list)
    #: Пакеты, исходники которых не удалось скачать (пропущены).
    missing_packages: list[str] = field(default_factory=list)
    #: Нераскрытые proxy-ссылки на целевой пакет (сигнал «граница не склеилась»).
    unlinked_boundaries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "language": self.language,
            "ecosystem": self.ecosystem,
            "package": self.package,
            "function": self.function,
            "mode": self.mode,
            "reachable": self.reachable,
            "diagnosis": self.diagnosis,
            "target_nodes": [node.to_dict() for node in self.target_nodes],
            "reachable_targets": [node.to_dict() for node in self.reachable_targets],
            "entrypoint_paths": [path.to_dict() for path in self.entrypoint_paths],
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "stats": dict(self.stats),
            "entrypoints": [node.to_dict() for node in self.entrypoints],
            "callers": [node.to_dict() for node in self.callers],
            "project_reachers": [node.to_dict() for node in self.project_reachers],
            "missing_packages": list(self.missing_packages),
            "unlinked_boundaries": list(self.unlinked_boundaries),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReachabilityReport:
        return cls(
            project=str(data.get("project", "")),
            language=str(data.get("language", "")),
            ecosystem=str(data.get("ecosystem", "")),
            package=str(data.get("package", "")),
            function=str(data.get("function", "")),
            mode=str(data.get("mode", "function")),
            reachable=bool(data.get("reachable", False)),
            diagnosis=str(data.get("diagnosis", "reachable")),
            target_nodes=[
                NodeRef.from_dict(n) for n in data.get("target_nodes", []) if isinstance(n, dict)
            ],
            reachable_targets=[
                NodeRef.from_dict(n)
                for n in data.get("reachable_targets", [])
                if isinstance(n, dict)
            ],
            entrypoint_paths=[
                EntryPointPath.from_dict(p)
                for p in data.get("entrypoint_paths", [])
                if isinstance(p, dict)
            ],
            dependencies=[
                DependencyCoordinate.from_dict(d)
                for d in data.get("dependencies", [])
                if isinstance(d, dict)
            ],
            stats={str(key): int(value) for key, value in (data.get("stats") or {}).items()},
            entrypoints=[
                NodeRef.from_dict(n) for n in data.get("entrypoints", []) if isinstance(n, dict)
            ],
            callers=[
                NodeRef.from_dict(n) for n in data.get("callers", []) if isinstance(n, dict)
            ],
            project_reachers=[
                NodeRef.from_dict(n)
                for n in data.get("project_reachers", [])
                if isinstance(n, dict)
            ],
            missing_packages=[
                str(p) for p in data.get("missing_packages", []) if isinstance(p, str)
            ],
            unlinked_boundaries=[
                str(p) for p in data.get("unlinked_boundaries", []) if isinstance(p, str)
            ],
        )
