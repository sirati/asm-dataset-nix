"""Pytest path setup — make ``template_graph`` importable when these
tests are invoked without first installing the sibling package. The
production planner imports template_graph for arch-indep terminal
classification; tests need the same path.
"""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
