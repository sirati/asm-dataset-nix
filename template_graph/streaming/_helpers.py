"""Free-function helpers used across the streaming subpackage.

Currently hosts only ``drv_name_full`` (drv-name passthrough). The
cross-arch sharing classifier (``_classify_cross_arch_sharing``) and
its ``_ARCH_FAMILIES`` table moved to
``template_graph.cowalk.cross_arch`` (their primary consumer); both
are re-exported here for back-compat.

The arch-triple table, triple/version extractors, and the
revisit-diff classifier moved to ``template_graph.cowalk._helpers``
(those are cowalk-internal — see that module's docstring). The
streaming package re-exports them for back-compat.
"""

from __future__ import annotations


def drv_name_full(name: str) -> str:
    """Identity passthrough for the post-hash drv name. Kept as a
    function so callers can switch in another extractor."""
    return name


# Back-compat re-exports: external code may still do
# ``from template_graph.streaming._helpers import _ARCH_FAMILIES`` or
# ``_classify_cross_arch_sharing``. Keep both names resolvable here.
from template_graph.cowalk.cross_arch import (  # noqa: E402,F401
    _ARCH_FAMILIES,
    _classify_cross_arch_sharing,
)
