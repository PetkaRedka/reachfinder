"""Кэш распарсенных графов trailmark на диске.

Графы сериализуются через pickle (внутренний локальный кэш: быстро и без
потерь) и ключуются по экосистеме и отпечатку исходников. Отпечаток учитывает
пути, размеры и mtime файлов, чтобы изменения локальных пакетов инвалидировали
кэш. Записи переписываются атомарно (временный файл + os.replace).
"""

from __future__ import annotations

import hashlib
import os
import pickle  # noqa: S403 — внутренний локальный кэш
from pathlib import Path

from reachfinder._graph import CodeGraph

_SCHEMA_VERSION = 1
_MAGIC = b"reachfinder-graph-v1"


def fingerprint(source_dir: str, exclude: set[str] | None = None) -> str:
    """Лёгкий отпечаток каталога исходников (абсолютный корень + пути + размер + mtime).

    :param exclude: абсолютные пути каталогов, исключаемых из отпечатка
        (например, кэш, лежащий внутри проекта).
    """
    digest = hashlib.sha256()
    root = Path(source_dir)
    # Абсолютный путь входит в отпечаток: граф хранит абсолютные пути файлов,
    # поэтому одинаковая по содержимому копия каталога в другом месте не должна
    # переиспользовать чужой кэш (например, после запуска в Docker).
    digest.update(os.path.normcase(os.path.abspath(source_dir)).encode("utf-8", "replace"))
    digest.update(b"\x00")
    excluded = {os.path.normcase(os.path.abspath(p)) for p in (exclude or set())}
    for dirpath, dirnames, filenames in os.walk(source_dir):
        dirnames[:] = [
            name
            for name in dirnames
            if os.path.normcase(os.path.abspath(os.path.join(dirpath, name))) not in excluded
        ]
        for filename in sorted(filenames):
            path = os.path.join(dirpath, filename)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, root).replace("\\", "/")
            digest.update(rel.encode("utf-8", "replace"))
            digest.update(b"\x00")
            digest.update(str(stat.st_size).encode())
            digest.update(b"\x00")
            digest.update(str(int(stat.st_mtime_ns)).encode())
            digest.update(b"\x00")
    return digest.hexdigest()[:16]


def graph_cache_path(cache_root: str, ecosystem: str, key: str) -> Path:
    """Путь к файлу кэша графа для экосистемы и ключа."""
    return Path(cache_root) / ecosystem / "graph" / f"{key}.pkl"


def save_graph(path: Path, graph: CodeGraph) -> None:
    """Сохранить граф атомарно (запись во временный файл + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump({"magic": _MAGIC, "version": _SCHEMA_VERSION, "graph": graph}, fh)
    os.replace(tmp, path)


def load_graph(path: Path) -> CodeGraph | None:
    """Загрузить граф из кэша (None при отсутствии/повреждении)."""
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as fh:
            data = pickle.load(fh)  # noqa: S301 — внутренний локальный кэш
    except Exception:  # noqa: BLE001 — повреждённый кэш просто пересобирается
        return None
    if not isinstance(data, dict) or data.get("magic") != _MAGIC:
        return None
    if data.get("version") != _SCHEMA_VERSION:
        return None
    graph = data.get("graph")
    return graph if isinstance(graph, CodeGraph) else None
