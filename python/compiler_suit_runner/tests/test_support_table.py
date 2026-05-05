"""Tests for ``compiler_suit_runner.support_table``."""

from __future__ import annotations

import pathlib

import pytest

from compiler_suit_runner.support_table import (
    is_supported,
    load_support_table,
)


@pytest.fixture(autouse=True)
def _clear_lru_cache():
    load_support_table.cache_clear()
    yield
    load_support_table.cache_clear()


_SAMPLE_TABLE = """\
- **Passed**: 256
- **Failed**: 41

| Compiler   | i686      | mips64el  | ppc32     |
|------------|-----------|-----------|-----------|
| gcc15      | OK        | OK        | OK        |
| gcc5       | OK        | FAIL      | FAIL      |
| gcc4_4     | OK        | OK        | OK        |
| clang3_5   | OK        | FAIL      | OK        |
| gcc6_old   | n/a       | n/a       | n/a       |

(footnote text below the table)
"""


def _write_table(tmp_path: pathlib.Path, content: str) -> pathlib.Path:
    p = tmp_path / "table.md"
    p.write_text(content)
    return p


def test_load_returns_empty_when_file_missing(tmp_path: pathlib.Path) -> None:
    assert load_support_table(tmp_path / "nope.md") == {}


def test_load_parses_table_cells(tmp_path: pathlib.Path) -> None:
    p = _write_table(tmp_path, _SAMPLE_TABLE)
    table = load_support_table(p)
    assert table[("gcc15", "i686")] == "OK"
    assert table[("gcc15", "mips64el")] == "OK"
    assert table[("gcc5", "mips64el")] == "FAIL"
    assert table[("gcc5", "ppc32")] == "FAIL"
    assert table[("gcc4_4", "i686")] == "OK"
    assert table[("clang3_5", "ppc32")] == "OK"


def test_load_stops_at_table_end(tmp_path: pathlib.Path) -> None:
    p = _write_table(tmp_path, _SAMPLE_TABLE)
    table = load_support_table(p)
    # Footnote text must not parse as a row.
    assert all(
        comp.startswith(("gcc", "clang")) for comp, _arch in table.keys()
    )


def test_is_supported_for_ok(tmp_path: pathlib.Path) -> None:
    p = _write_table(tmp_path, _SAMPLE_TABLE)
    table = load_support_table(p)
    assert is_supported(table, "gcc15", "i686") is True
    assert is_supported(table, "gcc15", "mips64el") is True


def test_is_supported_rejects_fail(tmp_path: pathlib.Path) -> None:
    p = _write_table(tmp_path, _SAMPLE_TABLE)
    table = load_support_table(p)
    assert is_supported(table, "gcc5", "mips64el") is False
    assert is_supported(table, "gcc5", "ppc32") is False
    assert is_supported(table, "clang3_5", "mips64el") is False


def test_is_supported_rejects_na(tmp_path: pathlib.Path) -> None:
    p = _write_table(tmp_path, _SAMPLE_TABLE)
    table = load_support_table(p)
    assert table[("gcc6_old", "i686")] == "n/a"
    assert is_supported(table, "gcc6_old", "i686") is False


def test_x86_64_always_supported(tmp_path: pathlib.Path) -> None:
    """``x86_64`` is the native arch; table.md only lists cross archs."""
    p = _write_table(tmp_path, _SAMPLE_TABLE)
    table = load_support_table(p)
    assert is_supported(table, "gcc15", "x86_64") is True
    assert is_supported(table, "gcc5", "x86_64") is True


def test_unknown_compiler_passes_through(tmp_path: pathlib.Path) -> None:
    """Compilers not listed in the table aren't filtered out."""
    p = _write_table(tmp_path, _SAMPLE_TABLE)
    table = load_support_table(p)
    assert is_supported(table, "future-cc-99", "mips64el") is True


def test_real_table_md_parses(tmp_path: pathlib.Path) -> None:
    """Sanity-check: the actual flake-root table.md parses cleanly."""
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    real = repo_root / "table.md"
    if not real.is_file():
        pytest.skip(f"{real} not present")
    table = load_support_table(real)
    # Spot-check a few canonical rows from the committed table.
    assert table.get(("gcc15", "i686")) == "OK"
    assert table.get(("gcc5", "mips64el")) == "FAIL"
    assert table.get(("clang3_5", "aarch64")) == "FAIL"
    # All known archs should be present in the table headers.
    archs = {arch for _, arch in table.keys()}
    for required in (
        "i686",
        "aarch64",
        "armv7l-hf",
        "mipsel",
        "mips64el",
        "ppc32",
        "ppc64",
        "riscv64",
    ):
        assert required in archs, f"{required} missing from table"
