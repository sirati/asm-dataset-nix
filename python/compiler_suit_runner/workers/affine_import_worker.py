"""Affine-import worker — runs each secondary-affine nix-store --import in a
worker subprocess.

BACKGROUND (#577)
-----------------

The legacy ``SuitTask.import_action`` callback ran the multi-GB
``nix-store --import`` on the secondary's single-threaded oploop.  At
scale (46 k-task runs, 14-worker secondaries) all workers on a node could
be queued behind a single large import, producing a collective-silence
wedge: the secondary stops sending heartbeats while blocked in the import,
the framework declares it dead, and the whole node's tasks are requeued.

The fix (framework #577): secondary-affine gates are now dispatched to a
WORKER SUBPROCESS just like ordinary build/eval tasks.  The oploop is
never blocked; the import runs in the subprocess's forked context and
signals completion via the normal ``WorkerOutput`` / error protocol.

FRAMEWORK API CONTRACT (#577, locked, do not change without owner ack)
-----------------------------------------------------------------------

* ``task.relative_path`` (str) — the gate's task_id (e.g.
  ``"import_common"``, ``"import_tc_<hash>"``,
  ``"import_matrix_drv_<binary>"``, …).  This field is NEW in #577;
  the pre-#577 ``Task`` object had ``resolved_path`` only.
  **Flag for orchestrator:** every reference to ``task.relative_path``
  below is a #577-coupled assumption and will not work on an older head.

* ``task.payload`` — the JSON payload the gate task was emitted with (the
  ``{item_class, task_id, …}`` dict from ``_discover_import_gate_tasks``
  and ``_build_common_dep_gate_infos``).  The payload content is UNUSED
  by the import logic; ``task.relative_path`` is the authoritative gate id.

* ``WorkerOutput()`` on success — releases the gate and drains queued
  dependents.

* ``RecoverableError`` — transient failure (OSError from the underlying
  ``nix-store --import``); the framework retries the task.

* ``NonRecoverableError`` — permanent failure (unknown gate id, or a
  hard archive-miss that the ``ensure_*`` helpers escalate).  Framework
  cascade-fails all dependents.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Optional


def main() -> int:
    """Subprocess entry point for the affine_import worker.

    Spawned by the dynamic_runner framework as::

        python -m compiler_suit_runner.workers.affine_import_worker

    The framework owns all wire-protocol plumbing (Ready handshake, command
    framing, SIGTERM → SystemExit).  This module supplies the per-task
    :func:`handle` body.

    CLI args
    --------

    Standard dynamic_runner worker args (mutually-exclusive comm channel):

    * ``--dynamic_queue <fd>`` — shared-memory queue fd (multi-computer local).
    * ``--socket-path <path>`` — Unix socket path (SLURM multi-node).

    Plus:

    * ``--log-file <path>`` — optional; defaults to
      ``/app/log-network/worker_<pid>.log``.
    * ``--flake-ref <ref>`` — flake reference (passed to comply with the
      common arg set; UNUSED by the import logic but kept so the framework's
      unified arg-builder can thread it without a type-id special-case).
    * ``--shared-fs <path>`` — NFS root; informational / for future use.
    * ``--secondary-id <id>`` — secondary identity; informational.
    * ``--matrix-eval-out-dir <path>`` — REQUIRED.  The shared bind-mounted
      directory where all ``*.nix-archive`` files live.  The import helpers
      derive archive paths from this root.
    """
    import argparse  # noqa: PLC0415

    from dynamic_runner.worker import (  # noqa: PLC0415
        NonRecoverableError,
        RecoverableError,
        Task,
        WorkerOutput,
        run,
    )
    from compiler_suit_runner.workers.build_worker import (  # noqa: PLC0415
        _run_affine_import,
    )

    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner.workers.affine_import_worker",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dynamic_queue", type=int)
    group.add_argument("--socket-path", type=str)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument(
        "--flake-ref",
        type=str,
        default=None,
        help=(
            "Flake reference (unused by the import logic; accepted so the"
            " framework's unified build_worker_command_args can thread it"
            " without a type-id special-case)."
        ),
    )
    parser.add_argument(
        "--shared-fs",
        type=str,
        default=None,
        help="NFS root; informational for log lines.",
    )
    parser.add_argument(
        "--secondary-id",
        type=str,
        default="",
        help="This worker's secondary id; informational for log lines.",
    )
    parser.add_argument(
        "--matrix-eval-out-dir",
        type=str,
        required=True,
        help=(
            "Shared bind-mounted directory containing all *.nix-archive"
            " files produced by the preflight + matrix_eval + dep_graph"
            " phases.  The import helpers derive archive paths from this"
            " root.  REQUIRED."
        ),
    )
    args, _ = parser.parse_known_args()

    _worker_log = args.log_file or f"/app/log-network/worker_{os.getpid()}.log"
    try:
        logging.basicConfig(
            filename=_worker_log,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )
        logging.getLogger(
            "compiler_suit_runner.affine_import_worker.startup"
        ).info(
            "affine_import_worker subprocess started; pid=%d argv=%r",
            os.getpid(), sys.argv,
        )
    except OSError:
        pass  # not writable in tests / outside SLURM wrapper — acceptable

    matrix_eval_out_dir = pathlib.Path(args.matrix_eval_out_dir)

    _handle_log = logging.getLogger(
        "compiler_suit_runner.affine_import_worker.handle"
    )

    def handle(task: Task) -> Optional[WorkerOutput]:
        # #577-CONTRACT: task.relative_path IS the gate's task_id — the bare
        # import identity ("import_common", "import_tc_<hash>",
        # "import_common_dep_<hash>", ...). The gate carries NO ``.json``
        # sidecar: the affine task imports a derivation/closure archive, not a
        # file, and its relative_path must equal the id that dependents
        # reference (else the framework can't resolve the secondary-affine dep).
        # Flag for orchestrator: this attribute is NEW in framework #577;
        # it does NOT exist on pre-#577 Task objects.
        gate_id: str = task.relative_path  # type: ignore[attr-defined]
        _handle_log.info(
            "handle: starting affine_import gate_id=%r matrix_eval_out_dir=%s",
            gate_id, matrix_eval_out_dir,
        )
        try:
            _run_affine_import(gate_id, matrix_eval_out_dir)
        except OSError as exc:
            # Transient: nix-store --import failed with a filesystem /
            # nix-daemon error.  The framework will retry the gate task.
            _handle_log.warning(
                "handle: affine_import gate_id=%r raised OSError (transient): %s",
                gate_id, exc,
            )
            raise RecoverableError(
                f"affine_import {gate_id!r}: transient OSError: {exc}"
            ) from exc
        except ValueError as exc:
            # Unknown gate id — this is a consumer programming error.
            _handle_log.error(
                "handle: affine_import gate_id=%r raised ValueError"
                " (unknown gate — non-recoverable): %s",
                gate_id, exc,
            )
            raise NonRecoverableError(
                f"affine_import {gate_id!r}: unknown gate id: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # nix-DAEMON startup race: the per-node nix-daemon socket may not
            # exist yet when this node's first import fires (the in-worker
            # retry in ``import_archive`` already waits ~63s; this is the
            # backstop if that wasn't enough).  The import helper surfaces it
            # as a RuntimeError, which would otherwise be treated as
            # non-recoverable and CASCADE-FAIL every dependent on this node —
            # including shared common_deps, whose failure then poisons their
            # dependents fleet-wide.  It is transient, so make the gate
            # RECOVERABLE: the framework re-dispatches it once the daemon is up.
            _low = str(exc).lower()
            if "cannot connect to socket" in _low or "daemon-socket" in _low:
                _handle_log.warning(
                    "handle: affine_import gate_id=%r hit nix-daemon"
                    " not-ready (transient, recoverable): %s",
                    gate_id, exc,
                )
                raise RecoverableError(
                    f"affine_import {gate_id!r}: nix-daemon not ready"
                    f" (transient): {exc}"
                ) from exc
            # Any other exception (e.g. RuntimeError from an archive miss)
            # is non-recoverable: the archive is genuinely absent or
            # corrupt, and retrying won't fix it.
            _handle_log.error(
                "handle: affine_import gate_id=%r raised %s (non-recoverable): %s",
                gate_id, type(exc).__name__, exc,
            )
            raise NonRecoverableError(
                f"affine_import {gate_id!r}: {type(exc).__name__}: {exc}"
            ) from exc
        _handle_log.info(
            "handle: affine_import gate_id=%r DONE", gate_id,
        )
        return WorkerOutput()

    run(handle, args=args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
