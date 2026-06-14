"""Unit tests for :mod:`compiler_suit_runner.streamed_spawn`.

Covers the pure wire codec (encode/decode descriptor,
:class:`SpawnBatchEncoder` count-cap / size-cap / flush / summary
semantics, the full :func:`decode_spawn_message` malformation matrix)
plus the no-framework consumer-pipeline integration test: the
dependency_graph worker streams batches through a fake ``task``, the
bytes ride ``SuitTask.worker_message_listener`` →
``SuitTask.custom_message_handler``, and
``on_phase_end("dependency_graph")`` reconciles the spawned count
against the worker's terminal summary.

KNOWN ACCEPTED Wave-1 LIMITATIONS (documented, deliberately NOT tested
as failures):

* a mid-stream failover replays only the still-Unhandled messages into
  a promoted primary whose counters start fresh, so the on_phase_end
  reconciliation barrier can false-alarm after failover;
* a duplicate ``spawn_batch`` redelivery double-counts (and
  double-spawns; the framework's duplicate_task_hash spawn error keeps
  it loud).
"""

from __future__ import annotations

import dataclasses as _dataclasses
import json
import logging
import pathlib
import re

import pytest

from compiler_suit_runner.dependency_graph_planner import Phase4Descriptor
from compiler_suit_runner.streamed_spawn import (
    DEFAULT_MAX_MESSAGE_BYTES,
    MAX_BATCH_DESCRIPTORS,
    SPAWN_TOPIC,
    SUMMARY_TOPIC,
    WIRE_VERSION,
    SpawnBatchEncoder,
    decode_descriptor,
    decode_spawn_message,
    encode_descriptor,
)


_SYS = "x86_64-linux"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _descriptor(
    i: int = 0,
    *,
    kind: str = "build_variant",
    pad: int = 0,
    depends_on: tuple[str, ...] = (),
    build_compilers_depends_on: tuple[str, ...] = (),
    priority_hint: int = 0,
) -> Phase4Descriptor:
    """A small synthetic descriptor; ``pad`` grows the encoded size."""
    payload: dict = {"pkg": "hello", "arch": "x86_64", "i": i}
    if pad:
        payload["pad"] = "x" * pad
    return Phase4Descriptor(
        kind=kind,
        task_id=f"task_{i}",
        name=f"name_{i}",
        payload=payload,
        depends_on=depends_on,
        build_compilers_depends_on=build_compilers_depends_on,
        priority_hint=priority_hint,
    )


def _part_len(d: Phase4Descriptor) -> int:
    """Encoded byte length of one descriptor (compact ASCII JSON)."""
    return len(json.dumps(
        encode_descriptor(d), separators=(",", ":"), ensure_ascii=True,
    ))


def _msg(obj) -> bytes:
    """Compact-serialize a raw wire object for decode tests."""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _batch(seq: int = 0, descriptors: list | None = None, **extra) -> dict:
    out = {
        "v": WIRE_VERSION,
        "kind": "spawn_batch",
        "seq": seq,
        "descriptors": (
            descriptors if descriptors is not None
            else [encode_descriptor(_descriptor())]
        ),
    }
    out.update(extra)
    return out


def _summary(
    total: int = 1, batches: int = 1, counters: dict | None = None, **extra,
) -> dict:
    out = {
        "v": WIRE_VERSION,
        "kind": "summary",
        "total": total,
        "batches": batches,
        "counters": counters if counters is not None else {"build_variant": 1},
    }
    out.update(extra)
    return out


def _task_ids(message: bytes) -> list[str]:
    return [d.task_id for d in decode_spawn_message(message)["descriptors"]]


# ---------------------------------------------------------------------------
# Descriptor codec roundtrip
# ---------------------------------------------------------------------------


