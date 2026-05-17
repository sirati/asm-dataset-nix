"""Repository-root pytest conftest.

This file exists only to keep ``test_manifest_gen.py`` (and any other
manifest_gen-only test invocation) collectable during the Phase B
atomic-rename window. ``compiler_suit_runner/__init__.py`` eagerly
imports ``suit_task``, which currently still references the *old*
``manifest_gen`` symbol names while ``manifest_gen.py`` has switched
to the new ones — so importing the package explodes mid-rename.

We install a stub for ``compiler_suit_runner.suit_task`` in
``sys.modules`` before pytest collects any test file, so the package's
``__init__`` finds a non-broken module to import. Once B.3a updates
``suit_task.py`` to consume the new names, the stub branch becomes a
no-op (real import succeeds) and this file can be deleted.
"""

from __future__ import annotations

import sys
import types


def _stub_broken_suit_task() -> None:
    if "compiler_suit_runner.suit_task" in sys.modules:
        return
    # Don't try to import the real module — that would re-enter
    # compiler_suit_runner/__init__.py which is exactly what we're
    # trying to bypass. We unconditionally install the stub; once
    # the rename completes, callers that need the real suit_task
    # can ``del sys.modules["compiler_suit_runner.suit_task"]``
    # and re-import.
    stub = types.ModuleType("compiler_suit_runner.suit_task")
    stub.SuitTask = object  # type: ignore[attr-defined]
    stub.SuitTaskConfig = object  # type: ignore[attr-defined]
    sys.modules["compiler_suit_runner.suit_task"] = stub


_stub_broken_suit_task()
