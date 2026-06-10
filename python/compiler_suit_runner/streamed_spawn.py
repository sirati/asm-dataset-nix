"""Wire protocol for the dependency_graph -> build streamed-spawn handoff.

This module is the pure codec for streaming :class:`Phase4Descriptor`
batches from the dependency_graph worker to the primary, replacing the
removed pickle-file transport (the worker used to write
``_dependency_graph.pkl`` / publish the pickle bytes; the primary's
``on_phase_end`` read it back).  Now the handoff rides the framework's
custom-message channel instead:

* **Producer** -- the dependency_graph worker.  As it plans, it feeds
  descriptors into a :class:`SpawnBatchEncoder` and sends every returned
  message via ``Task.send_message`` on topic :data:`SPAWN_TOPIC`
  (``"dependency_graph_spawn"``).  After the last descriptor it sends
  the encoder's :meth:`SpawnBatchEncoder.flush` remainder (if any) on
  the same topic, then exactly one
  :meth:`SpawnBatchEncoder.encode_summary` message on topic
  :data:`SUMMARY_TOPIC` (``"dependency_graph_summary"``) carrying the
  authoritative total/batch counts so the consumer can detect loss.

* **Consumer** -- ``SuitTask.custom_message_handler`` on the primary
  (the worker's messages are relayed through its secondary).  It calls
  :func:`decode_spawn_message` on each payload, spawns the decoded
  descriptors, and reconciles the summary's totals against what it saw.
  On any :class:`ValueError` the handler RAISES and relies on the
  framework poison-cap to surface the malformed message -- which is why
  every validation error here carries a precise, operator-readable
  message.

Wire format (JSON, utf-8, compact ``(",", ":")`` separators):

* batch:   ``{"v":1,"kind":"spawn_batch","seq":<0-based batch index>,
  "descriptors":[<encoded descriptor>,...]}``
* summary: ``{"v":1,"kind":"summary","total":<int>,"batches":<int>,
  "counters":{"<kind>":<int>,...}}``

Every emitted message is guaranteed to be at most ``max_message_bytes``
(default :data:`DEFAULT_MAX_MESSAGE_BYTES` = 100 KiB, mirroring the
framework's ``CUSTOM_MESSAGE_MAX_BYTES`` cap).  The encoder does exact
byte accounting -- it measures the real serialized envelope for the
batch's actual ``seq`` plus the per-descriptor encoded sizes and comma
separators -- so a returned message never exceeds the cap by even one
byte.  At production scale (~67,000 descriptors of ~622 bytes each)
this yields on the order of 340-700 messages.

This module is deliberately dependency-free: pure stdlib plus the
:class:`Phase4Descriptor` dataclass.  No dynamic_runner imports, no
I/O, no logging -- it is a codec, nothing more.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from .dependency_graph_planner.descriptors import Phase4Descriptor

SPAWN_TOPIC = "dependency_graph_spawn"
SUMMARY_TOPIC = "dependency_graph_summary"
WIRE_VERSION = 1
DEFAULT_MAX_MESSAGE_BYTES = 100 * 1024  # mirrors framework CUSTOM_MESSAGE_MAX_BYTES
MAX_BATCH_DESCRIPTORS = 200

_JSON_SEPARATORS = (",", ":")

_DESCRIPTOR_FIELDS = (
    "kind",
    "task_id",
    "name",
    "payload",
    "depends_on",
    "build_compilers_depends_on",
    "priority_hint",
)

_SPAWN_BATCH_FIELDS = ("v", "kind", "seq", "descriptors")
_SUMMARY_FIELDS = ("v", "kind", "total", "batches", "counters")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _check_keys(obj: dict, expected: tuple[str, ...], what: str) -> None:
    """ValueError unless ``obj``'s key set is exactly ``expected``."""
    keys = set(obj)
    missing = [k for k in expected if k not in keys]
    if missing:
        raise ValueError(f"{what} missing field(s): {', '.join(missing)}")
    extra = sorted(keys - set(expected))
    if extra:
        raise ValueError(f"{what} has unknown field(s): {', '.join(extra)}")


def _check_str(value: object, field: str, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"{what} field {field!r} must be a str, "
            f"got {type(value).__name__}"
        )
    return value


