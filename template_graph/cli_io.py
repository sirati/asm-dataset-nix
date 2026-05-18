"""Input-loading helpers for the ``template_graph`` CLI.

These helpers parse the on-disk inputs the CLI consumes — the
``variants`` file (``<label><TAB><drv_path>`` lines), the optional
``--toolchain-drvs`` file (one drv path per line), and the matrix
``binary`` name derived from the first variant drv. ``cli.py``
imports them via ``from .cli_io import ...``.
"""

from __future__ import annotations

from pathlib import Path

from .tree_walker import (
    parse_variant_path,
    TreeWalkError,
    VARIANT_SUFFIX,
)


def _read_text_lines(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _load_toolchain_drvs(path: Path | None) -> list[str]:
    if path is None:
        return []
    return list(_read_text_lines(path))


def _load_variants(path: Path) -> dict[str, str]:
    """Return ``{label: drv_path}``."""
    out: dict[str, str] = {}
    for line in _read_text_lines(path):
        if "\t" not in line:
            raise SystemExit(f"variants file line lacks TAB: {line!r}")
        label, drv = line.split("\t", 1)
        out[label.strip()] = drv.strip()
    return out


def _derive_binary(label_to_drv: dict[str, str], override: str | None) -> str:
    """Pick the matrix binary name. Prefer ``--binary`` override; else
    parse it out of the first variant drv name. Streaming wraps the
    variants in ``matrix-<binary>``; the binary derived from each
    variant drv MUST agree (the streaming planner asserts this).
    """
    if override:
        return override
    if not label_to_drv:
        raise SystemExit("no variants given; cannot derive --binary")
    first_drv = next(iter(label_to_drv.values()))
    base = first_drv.rsplit("/", 1)[-1]
    body = base.split("-", 1)[-1]  # strip leading hash
    try:
        binary, _arch, _comp, _opt = parse_variant_path(body)
    except TreeWalkError as exc:
        raise SystemExit(
            f"cannot derive --binary from {first_drv!r}: {exc}. "
            f"Pass --binary explicitly."
        ) from exc
    return binary


def _derive_streaming_label(drv_path: str) -> str:
    """Reproduce the streaming planner's ``f"{comp}-{opt}"`` label."""
    base = drv_path.rsplit("/", 1)[-1]
    body = base.split("-", 1)[-1]
    if not body.endswith(VARIANT_SUFFIX):
        raise SystemExit(
            f"drv {drv_path!r} doesn't look like a variant entry-point"
        )
    _binary, _arch, comp, opt = parse_variant_path(body)
    return f"{comp}-{opt}"
