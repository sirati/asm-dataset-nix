"""Minimal TaskDefinition: N sentinel items, one per requested debug
container. The framework dispatches one to each secondary, which
spawns the worker module that exec's sshd.
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from collections.abc import Iterable
from pathlib import Path

from dynamic_runner._shared import TaskInfo
from dynamic_runner._shared.binary_info import BinaryIdentifier
from dynamic_runner.task_protocol import (
    PhaseSpec,
    TaskTypeSpec,
)


PHASE_ID = "debug"
TYPE_ID = "sshd"
WORKER_MODULE = "ssh_debug_runner.worker"


class SshDebugTask:
    """TaskDefinition consumed by `dynamic_runner.run`.

    Holds a fixed item count (``n_secondaries``). Memory/CPU
    estimates are arbitrary low values — the workload is just sshd.
    """

    def __init__(self, n_secondaries: int = 2) -> None:
        if n_secondaries < 1:
            raise ValueError("n_secondaries must be >= 1")
        self.n_secondaries = n_secondaries

    # ── Topology ────────────────────────────────────────────────────────

    def get_phases(self) -> tuple[PhaseSpec, ...]:
        return (
            PhaseSpec(
                phase_id=PHASE_ID,
                types=(
                    TaskTypeSpec(
                        type_id=TYPE_ID,
                        worker_module=WORKER_MODULE,
                        timeout_seconds=None,
                        reserved_memory_per_worker=64 * 1024 * 1024,
                    ),
                ),
            ),
        )

    # ── Item discovery ─────────────────────────────────────────────────

    def _sentinel_items(self) -> list[TaskInfo]:
        # One sentinel item per debug container the user asked for.
        # `path` is intentionally a non-existent absolute path: the
        # framework's queue_initial_staging (c40aeeb+) treats a
        # ``compute_file_hash`` failure as "sentinel item, skip
        # StageFile queueing" — exactly what we want, since
        # ssh-debug has no real source binaries to ship to
        # secondaries. (If the file existed and was empty, the
        # primary would queue a StageFile that the secondary then
        # tries — and fails — to stage from its src_network.)
        # `identifier` is required by the dataclass; placeholder
        # values are fine since ssh-debug doesn't work on real
        # binaries.
        return [
            TaskInfo(
                path=Path(f"/nonexistent/ssh-debug-{i:02d}"),
                size=1,
                identifier=BinaryIdentifier(
                    binary_name=f"ssh-debug-{i:02d}",
                    platform="x64",
                    compiler="none",
                    version="0",
                    opt_level="O0",
                ),
                phase_id=PHASE_ID,
                type_id=TYPE_ID,
                affinity_id=None,
                payload={"index": i},
            )
            for i in range(self.n_secondaries)
        ]

    def discover_items(
        self, source_dir: Path, args: Namespace
    ) -> Iterable[TaskInfo]:
        return iter(self._sentinel_items())

    # ── Per-type plumbing ──────────────────────────────────────────────

    def estimate_memory(self, item: TaskInfo) -> int:
        return 64 * 1024 * 1024  # 64 MiB — sshd is tiny

    def add_task_arguments(self, parser: ArgumentParser) -> None:
        # All knobs are baked into the image / framework CLI; nothing
        # task-specific to expose.
        return None

    def build_worker_command_args(
        self,
        type_id: str,
        args: Namespace,
        source_dir: Path,
        output_dir: Path,
        skip_existing: bool,
    ) -> list[str]:
        return []

    def get_output_filename_pattern(
        self, type_id: str, item: TaskInfo
    ) -> str:
        return "{stem}.sshd"

    # ── Lifecycle hooks ────────────────────────────────────────────────

    def on_run_start(
        self, source_dir: Path, output_dir: Path, args: Namespace
    ) -> None:
        return None

    def on_run_end(self, success: bool) -> None:
        return None

    def on_phase_start(self, phase_id: str) -> None:
        return None

    def on_phase_end(
        self, phase_id: str, completed: int, failed: int
    ) -> None:
        return None
