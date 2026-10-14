"""Тесты нормализации языков и сопоставления экосистем."""

from __future__ import annotations

import pytest

from reachfinder._languages import ecosystem_for_language, normalize_language


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Python", "python"),
        ("py", "python"),
        ("golang", "go"),
        ("JS", "javascript"),
        ("TS", "typescript"),
        ("C++", "cpp"),
        ("C#", "c_sharp"),
        ("csharp", "c_sharp"),
        ("C", "c"),
    ],
)
def test_normalize_language(raw: str, expected: str) -> None:
    assert normalize_language(raw) == expected


@pytest.mark.parametrize(
    ("language", "expected_ecosystem"),
    [
        ("python", "python"),
        ("go", "go"),
        ("javascript", "js"),
        ("typescript", "js"),
        ("java", "java"),
        ("c", "cpp"),
        ("cpp", "cpp"),
        ("c_sharp", "dotnet"),
    ],
)
def test_ecosystem_mapping(language: str, expected_ecosystem: str) -> None:
    assert ecosystem_for_language(language) == expected_ecosystem


def test_ecosystem_unknown_language_raises() -> None:
    with pytest.raises(ValueError):
        ecosystem_for_language("ruby")
