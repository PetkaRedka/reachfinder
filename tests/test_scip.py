"""Тесты разбора SCIP-индекса и SCIP-резолва границ (без сети)."""

from __future__ import annotations

from trailmark.models import CodeEdge, EdgeKind, NodeKind

from reachfinder._boundary import link_boundaries
from reachfinder._scip import (
    ScipIndex,
    parse_descriptor_symbol,
    parse_index,
    parse_symbol,
)

# ---------------------------------------------------------------------------
# Минимальный кодировщик protobuf (для проверки ридера)
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _field_bytes(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _field_varint(number: int, value: int) -> bytes:
    return _varint((number << 3) | 0) + _varint(value)


def _field_string(number: int, text: str) -> bytes:
    return _field_bytes(number, text.encode("utf-8"))


def _occurrence(line: int, symbol: str, roles: int) -> bytes:
    packed_range = _field_bytes(1, _varint(line) + _varint(0) + _varint(3))
    return packed_range + _field_string(2, symbol) + _field_varint(3, roles)


def _typed_occurrence(line: int, symbol: str, roles: int) -> bytes:
    """Occurrence с новым single_line_range (поле 8) вместо range."""
    single_line = _field_varint(1, line)
    return _field_bytes(8, single_line) + _field_string(2, symbol) + _field_varint(3, roles)


def _document(relative_path: str, occurrences: list[bytes]) -> bytes:
    return _field_string(1, relative_path) + b"".join(
        _field_bytes(2, occurrence) for occurrence in occurrences
    )


def _index(documents: list[bytes]) -> bytes:
    return b"".join(_field_bytes(2, document) for document in documents)


def _edge(source: str, target: str) -> CodeEdge:
    return CodeEdge(source_id=source, target_id=target, kind=EdgeKind.CALLS)


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------


def test_parse_symbol() -> None:
    assert parse_symbol("scip-go gomod example.com/m v1.0 `example.com/m`/Run().") == (
        "example.com/m",
        "Run",
    )
    assert parse_symbol("scip-go gomod m v1 `m`/Command#Execute().") == ("m", "Command.Execute")
    # Поле (без ()) — не функция/метод.
    assert parse_symbol("scip-go gomod m v1 `m`/Options#Bash.") is None
    # Python: дескриптор с backtick-префиксом файла.
    assert parse_symbol("scip-python python requests 2.31.0 `requests/api.py`/get().") == (
        "requests",
        "get",
    )
    assert parse_symbol(
        "scip-python python requests 2.31.0 `requests/adapters.py`/HTTPAdapter#send()."
    ) == ("requests", "HTTPAdapter.send")
    # TypeScript/JavaScript: файл в backtick-сегменте, затем дескриптор.
    assert parse_symbol(
        "scip-typescript npm left-pad 1.3.0 `index.d.ts`/leftPad()."
    ) == ("left-pad", "leftPad")
    assert parse_symbol(
        "scip-typescript npm pkg 1.0.0 src/`index.ts`/Class#method()."
    ) == ("pkg", "Class.method")
    # Функция-значение в TS (дескриптор оканчивается на .).
    assert parse_symbol("scip-typescript npm pkg 1.0.0 `index.js`/arrow.") == (
        "pkg",
        "arrow",
    )
    # Java: полный Maven-символ.
    assert parse_symbol("scip-java maven com.example:c 1.0 com/example/c/C#vuln().") == (
        "com.example:c",
        "C.vuln",
    )
    # C#: методы с дискриминатором и конструктор.
    assert parse_symbol("scip-dotnet nuget DemoLibA 1.0.0.0 DemoLibA/Top#Call().") == (
        "DemoLibA",
        "Top.Call",
    )
    assert parse_symbol(
        "scip-dotnet nuget System.Console 8.0.0.0 System/Console#WriteLine(+11)."
    ) == ("System.Console", "Console.WriteLine")
    # Тип (без члена) — не функция.
    assert parse_symbol("scip-dotnet nuget DemoLibA 1.0.0.0 DemoLibA/Top#") is None


def test_parse_descriptor_symbol() -> None:
    # Относительный символ scip-javac (без заголовка scip-java maven ...).
    assert parse_descriptor_symbol("com/example/a/A#top().") == ("com/example/a/A", "top")
    assert parse_descriptor_symbol("A#top().") == ("A", "top")
    assert parse_descriptor_symbol("A#`<init>`().") == ("A", "<init>")
    # Пространства имён, типы и поля — не функции.
    assert parse_descriptor_symbol("com/example/a/") is None
    assert parse_descriptor_symbol("com/example/a/A#") is None
    assert parse_descriptor_symbol("A#field.") is None
    # Полный SCIP-символ с пробелами сюда не относится.
    assert parse_descriptor_symbol("scip-go gomod m v1 `m`/Run().") is None


def test_parse_index_roundtrip() -> None:
    data = _index(
        [
            _document(
                "a.go",
                [
                    _occurrence(10, "sym.ref", 0),
                    _occurrence(20, "sym.def", 1),
                ],
            )
        ]
    )

    index = parse_index(data, "/root")

    assert index.defs == {"sym.def": ("a.go", 20)}
    assert index.refs[("a.go", 10)] == {"sym.ref"}
    assert ("a.go", 20) not in index.refs


def test_parse_index_typed_range() -> None:
    data = _index(
        [
            _document(
                "Main.java",
                [
                    _typed_occurrence(6, "com/example/a/A#top().", 0),
                    _typed_occurrence(5, "com/example/app/Main#main().", 1),
                ],
            )
        ]
    )

    index = parse_index(data, "/root")

    assert index.defs == {"com/example/app/Main#main().": ("Main.java", 5)}
    assert index.refs[("Main.java", 6)] == {"com/example/a/A#top()."}


# ---------------------------------------------------------------------------
# SCIP-резолв границ
# ---------------------------------------------------------------------------


def test_link_boundaries_uses_scip_dep_qual(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit(
                "project::app:main",
                "main",
                NodeKind.FUNCTION,
                file_path="/proj/app.go",
                start_line=5,
            ),
            make_unit(
                "project::proxy.unresolved:dep.Run",
                "dep.Run",
                NodeKind.PROXY,
                file_path="/proj/app.go",
                start_line=10,
            ),
            make_unit("dep@1.0::mod:Run", "Run", NodeKind.FUNCTION, file_path="/dep/mod.go"),
        ]
    )
    graph.edges.append(_edge("project::app:main", "project::proxy.unresolved:dep.Run"))

    scip = ScipIndex(
        root="/proj",
        defs={},
        refs={("app.go", 9): {"scip-go gomod dep v1.0 `dep/mod`/Run()."}},
    )

    added = link_boundaries(graph, scip)

    assert added == 1
    assert any(
        edge.source_id == "project::proxy.unresolved:dep.Run"
        and edge.target_id == "dep@1.0::mod:Run"
        for edge in graph.edges
    )


