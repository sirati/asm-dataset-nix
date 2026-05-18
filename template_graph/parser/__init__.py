"""Parser subpackage: post-hash drv-name analysis.

Currently exposes role-extraction helpers used to align matrix-variant
template positions. Future submodules will cover other parse-time
concerns (triple/version diff classification, etc.).
"""

from template_graph.parser.role import (
    _COMPILER_WRAPPER_ROLE_RE,
    _EXT_RE,
    _QUALIFIER_RE,
    _STDENV_RE,
    _TRIPLE_ABI,
    _TRIPLE_ARCH,
    _TRIPLE_OS,
    _TRIPLE_RE,
    _TRIPLE_VENDOR,
    _UNIFIED_COMPILER_WRAPPER_ROLE,
    _is_compiler_wrapper_role,
    _is_stdenv_role,
    _strip_version,
    drv_role,
)

__all__ = [
    "drv_role",
    "_is_stdenv_role",
    "_is_compiler_wrapper_role",
    "_strip_version",
    "_STDENV_RE",
    "_COMPILER_WRAPPER_ROLE_RE",
    "_UNIFIED_COMPILER_WRAPPER_ROLE",
    "_TRIPLE_ARCH",
    "_TRIPLE_VENDOR",
    "_TRIPLE_OS",
    "_TRIPLE_ABI",
    "_TRIPLE_RE",
    "_QUALIFIER_RE",
    "_EXT_RE",
]