class TestDescriptorCodec:

    def test_roundtrip_equality(self):
        d = Phase4Descriptor(
            kind="build_variant",
            task_id="build_variant__x86_64-linux__hello__lbl",
            name="hello-x86_64-gcc15-O0",
            payload={
                "sys": _SYS,
                "pkg": "hello",
                "nested": {"a": [1, 2, "three", None, False], "b": 1.5},
                "unicode": "münich-ünïcode",
            },
            depends_on=("build_common_dep__glibc.drv", "build_common_dep__zlib.drv"),
            build_compilers_depends_on=("x86_64-linux__x86_64__gcc15",),
            priority_hint=7,
        )
        decoded = decode_descriptor(encode_descriptor(d))
        # Dataclass equality covers every field incl. payload fidelity.
        assert decoded == d
        # JSON turns tuples into lists; the decoder restores tuples.
        assert isinstance(decoded.depends_on, tuple)
        assert isinstance(decoded.build_compilers_depends_on, tuple)

    def test_roundtrip_through_full_json_wire(self):
        """Same fidelity through the actual bytes-on-the-wire path
        (encode → json → utf-8 → decode_spawn_message)."""
        d = _descriptor(3, depends_on=("dep_a",), priority_hint=2)
        enc = SpawnBatchEncoder()
        assert enc.add(d) is None
        message = enc.flush()
        out = decode_spawn_message(message)
        assert out["kind"] == "spawn_batch"
        assert out["seq"] == 0
        assert out["descriptors"] == [d]
        assert all(
            isinstance(item, Phase4Descriptor) for item in out["descriptors"]
        )

    def test_decode_rejects_unknown_extra_key(self):
        obj = encode_descriptor(_descriptor())
        obj["surprise"] = 1
        with pytest.raises(ValueError, match="unknown field\\(s\\): surprise"):
            decode_descriptor(obj)

    def test_decode_rejects_missing_field(self):
        obj = encode_descriptor(_descriptor())
        del obj["payload"]
        with pytest.raises(ValueError, match="missing field\\(s\\): payload"):
            decode_descriptor(obj)

    def test_non_json_serializable_payload_raises_value_error(self):
        bad = _descriptor(0)
        bad = _dataclasses.replace(bad, payload={"x": object()})
        enc = SpawnBatchEncoder()
        with pytest.raises(ValueError, match="non-JSON-serializable"):
            enc.add(bad)


# ---------------------------------------------------------------------------
# SpawnBatchEncoder
# ---------------------------------------------------------------------------


class TestSpawnBatchEncoderCountCap:

    def test_250_small_descriptors_split_200_plus_50(self):
        enc = SpawnBatchEncoder()
        descriptors = [_descriptor(i) for i in range(250)]
        returned: list[tuple[int, bytes]] = []
        for i, d in enumerate(descriptors):
            message = enc.add(d)
            if message is not None:
                returned.append((i, message))
        # Exactly one count-cap flush, triggered BY (and INCLUDING) the
        # 200th descriptor (index 199).
        assert [i for i, _ in returned] == [MAX_BATCH_DESCRIPTORS - 1]
        first = decode_spawn_message(returned[0][1])
        assert first["seq"] == 0
        assert len(first["descriptors"]) == MAX_BATCH_DESCRIPTORS
        assert first["descriptors"][-1].task_id == "task_199"
        remainder = enc.flush()
        rest = decode_spawn_message(remainder)
        assert rest["seq"] == 1
        assert len(rest["descriptors"]) == 50
        # Planner order preserved across the split.
        assert (
            first["descriptors"] + rest["descriptors"] == descriptors
        )
        assert enc.descriptors_emitted == 250
        assert enc.batches_emitted == 2
        summary = decode_spawn_message(
            enc.encode_summary({"build_variant": 250}),
        )
        assert summary == {
            "kind": "summary",
            "total": 250,
            "batches": 2,
            "counters": {"build_variant": 250},
        }


