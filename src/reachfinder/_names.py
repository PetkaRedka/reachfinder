"""Сопоставление имени пакета из ``--package`` с координатой из closure.

Пользователь обычно вводит имя так, как привык в своей экосистеме, и оно не
всегда совпадает с именем в локфайле:

* Go: модуль с мажорной версией — ``github.com/bmatcuk/doublestar/v4``;
* Java: ``group:artifact``, ``artifact`` или ``group:artifact:version``;
* Python: имена канонизируются (``-``/``_``/``.`` эквивалентны), плюс могут
  быть extras и спецификаторы версии;
* JS: ``name@version``, включая scoped-пакеты;
* C#/C++: ``Id/version`` и ``name/version#revision``.

Модуль приводит запрос и имя координаты к сравнимому виду и даёт понятную
ошибку с подсказками, если пакет не найден.
"""

from __future__ import annotations

import difflib
import re

from reachfinder._models import DependencyCoordinate
from reachfinder._sources import SourceError

# Мажорная версия Go-модуля: ``example.com/foo/v2`` и ``gopkg.in/yaml.v3``.
_GO_MAJOR_SUFFIX = re.compile(r"/v[0-9]+$")
_GO_GOPKG_SUFFIX = re.compile(r"\.v[0-9]+$")

# PEP 503: ``-``, ``_`` и ``.`` в именах дистрибутивов эквивалентны.
_PY_CANONICAL = re.compile(r"[-_.]+")
# Спецификаторы версии, extras и маркеры окружения в запросе Python.
_PY_SPECIFIER = re.compile(r"[<>=!~;([\s]")

_MAX_HINTS = 5


def find_coordinate(
    coords: list[DependencyCoordinate],
    package: str,
    ecosystem: str,
) -> DependencyCoordinate:
    """Найти координату целевого пакета по имени.

    Порядок поиска:

    1. точное совпадение имени (регистронезависимо);
    2. экосистемная нормализация (мажорные суффиксы Go, артефакт Maven,
       каноническое имя Python, версия после ``@``/``/``);
    3. короткое имя Go-модуля, если оно однозначно.

    :raises SourceError: пакет не найден или имя неоднозначно.
    """
    query = package.strip()
    if not query:
        raise SourceError("Пустое имя пакета.")

    exact = [coord for coord in coords if coord.name.strip().lower() == query.lower()]
    if exact:
        return _unique(exact, package, ecosystem)

    matches = [coord for coord in coords if _matches(query, coord.name, ecosystem)]
    if matches:
        return _unique(matches, package, ecosystem)

    short = _short_matches(coords, query, ecosystem)
    if short:
        return _unique(short, package, ecosystem)

    raise SourceError(_not_found_message(query, coords, ecosystem))


# ---------------------------------------------------------------------------
# Сравнение имён
# ---------------------------------------------------------------------------


def _matches(query: str, name: str, ecosystem: str) -> bool:
    """Совпадает ли запрос с именем координаты после нормализации экосистемы."""
    q = query.lower()
    n = name.lower()
    if ecosystem == "python":
        return _python_key(q) == _python_key(n)
    if ecosystem == "go":
        return _go_key(_at_key(q)) == _go_key(n)
    if ecosystem == "java":
        return n == _java_key(q) or n.rsplit(":", 1)[-1] == q
    if ecosystem == "js":
        return _at_key(q) == n
    if ecosystem in ("dotnet", "cpp"):
        return _slash_key(q) == n
    return q == n


def _short_matches(
    coords: list[DependencyCoordinate],
    query: str,
    ecosystem: str,
) -> list[DependencyCoordinate]:
    """Совпадения по короткому имени (только Go и только если оно однозначно)."""
    if ecosystem != "go":
        return []
    key = _go_key(_at_key(query.lower()))
    if "/" in key:
        # Это уже путь модуля, а не короткое имя: не угадываем.
        return []
    return [coord for coord in coords if _go_short(coord.name.lower()) == key]


def _go_key(name: str) -> str:
    """Имя Go-модуля без мажорного суффикса (``/v4`` или ``.v3``)."""
    return _GO_GOPKG_SUFFIX.sub("", _GO_MAJOR_SUFFIX.sub("", name))


def _go_short(name: str) -> str:
    """Последний компонент Go-модуля без мажорного суффикса."""
    return _go_key(name).rsplit("/", 1)[-1]


def _at_key(query: str) -> str:
    """Убрать версию после ``@``, сохранив scope npm (``@scope/pkg``)."""
    index = query.rfind("@")
    return query[:index] if index > 0 else query


def _slash_key(query: str) -> str:
    """Убрать версию после ``/`` (NuGet, Conan: ``name/version``)."""
    return query.split("/", 1)[0].strip()


def _python_key(query: str) -> str:
    """Каноническое имя Python-дистрибутива (PEP 503) без спецификаторов."""
    text = _PY_SPECIFIER.split(query, maxsplit=1)[0].strip()
    return _PY_CANONICAL.sub("-", text.lower())


def _java_key(query: str) -> str:
    """Maven-координата без версии/упаковки: ``group:artifact``."""
    parts = query.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else query


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


def _unique(
    matches: list[DependencyCoordinate],
    package: str,
    ecosystem: str,
) -> DependencyCoordinate:
    """Вернуть единственное совпадение или сообщить о неоднозначности."""
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(coord.name for coord in matches[:_MAX_HINTS])
    remainder = len(matches) - _MAX_HINTS
    suffix = f" и ещё {remainder}" if remainder > 0 else ""
    raise SourceError(
        f"Пакет '{package}' неоднозначен в closure '{ecosystem}': "
        f"{names}{suffix}. Укажите полное имя пакета."
    )


def _not_found_message(
    package: str,
    coords: list[DependencyCoordinate],
    ecosystem: str,
) -> str:
    """Сообщение об отсутствии пакета с возможными вариантами."""
    message = (
        f"Пакет '{package}' не найден в closure зависимостей экосистемы '{ecosystem}'."
    )
    hints = _hints(package, coords)
    if hints:
        message += " Возможно, вы имели в виду: " + ", ".join(hints) + "."
    return message


def _hints(package: str, coords: list[DependencyCoordinate]) -> list[str]:
    """Подсказки по похожим именам (подстрока + нечёткое совпадение)."""
    needle = package.lower()
    by_lower = {coord.name.lower(): coord.name for coord in coords}
    hints = [original for lower, original in by_lower.items() if needle in lower]
    for lower in difflib.get_close_matches(needle, list(by_lower), n=_MAX_HINTS, cutoff=0.6):
        original = by_lower[lower]
        if original not in hints:
            hints.append(original)
    return hints[:_MAX_HINTS]
