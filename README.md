# reachfinder

Определение **достижимости** кода пакета-зависимости относительно исходного
кода проекта.

Инструмент отвечает на вопрос: «можно ли дойти из точек входа нашего проекта
до уязвимой функции в пакете-зависимости (или до любого кода этого пакета)?».
Для этого он скачивает исходники всех зависимостей проекта (включая
транзитивные), строит граф вызовов на базе
[trailmark](https://github.com/trailofbits/trailmark) и ищет пути от точек входа
проекта к целевой функции.

## Возможности

- Два режима: конкретная функция/метод (`--function`) и **любой код пакета**
  (без `--function`).
- Определение полного closure зависимостей из локфайлов проекта (с точными версиями).
- Ограничение глубины скачивания зависимостей (`--package-depth`).
- Офлайн-режим: работа только из уже скачанного кэша (`--offline`).
- Скачивание исходников зависимостей в локальный кэш, ключуемый по purl.
- Построение объединённого графа вызовов проекта и зависимостей.
- Склейка нерешённых вызовов (`requests.get(...)`) с определениями в зависимостях.
- Точный резолв вызовов по SCIP-индексу (свой ридер, поддержка шести индексёров).
- Поиск путей **только от точек входа проекта** до цели (путь-свидетель).
- Отчёт в JSON и консоль, опционально mermaid-граф маршрутов.
- Диагностика недостижимости: почему путь не нашёлся и кто вызывает цель.

## Поддерживаемые языки

| Язык (`--language`) | Экосистема | Пакетный менеджер | Источник closure |
| --- | --- | --- | --- |
| `python` | python | pip | `uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml` |
| `go` | go | go modules | `go list -m all`, `go mod graph` |
| `javascript` / `typescript` | js | npm | `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock` |
| `java` | java | Maven | `mvn dependency:tree` (фоллбэк — `dependency:list`) |
| `c_sharp` | dotnet | NuGet | `obj/project.assets.json`, `packages.lock.json` (все проекты решения) |
| `c` / `cpp` | cpp | Conan | `conan graph info`, `conanfile.txt` (best-effort) |

C/C++ и C# — best-effort: в C/C++ нет единого стандарта зависимостей, а пакеты
NuGet часто публикуют только скомпилированные DLL (без исходников).

## Установка

```bash
cd reachfinder
uv pip install -e .
```

Требуется Python ≥ 3.12. Для каждой экосистемы должен быть доступен соответствующий
пакетный менеджер (`pip`, `go`, `npm`, `mvn`, `dotnet`, `conan`).

### Docker

```bash
docker build -t reachfinder .
docker run --rm -v "$PWD:/src" reachfinder reach --project /src \
    --package ... --cache-dir /tmp/cache --output-dir /tmp/out
```

В образе уже есть все индексёры (`scip-go`, `scip-python`, `scip-typescript`,
`scip-dotnet`, `scip-clang`), JDK, Maven, .NET SDK, Conan и заранее скачанные
грамматики tree-sitter (парсинг работает без сети). Для Python-проектов зависимости
нужно установить в окружение контейнера. Проще всего сделать это в том же запуске:

```bash
docker run --rm -v "$PWD:/src" --entrypoint sh reachfinder -c \
    "pip install -r /src/requirements.txt --break-system-packages && \
     reachfinder reach --project /src --package ... --cache-dir /tmp/cache --output-dir /tmp/out"
```

`scip-python` анализирует именно это окружение. После изменения исходников образ
нужно пересобрать: `docker build -t reachfinder .`.

#### Переиспользование кэша между запусками

В примерах выше используется `--cache-dir /tmp/cache` вместе с `--rm`: такой кэш
живёт только внутри контейнера и удаляется после его завершения. Чтобы не
скачивать исходники и не парсить графы заново, вынесите кэш в именованный том
или в каталог на хосте.

Именованный том:

```bash
docker volume create reachfinder_cache

docker run --rm \
    -v "$PWD:/src" \
    -v reachfinder_cache:/cache \
    reachfinder reach --project /src --package ... \
    --cache-dir /cache --output-dir /tmp/out
```

Каталог на хосте (удобно, если кэш нужно инспектировать или чистить вручную):

```bash
mkdir -p .reachfinder_cache
docker run --rm \
    -v "$PWD:/src" \
    -v "$PWD/.reachfinder_cache:/cache" \
    reachfinder reach --project /src --package ... \
    --cache-dir /cache --output-dir /tmp/out
```

Что важно учитывать:

- `docker run --rm` удаляет контейнер и его анонимные тома, но **не** удаляет
  именованные тома и bind-mount'ы. Поэтому `reachfinder_cache` переживает
  запуски.
- Без `--cache-dir` кэш по умолчанию пишется в `<project>/.reachfinder_cache`.
  Если проект примонтирован с хоста (`-v "$PWD:/src"`), кэш сохранится на хосте.
  При монтировании проекта только для чтения (`:ro`) запись не удастся — нужен
  явный `--cache-dir` в записываемый каталог или том.
- Проект и кэш нужно монтировать в **одни и те же пути контейнера** (`/src`,
  `/cache`). В отпечаток графов и SCIP-индексов входит абсолютный путь: при
  смене точки монтирования графы и индексы пересоберутся, хотя исходники из кэша
  переиспользуются.
- `--output-dir` тоже стоит вынести на том или на хост, если отчёты
  `reachability.json`/`reachability.mmd` нужны после завершения контейнера.
  Иначе `/tmp/out` исчезнет вместе с контейнером.
- Один и тот же кэш можно использовать для нескольких проектов: записи
  ключуются по purl (`имя@версия`), поэтому пакеты не конфликтуют.

Признак переиспользования — в логе нет строки `Парсинг ... графов` (все графы
взяты из кэша) и запуск завершается заметно быстрее. Если граф проекта или
SCIP-индекс всё равно пересобираются, значит изменился сам проект или его
локфайл.

## Использование

### CLI

```bash
# Проверить достижимость функции yaml.load пакета PyYAML из кода проекта
reachfinder reach --project /path/to/project --package PyYAML --function load

# Проверить, достижим ли ХОТЬ КАКОЙ-ТО код пакета (без --function)
reachfinder reach --project /path/to/project --package PyYAML

# С явным языком и генерацией mermaid-графа маршрутов
reachfinder reach --project . --package requests --function get --language python --graph

# Показать (и скачать) closure зависимостей
reachfinder deps --project . --download
```

Аргументы `reach`:

- `--project` — путь к исходному коду проекта;
- `--package` — имя пакета-зависимости (как в локфайле);
- `--function` — имя функции или метода (`Class.method`). **Необязательный**:
  если не задан, проверяется достижимость любого кода пакета;
- `--language` — язык (`auto` по умолчанию);
- `--output-dir` — каталог результатов (по умолчанию `./reachfinder_output`);
- `--cache-dir` — каталог кэша исходников (по умолчанию `.reachfinder_cache` внутри проекта);
- `--max-depth` — максимальная длина маршрута (по умолчанию `20`);
- `--package-depth` — глубина скачивания зависимостей (`0` — без ограничения, `1` — только прямые,
  `2` — прямые и их прямые и т. д.);
- `--offline` — использовать только уже скачанные исходники из кэша (без скачивания);
- `--graph` — дополнительно сохранить `reachability.mmd` (mermaid).

### Режим «любой код пакета»

Если `--function` не указан, инструмент находит в исходниках пакета **все**
функции и методы и одним прямым обходом графа от всех точек входа проверяет,
какие из них достижимы. В отчёте появляются `mode: "package"`, количество
функций пакета (`package_functions`), количество достижимых
(`reachable_functions`), их список (`reachable_targets`) и маршрут-свидетель
для каждой. Диагноз `reachable` в этом режиме означает «достижим хотя бы один
функционал пакета», а не «достижима конкретная уязвимая функция».

В счётчик функций пакета попадают все функции из скачанных исходников — включая
тестовые файлы (`*_test.go`, каталог `tests/`), если пакетный менеджер их
скачивает. Это завышает общее число функций, но не создаёт ложной достижимости:
тестовый код не вызывается из точек входа проекта. Для C# исходники из
git-репозитория сужаются до каталога проекта самого пакета, поэтому тесты и
примеры в граф не попадают.

```bash
# Достижим ли код chokidar из nodemon (JS)
reachfinder reach --project ./nodemon --package chokidar --language javascript

# Достижим ли код picocli из checkstyle (Java)
reachfinder reach --project ./checkstyle --package picocli --language java
```

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

## Использование по языкам

Общая схема для всех языков:

1. Установите пакетный менеджер и, желательно, SCIP-индексёр для языка.
2. Убедитесь, что в проекте есть манифест/локфайл с точными версиями.
3. По возможности заранее установите зависимости (`venv`, `node_modules`, `dotnet restore`,
   локальный репозиторий Maven, кэш Conan): это нужно и для SCIP, и для офлайна.
4. Запустите `reachfinder reach ...`. `--language` можно не указывать, если язык
   определяется однозначно; при нескольких языках в каталоге укажите его явно.

Достаточно иметь **исходники проекта и манифест/локфайл**: сами пакеты и их
транзитивные зависимости reachfinder скачает в кэш автоматически. Устанавливать
зависимости в окружение нужно только для работы SCIP-индексёра (venv,
`node_modules`, `dotnet restore`, `~/.m2`).

`--package` — имя пакета именно в терминах экосистемы:

| Язык | Формат `--package` | Пример |
| --- | --- | --- |
| Python | имя дистрибутива с PyPI | `PyYAML`, `requests` |
| Go | путь модуля | `github.com/gdamore/tcell/v2` |
| JS/TS | имя npm-пакета | `chokidar`, `@scope/pkg` |
| Java | `group:artifact` или только `artifact` | `com.example:c`, `picocli` |
| C# | ID пакета NuGet | `Newtonsoft.Json` |
| C/C++ | имя рецепта Conan | `zlib`, `demoa` |

Версия берётся из локфайла; указывать её в `--package` не нужно.

### Python

Что должно быть в проекте: один из `uv.lock`, `poetry.lock`, `Pipfile.lock`,
`requirements*.txt`, `pyproject.toml`.

Инструменты: Python 3.12+, `pip`/`uv`; для точного резолва — `scip-python` и
установленное окружение проекта (venv/site-packages). Если `scip-python`
недоступен (например, на Windows), используется name-based резолв.

```bash
# Конкретная функция
reachfinder reach --project /path/to/project \
    --package PyYAML --function load --language python

# Метод класса
reachfinder reach --project /path/to/project \
    --package requests --function Session.get --language python

# Весь пакет
reachfinder reach --project /path/to/project --package requests --language python
```

Для локальных пакетов, собранных в `dist/`, задайте `PIP_FIND_LINKS` — этот же
механизм используется для примеров из `test_projects/python_dep_demo`.

```bash
export PIP_FIND_LINKS=/path/to/dist
reachfinder reach --project ./project --package pkgc --function vuln --language python
```

Если в скачанном артефакте нет `.py` (например, wheel содержит только
скомпилированные модули), отчёт получит диагноз `no_source`; зависимость при
этом всё равно учитывается в графе зависимостей.

### Go

Что должно быть в проекте: `go.mod` и `go.sum`.

Инструменты: Go toolchain; для точного резолва — `scip-go`
(`go install github.com/scip-code/scip-go/cmd/scip-go@latest`).

```bash
# Модуль целиком или конкретная функция
reachfinder reach --project ./project \
    --package github.com/gdamore/tcell/v2 \
    --function NewScreen --language go

# Любой код модуля
reachfinder reach --project ./project \
    --package github.com/gdamore/tcell/v2 --language go
```

`--package` — путь модуля без версии. Поддерживаются локальные `replace`-модули
(они копируются в кэш напрямую). Для офлайна заранее выполните
`go mod download all`, чтобы модульный кэш был заполнен.

Проекты с каталогом `vendor/` поддерживаются отдельно: `go list` запускается с
`-mod=readonly`, чтобы обойти ошибку `can't compute 'all' using the vendor
directory`, а если модульный кэш недоступен — состав зависимостей берётся из
`vendor/modules.txt`, и исходники копируются прямо из `vendor/<module>`. Это
работает без сети. Рёбра для `--package-depth` в vendored-режиме могут быть
неизвестны (тогда ограничение по глубине не применяется и в лог пишется
предупреждение).

### JavaScript / TypeScript

Что должно быть в проекте: `package-lock.json`, `pnpm-lock.yaml` или `yarn.lock`
(подойдёт и `package.json` с зафиксированными версиями).

Инструменты: Node.js и npm; для точного резолва — `scip-typescript` и
установленные `node_modules`.

```bash
# JavaScript
reachfinder reach --project ./nodemon \
    --package chokidar --function watch --language javascript

# TypeScript
reachfinder reach --project ./app \
    --package lodash --function get --language typescript

# Весь пакет
reachfinder reach --project ./nodemon --package chokidar --language javascript
```

Точки входа CLI-проектов берутся из полей `bin`/`main` в `package.json`:
без этого вызовы на верхнем уровне исполняемого файла не считались бы
достижимыми. Для JS без собственного `tsconfig.json` генерируется временный
`tsconfig.reachfinder.json` с `allowJs`/`checkJs` (он удаляется после запуска).

Если `node_modules` не установлены, SCIP-индекс не построится; анализ продолжит
работу с name-based резолвом.

### Java

Что должно быть в проекте: `pom.xml`.

Инструменты: JDK 17+ и Maven; для точного резолва — `scip-javac`, который
подключается к `javac` (отдельный `scip-java` не нужен) и скачивается в
локальный репозиторий Maven при первом запуске.

```bash
# Полное имя артефакта
reachfinder reach --project ./app \
    --package com.example:c --function vuln --language java

# Допустимо только имя артефакта (group ищется в closure)
reachfinder reach --project ./checkstyle \
    --package picocli --function CommandLine.parseArgs --language java

# Весь пакет
reachfinder reach --project ./checkstyle --package picocli --language java
```

Closure и рёбра берутся из `mvn dependency:tree` (фоллбэк — `dependency:list`).
Исходники скачиваются как `-sources.jar`. Тестовые исходники в SCIP не
включаются; для проектов с генерацией кода (ANTLR и т. п.) выполните
`mvn generate-sources` заранее — `target/generated-sources` индексируется.

Для офлайна нужен заполненный `~/.m2/repository` (или `MAVEN_REPO_LOCAL`).

### C# / .NET

Что должно быть в проекте: `.sln` или `.csproj`; зависимости должны быть
восстановлены (`dotnet restore`), чтобы появились `obj/project.assets.json`.
Для решений reachfinder объединяет локфайлы всех проектов.

Инструменты: .NET SDK; для точного резолва — `scip-dotnet`
(`dotnet tool install --global scip-dotnet`). Источник NuGet должен быть
настроен: `dotnet nuget add source https://api.nuget.org/v3/index.json -n nuget.org`.

```bash
# Конкретный метод
reachfinder reach --project ./src/App \
    --package Newtonsoft.Json \
    --function JsonConvert.DeserializeObject --language c_sharp

# Весь пакет
reachfinder reach --project ./src/App \
    --package McMaster.Extensions.CommandLineUtils --language c_sharp
```

Многие пакеты NuGet публикуют только DLL. Если в `.nupkg` нет `.cs`, исходники
**целевого** пакета скачиваются из GitHub-репозитория по полям
`<repository url=... commit=...>` из `.nuspec`. Из репозитория берётся только
каталог `.csproj` самого пакета, чтобы тесты и примеры не попадали в граф.
Если репозитория нет или в нём нет исходников — диагноз `no_source`.

### C / C++

Что должно быть в проекте: `conanfile.txt` или `conanfile.py`.

Инструменты: Conan 2; для точного резолва — `scip-clang` и `clang`, а также
база компиляции `compile_commands.json` (CMake:
`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`).

```bash
cd project
conan profile detect --force

# Пакет из Conan Center
reachfinder reach --project . --package zlib --function compress --language cpp

# Локальный рецепт (сначала экспортируйте его в кэш Conan)
conan export ../demoa
reachfinder reach --project . --package demoa --language cpp
```

`--package` — имя рецепта Conan. Closure и рёбра берутся из
`conan graph info --format=json`. C/C++ — best-effort: перегрузки функций,
шаблоны и header-only библиотеки могут остаться неразрешёнными; в этом случае
отчёт покажет нераскрытые ссылки (`unlinked_boundaries`).

Офлайн-режим для C/C++ полностью проверен в Docker с `--network none`: при
заполненном кэше Conan и готовом `compile_commands.json` `scip-clang` работает
без сети.

## Использование как библиотека

Публичный API экспортируется из пакета `reachfinder`:

```python
from reachfinder import compute_reachability

report = compute_reachability(
    "/path/to/project",
    package="requests",
    function="get",
    language="python",
)

print(report.reachable)          # bool
print(report.diagnosis)          # reachable / no_callers / deps_only / ...
for route in report.entrypoint_paths:
    print(route.entrypoint.name, "->", [hop.name for hop in route.path])
```

### Сигнатура

```python
compute_reachability(
    project_root: str,
    package: str,
    function: str | None = None,
    *,
    language: str = "auto",
    cache_dir: str | None = None,
    max_depth: int = 20,
    package_depth: int | None = None,
    offline: bool = False,
    jobs: int | None = None,
    timeout: int | None = None,
    logger: Logger | None = None,
) -> ReachabilityReport
```

- `function=None` включает режим «любой код пакета»;
- `package_depth` ограничивает closure (`None`/`0` — без ограничения);
- `jobs` — число процессов парсинга (`None`/`0` — авто);
- `logger` — необязательный `Logger`; без него прогресс в консоль не пишется;
- `SourceError` — пакет не найден в closure, не удалось скачать исходники,
  офлайн-кэш неполон;
- `ValueError` — неизвестный язык или несколько языков в проекте без `--language`.

### Результат

`ReachabilityReport` — dataclass, который удобно обрабатывать программно:

| Поле | Назначение |
| --- | --- |
| `reachable` | итоговый ответ: достижима ли цель |
| `diagnosis` | код причины (`reachable`, `not_found`, `no_source`, `no_callers`, `deps_only`, `project_not_entrypoint`) |
| `mode` | `function` или `package` |
| `target_nodes` | найденные определения цели/пакета |
| `reachable_targets` | достижимые функции пакета (режим `package`) |
| `entrypoint_paths` | маршруты-свидетели: точка входа и цепочка `NodeRef` |
| `dependencies` | closure с точными версиями |
| `stats` | размер графа, число склеек, точек входа, функций пакета |
| `callers`, `project_reachers` | доказательства при недостижимости |
| `missing_packages`, `unlinked_boundaries` | что не удалось скачать/склеить |

`report.to_dict()` даёт готовый JSON-совместимый словарь, а
`ReachabilityReport.from_dict()` восстанавливает объект обратно.

### Сохранение и вывод

```python
from reachfinder import Logger, emit_mermaid, print_report, save_report

logger = Logger("out/log/reachfinder.log", verbose=True)
print_report(report, logger)             # консольная сводка
json_path = save_report(report, "out")   # out/reachability.json
mermaid = emit_mermaid(report)           # строка для flowchart TD
```

### Пример для CI

```python
import sys

from reachfinder import SourceError, compute_reachability

try:
    report = compute_reachability(
        ".", package="requests", function="Session.get", language="python"
    )
except SourceError as exc:
    print(f"Не удалось выполнить анализ: {exc}")
    sys.exit(2)

if report.reachable:
    print("Цель достижима — уязвимость потенциально эксплуатируема")
    sys.exit(1)  # блокируем сборку по политике
print(f"Цель недостижима ({report.diagnosis}) — риска эксплуатации не видно")
```

## Основные технические моменты

### Конвейер

1. По `--language` определяется экосистема пакетного менеджера.
2. Из локфайлов проекта извлекается граф зависимостей: полный closure (имя + версия)
   и рёбра «пакет → прямые зависимости» (для `--package-depth`).
3. Исходники каждой зависимости скачиваются в кэш (ключ — purl) и распаковываются.
4. Проект и каждая зависимость парсятся `trailmark` по отдельности; чтобы
   идентификаторы узлов из разных пакетов не пересекались, каждому подграфу
   присваивается уникальный префикс.
5. Нерешённые вызовы (`proxy.unresolved:*`) связываются с определениями в
   зависимостях: сначала собственным резолвером по SCIP-индексу (если доступен
   соответствующий индексёр), затем name-based резолвером как запасной вариант
   (`reachfinder._boundary`).
6. Целевые узлы ищутся в каталоге исходников пакета: конкретная функция по
   имени (`--function`) или все функции/методы пакета (режим «любой код пакета»).
7. В режиме функции обратным обходом графа от цели (линейный BFS) находятся все
   точки входа, которые её достигают, и для каждой — кратчайший путь-свидетель.
   В режиме пакета выполняется один прямой BFS от всех точек входа: он даёт
   глубины, дерево кратчайших путей и множество достижимых функций пакета.
   Полный перебор всех путей (экспоненциальный) не выполняется — иначе на плотных
   графах «проект + зависимости + склейки» поиск зависает.

### Closure зависимостей

Источник истины — локфайл проекта, а не установленное окружение:

| Язык | Откуда берётся closure | Рёбра для `--package-depth` |
| --- | --- | --- |
| Python | `uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml` | зависимости из lock-файлов (где есть) |
| Go | `go list -mod=readonly -m ... all` (для `vendor/` — `vendor/modules.txt`) | `go mod graph` (best-effort) |
| JS/TS | `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `package.json` | да (кроме случаев без рёбер) |
| Java | `mvn dependency:tree` | дерево Maven |
| C# | `obj/project.assets.json` / `packages.lock.json` всех проектов решения | `targets.*.dependencies` |
| C/C++ | `conan graph info --format=json` | граф Conan |

Closure всегда включает транзитивные зависимости. `--package-depth N` оставляет
только пакеты, достижимые от прямых зависимостей за `N` шагов; если экосистема
не отдаёт рёбра, ограничение не применяется (в лог пишется предупреждение).

### Кэш исходников

Все распакованные пакеты лежат в `<cache-dir>/<экосистема>/src/<ключ>/`:

```
<cache-dir>/
├─ python/
│  ├─ meta/<ключ>.json   # purl, имя, версия, путь к исходникам
│  ├─ src/<ключ>/        # исходники пакета
│  └─ graph/, scip/      # кэши графов и SCIP-индексов
├─ go/
├─ js/
├─ java/
├─ dotnet/
└─ cpp/
```

`<ключ>` — первые 16 hex-символов SHA-256 от purl вида
`pkg:<экосистема>/<имя>@<версия>`; `/` и `:` в имени кодируются, поэтому ключ
безопасен как имя каталога. Записи неизменяемы: одна версия пакета скачивается
один раз. Источники копируются, а не перемещаются, если пакетный менеджер
запрещает менять свой кэш (Go, NuGet, JS).

Архивы распаковываются с защитой от zip-slip: отклоняются абсолютные пути,
`..`-компоненты и NUL-байты; tar-архивы распаковываются с `filter="data"`.

По умолчанию кэш лежит в `<project>/.reachfinder_cache`; его можно вынести
флагом `--cache-dir`, например в общий каталог для нескольких проектов. Как
сохранить кэш между запусками контейнера, см. раздел
«Переиспользование кэша между запусками» выше.

### Кэш графов и SCIP-индексов

Граф trailmark для каждого каталога сериализуется в
`<cache-dir>/<экосистема>/graph/<ключ>-<отпечаток>.pkl`:

- отпечаток включает **абсолютный путь корня**, относительные пути файлов,
  размеры и `mtime` (наносекунды);
- каталог самого кэша исключается из отпечатка;
- запись пишется атомарно (временный файл + `os.replace`) и проверяется по
  magic/version при чтении;
- абсолютный путь в отпечатке нужен потому, что в графе хранятся абсолютные
  пути узлов: копия того же кода в другом месте (например, после запуска в
  Docker) не должна переиспользовать чужой кэш.

SCIP-индексы кэшируются в `<cache-dir>/scip/<отпечаток>.scip`. Если индекс уже
есть, индексёр повторно не запускается.

### Парсинг и слияние графов

- Проект и каждый пакет парсятся `trailmark` **отдельно**, со своим корнем.
  Это даёт корректные относительные идентификаторы модулей.
- Идентификаторы узлов каждого подграфа префиксуются уникальным
  `<пакет>@<версия>::`; проект получает префикс `project::`. Без этого
  `index.js` из двух npm-пакетов склеились бы в один узел.
- Парсинг нескольких пакетов распараллелен через `ProcessPoolExecutor`
  (`--jobs`); результаты пишутся в кэш по одному.
- Для глубоко вложенных AST (например, сгенерированный ANTLR-код) лимит
  рекурсии Python поднимается до 100 000 кадров.

### Точки входа

Достижимость считается **только от точек входа проекта**. Они берутся из
эвристик `trailmark` (фреймворки, `main`) и дополняются языковыми конвенциями:

- функции/методы с именем `main` без учёта регистра (покрывает C# `Main`);
- для JS/TS — файлы из полей `bin`/`main` в `package.json` (CLI-проекты вроде `nodemon`);
- top-level вызовы модулей (`bin/cli.js` → `main()`) связываются через SCIP:
  trailmark не создаёт для них рёбер, поэтому модуль получает их отдельным проходом.

Уверенность маршрутов не размечается — инструмент не использует LLM.

### Точный резолв вызовов (SCIP)

**Что такое SCIP простыми словами.** Это формат файла-«оглавления» проекта:
индексёр (отдельная программа для конкретного языка) читает исходники всех
пакетов и записывает, где объявлен каждый символ (функция, метод, класс) и где
на него ссылаются. В отличие от текстового поиска по имени, SCIP знает язык и
типы: он различает одноимённые функции из разных пакетов и понимает, что
`foo.bar()` относится к конкретному классу `bar` из конкретной зависимости.

**Зачем он здесь.** Trailmark строит граф по AST и оставляет кросс-пакетные
вызовы нерешёнными (`proxy.unresolved`). Name-based резолвер угадывает цель по
имени и может ошибиться или не справиться при нескольких кандидатах. SCIP даёт
точную ссылку «вызов на строке N файла F → определение в файле G, строка M»,
поэтому путь через границу «проект → зависимость» не теряется.

**Как это устроено в reachfinder.** Индекс читается собственным минимальным
protobuf-ридером (`reachfinder._scip`) — без внешних зависимостей и без CLI
`scip`. Поддерживаются и устаревшее поле `range`, и новый `typed_range` (им
пользуются `scip-javac`/`scip-dotnet`). Символы всех индексёров (`scip-go`,
`scip-python`, `scip-typescript`, `scip-java`/`scip-javac`, `scip-dotnet`,
`scip-clang`) разбираются единым образом. Готовый индекс кэшируется в
`<cache-dir>/scip/<отпечаток>.scip` и переиспользуется между запусками. Если
индексёр недоступен или индекс не построился, инструмент откатывается на
name-based резолвер — результат будет консервативнее (возможны ложные
«не достижимо»). Индексеры запускаются только на хосте/в контейнере, где они
установлены; сам анализ от них не зависит.

| Язык | Индексёр | Требования / особенности |
| --- | --- | --- |
| Go | `scip-go` | Двухфазная индексация: сначала проект, затем только реально используемые модули. |
| Python | `scip-python` | Нужно установленное окружение Python (venv/site-packages). В Docker/Linux работает; на Windows индексёр падает (баг с `path.sep`) — используется name-based резолв. |
| JS/TS | `scip-typescript` | Нужен `node_modules`. Для JS без своего конфига генерируется временный `tsconfig.reachfinder.json` (`allowJs`/`checkJs`). Точки входа — `bin`/`main` из `package.json`. |
| Java | `scip-javac` + `javac` | JDK 17+ и Maven. Плагин и classpath зависимостей разрешаются Maven; scip-java не используется. Тесты исключаются, `target/generated-sources` включаются; для ANTLR-проектов сначала `mvn generate-sources`. |
| C# | `scip-dotnet` | .NET SDK и восстановленные зависимости (`dotnet restore`). Если в `.nupkg` только DLL, исходники **целевого** пакета скачиваются из `<repository url commit>` в `.nuspec` (GitHub). |
| C/C++ | `scip-clang` | Нужен `compile_commands.json`. В символах `scip-clang` пакет — `.`, поэтому цели резолвятся по имени функции. |

### Склейка границ без SCIP

Если SCIP недоступен, работает name-based резолвер (`reachfinder._boundary`):

1. Для каждого proxy-узла берётся `raw_symbol` и разбирается на члена и
   приёмник (`chokidar.watch` → `watch` + `chokidar`, `std::vector::push_back`
   → `push_back` + `std::vector`).
2. Кандидаты выбираются по имени члена среди всех определений графа.
3. Если кандидатов несколько, они фильтруются по приёмнику: полное имя,
   последний компонент, имя файла без расширения, сегменты каталога и **имя
   пакета из id узла**. Последнее важно для npm/Go: каталог кэша — хэш без
   имени пакета, а id содержит `chokidar@3.5.3::...` или
   `github.com/gdamore/tcell/v2@...::...`.
4. Для интерфейсных вызовов допускается ограниченная переоценка: если метод
   вызывается на переменной, он связывается с одноимёнными методами того же
   пакета, но не более чем с 8 реализациями.
5. Несколько вызовов на одной строке фильтруются по имени — это отсекает
   ложные дуги.

Нераскрытые proxy-узлы не удаляются: они попадают в отчёт как
`unlinked_boundaries` — сигнал, что граница не склеилась и результат может быть
консервативным.

### Поиск достижимости

- **Режим функции.** Для каждой целевой функции строится обратная смежность по
  рёбрам `CALLS` и запускается обратный BFS от цели
  (`_reverse_shortest_tree`). За один проход получаются все предки и дерево
  кратчайших путей; для каждой точки входа, попавшей в дерево, восстанавливается
  один маршрут-свидетель (до 200 маршрутов в отчёте).
- **Режим пакета.** Один прямой BFS от всех точек входа
  (`_forward_reachable`) возвращает глубины и дерево путей; достижимые функции
  пакета и их маршруты-свидетели берутся из этих структур. Если достижимых нет,
  запускается обратный BFS для диагностики.
- Оба обхода — `O(V + E)`. Перечисление всех простых путей не используется:
  на графах вида «проект + зависимости + сотни тысяч склеек» оно
  экспоненциально и зависает.
- `--max-depth` ограничивает длину маршрута (по умолчанию 20 рёбер).

### Производительность и устойчивость

- Графы, SCIP-индексы и исходники кэшируются — повторный прогон не повторяет
  скачивание и парсинг.
- Парсинг пакетов распараллелен процессами; SCIP-индексация — самый дорогой
  этап и выполняется один раз на отпечаток проекта.
- Если один пакет не скачался или не распарсился, анализ продолжается без него;
  такие пакеты перечислены в `missing_packages`, а не обрывают весь запуск.
- Таймауты пакетных менеджеров настраиваются флагом `--timeout`.
- Сам анализ не требует сети и не вызывает LLM: все решения детерминированы.

### Офлайн-режим и индексёры

Сами SCIP-индексёры — локальные программы, но их входные данные должны быть
подготовлены заранее:

| Индексёр | Работает офлайн | Что должно быть локально |
| --- | --- | --- |
| `scip-clang` | **Да, проверено** | `compile_commands.json`, заголовки и зависимости |
| `scip-typescript` | Да | `node_modules` |
| `scip-go` | Да | заполненный module cache или `vendor/` |
| `scip-python` | Да | установленное venv/site-packages |
| `scip-dotnet` | Да | `obj/project.assets.json` и глобальный кэш NuGet |
| `scip-javac` | Да, при заполненном `~/.m2` | плагин и зависимости проекта в локальном репозитории Maven |

Флаг `--offline` гарантирует отсутствие **скачивания исходников пакетов** и
использование только `<cache-dir>`. Он не переключает пакетные менеджеры в
офлайн-режим: разрешение closure (`go list`, `mvn dependency:tree`,
`dotnet restore`, `conan graph info`) и построение SCIP-индекса могут обратиться
к сети, если локальные кэши неполны. Если `.scip` уже лежит в кэше, индексёр
повторно не запускается вообще.

Для строгого air-gapped запуска заранее наполните кэши (module cache Go,
`~/.m2`, NuGet, Conan, `node_modules`, venv) и используйте Docker с
`--network none`; C/C++-сценарий проверен именно так.

## Диагностика недостижимости

Если путей от точек входа нет, отчёт выводит «доказательства работы» (размер
графа, число пакетов и склеек, найденные точки входа, определения цели) и код
причины (поле `diagnosis`):

- `not_found` — целевая функция не найдена в исходниках пакета;
- `no_source` — в пакете нет исходников на целевом языке;
- `no_callers` — цель нигде не вызывается в графе (динамический вызов/рефлексия/мёртвый код);
- `deps_only` — цель вызывают только зависимости, код проекта до неё не доходит;
- `project_not_entrypoint` — цель достигают функции проекта, но не из точек входа;
- `reachable` — цель достижима.

В режиме «любой код пакета» те же диагнозы описывают пакет в целом, например
`deps_only` — «функции пакета вызывают только другие зависимости», а
`not_found` — «в исходниках пакета не найдено ни одной функции».

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

Реальные проекты (режим SCIP, Go):

| Проект | Целевой пакет | Функция | Результат |
| --- | --- | --- | --- |
| [fzf](https://github.com/junegunn/fzf) | `github.com/gdamore/tcell/v2` | `NewScreen` | достижима ✓ |
| [glow](https://github.com/charmbracelet/glow) | `github.com/spf13/cobra` | `Execute` | достижима ✓ |

Примеры с SCIP-резолвом (Windows):

| Язык | Пример | Целевой пакет | Результат |
| --- | --- | --- | --- |
| Java | `java_demo` | `com.example:c` | достижима ✓ |
| C# | `csharp_dep_demo` | `DemoLibC` | достижима ✓ |
| TypeScript | `ts_demo` | `left-pad` | достижима ✓ |
| JavaScript | `js_demo` | `left-pad` | достижима ✓ |

### Реальные проекты (Docker/Linux)

| Язык | Проект | Цель | Результат |
| --- | --- | --- | --- |
| Python | `python_dep_demo` | `pkgc.vuln` | ДА (SCIP 4/4, Docker) |
| Python | `instaloader` | `requests.Session.get` | ДА (SCIP, 18 691 склейка; closure 8) |
| Java | `checkstyle` | `picocli.CommandLine.parseArgs` | ДА (SCIP, 259 379 склеек) |
| Java | `checkstyle` | `picocli.CommandLine.execute` | **НЕТ** — корректно: checkstyle его не вызывает |
| JavaScript | `nodemon` | `chokidar.watch` | ДА (после top-level/package entrypoints) |
| C# | `dotnet-outdated` | `McMaster...Execute` | ДА (SCIP 448/1043; исходники из git-репозитория) |
| C/C++ | тестовый Conan-проект | `demoa.a_top` | ДА (SCIP 2/1) |

Дополнительно C/C++-прогон проверен в Docker с полностью отключённой сетью
(`--network none`) при заранее наполненном кэше Conan: `scip-clang` построил
индекс (2 определения, 1 ссылка), цель достижима.

Режим «любой код пакета» (без `--function`):

| Язык | Проект | Целевой пакет | Функций пакета | Достижимо |
| --- | --- | --- | --- | --- |
| Go | `fzf` | `github.com/gdamore/tcell/v2` | 709 | 12 |
| Python | `instaloader` | `requests` | 612 | 101 |
| Java | `checkstyle` | `picocli` | 185 | 14 |
| JavaScript | `nodemon` | `chokidar` | 76 | 67 |
| C# | `dotnet-outdated` | `McMaster.Extensions.CommandLineUtils` | 417 | 87 |
| C/C++ | тестовый Conan-проект | `demoa` | 1 | 1 |
| Go | `fzf` | `golang.org/x/xerrors` | 94 | 0 — **НЕТ**, диагноз `deps_only` |

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
│   ├── _graph_cache.py        # кэш распарсенных графов (pickle + отпечаток)
│   ├── _boundary.py           # склейка proxy-узлов с зависимостями
│   ├── _scip.py               # чтение SCIP-индексов (свой protobuf-ридер)
│   ├── _target.py             # поиск функции или всех функций пакета
│   ├── _reachability.py       # оркестрация конвейера
│   ├── _logger.py             # логгер (файл + консоль, UTF-8)
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
- `CONAN_REMOTE` — имя удалённого Conan-ремоута для `conan download`
  (по умолчанию `conancenter`).
- `NUGET_PACKAGES` — каталог глобального кэша NuGet (по умолчанию
  `~/.nuget/packages`).
- `JAVA_HOME` — JDK, из которого берётся `javac`.
- `REACHFINDER_SCIP_JAVAC_VERSION` — версия плагина `scip-javac`
  (по умолчанию закреплена в коде).
- `PIP_FIND_LINKS` — дополнительные источники для `pip` (локальные wheel/sdist).
- `PYTHONUTF8` — выставляется провайдерами автоматически, чтобы пути и логи с
  кириллицей не искажались в Windows.
