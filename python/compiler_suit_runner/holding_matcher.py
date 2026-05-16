"""Q3 fulfillability matcher — outpath-blind reinject decision.

The dynamic-runner framework invokes a user-supplied
``fulfillability_matcher`` callback for every task that enters
``TaskState::Unfulfillable``. The callback receives:

* ``failed_task`` — a ``PyTaskInfoView`` exposing the task's metadata,
  including ``.reason`` (the reason string set when the task was
  marked Unfulfillable by the worker that gave up on it).
* ``holdings`` — a mapping ``peer_id -> set[outpath]`` representing the
  union of every peer's currently-known toolchain holdings, as
  observed by the coordinator. Coalesced across concurrent peer
  updates over a 50 ms idle drain window before the matcher fires.

Return value:

* ``True``  — the failed task is *now* fulfillable: at least one peer
  holds the toolchain outpath the task needs. The framework will
  flip the task back to ``Pending`` for re-dispatch.
* ``False`` — leave the task in ``Unfulfillable`` (no peer holds
  the needed path; reason string couldn't be parsed; etc).

Contract with the repair worker (task #71)
==========================================

The reason string format below is part of the wire contract between
the repair worker (which emits Unfulfillable failures) and this
matcher (which parses them). Both sides MUST use
:data:`UNFULFILLABLE_REASON_TEMPLATE`; changes require coordinated
edits in both modules.

Format::

    toolchain outpath=/nix/store/<hash>-<name> dead_holders=[...]

Exactly one ``outpath=`` field per Unfulfillable reason today; the
matcher tolerates zero (returns False) but never assumes more than
one — the regex picks the first match.

Exception handling
==================

If this callable raises, the framework logs a warning and skips that
task (it stays Unfulfillable, will be retried on the next holdings
update). We therefore deliberately keep the matcher defensive but
not paranoid: a malformed reason → return False, not raise.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Set

log = logging.getLogger(__name__)

#: Reason-string template emitted by the repair worker (task #71) when
#: it fails a task into ``TaskState::Unfulfillable``. The matcher
#: parses this exact shape; keep the two sides in sync.
UNFULFILLABLE_REASON_TEMPLATE = (
    "toolchain outpath={outpath} dead_holders={dead_holders}"
)

#: Regex extracting ``/nix/store/<hash>-<name>`` outpaths from a
#: reason string. Greedy up to whitespace/newline so we don't gobble
#: the trailing ``dead_holders=[...]`` field.
_OUTPATH_RE = re.compile(r"outpath=(/nix/store/[a-z0-9]+-[^ \n]+)")


def extract_outpaths_from_unfulfillable_reason(reason: str) -> List[str]:
    """Return every ``/nix/store/...`` outpath embedded in *reason*.

    Public so the repair worker (task #71) and tests can share one
    parser. Returns an empty list for ``None``, empty string, or any
    string without an ``outpath=`` prefix.
    """
    if not reason:
        return []
    return _OUTPATH_RE.findall(reason)


def matcher(failed_task: Any, holdings: Dict[str, Set[str]]) -> bool:
    """Decide whether *failed_task* is now fulfillable.

    See module docstring for the framework contract. Returns True iff
    the outpath named in ``failed_task.reason`` is held by at least
    one peer in ``holdings``.

    Defensive: any of (missing/empty reason, no outpath parsed, empty
    holdings dict) → return False.
    """
    if not holdings:
        return False

    reason = getattr(failed_task, "reason", None)
    if not reason:
        return False

    outpaths = extract_outpaths_from_unfulfillable_reason(reason)
    if not outpaths:
        return False

    # Today there is exactly one outpath per Unfulfillable reason;
    # iterate defensively in case that ever grows.
    for outpath in outpaths:
        for peer_id, held in holdings.items():
            if outpath in held:
                log.info(
                    "fulfillability matcher: task reinject-eligible "
                    "outpath=%s holder=%s (reason=%r)",
                    outpath,
                    peer_id,
                    reason,
                )
                return True

    return False


__all__ = [
    "UNFULFILLABLE_REASON_TEMPLATE",
    "extract_outpaths_from_unfulfillable_reason",
    "matcher",
]
