"""Descriptor -> ManifestHeader glue and pickle reader.

These live on the primary-side spawn path: the watcher reads
``_dependency_graph.pkl`` off disk via :func:`load_phase4_descriptors`
and hands the typed descriptors to :func:`headers_from_descriptors`
before calling ``primary_handle.spawn_tasks``.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import pickle
from collections.abc import Iterable
from typing import Union

from .descriptors import Phase4Descriptor

_LOG = logging.getLogger(__name__)

# Magic + format-version stamped into the ``_dependency_graph.pkl``
# payload by the writer (see
# ``compiler_suit_runner.workers.dependency_graph_worker.output``).
# Defined here -- on the reader side -- because the writer already
# imports :class:`Phase4Descriptor` from this package, so making the
# worker the source of truth would form a cycle. The writer imports
# these names back from this module to keep both sides in lockstep.
PHASE4_PICKLE_MAGIC = "csr.dependency_graph.phase4.v1"
PHASE4_PICKLE_FORMAT_VERSION = 1


class DependencyGraphPickleError(RuntimeError):
    """Raised when the dependency_graph pickle is malformed,
    has a wrong magic, or carries an unknown format_version."""


def load_phase4_descriptors(
    pkl_path: pathlib.Path,
) -> tuple[list[Phase4Descriptor], dict[str, Union[int, float, str]]]:
    """Load and validate ``_dependency_graph.pkl``.

    Returns a ``(descriptors, summary)`` pair. Hard-fails with
    :class:`DependencyGraphPickleError` on any shape mismatch (bad
    magic, unknown format_version, non-dict payload, non-list
    descriptors). No JSON fallback.
    """
    try:
        with open(pkl_path, "rb") as fh:
            payload = pickle.load(fh)
    except (OSError, pickle.UnpicklingError) as exc:
        raise DependencyGraphPickleError(
            f"failed to read {pkl_path}: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise DependencyGraphPickleError(
            f"{pkl_path}: expected dict payload, got "
            f"{type(payload).__name__}",
        )
    magic = payload.get("format")
    if magic != PHASE4_PICKLE_MAGIC:
        raise DependencyGraphPickleError(
            f"{pkl_path}: unexpected format magic {magic!r}, "
            f"expected {PHASE4_PICKLE_MAGIC!r}",
        )
    version = payload.get("format_version")
    if version != PHASE4_PICKLE_FORMAT_VERSION:
        raise DependencyGraphPickleError(
            f"{pkl_path}: unknown format_version {version!r}, "
            f"expected {PHASE4_PICKLE_FORMAT_VERSION!r}",
        )
    if "descriptors" not in payload:
        raise DependencyGraphPickleError(
            f"{pkl_path}: missing required key 'descriptors'",
        )
    if "summary" not in payload:
        raise DependencyGraphPickleError(
            f"{pkl_path}: missing required key 'summary'",
        )
    descriptors = payload["descriptors"]
    summary = payload["summary"]
    if not isinstance(descriptors, list):
        raise DependencyGraphPickleError(
            f"{pkl_path}: 'descriptors' must be a list, got "
            f"{type(descriptors).__name__}",
        )
    if not isinstance(summary, dict):
        raise DependencyGraphPickleError(
            f"{pkl_path}: 'summary' must be a dict, got "
            f"{type(summary).__name__}",
        )
    return list(descriptors), dict(summary)


def headers_from_descriptors(descriptors: Iterable[Phase4Descriptor]) -> list:
    """Translate :class:`Phase4Descriptor` records into
    :class:`manifest_gen.ManifestHeader` instances ready for
    ``primary_handle.spawn_tasks``.

    Late-imports ``manifest_gen`` so this module stays loadable in
    environments where ``manifest_gen`` is mid-rename or absent (unit
    tests for the planner that don't need the constructors).

    Per-descriptor mapping:

    * ``build_common_dep`` -> :class:`ManifestHeader` with the planner's
      payload threaded straight through. The ``attr`` field is
      synthesised from the ident when the planner didn't carry one.
    * ``build_variant`` -> :class:`ManifestHeader` with the variant
      payload threaded through; ``attr`` is reconstructed from
      ``dataset.<sys>.<pkg>.<arch>.<label>`` if missing so the
      downstream build worker can resolve it the same way the legacy
      submit-time path does.

    Unknown ``kind`` values are skipped with no error so a future
    descriptor type added upstream doesn't crash older primaries; the
    spawn-side log surfaces the gap as a "no headers spawned" line.
    """
    from compiler_suit_runner.manifest_gen import ManifestHeader  # noqa: PLC0415

    # ``priority_hint`` was added on the framework side at the same
    # time as the planner-side field (plan §E7). Detect support via
    # dataclass-field introspection so an older ``ManifestHeader``
    # checked out alongside a newer planner degrades gracefully: the
    # hint is dropped and the descriptor's payload still carries the
    # information for downstream observers. The probe is cheap (one
    # ``dataclasses.fields`` call per ``headers_from_descriptors``
    # invocation, not per descriptor).
    header_field_names = {f.name for f in dataclasses.fields(ManifestHeader)}
    supports_priority = "priority_hint" in header_field_names

    headers: list = []
    for d in descriptors:
        if d.kind == "build_common_dep":
            payload = dict(d.payload)
            payload.setdefault(
                "attr", payload.get("drv") or payload.get("ident", ""),
            )
            headers.append(_build_header(
                ManifestHeader,
                item_class="build_common_dep",
                descriptor=d,
                payload=payload,
                supports_priority=supports_priority,
            ))
            continue
        if d.kind == "build_variant":
            payload = dict(d.payload)
            sys_name = payload.get("sys", "")
            pkg = payload.get("pkg", "")
            arch = payload.get("arch", "")
            label = payload.get("label", "")
            if "attr" not in payload and sys_name and pkg and arch and label:
                payload["attr"] = f"dataset.{sys_name}.{pkg}.{arch}.{label}"
            headers.append(_build_header(
                ManifestHeader,
                item_class="build_variant",
                descriptor=d,
                payload=payload,
                supports_priority=supports_priority,
            ))
            continue
        # Unknown kind -- skip silently; caller logs the gap.
    return headers


def _build_header(
    header_cls,
    *,
    item_class: str,
    descriptor: Phase4Descriptor,
    payload: dict,
    supports_priority: bool,
):
    """Mint a ManifestHeader; thread ``priority_hint`` when supported.

    Falls back to omitting the kwarg (and emitting a one-shot debug
    log line per process) when an older ``ManifestHeader`` predates
    plan §E7. The descriptor's hint is still observable via
    ``Phase4Descriptor.priority_hint`` for callers that bypass the
    framework wrapper.
    """
    kwargs = {
        "item_class": item_class,
        "name": descriptor.name,
        "size": 0,
        "payload": payload,
        "task_id": descriptor.task_id,
        "task_depends_on": tuple(descriptor.depends_on),
    }
    if descriptor.priority_hint:
        if supports_priority:
            kwargs["priority_hint"] = descriptor.priority_hint
        elif not getattr(_build_header, "_warned", False):
            _LOG.debug(
                "ManifestHeader has no 'priority_hint' field; dropping "
                "hint=%d for task %r (will not warn again this run)",
                descriptor.priority_hint, descriptor.task_id,
            )
            _build_header._warned = True  # type: ignore[attr-defined]
    return header_cls(**kwargs)


def variant_label_key(drv_path_or_name: str) -> str:
    """Strip the ``/nix/store/<hash>-`` prefix and ``.drv`` suffix.

    Useful for callers that have raw drv paths and want to build the
    ``(arch, label)`` lookup key without re-parsing the variant
    filename themselves. Mirrors the post-hash naming the streaming
    planner uses for ``VariantArray.variants``.
    """
    name = pathlib.Path(drv_path_or_name).name
    if name.endswith(".drv"):
        name = name[:-4]
    if "-" in name and len(name.split("-", 1)[0]) == 32:
        # Looks like a nix store basename; strip the leading hash.
        name = name.split("-", 1)[1]
    return name
