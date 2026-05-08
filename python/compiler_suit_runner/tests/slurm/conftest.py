"""Shared fixtures for the slurm test slice.

Per the test plan ("Pre-flight checks before each test"), every test
in this directory expects:

* a working ``ClusterProbe`` handle (sibling A2 owns the module;
  imported lazily here so a missing module degrades to a skip rather
  than an import error at collection time),
* the host-side log mount path,
* a cleanup hook (filled in by sibling B2; placeholder for now),
* a ``fresh_run`` callable that wraps :func:`run_compiler_suit` with
  a pre-/post- :func:`clear_incremental_cache` so each row exercises
  the full cache-cold path.
"""

from __future__ import annotations

import pathlib
from typing import Any, Callable, Iterator, Optional

import pytest

from compiler_suit_runner.tests.slurm.run_helpers import (
    SLURM_TEST_ENV_LOG_ROOT,
    RunInvocation,
    RunResult,
    clear_incremental_cache,
    run_compiler_suit,
)


@pytest.fixture(scope="session")
def cluster_probe() -> Any:
    """Session-scoped :class:`ClusterProbe` handle (A2-owned).

    Imported lazily because subagent A2 owns ``cluster_probe.py``;
    until that module lands the slurm tests should ``skip`` rather
    than fail at collection time, so the matrix can be implemented
    in any order.
    """
    try:
        from compiler_suit_runner.tests.slurm import (  # type: ignore[attr-defined]
            cluster_probe as cluster_probe_mod,
        )
    except ImportError as exc:
        pytest.skip(
            f"cluster_probe module not available yet ({exc}); "
            "sibling subagent A2 owns it"
        )
    probe_cls = getattr(cluster_probe_mod, "ClusterProbe", None)
    if probe_cls is None:
        pytest.skip(
            "ClusterProbe not exported from cluster_probe; A2 hasn't "
            "wired its public surface yet"
        )
    return probe_cls()


@pytest.fixture
def slurm_log_root() -> pathlib.Path:
    """Host-side mount path of the slurm-test-env's log directory.

    Plain value (not autouse): tests that need the path import this
    fixture; tests that don't, ignore it.
    """
    return SLURM_TEST_ENV_LOG_ROOT


@pytest.fixture
def cleanup_cluster() -> Iterator[None]:
    """Per-test cleanup hook — placeholder until B2 wires the real harness."""
    # B2 will replace this no-op with the squeue/podman cleanup chain
    # described in plan section "Cleanup harness". Yielding both
    # before and after the test keeps the call shape stable so tests
    # written against this fixture don't change when B2 lands.
    yield
    return


@pytest.fixture
def fresh_run(
    cleanup_cluster: None,  # noqa: ARG001 — drives ordering only
) -> Callable[..., RunResult]:
    """Run :func:`run_compiler_suit` with cache-cold guarantees.

    The cache is wiped before the call (so pre-flight always
    re-evaluates) and after (so the next test starts from a clean
    state regardless of this run's exit status). Extra kwargs flow
    through to :func:`run_compiler_suit`.
    """

    def _run(
        invocation: RunInvocation,
        *,
        timeout_s: float = 1800.0,
        env: Optional[dict[str, str]] = None,
    ) -> RunResult:
        clear_incremental_cache()
        try:
            return run_compiler_suit(
                invocation, timeout_s=timeout_s, env=env,
            )
        finally:
            clear_incremental_cache()

    return _run
