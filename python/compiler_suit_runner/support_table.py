"""Parse ``table.md`` — the authoritative (compiler, arch) support matrix.

``table.md`` lives at the flake root and is hand-curated to record which
``(compiler, target arch)`` pairs are known to build successfully on
the upstream nixpkgs revisions we pin. The cells use three statuses:

* ``OK``   — builds; safe to include in the variant matrix.
* ``FAIL`` — toolchain or stdenv works at eval time, but the actual
  build fails reliably (so dispatching tasks for this combo wastes
  worker slots).
* ``n/a``  — cross-compilation isn't representable at all (compiler
  doesn't know the target triple, or the target's minimum compiler
  version isn't met). Functionally indistinguishable from FAIL for
  filtering purposes.

The runner uses this table to filter the per-(pkg, arch) variant set
*before* asking nix to evaluate ``_drvPaths`` — without the filter,
forcing a broken cell can crash preflight with errors that escape
``builtins.tryEval`` (e.g. a hard ``builtins.throw`` from
``lib/old-gcc-cross.nix`` for ``gcc5 + mips64el``).

The parser is intentionally minimal: it expects a single GFM table
with the first column header ``Compiler`` and a header row of arch
labels. Anything outside that one table is ignored (so the
``Passed/Failed/Skipped/Unsupported`` summary block at the top is
fine to keep).
"""

from __future__ import annotations

import functools
import pathlib
from typing import Optional

__all__ = [
    "SupportStatus",
    "load_support_table",
    "is_supported",
    "default_table_path",
]

# String-typed enum because the raw cell values are 2-3 ASCII chars
# and we want them to round-trip through structured logs / JSON without
# extra encoding.
SupportStatus = str  # one of "OK", "FAIL", "n/a"


def default_table_path(flake_root: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Return the canonical path to ``table.md`` under ``flake_root``.

    Defaults to the cwd when ``flake_root`` is not supplied — preflight
    runs from the flake directory.
    """
    root = flake_root or pathlib.Path.cwd()
    return pathlib.Path(root) / "table.md"


@functools.lru_cache(maxsize=8)
def load_support_table(
    path: Optional[pathlib.Path] = None,
) -> dict[tuple[str, str], SupportStatus]:
    """Parse ``table.md`` into a ``{(compiler, arch): status}`` dict.

    Cached on the resolved path so repeated calls don't re-read the
    file. Pass a fresh ``path`` on disk-modified test cases (or call
    ``load_support_table.cache_clear()``).

    Returns an empty dict if the file is missing — callers that want
    "no filter" can fall back gracefully.
    """
    target = pathlib.Path(path) if path is not None else default_table_path()
    if not target.is_file():
        return {}

    rows: dict[tuple[str, str], SupportStatus] = {}
    archs: list[str] = []
    in_table = False
    saw_header = False
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            # Not a table line. If we were inside a table, close it
            # — table.md only has one matrix block.
            if in_table:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not saw_header:
            # First row of the table is the header — capture archs.
            if cells and cells[0].lower() == "compiler":
                archs = cells[1:]
                saw_header = True
                in_table = True
            continue
        # Skip the markdown separator row (``|---|---|...``).
        if cells and set(cells[0]) <= {"-", ":"}:
            continue
        compiler = cells[0]
        if not compiler:
            continue
        for i, status in enumerate(cells[1:]):
            if i >= len(archs):
                break
            rows[(compiler, archs[i])] = status
    return rows


def is_supported(
    table: dict[tuple[str, str], SupportStatus],
    compiler: str,
    arch: str,
) -> bool:
    """True iff ``table`` records ``(compiler, arch)`` as ``OK``.

    Unknown combos (compiler/arch not listed in table.md) are treated
    as ``True`` — the table only enumerates compilers we ship as
    legacy or modern, so a missing row means "not in the matrix at all"
    and we let other layers reject. ``FAIL`` and ``n/a`` both return
    ``False`` (both mean "do not dispatch this combo").

    The native arch (``x86_64``) is always allowed — table.md lists
    only cross-compilation targets.
    """
    if arch == "x86_64":
        return True
    status = table.get((compiler, arch))
    if status is None:
        return True
    return status == "OK"
