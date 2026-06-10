"""End-to-end T23 smoke: dependency_graph common-dep dedup on the slurm-test-env.

T23 verifies that the dependency_graph worker's graph-synthesis pass
actually collapses shared inputs across ≥ 2 variant drvs into
``build_common_dep`` tasks. Without this dedup, every variant would
rebuild glibc / cross-toolchain runtime libs from source, which
defeats the whole point of moving eval into matrix_eval.

Observation channels (post-pickle): the original T23 decoded the
worker's ``_dependency_graph.pkl`` artifact; that handoff was removed
when the dependency_graph worker started STREAMING its descriptors to
the primary as batched custom messages
(:mod:`compiler_suit_runner.streamed_spawn`). The descriptors now
exist only in transient wire messages, so the test reads the two
durable run artifacts that replaced the pickle:

1. ``_dependency_graph_summary.txt`` under ``<matrix_eval_out_dir>``
   (defaults to ``<shared_fs>/dataset/_matrix_eval``) — the worker's
   ``key: value`` summary carrying ``descriptor_count``,
   ``descriptors_by_kind.build_common_dep`` /
   ``descriptors_by_kind.build_variant``, and the planner's
   ``common_deps_*`` category counters.
2. The primary log (submitter stderr — ``cli._setup_logging`` attaches
   on ``sys.stderr``): the ``custom_message_handler: spawn_batch``
   lines emitted per streamed batch and the
   ``dependency_graph handoff reconciled: spawned == total == N``
   reconciliation-barrier line from
   :meth:`SuitTask.on_phase_end("dependency_graph")`.

What this test asserts:

* Run completes cleanly (standard 7-invariant audit;
  ``expected_failure_count=0``).
* ``_dependency_graph_summary.txt`` exists and decodes; the plan
  emitted ≥ 1 ``build_common_dep`` AND ≥ 2 ``build_variant``
  descriptors (two variants of the same package — the precondition
  for sharing; a ``build_common_dep`` descriptor is BY CONSTRUCTION
  one shared sub-derivation deduplicated across the variants that
  reference it, see :mod:`compiler_suit_runner.dependency_graph_planner`).
* The streamed-spawn handoff is transport-complete: ≥ 1 spawn_batch
  line in the primary log, the reconciliation line is present, and
  the reconciled spawn total equals the summary's
  ``descriptor_count`` — what was planned is exactly what was
  spawned, batch loss would fail loudly here.

DROPPED (genuinely unobservable now): the original per-edge assertion
that ≥ 1 common-dep ``task_id`` is referenced by ≥ 2 ``build_variant``
headers' ``task_depends_on``. Per-task dependency tuples are not
persisted by any run artifact anymore — they live only in the spawn
messages and the framework's in-memory TaskInfos. That edge-wiring
contract is covered at unit level instead
(``tests/test_dependency_graph_planner.py`` asserts the descriptor →
header dep translation; ``tests/test_streamed_spawn.py`` +
``tests/test_suit_task.py`` assert the wire codec and the spawn-side
``task_depends_on`` assembly).

Workload shape: ``--packages hello --archs x86_64 --variant-sample 2``
so we get two variants of the same package (e.g. hello-x86_64-O0 +
hello-x86_64-O2). They share glibc + the gcc toolchain runtime outputs
at minimum. Builds on T07's N=4 shape: same probe construction, same
WORKERS list, same invocation modulo the single-package +
``--variant-sample 2`` knobs.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
from typing import Callable, Optional

import pytest

from compiler_suit_runner.tests.slurm.cluster_probe import (
    ClusterProbe,
    GatewayConfig,
)
from compiler_suit_runner.tests.slurm.invariants import (
    InvariantResult,
    RunArtifacts,
    run_all_invariants,
    wait_squeue_empty,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    RunResult,
    default_invocation_for_smoke,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral; we ALWAYS pass it via ``-i`` and never via ssh-agent /
# ``~/.ssh/``.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. ``invariants``
# checks 5-7 walk this list to look for leaks.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Desired secondary count. The plan's dispatch shape pins ``--jobs 4``.
DESIRED_N_SECONDARIES = 4

# T23's invariant load is light (the planner runs once inside the
# single dependency_graph task; the rest of the cluster contributes
# matrix_eval + build work). We tolerate one missing worker — the
# planner still fires.
MIN_IDLE_WORKERS = 3

# Default wall-clock cap. matrix_eval + dependency_graph planning +
# the small variant build set fits comfortably in 1500s; matrix_eval
# is the dominant cost.
DEFAULT_TIMEOUT_S = 1500.0

# Primary-log markers for the streamed-spawn handoff. Shapes must stay
# in sync with ``SuitTask._handle_streamed_spawn_batch`` ("spawn_batch
# seq=%d from %s; spawning %d task(s)") and ``SuitTask.on_phase_end``
# ("dependency_graph handoff reconciled: spawned == total == %d
# (batches=%s, counters=%s)").
_SPAWN_BATCH_RE = re.compile(
    r"custom_message_handler: spawn_batch seq=(?P<seq>\d+) from \S+;"
    r"\s*spawning (?P<count>\d+) task\(s\)"
)
_RECONCILED_RE = re.compile(
    r"dependency_graph handoff reconciled:"
    r"\s*spawned == total == (?P<total>\d+)"
)


def _live_probe() -> ClusterProbe:
    return ClusterProbe(
        GatewayConfig(
            host="sirati@localhost",
            port=2244,
            identity_file=LIVE_KEY_PATH,
            timeout=8.0,
        ),
    )


def _format_results(results: list[InvariantResult]) -> str:
    lines: list[str] = []
    for r in results:
        tag = r.status.upper() if r.status else ("PASS" if r.passed else "FAIL")
        row_suffix = f" rows={len(r.rows)}" if r.rows else ""
        detail = r.detail or "(no detail)"
        lines.append(f"  [{tag}] {r.name}: {detail}{row_suffix}")
    return "\n".join(lines)


def _resolve_timeout() -> float:
    raw = os.environ.get("T23_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _candidate_summary_paths(
    shared_fs: pathlib.Path,
    log_dir: Optional[pathlib.Path],
) -> list[pathlib.Path]:
    """Return every plausible location for ``_dependency_graph_summary.txt``.

    The dependency_graph worker writes to the configured
    ``matrix_eval_out_dir`` — which defaults to
    ``<shared_fs>/dataset/_matrix_eval`` per :func:`cli._build_config`
    (the same directory T20 reads the ``matrix-<binary>.drv.archive``
    files from). Framework-supplied output_dir overrides may land the
    file elsewhere under ``shared_fs``; we probe a small set of
    candidates and fall back to a recursive scan so the test stays
    robust to wiring changes.
    """
    from compiler_suit_runner.workers.dependency_graph_worker.output import (
        DEPENDENCY_GRAPH_SUMMARY,
    )

    candidates: list[pathlib.Path] = [
        shared_fs / "dataset" / "_matrix_eval" / DEPENDENCY_GRAPH_SUMMARY,
        shared_fs / "dataset" / DEPENDENCY_GRAPH_SUMMARY,
        shared_fs / "out" / DEPENDENCY_GRAPH_SUMMARY,
        shared_fs / DEPENDENCY_GRAPH_SUMMARY,
    ]
    if log_dir is not None:
        candidates.append(log_dir / DEPENDENCY_GRAPH_SUMMARY)
    return candidates


def _find_summary_file(
    shared_fs: pathlib.Path,
    log_dir: Optional[pathlib.Path],
) -> Optional[pathlib.Path]:
    """Pick the first existing candidate; fall back to a recursive scan."""
    from compiler_suit_runner.workers.dependency_graph_worker.output import (
        DEPENDENCY_GRAPH_SUMMARY,
    )

    for c in _candidate_summary_paths(shared_fs, log_dir):
        if c.is_file():
            return c
    # Last-resort recursive scan — the file is small, the shared-fs
    # tree is bounded by the test run, and a mismatch between the
    # candidate list and the framework's actual output_dir shows up
    # here rather than as an opaque AssertionError.
    if shared_fs.is_dir():
        for hit in shared_fs.rglob(DEPENDENCY_GRAPH_SUMMARY):
            return hit
    return None


def _parse_summary(path: pathlib.Path) -> dict[str, str]:
    """Decode the ``key: value`` lines of ``_dependency_graph_summary.txt``.

    Values are kept as strings; numeric assertions convert at the call
    site so a malformed value yields a readable failure. A read failure
    raises AssertionError directly — that surfaces the real problem
    (worker crashed mid-write / wrong file) rather than a generic
    OSError.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssertionError(
            f"failed to read dependency_graph summary at {path}: {exc!r}"
        ) from exc
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(": ")
        assert sep, (
            f"malformed summary line (no ': ' separator) in {path}: "
            f"{line!r}"
        )
        out[key] = value
    return out