def test_link_boundaries_scip_project_def(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit(
                "project::app:main",
                "main",
                NodeKind.FUNCTION,
                file_path="/proj/app.go",
                start_line=5,
            ),
            make_unit(
                "project::proxy.unresolved:helper",
                "helper",
                NodeKind.PROXY,
                file_path="/proj/app.go",
                start_line=12,
            ),
            make_unit(
                "project::mod:helper",
                "helper",
                NodeKind.FUNCTION,
                file_path="/proj/mod.go",
                start_line=3,
            ),
        ]
    )
    graph.edges.append(_edge("project::app:main", "project::proxy.unresolved:helper"))

    scip = ScipIndex(
        root="/proj",
        defs={"sym.helper": ("mod.go", 2)},
        refs={("app.go", 11): {"sym.helper"}},
    )

    added = link_boundaries(graph, scip)

    assert added == 1
    assert any(
        edge.target_id == "project::mod:helper"
        for edge in graph.edges
        if edge.source_id == "project::proxy.unresolved:helper"
    )


def test_link_boundaries_java_relative_symbol(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit(
                "project::app:Main.main",
                "main",
                NodeKind.FUNCTION,
                file_path="/proj/src/main/java/com/example/app/Main.java",
                start_line=6,
            ),
            make_unit(
                "project::proxy.unresolved:A.top",
                "A.top",
                NodeKind.PROXY,
                file_path="/proj/src/main/java/com/example/app/Main.java",
                start_line=7,
            ),
            make_unit(
                "com.example:a@1.0::src.main.java.com.example.a.A:A.top",
                "top",
                NodeKind.METHOD,
                file_path="/dep/com/example/a/A.java",
                start_line=6,
            ),
        ]
    )
    graph.edges.append(_edge("project::app:Main.main", "project::proxy.unresolved:A.top"))

    scip = ScipIndex(
        root="/proj",
        defs={},
        refs={("src/main/java/com/example/app/Main.java", 6): {"com/example/a/A#top()."}},
    )

    added = link_boundaries(graph, scip)

    assert added == 1
    assert any(
        edge.target_id == "com.example:a@1.0::src.main.java.com.example.a.A:A.top"
        for edge in graph.edges
        if edge.source_id == "project::proxy.unresolved:A.top"
    )


