"""Unit tests for :mod:`compiler_suit_runner.observer`.

Covers the observer-side emitter that resolves locally-valid
toolchain outpaths and feeds them to the framework's
``RustObserverLateJoiner(holdings=...)`` constructor.

Hermetic: ``run_subprocess`` is a recording stub; no real ``nix``
is invoked. The framework class is mocked via a ``factory`` callable
so the tests don't depend on ``dynamic_runner`` being importable.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from compiler_suit_runner import observer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toolchain_drvs(
    path: pathlib.Path,
    triples: list[tuple[str, str, str]],
) -> None:
    """Write a toolchain_drvs file in the format cli.py emits."""
    payload = {
        "toolchain_drvs_by_pair": [list(t) for t in triples],
    }
    path.write_text(json.dumps(payload))


class _Runner:
    """Programmable run_subprocess stub.

    Pattern-matches on argv keyword (``path-info`` vs other) so the
    same stub handles both outpath resolution and the local-validity
    check. Records every call for argv assertions.
    """

    def __init__(self) -> None:
        # drv -> outpath (None means "path-info returns rc=1")
        self.path_info: dict[str, Any] = {}
        # outpath -> bool (None means "not locally valid")
        self.local_valid: dict[str, bool] = {}
        self.calls: list[list[str]] = []
        # If True, every subprocess call returns rc=1 (defensive test).
        self.force_failure = False

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        if self.force_failure:
            return b"", b"forced", 1
        if "path-info" in argv and "--json" in argv:
            # Last positional arg is the drv^* selector.
            sel = argv[-1]
            if not sel.endswith("^*"):
                return b"", b"", 1
            drv = sel[:-2]
            outpath = self.path_info.get(drv)
            if outpath is None:
                return b"", b"missing", 1
            # Modern shape: {outpath: {"deriver": drv, ...}}
            payload = {outpath: {"deriver": drv}}
            return json.dumps(payload).encode("utf-8"), b"", 0
        if "path-info" in argv:
            # Plain ``nix path-info <outpath>`` — local validity probe.
            outpath = argv[-1]
            ok = self.local_valid.get(outpath, False)
            return b"", b"", 0 if ok else 1
        return b"", b"unhandled argv", 1


# ---------------------------------------------------------------------------
# enumerate_local_toolchain_outpaths
# ---------------------------------------------------------------------------


def test_enumerate_happy_path_filters_to_local_only(tmp_path: pathlib.Path) -> None:
    """3 drvs, 2 with locally-valid outpaths, 1 missing → 2 returned."""
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(
        tc_file,
        [
            ("x86_64", "gcc14", "/nix/store/aaa-gcc14.drv"),
            ("aarch64", "gcc14", "/nix/store/bbb-gcc14.drv"),
            ("riscv64", "gcc14", "/nix/store/ccc-gcc14.drv"),
        ],
    )
    runner = _Runner()
    # Two drvs resolve to outpaths AND are locally valid; the third
    # resolves but the local store lacks it.
    runner.path_info = {
        "/nix/store/aaa-gcc14.drv": "/nix/store/aaa-gcc14-out",
        "/nix/store/bbb-gcc14.drv": "/nix/store/bbb-gcc14-out",
        "/nix/store/ccc-gcc14.drv": "/nix/store/ccc-gcc14-out",
    }
    runner.local_valid = {
        "/nix/store/aaa-gcc14-out": True,
        "/nix/store/bbb-gcc14-out": True,
        "/nix/store/ccc-gcc14-out": False,
    }
    holdings = observer.enumerate_local_toolchain_outpaths(
        tc_file, run_subprocess=runner,
    )
    assert holdings == [
        "/nix/store/aaa-gcc14-out",
        "/nix/store/bbb-gcc14-out",
    ]


def test_enumerate_empty_file_returns_empty(tmp_path: pathlib.Path) -> None:
    """A toolchain file with no entries → ``[]``, no subprocess calls."""
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(tc_file, [])
    runner = _Runner()
    holdings = observer.enumerate_local_toolchain_outpaths(
        tc_file, run_subprocess=runner,
    )
    assert holdings == []
    assert runner.calls == []  # nothing to resolve, nothing to probe


def test_enumerate_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    """Missing file → ``[]``, no exception, no subprocess calls."""
    tc_file = tmp_path / "does_not_exist.json"
    runner = _Runner()
    holdings = observer.enumerate_local_toolchain_outpaths(
        tc_file, run_subprocess=runner,
    )
    assert holdings == []
    assert runner.calls == []


def test_enumerate_subprocess_failure_returns_empty(
    tmp_path: pathlib.Path,
) -> None:
    """Every subprocess hiccup → no holdings (defensive, observer
    must not fail the whole framework on a transient nix outage).
    """
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(
        tc_file,
        [("x86_64", "gcc14", "/nix/store/aaa-gcc14.drv")],
    )
    runner = _Runner()
    runner.force_failure = True
    holdings = observer.enumerate_local_toolchain_outpaths(
        tc_file, run_subprocess=runner,
    )
    assert holdings == []


def test_enumerate_malformed_json_returns_empty(
    tmp_path: pathlib.Path,
) -> None:
    """Corrupt toolchain_drvs file → ``[]``, no exception."""
    tc_file = tmp_path / "toolchain_drvs.json"
    tc_file.write_text("{not valid json")
    runner = _Runner()
    holdings = observer.enumerate_local_toolchain_outpaths(
        tc_file, run_subprocess=runner,
    )
    assert holdings == []


def test_enumerate_drops_drv_with_path_info_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Per-drv resolution failure drops that drv but not others."""
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(
        tc_file,
        [
            ("x86_64", "gcc14", "/nix/store/aaa-gcc14.drv"),
            ("aarch64", "gcc14", "/nix/store/bbb-gcc14.drv"),
        ],
    )
    runner = _Runner()
    runner.path_info = {
        # aaa resolves; bbb missing (path-info returns rc=1).
        "/nix/store/aaa-gcc14.drv": "/nix/store/aaa-gcc14-out",
    }
    runner.local_valid = {"/nix/store/aaa-gcc14-out": True}
    holdings = observer.enumerate_local_toolchain_outpaths(
        tc_file, run_subprocess=runner,
    )
    assert holdings == ["/nix/store/aaa-gcc14-out"]


