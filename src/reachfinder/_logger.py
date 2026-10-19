"""Потокобезопасный логгер с выводом в файл и консоль."""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from pathlib import Path


def _reconfigure_stdio() -> None:
    """Переключить stdout/stderr на UTF-8 (корректная кириллица на Windows)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


class Logger:
    """Потокобезопасный логгер: пишет в файл всегда, в консоль — по запросу."""

    def __init__(
        self,
        log_file_path: str,
        log_level: int = logging.INFO,
        verbose: bool = False,
    ) -> None:
        _reconfigure_stdio()
        self._lock = threading.Lock()
        self._verbose = verbose
        self.log_file_path = Path(log_file_path)
        self.log_file_path.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(f"Reachfinder-{log_file_path}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()

        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        file_handler = logging.FileHandler(self.log_file_path, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        self._logger.addHandler(file_handler)

        self._console_handler = logging.StreamHandler(sys.stdout)
        self._console_handler.setLevel(logging.DEBUG if verbose else log_level)
        self._console_handler.setFormatter(formatter)

    def _console_log(self, level: int, message: str) -> None:
        """Записать сообщение, временно подключив консольный хендлер."""
        attach = self._console_handler not in self._logger.handlers
        if attach:
            self._logger.addHandler(self._console_handler)
        self._logger.log(level, message)
        if attach:
            self._logger.removeHandler(self._console_handler)

    def debug(self, *args: object) -> None:
        """Запись уровня DEBUG (в консоль — только при verbose)."""
        with self._lock:
            if self._verbose:
                self._console_log(logging.DEBUG, " ".join(map(str, args)))
            else:
                self._logger.debug(" ".join(map(str, args)))

    def print_log(self, *args: object) -> None:
        """Запись только в файл."""
        with self._lock:
            if self._console_handler in self._logger.handlers:
                self._logger.removeHandler(self._console_handler)
            self._logger.info(" ".join(map(str, args)))

    def print_console(self, *args: object) -> None:
        """Запись в файл **и** вывод в консоль."""
        with self._lock:
            self._console_log(logging.INFO, " ".join(map(str, args)))
