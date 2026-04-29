"""Phase-1b merge worker.

Reads all phase-1a shard outputs from the shared FS, aggregates input-drv
frequencies, classifies them into (toolchains, common_deps, incidental),
and writes a single ``partition.json`` plus a sidecar ``skip_list.json``.

The phase-1a barrier flag is written by the runner's bookkeeping thread,
*not* by this worker. This worker assumes phase-1a is already complete:
its only synchronization is the implicit barrier item that the
dynamic_runner scheduler will have completed before this item runs.

Stdlib only; no threading.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import time
from collections.abc import Callable, Iterable
from typing import Final

from compiler_suit_runner.partition import (
    Partition,
    ShardOutput,
    VariantSpec,
    aggregate_input_drv_frequencies,
    build_partition,
    classify_input_drvs,
    read_shard_outputs,
    write_partition_json,
)


PHASE_1B_ITEM_CLASS: Final[str] = "phase1b_merge"
SKIP_LIST_VERSION: Final[int] = 1


# ---------------------------------------------------------------------------
# Result + env dataclasses


@dataclasses.dataclass
class MergeWorkerResult:
    """Outcome of a single merge_worker dispatch."""

    partition_path: pathlib.Path
    skip_list_path: pathlib.Path
    shard_count: int
    variant_count: int
    toolchain_count: int
    common_dep_count: int
    incidental_count: int
    duration_seconds: float
    error: str | None = None


@dataclasses.dataclass
class MergeWorkerEnv:
    """Inputs to the merge worker.

    ``raw_partition_dir`` is the input directory containing per-shard
    JSON files written by phase-1a workers. ``partition_dir`` is the
    output directory for ``partition.json`` and ``skip_list.json``.
    """

    raw_partition_dir: pathlib.Path
    partition_dir: pathlib.Path
    input_hash: str
    variants: tuple[VariantSpec, ...]
    toolchain_drvs: frozenset[str]
    common_threshold: int = 10
    clock: Callable[[], float] | None = None


# ---------------------------------------------------------------------------
# Atomic JSON write (skip_list.json is written here; partition.json uses
# write_partition_json from the partition module which already writes
# atomically).


def _atomic_write_json(target: pathlib.Path, payload: object) -> pathlib.Path:
    """Write ``payload`` as JSON to ``target`` atomically.

    Sibling ``.tmp`` file + fsync + ``os.replace``; mirrors the helper in
    ``partition._atomic_write_json`` so the merge worker's sidecar files
    survive an interrupted run intact.
    """
    target = pathlib.Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    data = json.dumps(payload, sort_keys=True, indent=2)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------------------
# Manifest parsing


def parse_merge_manifest(manifest_json_path: pathlib.Path) -> dict:
    """Read the phase-1b manifest JSON and validate its ``item_class``.

    Returns the parsed payload dict on success. Raises:

    * :class:`FileNotFoundError` if the manifest file does not exist.
    * :class:`ValueError` if the JSON is not an object, or if
      ``item_class`` is missing or does not equal ``'phase1b_merge'``.

    The payload's other fields are not validated here — the merge
    worker's behavior is fully driven by :class:`MergeWorkerEnv`, not the
    manifest. This keeps the manifest schema free to evolve.
    """
    manifest_json_path = pathlib.Path(manifest_json_path)
    with open(manifest_json_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    if not isinstance(payload, dict):
        raise ValueError(
            f"{manifest_json_path}: manifest top-level JSON must be an object"
        )

    item_class = payload.get("item_class")
    if item_class != PHASE_1B_ITEM_CLASS:
        raise ValueError(
            f"{manifest_json_path}: expected item_class"
            f" {PHASE_1B_ITEM_CLASS!r}, got {item_class!r}"
        )
    return payload


# ---------------------------------------------------------------------------
# Skip list


def collect_skip_list(
    variants: Iterable[VariantSpec],
    *,
    classification_incidentals: frozenset[str],
) -> list[str]:
    """Return labels of variants the runner should skip building.

    V1 always returns ``[]``: every variant in the input superset is
    built. The argument list is intentional — future versions will
    filter variants whose entire build closure is incidental (already
    covered by phase-3's per-node dependency discovery), but the v1
    contract is "build everything in the input list". Stubbing the
    function here gives the runner a stable call site that won't change
    when filtering is added.
    """
    # Touch the parameters so static analysers see them as used; this
    # also documents the future shape for readers.
    _ = list(variants)
    _ = classification_incidentals
    return []


# ---------------------------------------------------------------------------
# Worker entry point


def merge_worker(
    manifest_json_path: pathlib.Path, env: MergeWorkerEnv
) -> MergeWorkerResult:
    """Phase-1b merge worker dispatch surface.

    Steps:

    1. ``parse_merge_manifest`` — assert ``item_class``.
    2. ``read_shard_outputs`` — load all phase-1a shard JSONs.
    3. ``aggregate_input_drv_frequencies`` — count drv references.
    4. ``classify_input_drvs`` — bucket into (toolchains, common,
       incidental).
    5. ``build_partition`` — assemble the v1 :class:`Partition`.
    6. ``write_partition_json`` — atomically write ``partition.json``.
    7. ``collect_skip_list`` + atomic write of ``skip_list.json``
       containing ``{"version": 1, "entries": [...]}``.
    8. Return :class:`MergeWorkerResult` with timing + counts.

    On any exception, the result is constructed with ``error=str(exc)``
    and the exception is re-raised so the runner's failure-tracking
    sees the failure. Partial output files may exist on disk; the
    runner is expected to retry by re-dispatching the merge item.

    The barrier flag (``phase1b_done``) is NOT written here — the
    runner's bookkeeping thread writes it after this worker returns
    successfully.
    """
    clock: Callable[[], float] = env.clock if env.clock is not None else time.monotonic
    start = clock()

    partition_path = pathlib.Path(env.partition_dir) / "partition.json"
    skip_list_path = pathlib.Path(env.partition_dir) / "skip_list.json"

    try:
        parse_merge_manifest(manifest_json_path)

        shard_outputs: list[ShardOutput] = read_shard_outputs(
            env.raw_partition_dir
        )
        frequencies = aggregate_input_drv_frequencies(shard_outputs)
        toolchains, common_deps, incidental = classify_input_drvs(
            frequencies,
            set(env.toolchain_drvs),
            common_threshold=env.common_threshold,
        )

        partition: Partition = build_partition(
            input_hash=env.input_hash,
            variants=env.variants,
            toolchains=toolchains,
            common_deps=common_deps,
        )
        written_partition = write_partition_json(partition_path, partition)

        skip_list = collect_skip_list(
            env.variants,
            classification_incidentals=frozenset(incidental),
        )
        written_skip = _atomic_write_json(
            skip_list_path,
            {"version": SKIP_LIST_VERSION, "entries": list(skip_list)},
        )

        duration = clock() - start
        return MergeWorkerResult(
            partition_path=written_partition,
            skip_list_path=written_skip,
            shard_count=len(shard_outputs),
            variant_count=len(partition.variants),
            toolchain_count=len(partition.toolchains),
            common_dep_count=len(partition.common_deps),
            incidental_count=len(incidental),
            duration_seconds=duration,
            error=None,
        )
    except BaseException as exc:
        duration = clock() - start
        # Build a best-effort result so a caller catching the
        # re-raised exception still has timing information for its
        # bookkeeping. We deliberately do not swallow the exception.
        result = MergeWorkerResult(
            partition_path=partition_path,
            skip_list_path=skip_list_path,
            shard_count=0,
            variant_count=0,
            toolchain_count=0,
            common_dep_count=0,
            incidental_count=0,
            duration_seconds=duration,
            error=str(exc),
        )
        # Attach the result to the exception for callers that want it
        # without restructuring their try/except. Use a private
        # attribute name that won't collide with builtin Exception
        # fields.
        try:
            exc.merge_worker_result = result  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            # Some exception types (e.g. C-extension ones) refuse new
            # attributes; ignore and re-raise unchanged.
            pass
        raise
