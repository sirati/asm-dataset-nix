"""Summary-text writer for ``_dependency_graph_summary.txt``.

The dependency_graph_worker writes a small
``_dependency_graph_summary.txt`` for operator inspection
(``key: value`` per line, sorted by key for diff-friendliness).
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Mapping
from typing import Union


__all__ = [
    "DEPENDENCY_GRAPH_SUMMARY",
    "write_phase4_summary_text",
]


# Output filename written under ``<matrix_eval_out_dir>``.
DEPENDENCY_GRAPH_SUMMARY = "_dependency_graph_summary.txt"


# Values stored in the summary dict; we accept ints, floats, and strings
# (e.g. binary list joined as ``"a/b/c"``) so the human-readable companion
# can carry mixed-type stats.
_SummaryValue = Union[int, float, str]


def write_phase4_summary_text(
    *,
    summary: Mapping[str, _SummaryValue],
    out_path: pathlib.Path,
) -> pathlib.Path:
    """Atomically write ``_dependency_graph_summary.txt``.

    One ``key: value`` line per entry, sorted by key. ``value`` is
    rendered via plain ``str()`` for every type (ints, floats, and
    strings alike); strings are emitted verbatim, without ``repr``
    quoting. Atomic write via ``tmp + fsync + os.replace``, including
    ``.tmp`` cleanup on a crashed write.
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