class TestSpawnBatchEncoderSizeCap:

    CAP = 2000

    def test_size_cap_excludes_trigger_and_stays_byte_exact(self):
        """Realistic ~600-byte descriptors against a 2000-byte cap:
        every emitted message is ≤ the cap, the descriptor whose add()
        triggered a size flush is NOT in the returned batch (it starts
        the next one), and the flushed batch was byte-exactly full
        (adding the trigger would have exceeded the cap)."""
        enc = SpawnBatchEncoder(max_message_bytes=self.CAP)
        descriptors = [_descriptor(i, pad=420) for i in range(10)]
        assert all(550 <= _part_len(d) <= 650 for d in descriptors)

        batches: list[bytes] = []
        for d in descriptors:
            message = enc.add(d)
            if message is not None:
                batches.append(message)
                # Size-cap semantics: the trigger is EXCLUDED from the
                # flushed batch...
                assert d.task_id not in _task_ids(message)
                # ...because including it would have blown the cap by
                # exactly the comma + its encoded bytes.
                assert len(message) + 1 + _part_len(d) > self.CAP
        remainder = enc.flush()
        assert remainder is not None
        batches.append(remainder)

        assert len(batches) >= 2  # the cap actually split the stream
        for message in batches:
            assert len(message) <= self.CAP
        # seq grows 0, 1, 2, ... and order is preserved end-to-end.
        decoded = [decode_spawn_message(m) for m in batches]
        assert [b["seq"] for b in decoded] == list(range(len(batches)))
        flat = [d for b in decoded for d in b["descriptors"]]
        assert flat == descriptors
        assert enc.descriptors_emitted == len(descriptors)
        assert enc.batches_emitted == len(batches)

    def test_oversize_single_descriptor_raises_before_mutating_state(self):
        enc = SpawnBatchEncoder(max_message_bytes=self.CAP)
        small = _descriptor(0)
        assert enc.add(small) is None
        huge = _descriptor(1, pad=self.CAP)
        with pytest.raises(ValueError, match="cannot fit in a single message"):
            enc.add(huge)
        # State preserved: the pending small descriptor is still
        # flushable and counters never saw the oversize one.
        message = enc.flush()
        assert _task_ids(message) == ["task_0"]
        assert enc.descriptors_emitted == 1
        assert enc.batches_emitted == 1

    def test_oversize_first_descriptor_raises_with_empty_state(self):
        enc = SpawnBatchEncoder(max_message_bytes=200)
        with pytest.raises(ValueError, match="cannot fit in a single message"):
            enc.add(_descriptor(0, pad=400))
        assert enc.flush() is None
        assert enc.descriptors_emitted == 0
        assert enc.batches_emitted == 0


class TestSpawnBatchEncoderFlushAndSummary:

    def test_empty_encoder_flush_returns_none(self):
        enc = SpawnBatchEncoder()
        assert enc.flush() is None
        assert enc.batches_emitted == 0

    def test_summary_alone_roundtrip(self):
        enc = SpawnBatchEncoder()
        out = decode_spawn_message(enc.encode_summary({}))
        assert out == {
            "kind": "summary", "total": 0, "batches": 0, "counters": {},
        }

    def test_encode_summary_with_pending_descriptors_raises(self):
        enc = SpawnBatchEncoder()
        enc.add(_descriptor(0))
        with pytest.raises(ValueError, match="1 pending"):
            enc.encode_summary({})
        # flush() resolves the pending state; summary then encodes.
        enc.flush()
        out = decode_spawn_message(enc.encode_summary({"build_variant": 1}))
        assert out["total"] == 1
        assert out["batches"] == 1

    def test_encode_summary_rejects_bool_counter_value(self):
        enc = SpawnBatchEncoder()
        with pytest.raises(ValueError, match="must be an int, got bool"):
            enc.encode_summary({"build_variant": True})

    def test_encode_summary_rejects_non_str_counter_key(self):
        enc = SpawnBatchEncoder()
        with pytest.raises(ValueError, match="non-str key"):
            enc.encode_summary({1: 1})

    def test_summary_exceeding_cap_raises(self):
        enc = SpawnBatchEncoder(max_message_bytes=10)
        with pytest.raises(ValueError, match="exceeding"):
            enc.encode_summary({})

    @pytest.mark.parametrize(
        "kwargs", [{"max_message_bytes": 0}, {"max_batch_descriptors": 0}],
    )
    def test_constructor_rejects_non_positive_caps(self, kwargs):
        with pytest.raises(ValueError, match="must be positive"):
            SpawnBatchEncoder(**kwargs)

    def test_default_cap_is_100_kib(self):
        assert DEFAULT_MAX_MESSAGE_BYTES == 100 * 1024


# ---------------------------------------------------------------------------
# decode_spawn_message malformation matrix
# ---------------------------------------------------------------------------


