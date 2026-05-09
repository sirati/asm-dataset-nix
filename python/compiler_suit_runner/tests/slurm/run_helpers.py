"""Subprocess wrapper around ``python -m compiler_suit_runner submit``.

The wrapper is the single chokepoint by which the slurm test slice
invokes the CLI. It captures the dynamic_runner-assigned ``run_id``
from stderr (``Run ID: run_<TS>``), resolves the resulting log
directory under the locally bind-mounted slurm-test-env, and returns
both alongside the raw process output.

The wrapper deliberately does **not** start the framework in-process:
the slurm dispatch path forks a long-lived primary coordinator that
needs its own argv and lifecycle, and reusing that out-of-band would
couple the test fixtures to framework internals that change between
versions. Spawning ``python -m compiler_suit_runner submit`` keeps the
test slice on the same surface a human operator drives by hand
(see ``slurm.md``).

The CLI itself is invoked on the **host**: the dispatching process
runs locally, and only the per-secondary containers run inside the
slurm-test-env podman cluster. This matches ``cmd_submit``'s actual
behaviour — ``--multi-computer slurm`` still drives ``dynamic_runner``
from the local interpreter, the gateway is reached over SSH, and the
log mount under ``/home/sirati/.local/state/slurm-test-env/...`` is
the host's view of the gateway's ``~/slurm/log/`` directory.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Iterable, Literal, Optional


# Host-side view of the slurm-test-env's gateway log directory.
# /home is bind-mounted at ``<state>/ds-test/home/sirati/`` so the
# gateway-relative path ``~/slurm/log/`` lands here.
SLURM_TEST_ENV_LOG_ROOT = pathlib.Path(
    "/home/sirati/.local/state/slurm-test-env/ds-test/home/sirati/slurm/log"
)

# Gateway-side path the CLI's ``--slurm-root-folder`` is set to. The
# log subtree under ``<slurm-root>/log/<run_id>/`` is what the test
# harness reads via :data:`SLURM_TEST_ENV_LOG_ROOT`.
SLURM_TEST_ENV_GATEWAY_ROOT = pathlib.Path("/home/sirati/slurm")

# Default gateway URL for the local slurm-test-env. The host string
# is the alias workers DNS-resolve through the podman bridge (see
# slurm-test-env owner peer note 26-05-09 02:56). The dispatcher's
# SSH client redirects ``slurm-gateway`` -> ``localhost:2244`` via
# the per-cluster ssh_config the conftest fixture writes; using
# ``localhost`` directly here would propagate into worker wrappers
# as ``--secondary tcp://localhost:port``, which workers would dial
# in their own netns and never reach the dispatcher.
SLURM_TEST_ENV_GATEWAY_URL = "ssh://sirati@slurm-gateway"

# Per-cluster ssh_config path written by the ``ssh_master`` fixture.
# Both the master pre-spawn and the framework's ``--ssh-config``
# read from the same file, keeping SSH directives in one place.
SLURM_TEST_ENV_SSH_CONFIG = pathlib.Path("/tmp/asm-dr-cluster.cfg")

# Per-process Unix-socket path for the pre-spawned SSH master.
# MUST stay below 108 bytes (``sockaddr_un.sun_path`` kernel limit
# per migration doc Caveat); ``/tmp/asm-dr-master.sock`` is 26.
SLURM_TEST_ENV_SSH_CONTROL_PATH = pathlib.Path("/tmp/asm-dr-master.sock")

# Local incremental-cache root the wrapper wipes on demand. Mirrors
# ``compiler_suit_runner.incremental_cache.DEFAULT_CACHE_ROOT`` but
# duplicated as a literal so this module stays import-light (no
# dynamic_runner / preflight pull-in for tests that only mock
# subprocess).
DEFAULT_CACHE_ROOT = pathlib.Path.home() / ".cache" / "compiler_suit_runner"

# dynamic_runner stamps the run_id at INFO level via the standard
# ``logging`` module. The exact prefix is defined in
# ``dynamic_runner.packaging.pipeline._make_run_id`` /
# ``run_slurm_pipeline``: ``log.info(f"Run ID: {run_id}")``. That line
# is sent to stderr because :func:`compiler_suit_runner.cli._setup_logging`
# attaches a stream handler on ``sys.stderr``. Capture both streams to
# stay robust across interpreter reconfigurations (e.g. a future
# ``--debug``-only field on stdout).
_RUN_ID_PATTERN = re.compile(r"Run ID:\s*(run_[0-9]{8}_[0-9]{6})")


Workload = Literal["tiny", "medium", "large"]


@dataclasses.dataclass(frozen=True, slots=True)
class RunInvocation:
    """Inputs for a single ``compiler_suit_runner submit`` invocation.

    The set of fields mirrors the CLI flags exercised by the slurm
    test matrix (T1..T10 in the plan). Optional fields default to
    ``None``; the wrapper only emits the corresponding flag when set,
    so the framework's own defaults take effect for the rest.
    """

    shared_fs: pathlib.Path
    packages: tuple[str, ...] = ()
    archs: tuple[str, ...] = ()
    multi_computer: Literal["single-process", "slurm"] = "slurm"
    packaging: Literal["podman", "none"] = "podman"
    jobs: Optional[int] = None
    gateway: Optional[str] = SLURM_TEST_ENV_GATEWAY_URL
    slurm_root_folder: Optional[pathlib.Path] = SLURM_TEST_ENV_GATEWAY_ROOT
    slurm_partition: Optional[str] = None
    slurm_time_limit: Optional[str] = None
    slurm_cpus_per_task: Optional[int] = None
    ssh_identity_file: Optional[pathlib.Path] = None
    ssh_config: Optional[pathlib.Path] = None
    variant_sample: Optional[int] = None
    variant_seed: Optional[str] = None
    max_variants: Optional[int] = None
    flake: str = "."
    sys_name: str = "x86_64-linux"
    no_cache: bool = False
    extra_args: tuple[str, ...] = ()
    workdir: Optional[pathlib.Path] = None

    def to_argv(self, *, python: str = sys.executable) -> list[str]:
        """Render the invocation as a concrete argv list.

        The first element is the python interpreter; the second is
        ``-m compiler_suit_runner`` so we go through the package's
        ``__main__`` (matches what ``slurm.md`` documents for hand
        invocation).
        """
        argv: list[str] = [python, "-m", "compiler_suit_runner", "submit"]
        argv += ["--flake", self.flake, "--sys", self.sys_name]
        argv += ["--shared-fs", str(self.shared_fs)]
        argv += ["--multi-computer", self.multi_computer]
        argv += ["--packaging", self.packaging]
        if self.packages:
            argv += ["--packages", *self.packages]
        if self.archs:
            argv += ["--archs", *self.archs]
        if self.jobs is not None:
            argv += ["--jobs", str(self.jobs)]
        if self.gateway is not None:
            argv += ["--gateway", self.gateway]
        if self.slurm_root_folder is not None:
            argv += ["--slurm-root-folder", str(self.slurm_root_folder)]
        if self.slurm_partition is not None:
            argv += ["--slurm-partition", self.slurm_partition]
        if self.slurm_time_limit is not None:
            argv += ["--slurm-time-limit", self.slurm_time_limit]
        if self.slurm_cpus_per_task is not None:
            argv += ["--slurm-cpus-per-task", str(self.slurm_cpus_per_task)]
        if self.ssh_identity_file is not None:
            argv += ["--ssh-identity-file", str(self.ssh_identity_file)]
        if self.ssh_config is not None:
            argv += ["--ssh-config", str(self.ssh_config)]
        if self.variant_sample is not None:
            argv += ["--variant-sample", str(self.variant_sample)]
        if self.variant_seed is not None:
            argv += ["--variant-seed", self.variant_seed]
        if self.max_variants is not None:
            argv += ["--max-variants", str(self.max_variants)]
        if self.no_cache:
            argv += ["--no-cache"]
        argv += list(self.extra_args)
        return argv


@dataclasses.dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of a single ``compiler_suit_runner submit`` invocation."""

    run_id: Optional[str]
    log_dir: Optional[pathlib.Path]
    exit_code: int
    stdout: str
    stderr: str
    wall_time_s: float
    argv: tuple[str, ...]