def _summary_int(summary: dict[str, str], key: str, *, default: int = 0) -> int:
    """Fetch an integer summary field, defaulting when absent.

    ``descriptors_by_kind.*`` keys are only written for kinds that
    occurred, so absence legitimately means zero.
    """
    raw = summary.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise AssertionError(
            f"summary field {key!r} is not an integer: {raw!r}"
        ) from exc


@pytest.mark.slurm_live
def test_t23_dependency_graph_common_dep_dedup(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- ordering only
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- B2 cleanup harness
) -> None:
    """dependency_graph plans common_deps and the streamed handoff spawns the full plan.

    Pre-flight: same gate as T07 (gateway reachable, ``squeue --me``
    empty, ≥ :data:`MIN_IDLE_WORKERS` workers idle). T23 tolerates
    the degraded N=3 path — the dependency_graph task runs on a
    single secondary so worker count only affects how many
    matrix_eval / variant builds run in parallel, not the dedup
    contract itself.

    Dispatch: two variants of one package
    (``--packages hello --variant-sample 2 --archs x86_64``). The
    matrix_eval + dependency_graph split is the only mode now; no
    extra CLI flag is required to engage it.

    Post-flight: standard 7-invariant audit, then locate the worker's
    ``_dependency_graph_summary.txt`` under the shared FS, decode it,
    and assert:

    * ≥ 1 ``build_common_dep`` descriptor was planned (the dedup —
      each one is a shared sub-derivation that multiple variants
      reference) alongside ≥ 2 ``build_variant`` descriptors.
    * The primary log shows ≥ 1 streamed ``spawn_batch`` and the
      ``on_phase_end`` reconciliation line, whose total matches the
      summary's ``descriptor_count`` — the transport-independent
      "planned == spawned" half of the contract.

    Failure surface: the assertion messages dump the parsed summary
    and the primary-log match counts so a CI run yields enough
    context to triage without re-running.
    """
    probe = _live_probe()

    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T23 start; found {len(queued)} "
        f"job(s): {queued!r}"
    )

    sinfo_rows = probe.sinfo_nodes()
    sinfo_by_node = {row.node: row for row in sinfo_rows}
    missing = [w for w in WORKERS if w not in sinfo_by_node]
    assert not missing, (
        f"sinfo missing expected nodes {missing!r}; got "
        f"{sorted(sinfo_by_node)!r}"
    )
    idle_workers = [
        w for w in WORKERS
        if sinfo_by_node[w].state.startswith("idle")
    ]
    if len(idle_workers) < MIN_IDLE_WORKERS:
        pytest.skip(
            f"T23 needs at least {MIN_IDLE_WORKERS} idle worker(s); got "
            f"{len(idle_workers)}: "
            + ", ".join(
                f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            )
        )

    n_secondaries = min(DESIRED_N_SECONDARIES, len(idle_workers))

    # ``default_invocation_for_smoke(jobs=N, workload="medium")`` already
    # pins ``packages=("hello",)``, ``archs=("x86_64",)``,
    # ``variant_sample=2`` and ``--slurm-partition debug``. We override:
    #
    # * ``ssh_identity_file`` — explicit key path (project policy).
    # * ``slurm_cpus_per_task`` — 2 to match the test-env's CPUTot=2.
    # * ``max_variants`` — 2 to record intent; the flag is currently a
    #   documented no-op (see docs/known-issues.md "--max-variants cap
    #   not applied"), so the 2-variant shape actually comes from the
    #   medium workload's ``variant_sample=2`` (two variants is the
    #   minimum for a shared dep to be shared).
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=n_secondaries, workload="medium"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        archs=("x86_64",),
        max_variants=2,
    )

    timeout_s = _resolve_timeout()
    result = fresh_run(invocation, timeout_s=timeout_s)

    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"jobs={n_secondaries} "
        f"log_dir={result.log_dir!s} "
        f"shared_fs={invocation.shared_fs!s}"
    )

    assert result.exit_code == 0, (
        f"compiler_suit_runner submit returned non-zero ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.run_id is not None, (
        f"run_id not parsed from CLI output ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.log_dir is not None and result.log_dir.is_dir(), (
        f"log_dir missing or not a directory ({detail})"
    )

    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after dispatch "
        f"completed ({detail})"
    )

    # Standard 7-invariant audit. ``expected_failure_count=0`` because
    # T23 is a clean-path test: the variant set is tiny (2) and the
    # cross-toolchain envelope is sidestepped via ``archs=("x86_64",)``.
    artifacts = RunArtifacts.from_dir(
        result.log_dir, shared_fs=invocation.shared_fs,
    )
    inv_results = run_all_invariants(
        artifacts,
        probe,
        WORKERS,
        expected_failure_count=0,
    )
    failed = [r for r in inv_results if not r.passed]
    assert not failed, (
        f"invariant(s) failed for {detail}:\n{_format_results(inv_results)}"
    )

    # ------------------------------------------------------------------
    # T23 core assertion (a): the worker's summary artifact shows the
    # planned descriptor mix — ≥ 1 build_common_dep (the dedup) next
    # to ≥ 2 build_variant.
    # ------------------------------------------------------------------
    summary_path = _find_summary_file(invocation.shared_fs, result.log_dir)
    assert summary_path is not None, (
        f"_dependency_graph_summary.txt not found under "
        f"{invocation.shared_fs!s}; checked candidates: "
        + ", ".join(
            str(c) for c in _candidate_summary_paths(
                invocation.shared_fs, result.log_dir,
            )
        )
        + f" ({detail}). Did the dependency_graph worker run? "
        f"Check log_dir for dependency_graph worker output."
    )

    summary = _parse_summary(summary_path)
    common_dep_count = _summary_int(
        summary, "descriptors_by_kind.build_common_dep",
    )
    variant_count = _summary_int(
        summary, "descriptors_by_kind.build_variant",
    )
    descriptor_count = _summary_int(summary, "descriptor_count")

    assert variant_count >= 2, (
        f"dependency_graph planned only {variant_count} build_variant "
        f"descriptor(s) for summary at {summary_path!s} ({detail}); "
        f"--variant-sample 2 must yield ≥ 2 variants of the same "
        f"package or the dedup has nothing to share. Summary:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(summary.items()))
    )
    assert common_dep_count >= 1, (
        f"dependency_graph emitted zero build_common_dep descriptors "
        f"for summary at {summary_path!s} ({detail}). Two variants of "
        f"the same package should share at least one non-toolchain dep "
        f"(e.g. glibc) — a shared input must collapse into one "
        f"build_common_dep task. Summary:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(summary.items()))
    )
    assert descriptor_count == common_dep_count + variant_count, (
        f"summary descriptor_count={descriptor_count} does not equal "
        f"build_common_dep({common_dep_count}) + "
        f"build_variant({variant_count}) for {summary_path!s} "
        f"({detail}); an unknown descriptor kind crept into the plan. "
        f"Summary:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(summary.items()))
    )

    # ------------------------------------------------------------------
    # T23 core assertion (b): the streamed-spawn handoff carried the
    # WHOLE plan to the primary — ≥ 1 spawn_batch line, the
    # reconciliation-barrier line present, and its total equals the
    # summary's descriptor_count (planned == spawned, independent of
    # the message transport).
    # ------------------------------------------------------------------
    primary_log = result.stderr
    batch_counts = [
        int(m.group("count"))
        for m in _SPAWN_BATCH_RE.finditer(primary_log)
    ]
    assert batch_counts, (
        f"no 'custom_message_handler: spawn_batch' line in the primary "
        f"log ({detail}); the dependency_graph worker's streamed "
        f"descriptors never reached SuitTask.custom_message_handler. "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )

    reconciled = _RECONCILED_RE.search(primary_log)
    assert reconciled is not None, (
        f"no 'dependency_graph handoff reconciled' line in the primary "
        f"log ({detail}); on_phase_end's reconciliation barrier never "
        f"confirmed the stream (lost summary message, or the phase "
        f"ended abnormally). spawn_batch counts seen: {batch_counts!r}. "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    spawned_total = int(reconciled.group("total"))

    assert spawned_total == descriptor_count, (
        f"streamed-spawn handoff spawned {spawned_total} task(s) but "
        f"the worker's summary planned {descriptor_count} "
        f"descriptor(s) ({detail}); a batch was lost or double-spawned "
        f"without tripping the reconciliation barrier. spawn_batch "
        f"counts: {batch_counts!r} (sum={sum(batch_counts)})"
    )
    assert sum(batch_counts) == spawned_total, (
        f"primary-log spawn_batch counts sum to {sum(batch_counts)} "
        f"but the reconciliation line reports {spawned_total} "
        f"({detail}); the log stream and the barrier disagree — "
        f"counts: {batch_counts!r}"
    )
