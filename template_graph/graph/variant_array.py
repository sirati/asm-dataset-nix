"""Per-arch variant array: hashes indexed by (node, variant)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class VariantArray:
    template_id: int
    arch: str
    variants: list[str]
    # Each cell is ``(hash, name)`` (the ``/nix/store/`` prefix implied)
    # or ``None`` when the variant doesn't realise that template node.
    # ``hash`` is the raw 32-byte ASCII base32 hash from the parsers.
    hashes: list[list[Optional[tuple[bytes, str]]]]
    # The matrix (package) this array was built under, taken from the
    # ``matrix-<binary>.drv`` section the planner was in. Consumers use
    # this instead of parsing the binary back out of a drv name, which
    # breaks for dashed names such as ``c-ares`` or ``zlib-ng``.
    # ``None`` only for arrays built outside the streaming planner.
    binary: Optional[str] = None