_MALFORMED_CASES: list[tuple[str, bytes, str]] = [
    ("non_utf8", b"\xff\xfe\xfa", "not valid utf-8"),
    ("non_json", b"{nope", "not valid JSON"),
    ("non_object", _msg([1, 2]), "must be a JSON object"),
    ("missing_v", _msg({"kind": "summary"}), r"missing field\(s\): v"),
    ("bool_v", _msg({"v": True, "kind": "summary"}),
     "'v' must be an int, got bool"),
    ("str_v", _msg({"v": "1", "kind": "summary"}), "'v' must be an int"),
    ("wrong_version", _msg({"v": 2, "kind": "summary"}),
     "unsupported wire version 2"),
    ("missing_kind", _msg({"v": 1}), r"missing field\(s\): kind"),
    ("non_str_kind", _msg({"v": 1, "kind": 3}), "'kind' must be a str"),
    ("unknown_kind", _msg({"v": 1, "kind": "pickle"}),
     "unknown kind 'pickle'"),
    # spawn_batch envelope
    ("batch_missing_fields", _msg({"v": 1, "kind": "spawn_batch"}),
     r"missing field\(s\): seq, descriptors"),
    ("batch_extra_field", _msg(_batch(extra=1)),
     r"unknown field\(s\): extra"),
    ("batch_bool_seq", _msg(_batch(seq=True)),
     "'seq' must be an int, got bool"),
    ("batch_negative_seq", _msg(_batch(seq=-1)),
     "'seq' must be non-negative"),
    ("batch_descriptors_not_list", _msg(_batch(descriptors={})),
     "'descriptors' must be a list"),
    ("batch_descriptor_not_dict", _msg(_batch(descriptors=[1])),
     r"descriptors\[0\].*must be a dict"),
    ("batch_descriptor_missing_field",
     _msg(_batch(descriptors=[{"kind": "build_variant"}])),
     r"descriptors\[0\].*missing field\(s\)"),
    ("batch_descriptor_extra_field",
     _msg(_batch(descriptors=[
         {**encode_descriptor(_descriptor()), "bogus": 1},
     ])),
     r"descriptors\[0\].*unknown field\(s\): bogus"),
    ("batch_descriptor_non_str_task_id",
     _msg(_batch(descriptors=[
         {**encode_descriptor(_descriptor()), "task_id": 5},
     ])),
     "'task_id' must be a str, got int"),
    ("batch_descriptor_payload_not_dict",
     _msg(_batch(descriptors=[
         {**encode_descriptor(_descriptor()), "payload": []},
     ])),
     "'payload' must be a dict"),
    ("batch_descriptor_depends_on_not_list",
     _msg(_batch(descriptors=[
         {**encode_descriptor(_descriptor()), "depends_on": "dep"},
     ])),
     "'depends_on' must be a list"),
    ("batch_descriptor_depends_on_non_str_item",
     _msg(_batch(descriptors=[
         {**encode_descriptor(_descriptor()), "depends_on": [3]},
     ])),
     r"'depends_on'\[0\] must be a str"),
    ("batch_descriptor_bc_depends_on_not_list",
     _msg(_batch(descriptors=[
         {
             **encode_descriptor(_descriptor()),
             "build_compilers_depends_on": 0,
         },
     ])),
     "'build_compilers_depends_on' must be a list"),
    ("batch_descriptor_bool_priority_hint",
     _msg(_batch(descriptors=[
         {**encode_descriptor(_descriptor()), "priority_hint": True},
     ])),
     "'priority_hint' must be an int, got bool"),
    ("batch_descriptor_negative_priority_hint",
     _msg(_batch(descriptors=[
         {**encode_descriptor(_descriptor()), "priority_hint": -3},
     ])),
     "'priority_hint' must be non-negative"),
    # summary envelope
    ("summary_missing_fields", _msg({"v": 1, "kind": "summary"}),
     r"missing field\(s\): total, batches, counters"),
    ("summary_extra_field", _msg(_summary(seq=0)),
     r"unknown field\(s\): seq"),
    ("summary_bool_total", _msg(_summary(total=True)),
     "'total' must be an int, got bool"),
    ("summary_negative_total", _msg(_summary(total=-1)),
     "'total' must be non-negative"),
    ("summary_negative_batches", _msg(_summary(batches=-2)),
     "'batches' must be non-negative"),
    ("summary_counters_not_dict", _msg(_summary(counters=[])),
     "'counters' must be a dict"),
    ("summary_counters_bool_value",
     _msg(_summary(counters={"build_variant": True})),
     "must be an int, got bool"),
    ("summary_counters_str_value",
     _msg(_summary(counters={"build_variant": "5"})),
     "must be an int, got str"),
]


