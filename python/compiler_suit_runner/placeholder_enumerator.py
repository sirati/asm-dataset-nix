"""Submit-time enumeration of placeholder ``build_variant`` tasks.

The placeholder pattern (per plan/placeholder-pattern-restructure.md
PH-B) declares K-sized ``build_variant`` TaskInfos at submit time, one
per ``(binary, compiler, arch)`` cell, where K is a conservative upper
bound on the number of variants that the ``dependency_graph`` planner
will emit for that cell. Each placeholder's ``task_depends_on``
references the per-binary ``dependency_graph__<binary>`` task; the
CRDT's atomic ``resume_blocked_on`` transition (cluster_state/apply.rs)
flips Blocked → Pending in the same apply pass as ``dep_graph``'s
TaskCompleted, sidestepping the race that bit ``on_phase_end``-driven
``spawn_tasks``.

At dispatch time the worker (PH-C) reads the per-cell sidecar manifest
that :func:`workers.dependency_graph_worker.output.write_per_cell_manifests`
writes, looks up its ``slot_idx``, and either executes the resolved
variant or returns a no-op when the slot is out of range.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from .manifest_gen import ManifestHeader, dependency_graph_task_id
from .support_table import SupportStatus, is_supported

__all__ = [
    "DEFAULT_PLACEHOLDER_K",
    "compute_placeholder_k",
    "build_variant_placeholder_task_id",
    "build_variant_placeholder_name",
    "make_build_variant_placeholder_header",
    "enumerate_build_variant_placeholders",
]


# Conservative upper bound on variants per ``(binary, compiler, arch)``
# cell. The full flag matrix is 7 opts × 7 flag_sets × 2 hardening = 98,
# plus sanitizer / march expansions that grow the cell by ~2-3x on x86_64
# (see lib/flags.nix). 200 covers every cell observed empirically (the
# global derivation count divided by compilers × archs × packages places
# the average ~55, the worst cells below 200).
DEFAULT_PLACEHOLDER_K = 200


def compute_placeholder_k(variant_sample: int | None) -> int:
    """Per-cell placeholder slot count.

    When the operator passed ``--variant-sample N`` (N > 0), the
    matrix_eval worker emits at most N variants per cell, so K = N
    suffices. Otherwise fall back to :data:`DEFAULT_PLACEHOLDER_K` as a
    conservative upper bound.
    """
    if variant_sample and int(variant_sample) > 0:
        return int(variant_sample)
    return DEFAULT_PLACEHOLDER_K


def build_variant_placeholder_task_id(
    sys_name: str,
    binary: str,
    compiler: str,
    arch: str,
    slot_idx: int,
) -> str:
    """Stable per-slot task id."""
    return (
        f"build_variant__{sys_name}__{binary}__{compiler}__{arch}"
        f"__slot{slot_idx}"
    )


def build_variant_placeholder_name(
    binary: str, compiler: str, arch: str, slot_idx: int,
) -> str:
    """Filesystem-friendly manifest name (no ``sys_name`` tag)."""
    return (
        f"build_variant__{binary}__{compiler}__{arch}__slot{slot_idx}"
    )


def make_build_variant_placeholder_header(
    *,
    binary: str,
    sys_name: str,
    compiler: str,
    arch: str,
    slot_idx: int,
    manifest_path: str,
) -> ManifestHeader:
    """Build one placeholder ``build_variant`` header.

    ``payload.manifest_path`` points to the per-cell sidecar file that
    the dependency_graph worker writes. The build_variant worker reads
    that file at dispatch time and looks up ``slot_idx`` in its
    ``variants`` array; an out-of-range index is treated as a no-op (the
    placeholder slot exceeded the actual variant count for the cell).

    ``payload.placeholder = True`` is the unambiguous marker the
    consumer dispatch path uses to distinguish placeholder shape from
    the legacy resolved-drv shape that direct
    :func:`manifest_gen.make_build_variant_header` callers produce.
    """
    payload = {
        "binary": binary,
        "sys": sys_name,
        "compiler": compiler,
        "arch": arch,
        "slot_idx": slot_idx,
        "manifest_path": manifest_path,
        "placeholder": True,
    }
    return ManifestHeader(
        item_class="build_variant",
        name=build_variant_placeholder_name(
            binary, compiler, arch, slot_idx,
        ),
        size=0,
        payload=payload,
        task_id=build_variant_placeholder_task_id(
            sys_name, binary, compiler, arch, slot_idx,
        ),
        task_depends_on=(dependency_graph_task_id(binary),),
    )


def enumerate_build_variant_placeholders(
    per_binary_metadata: dict[str, dict],
    *,
    sys_name: str,
    tc_pairs: Iterable[tuple[str, str]],
    support_table: dict[tuple[str, str], SupportStatus],
    matrix_eval_out_dir: str,
) -> Iterator[ManifestHeader]:
    """Yield placeholder headers for every viable ``(binary, compiler, arch)``.

    Filters cells by:

    * ``arch`` must appear in ``per_binary_metadata[binary]['archs']`` —
      the binary advertises a per-arch matrix and the submitter only
      promises placeholders against archs it confirmed exist.
    * ``is_supported(support_table, compiler, arch)`` must be true —
      ``FAIL`` / ``n/a`` cells are dropped before they ever reach the
      cluster.

    K per ``(binary, compiler, arch)`` derives from the per-binary
    ``variant_sample`` (via :func:`compute_placeholder_k`).
    ``tc_pairs`` carries the resolved ``(arch, compiler)`` toolchain
    list from preflight; only those pairs become candidates.
    ``matrix_eval_out_dir`` is the bind-mounted directory the
    dep_graph worker drops sidecars into.
    """
    pairs = sorted(set(tc_pairs))
    for binary, meta in per_binary_metadata.items():
        archs = set(meta.get("archs") or ())
        if not archs:
            continue
        k = compute_placeholder_k(meta.get("variant_sample"))
        if k <= 0:
            continue
        for arch, compiler in pairs:
            if arch not in archs:
                continue
            if not is_supported(support_table, compiler, arch):
                continue
            sidecar = (
                f"{matrix_eval_out_dir}/_manifests/"
                f"{binary}__{compiler}__{arch}.json"
            )
            for slot_idx in range(k):
                yield make_build_variant_placeholder_header(
                    binary=binary,
                    sys_name=sys_name,
                    compiler=compiler,
                    arch=arch,
                    slot_idx=slot_idx,
                    manifest_path=sidecar,
                )
