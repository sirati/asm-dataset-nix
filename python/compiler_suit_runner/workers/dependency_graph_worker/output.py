"""Atomic JSON writer for ``_dependency_graph.json``."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from typing import Any


__all__ = [
    "DEPENDENCY_GRAPH_JSON",
    "write_dependency_graph_json",
]


# Output filename written under ``<matrix_eval_out_dir>``.
DEPENDENCY_GRAPH_JSON = "_dependency_graph.json"


def write_dependency_graph_json(
    out_path: pathlib.Path, descriptors: list[Any],
) -> pathlib.Path:
    """Atomically write the Phase 4 descriptor list to ``out_path``.

    Each descriptor is :func:`dataclasses.asdict`-converted. Tuples
    become lists in JSON (depends_on); the consumer
    (dependency_graph_planner spawn step) re-tuples on read.
    """
    serialised: list[dict] = []
    for d in descriptors:
        if dataclasses.is_dataclass(d):
            entry = dataclasses.asdict(d)
        elif isinstance(d, dict):
            entry = dict(d)
        else:
            entry = {"opaque": repr(d)}
        # Tuples in dataclasses.asdict become tuples — JSON wants lists.
        if "depends_on" in entry and isinstance(entry["depends_on"], tuple):
            entry["depends_on"] = list(entry["depends_on"])
        serialised.append(entry)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    encoded = json.dumps(
        {"phase4_descriptors": serialised}, indent=2, sort_keys=True,
    ).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, out_path)
    return out_path