class TestDecodeSpawnMessageMalformations:

    @pytest.mark.parametrize(
        "data, match",
        [pytest.param(data, match, id=name)
         for name, data, match in _MALFORMED_CASES],
    )
    def test_malformed_message_raises_value_error(self, data, match):
        with pytest.raises(ValueError, match=match):
            decode_spawn_message(data)

    def test_valid_batch_and_summary_still_decode(self):
        """Sanity guard for the matrix: the base objects the malformed
        cases are derived from ARE valid."""
        batch = decode_spawn_message(_msg(_batch()))
        assert batch["kind"] == "spawn_batch"
        assert isinstance(batch["descriptors"][0], Phase4Descriptor)
        summary = decode_spawn_message(_msg(_summary()))
        assert summary == {
            "kind": "summary", "total": 1, "batches": 1,
            "counters": {"build_variant": 1},
        }


# ---------------------------------------------------------------------------
# End-to-end consumer pipeline (no framework): worker stream → relay →
# primary handler → reconciliation barrier
# ---------------------------------------------------------------------------


class _FakeSecondaryHandle:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bool]] = []

    def send_to_primary(
        self, topic: str, data: bytes, important: bool = False,
    ) -> None:
        self.sent.append((topic, data, important))


class _FakePrimaryHandle:
    def __init__(self) -> None:
        self.calls: list[list] = []

    def spawn_tasks(self, task_infos):
        self.calls.append(list(task_infos))
        return []


