"""CLI-интерфейс инструмента reachfinder."""

from __future__ import annotations

import argparse
import os
import sys

from reachfinder import __version__
from reachfinder._cache import PkgCache
from reachfinder._languages import ecosystem_for_language
from reachfinder._logger import Logger
from reachfinder._reachability import (
    _limit_closure,
    compute_reachability,
    resolve_language,
)
from reachfinder._report import emit_mermaid, print_report, save_report
from reachfinder._sources import get_provider


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reachfinder",
        description="Определение достижимости функции пакета-зависимости из кода проекта.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    # --- reach ---------------------------------------------------------
    reach = sub.add_parser(
        "reach", help="Проверить достижимость функции пакета из точек входа проекта"
    )
    reach.add_argument("--project", required=True, help="Путь к исходному коду проекта")
    reach.add_argument("--package", required=True, help="Имя пакета-зависимости")
    reach.add_argument("--function", required=True, help="Имя функции или метода (Class.method)")
    reach.add_argument(
        "--language",
        default="auto",
        help="Язык (auto|python|go|javascript|typescript|java|c|cpp|c_sharp)",
    )
    reach.add_argument("--output-dir", default="./reachfinder_output", help="Каталог результатов")
    reach.add_argument(
        "--cache-dir", default=None, help="Каталог кэша исходников (по умолчанию внутри проекта)"
    )
    reach.add_argument("--max-depth", type=int, default=20, help="Максимальная длина маршрута")
    reach.add_argument(
        "--package-depth",
        type=int,
        default=0,
        help="Глубина скачивания зависимостей (0 — без ограничения, 1 — только прямые)",
    )
    reach.add_argument(
        "--offline",
        action="store_true",
        help="Использовать только уже скачанные исходники из кэша (без сети)",
    )
    reach.add_argument(
        "--jobs", type=int, default=0, help="Процессов для параллельного парсинга (0 — авто)"
    )
    reach.add_argument(
        "--timeout", type=int, default=None, help="Таймаут команд пакетных менеджеров, сек"
    )
    reach.add_argument("--verbose", action="store_true", help="Подробный вывод (DEBUG в консоль)")
    reach.add_argument("--graph", action="store_true", help="Сгенерировать mermaid-граф маршрутов")

    # --- deps ----------------------------------------------------------
    deps = sub.add_parser("deps", help="Показать (и опционально скачать) closure зависимостей")
    deps.add_argument("--project", required=True, help="Путь к исходному коду проекта")
    deps.add_argument(
        "--language",
        default="auto",
        help="Язык (auto|python|go|javascript|typescript|java|c|cpp|c_sharp)",
    )
    deps.add_argument("--cache-dir", default=None, help="Каталог кэша исходников")
    deps.add_argument(
        "--output-dir", default="./reachfinder_output", help="Каталог результатов (логов)"
    )
    deps.add_argument(
        "--package-depth",
        type=int,
        default=0,
        help="Глубина зависимостей (0 — без ограничения, 1 — только прямые)",
    )
    deps.add_argument(
        "--offline",
        action="store_true",
        help="Скачивать только уже доступные исходники (без сети)",
    )
    deps.add_argument(
        "--timeout", type=int, default=None, help="Таймаут команд пакетных менеджеров, сек"
    )
    deps.add_argument("--verbose", action="store_true", help="Подробный вывод (DEBUG в консоль)")
    deps.add_argument("--download", action="store_true", help="Скачать исходники зависимостей")

    return p


def main(argv: list[str] | None = None) -> None:
    """Точка входа CLI."""
    args = _build_parser().parse_args(argv)
    if args.command == "reach":
        _cmd_reach(args)
    elif args.command == "deps":
        _cmd_deps(args)


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def _cmd_reach(args: argparse.Namespace) -> None:
    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    logger = Logger(os.path.join(output_dir, "log", "reachfinder.log"), verbose=args.verbose)

    project_root = os.path.abspath(args.project)
    cache_dir = args.cache_dir or os.path.join(project_root, ".reachfinder_cache")

    logger.print_console(f"Проект: {args.project}")
    logger.print_console(f"Цель: {args.package} -> {args.function}")
    logger.print_console(f"Язык: {args.language}")

    try:
        report = compute_reachability(
            project_root,
            args.package,
            args.function,
            language=args.language,
            cache_dir=cache_dir,
            max_depth=args.max_depth,
            package_depth=args.package_depth,
            offline=args.offline,
            jobs=args.jobs,
            timeout=args.timeout,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001 — CLI: любую ошибку превращаем в понятное сообщение
        logger.print_console(f"Ошибка: {exc}")
        sys.exit(1)

    print_report(report, logger)
    json_path = save_report(report, output_dir)
    logger.print_console(f"Отчёт: {json_path}")

    if args.graph:
        graph_path = os.path.join(output_dir, "reachability.mmd")
        with open(graph_path, "w", encoding="utf-8") as fh:
            fh.write(emit_mermaid(report))
        logger.print_console(f"Граф: {graph_path}")

    logger.print_console("Готово.")


def _cmd_deps(args: argparse.Namespace) -> None:
    logger = Logger(
        os.path.join(os.path.abspath(args.output_dir or "."), "log", "deps.log"),
        verbose=args.verbose,
    )

    project_root = os.path.abspath(args.project)
    cache_dir = args.cache_dir or os.path.join(project_root, ".reachfinder_cache")

    try:
        language = resolve_language(project_root, args.language)
        ecosystem = ecosystem_for_language(language)
        provider = get_provider(ecosystem, PkgCache(cache_dir))
        if args.timeout is not None:
            provider.default_timeout = args.timeout
        closure = provider.resolve_closure_graph(project_root)
        coords = _limit_closure(closure, args.package_depth, logger)
    except Exception as exc:  # noqa: BLE001 — CLI: любую ошибку превращаем в понятное сообщение
        logger.print_console(f"Ошибка: {exc}")
        sys.exit(1)

    logger.print_console(f"Экосистема: {ecosystem}")
    logger.print_console(f"Зависимостей в closure: {len(coords)}")
    for coord in coords:
        logger.print_console(f"  {coord.name}@{coord.version}")

    if args.download:
        logger.print_console("Скачивание исходников...")
        cache = PkgCache(cache_dir)
        for coord in coords:
            if args.offline and not cache.has(coord):
                logger.print_console(f"  {coord.name}@{coord.version} — нет в кэше (пропущен)")
                continue
            source_dir = provider.fetch(coord)
            logger.print_console(f"  {coord.name}@{coord.version} -> {source_dir}")

    logger.print_console("Готово.")


if __name__ == "__main__":
    main()
