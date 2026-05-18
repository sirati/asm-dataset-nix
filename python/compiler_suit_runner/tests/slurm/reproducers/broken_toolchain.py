"""Reproducer helpers for the post-promotion-with-failure hang (T2).

The framework has an open bug: when a secondary's task fails AFTER it
has been promoted to primary (because the local primary disconnected),
the secondary HANGS instead of draining and exiting. To exercise this
path deterministically the test:

1. Adds a hidden flake attr ``_drvPaths.x86_64-linux.__test_broken__``
   whose buildPhase prints ``TEST_BROKEN`` on stderr and exits 1 — see
   ``flake.nix``.
2. Runs the normal ``compiler_suit_runner submit`` flow against the
   tiny ``hello`` workload (so preflight emits a single phase-3 variant
   manifest), then mutates that manifest's ``payload.drv`` field to
   point at the broken drv before the framework ships the manifest
   tarball to the gateway.
3. Watches secondary logs for ``promoted to primary epoch=N`` and
   ``local primary disconnected``; once both appear, starts a hang
   timer.

This module owns step 1's static facts (the flake attr and the stderr
signature) plus the shape of step 2 (a callback the test installs on
its :class:`run_helpers.RunInvocation` so the manifest swap happens
between preflight and dispatch). The test composes these helpers with
the py-spy capture harness in :mod:`broken_toolchain` siblings.

The functions here are intentionally side-effect-free except for
``mutate_manifests_in_place`` (which by design mutates JSON on disk).
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import shutil
import subprocess
import time
from typing import Callable, Optional

from compiler_suit_runner.tests.slurm.run_helpers import (
    RunInvocation,
    default_invocation_for_smoke,
)


# ---------------------------------------------------------------------------
# Static facts
# ---------------------------------------------------------------------------


BROKEN_FLAKE_ATTR: str = "_drvPaths.x86_64-linux.__test_broken__"
"""Flake attribute path the broken-toolchain repro depends on.

Defined in ``flake.nix`` under the hidden ``__test_broken__`` namespace
so the existing matrix evaluation is unaffected. The double-underscore
prefix matches the plan's "hidden namespace" convention and is what
makes preflight's ``nix-eval-jobs`` pass over ``dataset.<sys>.<pkg>.<arch>``
skip it (the attr lives at a different nesting level).
"""


BROKEN_BUILD_FAILURE_SIGNATURE: str = "TEST_BROKEN"
"""Stderr substring emitted by the broken derivation's buildPhase.

The broken derivation's buildPhase is literally ``echo TEST_BROKEN >&2;
exit 1`` — so any per-failure log entry the framework writes for a build
of this drv WILL contain ``TEST_BROKEN`` somewhere in the captured
stderr. Tests assert on this token to confirm the failure they observed
came from the planted broken drv (and not, e.g., a transient network
glitch that produced a different failure mode).
"""


# ---------------------------------------------------------------------------
# Broken-drv resolution (live-cluster path)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class BrokenDrvInfo:
    """Resolved broken drv path, plus how we got it.

    ``drv_path`` is what the manifest mutator splices into
    ``payload.drv``. ``raw_eval_argv`` is preserved so a failed test can
    surface the exact command we ran (useful when debugging a
    "broken-drv eval failed" pre-flight skip).
    """

    drv_path: str
    raw_eval_argv: tuple[str, ...]


def resolve_broken_drv_path(
    *,
    flake_ref: str = ".",
    timeout_s: float = 60.0,
) -> BrokenDrvInfo:
    """Evaluate the broken flake attr and return its drv path.

    Uses ``nix eval --raw .#_drvPaths.x86_64-linux.__test_broken__.drvPath``
    so the result is a single ``/nix/store/...drv`` line we can splice
    directly into a manifest's ``payload.drv``. We do NOT instantiate
    via ``nix build`` here: the drv is already realised in the store as
    a side-effect of the eval (nix eval forces drv instantiation), and
    actually building it would just reproduce the failure for no
    benefit.

    Raises :class:`RuntimeError` on any non-zero exit so the test's
    pre-flight gate surfaces a clear cause (e.g. flake-eval failure
    after a ``flake.lock`` change, or a missing ``__test_broken__`` attr).
    """
    argv = [
        "nix",
        "eval",
        "--extra-experimental-features",
        "nix-command flakes",
        "--raw",
        f"{flake_ref}#{BROKEN_FLAKE_ATTR}.drvPath",
    ]
    proc = subprocess.run(  # noqa: S603 - argv is fully programmatic
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"nix eval {BROKEN_FLAKE_ATTR}.drvPath failed "
            f"(rc={proc.returncode}): {proc.stderr.strip()[-500:]}"
        )
    drv = proc.stdout.strip()
    if not drv.startswith("/nix/store/") or not drv.endswith(".drv"):
        raise RuntimeError(
            f"nix eval {BROKEN_FLAKE_ATTR}.drvPath returned an "
            f"unexpected value: {drv!r}"
        )
    return BrokenDrvInfo(drv_path=drv, raw_eval_argv=tuple(argv))


# ---------------------------------------------------------------------------
# Manifest mutation
# ---------------------------------------------------------------------------


_VARIANT_ITEM_CLASS: str = "build_variant"
"""``item_class`` of the manifests we want to repoint at the broken drv.

