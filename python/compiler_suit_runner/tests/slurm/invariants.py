"""File-only per-run invariant checks for SLURM smoke runs.

This module consumes the locally-mounted run-log tree at
``/home/sirati/.local/state/slurm-test-env/ds-test/home/sirati/slurm/log/run_<TS>/``
and asserts the four invariants that can be verified without SSHing
into the cluster:

1. **Clean exit** — ``slurm_<jobid>.out`` contains both ``secondary
   finished successfully`` and ``Container exited with code: 0``.
2. **No bind errors** — ``slurm_<jobid>.err`` has zero matches for
   ``Address already in use`` or ``EADDRINUSE``.
3. **Manifest count matches** — the number of files under
   ``manifests/`` equals the count of ``task completed ...
   task_type=variant`` lines (with success=true) parsed from
   ``slurm_<jobid>.out``.
4. **Build-failure count expected** — ``build-failures/`` is empty for
   clean-path tests; for failure-injection tests the caller passes the
   expected count.

Checks 5–7 (no leaked podman containers, no leaked listener ports, no
leaked PPID=1 processes) require SSH and live in a sibling module.

The module is also runnable as ``python -m
compiler_suit_runner.tests.slurm.invariants <run_dir>`` as a
post-flight check (exits 0 if all pass, 1 otherwise).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "InvariantResult",
    "RunArtifacts",
    "check_build_failures",
    "check_clean_exit",
    "check_manifest_count_matches",
    "check_no_bind_errors",
    "run_file_invariants",
]


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Each line of the structured Rust log carries ANSI escape codes around
# field markers, e.g.
#     ... task completed [3msecondary[0m=secondary-0 ...
# Strip ANSI before regex-matching so the patterns stay readable.
_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")

# We require BOTH markers in the file (the literal last line is the
# job-cleanup banner, not the success markers).
_CLEAN_EXIT_MARKERS: tuple[str, ...] = (
    "secondary finished successfully",
    "Container exited with code: 0",
)

_BIND_ERROR_RE: re.Pattern[str] = re.compile(
    r"Address already in use|EADDRINUSE"
)

# Match `task completed ... task_type=Some("variant") ... success=true`.
# The structured logger emits keys as `task_type=Some("variant")` (Rust
# `Option<&str>`); we accept the bare `task_type=variant` form too in case
# a future framework version drops the `Some(...)` wrapper.
_VARIANT_COMPLETED_RE: re.Pattern[str] = re.compile(
    r"task completed.*?task_type=(?:Some\(\"variant\"\)|variant)"
    r".*?success=true"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunArtifacts:
    """Filesystem paths inside one run-log directory.

    The directory layout matches the dynamic_runner SLURM container's
    bind-mount: ``run_<TS>/`` containing ``slurm_<jobid>.{out,err}``,
    ``manifests/``, ``build-failures/``, etc.
    """

    run_dir: Path

    @property
    def manifests_dir(self) -> Path:
        return self.run_dir / "manifests"

    @property
    def build_failures_dir(self) -> Path:
        return self.run_dir / "build-failures"

    def slurm_out_files(self) -> list[Path]:
        """All ``slurm_<jobid>.out`` files in ``run_dir`` (sorted)."""
        return sorted(self.run_dir.glob("slurm_*.out"))

    def slurm_err_files(self) -> list[Path]:
        """All ``slurm_<jobid>.err`` files in ``run_dir`` (sorted)."""
        return sorted(self.run_dir.glob("slurm_*.err"))


@dataclass(frozen=True)
class InvariantResult:
    """Outcome of a single invariant check.

    ``detail`` is a short, human-readable description of the failure
    (or a confirmation when ``passed`` is True). On failure, the detail
    quotes the offending line or names the missing artefact so the
    caller can act on it without re-reading the logs.
    """

    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial formatter
        status = "PASS" if self.passed else "FAIL"
        if self.detail:
            return f"[{status}] {self.name}: {self.detail}"
        return f"[{status}] {self.name}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _read_text(path: Path) -> str:
    """Read a file, stripping ANSI escape sequences.

    Returns an empty string for an unreadable file; the calling
    invariant decides whether that is itself a failure.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _strip_ansi(raw)


def _count_dir_entries(path: Path) -> int:
    """Count direct children of ``path`` (files and subdirs).

    Returns 0 if the directory is missing — the parser is defined to
    handle empty/missing dirs gracefully per the harness contract.
    """
    if not path.is_dir():
        return 0
    return sum(1 for _ in path.iterdir())


