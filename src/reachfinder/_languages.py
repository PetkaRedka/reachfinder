"""Нормализация имён языков и сопоставление с экосистемами пакетных менеджеров.

Инструмент работает на стыке двух классификаций:

1. **Язык trailmark** — имя парсера исходников (``python``, ``go``,
   ``javascript``, ``typescript``, ``java``, ``c``, ``cpp``, ``c_sharp``).
2. **Экосистема** — пакетный менеджер, которым скачиваются исходники
   зависимостей (``python``→pip, ``go``→go modules, ``javascript``/
   ``typescript``→npm, ``java``→maven, ``c_sharp``→nuget, ``c``/``cpp``→conan).
"""

from __future__ import annotations

# Синонимы имён языков (как вводит пользователь) → имя trailmark.
LANGUAGE_ALIASES: dict[str, str] = {
    "python": "python",
    "py": "python",
    "go": "go",
    "golang": "go",
    "javascript": "javascript",
    "js": "javascript",
    "typescript": "typescript",
    "ts": "typescript",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "csharp": "c_sharp",
    "c#": "c_sharp",
    "c_sharp": "c_sharp",
    "cs": "c_sharp",
}

# Экосистемы пакетных менеджеров, поддерживаемые инструментом.
PYTHON_ECOSYSTEM = "python"
GO_ECOSYSTEM = "go"
JS_ECOSYSTEM = "js"
JAVA_ECOSYSTEM = "java"
DOTNET_ECOSYSTEM = "dotnet"
CPP_ECOSYSTEM = "cpp"

# Имя языка trailmark → экосистема.
ECOSYSTEM_BY_LANGUAGE: dict[str, str] = {
    "python": PYTHON_ECOSYSTEM,
    "go": GO_ECOSYSTEM,
    "javascript": JS_ECOSYSTEM,
    "typescript": JS_ECOSYSTEM,
    "java": JAVA_ECOSYSTEM,
    "c": CPP_ECOSYSTEM,
    "cpp": CPP_ECOSYSTEM,
    "c_sharp": DOTNET_ECOSYSTEM,
}

# Экосистема → первичное имя языка trailmark (для парсинга по умолчанию).
LANGUAGE_BY_ECOSYSTEM: dict[str, str] = {
    PYTHON_ECOSYSTEM: "python",
    GO_ECOSYSTEM: "go",
    JS_ECOSYSTEM: "javascript",
    JAVA_ECOSYSTEM: "java",
    DOTNET_ECOSYSTEM: "c_sharp",
    CPP_ECOSYSTEM: "cpp",
}


def normalize_language(name: str) -> str:
    """Привести имя языка, введённое пользователем, к имени trailmark."""
    return LANGUAGE_ALIASES.get(name.strip().lower(), name.strip().lower())


def ecosystem_for_language(language: str) -> str:
    """Вернуть экосистему пакетного менеджера по имени языка trailmark.

    :raises ValueError: если язык не поддерживается инструментом.
    """
    lang = normalize_language(language)
    if lang not in ECOSYSTEM_BY_LANGUAGE:
        supported = ", ".join(sorted(ECOSYSTEM_BY_LANGUAGE))
        raise ValueError(f"Неподдерживаемый язык '{language}' (доступны: {supported})")
    return ECOSYSTEM_BY_LANGUAGE[lang]
