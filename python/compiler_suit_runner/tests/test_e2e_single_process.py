"""End-to-end single-process tests for the compiler-suit runner.

NOTE: this file is being replaced in unit 8.8 of the dynamic_runner
phase-8 migration; the old in-process dispatch loop and barrier-flag
contracts that these tests exercised have been removed.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "test_e2e_single_process.py is rewritten in dynamic_runner phase 8.8;"
    " old in-process dispatch + barrier-flag contracts are obsolete",
    allow_module_level=True,
)