def _check_int(
    value: object, field: str, what: str, *, non_negative: bool = True
) -> int:
    # bool is an int subclass; a wire-level True/False is a malformation.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{what} field {field!r} must be an int, "
            f"got {type(value).__name__}"
        )
    if non_negative and value < 0:
        raise ValueError(
            f"{what} field {field!r} must be non-negative, got {value}"
        )
    return value


def _check_str_list(value: object, field: str, what: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(
            f"{what} field {field!r} must be a list, "
            f"got {type(value).__name__}"
        )
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"{what} field {field!r}[{i}] must be a str, "
                f"got {type(item).__name__}"
            )
    return tuple(value)


# ---------------------------------------------------------------------------
# Descriptor codec
# ---------------------------------------------------------------------------


def encode_descriptor(d: Phase4Descriptor) -> dict:
    """Encode one :class:`Phase4Descriptor` as a JSON-safe dict.

    Tuples become lists; the payload dict rides as-is (its values are
    JSON-native already -- they ride task payloads today).
    """
    return {
        "kind": d.kind,
        "task_id": d.task_id,
        "name": d.name,
        "payload": d.payload,
        "depends_on": list(d.depends_on),
        "build_compilers_depends_on": list(d.build_compilers_depends_on),
        "priority_hint": d.priority_hint,
    }


def decode_descriptor(obj: dict) -> Phase4Descriptor:
    """Strictly decode :func:`encode_descriptor` output.

    ValueError on missing fields, wrong types, or unknown extra keys.
    """
    what = "descriptor"
    if not isinstance(obj, dict):
        raise ValueError(
            f"{what} must be a dict, got {type(obj).__name__}"
        )
    _check_keys(obj, _DESCRIPTOR_FIELDS, what)
    kind = _check_str(obj["kind"], "kind", what)
    task_id = _check_str(obj["task_id"], "task_id", what)
    name = _check_str(obj["name"], "name", what)
    payload = obj["payload"]
    if not isinstance(payload, dict):
        raise ValueError(
            f"{what} field 'payload' must be a dict, "
            f"got {type(payload).__name__}"
        )
    for key in payload:
        if not isinstance(key, str):
            raise ValueError(
                f"{what} field 'payload' has a non-str key: {key!r}"
            )
    depends_on = _check_str_list(obj["depends_on"], "depends_on", what)
    build_compilers_depends_on = _check_str_list(
        obj["build_compilers_depends_on"], "build_compilers_depends_on", what
    )
    priority_hint = _check_int(obj["priority_hint"], "priority_hint", what)
    return Phase4Descriptor(
        kind=kind,
        task_id=task_id,
        name=name,
        payload=payload,
        depends_on=depends_on,
        build_compilers_depends_on=build_compilers_depends_on,
        priority_hint=priority_hint,
    )


# ---------------------------------------------------------------------------
# Batch encoder
# ---------------------------------------------------------------------------


def _batch_envelope(seq: int) -> str:
    """The serialized spawn_batch envelope with an EMPTY descriptor list.

    ``descriptors`` is serialized last, so the string ends in ``]}`` --
    the emitter splices the comma-joined descriptor encodings in front
    of that tail.  Measuring this real envelope (per actual ``seq``, so
    digit growth is accounted for) is what makes the size accounting
    exact rather than approximate.
    """
    return json.dumps(
        {
            "v": WIRE_VERSION,
            "kind": "spawn_batch",
            "seq": seq,
            "descriptors": [],
        },
        separators=_JSON_SEPARATORS,
        ensure_ascii=True,
    )


def _encode_descriptor_str(descriptor: Phase4Descriptor) -> str:
    """Compact-serialize one descriptor; wrap payload TypeError.

    ``ensure_ascii=True`` keeps the output pure ASCII, so ``len(str)``
    IS the encoded byte length.
    """
    try:
        return json.dumps(
            encode_descriptor(descriptor),
            separators=_JSON_SEPARATORS,
            ensure_ascii=True,
        )
    except TypeError as exc:
        raise ValueError(
            f"descriptor {descriptor.task_id!r} has a non-JSON-serializable "
            f"payload: {exc}"
        ) from exc