def parse_run_id(stream_text: str) -> Optional[str]:
    """Extract the dynamic_runner ``run_<TS>`` token from log output.

    Returns the **last** match in ``stream_text`` so a re-run that
    re-emits the line (e.g. retry-on-bind-error in the framework)
    yields the most recent id. ``None`` if no match.
    """
    matches = _RUN_ID_PATTERN.findall(stream_text)
    return matches[-1] if matches else None


def resolve_log_dir(
    run_id: str,
    *,
    log_root: pathlib.Path = SLURM_TEST_ENV_LOG_ROOT,
) -> pathlib.Path:
    """Return ``<log_root>/<run_id>`` regardless of whether it exists.

    Existence is intentionally not asserted: the caller may need the
    path before the gateway has flushed its first slurm log file (the
    primary coordinator creates the directory, but file fsync ordering
    on the bind-mount is async). Tests that need to wait on logs use
    :func:`wait_for_log_dir`.
    """
    return log_root / run_id


def wait_for_log_dir(
    run_id: str,
    *,
    log_root: pathlib.Path = SLURM_TEST_ENV_LOG_ROOT,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.5,
) -> Optional[pathlib.Path]:
    """Block until ``<log_root>/<run_id>/`` exists, or timeout.

    Used by tests that need to read the gateway's slurm logs as soon
    as the framework has placed its first artifact. Returns the
    directory on success; ``None`` on timeout.
    """
    target = resolve_log_dir(run_id, log_root=log_root)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if target.is_dir():
            return target
        time.sleep(poll_interval_s)
    return target if target.is_dir() else None


