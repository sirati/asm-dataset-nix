"""Unit tests for the Q3 fulfillability matcher."""

from __future__ import annotations

import logging
import types

import pytest

from compiler_suit_runner.holding_matcher import (
    UNFULFILLABLE_REASON_TEMPLATE,
    extract_outpaths_from_unfulfillable_reason,
    matcher,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

OUTPATH_X = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-gcc-15.2.0"
OUTPATH_Y = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-clang-19.1.7"
OUTPATH_Z = "/nix/store/cccccccccccccccccccccccccccccccc-gcc-14.3.0"


def _reason(outpath: str, dead=("peer-A", "peer-B")) -> str:
    return UNFULFILLABLE_REASON_TEMPLATE.format(
        outpath=outpath,
        dead_holders=sorted(dead),
    )


def _task(reason):
    return types.SimpleNamespace(reason=reason)


# ---------------------------------------------------------------------------
# matcher() — happy / no-match / edge cases
# ---------------------------------------------------------------------------


def test_matcher_happy_match_single_peer():
    task = _task(_reason(OUTPATH_X))
    holdings = {"peer-A": {OUTPATH_X}}
    assert matcher(task, holdings) is True


def test_matcher_no_match_different_outpath():
    task = _task(_reason(OUTPATH_X))
    holdings = {"peer-A": {OUTPATH_Y}}
    assert matcher(task, holdings) is False


def test_matcher_multiple_peers_one_holds():
    task = _task(_reason(OUTPATH_X))
    holdings = {
        "peer-A": {OUTPATH_X},
        "peer-B": {OUTPATH_Z},
    }
    assert matcher(task, holdings) is True


def test_matcher_multiple_peers_other_holds():
    """Holder is not necessarily the first peer iterated."""
    task = _task(_reason(OUTPATH_X))
    holdings = {
        "peer-A": {OUTPATH_Y},
        "peer-B": {OUTPATH_X},
    }
    assert matcher(task, holdings) is True


def test_matcher_empty_holdings_dict():
    task = _task(_reason(OUTPATH_X))
    assert matcher(task, {}) is False


def test_matcher_empty_holdings_sets():
    task = _task(_reason(OUTPATH_X))
    holdings = {"peer-A": set(), "peer-B": set()}
    assert matcher(task, holdings) is False


def test_matcher_empty_reason_string():
    task = _task("")
    holdings = {"peer-A": {OUTPATH_X}}
    assert matcher(task, holdings) is False


def test_matcher_none_reason():
    task = _task(None)
    holdings = {"peer-A": {OUTPATH_X}}
    assert matcher(task, holdings) is False


def test_matcher_reason_missing_outpath_prefix():
    task = _task("something blew up: /nix/store/zzzz-foo not found")
    holdings = {"peer-A": {OUTPATH_X}}
    assert matcher(task, holdings) is False


def test_matcher_task_view_without_reason_attr():
    """If the framework hands us an object missing .reason, fall through."""
    task = types.SimpleNamespace()  # no .reason
    holdings = {"peer-A": {OUTPATH_X}}
    assert matcher(task, holdings) is False


def test_matcher_logs_info_on_match(caplog):
    task = _task(_reason(OUTPATH_X))
    holdings = {"peer-A": {OUTPATH_X}}
    with caplog.at_level(logging.INFO, logger="compiler_suit_runner.holding_matcher"):
        assert matcher(task, holdings) is True
    assert any(
        "reinject-eligible" in rec.message and OUTPATH_X in rec.message
        for rec in caplog.records
    )


def test_matcher_no_log_on_miss(caplog):
    task = _task(_reason(OUTPATH_X))
    holdings = {"peer-A": {OUTPATH_Y}}
    with caplog.at_level(logging.INFO, logger="compiler_suit_runner.holding_matcher"):
        assert matcher(task, holdings) is False
    assert not any(
        "reinject-eligible" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# extract_outpaths_from_unfulfillable_reason() — standalone parser
# ---------------------------------------------------------------------------


def test_extract_outpath_typical_reason():
    reason = _reason(OUTPATH_X)
    assert extract_outpaths_from_unfulfillable_reason(reason) == [OUTPATH_X]


def test_extract_outpath_empty_string():
    assert extract_outpaths_from_unfulfillable_reason("") == []


def test_extract_outpath_none():
    assert extract_outpaths_from_unfulfillable_reason(None) == []


def test_extract_outpath_no_outpath_prefix():
    assert (
        extract_outpaths_from_unfulfillable_reason(
            "toolchain dead_holders=['peer-A']"
        )
        == []
    )


def test_extract_outpath_does_not_swallow_trailing_fields():
    """Greedy match must stop before whitespace so dead_holders isn't eaten."""
    reason = _reason(OUTPATH_X)
    [hit] = extract_outpaths_from_unfulfillable_reason(reason)
    assert hit == OUTPATH_X
    assert "dead_holders" not in hit


def test_extract_outpath_arbitrary_name_chars():
    """Names can contain dashes, digits, dots — only space/newline stops us."""
    weird = "/nix/store/0123456789abcdef0123456789abcdef-foo-bar.baz_2"
    reason = f"toolchain outpath={weird} dead_holders=[]"
    assert extract_outpaths_from_unfulfillable_reason(reason) == [weird]


def test_extract_outpath_two_paths_returns_both():
    """Defensive: matcher iterates all extracted paths, parser surfaces all."""
    reason = (
        f"toolchain outpath={OUTPATH_X} dead_holders=[] "
        f"also outpath={OUTPATH_Y} dead_holders=[]"
    )
    found = extract_outpaths_from_unfulfillable_reason(reason)
    assert OUTPATH_X in found and OUTPATH_Y in found


# ---------------------------------------------------------------------------
# Constant export contract — the repair worker (#71) imports this
# ---------------------------------------------------------------------------


def test_template_format_contract():
    """If anyone changes the template shape, the matcher regex must follow."""
    rendered = UNFULFILLABLE_REASON_TEMPLATE.format(
        outpath=OUTPATH_X,
        dead_holders=["peer-A"],
    )
    assert extract_outpaths_from_unfulfillable_reason(rendered) == [OUTPATH_X]


def test_template_is_a_string():
    assert isinstance(UNFULFILLABLE_REASON_TEMPLATE, str)
    assert "{outpath}" in UNFULFILLABLE_REASON_TEMPLATE
    assert "{dead_holders}" in UNFULFILLABLE_REASON_TEMPLATE


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