class SpawnBatchEncoder:
    """Accumulates descriptors into size- and count-capped batch messages.

    :meth:`add` returns a fully-encoded ``spawn_batch`` message whenever
    adding a descriptor triggers a flush, else ``None``:

    * count cap -- the returned batch contains exactly
      ``max_batch_descriptors`` descriptors INCLUDING the one just
      added;
    * size cap -- the returned batch does NOT include the new
      descriptor (it starts the next batch instead).

    ``seq`` grows 0, 1, 2, ... across the encoder's lifetime;
    :meth:`flush` emits the remainder under the current seq.  A
    descriptor that cannot fit alone in a single message raises
    ValueError (before any state mutation, so previously-added
    descriptors are preserved and still flushable).
    """

    def __init__(
        self,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        max_batch_descriptors: int = MAX_BATCH_DESCRIPTORS,
    ) -> None:
        if max_message_bytes <= 0:
            raise ValueError("max_message_bytes must be positive")
        if max_batch_descriptors <= 0:
            raise ValueError("max_batch_descriptors must be positive")
        self._max_message_bytes = max_message_bytes
        self._max_batch_descriptors = max_batch_descriptors
        self._parts: list[str] = []  # encoded descriptors of pending batch
        self._pending_bytes = 0  # sum(len(parts)) + len(parts)-1 commas
        self._batches_emitted = 0  # == seq of the pending batch
        self._descriptors_emitted = 0

    @property
    def descriptors_emitted(self) -> int:
        """Descriptors emitted in RETURNED batches (incl. flush())."""
        return self._descriptors_emitted

    @property
    def batches_emitted(self) -> int:
        return self._batches_emitted

    def _emit(self) -> bytes:
        """Serialize and return the pending batch; advance seq."""
        envelope = _batch_envelope(self._batches_emitted)
        # envelope ends in ']}' (descriptors is the last key); splice
        # the pre-encoded descriptor strings in front of that tail so
        # the emitted bytes match the accounting exactly.
        message = envelope[:-2] + ",".join(self._parts) + "]}"
        self._descriptors_emitted += len(self._parts)
        self._batches_emitted += 1
        self._parts = []
        self._pending_bytes = 0
        return message.encode("utf-8")

    def add(self, descriptor: Phase4Descriptor) -> bytes | None:
        """Add one descriptor; return a flushed batch message or None."""
        part = _encode_descriptor_str(descriptor)
        if not self._parts:
            # Descriptor starts the pending batch (seq = batches_emitted).
            if len(_batch_envelope(self._batches_emitted)) + len(part) > (
                self._max_message_bytes
            ):
                raise ValueError(
                    f"descriptor {descriptor.task_id!r} cannot fit in a "
                    f"single message: {len(part)} encoded bytes vs "
                    f"max_message_bytes={self._max_message_bytes}"
                )
            self._parts.append(part)
            self._pending_bytes = len(part)
            if len(self._parts) >= self._max_batch_descriptors:
                return self._emit()
            return None
        projected = (
            len(_batch_envelope(self._batches_emitted))
            + self._pending_bytes
            + 1  # the comma before the new descriptor
            + len(part)
        )
        if projected <= self._max_message_bytes:
            self._parts.append(part)
            self._pending_bytes += 1 + len(part)
            if len(self._parts) >= self._max_batch_descriptors:
                return self._emit()
            return None
        # Size-triggered flush: the new descriptor starts the NEXT
        # batch (seq + 1) -- verify it can fit there at all BEFORE
        # mutating state, so a ValueError leaves the pending batch
        # intact.
        next_seq = self._batches_emitted + 1
        if len(_batch_envelope(next_seq)) + len(part) > (
            self._max_message_bytes
        ):
            raise ValueError(
                f"descriptor {descriptor.task_id!r} cannot fit in a "
                f"single message: {len(part)} encoded bytes vs "
                f"max_message_bytes={self._max_message_bytes}"
            )
        message = self._emit()
        self._parts.append(part)
        self._pending_bytes = len(part)
        return message

    def flush(self) -> bytes | None:
        """Emit the remainder batch, or None if nothing is pending."""
        if not self._parts:
            return None
        return self._emit()

    def encode_summary(self, counters: Mapping[str, int]) -> bytes:
        """Encode the terminal summary message from internal totals.

        ``counters`` maps descriptor kind -> count; keys must be str,
        values int.  ValueError if descriptors are still pending
        (unflushed -- the summary would undercount).
        """
        if self._parts:
            raise ValueError(
                f"encode_summary called with {len(self._parts)} pending "
                "descriptor(s); call flush() first"
            )
        what = "summary counters"
        validated: dict[str, int] = {}
        for key, value in counters.items():
            if not isinstance(key, str):
                raise ValueError(f"{what} has a non-str key: {key!r}")
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"{what} value for {key!r} must be an int, "
                    f"got {type(value).__name__}"
                )
            validated[key] = value
        message = json.dumps(
            {
                "v": WIRE_VERSION,
                "kind": "summary",
                "total": self._descriptors_emitted,
                "batches": self._batches_emitted,
                "counters": validated,
            },
            separators=_JSON_SEPARATORS,
            ensure_ascii=True,
        ).encode("utf-8")
        if len(message) > self._max_message_bytes:
            raise ValueError(
                f"summary message is {len(message)} bytes, exceeding "
                f"max_message_bytes={self._max_message_bytes}"
            )
        return message


