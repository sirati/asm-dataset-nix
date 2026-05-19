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
    """Outcome of a single ``run_dependency_graph_task`` invocation."""

    output_path: pathlib.Path
    binary_count: int
    descriptor_count: int
    duration_seconds: float
