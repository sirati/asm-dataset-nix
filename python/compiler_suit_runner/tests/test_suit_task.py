"""Unit tests for ``compiler_suit_runner.suit_task``.

NOTE: this file is being replaced in unit 8.8 of the dynamic_runner
phase-8 migration; its old PhaseCounter / barrier-flag / in-process
dispatch surface no longer exists. Skip the whole module at import
time until 8.8 lands new fixtures.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "test_suit_task.py is rewritten in dynamic_runner phase 8.8;"
    " old PhaseCounter / barrier-flag tests are obsolete",
    allow_module_level=True,
)
