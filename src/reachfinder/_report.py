"""Формирование отчётов: JSON, консоль и опционально mermaid-граф.

При недостижимости выводятся «доказательства работы» (размер графа, точки
входа, определения цели) и диагноз причины, а также прямые вызывающие цели и
проектные функции, достигающие цели (но не являющиеся точками входа).
"""

from __future__ import annotations

import json
import os

from reachfinder._logger import Logger
from reachfinder._models import NodeRef, ReachabilityReport

_MAX_PATHS_IN_CONSOLE = 20
_MAX_NAMES_IN_CONSOLE = 10

_DIAGNOSIS_MESSAGES: dict[str, str] = {
    "reachable": "функция достижима из точек входа проекта.",
    "not_found": "целевая функция не найдена в исходниках пакета.",
    "no_source": (
        "в исходниках пакета нет кода на целевом языке "
        "(возможно, пакет публикует только скомпилированные артефакты)."
    ),
    "no_callers": (
        "функция нигде не вызывается в графе "
        "(возможен динамический вызов, рефлексия или мёртвый код)."
    ),
    "deps_only": "функцию вызывают только зависимости; код проекта до неё не доходит.",
    "project_not_entrypoint": (
        "функцию достигают функции проекта, но ни одна из них не является точкой входа."
    ),
}


def save_report(report: ReachabilityReport, output_dir: str) -> str:
    """Сохранить отчёт в JSON и вернуть путь к файлу."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "reachability.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=4, ensure_ascii=False)
    return path


def print_report(report: ReachabilityReport, logger: Logger) -> None:
    """Вывести сводку результата в консоль."""
    logger.print_console("=" * 60)
    logger.print_console("Результат анализа достижимости")
    logger.print_console("=" * 60)
    logger.print_console(f"Проект:     {report.project}")
    logger.print_console(f"Язык:       {report.language} ({report.ecosystem})")
    logger.print_console(f"Пакет:      {report.package}")
    logger.print_console(f"Функция:    {report.function}")

    _print_stats(report, logger)

    if not report.target_nodes:
        logger.print_console("Целевых определений: 0")
        message = _DIAGNOSIS_MESSAGES.get(report.diagnosis, _DIAGNOSIS_MESSAGES["not_found"])
        logger.print_console(f"Диагноз:   {message}")
        return

    status = "ДА" if report.reachable else "НЕТ"
    logger.print_console(f"Достижима:  {status}")
    logger.print_console(f"Целевых определений: {len(report.target_nodes)}")
    for node in report.target_nodes[:_MAX_PATHS_IN_CONSOLE]:
        logger.print_console(f"  - {_short(node)}")
    logger.print_console(
        f"Диагноз:   {_DIAGNOSIS_MESSAGES.get(report.diagnosis, report.diagnosis)}"
    )

    if report.entrypoint_paths:
        _print_paths(report, logger)
    else:
        _print_negative_evidence(report, logger)


def emit_mermaid(report: ReachabilityReport) -> str:
    """Собрать mermaid-диаграмму достижимых маршрутов (flowchart TD)."""
    lines = ["flowchart TD"]
    node_keys: dict[str, str] = {}

    def key_for(node_id: str) -> str:
        if node_id not in node_keys:
            node_keys[node_id] = f"n{len(node_keys)}"
        return node_keys[node_id]

    for route in report.entrypoint_paths:
        for hop in route.path:
            if hop.node_id not in node_keys:
                lines.append(f"    {key_for(hop.node_id)}[{_mermaid_label(hop)}]")
        for left, right in zip(route.path, route.path[1:], strict=False):
            lines.append(f"    {key_for(left.node_id)} --> {key_for(right.node_id)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Вспомогательные принтеры
# ---------------------------------------------------------------------------

def _print_stats(report: ReachabilityReport, logger: Logger) -> None:
    """Вывести «доказательства работы»: размер графа и точки входа."""
    stats = report.stats
    if not stats:
        return
    logger.print_console(
        f"Граф: {stats.get('graph_nodes', 0)} узлов, {stats.get('graph_edges', 0)} рёбер "
        f"(пакетов: {stats.get('packages', 0)}, склеек: {stats.get('boundary_links', 0)})"
    )
    logger.print_console(f"Точек входа проекта: {stats.get('entrypoints', 0)}")
    if report.entrypoints:
        names = ", ".join(node.name for node in report.entrypoints[:_MAX_NAMES_IN_CONSOLE])
        logger.print_console(f"  {names}")
    if report.missing_packages:
        logger.print_console(f"Не удалось скачать пакетов: {len(report.missing_packages)}")
        for name in report.missing_packages[:_MAX_NAMES_IN_CONSOLE]:
            logger.print_console(f"  - {name}")


def _print_paths(report: ReachabilityReport, logger: Logger) -> None:
    logger.print_console("-" * 60)
    logger.print_console("Маршруты (точка входа -> целевая функция):")
    for index, route in enumerate(report.entrypoint_paths[:_MAX_PATHS_IN_CONSOLE], start=1):
        ep = route.entrypoint
        logger.print_console(f"  [{index}] {ep.name} ({ep.file_path}:{ep.start_line})")
        chain = " -> ".join(_hop_label(hop) for hop in route.path)
        logger.print_console(f"      {chain}")
    if len(report.entrypoint_paths) > _MAX_PATHS_IN_CONSOLE:
        logger.print_console(
            f"  ...и ещё {len(report.entrypoint_paths) - _MAX_PATHS_IN_CONSOLE} маршрутов."
        )


def _print_negative_evidence(report: ReachabilityReport, logger: Logger) -> None:
    """При недостижимости показать вызывающих цели и проектные «дотягивания»."""
    logger.print_console("-" * 60)
    if report.callers:
        logger.print_console(f"Прямые вызывающие цели ({len(report.callers)}):")
        for node in report.callers[:_MAX_PATHS_IN_CONSOLE]:
            logger.print_console(f"  - {_short(node)}")
    else:
        logger.print_console("Прямых вызывающих у цели нет.")
    if report.project_reachers:
        logger.print_console(
            f"Функции проекта, достигающие цели (но не точки входа) "
            f"({len(report.project_reachers)}):"
        )
        for node in report.project_reachers[:_MAX_PATHS_IN_CONSOLE]:
            logger.print_console(f"  - {_short(node)}")
    if report.unlinked_boundaries:
        logger.print_console(
            f"Нераскрытые ссылки на целевой пакет ({len(report.unlinked_boundaries)}):"
        )
        for symbol in report.unlinked_boundaries[:_MAX_NAMES_IN_CONSOLE]:
            logger.print_console(f"  - {symbol}")


def _short(node: NodeRef) -> str:
    location = f"{node.file_path}:{node.start_line}" if node.file_path else node.node_id
    return f"{node.name} ({location})"


def _hop_label(hop: NodeRef) -> str:
    """Подпись узла в консольном выводе маршрута."""
    location = f"{hop.file_path}:{hop.start_line}" if hop.file_path else hop.node_id
    return f"{hop.name} ({location})"


def _mermaid_label(hop: NodeRef) -> str:
    return _escape_mermaid(hop.name or hop.node_id)


def _escape_mermaid(text: str) -> str:
    return text.replace('"', "'")
