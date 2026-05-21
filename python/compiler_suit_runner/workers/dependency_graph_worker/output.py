"""Atomic pickle writer for ``_dependency_graph.pkl`` + companion summary.

The dependency_graph_worker emits the Phase 4 descriptor list as a
pickle so the watcher (loaded via ``manifest_glue.load_descriptors_from_pickle``)
gets typed :class:`Phase4Descriptor` instances back with zero
re-tupling. A small ``_dependency_graph_summary.txt`` companion is
written alongside for operator inspection (``key: value`` per line,
sorted by key for diff-friendliness).
"""

from __future__ import annotations

import json
import os
import pathlib
import pickle
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Union

from compiler_suit_runner.dependency_graph_planner.manifest_glue import (
    PHASE4_PICKLE_FORMAT_VERSION,
    PHASE4_PICKLE_MAGIC,
)

if TYPE_CHECKING:
    from compiler_suit_runner.dependency_graph_planner import Phase4Descriptor


__all__ = [
    "DEPENDENCY_GRAPH_PICKLE",
    "DEPENDENCY_GRAPH_SUMMARY",
    "PHASE4_PICKLE_MAGIC",
    "PHASE4_PICKLE_FORMAT_VERSION",
    "write_phase4_descriptors",
    "write_phase4_summary_text",
]


# Output filenames written under ``<matrix_eval_out_dir>``.
DEPENDENCY_GRAPH_PICKLE = "_dependency_graph.pkl"
DEPENDENCY_GRAPH_SUMMARY = "_dependency_graph_summary.txt"
# Per-(binary, compiler, arch) sidecar directory under
# ``<matrix_eval_out_dir>``. Each placeholder build_variant /
# build_common_dep task reads its cell's manifest at dispatch time;
# the file is keyed deterministically so the manifest path is
# enumerable at submit-time (PH-B). See plan/placeholder-pattern-
# restructure.md.
DEPENDENCY_GRAPH_MANIFEST_DIR = "_manifests"


# ``PHASE4_PICKLE_MAGIC`` / ``PHASE4_PICKLE_FORMAT_VERSION`` are sourced
# from :mod:`compiler_suit_runner.dependency_graph_planner.manifest_glue`
# so the writer here and the reader there can never drift.


# Values stored in the summary dict; we accept ints, floats, and strings
# (e.g. binary list joined as ``"a/b/c"``) so the human-readable companion
# can carry mixed-type stats.
_SummaryValue = Union[int, float, str]


def write_phase4_descriptors(
    *,
    descriptors: Sequence[Phase4Descriptor],
    summary: Mapping[str, _SummaryValue],
    out_path: pathlib.Path,
) -> pathlib.Path:
    """Atomically write the Phase 4 descriptor list to ``out_path`` as a
    pickle file.

    The payload is a dict ``{format, format_version, written_at,
    descriptors, summary}``; ``descriptors`` is the original sequence
    materialised into a list (frozen dataclasses pickle cleanly). The
    file is written to ``<out_path>.tmp``, fsync'd, then ``os.replace``-d
    onto ``out_path`` so a concurrent watcher never sees a partial file.

    Uses :data:`pickle.HIGHEST_PROTOCOL` (Python 3.11+ → protocol 5).
    """
    payload = {
        "format": PHASE4_PICKLE_MAGIC,
        "format_version": PHASE4_PICKLE_FORMAT_VERSION,
        "written_at": time.time(),
        "descriptors": list(descriptors),
        "summary": dict(summary),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "wb", closefd=True) as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    os.replace(tmp, out_path)
    return out_path


def write_phase4_summary_text(
    *,
    summary: Mapping[str, _SummaryValue],
    out_path: pathlib.Path,
) -> pathlib.Path:
    """Atomically write ``_dependency_graph_summary.txt``.

    One ``key: value`` line per entry, sorted by key. ``value`` is
    rendered via plain ``str()`` for every type (ints, floats, and
    strings alike); strings are emitted verbatim, without ``repr``
    quoting. Atomic write via ``tmp + fsync + os.replace`` mirrors
    :func:`write_phase4_descriptors`, including ``.tmp`` cleanup on a
    crashed write.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for key in sorted(summary):
        value = summary[key]
        lines.append(f"{key}: {value}")
    encoded = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    os.replace(tmp, out_path)
    return out_path


def write_per_cell_manifests(
    *,
    descriptors: Sequence[Phase4Descriptor],
    matrix_eval_out_dir: pathlib.Path,
) -> list[pathlib.Path]:
    """Write one per-(binary, compiler, arch) sidecar manifest under
    ``<matrix_eval_out_dir>/_manifests/``.

    Each sidecar carries the list of build_variant / build_common_dep
    descriptor payloads for that cell, ordered by ``slot_idx``. The
    PH-B placeholder enumerator writes its task_id scheme to match
    ``<binary>__<compiler>__<arch>__slot<N>`` where N is the position
    in this list; the PH-C build_variant worker looks up its payload
    by reading ``payload.manifest_path`` + ``payload.slot_idx``.

    Returns the list of written sidecar paths (one per non-empty cell)
    for observability / testability.
    """
    sidecar_dir = matrix_eval_out_dir / DEPENDENCY_GRAPH_MANIFEST_DIR
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    # Group descriptors by (binary, compiler, arch). build_common_dep
    # entries live under their (binary, arch) cell with an empty
    # compiler — the placeholder enumerator emits them under a
    # synthetic ``__common__`` compiler tag to keep the filename
    # convention single-shaped.
    grouped: dict[tuple[str, str, str], list[Phase4Descriptor]] = {}
    for d in descriptors:
        binary = d.payload.get("binary", "")
        arch = d.payload.get("arch", "")
        if d.kind == "build_variant":
            compiler = d.payload.get("compiler_id", "")
        else:
            compiler = "__common__"
        if not binary or not arch or not compiler:
            continue
        key = (binary, compiler, arch)
        grouped.setdefault(key, []).append(d)
    written: list[pathlib.Path] = []
    for (binary, compiler, arch), cell_descriptors in sorted(grouped.items()):
        cell_descriptors = sorted(cell_descriptors, key=lambda d: d.task_id)
        variants = [
            {
                "slot_idx": idx,
                "kind": d.kind,
                "task_id": d.task_id,
                "name": d.name,
                "payload": dict(d.payload),
                "depends_on": list(d.depends_on),
            }
            for idx, d in enumerate(cell_descriptors)
        ]
        body = {
            "binary": binary,
            "compiler": compiler,
            "arch": arch,
            "variants": variants,
        }
        target = sidecar_dir / f"{binary}__{compiler}__{arch}.json"
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        written.append(target)
    return written
