# reachfinder

Определение **достижимости** функции или метода из пакета-зависимости
относительно исходного кода проекта.

Инструмент отвечает на вопрос: «есть ли в известном уязвимом пакете функция,
до которой можно дойти из точек входа нашего проекта?». Для этого он скачивает
исходники всех зависимостей проекта (включая транзитивные), строит граф вызовов
на базе [trailmark](https://github.com/trailofbits/trailmark) и ищет пути от точек
входа проекта к целевой функции.

## Возможности

- Определение полного closure зависимостей из локфайлов проекта (с точными версиями).
- Ограничение глубины скачивания зависимостей (`--package-depth`).
- Офлайн-режим: работа только из уже скачанного кэша (`--offline`).
- Скачивание исходников зависимостей в локальный кэш, ключуемый по purl.
- Построение объединённого графа вызовов проекта и зависимостей.
- Склейка нерешённых вызовов (`requests.get(...)`) с определениями в зависимостях.
- Поиск путей **только от точек входа проекта** до целевой функции.
- Отчёт в JSON и консоль, опционально mermaid-граф маршрутов.

## Поддерживаемые языки

| Язык (`--language`) | Экосистема | Пакетный менеджер | Источник closure |
| --- | --- | --- | --- |
| `python` | python | pip | `uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml` |
| `go` | go | go modules | `go list -m -json all` |
| `javascript` / `typescript` | js | npm | `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock` |
| `java` | java | Maven | `mvn dependency:list` |
| `c_sharp` | dotnet | NuGet | `obj/project.assets.json`, `packages.lock.json` |
| `c` / `cpp` | cpp | Conan | `conan graph info`, `conanfile.txt` (best-effort) |

C/C++ и C# — best-effort: в C/C++ нет единого стандарта зависимостей, а пакеты
NuGet часто публикуют только скомпилированные DLL (без исходников).

## Установка

```bash
uv pip install -e ./reachfinder
```

Требуется Python ≥ 3.12. Для каждой экосистемы должен быть доступен соответствующий
пакетный менеджер (`pip`, `go`, `npm`, `mvn`, `dotnet`, `conan`).

## Использование

### CLI

```bash
# Проверить достижимость функции yaml.load пакета PyYAML из кода проекта
reachfinder reach --project /path/to/project --package PyYAML --function load

# С явным языком и генерацией mermaid-графа маршрутов
reachfinder reach --project . --package requests --function get --language python --graph

# Показать (и скачать) closure зависимостей
reachfinder deps --project . --download
```

Аргументы `reach`:

- `--project` — путь к исходному коду проекта;
- `--package` — имя пакета-зависимости (как в локфайле);
- `--function` — имя функции или метода (`Class.method`);
- `--language` — язык (`auto` по умолчанию);
- `--output-dir` — каталог результатов (по умолчанию `./reachfinder_output`);
- `--cache-dir` — каталог кэша исходников (по умолчанию `.reachfinder_cache` внутри проекта);
- `--max-depth` — максимальная длина маршрута (по умолчанию `20`);
- `--package-depth` — глубина скачивания зависимостей (`0` — без ограничения, `1` — только прямые,
  `2` — прямые и их прямые и т. д.);
- `--offline` — использовать только уже скачанные исходники из кэша (без скачивания);
- `--graph` — дополнительно сохранить `reachability.mmd` (mermaid).

### Глубина зависимостей и офлайн-режим

`--package-depth N` ограничивает, насколько глубоко по графу зависимостей инструмент
скачивает исходники: `1` — только прямые зависимости проекта, `2` — прямые и их
прямые, и так далее (`0` или отсутствие флага — без ограничения). Рёбра графа берутся
из локфайлов (для pip, Go, npm, NuGet, Maven `dependency:tree`, Conan `graph info`).
Если экосистема не отдаёт рёбра (например, `pnpm-lock.yaml`/`yarn.lock`), ограничение
не применяется — скачивается весь closure (в лог пишется предупреждение).

`--offline` не скачивает новые исходники: используются только пакеты, уже лежащие в
кэше. Если чего-то не хватает, инструмент сообщает, каких именно пакетов нет, и
завершается с ошибкой. Разрешение состава зависимостей всё равно выполняется
пакетным менеджером и обычно опирается на его локальные метаданные (модульный кэш
Go, локальный репозиторий Maven, глобальный кэш NuGet и т. п.).

```bash
# Скачать только прямые зависимости (и найти цель среди них)
reachfinder reach --project . --package requests --function get --package-depth 1

# Повторный прогон из кэша, без обращения к сети
reachfinder reach --project . --package requests --function get --offline
```

### Как библиотека

```python
from reachfinder import compute_reachability

report = compute_reachability(
    "/path/to/project",
    package="requests",
    function="get",
    language="python",
)
print(report.reachable)
for route in report.entrypoint_paths:
    print(route.entrypoint.name, "→", [hop.name for hop in route.path])
```

## Как это работает

1. По `--language` определяется экосистема пакетного менеджера.
2. Из локфайлов проекта извлекается граф зависимостей: полный closure (имя + версия)
   и рёбра «пакет → прямые зависимости» (для `--package-depth`).
3. Исходники каждой зависимости скачиваются в кэш (ключ — purl) и распаковываются.
4. Проект и каждая зависимость парсятся `trailmark` по отдельности; чтобы
   идентификаторы узлов из разных пакетов не пересекались, каждому подграфу
   присваивается уникальный префикс.
5. Нерешённые вызовы (`proxy.unresolved:*`) связываются с определениями в
   зависимостях собственным резолвером (`reachfinder._boundary`).
6. Целевая функция ищется по имени и каталогу исходников пакета.
7. Для каждой точки входа проекта, которая транзитивно вызывает цель,
   вычисляются конкретные пути вызовов.

Достижимость считается **только от точек входа проекта**. Точки входа берутся из
эвристик `trailmark` (фреймворки, `main`) и дополняются языковой конвенцией:
функции/методы с именем `main` без учёта регистра (покрывает C# `Main`).
Уверенность маршрутов не размечается — инструмент не использует LLM.

## Диагностика недостижимости

Если путей от точек входа нет, отчёт выводит «доказательства работы» (размер
графа, число пакетов и склеек, найденные точки входа, определения цели) и код
причины (поле `diagnosis`):

- `not_found` — функция не найдена в исходниках пакета;
- `no_callers` — цель нигде не вызывается в графе (динамический вызов/рефлексия/мёртвый код);
- `deps_only` — цель вызывают только зависимости, код проекта до неё не доходит;
- `project_not_entrypoint` — цель достигают функции проекта, но не из точек входа;
- `reachable` — функция достижима.

Дополнительно выводятся прямые вызывающие цели и проектные функции, достигающие
цели (но не точки входа). Все поля доступны в `reachability.json`.

## Проверено end-to-end

Прямые зависимости (глубина 1):

| Язык | Пример | Пакет | Маршрут |
| --- | --- | --- | --- |
| Python | `demo` | PyYAML | `main → yaml.load → load` ✓ |
| Go | `go_demo` | `github.com/google/uuid` | `main → uuid.New → New` ✓ |
| JavaScript | `js_demo` | `left-pad` | `main → index:leftPad → leftPad` ✓ |
| TypeScript | `ts_demo` | `left-pad` | `main → src.index:leftPad → leftPad` ✓ |
| C# | `csharp_demo` | `DemoLib` (локальный фид) | `Main → DemoLib.Vuln.Run → Run` ✓ |
| C/C++ | `cpp_demo` | `zlib` (Conan) | `main → compress` ✓ |

Транзитивные зависимости (глубина 3: проект → `a` → `b` → `c`, цель в `c`):

| Язык | Пример | Целевой пакет | Маршрут |
| --- | --- | --- | --- |
| Python | `python_dep_demo` | `pkgc` | `main → pkga.top → pkgb.mid → pkgc.vuln → vuln` ✓ |
| Go | `go_dep_demo` | `example.com/c` | `main → a.Top → b.Mid → c.Vuln → Vuln` ✓ |
| JavaScript | `js_dep_demo` | `pkg-c` | `main → a.top → b.mid → c.vuln → vuln` ✓ |
| TypeScript | `ts_dep_demo` | `pkg-c` | `main → a.top → b.mid → c.vuln → vuln` ✓ |
| C# | `csharp_dep_demo` | `DemoLibC` | `Main → DemoLibA.Top.Call → DemoLibB.Mid.Call → DemoLibC.Vuln.Run → Run` ✓ |
| C/C++ | `cpp_dep_demo` | `democ` | `main → a_top → b_mid → c_vuln` ✓ |
| Java | `java_demo` | `com.example:c` | `main → A.top → B.mid → C.vuln → vuln` ✓ |

Готовые примеры и инструкции по подготовке — в `test_projects/README.md`.

## Структура проекта

```
reachfinder/
├── pyproject.toml
├── README.md
├── src/reachfinder/
│   ├── __init__.py            # публичный API
│   ├── cli.py                 # CLI (reach / deps)
│   ├── _models.py             # DependencyCoordinate, ReachabilityReport, …
│   ├── _languages.py          # нормализация языков и экосистем
│   ├── _cache.py              # кэш исходников по purl
│   ├── _sources.py            # базовый SourceProvider + реестр + утилиты
│   ├── _sources_python.py     # pip
│   ├── _sources_go.py         # go modules
│   ├── _sources_js.py         # npm
│   ├── _sources_java.py       # maven
│   ├── _sources_csharp.py     # nuget
│   ├── _sources_cpp.py        # conan
│   ├── _graph.py              # парсинг/объединение/префиксация графов
│   ├── _boundary.py           # склейка proxy-узлов с зависимостями
│   ├── _target.py             # поиск целевой функции
│   ├── _reachability.py       # оркестрация конвейера
│   └── _report.py             # JSON/консоль/mermaid
└── tests/
```

## Тестирование

```bash
cd reachfinder
uv run pytest tests/
uv run ruff check src tests
```

Тесты не требуют сети: графы строятся на локальных фикстурах, локфайлы и кэш
тестируются на файловых образцах.

## Переменные окружения

- `MAVEN_REPO_LOCAL` — путь к локальному репозиторию Maven (по умолчанию
  `~/.m2/repository`).
- `CONAN_HOME` — корень кэша Conan (по умолчанию `~/.conan2`).
