"""``python -m compiler_suit_runner.workers.dependency_graph_worker`` shim.

Delegates to :func:`compiler_suit_runner.workers.dependency_graph_worker.cli.main`.
The runner framework's ``suit_task`` invokes the package as a module
(``python -m ...``) — this entry point keeps that invocation working
after the split into a sub-package.
"""

from __future__ import annotations

import sys

from .cli import main


if __name__ == "__main__":
    sys.exit(main())
