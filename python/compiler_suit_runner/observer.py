"""Observer-side emitter for Q3 outpath-blind recovery.

This module is the consumer half of the Q3 observer-driven recovery
flow: when a local operator attaches to a live SLURM run via
``--observer-join-from-peer-info-dir`` (or any other observer-join
entrypoint), the framework needs to know which toolchain outpaths
the observer's local store already carries so that
:mod:`compiler_suit_runner.holding_matcher` can re-evaluate stuck
``Unfulfillable`` tasks and auto-reinject the ones whose outpaths
now have a holder.

The wire-up is intentionally one-shot at attach time (the join
announcement). For mid-run holdings refresh — e.g. the observer
builds something locally after attaching and now has new outpaths to
contribute — the framework's mutation API isn't surfaced yet (see
``refresh`` TODO at the bottom of this file). For now,
``RustObserverLateJoiner`` re-announces the latest ``holdings`` on
every ``PrimaryChanged`` event via its
:class:`PeerMeshAnnouncerSender` outbox, which covers the
disaster-recovery case the Q3 wire targets (kill all 4 toolchain
holders, attach observer → primary failover triggers re-announce →
matcher fires → reinject).

The toolchain-drvs source is the per-run JSON written by
:func:`compiler_suit_runner.cli._serialize_preflight_for_cache` (see
``toolchain_drvs_by_pair`` field). The submitter persists it after
preflight; an observer attaching to the same shared-FS run reads it
back and resolves each drv's outpath via ``nix path-info --json``,
keeping only those that are *locally valid* in the observer's store
(an observer with an empty store would announce nothing — the
correct shape: framework treats it as a non-resource-hosting
observer and the matcher won't reinject against it).
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Callable, Optional

from compiler_suit_runner.peer_paths_fetch import (
    RunSubprocess,
    _default_run_subprocess,
    is_path_locally_valid,
)

__all__ = [
    "enumerate_local_toolchain_outpaths",
    "build_observer_late_joiner_kwargs",
    "build_observer_late_joiner",
    "ObserverLateJoinerWrapper",
]

logger = logging.getLogger(__name__)


_NIX_BASE_CMD: tuple[str, ...] = (
    "nix",
    "--extra-experimental-features",
    "nix-command flakes",
)


def _load_toolchain_drvs(
    toolchain_drvs_file: pathlib.Path,
) -> list[str]:
    """Read the persisted toolchain-drvs list, return drv paths.

    The file layout matches what
    :func:`compiler_suit_runner.cli._serialize_preflight_for_cache`
    writes: a JSON object with a ``toolchain_drvs_by_pair`` array of
    ``[arch, compiler, drv_path]`` triples. Returns the deduplicated
    drv-path list; missing file / unparseable JSON / unknown shape
    all degrade silently to ``[]`` so observer attach never fails
    the whole run on a transient FS hiccup.
    """
    if not toolchain_drvs_file.exists():
        logger.debug(
            "observer: toolchain_drvs_file %s missing; announcing nothing",
            toolchain_drvs_file,
        )
        return []
    try:
        payload = json.loads(toolchain_drvs_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "observer: failed to read toolchain_drvs_file %s: %s",
            toolchain_drvs_file, exc,
        )
        return []
    entries = payload.get("toolchain_drvs_by_pair") if isinstance(
        payload, dict
    ) else None
    if not isinstance(entries, list):
        return []
    drvs: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 3:
            continue
        drv = entry[2]
        if not isinstance(drv, str) or not drv.endswith(".drv"):
            continue
        if drv in seen:
            continue
        seen.add(drv)
        drvs.append(drv)
    return drvs


def _resolve_outpath(
    drv: str, *, run_subprocess: RunSubprocess,
) -> Optional[str]:
    """Resolve one drv's outpath via ``nix path-info --json <drv>^*``.

    Single-path ``path-info`` is used here (not the batched variant
    in :mod:`preflight`) because observer attach is a one-shot
    bootstrap and we *want* per-drv isolation: one nix CLI hiccup
    on one drv shouldn't blank the whole holdings announcement.
    The cost (a fork per toolchain) is paid once per observer
    attach, not per job.
    """
    argv = [
        *_NIX_BASE_CMD,
        "path-info",
        "--json",
        f"{drv}^*",
    ]
    try:
        stdout, _stderr, rc = run_subprocess(argv)
    except Exception as exc:  # noqa: BLE001 - log + degrade
        logger.warning(
            "observer: nix path-info failed for %s: %s", drv, exc,
        )
        return None
    if rc != 0:
        return None
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    # Modern shape: {outpath: {"deriver": drv, ...}, ...}
    if isinstance(payload, dict):
        for key, entry in payload.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            if key.endswith(".drv"):
                continue
            deriver = entry.get("deriver")
            if isinstance(deriver, str) and deriver == drv:
                return key
        # Fallback: any non-drv key (single-output drv, common case).
        for key in payload:
            if isinstance(key, str) and not key.endswith(".drv"):
                return key
    # Legacy shape: list of {"path", "deriver", "valid"} entries.
    elif isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if entry.get("valid") is False:
                continue
            path = entry.get("path")
            if isinstance(path, str) and not path.endswith(".drv"):
                return path
    return None


def enumerate_local_toolchain_outpaths(
    toolchain_drvs_file: pathlib.Path,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> list[str]:
    """Return the outpaths from ``toolchain_drvs_file`` that are local.

    Steps:

    1. Read ``toolchain_drvs_file`` (the JSON written at submit time
       by :func:`cli._serialize_preflight_for_cache`). Missing file
       → ``[]``; malformed JSON → ``[]``; subprocess hiccup → drop
       the offending drv (don't fail the whole announcement).
    2. For each drv, run ``nix path-info --json <drv>^*`` to resolve
       its outpath.
    3. Filter to outpaths that pass
       :func:`peer_paths_fetch.is_path_locally_valid`. An observer
       with an empty store yields ``[]`` (framework treats it as a
       non-hosting observer).

    The return order is the persisted order (which is itself sorted
    by (arch, compiler) on submit). Deterministic ordering keeps the
    framework's announce-then-match path observable in tests.
    """
    runner = run_subprocess or _default_run_subprocess
    drvs = _load_toolchain_drvs(toolchain_drvs_file)
    if not drvs:
        return []
    out: list[str] = []
    for drv in drvs:
        outpath = _resolve_outpath(drv, run_subprocess=runner)
        if outpath is None:
            continue
        if not is_path_locally_valid(outpath, run_subprocess=runner):
            continue
        out.append(outpath)
    logger.info(
        "observer: enumerated %d/%d toolchain outpaths locally valid",
        len(out), len(drvs),
    )
    return out


def build_observer_late_joiner_kwargs(
    toolchain_drvs_file: pathlib.Path,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
    extra_kwargs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble the kwargs dict for ``RustObserverLateJoiner(...)``.

    The framework's constructor takes ``holdings: list[str]``. Any
    additional kwargs the framework grows later (e.g.
    ``peer_info_dir`` for cross-handoff state) can be merged via
    ``extra_kwargs`` so callers don't need to import this module to
    add them.

    Returned dict always contains a ``holdings`` key, even when
    empty — the framework treats an empty list as
    "non-resource-hosting observer", which is the intended degraded
    behaviour when the toolchain-drvs file is missing or no
    outpaths are local.
    """
    holdings = enumerate_local_toolchain_outpaths(
        toolchain_drvs_file, run_subprocess=run_subprocess,
    )
    kwargs: dict[str, Any] = {"holdings": holdings}
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return kwargs


class ObserverLateJoinerWrapper:
    """Thin wrapper around the framework's ``RustObserverLateJoiner``.

    Owns the (toolchain_drvs_file, run_subprocess) pair so the
    operator can call :meth:`refresh` after a local build adds new
    outpaths to the observer's store. ``refresh`` re-enumerates and
    stashes the new holdings on ``self.holdings``; framework-side
    mid-run mutation (``PeerResourceHoldingsUpdated``) isn't yet
    exposed in the public API, so for now refresh only takes effect
    on the next ``PrimaryChanged`` re-announce. See TODO in the
    module docstring.

    Tests construct this with ``late_joiner_factory`` injected; in
    production the factory is ``RustObserverLateJoiner`` itself.
    """

    def __init__(
        self,
        toolchain_drvs_file: pathlib.Path,
        *,
        late_joiner_factory: Callable[..., Any],
        run_subprocess: Optional[RunSubprocess] = None,
        extra_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        self._toolchain_drvs_file = toolchain_drvs_file
        self._run_subprocess = run_subprocess
        self._extra_kwargs = dict(extra_kwargs or {})
        self._factory = late_joiner_factory
        kwargs = build_observer_late_joiner_kwargs(
            toolchain_drvs_file,
            run_subprocess=run_subprocess,
            extra_kwargs=self._extra_kwargs,
        )
        self.holdings: list[str] = list(kwargs["holdings"])
        self.late_joiner = late_joiner_factory(**kwargs)

    def refresh(self) -> list[str]:
        """Re-enumerate local outpaths; return the fresh holdings.

        Updates ``self.holdings`` so the next ``PrimaryChanged``
        triggers a re-announce with the new set. The framework
        re-reads ``self.late_joiner.holdings`` on that event (when
        the framework exposes a mid-run setter we'll wire it here;
        until then this is the announce-at-handoff contract).

        TODO(framework): expose ``RustObserverLateJoiner.set_holdings``
        or an equivalent ``PeerResourceHoldingsUpdated`` mutation API
        so mid-run refreshes propagate without waiting for primary
        failover.
        """
        new_holdings = enumerate_local_toolchain_outpaths(
            self._toolchain_drvs_file,
            run_subprocess=self._run_subprocess,
        )
        self.holdings = new_holdings
        # Best-effort: if the framework class grows a setter, use it.
        setter = getattr(self.late_joiner, "set_holdings", None)
        if callable(setter):
            try:
                setter(new_holdings)
            except Exception as exc:  # noqa: BLE001 - defensive
                logger.warning(
                    "observer: set_holdings failed (%s); "
                    "next announce on PrimaryChanged will carry "
                    "the stale list",
                    exc,
                )
        return new_holdings


def build_observer_late_joiner(
    toolchain_drvs_file: pathlib.Path,
    *,
    late_joiner_factory: Optional[Callable[..., Any]] = None,
    run_subprocess: Optional[RunSubprocess] = None,
    extra_kwargs: Optional[dict[str, Any]] = None,
) -> ObserverLateJoinerWrapper:
    """Construct an :class:`ObserverLateJoinerWrapper`.

    ``late_joiner_factory`` defaults to
    ``dynamic_runner.RustObserverLateJoiner`` (imported lazily so
    unit tests don't need the framework on PYTHONPATH). Tests pass
    a fake factory and assert on the recorded ``holdings`` kwarg.
    """
    factory = late_joiner_factory or _default_late_joiner_factory()
    return ObserverLateJoinerWrapper(
        toolchain_drvs_file,
        late_joiner_factory=factory,
        run_subprocess=run_subprocess,
        extra_kwargs=extra_kwargs,
    )


def _default_late_joiner_factory() -> Callable[..., Any]:
    """Lazy import of the framework's late-joiner pyclass.

    Kept out of module import time so this module is importable
    (and unit-testable) in environments where ``dynamic_runner``
    isn't installed.
    """
    from dynamic_runner import (  # type: ignore[import-not-found]
        RustObserverLateJoiner,
    )
    return RustObserverLateJoiner