def run_compiler_suit(
    args: RunInvocation,
    *,
    timeout_s: float = 1800.0,
    env: Optional[dict[str, str]] = None,
) -> RunResult:
    """Spawn ``python -m compiler_suit_runner submit`` and capture output.

    The subprocess is run with stdout/stderr captured (text mode,
    UTF-8). On timeout the process is terminated and the partial
    output is preserved in the returned :class:`RunResult` with
    ``exit_code=-1``.

    The caller's ``env`` is merged on top of ``os.environ``; pass an
    explicit dict to override (e.g.) ``PATH`` or ``PYTHONPATH``.
    """
    argv = args.to_argv()
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    cwd = str(args.workdir) if args.workdir is not None else None
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=merged_env,
            cwd=cwd,
        )
        exit_code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(
            exc.stdout, (bytes, bytearray)
        ) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(
            exc.stderr, (bytes, bytearray)
        ) else (exc.stderr or "")

    wall = time.monotonic() - started
    # The framework log line is on stderr (cli._setup_logging attaches
    # to sys.stderr); fall back to stdout for forward-compat with any
    # future change.
    run_id = parse_run_id(stderr) or parse_run_id(stdout)
    log_dir = resolve_log_dir(run_id) if run_id else None

    return RunResult(
        run_id=run_id,
        log_dir=log_dir,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        wall_time_s=wall,
        argv=tuple(argv),
    )


def default_invocation_for_smoke(
    jobs: int,
    workload: Workload,
    *,
    shared_fs: Optional[pathlib.Path] = None,
) -> RunInvocation:
    """Build a small :class:`RunInvocation` for the test matrix.

    Workload sizing maps to ``--variant-sample`` / ``--max-variants``
    against the existing ``hello`` package, mirroring the dispatch
    documented in ``slurm.md``:

    * ``"tiny"`` — single variant (T1, T2, T4, T6, T10).
    * ``"medium"`` — ~10 variants (T3, T5, T8).
    * ``"large"`` — ~50 variants (T9 load smoke).

    The exact knob values are tuned so a cache-cold run finishes
    within the per-test 600s timeout floor used by the invariant
    harness; tweak via ``dataclasses.replace`` if a particular row
    needs more headroom.
    """
    if workload == "tiny":
        sample, max_v = 1, 1
    elif workload == "medium":
        sample, max_v = 2, 10
    elif workload == "large":
        sample, max_v = 4, 50
    else:  # pragma: no cover — exhaustively typed via Literal
        raise ValueError(f"unknown workload: {workload!r}")

    return RunInvocation(
        shared_fs=shared_fs if shared_fs is not None else _default_shared_fs(),
        packages=("hello",),
        multi_computer="slurm",
        packaging="podman",
        jobs=jobs,
        gateway=SLURM_TEST_ENV_GATEWAY_URL,
        slurm_root_folder=SLURM_TEST_ENV_GATEWAY_ROOT,
        slurm_partition="debug",
        slurm_time_limit="0:30:00",
        ssh_config=SLURM_TEST_ENV_SSH_CONFIG,
        variant_sample=sample,
        max_variants=max_v,
        # Tests want repeatability; a fixed seed makes the sampled
        # variant set identical run-to-run, so failures reproduce.
        variant_seed="slurm-test",
    )


def _default_shared_fs() -> pathlib.Path:
    """Per-test shared-fs root under /tmp.

    A timestamped subdir keeps parallel test invocations from racing
    on the same manifests/partition tree.
    """
    return pathlib.Path("/tmp") / f"asm-suit-shared-{int(time.time() * 1000)}"


def clear_incremental_cache(
    cache_root: pathlib.Path = DEFAULT_CACHE_ROOT,
) -> int:
    """Delete every entry under the local incremental cache.

    The slurm test slice runs cache-cold so each test exercises the
    full pre-flight + dispatch path (the plan's "What this plan does
    NOT do" section: the cache is invalidated between tests, never
    relied on).

    Returns the number of top-level entries removed (sub-dirs of
    ``cache_root``); ``0`` if the cache was already absent.
    """
    if not cache_root.exists():
        return 0
    removed = 0
    for child in cache_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                continue
        removed += 1
    return removed


def iter_log_files(
    log_dir: pathlib.Path, patterns: Iterable[str] = ("slurm_*.out", "slurm_*.err"),
) -> list[pathlib.Path]:
    """Convenience: collect log files matching any of ``patterns``.

    Used by the invariant harness (A1) and by tests that want to
    snapshot the per-run stdout/stderr after a call. Sorted for
    deterministic ordering.
    """
    out: list[pathlib.Path] = []
    if not log_dir.is_dir():
        return out
    for pat in patterns:
        out.extend(sorted(log_dir.glob(pat)))
    return out


__all__ = [
    "DEFAULT_CACHE_ROOT",
    "RunInvocation",
    "RunResult",
    "SLURM_TEST_ENV_GATEWAY_ROOT",
    "SLURM_TEST_ENV_GATEWAY_URL",
    "SLURM_TEST_ENV_LOG_ROOT",
    "SLURM_TEST_ENV_SSH_CONFIG",
    "SLURM_TEST_ENV_SSH_CONTROL_PATH",
    "Workload",
    "clear_incremental_cache",
    "default_invocation_for_smoke",
    "iter_log_files",
    "parse_run_id",
    "resolve_log_dir",
    "run_compiler_suit",
    "wait_for_log_dir",
]
