"""Read ONE derivation record from disk via ``nix derivation show``.

Strict streaming contract: callers fetch a single drv record at a
time, use it, and drop it. We never call ``--recursive`` and never
hold the whole closure in memory.

Output shape from ``nix derivation show <drv>`` (nix 2.34+):

    { "derivations": { "<basename>.drv": { ... } }, "version": 4 }

The inner record uses BASENAMES (not full /nix/store paths) in its
``inputs.drvs`` map. We normalise these to full paths so the
algorithm can pass them straight back into ``read_drv_record`` to
recurse.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable


# RunSubprocess(argv) -> (stdout, stderr, returncode)
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


class DrvIoError(RuntimeError):
    def __init__(self, drv_path: str, detail: str) -> None:
        super().__init__(f"drv_io({drv_path!r}): {detail}")
        self.drv_path = drv_path
        self.detail = detail


def _default_runner(argv: list[str]) -> tuple[bytes, bytes, int]:
    proc = subprocess.run(argv, capture_output=True, check=False)
    return proc.stdout, proc.stderr, proc.returncode


_STORE_PREFIX = "/nix/store/"


def read_drv_record(
    drv_path: str,
    *,
    run_subprocess: RunSubprocess = _default_runner,
) -> dict:
    """Return a single-drv record with normalised input drv paths.

    Returned shape::

        {
          "name": "<derivation name>",
          "inputDrvs": { "/nix/store/.../<input>.drv": {...}, ... },
        }

    Any fields the algorithm doesn't need are dropped — we keep the
    in-flight record tiny because we may walk thousands of drvs in
    one planner run.
    """
    argv = ["nix", "derivation", "show", drv_path]
    stdout, stderr, rc = run_subprocess(argv)
    if rc != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        # Strip the "warning: 'show-derivation' is a deprecated alias"
        # banner that newer nix prefixes onto perfectly good error
        # output — not relevant if we're already on `derivation show`.
        raise DrvIoError(drv_path, detail or f"nix exited with rc={rc}")
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DrvIoError(drv_path, "malformed JSON") from exc
    if not isinstance(payload, dict):
        raise DrvIoError(drv_path, "top-level JSON is not an object")
    derivations = payload.get("derivations")
    if not isinstance(derivations, dict) or len(derivations) != 1:
        raise DrvIoError(drv_path, "unexpected `derivations` shape")
    (_only_basename, rec), = derivations.items()
    if not isinstance(rec, dict):
        raise DrvIoError(drv_path, "record is not an object")
    inputs = rec.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise DrvIoError(drv_path, "`inputs` is not an object")
    drvs_by_basename = inputs.get("drvs") or {}
    if not isinstance(drvs_by_basename, dict):
        raise DrvIoError(drv_path, "`inputs.drvs` is not an object")
    full_input_drvs: dict[str, dict] = {}
    for bn, info in drvs_by_basename.items():
        if not isinstance(bn, str):
            raise DrvIoError(drv_path, "input drv key is not a string")
        full_input_drvs[_STORE_PREFIX + bn] = info if isinstance(info, dict) else {}
    name = rec.get("name", "")
    if not isinstance(name, str):
        name = ""
    return {"name": name, "inputDrvs": full_input_drvs}
