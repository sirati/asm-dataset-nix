"""Shape translation between the streaming planner's native dataclass
output and its JSON-roundtripped form.

This module is purely about reading the dict/dataclass cells that the
streaming planner emits; no descriptor minting or cycle detection
happens here. See :mod:`.cycle`, :mod:`.descriptors`, :mod:`.plan_total`
for the consumers that build on these helpers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional


# ---------------------------------------------------------------------------
# (hash, name) tuples <-> "<hash>-<name>" strings
# ---------------------------------------------------------------------------


def _coerce_ident(raw: Any) -> Optional[tuple[str, str]]:
    """Normalise a single toolchain ident entry to ``(hash, name)``.

    Accepts:
      * ``(hash, name)`` tuples (native streaming output);
      * ``[hash, name]`` lists (JSON-roundtripped form);
      * ``"<hash>-<name>"`` strings (legacy refcount form).

    Returns ``None`` for anything else so the caller can decide whether
    to log + skip vs raise.
    """
    if isinstance(raw, tuple) and len(raw) == 2:
        h, n = raw
        if isinstance(h, str) and isinstance(n, str):
            return h, n
        return None
    if isinstance(raw, list) and len(raw) == 2:
        h, n = raw
        if isinstance(h, str) and isinstance(n, str):
            return h, n
        return None
    if isinstance(raw, str):
        # ``<hash>-<name>`` -- split on first dash. The hash is a
        # nixbase32 32-char fixed-length prefix, so any earlier dash
        # would corrupt; rely on it being well-formed at the caller.
        if "-" in raw:
            h, n = raw.split("-", 1)
            return h, n
        return None
    return None


def _ident_to_str(ident: tuple[str, str]) -> str:
    """Join ``(hash, name)`` into the legacy ``"<hash>-<name>"`` shape."""
    return f"{ident[0]}-{ident[1]}"


def convert_toolchain_drvs(raw: Iterable[Any]) -> set[str]:
    """Translate the streaming planner's ``set[(hash, name)]`` shape
    into the legacy ``set[str]`` shape expected by
    ``manifest_gen``-era code.

    Entries that fail to coerce are dropped (the caller's contract is
    "best-effort conversion" -- a malformed entry shouldn't sink the
    whole plan; the upstream invariant checker will already have
    surfaced the malformation elsewhere if it matters).
    """
    out: set[str] = set()
    for entry in raw:
        ident = _coerce_ident(entry)
        if ident is None:
            continue
        out.add(_ident_to_str(ident))
    return out


# ---------------------------------------------------------------------------
# Reading dataclass-or-dict cells from the streaming dict
# ---------------------------------------------------------------------------


def _attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``obj.name`` (dataclass) or ``obj[name]`` (dict)."""
    if obj is None:
        return default
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return default


def _template_nodes(template: Any) -> list[Any]:
    """Return ``template.nodes`` as a sequence; accepts dataclass or dict."""
    nodes = _attr_or_key(template, "nodes", []) or []
    return list(nodes)


def _node_field(node: Any, name: str, default: Any = None) -> Any:
    return _attr_or_key(node, name, default)


def _variant_array_fields(arr: Any) -> tuple[int, str, list[str], list[list]]:
    """Return ``(template_id, arch, variants, hashes)`` from a
    VariantArray dataclass or its JSON dict form."""
    template_id = _attr_or_key(arr, "template_id", 0)
    arch = _attr_or_key(arr, "arch", "")
    variants = list(_attr_or_key(arr, "variants", []) or [])
    hashes_raw = _attr_or_key(arr, "hashes", []) or []
    hashes: list[list] = [list(row) if row is not None else [] for row in hashes_raw]
    return int(template_id), str(arch), variants, hashes


def _iter_variant_arrays(
    variant_arrays: Any,
) -> Iterable[tuple[tuple[int, str], Any]]:
    """Yield ``((template_id, arch), VariantArray)`` pairs.

    The streaming planner returns a dict keyed by ``(int, str)``
    tuples. JSON roundtripping can stringify those keys (e.g.
    ``"3|x86_64"``); we accept either by inspecting the key shape.
    """
    if isinstance(variant_arrays, Mapping):
        for key, arr in variant_arrays.items():
            if isinstance(key, tuple) and len(key) == 2:
                yield (int(key[0]), str(key[1])), arr
            elif isinstance(key, str) and "|" in key:
                tid_s, arch = key.split("|", 1)
                yield (int(tid_s), arch), arr
            else:
                # Caller passed an unparseable key -- surface as
                # zero/empty so downstream still works deterministically
                # rather than crashing the whole plan over one bad
                # cell.
                yield (0, str(key)), arr