class TestConsumerPipelineEndToEnd:
    """The whole consumer side coheres: ``run_dependency_graph_task``
    streams via a fake task, every message is relayed through
    ``worker_message_listener`` (verbatim, important=True) into
    ``custom_message_handler`` on a primary-side ``SuitTask``, and
    ``on_phase_end("dependency_graph")`` reconciles green.

    KNOWN Wave-1 limitation (NOT asserted as a failure here): redelivering
    a spawn_batch to the same primary would double-count, and a failover
    into a fresh-count promoted primary false-alarms the barrier.
    """

    _TC_AGG = "/nix/store/zzzz-toolchains.drv"
    _MATRIX_AGG = "/nix/store/" + "m" * 32 + "-matrix-hello.drv"

    def _planned_descriptors(self) -> list[Phase4Descriptor]:
        label = "hello__x86_64__gcc15-O0-baseline-default-san-off-march-default"
        return [
            Phase4Descriptor(
                kind="build_common_dep",
                task_id="build_common_dep__glibc.drv",
                name="common_dep__glibc",
                payload={"drv": "/nix/store/x-glibc.drv", "label": "glibc"},
                depends_on=(),
            ),
            Phase4Descriptor(
                kind="build_variant",
                task_id=f"build_variant__{_SYS}__hello__{label}",
                name=label,
                payload={
                    "sys": _SYS,
                    "pkg": "hello",
                    "arch": "x86_64",
                    "label": label,
                    "drv": f"/nix/store/v-{label}.drv",
                    "variant_dir": label,
                    "metadata_name": f"{label}.json",
                    "compiler_id": "gcc15",
                },
                depends_on=("build_common_dep__glibc.drv",),
                build_compilers_depends_on=(
                    f"{_SYS}__x86_64__gcc15",
                ),
            ),
        ]

    def _run_worker(self, tmp_path: pathlib.Path, monkeypatch):
        """Run the real worker entry-point with stubbed nix/planner and
        return ``(fake_task, planned_descriptors)``."""
        from compiler_suit_runner.tests.test_dependency_graph_worker import (
            _FakeStreamTask,
            _SubprocessStub,
        )
        from compiler_suit_runner.workers import (
            dependency_graph_worker as dgw,
        )
        from compiler_suit_runner.workers.dependency_graph_worker import (
            archive as _archive_mod,
        )

        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        (matrix_dir / "toolchains.drv.archive").write_bytes(b"fake-toolchain")
        (matrix_dir / "matrix-hello.drv.archive").write_bytes(b"fake")

        suffix = "gcc15-O0-baseline-default-san-off-march-default"
        label = f"hello__x86_64__{suffix}"
        monkeypatch.setattr(
            _archive_mod, "derive_variant_lookup_from_aggregate",
            lambda _agg, **_kw: {
                ("x86_64", label): {
                    "drv": "/nix/store/" + "v" * 32 + "-hello-elf-folder.drv",
                    "arch": "x86_64",
                    "label": label,
                    "suffix": suffix,
                },
            },
        )
        monkeypatch.setattr(
            dgw, "build_sum_drv_multi", lambda **kw: "/nix/store/sum.drv",
        )
        planned = self._planned_descriptors()
        monkeypatch.setattr(dgw, "plan_total", lambda **kw: list(planned))

        task = _FakeStreamTask()
        result = dgw.run_dependency_graph_task(
            task=task,
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            binary="hello",
            matrix_drv=self._MATRIX_AGG,
            run_subprocess=_SubprocessStub(),
        )
        assert result.descriptor_count == len(planned)
        return task, planned

    def test_stream_relay_spawn_and_reconcile(
        self, tmp_path: pathlib.Path, monkeypatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig

        worker_task, planned = self._run_worker(tmp_path, monkeypatch)
        assert worker_task.messages, "worker streamed nothing"
        topics = [t for t, _ in worker_task.messages]
        assert topics == [SPAWN_TOPIC, SUMMARY_TOPIC]

        suit = SuitTask(SuitTaskConfig(
            flake_ref=".",
            sys_name=_SYS,
            shared_fs=tmp_path,
            manifest_dir=tmp_path / "manifests",
            dataset_dir=tmp_path / "dataset",
            peers_dir=tmp_path / "peers",
            run_id="r1",
            secondary_id="primary",
            hostname="host",
        ))
        primary = _FakePrimaryHandle()
        suit._primary_handle = primary

        # Secondary-side relay: forwards verbatim, important=True.
        secondary = _FakeSecondaryHandle()
        for topic, data in worker_task.messages:
            suit.worker_message_listener(0, "dep_graph", topic, data, secondary)
        assert [
            (topic, data) for topic, data, _imp in secondary.sent
        ] == worker_task.messages
        assert all(important for _t, _d, important in secondary.sent)

        # Primary-side handler: spawn batches as they arrive, record the
        # summary, then the on_phase_end barrier reconciles green.
        for topic, data, _important in secondary.sent:
            suit.custom_message_handler("secondary-0", topic, data, True, primary)

        spawned = [ti for call in primary.calls for ti in call]
        assert [ti.task_id for ti in spawned] == [d.task_id for d in planned]
        assert [ti.type_id for ti in spawned] == ["common_dep", "variant"]
        assert all(ti.phase_id == "build" for ti in spawned)
        assert suit._streamed_spawned_count == len(planned)

        with caplog.at_level(logging.INFO, logger="compiler_suit_runner.suit_task"):
            suit.on_phase_end("dependency_graph", completed=1, failed=0)
        assert any(
            "handoff reconciled" in rec.getMessage() for rec in caplog.records
        )

    def test_lost_batch_trips_the_barrier(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """Drop the spawn batch (deliver only the summary): the barrier
        must fail loudly with the pinned mismatch message instead of
        silently under-spawning the build phase."""
        from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig

        worker_task, planned = self._run_worker(tmp_path, monkeypatch)
        suit = SuitTask(SuitTaskConfig(
            flake_ref=".",
            sys_name=_SYS,
            shared_fs=tmp_path,
            manifest_dir=tmp_path / "manifests",
            dataset_dir=tmp_path / "dataset",
            peers_dir=tmp_path / "peers",
            run_id="r1",
            secondary_id="primary",
            hostname="host",
        ))
        suit._primary_handle = _FakePrimaryHandle()
        summary_only = [
            (topic, data) for topic, data in worker_task.messages
            if topic == SUMMARY_TOPIC
        ]
        for topic, data in summary_only:
            suit.custom_message_handler(
                "secondary-0", topic, data, True, suit._primary_handle,
            )
        expected = re.escape(
            "dependency_graph handoff mismatch:"
            f" spawned=0 != total={len(planned)}"
        )
        with pytest.raises(RuntimeError, match=expected):
            suit.on_phase_end("dependency_graph", completed=1, failed=0)
