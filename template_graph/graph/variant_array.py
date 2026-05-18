"""Per-arch variant array: hashes indexed by (node, variant)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class VariantArray:
    template_id: int
    arch: str
    variants: list[str]
    hashes: list[list[Optional[str]]]