def _arch_indep_idents_for_binary(
    arch_indep_deps_raw: Any,
    binary: str,
) -> list[tuple[str, str]]:
    """Extract this binary's arch-indep idents from the streaming
    result's ``arch_indep_deps`` field.

    Streaming-native form is ``dict[str, set[(hash, name)]]``. After a
    JSON roundtrip both the outer set->list and inner tuple->list
    coercions apply, so we accept either shape and emit a typed
    ``list[(hash, name)]``. Entries that fail to coerce are dropped --
    same best-effort policy as :func:`convert_toolchain_drvs`.
    """
    if not isinstance(arch_indep_deps_raw, Mapping):
        return []
    bucket = arch_indep_deps_raw.get(binary)
    if bucket is None:
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in bucket:
        ident = _coerce_ident(entry)
        if ident is None or ident in seen:
            continue
        seen.add(ident)
        out.append(ident)
    return out


def _iter_classifications(
    raw: Any,
) -> Iterable[tuple[tuple[int, str], dict[int, str]]]:
    """Same key-shape tolerance as :func:`_iter_variant_arrays`,
    yielding ``((template_id, arch), {node_id: classification})``.

    Node-id keys may be int (native) or str (JSON-roundtripped); we
    coerce to int.
    """
    if not isinstance(raw, Mapping):
        return
    for key, inner in raw.items():
        if isinstance(key, tuple) and len(key) == 2:
            tid, arch = int(key[0]), str(key[1])
        elif isinstance(key, str) and "|" in key:
            tid_s, arch = key.split("|", 1)
            tid = int(tid_s)
        else:
            tid = 0
            arch = str(key)
        normalised: dict[int, str] = {}
        if isinstance(inner, Mapping):
            for nid, cls in inner.items():
                try:
                    normalised[int(nid)] = str(cls)
                except (TypeError, ValueError):
                    continue
        yield (tid, arch), normalised


def _coerce_toolchain_node_ids(raw: Any) -> dict[int, list[int]]:
    """Normalise ``toolchain_node_ids_per_template`` to ``{int: [int, ...]}``.

    The streaming planner emits this as ``dict[int, list[int]]``; a
    JSON roundtrip turns the outer keys into strings. We accept either
    shape and silently drop entries that won't coerce so a malformed
    snapshot never crashes the plan.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[int, list[int]] = {}
    for key, inner in raw.items():
        try:
            tid = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(inner, (list, tuple)):
            continue
        node_ids: list[int] = []
        for nid in inner:
            try:
                node_ids.append(int(nid))
            except (TypeError, ValueError):
                continue
        out[tid] = node_ids
    return out


def _toolchain_idents_by_name(raw: Any) -> dict[str, list[tuple[str, str]]]:
    """Index ``out.toolchain_drvs`` by drv ``name`` for fast role-lookup.

    The cowalk short-circuits toolchain subtrees so ``arr.hashes`` rows
    at toolchain node_ids are empty (E6). Instead, we resolve each
    toolchain TemplateNode's role to one or more ``(hash, name)`` idents
    by matching on the post-hash drv name carried in
    ``out.toolchain_drvs``. Multiple compiler versions can share a
    unified wrapper role (``wrapped-compiler-suit.drv``) so the map
    value is a LIST: every matching ident's task_id gets wired into
    each variant's ``depends_on``. Over-wiring is harmless (the
    variant waits on extra ``build_compilers__*`` tasks that would
    have been built anyway); under-wiring would break the build by
    starting a variant before its compiler is ready.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    if not raw:
        return out
    for entry in raw:
        ident = _coerce_ident(entry)
        if ident is None:
            continue
        out.setdefault(ident[1], []).append(ident)
    return out


def _toolchain_ident_strs(raw: Any) -> frozenset[str]:
    """Project ``out.toolchain_drvs`` to a ``frozenset`` of
    ``"<hash>-<name>"`` strings.

    Replaces the role-name keyed :func:`_toolchain_idents_by_name`
    lookup for the meta-pass toolchain-position check: a MetaTemplate
    position is a toolchain position iff its ident string appears in
    this set. Direct ident match avoids the role-collapse conflation
    that bit the old keyed-by-name path (multiple compilers folding
    onto ``wrapped-compiler-suit.drv``).
    """
    out: set[str] = set()
    if not raw:
        return frozenset()
    for entry in raw:
        ident = _coerce_ident(entry)
        if ident is None:
            continue
        out.add(_ident_to_str(ident))
    return frozenset(out)
