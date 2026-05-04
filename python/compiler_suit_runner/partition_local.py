"""Local replacement for the dropped phase-1a/1b partition workers.

The original design did the partition (per-variant ``inputDrvs`` walk)
and the merge (refcount + classify) on SLURM secondaries via
:mod:`compiler_suit_runner.workers.partition_worker` and
:mod:`compiler_suit_runner.workers.merge_worker`. That layout was
wrong: the secondaries don't have the build host's ``/nix/store`` and
can't ``nix derivation show`` the variant drvs without a fresh
re-instantiation. The drvs are already on the dev box (preflight just
wrote them), so the right place to do the walk is here, before
submission.

Public entry point: :func:`compute_partition_locally`.

Subprocess invocation is dependency-injected via ``run_subprocess``
(the same seam used by :mod:`compiler_suit_runner.preflight`) so unit
tests stay hermetic.

Graph-walk algorithm (copied with attribution from
:mod:`compiler_suit_runner.workers.partition_worker`):

* For each variant we run ``nix derivation show --recursive <drv>``,
  which returns a flat JSON object ``{drv_path: node, ...}`` covering
  the variant's full transitive sub-graph in one shot.
* The sub-graph is merged into a cumulative cache so subsequent
  variants in the same call don't re-fetch shared drvs (variants in a
  shard typically share the toolchain + libc + kernel headers).
* For each variant we walk ``inputDrvs`` from its root, accumulating
  every transitive child into a per-variant frozenset. The walk is
  iterative + visited-tracked so cycles can't loop forever.

The classifier is a flat refcount over per-variant frozensets:

* For every drv path we count how many variants reference it.
* A drv is a "common dep" iff its refcount >= ``threshold`` AND it is
  NOT in ``toolchain_drvs`` (the canonical set of variant root drvs;
  variants are scheduled directly, not as common deps).
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from collections.abc import Callable
from typing import Any, Optional

from compiler_suit_runner.partition import VariantSpec


# ---------------------------------------------------------------------------
# Subprocess injection
#
# Mirrors the seam in :mod:`compiler_suit_runner.preflight` so tests can
# stub out every nix subprocess invocation.


# ``run_subprocess`` accepts argv (list[str]) and returns
# (stdout_bytes, stderr_bytes, returncode).
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell."""
    proc = subprocess.run(  # noqa: S603 - argv is constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# Public dataclass


@dataclasses.dataclass(frozen=True)
class PartitionResult:
    """Outcome of a local partition pass.

    ``common_dep_drvs`` is the list of ``(label, drv)`` pairs the
    primary should hoist into phase-2 manifests: drv paths whose
    cross-variant refcount cleared the configured threshold and that
    are not themselves variant roots (toolchains).

    ``per_variant_inputs`` is the raw ``variant_label -> {drv}`` map
    used to compute the refcount; it is preserved on the result for
    debugging, sanity checks, and future per-variant features (e.g.
    "which variants need this common dep?").
    """

    common_dep_drvs: tuple[tuple[str, str], ...]
    per_variant_inputs: dict[str, frozenset[str]]


# ---------------------------------------------------------------------------
# Nix subprocess wrapper


_NIX_BASE_CMD: tuple[str, ...] = (
    "nix",
    "--extra-experimental-features",
    "nix-command flakes",
    "derivation",
    "show",
    "--recursive",
)


def _show_drv_recursive(
    drv: str, runner: RunSubprocess
) -> dict[str, Any]:
    """Run ``nix derivation show --recursive <drv>`` and return parsed JSON.

    Equivalent to the worker's :func:`show_drv_recursive` but
    parametrised on ``runner`` directly rather than a ``WorkerEnv``.
    """
    cmd = [*_NIX_BASE_CMD, drv]
    stdout, stderr, returncode = runner(cmd)
    if returncode != 0:
        raise RuntimeError(
            f"nix derivation show --recursive {drv!r} exited"
            f" {returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )
    try:
        parsed = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"nix derivation show --recursive {drv!r} produced invalid"
            f" JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"nix derivation show --recursive {drv!r} did not return a"
            " JSON object"
        )
    return parsed


# ---------------------------------------------------------------------------
# Graph walk
#
# Lifted from compiler_suit_runner.workers.partition_worker: same shape,
# same robustness to the older array-form vs newer nested-form
# ``inputDrvs`` schema.


def _input_drv_keys(input_drvs_field: Any) -> list[str]:
    """Return the drv paths from an ``inputDrvs`` field.

    Tolerates both schema variants nix has emitted:

    * Older / array form: ``{"inputDrvs": {"<drv>": ["out"]}}``
    * Newer / nested form:
      ``{"inputDrvs": {"<drv>": {"dynamicOutputs": {}, "outputs": [...]}}}``
    """
    if not isinstance(input_drvs_field, dict):
        return []
    return [key for key in input_drvs_field.keys() if isinstance(key, str)]


def _extract_input_drvs(
    show_output: dict[str, Any], root_drv: str
) -> frozenset[str]:
    """Walk ``inputDrvs`` from ``root_drv`` and return all transitive deps.

    Iterative (uses a stack) and tracks visited nodes in a set so
    cycles cannot loop forever. The result excludes ``root_drv`` itself
    (the variant's own drv is dispatched as a build item, not as a
    common dep).
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
            continue
        for child in _input_drv_keys(node.get("inputDrvs")):
            if child == root_drv:
                continue
            collected.add(child)
            if child not in visited:
                stack.append(child)

    return frozenset(collected)


# ---------------------------------------------------------------------------
# Label derivation


# Strip the /nix/store/<hash>- prefix and the trailing .drv suffix to
# get the human-readable name nix gave the derivation.
_STORE_PATH_RE = re.compile(
    r"^/nix/store/[a-z0-9]+-(?P<name>.+?)(?:\.drv)?$"
)

# Filesystem-safe replacement for everything that isn't already
# alphanumeric / dash / underscore. Dots become dashes so version
# numbers ("glibc-2.39") survive as "glibc-2-39".
_LABEL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _label_from_drv(drv: str) -> str:
    """Derive a stable, filesystem-safe label from a drv path.

    Examples (illustrative):

    * ``/nix/store/aaa...-glibc-2.39.drv`` -> ``glibc-2-39``
    * ``/nix/store/bbb...-gcc-13-cc.drv``  -> ``gcc-13-cc``

    The hash prefix is intentionally stripped so two drv paths that
    share the same ``name`` collapse to the same label. Callers that
    need disambiguation should keep the full drv path alongside the
    label (the public API returns ``(label, drv)`` pairs).
    """
    match = _STORE_PATH_RE.match(drv)
    if match is None:
        # Fallback: treat the basename (minus .drv) as the name.
        base = drv.rsplit("/", 1)[-1]
        if base.endswith(".drv"):
            base = base[: -len(".drv")]
        name = base
    else:
        name = match.group("name")

    cleaned = _LABEL_UNSAFE_RE.sub("-", name)
    # Collapse runs of dashes the substitution may have created.
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "unnamed"


# ---------------------------------------------------------------------------
# Public entry point


def compute_partition_locally(
    variants: tuple[VariantSpec, ...],
    *,
    toolchain_drvs: frozenset[str],
    threshold: int = 10,
    run_subprocess: Optional[RunSubprocess] = None,
) -> PartitionResult:
    """Walk each variant's drv graph locally and classify input drvs.

    Reference-counts every transitive input drv across all variants.
    Returns a :class:`PartitionResult` with:

    * ``common_dep_drvs``: ``tuple[(label, drv), ...]`` for input drvs
      whose refcount is at least ``threshold`` AND that are NOT in
      ``toolchain_drvs``. Sorted by ``(label, drv)`` for determinism.
    * ``per_variant_inputs``: ``dict[variant_label, frozenset[drv]]``
      with the raw input sets used for the refcount.

    For each variant this calls ``nix derivation show --recursive
    <drv>`` locally; the dev box has the drvs from preflight
    instantiation. The walk is sequential — variants in a shard share
    huge swathes of inputs, so the per-variant cost amortises against
    the cumulative graph cache.
    """
    runner = run_subprocess or _default_run_subprocess

    # Cumulative ``nix derivation show --recursive`` cache. Each call
    # returns the variant's full transitive sub-graph; identical nodes
    # across variants are idempotent (nix is content-addressed).
    graph_cache: dict[str, Any] = {}

    per_variant: dict[str, frozenset[str]] = {}
    refcounts: dict[str, int] = {}

    for variant in variants:
        drv = variant["drv"]
        label = variant["label"]
        if drv not in graph_cache:
            sub_graph = _show_drv_recursive(drv, runner)
            graph_cache.update(sub_graph)

        inputs = _extract_input_drvs(graph_cache, drv)
        per_variant[label] = inputs
        for input_drv in inputs:
            refcounts[input_drv] = refcounts.get(input_drv, 0) + 1

    # Classify. ``threshold <= 0`` is treated as "any refcount, even
    # zero, qualifies"; the only drvs in ``refcounts`` are ones that
    # appeared at least once, but a zero threshold still has a
    # well-defined meaning (every observed drv is common).
    common: list[tuple[str, str]] = []
    for input_drv, count in refcounts.items():
        if count < threshold:
            continue
        if input_drv in toolchain_drvs:
            continue
        common.append((_label_from_drv(input_drv), input_drv))

    # Sort by (label, drv) so the result is deterministic regardless
    # of dict iteration order.
    common.sort()

    return PartitionResult(
        common_dep_drvs=tuple(common),
        per_variant_inputs=per_variant,
    )
