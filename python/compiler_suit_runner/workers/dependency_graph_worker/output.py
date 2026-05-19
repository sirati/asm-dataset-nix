"""Atomic pickle writer for ``_dependency_graph.pkl`` + companion summary.

The dependency_graph_worker emits the Phase 4 descriptor list as a
pickle so the watcher (loaded via ``manifest_glue.load_descriptors_from_pickle``)
gets typed :class:`Phase4Descriptor` instances back with zero
re-tupling. A small ``_dependency_graph_summary.txt`` companion is
written alongside for operator inspection (``key: value`` per line,
sorted by key for diff-friendliness).
"""

from __future__ import annotations

import os
import pathlib
import pickle
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Union

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


# Magic + version stamped into the pickle payload so the reader can
# refuse a file produced by an incompatible writer without unpickling
# anything more than the top-level dict.
PHASE4_PICKLE_MAGIC = "csr.dependency_graph.phase4.v1"
PHASE4_PICKLE_FORMAT_VERSION = 1


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
    formatted via ``repr`` for strings (so embedded whitespace is
    visible) and ``str`` for ints/floats. Atomic write via
    ``tmp + fsync + os.replace`` mirrors :func:`write_phase4_descriptors`.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for key in sorted(summary):
        value = summary[key]
        if isinstance(value, str):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {value}")
    encoded = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, out_path)
    return out_path
