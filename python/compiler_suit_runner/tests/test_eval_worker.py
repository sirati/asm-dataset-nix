"""Unit tests for ``compiler_suit_runner.workers.eval_worker``.

The nix subprocess and the BroadcastSender are always stubbed; tests
run in milliseconds and never touch the network or the local
/nix/store.

Note: ``eval_worker`` is a pure library module. The subprocess
entry point lives in :func:`workers.build_worker.main`, which
sniffs ``task.payload`` and dispatches matrix_eval tasks into
:func:`run_eval_task`. The framework-entry tests are in
``test_build_worker.py`` accordingly.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from compiler_suit_runner.peer_replication import BroadcastResult
from compiler_suit_runner.workers import eval_worker
from compiler_suit_runner.workers.eval_worker import (
    MATRIX_EVAL_ITEM_CLASS,
    parse_payload,
    run_eval_task,
    sample_suffix_attrs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    binary: str = "hello",
    sys_name: str = "x86_64-linux",
    archs: Optional[list[str]] = None,
    suffixes: Optional[list[str]] = None,
    variant_sample: Optional[int] = None,
    variant_seed: Optional[str] = None,
) -> dict:
    """Build a matrix_eval payload matching make_matrix_eval_header."""
    archs = archs if archs is not None else ["x86_64"]
    suffixes = suffixes if suffixes is not None else ["O0", "O2"]
    payload: dict[str, Any] = {
        "binary": binary,
        "sys": sys_name,
        "archs": archs,
        "suffixes": suffixes,
        "attr": f"dataset.{sys_name}.{binary}",
    }
    if variant_sample is not None:
        payload["variant_sample"] = variant_sample
    if variant_seed is not None:
        payload["variant_seed"] = variant_seed
    return payload


class _EvalJobsStub:
    """Stub matching the ``RunSubprocess`` signature.

    Dispatches based on argv[0]:

    * ``nix-eval-jobs``: emits one JSONL line per (suffix → drvPath)
      mapping from ``drv_map[arch]``. ``arch`` is parsed out of the
      ``--flake`` argument (form ``<attr>.<arch>``).
    * ``nix``: emits the JSON dict from ``meta_map[arch]`` for
      ``_meta.<sys>.<binary>.<arch>`` lookups.
    * ``nix-store --query --requisites``: synthesises the closure as
      ``[<drv>, <drv>-input] for drv in argv[3:]`` so the export step
      has paths to act on. Override the synthesis by setting
      ``requisites_for[<drv>] = [<closure>, ...]``.
    * ``nix-store --export``: returns the synthetic byte payload
      ``b"NIX_EXPORT:" + b",".join(argv[2:])`` so tests can decode
      what closure was exported. Set ``export_fail=True`` to make this
      arm return rc=1.

    Set ``fail_archs`` to make a given arch's nix-eval-jobs invocation
    return rc=1 with the supplied stderr; used to test error
    propagation.
    """

    def __init__(
        self,
        *,
        drv_map: Optional[dict[str, dict[str, str]]] = None,
        meta_map: Optional[dict[str, dict[str, dict]]] = None,
        fail_archs: Optional[dict[str, str]] = None,
        requisites_for: Optional[dict[str, list[str]]] = None,
        export_fail: bool = False,
        requisites_fail: bool = False,
    ) -> None:
        self.drv_map = drv_map or {}
        self.meta_map = meta_map or {}
        self.fail_archs = fail_archs or {}
        self.requisites_for = requisites_for or {}
        self.export_fail = export_fail
        self.requisites_fail = requisites_fail
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        if argv and argv[0] == "nix-eval-jobs":
            # --flake <attr>.<arch> is at index 2
            flake_arg = argv[2]
            arch = flake_arg.rsplit(".", 1)[-1]
            if arch in self.fail_archs:
                return b"", self.fail_archs[arch].encode(), 1
            drvs = self.drv_map.get(arch, {})
            # Honour the --select intersectAttrs filter so the stub
            # behaves like nix-eval-jobs would: only emit drvs for
            # suffixes the worker actually asked for. argv[4] is the
            # select expression "m: intersectAttrs { "S1" = null; ... } m".
            select_expr = argv[4] if len(argv) > 4 else ""
            import re as _re
            requested = set(_re.findall(r'"([^"]+)" = null;', select_expr))
            lines = []
            for suffix, drv in sorted(drvs.items()):
                if requested and suffix not in requested:
                    continue
                lines.append(
                    json.dumps({"attr": suffix, "drvPath": drv})
                )
            return ("\n".join(lines) + "\n").encode(), b"", 0
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            if self.requisites_fail:
                return b"", b"requisites stub forced failure", 1
            seeds = argv[3:]
            closure: list[str] = []
            for seed in seeds:
                if seed in self.requisites_for:
                    closure.extend(self.requisites_for[seed])
                else:
                    # Default: the drv itself + a synthesised input dep so
                    # the export step always sees a non-trivial closure.
                    closure.append(seed)
                    closure.append(seed + "-input")
            return ("\n".join(closure) + "\n").encode(), b"", 0
        if argv[:2] == ["nix-store", "--export"]:
            if self.export_fail:
                return b"", b"export stub forced failure", 1
            payload = b"NIX_EXPORT:" + ",".join(argv[2:]).encode()
            return payload, b"", 0
        if argv and argv[0] == "nix":
            # nix eval --json <flake>#_meta.<sys>.<binary>.<arch>
            for piece in argv:
                if "#_meta." in piece:
                    arch = piece.rsplit(".", 1)[-1]
                    meta = self.meta_map.get(arch, {})
                    return json.dumps(meta).encode(), b"", 0
            return b"{}", b"", 0
        return b"", f"unexpected argv: {argv!r}\n".encode(), 1


def _make_broadcast_sender(
    *,
    success_count: int = 2,
    fail_count: int = 0,
    failed_peers: tuple[str, ...] = (),
) -> MagicMock:
    """Return a MagicMock typed as BroadcastSender.

    enqueue_broadcast returns a deterministic counter-based id and
    records the call args; wait_for_completion returns a synthetic
    BroadcastResult unless the id is in ``timeout_ids``.
    """
    sender = MagicMock()
    counter = {"n": 0}

    def _enqueue(path: str, size: int, item_class=None) -> str:
        counter["n"] += 1
        return f"bid-{counter['n']:04d}"

    def _wait(broadcast_id: str, timeout=None):
        return BroadcastResult(
            broadcast_id=broadcast_id,
            success_count=success_count,
            fail_count=fail_count,
            failed_peers=failed_peers,
        )

    sender.enqueue_broadcast.side_effect = _enqueue
    sender.wait_for_completion.side_effect = _wait
    return sender


# ---------------------------------------------------------------------------
# parse_payload
# ---------------------------------------------------------------------------


def test_parse_payload_happy() -> None:
    payload = _make_payload(
        archs=["x86_64", "aarch64"],
        suffixes=["O0", "O2"],
        variant_sample=64,
        variant_seed="seed-1",
    )
    parsed = parse_payload(payload)
    assert parsed["binary"] == "hello"
    assert parsed["sys"] == "x86_64-linux"
    assert parsed["archs"] == ["x86_64", "aarch64"]
    assert parsed["suffixes"] == ["O0", "O2"]
    assert parsed["attr"] == "dataset.x86_64-linux.hello"
    assert parsed["variant_sample"] == 64
    assert parsed["variant_seed"] == "seed-1"


def test_parse_payload_omits_optional_fields() -> None:
    payload = _make_payload()
    parsed = parse_payload(payload)
    assert parsed["variant_sample"] is None
    assert parsed["variant_seed"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("binary"),
        lambda p: p.update(sys=None),
        lambda p: p.update(archs="not-a-list"),
        lambda p: p.update(suffixes=[42]),
        lambda p: p.update(attr=""),
        lambda p: p.update(variant_sample="64"),
    ],
)
def test_parse_payload_rejects_bad_shape(mutate) -> None:
    payload = _make_payload()
    mutate(payload)
    with pytest.raises(ValueError):
        parse_payload(payload)


def test_parse_payload_rejects_unsafe_suffix() -> None:
    payload = _make_payload(suffixes=["O0", "evil; rm -rf /"])
    with pytest.raises(ValueError, match="unsafe suffix"):
        parse_payload(payload)


def test_parse_payload_item_class_constant() -> None:
    # Sanity: the module's exported item-class string matches what
    # manifest_gen uses on the wire.
    assert MATRIX_EVAL_ITEM_CLASS == "matrix_eval"


# ---------------------------------------------------------------------------
# sample_suffix_attrs determinism
# ---------------------------------------------------------------------------


def _build_meta(n: int) -> dict[str, dict]:
    """Build a meta dict with n suffixes spanning 2 compilers × 2 opts."""
    meta: dict[str, dict] = {}
    compilers = ["gcc15", "clang19"]
    opts = ["O0", "O2"]
    for i in range(n):
        suffix = f"S{i:03d}"
        meta[suffix] = {
            "compiler": compilers[i % len(compilers)],
            "optimization": opts[(i // 2) % len(opts)],
            "label": suffix,
        }
    return meta


def test_sample_suffix_attrs_determinism_same_seed() -> None:
    """Same seed → identical samples across two independent calls.

    This is the contract the Phase 0 protocol leans on: a submitter
    that fixes ``variant_seed`` knows every secondary will sample
    the same subset without coordinating.
    """
    meta = _build_meta(40)
    a = sample_suffix_attrs(
        meta, arch="x86_64", sample_size=3, seed="seed-1"
    )
    b = sample_suffix_attrs(
        meta, arch="x86_64", sample_size=3, seed="seed-1"
    )
    assert set(a.keys()) == set(b.keys())


def test_sample_suffix_attrs_different_seed_reshuffles() -> None:
    """Different seed → at least one group ends up with a different
    sample. (We can't assert "all different" because tiny groups
    have few permutations; but we expect the overall multiset to
    shift.)"""
    meta = _build_meta(40)
    a = sample_suffix_attrs(
        meta, arch="x86_64", sample_size=3, seed="seed-1"
    )
    b = sample_suffix_attrs(
        meta, arch="x86_64", sample_size=3, seed="seed-2"
    )
    assert set(a.keys()) != set(b.keys())


def test_sample_suffix_attrs_passthrough_on_zero() -> None:
    meta = _build_meta(10)
    out = sample_suffix_attrs(meta, arch="x86_64", sample_size=0, seed="x")
    assert out == meta


def test_sample_suffix_attrs_passthrough_unstructured_entries() -> None:
    """Entries lacking compiler / optimization metadata are kept as-is."""
    meta = {
        "good": {"compiler": "gcc15", "optimization": "O0"},
        "bad": {"compiler": "gcc15"},  # missing optimization
        "non-dict": "scalar",
    }
    out = sample_suffix_attrs(
        meta, arch="x86_64", sample_size=10, seed="s"
    )
    assert "bad" in out
    assert "non-dict" in out
    assert "good" in out


# ---------------------------------------------------------------------------
# run_eval_task — happy path
# ---------------------------------------------------------------------------


def test_run_eval_task_happy_path(tmp_path: pathlib.Path) -> None:
    """Mock subprocess + BroadcastSender; verify the full flow.

    Asserts: (1) nix-eval-jobs is called once per arch with the
    correct argv; (2) BroadcastSender.enqueue_broadcast is called
    once per (arch × suffix); (3) the per-binary ``<binary>.nix-archive``
    is written; (4) the return value carries the variants + variant_drvs
    + broadcast results; (5) the legacy ``<binary>/manifest.json``
    marker is NOT emitted (hard cutover post-A3).
    """
    payload = _make_payload(
        archs=["x86_64", "aarch64"],
        suffixes=["O0", "O2"],
    )
    runner = _EvalJobsStub(
        drv_map={
            "x86_64": {
                "O0": "/nix/store/aaa-hello-x86_64-O0.drv",
                "O2": "/nix/store/bbb-hello-x86_64-O2.drv",
            },
            "aarch64": {
                "O0": "/nix/store/ccc-hello-aarch64-O0.drv",
                "O2": "/nix/store/ddd-hello-aarch64-O2.drv",
            },
        },
    )
    sender = _make_broadcast_sender()

    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 123.5,
    )

    # nix-eval-jobs invoked once per arch (no _meta call when
    # variant_sample is not supplied).
    eval_calls = [c for c in runner.calls if c and c[0] == "nix-eval-jobs"]
    assert len(eval_calls) == 2
    flake_args = sorted(c[2] for c in eval_calls)
    assert flake_args == [
        ".#dataset.x86_64-linux.hello.aarch64",
        ".#dataset.x86_64-linux.hello.x86_64",
    ]
    # The --select expression must use intersectAttrs with both suffixes.
    for c in eval_calls:
        select_expr = c[4]
        assert "intersectAttrs" in select_expr
        assert '"O0"' in select_expr
        assert '"O2"' in select_expr

    # BroadcastSender.enqueue_broadcast: one call per (arch × suffix) = 4.
    assert sender.enqueue_broadcast.call_count == 4
    broadcast_drvs = [
        call.args[0] for call in sender.enqueue_broadcast.call_args_list
    ]
    assert set(broadcast_drvs) == {
        "/nix/store/aaa-hello-x86_64-O0.drv",
        "/nix/store/bbb-hello-x86_64-O2.drv",
        "/nix/store/ccc-hello-aarch64-O0.drv",
        "/nix/store/ddd-hello-aarch64-O2.drv",
    }
    # item_class kwarg is the broadcast namespace marker.
    for call in sender.enqueue_broadcast.call_args_list:
        assert call.kwargs["item_class"] == "matrix_eval_drv"

    # wait_for_completion: one call per broadcast id.
    assert sender.wait_for_completion.call_count == 4

    # nix-store --query --requisites called once with all kept drvs
    # (sorted, deduped); nix-store --export called once with the
    # synthesised closure.
    req_calls = [
        c for c in runner.calls
        if c[:3] == ["nix-store", "--query", "--requisites"]
    ]
    assert len(req_calls) == 1
    # Seeds are sorted (the worker sorts the kept drv set).
    assert req_calls[0][3:] == [
        "/nix/store/aaa-hello-x86_64-O0.drv",
        "/nix/store/bbb-hello-x86_64-O2.drv",
        "/nix/store/ccc-hello-aarch64-O0.drv",
        "/nix/store/ddd-hello-aarch64-O2.drv",
    ]
    export_calls = [
        c for c in runner.calls if c[:2] == ["nix-store", "--export"]
    ]
    assert len(export_calls) == 1
    # Closure passed to --export is what the stub synthesised from the
    # requisites argv (each seed → seed + seed-input).
    assert "/nix/store/aaa-hello-x86_64-O0.drv" in export_calls[0]
    assert "/nix/store/aaa-hello-x86_64-O0.drv-input" in export_calls[0]

    # Archive written with the stub's synthetic export payload.
    archive = tmp_path / "hello.nix-archive"
    assert archive.exists()
    assert archive.stat().st_size > 0
    assert archive.read_bytes().startswith(b"NIX_EXPORT:")

    # Returned dict carries the in-memory summary (no sidecar JSON is
    # written — the dependency_graph_worker derives variant_lookup from
    # the imported .drv paths via parse_variant_path).
    assert result["binary"] == "hello"
    assert result["sys"] == "x86_64-linux"
    assert result["produced_at"] == 123.5
    # variant_drvs is the sorted+deduped list — the dependency_graph
    # worker's primary discovery path keys off this field.
    assert result["variant_drvs"] == [
        "/nix/store/aaa-hello-x86_64-O0.drv",
        "/nix/store/bbb-hello-x86_64-O2.drv",
        "/nix/store/ccc-hello-aarch64-O0.drv",
        "/nix/store/ddd-hello-aarch64-O2.drv",
    ]
    assert len(result["variants"]) == 4
    # Sorted by (arch, suffix) order in our stub.
    labels = sorted(v["label"] for v in result["variants"])
    assert labels == [
        "hello__aarch64__O0",
        "hello__aarch64__O2",
        "hello__x86_64__O0",
        "hello__x86_64__O2",
    ]
    assert all(v["drv"].startswith("/nix/store/") for v in result["variants"])
    # Broadcast results recorded.
    assert len(result["broadcasts"]) == 4
    for b in result["broadcasts"]:
        assert b["status"] == "ok"
        assert b["success_count"] == 2
        assert b["fail_count"] == 0

    # Legacy per-binary manifest.json marker is NOT emitted (hard cutover).
    assert not (tmp_path / "hello" / "manifest.json").exists()
    assert not (tmp_path / "hello").exists()
    # No companion JSON file is emitted alongside the archive — the
    # archive itself is the resume marker, and the dependency_graph
    # worker derives variant_lookup from the imported .drv paths.
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert siblings == ["hello.nix-archive"], siblings


# ---------------------------------------------------------------------------
# run_eval_task — resume short-circuit
# ---------------------------------------------------------------------------


def test_run_eval_task_resume_short_circuits(tmp_path: pathlib.Path) -> None:
    """If the archive exists and is non-empty, short-circuit eval +
    broadcast + export and return a minimal ``{"resumed": True}``
    dict. The archive presence is the matrix_eval-done signal; the
    dependency_graph_worker derives variant_lookup from the imported
    .drv paths so no in-memory variant list is needed.
    """
    payload = _make_payload()
    archive = tmp_path / "hello.nix-archive"
    archive.write_bytes(b"NIX_EXPORT:previous-run")

    runner = _EvalJobsStub()
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner,
    )

    # Returned dict carries the resumed marker and the per-binary
    # identity fields; no variants enumerated.
    assert result.get("resumed") is True
    assert result["binary"] == "hello"
    assert result["sys"] == "x86_64-linux"
    assert result["variant_drvs"] == []
    assert result["variants"] == []
    assert result["broadcasts"] == []
    # No subprocess calls — no eval, no requisites query, no export.
    assert runner.calls == []
    # No broadcasts.
    sender.enqueue_broadcast.assert_not_called()
    sender.wait_for_completion.assert_not_called()


def test_run_eval_task_empty_archive_re_runs(
    tmp_path: pathlib.Path,
) -> None:
    """An empty archive file (zero bytes) doesn't satisfy the resume
    check — we re-run rather than wedge on a half-written export.
    """
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    archive = tmp_path / "hello.nix-archive"
    archive.write_bytes(b"")  # empty

    runner = _EvalJobsStub(
        drv_map={"x86_64": {"O0": "/nix/store/aaa.drv"}},
    )
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 42.0,
    )
    # nix-eval-jobs ran (resume short-circuit did not fire).
    assert any(c and c[0] == "nix-eval-jobs" for c in runner.calls)
    # Archive rewritten via the export path.
    assert archive.stat().st_size > 0
    assert result["produced_at"] == 42.0
    assert result.get("resumed") is not True
    assert result["variant_drvs"] == ["/nix/store/aaa.drv"]


# (test_run_eval_task_corrupt_sidecar_falls_through was deleted — the
# eval_worker no longer writes the JSON sidecar, so a corrupt-sidecar
# fall-through case is structurally obsolete. The archive itself is
# now the sole resume marker; coverage for a non-empty archive is in
# test_run_eval_task_resume_short_circuits and for an empty/missing
# archive in test_run_eval_task_empty_archive_re_runs.)


# ---------------------------------------------------------------------------
# Determinism end-to-end: same seed → identical drv list
# ---------------------------------------------------------------------------


def test_run_eval_task_determinism_same_seed(
    tmp_path: pathlib.Path,
) -> None:
    """Two runs with the same ``variant_seed`` produce identical
    variants[] lists. Each run uses a fresh out_dir so no resume
    short-circuit hides drift between runs.
    """
    payload = _make_payload(
        archs=["x86_64"],
        suffixes=[f"S{i:03d}" for i in range(20)],
        variant_sample=4,
        variant_seed="deterministic-seed",
    )
    # Meta has 20 suffixes spanning 2 compilers × 2 opts.
    meta = {
        f"S{i:03d}": {
            "compiler": ["gcc15", "clang19"][i % 2],
            "optimization": ["O0", "O2"][(i // 2) % 2],
        }
        for i in range(20)
    }
    drv_table = {f"S{i:03d}": f"/nix/store/x{i:03d}.drv" for i in range(20)}

    def _run(out: pathlib.Path) -> dict:
        runner = _EvalJobsStub(
            drv_map={"x86_64": drv_table},
            meta_map={"x86_64": meta},
        )
        sender = _make_broadcast_sender()
        return run_eval_task(
            payload, out, sender, run_subprocess=runner, now=lambda: 1.0,
        )

    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    a = _run(out_a)
    b = _run(out_b)
    labels_a = sorted(v["label"] for v in a["variants"])
    labels_b = sorted(v["label"] for v in b["variants"])
    drvs_a = sorted(v["drv"] for v in a["variants"])
    drvs_b = sorted(v["drv"] for v in b["variants"])
    assert labels_a == labels_b
    assert drvs_a == drvs_b


def test_run_eval_task_determinism_different_seed(
    tmp_path: pathlib.Path,
) -> None:
    """Different seed → different drv list (with enough samples to
    avoid the small-group degenerate case)."""
    suffixes = [f"S{i:03d}" for i in range(40)]
    meta = {
        f"S{i:03d}": {
            "compiler": ["gcc15", "clang19"][i % 2],
            "optimization": ["O0", "O2"][(i // 2) % 2],
        }
        for i in range(40)
    }
    drv_table = {f"S{i:03d}": f"/nix/store/x{i:03d}.drv" for i in range(40)}

    def _run(out: pathlib.Path, seed: str) -> dict:
        payload = _make_payload(
            archs=["x86_64"],
            suffixes=suffixes,
            variant_sample=2,
            variant_seed=seed,
        )
        runner = _EvalJobsStub(
            drv_map={"x86_64": drv_table},
            meta_map={"x86_64": meta},
        )
        sender = _make_broadcast_sender()
        return run_eval_task(
            payload, out, sender, run_subprocess=runner, now=lambda: 1.0,
        )

    a = _run(tmp_path / "a", "seed-1")
    b = _run(tmp_path / "b", "seed-2")
    labels_a = sorted(v["label"] for v in a["variants"])
    labels_b = sorted(v["label"] for v in b["variants"])
    assert labels_a != labels_b


# ---------------------------------------------------------------------------
# Failure propagation — Errored (retry-pass eligible)
# ---------------------------------------------------------------------------


def test_run_eval_task_subprocess_failure_raises(
    tmp_path: pathlib.Path,
) -> None:
    """nix-eval-jobs returning rc=1 raises RuntimeError so the
    framework harness surfaces ErrorType::Errored.

    The exception message must include enough context (which arch /
    attr) for operator triage.
    """
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    runner = _EvalJobsStub(
        fail_archs={"x86_64": "eval-time OOM: out of memory"},
    )
    sender = _make_broadcast_sender()

    with pytest.raises(RuntimeError) as exc_info:
        run_eval_task(payload, tmp_path, sender, run_subprocess=runner)

    msg = str(exc_info.value)
    assert "nix-eval-jobs" in msg
    assert "dataset.x86_64-linux.hello.x86_64" in msg
    assert "rc=1" in msg
    assert "eval-time OOM" in msg
    # No archive on failure — re-execution must re-eval.
    assert not (tmp_path / "hello.nix-archive").exists()
    # No broadcasts fired (we failed before the first arch's
    # drvs were extracted).
    sender.enqueue_broadcast.assert_not_called()


def test_run_eval_task_bad_payload_raises_valueerror(
    tmp_path: pathlib.Path,
) -> None:
    """A malformed payload raises ValueError, NOT RuntimeError —
    payload errors are programmer bugs, not transient failures.
    """
    payload = _make_payload()
    payload["archs"] = "not-a-list"
    sender = _make_broadcast_sender()
    runner = _EvalJobsStub()
    with pytest.raises(ValueError):
        run_eval_task(payload, tmp_path, sender, run_subprocess=runner)


def test_run_eval_task_broadcast_timeout_recorded(
    tmp_path: pathlib.Path,
) -> None:
    """A broadcast timeout is non-fatal: we record ``status=timeout``
    on the returned summary and continue. The flood-fill protocol is
    best-effort.
    """
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    runner = _EvalJobsStub(
        drv_map={"x86_64": {"O0": "/nix/store/aaa.drv"}}
    )
    sender = MagicMock()
    sender.enqueue_broadcast.return_value = "bid-1"
    sender.wait_for_completion.return_value = None  # timeout

    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )
    assert len(result["broadcasts"]) == 1
    assert result["broadcasts"][0]["status"] == "timeout"
    # Archive still persisted.
    assert (tmp_path / "hello.nix-archive").exists()


# ---------------------------------------------------------------------------
# Archive export — failure + atomicity
# ---------------------------------------------------------------------------


def test_run_eval_task_requisites_failure_raises(
    tmp_path: pathlib.Path,
) -> None:
    """``nix-store --query --requisites`` returning rc=1 surfaces as
    RuntimeError — retry-pass eligible. No archive on failure.
    """
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    runner = _EvalJobsStub(
        drv_map={"x86_64": {"O0": "/nix/store/aaa.drv"}},
        requisites_fail=True,
    )
    sender = _make_broadcast_sender()
    with pytest.raises(RuntimeError, match="nix-store --query --requisites"):
        run_eval_task(payload, tmp_path, sender, run_subprocess=runner)
    assert not (tmp_path / "hello.nix-archive").exists()


def test_run_eval_task_export_failure_raises_and_cleans_up(
    tmp_path: pathlib.Path,
) -> None:
    """``nix-store --export`` rc=1 surfaces as RuntimeError; the
    temporary archive file is removed so re-execution sees a clean
    out_dir.
    """
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    runner = _EvalJobsStub(
        drv_map={"x86_64": {"O0": "/nix/store/aaa.drv"}},
        export_fail=True,
    )
    sender = _make_broadcast_sender()
    with pytest.raises(RuntimeError, match="nix-store --export"):
        run_eval_task(payload, tmp_path, sender, run_subprocess=runner)
    # No archive (export failed). No stale .tmp lingering either.
    assert not (tmp_path / "hello.nix-archive").exists()
    assert not (tmp_path / "hello.nix-archive.tmp").exists()


def test_run_eval_task_empty_kept_drvs_writes_empty_archive(
    tmp_path: pathlib.Path,
) -> None:
    """All archs returning zero drvs produces an empty archive (zero
    bytes) with ``variant_drvs=[]`` — a valid "binary has no
    plannable variants" outcome the dependency_graph worker will
    skip.
    """
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    # Empty drv map for the requested arch → arch_drvs ends up empty.
    runner = _EvalJobsStub(drv_map={"x86_64": {}})
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )
    assert result["variant_drvs"] == []
    assert result["variants"] == []
    # Archive exists (zero bytes).
    archive = tmp_path / "hello.nix-archive"
    assert archive.exists()
    assert archive.stat().st_size == 0
    # No requisites / export subprocesses fired (no kept drvs).
    assert not any(
        c[:3] == ["nix-store", "--query", "--requisites"]
        for c in runner.calls
    )
    assert not any(c[:2] == ["nix-store", "--export"] for c in runner.calls)


# ---------------------------------------------------------------------------
# Sampling integration with _meta lookup
# ---------------------------------------------------------------------------


def test_run_eval_task_invokes_meta_eval_when_sampling(
    tmp_path: pathlib.Path,
) -> None:
    """When ``variant_sample`` is set we MUST query ``_meta`` to drive
    the sampling. Without that the worker would have no compiler/opt
    metadata to group by.
    """
    payload = _make_payload(
        archs=["x86_64"],
        suffixes=["A", "B", "C", "D"],
        variant_sample=1,
        variant_seed="s",
    )
    meta = {
        "A": {"compiler": "gcc15", "optimization": "O0"},
        "B": {"compiler": "gcc15", "optimization": "O2"},
        "C": {"compiler": "clang19", "optimization": "O0"},
        "D": {"compiler": "clang19", "optimization": "O2"},
    }
    drv_table = {k: f"/nix/store/{k}.drv" for k in meta}
    runner = _EvalJobsStub(
        drv_map={"x86_64": drv_table}, meta_map={"x86_64": meta},
    )
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )

    # _meta lookup happened.
    meta_calls = [
        c for c in runner.calls
        if c and c[0] == "nix" and any("_meta." in p for p in c)
    ]
    assert len(meta_calls) == 1
    # 4 groups × sample_size=1 → 4 variants kept.
    assert len(result["variants"]) == 4
    # Each broadcast enqueued once.
    assert sender.enqueue_broadcast.call_count == 4


def test_run_eval_task_skips_meta_when_no_sampling(
    tmp_path: pathlib.Path,
) -> None:
    """No ``variant_sample`` → no _meta call; we use the full
    ``suffixes`` list straight from the payload.
    """
    payload = _make_payload(archs=["x86_64"], suffixes=["O0", "O2"])
    runner = _EvalJobsStub(
        drv_map={
            "x86_64": {"O0": "/nix/store/a.drv", "O2": "/nix/store/b.drv"}
        }
    )
    sender = _make_broadcast_sender()
    run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )
    # Zero _meta lookups — we never invoked `nix eval`.
    meta_calls = [
        c for c in runner.calls if c and c[0] == "nix"
    ]
    assert meta_calls == []


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_run_subprocess_uses_real_subprocess() -> None:
    """The default ``run_subprocess`` is a real subprocess invocation,
    not a no-op. Calling /usr/bin/true (or /bin/true) demonstrates the
    callable shape; tests do NOT exercise this with nix-eval-jobs
    against the real flake.
    """
    # We only assert the shape: stdout bytes, stderr bytes, rc int.
    stdout, stderr, rc = eval_worker._default_run_subprocess(
        ["/usr/bin/env", "true"]
    )
    assert isinstance(stdout, bytes)
    assert isinstance(stderr, bytes)
    assert isinstance(rc, int)
    assert rc == 0


# ---------------------------------------------------------------------------
# read_peer_push_urls — public helper consumed by build_worker.main
# ---------------------------------------------------------------------------


def test_read_peer_push_urls_returns_empty_when_shared_fs_none() -> None:
    """The shared-fs unset path is the offline / unit-test default."""
    assert eval_worker.read_peer_push_urls(None, "sec1") == []


def test_read_peer_push_urls_enumerates_peers(
    tmp_path: pathlib.Path,
) -> None:
    """Each ``peers/<id>.json`` file maps to ``http://host:harmonia+offset``
    via :func:`peer_push.push_port_for`. The self-id is excluded."""
    from compiler_suit_runner import peer_cache
    from compiler_suit_runner.peer_push import push_port_for

    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()
    # Write two peers and one self-record; expect only the two non-self
    # entries in the URL list, sorted alphabetically by file name
    # (peer_cache.list_peers sorts iterdir).
    for sid, host, port in [
        ("self", "host-self", 5000),
        ("peer-a", "host-a", 5001),
        ("peer-b", "host-b", 5002),
    ]:
        info = peer_cache.PeerInfo(
            secondary_id=sid, hostname=host, port=port,
            public_key="cluster:pub",
        )
        peer_cache.announce_self(tmp_path, info)

    urls = eval_worker.read_peer_push_urls(tmp_path, "self")
    assert f"http://host-a:{push_port_for(5001)}" in urls
    assert f"http://host-b:{push_port_for(5002)}" in urls
    assert not any("host-self" in u for u in urls)


def test_read_peer_push_urls_private_alias_is_public_helper() -> None:
    """``_read_peer_push_urls`` is a back-compat alias for the public
    :func:`read_peer_push_urls` (kept while in-tree callers migrate)."""
    assert eval_worker._read_peer_push_urls is eval_worker.read_peer_push_urls


def test_module_exports_drop_main() -> None:
    """``eval_worker`` is now a pure library module: ``main`` is gone,
    the framework entry lives in :mod:`build_worker`. The ``__all__``
    list must reflect that (no ``main`` symbol)."""
    assert "main" not in eval_worker.__all__
    assert not hasattr(eval_worker, "main")
    # The library surface stays exported.
    for name in (
        "MATRIX_EVAL_ITEM_CLASS",
        "RunSubprocess",
        "parse_payload",
        "read_peer_push_urls",
        "run_eval_task",
        "sample_suffix_attrs",
    ):
        assert name in eval_worker.__all__, name
