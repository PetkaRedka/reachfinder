"""Базовые классы поставщиков исходников зависимостей и общие утилиты.

Каждая экосистема пакетного менеджера реализуется отдельным поставщиком
(:class:`SourceProvider`). Единый интерфейс из двух шагов:

1. ``resolve_closure_graph`` — определить граф зависимостей (полный список
   координат включая транзитивные + рёбра «пакет → прямые зависимости») из
   локфайлов проекта;
2. ``fetch`` — скачать и распаковать исходники одной координаты в кэш.

Метод ``resolve_closure`` — тонкая обёртка над ``resolve_closure_graph``,
возвращающая только список координат (DRY).

Общие операции (запуск внешних команд, распаковка архивов, загрузка по URL)
вынесены сюда, чтобы не дублировать их в каждом поставщике (DRY).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from reachfinder._cache import PkgCache
from reachfinder._models import ClosureGraph, DependencyCoordinate


class SourceError(RuntimeError):
    """Ошибка получения исходников зависимостей."""


class SourceProvider(ABC):
    """Интерфейс поставщика исходников для одной экосистемы."""

    #: Имя экосистемы (см. ``reachfinder._languages``).
    ecosystem: ClassVar[str]
    #: Первичное имя языка trailmark для парсинга исходников.
    trailmark_language: ClassVar[str]
    #: Таймаут команд по умолчанию (сек), переопределяется флагом ``--timeout``.
    default_timeout: ClassVar[int] = 600

    def __init__(self, cache: PkgCache) -> None:
        self._cache = cache
        self.default_timeout = type(self).default_timeout

    @abstractmethod
    def resolve_closure_graph(self, project_root: str) -> ClosureGraph:
        """Определить граф зависимостей (координаты + рёбра) из локфайлов."""

    def resolve_closure(self, project_root: str) -> list[DependencyCoordinate]:
        """Полный список зависимостей с версиями (обёртка над графом)."""
        return self.resolve_closure_graph(project_root).coords

    @abstractmethod
    def fetch(self, coord: DependencyCoordinate) -> Path:
        """Скачать и распаковать исходники координаты; вернуть каталог в кэше."""

    # ------------------------------------------------------------------
    # Утилиты для наследников
    # ------------------------------------------------------------------

    def run(
        self,
        cmd: list[str],
        cwd: str,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Запустить внешнюю команду и вернуть её результат.

        Исполняемый файл резолвится через ``shutil.which``, что на Windows
        позволяет находить ``npm``/``mvn`` (это ``.cmd``-обёртки, а не ``.exe``).

        :param timeout: таймаут в секундах (по умолчанию ``default_timeout``).
        :raises SourceError: если исполняемый файл не найден.
        """
        if timeout is None:
            timeout = self.default_timeout
        tool = cmd[0]
        executable = shutil.which(tool)
        if executable is None:
            raise SourceError(
                f"Не найден исполняемый файл '{tool}'. Установите пакетный менеджер "
                f"для экосистемы и добавьте его в PATH."
            )
        resolved = [executable, *cmd[1:]]
        # PYTHONUTF8 заставляет Python-утилиты (conan, pip) писать пути в UTF-8,
        # иначе пути с не-ASCII символами искажаются.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            return subprocess.run(  # noqa: S603 — намеренный запуск пакетного менеджера
                resolved,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:  # pragma: no cover — защитная ветка
            raise SourceError(f"Не удалось запустить '{tool}': {exc}") from exc

    @staticmethod
    def require_success(
        proc: subprocess.CompletedProcess[str],
        what: str,
    ) -> str:
        """Проверить, что команда завершилась успешно, иначе поднять ошибку.

        :returns: stdout команды (обрезка хвоста ошибки — для диагностики).
        """
        if proc.returncode == 0:
            return proc.stdout
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
        detail = "\n".join(tail)
        raise SourceError(f"Не удалось {what}: {detail}")


def extract_archive(archive_path: Path, dest_dir: Path) -> None:
    """Распаковать ``.tar.gz``/``.tgz``/``.tar`` или zip-подобные архивы.

    К zip-подобным относятся ``.zip``, ``.whl`` (Python wheel) и ``.nupkg``
    (.NET) — все они являются ZIP-контейнерами.
    """
    name = archive_path.name.lower()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(dest_dir, filter="data")
    elif name.endswith((".zip", ".whl", ".nupkg")):
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                if not _is_safe_member(member.filename):
                    raise SourceError(
                        f"Опасный путь в архиве {archive_path.name}: {member.filename!r}"
                    )
            zf.extractall(dest_dir)  # noqa: S202 — члены архива проверены на zip-slip выше
    else:
        raise SourceError(f"Неподдерживаемый формат архива: {archive_path.name}")


def _is_safe_member(name: str) -> bool:
    """Безопасен ли путь члена архива (защита от zip-slip).

    Отклоняются пустые имена, NUL, абсолютные пути (``/...``, ``C:/...``) и
    имена с компонентом ``..``.
    """
    if not name or "\x00" in name:
        return False
    norm = name.replace("\\", "/")
    if norm.startswith("/"):
        return False
    if len(norm) >= 2 and norm[1] == ":":
        return False
    return ".." not in norm.split("/")


def download_url(url: str, dest_path: Path) -> Path:
    """Скачать файл по URL (без проверки сертификата не отключается).

    :raises SourceError: при сетевой ошибке или не-2xx ответе.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # загрузка только по HTTPS (реестры пакетов).
        with urllib.request.urlopen(url, timeout=120) as resp, open(  # noqa: S310
            dest_path, "wb"
        ) as out:
            shutil.copyfileobj(resp, out)
    except Exception as exc:  # noqa: BLE001 — оборачиваем любую сетевую ошибку
        raise SourceError(f"Не удалось скачать {url}: {exc}") from exc
    return dest_path


# ---------------------------------------------------------------------------
# Реестр поставщиков
# ---------------------------------------------------------------------------

def get_provider(ecosystem: str, cache: PkgCache) -> SourceProvider:
    """Вернуть поставщик исходников для экосистемы.

    Импорты выполняются лениво, чтобы подключение поставщиков не тянуло
    неиспользуемые зависимости и не создавало циклов импорта.
    """
    from reachfinder._sources_cpp import CppSourceProvider
    from reachfinder._sources_csharp import CSharpSourceProvider
    from reachfinder._sources_go import GoSourceProvider
    from reachfinder._sources_java import JavaSourceProvider
    from reachfinder._sources_js import JsSourceProvider
    from reachfinder._sources_python import PythonSourceProvider

    providers: dict[str, type[SourceProvider]] = {
        PythonSourceProvider.ecosystem: PythonSourceProvider,
        GoSourceProvider.ecosystem: GoSourceProvider,
        JsSourceProvider.ecosystem: JsSourceProvider,
        JavaSourceProvider.ecosystem: JavaSourceProvider,
        CSharpSourceProvider.ecosystem: CSharpSourceProvider,
        CppSourceProvider.ecosystem: CppSourceProvider,
    }
    if ecosystem not in providers:
        raise SourceError(f"Неизвестная экосистема пакетного менеджера: {ecosystem}")
    return providers[ecosystem](cache)