# ---------------------------------------------------------------------------
# Individual invariants
# ---------------------------------------------------------------------------


def check_clean_exit(artifacts: RunArtifacts) -> InvariantResult:
    """Invariant 1: every ``slurm_*.out`` carries the success markers."""
    name = "clean_exit"
    out_files = artifacts.slurm_out_files()
    if not out_files:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"no slurm_*.out files under {artifacts.run_dir}",
        )

    failures: list[str] = []
    for path in out_files:
        text = _read_text(path)
        if not text:
            failures.append(f"{path.name} is empty or unreadable")
            continue
        missing = [m for m in _CLEAN_EXIT_MARKERS if m not in text]
        if missing:
            failures.append(
                f"{path.name} missing markers: {missing!r}"
            )

    if failures:
        return InvariantResult(
            name=name, passed=False, detail="; ".join(failures)
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=f"{len(out_files)} slurm_*.out file(s) clean",
    )


def check_no_bind_errors(artifacts: RunArtifacts) -> InvariantResult:
    """Invariant 2: zero ``Address already in use``/``EADDRINUSE`` matches."""
    name = "no_bind_errors"
    err_files = artifacts.slurm_err_files()
    if not err_files:
        # Absent .err is not itself a bind error — skip cleanly. A
        # missing .out is an exit-marker failure instead.
        return InvariantResult(
            name=name,
            passed=True,
            detail=f"no slurm_*.err files under {artifacts.run_dir}",
        )

    offenders: list[str] = []
    for path in err_files:
        text = _read_text(path)
        for line in text.splitlines():
            if _BIND_ERROR_RE.search(line):
                offenders.append(f"{path.name}: {line.strip()!r}")

    if offenders:
        return InvariantResult(
            name=name,
            passed=False,
            detail="; ".join(offenders),
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=f"{len(err_files)} slurm_*.err file(s) clean",
    )


def check_manifest_count_matches(
    artifacts: RunArtifacts,
) -> InvariantResult:
    """Invariant 3: ``len(manifests/)`` equals completed-variant count."""
    name = "manifest_count_matches"
    manifest_count = _count_dir_entries(artifacts.manifests_dir)

    completed = 0
    for path in artifacts.slurm_out_files():
        text = _read_text(path)
        completed += len(_VARIANT_COMPLETED_RE.findall(text))

    if manifest_count != completed:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"manifests/ has {manifest_count} entries but "
                f"slurm_*.out reports {completed} completed variant(s)"
            ),
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=f"{manifest_count} manifest(s) match completed variants",
    )


def check_build_failures(
    artifacts: RunArtifacts, expected_count: int
) -> InvariantResult:
    """Invariant 4: ``build-failures/`` matches caller's expected count.

    For clean-path tests pass ``expected_count=0``. For failure-
    injection tests the caller knows how many failures it injected.
    """
    name = "build_failures"
    if expected_count < 0:
        raise ValueError(
            f"expected_count must be >= 0, got {expected_count}"
        )
    actual = _count_dir_entries(artifacts.build_failures_dir)
    if actual != expected_count:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"build-failures/ has {actual} entries but "
                f"expected {expected_count}"
            ),
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=(
            f"build-failures/ has {actual} entries (matches expected)"
        ),
    )


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------


def run_file_invariants(
    artifacts: RunArtifacts, expected_failure_count: int = 0
) -> list[InvariantResult]:
    """Run the four file-only invariants and return their results.

    Results are returned in deterministic order regardless of which
    one(s) failed — the caller can iterate or filter as it pleases.
    """
    return [
        check_clean_exit(artifacts),
        check_no_bind_errors(artifacts),
        check_manifest_count_matches(artifacts),
        check_build_failures(artifacts, expected_failure_count),
    ]


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def _format_results(results: list[InvariantResult]) -> str:
    return "\n".join(str(r) for r in results)


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help"}:
        print(
            "usage: python -m compiler_suit_runner.tests.slurm.invariants "
            "<run_dir> [expected_failure_count]",
            file=sys.stderr,
        )
        return 2

    run_dir = Path(argv[1])
    expected = int(argv[2]) if len(argv) > 2 else 0

    if not run_dir.is_dir():
        print(
            f"error: run_dir does not exist or is not a directory: {run_dir}",
            file=sys.stderr,
        )
        return 2

    artifacts = RunArtifacts(run_dir=run_dir)
    results = run_file_invariants(artifacts, expected_failure_count=expected)
    print(_format_results(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(_main(sys.argv))
