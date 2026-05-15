"""Derivation-graph helpers built on ``nix show-derivation``.

This module is the small primitive Phase 1 needs to walk the union of
variant ``.drv`` files and find their input derivations. The Phase 1
planner refcounts these inputs across variants to pick "common deps"
(refcount >= 2) and to assemble the per-variant dependency set for
``task_depends_on`` wiring (see Part B of the cluster-side dispatch
plan).

The ``.drv`` file on disk is textual ATerm; rather than parse that
format directly we shell out to ``nix show-derivation <drv>`` which
emits a stable, well-defined JSON envelope:

    {
      "<drv_path>": {
        "inputDrvs": { "<input-drv-1>": {...}, "<input-drv-2>": {...} },
        "inputSrcs": [...],
        "outputs": {...},
        ...
      }
    }

Subprocess invocation is dependency-injected via ``run_subprocess`` so
unit tests stay hermetic — the real nix daemon never has to be reached.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

# RunSubprocess(argv) -> (stdout_bytes, stderr_bytes, returncode)
#
# Matches the shape used elsewhere in the package (preflight.py etc.):
# bytes in/out, explicit returncode, no exception on non-zero.
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


class DrvGraphError(RuntimeError):
    """Raised when ``read_input_drvs`` cannot extract the inputDrvs set.

    Attributes
    ----------
    drv_path
        The ``.drv`` path the call was made against.
    detail
        Human-readable detail (e.g. captured stderr, ``"malformed JSON"``,
        ``"unexpected JSON shape"``).
    """

    def __init__(self, drv_path: str, detail: str) -> None:
        super().__init__(f"drv_graph({drv_path!r}): {detail}")
        self.drv_path = drv_path
        self.detail = detail


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Default subprocess runner: invoke ``argv`` and capture output."""
    proc = subprocess.run(argv, capture_output=True, check=False)
    return proc.stdout, proc.stderr, proc.returncode


def read_input_drvs(
    drv_path: str,
    *,
    run_subprocess: RunSubprocess = _default_run_subprocess,
) -> set[str]:
    """Return the set of input derivations for ``drv_path``.

    Shells out to ``nix show-derivation <drv_path>``, parses the JSON
    response, and returns the keys of the single top-level entry's
    ``inputDrvs`` mapping. If ``inputDrvs`` is absent or empty the
    result is an empty set.

    Parameters
    ----------
    drv_path
        Path to a ``.drv`` file (a /nix/store/...drv path).
    run_subprocess
        Injection seam for the subprocess call. Tests override this
        with a stub returning a synthetic ``(stdout, stderr, rc)``
        triple; production code uses the default runner.

    Raises
    ------
    DrvGraphError
        If ``nix show-derivation`` exits non-zero, emits non-JSON
        output, or returns a structure that does not match the
        documented envelope.
    """
    argv = ["nix", "show-derivation", drv_path]
    stdout, stderr, rc = run_subprocess(argv)
    if rc != 0:
        # Surface stderr verbatim so the caller (or the framework's
        # error log) can see why nix refused — common causes:
        # path doesn't exist, daemon unavailable, store corruption.
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise DrvGraphError(drv_path, detail or f"nix exited with rc={rc}")

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DrvGraphError(drv_path, "malformed JSON") from exc

    if not isinstance(payload, dict) or len(payload) != 1:
        # We expect exactly one top-level key (the drv_path itself);
        # zero entries or multiple entries are both wire-format
        # violations as far as this helper is concerned.
        raise DrvGraphError(drv_path, "unexpected JSON shape")

    (_only_key, entry), = payload.items()
    if not isinstance(entry, dict):
        raise DrvGraphError(drv_path, "unexpected JSON shape")

    input_drvs = entry.get("inputDrvs", {})
    if input_drvs is None:
        return set()
    if not isinstance(input_drvs, dict):
        raise DrvGraphError(drv_path, "unexpected JSON shape")

    return set(input_drvs.keys())