# ---------------------------------------------------------------------------
# Message decoder
# ---------------------------------------------------------------------------


def decode_spawn_message(data: bytes) -> dict:
    """Strictly decode one wire message (batch or summary).

    Returns ``{"kind": "spawn_batch", "seq": int,
    "descriptors": [Phase4Descriptor, ...]}`` or ``{"kind": "summary",
    "total": int, "batches": int, "counters": {str: int}}``.
    ValueError on ANY malformation.
    """
    what = "spawn message"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{what} is not valid utf-8: {exc}") from exc
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(
            f"{what} must be a JSON object, got {type(obj).__name__}"
        )
    if "v" not in obj:
        raise ValueError(f"{what} missing field(s): v")
    version = obj["v"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(
            f"{what} field 'v' must be an int, "
            f"got {type(version).__name__}"
        )
    if version != WIRE_VERSION:
        raise ValueError(
            f"{what} has unsupported wire version {version} "
            f"(expected {WIRE_VERSION})"
        )
    if "kind" not in obj:
        raise ValueError(f"{what} missing field(s): kind")
    kind = obj["kind"]
    if not isinstance(kind, str):
        raise ValueError(
            f"{what} field 'kind' must be a str, got {type(kind).__name__}"
        )
    if kind == "spawn_batch":
        what = "spawn_batch message"
        _check_keys(obj, _SPAWN_BATCH_FIELDS, what)
        seq = _check_int(obj["seq"], "seq", what)
        raw_descriptors = obj["descriptors"]
        if not isinstance(raw_descriptors, list):
            raise ValueError(
                f"{what} field 'descriptors' must be a list, "
                f"got {type(raw_descriptors).__name__}"
            )
        descriptors = []
        for i, item in enumerate(raw_descriptors):
            try:
                descriptors.append(decode_descriptor(item))
            except ValueError as exc:
                raise ValueError(
                    f"{what} descriptors[{i}]: {exc}"
                ) from exc
        return {"kind": "spawn_batch", "seq": seq, "descriptors": descriptors}
    if kind == "summary":
        what = "summary message"
        _check_keys(obj, _SUMMARY_FIELDS, what)
        total = _check_int(obj["total"], "total", what)
        batches = _check_int(obj["batches"], "batches", what)
        raw_counters = obj["counters"]
        if not isinstance(raw_counters, dict):
            raise ValueError(
                f"{what} field 'counters' must be a dict, "
                f"got {type(raw_counters).__name__}"
            )
        counters: dict[str, int] = {}
        for key, value in raw_counters.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"{what} field 'counters' has a non-str key: {key!r}"
                )
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"{what} field 'counters' value for {key!r} must be "
                    f"an int, got {type(value).__name__}"
                )
            counters[key] = value
        return {
            "kind": "summary",
            "total": total,
            "batches": batches,
            "counters": counters,
        }
    raise ValueError(f"spawn message has unknown kind {kind!r}")
