"""Typed errors + result dataclasses for the dependency_graph_worker."""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Optional


__all__ = [
    "DependencyGraphWorkerError",
    "DependencyGraphResult",
]


class DependencyGraphWorkerError(Exception):
    """Raised on any binary's planning failure.

    The worker's exit code is nonzero on any one binary's failure;
    this exception carries a structured ``binary`` + ``stage`` so the
    watcher can attach context to its log line.
    """

    def __init__(
        self,
        binary: str,
        stage: str,
        message: str,
        *,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(f"binary={binary!r} stage={stage!r}: {message}")
        self.binary = binary
        self.stage = stage
        self.cause = cause


@dataclasses.dataclass
class DependencyGraphResult:
    """Outcome of a single ``run_dependency_graph_task`` invocation.

    Counter fields (all defaulting to 0 for backward compatibility) are
    populated by :mod:`.plan` from the streaming planner result + the
    emitted phase-4 descriptor list, and surfaced in the worker's
    post-planning summary log line (plan §E8).
    """

    output_path: pathlib.Path
    binary_count: int
    descriptor_count: int
    duration_seconds: float
    # ── Phase 6.1b: planning counters ─────────────────────────────────
    templates: int = dataclasses.field(default=0)
    meta_templates: int = dataclasses.field(default=0)
    variants: int = dataclasses.field(default=0)
    common_deps_cross_arch: int = dataclasses.field(default=0)
    common_deps_family: int = dataclasses.field(default=0)
    common_deps_uni_arch: int = dataclasses.field(default=0)
    common_deps_arch_indep: int = dataclasses.field(default=0)
    source_terminal_skipped: int = dataclasses.field(default=0)
    toolchain_wired: int = dataclasses.field(default=0)
    stdenv_subtrees: int = dataclasses.field(default=0)
    violations: int = dataclasses.field(default=0)