# ---------------------------------------------------------------------------
# build_observer_late_joiner / wrapper
# ---------------------------------------------------------------------------


class _FakeLateJoiner:
    """Stand-in for the framework's ``RustObserverLateJoiner``."""

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.set_calls: list[list[str]] = []

    def set_holdings(self, new: list[str]) -> None:
        self.set_calls.append(list(new))


def test_build_observer_late_joiner_passes_holdings_kwarg(
    tmp_path: pathlib.Path,
) -> None:
    """The wrapper hands the resolved holdings to the framework class."""
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(
        tc_file,
        [("x86_64", "gcc14", "/nix/store/aaa-gcc14.drv")],
    )
    runner = _Runner()
    runner.path_info = {
        "/nix/store/aaa-gcc14.drv": "/nix/store/aaa-gcc14-out",
    }
    runner.local_valid = {"/nix/store/aaa-gcc14-out": True}

    captured: dict[str, Any] = {}

    def _factory(**kwargs: Any) -> _FakeLateJoiner:
        captured.update(kwargs)
        return _FakeLateJoiner(**kwargs)

    wrapper = observer.build_observer_late_joiner(
        tc_file,
        late_joiner_factory=_factory,
        run_subprocess=runner,
    )
    assert wrapper.holdings == ["/nix/store/aaa-gcc14-out"]
    assert captured == {"holdings": ["/nix/store/aaa-gcc14-out"]}
    assert isinstance(wrapper.late_joiner, _FakeLateJoiner)
    assert wrapper.late_joiner.init_kwargs == {
        "holdings": ["/nix/store/aaa-gcc14-out"],
    }


def test_build_observer_late_joiner_merges_extra_kwargs(
    tmp_path: pathlib.Path,
) -> None:
    """``extra_kwargs`` are forwarded alongside ``holdings``."""
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(tc_file, [])  # empty -> holdings=[]
    captured: dict[str, Any] = {}

    def _factory(**kwargs: Any) -> _FakeLateJoiner:
        captured.update(kwargs)
        return _FakeLateJoiner(**kwargs)

    wrapper = observer.build_observer_late_joiner(
        tc_file,
        late_joiner_factory=_factory,
        run_subprocess=_Runner(),
        extra_kwargs={"peer_info_dir": "/tmp/peers"},
    )
    assert wrapper.holdings == []
    assert captured == {
        "holdings": [],
        "peer_info_dir": "/tmp/peers",
    }