The tiny invocation produces exactly one build_variant manifest
(plus toolchain_validate + build_common_dep manifests). Mutating
only the build_variant manifest forces the secondary to dispatch
the broken build during the variant phase (the phase the upstream
hang lives in) without disturbing the toolchain bring-up that the
framework needs to function before variants even start.
"""


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestMutationResult:
    """Outcome of one ``mutate_manifests_in_place`` call.

    ``mutated_paths`` is a list (rather than a count) so the test can
    surface the actual filenames in a pytest assertion message — useful
    when debugging "we expected one manifest, found N" mismatches.
    """

    mutated_paths: tuple[pathlib.Path, ...]
    skipped_paths: tuple[pathlib.Path, ...]


def mutate_manifests_in_place(
    manifests_dir: pathlib.Path,
    *,
    broken_drv: str,
    item_class: str = _VARIANT_ITEM_CLASS,
) -> ManifestMutationResult:
    """Rewrite every variant manifest's ``payload.drv`` to ``broken_drv``.

    Walks ``<manifests_dir>/*.json`` (non-recursive — toolchain
    and common-dep manifests live in the same flat directory). Every
    manifest with ``item_class == build_variant`` is rewritten:

    * ``payload.drv`` -> ``broken_drv``;
    * ``payload.attr`` -> ``broken_drv`` (so the secondary's build_worker
      uses the absolute drv path instead of trying to re-resolve a flake
      attr that doesn't exist on the gateway-side container).

    The original ``size`` is preserved (it carries phase-rank packing
    bits that the framework's dispatch ordering depends on). The other
    payload fields (compiler_id, variant_dir, ...) are kept verbatim so
    the framework still classifies the failure as a variant-build
    failure (matching the hang scenario we're trying to reproduce).

    A leading ``_meta.json`` (or any other file whose ``item_class`` is
    not the requested variant class) is left untouched and reported in
    ``skipped_paths``. The mutator is idempotent: running it twice on
    the same dir produces the same on-disk content.
    """
    manifests_dir = pathlib.Path(manifests_dir)
    if not manifests_dir.is_dir():
        raise RuntimeError(
            f"manifests dir does not exist: {manifests_dir}"
        )
    mutated: list[pathlib.Path] = []
    skipped: list[pathlib.Path] = []
    for entry in sorted(manifests_dir.iterdir()):
        if entry.is_dir():
            continue
        if entry.suffix != ".json":
            continue
        # Skip dotfiles and the run-meta file.
        if entry.name.startswith(".") or entry.name.startswith("_"):
            skipped.append(entry)
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append(entry)
            continue
        if not isinstance(data, dict):
            skipped.append(entry)
            continue
        if data.get("item_class") != item_class:
            skipped.append(entry)
            continue
        payload = data.get("payload")
        if not isinstance(payload, dict):
            skipped.append(entry)
            continue
        payload["drv"] = broken_drv
        payload["attr"] = broken_drv
        # Atomically replace via a tmp-file so a concurrent reader (the
        # framework's dispatch packing the manifest tarball) never sees
        # a half-written JSON document.
        tmp = entry.with_suffix(entry.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        tmp.replace(entry)
        mutated.append(entry)
    return ManifestMutationResult(
        mutated_paths=tuple(mutated),
        skipped_paths=tuple(skipped),
    )


# ---------------------------------------------------------------------------
# Manifest watcher (post-preflight, pre-dispatch hook)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestWatcherResult:
    """Outcome of the post-preflight manifest watch.

    ``mutated``: the manifest filenames that got their drv repointed.
    ``polled_for_s``: how long we spent waiting for the manifest dir to
    materialise (useful when debugging timeout-vs-race confusion).
    """

    mutated: tuple[pathlib.Path, ...]
    polled_for_s: float


def watch_and_mutate_manifests(
    manifests_dir: pathlib.Path,
    *,
    broken_drv: str,
    timeout_s: float = 60.0,
    poll_interval_s: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[ManifestWatcherResult]:
    """Block until ``manifests_dir`` contains a phase-3 variant JSON,
    then mutate it in place.

    Used by the test as a thread target so the mutation happens between
    ``compiler_suit_runner submit``'s preflight emission and the SLURM
    dispatch's manifest-tarball pack. The watcher returns as soon as
    the first phase-3 variant manifest appears AND has been mutated; it
    does NOT keep watching for future arrivals (the tiny invocation
    produces exactly one variant manifest, so a single mutation pass is
    sufficient).

    Returns ``None`` on timeout, an
    :class:`ManifestWatcherResult` describing the mutation otherwise.
    """
    manifests_dir = pathlib.Path(manifests_dir)
    started = clock()
    deadline = started + max(timeout_s, 0.0)
    while clock() < deadline:
        if manifests_dir.is_dir():
            try:
                result = mutate_manifests_in_place(
                    manifests_dir, broken_drv=broken_drv,
                )
            except RuntimeError:
                result = None
            if result is not None and result.mutated_paths:
                return ManifestWatcherResult(
                    mutated=result.mutated_paths,
                    polled_for_s=clock() - started,
                )
        sleep(poll_interval_s)
    return None


# ---------------------------------------------------------------------------
# Invocation builder
# ---------------------------------------------------------------------------


def make_broken_run_args(
    base_invocation: RunInvocation,
) -> RunInvocation:
    """Return an invocation scoped to a single broken variant.

    The base invocation is preserved verbatim except for the variant
    sample / cap, which we hard-pin to 1 so preflight emits exactly one
    phase-3 variant manifest. The test installs a separate watcher
    thread that mutates that manifest's ``payload.drv`` to the broken
    flake attr's drv path; this builder doesn't touch the manifests
    itself (mutation is a side-effect that needs the live preflight
    output).

    Why we don't change the package: the framework's preflight assumes
    a real package can be discovered via ``dataset.<sys>.<pkg>.<arch>``,
    which our hidden ``__test_broken__`` attr deliberately is NOT (it
    sits at the system level, not under any pkg). So we keep the
    base ``packages=("hello",)`` for preflight to succeed, then swap the
    drv at manifest-write time.
    """
    return dataclasses.replace(
        base_invocation,
        packages=("hello",) if not base_invocation.packages else base_invocation.packages,
        variant_sample=1,
        max_variants=1,
    )


def make_broken_smoke_invocation(**overrides: object) -> RunInvocation:
    """Convenience: build a tiny smoke invocation pre-tweaked for T2.

    Equivalent to ``make_broken_run_args(default_invocation_for_smoke(
    jobs=1, workload="tiny"))`` with any caller-supplied
    ``dataclasses.replace`` overrides applied last (so the test can
    inject ``ssh_identity_file`` / ``slurm_cpus_per_task`` without
    re-importing :func:`default_invocation_for_smoke`).
    """
    base = make_broken_run_args(
        default_invocation_for_smoke(jobs=1, workload="tiny"),
    )
    if overrides:
        base = dataclasses.replace(base, **overrides)
    return base


# ---------------------------------------------------------------------------
# Failure-signature helpers
# ---------------------------------------------------------------------------


def expected_failure_signature() -> str:
    """Return the exact stderr token the broken drv emits.

    The build-failure log entry the framework writes per failed item
    captures the drv's stderr; tests grep for this signature to confirm
    the failure they observed came from our planted broken drv. Returned
    as a literal string (not a regex) — the underlying token has no
    metacharacters.
    """
    return BROKEN_BUILD_FAILURE_SIGNATURE


def build_failure_log_carries_signature(
    build_failures_dir: pathlib.Path,
    *,
    signature: Optional[str] = None,
) -> bool:
    """Return ``True`` iff any file under ``build-failures/`` contains
    the broken-drv signature.

    The framework writes one log per failed item under
    ``run_dir/build-failures/<task_id>.log``. We don't pin the filename
    (it's the framework's choice) — instead, we scan every file in the
    directory for the signature. The first match wins; we don't try to
    enumerate them.
    """
    needle = signature if signature is not None else expected_failure_signature()
    if not build_failures_dir.is_dir():
        return False
    rx = re.compile(re.escape(needle))
    for entry in build_failures_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rx.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# py-spy / podman attach harness (T2-γ)
# ---------------------------------------------------------------------------


_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")
"""Match ANSI SGR escape sequences in framework log lines.

The Rust tracing layer wraps every field marker in colour codes
(``[3mepoch[0m[2m=[0m1``); our log-line regexes need to be applied
AFTER stripping these so substring patterns like ``epoch=\\d+`` match
the visible text. Mirrors the same constant in
:mod:`compiler_suit_runner.tests.slurm.invariants`.
"""


PROMOTED_RE: re.Pattern[str] = re.compile(
    r"promoted to primary",
)
"""Pattern that signals a secondary won the post-disconnect election.

The framework's actual log line is::

    this secondary has been promoted to primary epoch=1

We match on the substring ``promoted to primary`` (without insisting
on ``epoch=\\d+``) because the epoch / field markers carry ANSI codes
even after :data:`_ANSI_RE` strip on some framework versions and
because the substring is unambiguous (no other log message uses
"promoted to primary" verbatim).
"""


LOCAL_PRIMARY_DISCONNECTED_RE: re.Pattern[str] = re.compile(
    r"local primary disconnected",
)
"""Pattern that signals the local primary driver is gone.

The framework's actual line is::

    local primary disconnected; primary continues independently — this
    node owns the pool, local primary's exit is benign post-promotion
    [...]

We match on the substring; the trailing prose may change between
framework versions.
"""


SECONDARY_FINISHED_RE: re.Pattern[str] = re.compile(
    r"secondary finished successfully",
)
"""Pattern that signals a secondary cleanly exited the dispatch loop.

A clean exit (with or without build failures) emits this line. If we
see it before the hang timer fires, the bug did NOT reproduce on this
run.
"""


@dataclasses.dataclass(frozen=True, slots=True)
class HangCaptureResult:
    """Files saved by :func:`capture_hang_stack`.

    Both fields may be ``None`` if py-spy was unavailable in the
    container image or the worker SSH probe failed; the test surfaces
    the absence as a clear "needs image rebuild" message.
    """

    pyspy_dump: Optional[pathlib.Path]
    ps_dump: Optional[pathlib.Path]
    container_id: Optional[str]
    worker: Optional[str]
    notes: tuple[str, ...]


def _find_secondary_container(
    probe: object, workers: tuple[str, ...],
) -> Optional[tuple[str, str]]:
    """Find the secondary's container ID + the worker hosting it.

    Returns ``(worker, container_id)`` for the first running container
    we can attribute to a secondary (image name carrying
    ``asm-dataset-nix-runner`` or container name including ``secondary``).
    Returns ``None`` if no candidate is found.
    """
    podman_ps = getattr(probe, "podman_ps", None)
    if podman_ps is None:
        return None
    for worker in workers:
        try:
            rows = podman_ps(worker)
        except Exception:  # noqa: BLE001 -- probe failure shouldn't crash the test
            continue
        for row in rows:
            state = (getattr(row, "state", "") or "").lower()
            # Skip exited/created containers - py-spy needs a live PID.
            if "running" not in state and state not in ("up",):
                continue
            image = getattr(row, "image", "") or ""
            name = getattr(row, "name", "") or ""
            if (
                "asm-dataset-nix-runner" in image
                or "secondary" in name
                or "asm-secondary" in name
            ):
                return worker, getattr(row, "id", "") or name
    return None


def _save_text(path: pathlib.Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically.

    The artifacts dir is created if missing. Errors during the rename
    propagate (they should NEVER happen on the local FS — the dir is
    under the worktree, not a network mount).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def capture_hang_stack(
    probe: object,
    *,
    artifacts_dir: pathlib.Path,
    workers: tuple[str, ...],
    timestamp: Optional[str] = None,
    timeout_s: float = 30.0,
) -> HangCaptureResult:
    """Attach py-spy to the hung secondary and save a stack trace.

    Resolution: locate the secondary's running podman container via
    ``ClusterProbe.podman_ps``, then run ``podman exec <cid> py-spy
    dump --pid 1`` (PID 1 inside the container is the secondary's
    Python entry — the framework spawns its child workers as children
    of that PID-1 process). The capture runs only if the container is
    found AND py-spy is on PATH inside the image; otherwise the
    function returns a result with ``pyspy_dump=None`` plus a note
    explaining what was missing, and the caller surfaces the absence as
    a "needs image rebuild" skip.

    ``ps auxf`` is also captured (saved to ``t02_<TS>_ps.txt``) for
    cross-referencing the py-spy stack against the worker process tree.

    Parameters:
    - ``probe``: a :class:`ClusterProbe`-shaped object with a
      ``podman_ps(worker)`` method and a ``worker_ssh(worker, cmd,
      timeout=...)`` method. Loosely typed so the import here doesn't
      drag in cluster_probe at module-collection time.
    - ``artifacts_dir``: where to write the dump files. Created if
      missing. Lives under the test slice's ``artifacts/`` dir by
      convention.
    - ``workers``: ordered list of worker hostnames to search.
    - ``timestamp``: file-name suffix (e.g. ``20260508_123456``);
      defaults to UTC ``time.strftime`` of the current moment.
    - ``timeout_s``: per-SSH-call timeout for podman exec.
    """
    if timestamp is None:
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    notes: list[str] = []

    found = _find_secondary_container(probe, workers)
    if found is None:
        notes.append(
            "no running secondary container found via podman_ps "
            "(check that the run actually started a secondary, then "
            "verify the cluster probe identity-file is correct)"
        )
        return HangCaptureResult(
            pyspy_dump=None,
            ps_dump=None,
            container_id=None,
            worker=None,
            notes=tuple(notes),
        )
    worker, cid = found
    short_cid = cid[:12] if cid else "<empty>"

    pyspy_path = artifacts_dir / f"t02_{timestamp}_pyspy.txt"
    ps_path = artifacts_dir / f"t02_{timestamp}_ps.txt"

    worker_ssh = getattr(probe, "worker_ssh", None)
    if worker_ssh is None:
        notes.append("probe missing worker_ssh; cannot attach")
        return HangCaptureResult(
            pyspy_dump=None,
            ps_dump=None,
            container_id=cid,
            worker=worker,
            notes=tuple(notes),
        )

    # ps auxf is cheap and always available - run it first so even a
    # failed py-spy attach leaves SOMETHING for offline analysis.
    try:
        cp = worker_ssh(
            worker,
            f"podman exec {short_cid} ps auxf",
            timeout=timeout_s,
        )
        ps_text = cp.stdout
        if cp.returncode != 0 and cp.stderr:
            ps_text = (
                f"# rc={cp.returncode}\n# stderr:\n{cp.stderr}\n\n"
                f"# stdout:\n{ps_text}"
            )
        _save_text(ps_path, ps_text)
        ps_saved: Optional[pathlib.Path] = ps_path
    except subprocess.TimeoutExpired as exc:
        notes.append(f"ps auxf timed out: {exc}")
        ps_saved = None
    except OSError as exc:
        notes.append(f"ps auxf SSH error: {exc}")
        ps_saved = None

    # py-spy is the high-value capture. The image MAY not bake it in;
    # the test surfaces that as a clear skip note rather than a hard
    # failure (image rebuild is out of scope here).
    try:
        cp = worker_ssh(
            worker,
            f"podman exec {short_cid} sh -c 'command -v py-spy >/dev/null'",
            timeout=timeout_s,
        )
        py_spy_present = cp.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        py_spy_present = False
        notes.append(f"py-spy presence check failed: {exc}")

    pyspy_saved: Optional[pathlib.Path] = None
    if not py_spy_present:
        notes.append(
            "py-spy not available in container image; rebuild the "
            "secondary image with py-spy on PATH (see plan section "
            '"Reproducer for post-promotion-with-failure hang")'
        )
    else:
        try:
            cp = worker_ssh(
                worker,
                f"podman exec {short_cid} py-spy dump --pid 1",
                timeout=timeout_s,
            )
            stack_text = cp.stdout
            if cp.returncode != 0 and cp.stderr:
                stack_text = (
                    f"# rc={cp.returncode}\n# stderr:\n{cp.stderr}\n\n"
                    f"# stdout:\n{stack_text}"
                )
            _save_text(pyspy_path, stack_text)
            pyspy_saved = pyspy_path
        except subprocess.TimeoutExpired as exc:
            notes.append(f"py-spy dump timed out: {exc}")
        except OSError as exc:
            notes.append(f"py-spy dump SSH error: {exc}")

    return HangCaptureResult(
        pyspy_dump=pyspy_saved,
        ps_dump=ps_saved,
        container_id=cid,
        worker=worker,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Hang-trigger detection
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class HangTriggerState:
    """Result of :func:`detect_hang_trigger` on the live log dir.

    ``promoted`` and ``primary_disconnected`` are independently observed
    so the test failure message can distinguish the two pre-conditions
    of the bug; ``finished`` short-circuits the hang detection (a
    well-behaved post-promotion drain emits both pre-conditions AND a
    ``secondary finished successfully`` line, so we must NOT count that
    as a hang).
    """

    promoted: bool
    primary_disconnected: bool
    finished: bool

    @property
    def hang_pre_conditions_met(self) -> bool:
        """Both bug pre-conditions seen, secondary not finished."""
        return self.promoted and self.primary_disconnected and not self.finished


def detect_hang_trigger(log_dir: pathlib.Path) -> HangTriggerState:
    """Inspect every ``slurm_*.out`` for the hang pre-conditions.

    Reads each ``slurm_*.out`` file under ``log_dir`` (non-recursive)
    and returns the disjunction of the three flags across all files.
    Files that don't yet exist are treated as absent (returning all
    flags False); the caller polls until at least
    ``hang_pre_conditions_met`` is True or a wall-clock deadline passes.
    """
    state = HangTriggerState(False, False, False)
    if not log_dir.is_dir():
        return state
    promoted = False
    disconnected = False
    finished = False
    for path in sorted(log_dir.glob("slurm_*.out")):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # The framework's tracing layer interleaves ANSI SGR codes
        # around every field marker, so substring patterns must run
        # against the ANSI-stripped text or they'll miss matches.
        text = _ANSI_RE.sub("", raw)
        if not promoted and PROMOTED_RE.search(text):
            promoted = True
        if not disconnected and LOCAL_PRIMARY_DISCONNECTED_RE.search(text):
            disconnected = True
        if not finished and SECONDARY_FINISHED_RE.search(text):
            finished = True
    return HangTriggerState(
        promoted=promoted,
        primary_disconnected=disconnected,
        finished=finished,
    )


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


def reset_artifacts_dir(artifacts_dir: pathlib.Path) -> None:
    """Idempotently ensure ``artifacts_dir`` exists and is empty of T2 files.

    Test runs may leave behind ``t02_<TS>_pyspy.txt`` and
    ``t02_<TS>_ps.txt`` files from previous reproducer attempts. The
    test harness runs cache-cold, so we drop matching files at the
    start of each test to keep the artifacts dir focused on the
    current run's diagnostics. Files NOT matching the ``t02_*``
    prefix are left alone (other repros own those filenames).
    """
    artifacts_dir = pathlib.Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for entry in artifacts_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.startswith("t02_"):
            continue
        try:
            entry.unlink()
        except OSError:
            continue


def remove_dir_if_present(path: pathlib.Path) -> None:
    """Delete ``path`` (recursive) if it exists; no-op otherwise.

    Used by the test fixture to wipe the per-test ``--shared-fs`` root
    after the run, so the next test starts from a clean state.
    """
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "BROKEN_BUILD_FAILURE_SIGNATURE",
    "BROKEN_FLAKE_ATTR",
    "BrokenDrvInfo",
    "HangCaptureResult",
    "HangTriggerState",
    "ManifestMutationResult",
    "ManifestWatcherResult",
    "build_failure_log_carries_signature",
    "capture_hang_stack",
    "detect_hang_trigger",
    "expected_failure_signature",
    "make_broken_run_args",
    "make_broken_smoke_invocation",
    "mutate_manifests_in_place",
    "remove_dir_if_present",
    "reset_artifacts_dir",
    "resolve_broken_drv_path",
    "watch_and_mutate_manifests",
]
