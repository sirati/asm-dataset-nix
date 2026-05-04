"""Phase-1a partition worker.

Reads a manifest header (the JSON contents written by ``manifest_gen``
for a ``phase1a_partition`` item), runs ``nix derivation show
--recursive`` for each variant's drv path, extracts the transitive
``inputDrvs`` set per variant, and writes the result via
:func:`compiler_suit_runner.partition.write_shard_output`.

This is read-only nix-eval work — no peer cache, no build. The phase
exists so the merge worker (phase 1b) can score per-drv frequencies and
classify drvs as toolchain / common / incidental.

The module is intentionally narrow: subprocess invocation and clock
access go through callables on :class:`WorkerEnv` so unit tests inject
deterministic fakes.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import subprocess
import time
from collections.abc import Callable
from typing import Any

from compiler_suit_runner.partition import (
    ShardOutput,
    VariantSpec,
    write_shard_output,
)


PHASE_1A_ITEM_CLASS = "phase1a_partition"


# ---------------------------------------------------------------------------
# Worker environment


@dataclasses.dataclass
class WorkerEnv:
    """Environment passed to the worker by the harness.

    ``run_subprocess`` defaults to :func:`_default_run_subprocess`;
    ``clock`` defaults to :func:`time.monotonic`. Tests inject fakes for
    both.
    """

    raw_partition_dir: pathlib.Path
    flake_ref: str
    run_subprocess: Callable[[list[str]], tuple[bytes, bytes, int]] | None = None
    clock: Callable[[], float] | None = None


def _default_run_subprocess(
    cmd: list[str],
) -> tuple[bytes, bytes, int]:
    """Run ``cmd`` (a list, no shell) and return ``(stdout, stderr, rc)``.

    The worker NEVER spawns shells; ``cmd`` is always a list of literal
    arguments. Output is captured so callers can surface it in error
    messages without leaking it into the worker's stdout.
    """
    completed = subprocess.run(cmd, capture_output=True)
    return completed.stdout, completed.stderr, completed.returncode


# ---------------------------------------------------------------------------
# Result type


@dataclasses.dataclass
class PartitionWorkerResult:
    """Outcome of one phase-1a partition shard.

    ``error`` is non-None on any failure (manifest parse error, nix
    subprocess failure, JSON decode error). When set, ``output_path``
    is :data:`None` because :func:`partition_worker` does not write a
    partial shard — see the docstring of :func:`partition_worker` for
    the choice and rationale.
    """

    shard_name: str
    variant_count: int
    output_path: pathlib.Path | None
    duration_seconds: float
    nix_calls: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Manifest parsing


def parse_manifest_payload(
    manifest_json_path: pathlib.Path,
) -> tuple[str, str, list[VariantSpec]]:
    """Read ``manifest_json_path`` and unpack a phase-1a manifest.

    Returns ``(pkg, arch, variants)``. Raises :class:`ValueError` on
    schema mismatch (missing keys, wrong ``item_class``, malformed
    variant entries).
    """
    manifest_json_path = pathlib.Path(manifest_json_path)
    with open(manifest_json_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    if not isinstance(payload, dict):
        raise ValueError(
            f"{manifest_json_path}: top-level JSON must be an object"
        )

    item_class = payload.get("item_class")
    if item_class != PHASE_1A_ITEM_CLASS:
        raise ValueError(
            f"{manifest_json_path}: expected item_class "
            f"{PHASE_1A_ITEM_CLASS!r}, got {item_class!r}"
        )

    pkg = payload.get("pkg")
    arch = payload.get("arch")
    if not isinstance(pkg, str) or not pkg:
        raise ValueError(
            f"{manifest_json_path}: missing/invalid 'pkg' (got {pkg!r})"
        )
    if not isinstance(arch, str) or not arch:
        raise ValueError(
            f"{manifest_json_path}: missing/invalid 'arch' (got {arch!r})"
        )

    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list):
        raise ValueError(
            f"{manifest_json_path}: 'variants' must be a list"
        )

    required_fields = ("label", "drv", "tarball_name", "compiler_id", "tier")
    variants: list[VariantSpec] = []
    for index, raw in enumerate(raw_variants):
        if not isinstance(raw, dict):
            raise ValueError(
                f"{manifest_json_path}: variants[{index}] is not an object"
            )
        for field in required_fields:
            if field not in raw:
                raise ValueError(
                    f"{manifest_json_path}: variants[{index}]"
                    f" missing field {field!r}"
                )
        for field in ("label", "drv", "tarball_name", "compiler_id"):
            if not isinstance(raw[field], str):
                raise ValueError(
                    f"{manifest_json_path}: variants[{index}].{field}"
                    " must be a string"
                )
        if not isinstance(raw["tier"], int):
            raise ValueError(
                f"{manifest_json_path}: variants[{index}].tier must be int"
            )
        variant: VariantSpec = {
            "label": raw["label"],
            "drv": raw["drv"],
            "tarball_name": raw["tarball_name"],
            "compiler_id": raw["compiler_id"],
            "tier": raw["tier"],
            "pkg": pkg,
            "arch": arch,
        }
        variants.append(variant)

    return pkg, arch, variants


# ---------------------------------------------------------------------------
# Nix derivation graph


_NIX_BASE_CMD: tuple[str, ...] = (
    "nix",
    "--extra-experimental-features",
    "nix-command flakes",
    "derivation",
    "show",
    "--recursive",
)


def show_drv_recursive(drv: str, env: WorkerEnv) -> dict[str, Any]:
    """Run ``nix derivation show --recursive <drv>`` and return parsed JSON.

    The flake-related experimental features are enabled because the drv
    paths we receive may originate from a flake-only repo configuration;
    on a host where they are already enabled in nix.conf this is a
    harmless no-op.

    Raises :class:`RuntimeError` (with stderr) on any non-zero exit or
    JSON decode failure.
    """
    runner = env.run_subprocess or _default_run_subprocess
    cmd = [*_NIX_BASE_CMD, drv]
    stdout, stderr, returncode = runner(cmd)
    if returncode != 0:
        raise RuntimeError(
            f"nix derivation show --recursive {drv!r} exited {returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )
    try:
        parsed = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"nix derivation show --recursive {drv!r} produced invalid JSON:"
            f" {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"nix derivation show --recursive {drv!r} did not return a"
            " JSON object"
        )
    return parsed


def _input_drv_keys(input_drvs_field: Any) -> list[str]:
    """Return the drv paths from an ``inputDrvs`` field.

    Robust to both schema variants nix has emitted:

    * Older / array form: ``{"inputDrvs": {"<drv>": ["out"]}}``
    * Newer / nested form:
      ``{"inputDrvs": {"<drv>": {"dynamicOutputs": {}, "outputs": [...]}}}``

    Either way the keys are the drv paths we need. Non-dict values are
    ignored defensively.
    """
    if not isinstance(input_drvs_field, dict):
        return []
    return [key for key in input_drvs_field.keys() if isinstance(key, str)]


def extract_input_drvs(
    show_output: dict[str, Any], root_drv: str
) -> list[str]:
    """Walk ``inputDrvs`` from ``root_drv`` and return all transitive deps.

    The traversal is iterative (uses a stack) and tracks visited nodes
    in a set, so cycles in the graph cannot loop forever. Robust to
    either ``inputDrvs`` schema variant (see :func:`_input_drv_keys`).

    The returned list is sorted, deduplicated, and **excludes**
    ``root_drv`` itself.
    """
    visited: set[str] = set()
    collected: set[str] = set()
    stack: list[str] = [root_drv]

    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)

        node = show_output.get(current)
        if not isinstance(node, dict):
            # Leaf reference (the recursive show output normally
            # includes every transitive drv, but we tolerate a missing
            # node so the worker doesn't crash on truncated input).
            continue
        children = _input_drv_keys(node.get("inputDrvs"))
        for child in children:
            if child == root_drv:
                # Defensive: a self-edge is uncommon but should not be
                # treated as a transitive dep.
                continue
            collected.add(child)
            if child not in visited:
                stack.append(child)

    return sorted(collected)


# ---------------------------------------------------------------------------
# Dispatch entry point


def partition_worker(
    manifest_json_path: pathlib.Path,
    env: WorkerEnv,
) -> PartitionWorkerResult:
    """Phase-1a dispatch entry point.

    Steps:

    1. :func:`parse_manifest_payload` -> ``(pkg, arch, variants)``.
    2. For each variant, run :func:`show_drv_recursive` on its drv path.
       Each call returns the full transitive sub-graph; we merge those
       graphs into one in-memory cache so subsequent
       :func:`extract_input_drvs` calls have a single map to consult.
    3. Build a :class:`compiler_suit_runner.partition.ShardOutput` and
       write it via
       :func:`compiler_suit_runner.partition.write_shard_output`.
    4. Return :class:`PartitionWorkerResult`.

    Failure handling: if any variant's nix call fails, the worker
    aborts the shard, records the error in the result, and **does not
    write a partial shard JSON**. Callers (the merge worker and the
    primary's bookkeeping thread) treat a missing shard file as
    "shard failed" and route around it. This avoids the worse failure
    mode of merging an incomplete shard as if it were complete.

    The function never raises — fatal errors are returned in
    ``result.error``.
    """
    clock = env.clock or time.monotonic
    started = clock()
    nix_calls = 0
    shard_name = pathlib.Path(manifest_json_path).stem

    try:
        pkg, arch, variants = parse_manifest_payload(manifest_json_path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return PartitionWorkerResult(
            shard_name=shard_name,
            variant_count=0,
            output_path=None,
            duration_seconds=clock() - started,
            nix_calls=nix_calls,
            error=f"parse_manifest_payload: {exc}",
        )

    shard_name = f"{pkg}__{arch}"

    # Cumulative graph cache. Each variant's --recursive call returns
    # the variant's full sub-graph; once a drv path is in the cache we
    # don't need to fetch it again. In practice variants in the same
    # shard share enormous swathes of their inputs (same toolchain,
    # same libc) so the merged graph saves little memory but costs
    # nothing to maintain.
    graph_cache: dict[str, Any] = {}

    variant_to_inputs: dict[str, list[str]] = {}
    for variant in variants:
        drv = variant["drv"]
        if drv not in graph_cache:
            try:
                sub_graph = show_drv_recursive(drv, env)
            except (RuntimeError, OSError) as exc:
                return PartitionWorkerResult(
                    shard_name=shard_name,
                    variant_count=len(variants),
                    output_path=None,
                    duration_seconds=clock() - started,
                    nix_calls=nix_calls,
                    error=f"show_drv_recursive({drv!r}): {exc}",
                )
            nix_calls += 1
            # Merge sub-graph into the cache. Later variants may add
            # nodes that earlier sub-graphs lacked; identical nodes are
            # idempotent (nix derivation show is content-addressed).
            graph_cache.update(sub_graph)

        try:
            inputs = extract_input_drvs(graph_cache, drv)
        except Exception as exc:  # noqa: BLE001 — defensive
            return PartitionWorkerResult(
                shard_name=shard_name,
                variant_count=len(variants),
                output_path=None,
                duration_seconds=clock() - started,
                nix_calls=nix_calls,
                error=f"extract_input_drvs({drv!r}): {exc}",
            )
        variant_to_inputs[variant["label"]] = inputs

    output = ShardOutput(
        shard_name=shard_name,
        variant_to_input_drvs=variant_to_inputs,
    )
    try:
        env.raw_partition_dir.mkdir(parents=True, exist_ok=True)
        output_path = write_shard_output(env.raw_partition_dir, output)
    except OSError as exc:
        return PartitionWorkerResult(
            shard_name=shard_name,
            variant_count=len(variants),
            output_path=None,
            duration_seconds=clock() - started,
            nix_calls=nix_calls,
            error=f"write_shard_output: {exc}",
        )

    return PartitionWorkerResult(
        shard_name=shard_name,
        variant_count=len(variants),
        output_path=output_path,
        duration_seconds=clock() - started,
        nix_calls=nix_calls,
        error=None,
    )


# ---------------------------------------------------------------------------
# Subprocess entry point
#
# The dynamic_runner framework spawns this module as
# ``python -m compiler_suit_runner.workers.partition_worker`` with its
# standard worker-protocol argv (``--dynamic_queue`` / ``--socket-path``,
# plus shared-fs / output paths). The framework's per-task IPC is owned
# by ``dynamic_runner.comm``; in this best-effort shim the per-manifest
# loop reads one manifest path per line from stdin so the entry point
# can be exercised both by the framework's worker protocol (driven via
# its dispatcher's stdin pipe) and by unit tests.
#
# TODO(phase 8 follow-up): wire this up to ``dynamic_runner.comm``
# (ReadyResponse / ProcessTask / DoneResponse) once the comm shape for
# TaskInfo dispatch is stabilised. For now the manifest-per-line stdin
# protocol is enough to satisfy the per-worker entry-point contract.


def _build_env_from_args(args) -> "WorkerEnv":
    """Construct a :class:`WorkerEnv` from parsed argv."""
    return WorkerEnv(
        raw_partition_dir=pathlib.Path(args.raw_partition_dir),
        flake_ref=args.flake_ref,
    )


def main() -> int:
    """Subprocess entry point for the phase-1a partition worker.

    Drives the framework's worker protocol via the comm fd
    (``--dynamic_queue`` / ``--socket-path``). See
    :mod:`compiler_suit_runner.workers._runner_protocol` for the
    line-based protocol details.
    """
    import argparse
    import logging

    from ._runner_protocol import (
        DispatchResult,
        connect_comm,
        run_protocol_loop,
    )

    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner.workers.partition_worker",
    )
    parser.add_argument("--dynamic_queue", type=int, default=None)
    parser.add_argument("--socket-path", type=str, default=None)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument(
        "--raw-partition-dir",
        type=str,
        required=True,
        help="Shared-FS directory for per-shard partition outputs.",
    )
    parser.add_argument(
        "--flake-ref",
        type=str,
        required=True,
        help="Flake reference passed through to ``nix derivation show``.",
    )
    parser.add_argument("--skip-existing", action="store_true")
    args, _ = parser.parse_known_args()

    log = logging.getLogger("compiler_suit_runner.workers.partition_worker")
    env = _build_env_from_args(args)

    sock = connect_comm(
        dynamic_queue=args.dynamic_queue,
        socket_path=args.socket_path,
        log=log,
    )
    if sock is None:
        log.warning("no comm channel supplied; worker exiting (test mode)")
        return 0

    def dispatch(manifest_path: pathlib.Path) -> DispatchResult:
        result = partition_worker(manifest_path, env)
        if result.error is None:
            return DispatchResult.ok()
        return DispatchResult.error(result.error)

    return run_protocol_loop(
        sock=sock,
        source=args.source,
        dispatch=dispatch,
        log=log,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main())
