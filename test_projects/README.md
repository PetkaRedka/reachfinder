# Примеры проектов для end-to-end проверки reachfinder

Каждый проект вызывает «уязвимую» функцию из зависимости. Есть два набора:

- **прямые примеры** (`go_demo`, `js_demo`, `ts_demo`, `csharp_demo`, `cpp_demo`)
  — цель в прямой зависимости (глубина 1);
- **транзитивные примеры** (`*_dep_demo`, `java_demo`) — цель в зависимости
  глубины 3: проект → `a` → `b` → `c`, а исследуемая функция лежит в `c`.

Запуск:

```bash
reachfinder reach --project test_projects/<пример>/<корень> \
    --package <пакет> --function <функция> --language <язык>
```

### Глубина зависимостей и офлайн-режим

Флаг `--package-depth N` ограничивает глубину скачивания: `1` — только прямые
зависимости, `2` — прямые и их прямые, и т. д. (`0` — без ограничения).

В транзитивных примерах (`*_dep_demo`, `java_demo`) цель лежит на глубине 3, поэтому:

```bash
# Глубина 1 оставит только прямую зависимость — цель не найдена:
reachfinder reach --project test_projects/go_dep_demo/project \
    --package example.com/c --function Vuln --language go --package-depth 1
# -> Ошибка: Пакет 'example.com/c' не найден в closure ...

# Глубина 3 включает всю цепочку — цель достижима:
reachfinder reach --project test_projects/go_dep_demo/project \
    --package example.com/c --function Vuln --language go --package-depth 3
```

Флаг `--offline` использует только уже скачанные исходники из кэша (без сети).
Сначала выполните обычный прогон (кэш наполнится), затем:

```bash
reachfinder reach --project <пример> --package <пакет> --function <функция> \
    --language <язык> --offline
```

---

## Python

Прямой: `PyYAML` → `yaml.load`.

```bash
reachfinder reach --project <проект с requirements.txt> \
    --package PyYAML --function load --language python
```

Транзитивный (`python_dep_demo`): `app → pkga → pkgb → pkgc`, цель `pkgc.vuln`.
Локальные пакеты собираются в `dist`, поиск идёт по `PIP_FIND_LINKS`:

```bash
cd test_projects/python_dep_demo
uv run python -m pip wheel ./pkgc ./pkgb ./pkga --no-deps -w dist
reachfinder reach --project test_projects/python_dep_demo/project \
    --package pkgc --function vuln --language python
# с PIP_FIND_LINKS, указывающим на dist:
PIP_FIND_LINKS="<abs>/test_projects/python_dep_demo/dist" reachfinder reach ...
```

Маршрут: `main → pkga.top → top → pkgb.mid → mid → pkgc.vuln → vuln`.

### Проверка недостижимости (в `python_dep_demo`)

В пакеты добавлены функции для всех диагнозов: в `pkgc` — `dead` (никто не
вызывает), `orphan` (вызывает проектный `helper`, но он не точка входа),
`dep_only` (вызывает только зависимость `pkgb.extra`); в `pkgb` — `extra`;
в проекте — `helper.py`.

```bash
export PIP_FIND_LINKS="<abs>/test_projects/python_dep_demo/dist"
reachfinder reach --project test_projects/python_dep_demo/project --package pkgc --function vuln     --language python  # reachable
reachfinder reach --project test_projects/python_dep_demo/project --package pkgc --function dead     --language python  # no_callers
reachfinder reach --project test_projects/python_dep_demo/project --package pkgc --function orphan   --language python  # project_not_entrypoint
reachfinder reach --project test_projects/python_dep_demo/project --package pkgc --function dep_only --language python  # deps_only
reachfinder reach --project test_projects/python_dep_demo/project --package pkgc --function nope     --language python  # not_found
```

---

## Go

Прямой (`go_demo`): `github.com/google/uuid` → `uuid.New`.

```bash
cd test_projects/go_demo && go mod tidy
reachfinder reach --project test_projects/go_demo \
    --package github.com/google/uuid --function New --language go
```

Транзитивный (`go_dep_demo`): `project → a → b → c` (локальные модули через
`replace`), цель `example.com/c` → `Vuln`.

```bash
reachfinder reach --project test_projects/go_dep_demo/project \
    --package example.com/c --function Vuln --language go
```

Маршрут: `main → a.Top → Top → b.Mid → Mid → c.Vuln → Vuln`.

---

## JavaScript

Прямой (`js_demo`): `left-pad` → `leftPad`.

```bash
cd test_projects/js_demo && npm install --package-lock-only
reachfinder reach --project test_projects/js_demo \
    --package left-pad --function leftPad --language javascript
```

Транзитивный (`js_dep_demo`): `project → pkg-a → pkg-b → pkg-c` (локальные
пакеты через `file:`), цель `pkg-c` → `vuln`.

```bash
cd test_projects/js_dep_demo/project && npm install --package-lock-only
reachfinder reach --project test_projects/js_dep_demo/project \
    --package pkg-c --function vuln --language javascript
```

Маршрут: `main → a.top → top → b.mid → mid → c.vuln → vuln`.

---

## TypeScript

Прямой (`ts_demo`) и транзитивный (`ts_dep_demo`, переиспользует пакеты из
`js_dep_demo`) — тот же npm-механизм, но проект парсится как `typescript`.

```bash
cd test_projects/ts_dep_demo/project && npm install --package-lock-only
reachfinder reach --project test_projects/ts_dep_demo/project \
    --package pkg-c --function vuln --language typescript
```

---

## C# (.NET, NuGet)

Прямой (`csharp_demo`): `DemoLib` → `Vuln.Run`. Пакет включает исходник в
`.nupkg` (реальные пакеты NuGet часто содержат только DLL).

Транзитивный (`csharp_dep_demo`): `app → DemoLibA → DemoLibB → DemoLibC`, цель
`DemoLibC` → `Run`.

```bash
cd test_projects/csharp_dep_demo
mkdir -p localfeed
dotnet pack libc -o localfeed
dotnet pack libb -o localfeed
dotnet pack liba -o localfeed
dotnet restore app
reachfinder reach --project test_projects/csharp_dep_demo/app \
    --package DemoLibC --function Run --language c_sharp
```

Маршрут: `Main → DemoLibA.Top.Call → Call → DemoLibB.Mid.Call → Call → DemoLibC.Vuln.Run → Run`.

---

## C/C++ (Conan)

Прямой (`cpp_demo`): `zlib` → `compress`.

```bash
cd test_projects/cpp_demo && conan profile detect --force
reachfinder reach --project test_projects/cpp_demo \
    --package zlib --function compress --language cpp
```

Транзитивный (`cpp_dep_demo`): `project → demoa → demob → democ` (локальные
рецепты Conan через `conan export`), цель `democ` → `c_vuln`.

```bash
cd test_projects/cpp_dep_demo
conan export democ && conan export demob && conan export demoa
reachfinder reach --project test_projects/cpp_dep_demo/project \
    --package democ --function c_vuln --language cpp
```

Маршрут: `main → a_top → b_mid → c_vuln`.

---

## Java (Maven)

Транзитивный (`java_demo`): `app → a → b → c` (модули Maven), цель
`com.example:c` → `vuln`.

```bash
cd test_projects/java_demo
JAVA_HOME=<jdk> mvn install -DskipTests    # модули с -sources.jar
reachfinder reach --project test_projects/java_demo/app \
    --package com.example:c --function vuln --language java
```

Маршрут: `main → A.top → top → B.mid → mid → C.vuln → vuln`.
