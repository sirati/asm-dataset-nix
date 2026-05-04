"""Module entry: forwards to :func:`ssh_debug_runner.cli.main`."""

from __future__ import annotations

import sys

from .cli import main


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