def test_build_observer_late_joiner_empty_holdings_when_no_file(
    tmp_path: pathlib.Path,
) -> None:
    """Missing toolchain_drvs file → wrapper still constructs with
    ``holdings=[]`` (framework treats as non-hosting observer)."""
    tc_file = tmp_path / "missing.json"

    def _factory(**kwargs: Any) -> _FakeLateJoiner:
        return _FakeLateJoiner(**kwargs)

    wrapper = observer.build_observer_late_joiner(
        tc_file,
        late_joiner_factory=_factory,
        run_subprocess=_Runner(),
    )
    assert wrapper.holdings == []
    assert wrapper.late_joiner.init_kwargs == {"holdings": []}


def test_refresh_re_enumerates_and_invokes_setter_when_available(
    tmp_path: pathlib.Path,
) -> None:
    """``refresh`` re-reads the file and calls
    ``late_joiner.set_holdings`` when the framework exposes it."""
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(tc_file, [])
    runner = _Runner()

    def _factory(**kwargs: Any) -> _FakeLateJoiner:
        return _FakeLateJoiner(**kwargs)

    wrapper = observer.build_observer_late_joiner(
        tc_file,
        late_joiner_factory=_factory,
        run_subprocess=runner,
    )
    assert wrapper.holdings == []

    # Operator builds a toolchain locally; rewrite the file and the
    # runner state to reflect the new local-store contents.
    _write_toolchain_drvs(
        tc_file,
        [("x86_64", "gcc14", "/nix/store/aaa-gcc14.drv")],
    )
    runner.path_info = {
        "/nix/store/aaa-gcc14.drv": "/nix/store/aaa-gcc14-out",
    }
    runner.local_valid = {"/nix/store/aaa-gcc14-out": True}

    refreshed = wrapper.refresh()
    assert refreshed == ["/nix/store/aaa-gcc14-out"]
    assert wrapper.holdings == ["/nix/store/aaa-gcc14-out"]
    # Setter invoked once with the new list.
    assert isinstance(wrapper.late_joiner, _FakeLateJoiner)
    assert wrapper.late_joiner.set_calls == [
        ["/nix/store/aaa-gcc14-out"],
    ]


def test_refresh_tolerates_late_joiner_without_setter(
    tmp_path: pathlib.Path,
) -> None:
    """If the framework class has no ``set_holdings``, refresh still
    updates the wrapper's view; next ``PrimaryChanged`` re-announces."""
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(tc_file, [])

    class _NoSetter:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    wrapper = observer.build_observer_late_joiner(
        tc_file,
        late_joiner_factory=_NoSetter,
        run_subprocess=_Runner(),
    )
    # No setter, no error.
    refreshed = wrapper.refresh()
    assert refreshed == []
    assert wrapper.holdings == []


# ---------------------------------------------------------------------------
# Argv shape: ensure --extra-experimental-features is passed (so nix CLI
# accepts the new-style subcommand on stock nix).
# ---------------------------------------------------------------------------


def test_path_info_argv_includes_experimental_features(
    tmp_path: pathlib.Path,
) -> None:
    """argv for ``nix path-info --json <drv>^*`` carries the
    ``--extra-experimental-features nix-command flakes`` flag pair so
    the subcommand is accepted on default nix.conf."""
    tc_file = tmp_path / "toolchain_drvs.json"
    _write_toolchain_drvs(
        tc_file,
        [("x86_64", "gcc14", "/nix/store/aaa-gcc14.drv")],
    )
    runner = _Runner()
    runner.path_info = {
        "/nix/store/aaa-gcc14.drv": "/nix/store/aaa-gcc14-out",
    }
    runner.local_valid = {"/nix/store/aaa-gcc14-out": True}
    observer.enumerate_local_toolchain_outpaths(
        tc_file, run_subprocess=runner,
    )
    # First call is path-info --json; ensure the flag pair is there.
    assert runner.calls, "no subprocess invocations recorded"
    first = runner.calls[0]
    assert "path-info" in first
    assert "--extra-experimental-features" in first
    idx = first.index("--extra-experimental-features")
    assert first[idx + 1] == "nix-command flakes"
    assert first[-1] == "/nix/store/aaa-gcc14.drv^*"
