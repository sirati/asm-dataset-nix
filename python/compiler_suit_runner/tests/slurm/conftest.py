"""Shared fixtures for the slurm test slice.

Per the test plan ("Pre-flight checks before each test"), every test
in this directory expects:

* a working ``ClusterProbe`` handle (sibling A2 owns the module;
  imported lazily here so a missing module degrades to a skip rather
  than an import error at collection time),
* the host-side log mount path,
* a cleanup hook that runs the between-test cleanup harness on demand,
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


CleanupCallable = Callable[..., Any]
"""Type alias for the ``cleanup_cluster`` fixture's yielded callable.

Concretely returns a :class:`CleanupReport` from
``compiler_suit_runner.tests.slurm.cluster_probe`` but typed loosely
here so a missing ``cluster_probe`` module (during incremental
implementation) doesn't produce import errors at collection time.
"""


@pytest.fixture(scope="session")
def cluster_probe() -> Any:
    """Session-scoped :class:`ClusterProbe` handle.

    The identity-file path is read from ``ASM_CLUSTER_PROBE_KEY`` (env
    var); when unset, falls back to the canonical
    ``.ssh-debug/id_ed25519`` checked into this repo. The fixture
    constructs the probe with a ``GatewayConfig`` carrying that key so
    SSH probes authenticate against the live test-env.
    """
    import os

    try:
        from compiler_suit_runner.tests.slurm import (  # type: ignore[attr-defined]
            cluster_probe as cluster_probe_mod,
        )
    except ImportError as exc:
        pytest.skip(
            f"cluster_probe module not available yet ({exc})"
        )
    probe_cls = getattr(cluster_probe_mod, "ClusterProbe", None)
    if probe_cls is None:
        pytest.skip("ClusterProbe not exported from cluster_probe")

    gateway_cfg_cls = getattr(cluster_probe_mod, "GatewayConfig", None)
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    default_key = repo_root / ".ssh-debug" / "id_ed25519"
    key_path = os.environ.get("ASM_CLUSTER_PROBE_KEY", str(default_key))
    if gateway_cfg_cls is None:
        return probe_cls()
    return probe_cls(gateway=gateway_cfg_cls(identity_file=key_path))


@pytest.fixture
def slurm_log_root() -> pathlib.Path:
    """Host-side mount path of the slurm-test-env's log directory.

    Plain value (not autouse): tests that need the path import this
    fixture; tests that don't, ignore it.
    """
    return SLURM_TEST_ENV_LOG_ROOT


@pytest.fixture
def cleanup_cluster(
    cluster_probe: Any,
) -> Iterator[CleanupCallable]:
    """Per-test cleanup hook implementing plan section "Cleanup harness".

    The fixture runs cleanup at fixture START (before yielding to the
    test), yields a callable so the test can invoke an extra cleanup
    pass mid-test if it needs to (e.g. a failure-injection test that
    wants to assert the cluster recovered before continuing), then
    runs cleanup at fixture END.

    The yielded callable returns a fresh
    :class:`compiler_suit_runner.tests.slurm.cluster_probe.CleanupReport`
    on each call so the test can assert on what got cleaned up.

    The fixture is NOT autouse - tests opt in by listing it (or by
    depending on :func:`fresh_run`, which wires it transitively). This
    matters because the cleanup harness mutates the cluster; tests
    that only inspect read-only state (e.g. parser unit tests) must
    not pay for it.
    """
    cleanup_fn = getattr(cluster_probe, "cleanup", None)
    if cleanup_fn is None:
        pytest.skip(
            "ClusterProbe.cleanup not available; B2 cleanup harness "
            "not yet wired",
        )

    def _run_cleanup(**kwargs: Any) -> Any:
        """Invoke the cleanup harness. ``kwargs`` flow through to
        :meth:`ClusterProbe.cleanup` so individual tests can override
        e.g. ``scancel_pattern`` for failure-injection workloads."""
        return cleanup_fn(**kwargs)

    # START: drain any leftover state from the previous test or run.
    _run_cleanup()

    try:
        yield _run_cleanup
    finally:
        # END: leave the cluster clean regardless of test outcome.
        # Errors here are NOT raised (they land in the
        # CleanupReport.errors list) so a failed cleanup doesn't mask
        # a more interesting test failure.
        _run_cleanup()


@pytest.fixture
def fresh_run(
    cleanup_cluster: CleanupCallable,  # noqa: ARG001 — drives ordering only
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
