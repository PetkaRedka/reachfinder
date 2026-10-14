"""Файловый кэш исходников зависимостей, ключуемый по purl.

Структура каталога кэша::

    <корень>/
      <экосистема>/
        meta/<ключ>.json   # {purl, ecosystem, name, version, source_dir}
        src/<ключ>/        # распакованные исходники пакета

Ключ — первые 16 символов SHA-256 от purl. Записи неизменяемы: исходники
одной версии пакета скачиваются один раз и переиспользуются при повторных
прогонах. Отличие от прежней схемы (``.pkg_cache``) — кэш разложен по
экосистемам, а координата хранится не в отдельном файле рядом, а внутри
метаданных той же записи.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from reachfinder._models import DependencyCoordinate


class PkgCache:
    """Хранилище исходников зависимостей для одного корня кэша."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> str:
        """Корень кэша (для сторонних подкэшей, например графов)."""
        return os.fspath(self._root)

    # ------------------------------------------------------------------
    # Расположение записей
    # ------------------------------------------------------------------

    @staticmethod
    def key_for(coord: DependencyCoordinate) -> str:
        """Стабильный короткий ключ записи по purl координаты."""
        return hashlib.sha256(coord.purl.encode("utf-8")).hexdigest()[:16]

    def source_dir(self, coord: DependencyCoordinate) -> Path:
        """Каталог, в который будут положены исходники координаты."""
        return self._root / coord.ecosystem / "src" / self.key_for(coord)

    def has(self, coord: DependencyCoordinate) -> bool:
        """Есть ли в кэше извлечённые исходники координаты."""
        return self.source_dir(coord).is_dir()

    def install(
        self,
        coord: DependencyCoordinate,
        extracted_dir: Path,
        *,
        move: bool = True,
    ) -> Path:
        """Положить извлечённые исходники в кэш и вернуть их каталог.

        Если запись уже есть, существующий каталог переиспользуется, а
        переданный ``extracted_dir`` не трогается. При ``move=False`` исходники
        копируются (нужно для кэшей пакетных менеджеров, которые нельзя
        перемещать, например модульного кэша Go).
        """
        dest = self.source_dir(coord)
        if dest.is_dir():
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(os.fspath(extracted_dir), os.fspath(dest))
        else:
            shutil.copytree(extracted_dir, dest)
        self._write_meta(coord, dest)
        return dest

    # ------------------------------------------------------------------
    # Метаданные записи
    # ------------------------------------------------------------------

    def _meta_path(self, coord: DependencyCoordinate) -> Path:
        return self._root / coord.ecosystem / "meta" / f"{self.key_for(coord)}.json"

    def _write_meta(self, coord: DependencyCoordinate, source_dir: Path) -> None:
        path = self._meta_path(coord)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "purl": coord.purl,
            "ecosystem": coord.ecosystem,
            "name": coord.name,
            "version": coord.version,
            "source_dir": os.fspath(source_dir),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def read_meta(self, coord: DependencyCoordinate) -> dict[str, Any] | None:
        """Прочитать метаданные записи (``None``, если записи нет)."""
        path = self._meta_path(coord)
        if not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None
