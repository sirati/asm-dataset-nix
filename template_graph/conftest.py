"""Pytest path setup — make ``template_graph`` importable when this
package's tests are invoked directly (``pytest template_graph/tests/``)
without first installing the package.
"""

import sys
from pathlib import Path

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
