"""reachfinder: достижимость функции пакета-зависимости из кода проекта."""

from reachfinder._cache import PkgCache
from reachfinder._languages import ecosystem_for_language, normalize_language
from reachfinder._logger import Logger
from reachfinder._models import (
    ClosureGraph,
    DependencyCoordinate,
    EntryPointPath,
    NodeRef,
    ReachabilityReport,
    make_purl,
)
from reachfinder._reachability import compute_reachability, resolve_language
from reachfinder._report import emit_mermaid, print_report, save_report
from reachfinder._sources import SourceError, get_provider

__all__ = [
    "ClosureGraph",
    "DependencyCoordinate",
    "EntryPointPath",
    "Logger",
    "NodeRef",
    "PkgCache",
    "ReachabilityReport",
    "SourceError",
    "compute_reachability",
    "ecosystem_for_language",
    "emit_mermaid",
    "get_provider",
    "make_purl",
    "normalize_language",
    "print_report",
    "resolve_language",
    "save_report",
]

__version__ = "0.1.0"
