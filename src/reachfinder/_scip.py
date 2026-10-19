"""Разбор SCIP-индекса без внешних зависимостей.

SCIP (Code Intelligence Protocol) — protobuf-формат, который строят индексеры
вроде scip-go/scip-python. Он даёт **точные** символы определений и
ссылок (кросс-пакетные, с учётом типов), что позволяет резолвить вызовы,
которые name-based анализ (trailmark) оставляет нерешёнными.

Здесь реализован минимальный ридер protobuf-полей, нужных для резолва
(документы → вхождения: range/symbol/symbol_roles), чтобы не
тянуть тяжёлую зависимость protobuf и не требовать CLI scip:

* Index.documents   — field 2 (message)
* Document.relative_path — field 1 (string)
* Document.occurrences   — field 2 (message)
* Occurrence.range       — field 1 (repeated int32, обычно packed; устарел)
* Occurrence.typed_range.single_line_range — field 8 (message, line = field 1)
* Occurrence.typed_range.multi_line_range  — field 9 (message, start_line = field 1)
* Occurrence.symbol      — field 2 (string)
* Occurrence.symbol_roles — field 3 (int32; бит 1 — определение)
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

# Бит определения в Occurrence.symbol_roles.
_ROLE_DEFINITION = 1

# <scheme> <manager> <package> <version> <descriptor> (SCIP для всех языков).
_SYMBOL_HEAD_RE = re.compile(r"^(\S+) (\S+) (\S+) (\S+) (.+)$")
# Дескриптор с backtick-префиксом пакета (Go/Python:  `pkg`/member(). ).
_BACKTICK_DESCRIPTOR_RE = re.compile(r"^`[^`]*`/(.+)$")
# Сигнатура в конце дескриптора: (), (+11), (dup).
_SIGNATURE_RE = re.compile(r"\([^)]*\)\.$")


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Прочитать varint из data начиная с pos (вернуть значение и новую позицию)."""
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def _iter_fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Пройтись по protobuf-полям сообщения: (field_number, wire_type, value).

    value — int для varint-полей (wire type 0) и bytes для length-delimited
    (wire type 2). Wire types 1/5 (64/32-бит) возвращаются как bytes.
    """
    pos = 0
    size = len(data)
    while pos < size:
        tag, pos = _read_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            yield field_number, 0, value
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            yield field_number, 2, data[pos : pos + length]
            pos += length
        elif wire_type == 1:
            yield field_number, 1, data[pos : pos + 8]
            pos += 8
        elif wire_type == 5:
            yield field_number, 5, data[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"Неподдерживаемый wire type SCIP: {wire_type}")


@dataclass
class ScipIndex:
    """Прочитанный SCIP-индекс: определения и ссылки по координатам файл/строка."""

    root: str
    #: символ -> (относительный путь, строка 0-базная)
    defs: dict[str, tuple[str, int]] = field(default_factory=dict)
    #: (относительный путь, строка 0-базная) -> множество символов-ссылок
    refs: dict[tuple[str, int], set[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.refs, defaultdict):
            self.refs = defaultdict(set, self.refs)


def parse_index(data: bytes, root: str) -> ScipIndex:
    """Разобрать содержимое index.scip в :class:`ScipIndex`."""
    defs: dict[str, tuple[str, int]] = {}
    refs: dict[tuple[str, int], set[str]] = defaultdict(set)
    for field_number, wire_type, value in _iter_fields(data):
        if field_number == 2 and wire_type == 2:
            _parse_document(value, defs, refs)
    return ScipIndex(root=root, defs=defs, refs=dict(refs))


def load_index(path: str, root: str) -> ScipIndex:
    """Прочитать SCIP-индекс из файла."""
    with open(path, "rb") as handle:
        return parse_index(handle.read(), root)


def _parse_document(
    data: bytes,
    defs: dict[str, tuple[str, int]],
    refs: dict[tuple[str, int], set[str]],
) -> None:
    relative_path = ""
    occurrences: list[bytes] = []
    for field_number, wire_type, value in _iter_fields(data):
        if field_number == 1 and wire_type == 2:
            relative_path = value.decode("utf-8", "replace")
        elif field_number == 2 and wire_type == 2:
            occurrences.append(value)
    relative_path = relative_path.replace("\\", "/")
    for occurrence in occurrences:
        _parse_occurrence(occurrence, relative_path, defs, refs)


def _parse_occurrence(
    data: bytes,
    relative_path: str,
    defs: dict[str, tuple[str, int]],
    refs: dict[tuple[str, int], set[str]],
) -> None:
    line0 = -1
    typed_line0: int | None = None
    symbol = ""
    roles = 0
    for field_number, wire_type, value in _iter_fields(data):
        if field_number == 1:
            if wire_type == 2:  # packed repeated int32
                pos = 0
                while pos < len(value):
                    item, pos = _read_varint(value, pos)
                    if line0 < 0:
                        line0 = item
            elif wire_type == 0 and line0 < 0:
                line0 = value
        elif field_number in (8, 9) and wire_type == 2:
            # single_line_range/multi_line_range: первое поле — строка.
            typed_line0 = _first_varint_field(value, 1)
        elif field_number == 2 and wire_type == 2:
            symbol = value.decode("utf-8", "replace")
        elif field_number == 3 and wire_type == 0:
            roles = value
    # Типизированный диапазон имеет приоритет над устаревшим range.
    if typed_line0 is not None:
        line0 = typed_line0
    if line0 < 0 or not symbol:
        return
    if roles & _ROLE_DEFINITION:
        defs.setdefault(symbol, (relative_path, line0))
    else:
        refs.setdefault((relative_path, line0), set()).add(symbol)


def _first_varint_field(data: bytes, number: int) -> int | None:
    """Прочитать первое varint-поле number из вложенного сообщения."""
    for field_number, wire_type, value in _iter_fields(data):
        if field_number == number and wire_type == 0:
            return value
    return None


def parse_symbol(symbol: str) -> tuple[str, str] | None:
    """Разобрать SCIP-символ: (пакет, квалифицированное имя).

    Поддерживаются форматы всех индексёров:

    * Go — scip-go gomod <module> <ver> `<pkg>`/Type#Method().;
    * Python — scip-python python <dist> <ver> `path/mod.py`/func().;
    * JS/TS — scip-typescript npm <pkg> <ver> src/`file.ts`/Class#method().;
    * Java — scip-java maven <group:artifact> <ver> com/x/C#vuln().;
    * C# — scip-dotnet nuget <pkg> <ver> Ns/Class#Method()..

    Не-функции (поля, типы, пространства имён) дают None.
    """
    match = _SYMBOL_HEAD_RE.match(symbol)
    if match is None:
        return None
    scheme = match.group(1)
    package = match.group(3)
    descriptor = match.group(5)
    # Отбросить префикс пакета/модуля в дескрипторе.
    backticked = _BACKTICK_DESCRIPTOR_RE.match(descriptor)
    if backticked is not None:
        descriptor = backticked.group(1)
    elif "/" in descriptor:
        descriptor = descriptor.rsplit("/", 1)[-1]
    # Оставить только функции/методы: name()., name(+N).;
    # для TS/JS также name. (функция-значение/стрелочная функция).
    signature = _SIGNATURE_RE.search(descriptor)
    if signature is not None:
        name = descriptor[: signature.start()]
    elif scheme == "scip-typescript" and descriptor.endswith("."):
        name = descriptor[:-1]
    else:
        return None
    if not name:
        return None
    return package, name.replace("#", ".")


def parse_descriptor_symbol(symbol: str) -> tuple[str, str] | None:
    """Разобрать «относительный» дескриптор-символ (com/x/A#run().).

    Некоторые индексёры (scip-javac) пишут ссылки без заголовка
    scip-java maven .... Из такого символа извлекается путь класса и имя
    метода; пространства имён и типы (без #member) дают None.
    """
    if " " in symbol:
        return None
    signature = _SIGNATURE_RE.search(symbol)
    if signature is None:
        return None
    descriptor = symbol[: signature.start()]
    if "#" not in descriptor:
        return None
    class_path, _, member = descriptor.rpartition("#")
    class_path = class_path.strip("/")
    if not class_path or not member:
        return None
    return class_path, member.replace("`", "")


def relative_to_abs(root: str, relative_path: str) -> str:
    """Нормализовать относительный путь SCIP в абсолютный (для сопоставления)."""
    return os.path.normcase(os.path.abspath(os.path.join(root, relative_path)))


def referenced_modules(index: ScipIndex, candidates: set[str]) -> list[str]:
    """Модули из candidates, на символы которых ссылаются документы индекса.

    Нужны, чтобы индексировать только те зависимости, которые проект реально
    использует, а не весь граф модулей. Сравнение имён регистронезависимо
    (в Python и Maven регистр в разных источниках может отличаться).
    """
    by_lower = {name.lower(): name for name in candidates}
    found: set[str] = set()
    for symbols in index.refs.values():
        for symbol in symbols:
            match = _SYMBOL_HEAD_RE.match(symbol)
            if match is None:
                continue
            candidate = by_lower.get(match.group(3).lower())
            if candidate is not None:
                found.add(candidate)
    return sorted(found)