def test_link_boundaries_filters_same_line_symbols(make_unit, make_graph) -> None:
    graph = make_graph(
        [
            make_unit(
                "project::app:Main.main",
                "main",
                NodeKind.FUNCTION,
                file_path="/proj/Program.cs",
                start_line=5,
            ),
            make_unit(
                "project::proxy.unresolved:System.Console.WriteLine",
                "System.Console.WriteLine",
                NodeKind.PROXY,
                file_path="/proj/Program.cs",
                start_line=7,
            ),
            make_unit(
                "project::proxy.unresolved:DemoLibA.Top.Call",
                "DemoLibA.Top.Call",
                NodeKind.PROXY,
                file_path="/proj/Program.cs",
                start_line=7,
            ),
            make_unit(
                "System.Console@1.0::System/Console:Console.WriteLine",
                "WriteLine",
                NodeKind.METHOD,
                file_path="/dep/Console.cs",
                start_line=3,
            ),
            make_unit(
                "DemoLibA@1.0::DemoLibA/Top:Top.Call",
                "Call",
                NodeKind.METHOD,
                file_path="/dep/Top.cs",
                start_line=5,
            ),
        ]
    )
    for proxy in (
        "project::proxy.unresolved:System.Console.WriteLine",
        "project::proxy.unresolved:DemoLibA.Top.Call",
    ):
        graph.edges.append(_edge("project::app:Main.main", proxy))

    scip = ScipIndex(
        root="/proj",
        defs={},
        refs={
            ("Program.cs", 6): {
                "scip-dotnet nuget System.Console 8.0.0.0 System/Console#WriteLine().",
                "scip-dotnet nuget DemoLibA 1.0.0.0 DemoLibA/Top#Call().",
            }
        },
    )

    link_boundaries(graph, scip)

    targets = {
        edge.source_id: edge.target_id
        for edge in graph.edges
        if edge.source_id.startswith("project::proxy.unresolved:")
    }
    assert targets["project::proxy.unresolved:System.Console.WriteLine"] == (
        "System.Console@1.0::System/Console:Console.WriteLine"
    )
    assert targets["project::proxy.unresolved:DemoLibA.Top.Call"] == (
        "DemoLibA@1.0::DemoLibA/Top:Top.Call"
    )


def test_link_boundaries_cpp_scip_clang_no_package(make_unit, make_graph) -> None:
    """scip-clang пишет пакет как . — резолв идёт по имени функции."""
    graph = make_graph(
        [
            make_unit(
                "project::app:main",
                "main",
                NodeKind.FUNCTION,
                file_path="/proj/app/main.c",
                start_line=2,
            ),
            make_unit(
                "project::proxy.unresolved:a_top",
                "a_top",
                NodeKind.PROXY,
                file_path="/proj/app/main.c",
                start_line=2,
            ),
            make_unit(
                "demoa@1.0::src.a:a_top",
                "a_top",
                NodeKind.FUNCTION,
                file_path="/dep/src/a.cpp",
                start_line=1,
            ),
        ]
    )
    graph.edges.append(_edge("project::app:main", "project::proxy.unresolved:a_top"))

    scip = ScipIndex(
        root="/proj",
        defs={},
        refs={
            ("app/main.c", 1): {
                "cxx . . $ `<file>/lib/a.h`/a_top(0123456789abcdef).",
            }
        },
    )

    added = link_boundaries(graph, scip)

    assert added == 1
    assert any(
        edge.target_id == "demoa@1.0::src.a:a_top"
        for edge in graph.edges
        if edge.source_id == "project::proxy.unresolved:a_top"
    )


def test_link_boundaries_module_toplevel(make_unit, make_graph) -> None:
    """Top-level вызовы модуля связываются с целями, вызовы внутри функций — нет."""
    graph = make_graph(
        [
            make_unit(
                "project::mod",
                "mod",
                NodeKind.MODULE,
                file_path="/proj/index.js",
                start_line=1,
                end_line=10,
            ),
            make_unit(
                "project::mod:main",
                "main",
                NodeKind.FUNCTION,
                file_path="/proj/index.js",
                start_line=3,
                end_line=5,
            ),
            make_unit(
                "dep@1.0::dep:dep",
                "dep",
                NodeKind.FUNCTION,
                file_path="/dep/index.js",
                start_line=1,
            ),
        ]
    )

    scip = ScipIndex(
        root="/proj",
        defs={},
        refs={
            ("index.js", 1): {"scip-typescript npm dep 1.0 `index.js`/dep()."},
            ("index.js", 3): {"scip-typescript npm dep 1.0 `index.js`/dep()."},
        },
    )

    added = link_boundaries(graph, scip)

    module_edges = [
        edge.target_id for edge in graph.edges if edge.source_id == "project::mod"
    ]
    assert added == 1
    assert module_edges == ["dep@1.0::dep:dep"]
