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


_DEFAULT_TOOLCHAIN_AGG = "/nix/store/tttt-toolchains.drv"


def _make_payload(
    *,
    binary: str = "hello",
    sys_name: str = "x86_64-linux",
    archs: Optional[list[str]] = None,
    suffixes: Optional[list[str]] = None,
    variant_sample: Optional[int] = None,
    variant_seed: Optional[str] = None,
    toolchain_aggregate_drv: str = _DEFAULT_TOOLCHAIN_AGG,
) -> dict:
    """Build a matrix_eval payload matching make_matrix_eval_header.

    The phase-1 toolchain aggregate drv is now wired into every header
    (Phase B), so every test payload must carry one. We default to a
    deterministic stub path; tests that exercise the aggregate-builder
    monkeypatch :func:`template_graph.make_sum_drv.make_wrapper_drv_from_paths`
    and can assert this path appears as the first inputDrv.
    """
    archs = archs if archs is not None else ["x86_64"]
    suffixes = suffixes if suffixes is not None else ["O0", "O2"]
    payload: dict[str, Any] = {
        "binary": binary,
        "sys": sys_name,
        "archs": archs,
        "suffixes": suffixes,
        "attr": f"dataset.{sys_name}.{binary}",
        "toolchain_aggregate_drv": toolchain_aggregate_drv,
    }
    if variant_sample is not None:
        payload["variant_sample"] = variant_sample
    if variant_seed is not None:
        payload["variant_seed"] = variant_seed
    return payload


class _EvalJobsStub:
    """Stub matching the ``RunSubprocess`` signature.

    Dispatches based on argv[0]:

    * ``nix-eval-jobs``: ONE bulk invocation per binary. The ``--flake``
      argument is ``<flake_ref>#<attr>`` (no per-arch suffix); the
      per-arch suffix filter is encoded in the ``--select`` lambda as
      a nested ``{ <arch> = { <suffix> = null; ... }; ... }`` attrset.
      The stub parses that nested map out of the select expression and
      emits one JSONL line per surviving (arch, suffix) → drvPath,
      using ``attrPath = [arch, suffix]`` (matching what nix-eval-jobs
      itself emits with ``--force-recurse``). ``arch``/``suffix`` drvs
      come from ``drv_map[arch]``.
    * ``nix``: emits the JSON dict from ``meta_map[arch]`` for
      ``_meta.<sys>.<binary>.<arch>`` lookups. When ``meta_map`` is
      omitted (``None``) the stub auto-synthesises a passing meta dict
      from ``drv_map[arch]`` keys (every suffix gets
      ``{"compiler": "gcc15", "optimization": "O0"}`` — gcc15 is OK
      across every arch in ``table.md`` and the entry sails through
      ``is_known_bad_combo``). Pass ``meta_map={}`` to suppress the
      auto-derivation (an empty mapping is honoured as-is).
    * ``nix-store --query --requisites``: synthesises the closure as
      ``[<drv>, <drv>-input] for drv in argv[3:]`` so the export step
      has paths to act on. Override the synthesis by setting
      ``requisites_for[<drv>] = [<closure>, ...]``.
    * ``nix-store --export``: returns the synthetic byte payload
      ``b"NIX_EXPORT:" + b",".join(argv[2:])`` so tests can decode
      what closure was exported. Set ``export_fail=True`` to make this
      arm return rc=1.

    Set ``bulk_eval_fail`` to make the (single) bulk nix-eval-jobs
    invocation return rc=1 with the supplied stderr; used to test
    error propagation.
    """

    _META_AUTO = object()  # sentinel: derive meta from drv_map

    def __init__(
        self,
        *,
        drv_map: Optional[dict[str, dict[str, str]]] = None,
        meta_map: Any = _META_AUTO,
        bulk_eval_fail: Optional[str] = None,
        requisites_for: Optional[dict[str, list[str]]] = None,
        export_fail: bool = False,
        requisites_fail: bool = False,
    ) -> None:
        self.drv_map = drv_map or {}
        self._auto_meta = meta_map is _EvalJobsStub._META_AUTO
        self.meta_map = {} if self._auto_meta else (meta_map or {})
        self.bulk_eval_fail = bulk_eval_fail
        self.requisites_for = requisites_for or {}
        self.export_fail = export_fail
        self.requisites_fail = requisites_fail
        self.calls: list[list[str]] = []

    def _meta_for(self, arch: str) -> dict[str, dict]:
        """Return meta for ``arch``: explicit map if set, else auto-synth.

        Auto-synth: every suffix in ``drv_map[arch]`` gets a stub meta
        entry that ``is_supported`` accepts (gcc15 + every cross arch
        is ``OK`` per table.md, native x86_64 is unconditionally OK)
        and ``is_known_bad_combo`` returns ``None`` for (gcc15 isn't
        legacy so the broken-flag branches never fire, and the
        ``-O0`` optimisation alone is not a flag-vs-flag conflict).
        """
        if not self._auto_meta:
            return self.meta_map.get(arch, {})
        return {
            suffix: {"compiler": "gcc15", "optimization": "O0"}
            for suffix in self.drv_map.get(arch, {})
        }

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        if argv and argv[0] == "nix-eval-jobs":
            if self.bulk_eval_fail is not None:
                return b"", self.bulk_eval_fail.encode(), 1
            # Parse the per-arch suffix filter out of the --select
            # expression: form is
            #   m: let filter = { "<a>" = { "<s>" = null; ... }; ... }; in ...
            select_expr = argv[4] if len(argv) > 4 else ""
            import re as _re
            # Match each outer arch block: "arch" = { ...inner... };
            arch_blocks = _re.findall(
                r'"([A-Za-z0-9._-]+)"\s*=\s*\{([^}]*)\};', select_expr,
            )
            requested: dict[str, set[str]] = {}
            for arch, inner in arch_blocks:
                requested[arch] = set(
                    _re.findall(r'"([A-Za-z0-9._-]+)"\s*=\s*null;', inner)
                )
            lines = []
            for arch in sorted(requested):
                drvs = self.drv_map.get(arch, {})
                for suffix, drv in sorted(drvs.items()):
                    if suffix not in requested[arch]:
                        continue
                    lines.append(
                        json.dumps({
                            "attrPath": [arch, suffix],
                            "attr": f"{arch}.{suffix}",
                            "drvPath": drv,
                        })
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
                    meta = self._meta_for(arch)
                    return json.dumps(meta).encode(), b"", 0
            return b"{}", b"", 0
        return b"", f"unexpected argv: {argv!r}\n".encode(), 1


class _WrapperStub:
    """Capturing stub for
    :func:`template_graph.make_sum_drv.make_wrapper_drv_from_paths`.

    Records every call's kwargs and returns a deterministic synthetic
    drv path derived from the ``name`` argument so tests can assert
    the worker stores the wrapper drv in its return dict / passes it
    to ``nix-store --query --requisites``.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        drvs: list[str],
        name: str,
        system: str = "x86_64-linux",
        extra_nix_args: Optional[list[str]] = None,
    ) -> str:
        self.calls.append({
            "drvs": list(drvs),
            "name": name,
            "system": system,
            "extra_nix_args": extra_nix_args,
        })
        return f"/nix/store/wrap-{name}.drv"


def _install_wrapper_stub(monkeypatch: pytest.MonkeyPatch) -> _WrapperStub:
    """Monkey-patch the lazy-imported make_wrapper_drv_from_paths.

    eval_worker imports the symbol inside the function body
    (``from template_graph.make_sum_drv import make_wrapper_drv_from_paths``)
    so we patch on the source module — every subsequent import inside
    run_eval_task picks up the stub.
    """
    stub = _WrapperStub()
    monkeypatch.setattr(
        "template_graph.make_sum_drv.make_wrapper_drv_from_paths",
        stub,
    )
    return stub


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
    assert parsed["toolchain_aggregate_drv"] == _DEFAULT_TOOLCHAIN_AGG


def test_parse_payload_omits_optional_fields() -> None:
    """``variant_sample`` / ``variant_seed`` stay optional; only the
    phase-B-wired ``toolchain_aggregate_drv`` is mandatory on every
    payload (sample / seed govern variant sub-selection and are
    unrelated to the matrix-aggregate refactor).
    """
    payload = _make_payload()
    parsed = parse_payload(payload)
    assert parsed["variant_sample"] is None
    assert parsed["variant_seed"] is None
    # The toolchain aggregate drv is required and always present.
    assert parsed["toolchain_aggregate_drv"] == _DEFAULT_TOOLCHAIN_AGG


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("binary"),
        lambda p: p.update(sys=None),
        lambda p: p.update(archs="not-a-list"),
        lambda p: p.update(suffixes=[42]),
        lambda p: p.update(attr=""),
        lambda p: p.update(variant_sample="64"),
        lambda p: p.pop("toolchain_aggregate_drv"),
        lambda p: p.update(toolchain_aggregate_drv=""),
        lambda p: p.update(toolchain_aggregate_drv=42),
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


def test_run_eval_task_happy_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock subprocess + BroadcastSender; verify the full flow.

    Asserts: (1) nix-eval-jobs is called EXACTLY ONCE for the whole
    binary (bulk intersect over every arch + suffix); (2)
    BroadcastSender.enqueue_broadcast is called once per (arch ×
    suffix); (3) make_wrapper_drv_from_paths is invoked with the
    toolchain aggregate first + sorted leaves; (4) the per-binary
    ``<binary>.nix-archive`` is written; (5) the return value carries
    the variants + variant_drvs + broadcast results + the new
    ``matrix_aggregate_drv`` field; (6) the legacy
    ``<binary>/manifest.json`` marker is NOT emitted.
    """
    wrapper = _install_wrapper_stub(monkeypatch)
    payload = _make_payload(
        archs=["x86_64", "aarch64"],
        suffixes=["O0", "O2"],
        toolchain_aggregate_drv="/nix/store/aggr-toolchains.drv",
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

    # nix-eval-jobs invoked EXACTLY ONCE — one bulk eval per binary
    # is the central performance win of the matrix-aggregate refactor.
    eval_calls = [c for c in runner.calls if c and c[0] == "nix-eval-jobs"]
    assert len(eval_calls) == 1
    # --flake points at the binary attr (no per-arch tail).
    assert eval_calls[0][2] == ".#dataset.x86_64-linux.hello"
    # --select must encode the per-arch suffix filter and use
    # intersectAttrs at both levels.
    select_expr = eval_calls[0][4]
    assert "intersectAttrs" in select_expr
    for arch in ("x86_64", "aarch64"):
        assert f'"{arch}"' in select_expr
    for suffix in ("O0", "O2"):
        assert f'"{suffix}"' in select_expr
    # --force-recurse must be passed so nix-eval-jobs walks both
    # nested levels (arch → suffix) and emits leaves.
    assert "--force-recurse" in eval_calls[0]

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

    # make_wrapper_drv_from_paths invoked once for the matrix
    # aggregate: toolchain aggregate first, then sorted leaves.
    assert len(wrapper.calls) == 1
    wrap_call = wrapper.calls[0]
    assert wrap_call["drvs"][0] == "/nix/store/aggr-toolchains.drv"
    assert wrap_call["drvs"][1:] == [
        "/nix/store/aaa-hello-x86_64-O0.drv",
        "/nix/store/bbb-hello-x86_64-O2.drv",
        "/nix/store/ccc-hello-aarch64-O0.drv",
        "/nix/store/ddd-hello-aarch64-O2.drv",
    ]
    assert wrap_call["name"] == "matrix-hello"
    assert wrap_call["system"] == "x86_64-linux"

    # nix-store --query --requisites called once seeded with ONLY the
    # matrix aggregate (closure expansion follows inputDrvs to every
    # leaf and the toolchain aggregate). The pre-refactor seeding with
    # raw leaf drvs is gone — the aggregate is the single export root.
    req_calls = [
        c for c in runner.calls
        if c[:3] == ["nix-store", "--query", "--requisites"]
    ]
    assert len(req_calls) == 1
    assert req_calls[0][3:] == ["/nix/store/wrap-matrix-hello.drv"]
    export_calls = [
        c for c in runner.calls if c[:2] == ["nix-store", "--export"]
    ]
    assert len(export_calls) == 1
    # Closure passed to --export is what the stub synthesised from the
    # requisites argv (seed + seed-input for the aggregate).
    assert "/nix/store/wrap-matrix-hello.drv" in export_calls[0]
    assert "/nix/store/wrap-matrix-hello.drv-input" in export_calls[0]

    # Archive written with the stub's synthetic export payload.
    archive = tmp_path / "hello.nix-archive"
    assert archive.exists()
    assert archive.stat().st_size > 0
    assert archive.read_bytes().startswith(b"NIX_EXPORT:")

    # Returned dict carries the in-memory summary including the new
    # matrix_aggregate_drv field (consumed by the watcher / phase 3).
    assert result["binary"] == "hello"
    assert result["sys"] == "x86_64-linux"
    assert result["produced_at"] == 123.5
    assert result["matrix_aggregate_drv"] == "/nix/store/wrap-matrix-hello.drv"
    # variant_drvs is the sorted+deduped list — kept for backwards
    # compatibility while D.1b migrates consumers off it.
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
    # Worker drops a sidecar JSON alongside the archive so the
    # dependency_graph watcher can read matrix_aggregate_drv without
    # relying on the framework's payload-less TaskCompletedEvent wire.
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert siblings == [
        "hello.matrix_aggregate.json", "hello.nix-archive",
    ], siblings


# ---------------------------------------------------------------------------
# run_eval_task — resume short-circuit
# ---------------------------------------------------------------------------


def test_run_eval_task_resume_short_circuits(tmp_path: pathlib.Path) -> None:
    """If BOTH the archive and the matrix_aggregate sidecar from a
    prior run are present, short-circuit eval + broadcast + export and
    return a minimal ``{"resumed": True}`` dict. The archive carries the
    closure import for dep_graph; the sidecar carries the aggregate drv
    path. Without the sidecar, PH-A's dependency_graph framework task
    has no handoff, so the resume MUST require both (see
    :func:`test_run_eval_task_resume_requires_sidecar`)."""
    payload = _make_payload()
    archive = tmp_path / "hello.nix-archive"
    archive.write_bytes(b"NIX_EXPORT:previous-run")
    # Sidecar from the prior run must also be present for resume to fire.
    sidecar = tmp_path / "hello.matrix_aggregate.json"
    sidecar.write_text('{"binary":"hello","matrix_aggregate_drv":"/nix/store/prev.drv"}')

    runner = _EvalJobsStub()
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner,
    )

    # Returned dict carries the resumed marker and the per-binary
    # identity fields; no variants enumerated; matrix_aggregate_drv is
    # None because the archive already carries the (previously-built)
    # aggregate — we deliberately don't re-derive it from scratch.
    assert result.get("resumed") is True
    assert result["binary"] == "hello"
    assert result["sys"] == "x86_64-linux"
    assert result["variant_drvs"] == []
    assert result["variants"] == []
    assert result["broadcasts"] == []
    assert result["matrix_aggregate_drv"] is None
    # No subprocess calls — no eval, no requisites query, no export.
    assert runner.calls == []
    # No broadcasts.
    sender.enqueue_broadcast.assert_not_called()
    sender.wait_for_completion.assert_not_called()


def test_run_eval_task_empty_archive_re_runs(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty archive file (zero bytes) doesn't satisfy the resume
    check — we re-run rather than wedge on a half-written export.
    """
    _install_wrapper_stub(monkeypatch)
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
    # The freshly-built matrix aggregate path is reported.
    assert result["matrix_aggregate_drv"] == "/nix/store/wrap-matrix-hello.drv"


# Resume coverage:
#   * archive AND sidecar present  →  short-circuit (test_run_eval_task_resume_short_circuits)
#   * empty archive                 →  re-run (test_run_eval_task_empty_archive_re_runs)
#   * archive but NO sidecar       →  re-run + write sidecar (test_run_eval_task_resume_requires_sidecar)


def test_run_eval_task_resume_requires_sidecar(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An archive from a prior run is NOT sufficient for resume — the
    matrix_aggregate sidecar must also be present. PH-A's
    dependency_graph framework task reads the sidecar to learn the
    aggregate drv path; without it, dep_graph hangs (observed on the
    2026-05-21 cluster smoke).
    """
    _install_wrapper_stub(monkeypatch)
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    archive = tmp_path / "hello.nix-archive"
    archive.write_bytes(b"NIX_EXPORT:previous-run")
    # No sidecar — should trigger full re-evaluation despite archive.
    assert not (tmp_path / "hello.matrix_aggregate.json").exists()

    runner = _EvalJobsStub(
        drv_map={"x86_64": {"O0": "/nix/store/aaa.drv"}},
    )
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 7.0,
    )
    # nix-eval-jobs ran — resume short-circuit didn't fire.
    assert any(c and c[0] == "nix-eval-jobs" for c in runner.calls)
    assert result.get("resumed") is not True
    assert result["matrix_aggregate_drv"] == "/nix/store/wrap-matrix-hello.drv"
    # Sidecar was written this time.
    sidecar = tmp_path / "hello.matrix_aggregate.json"
    assert sidecar.exists() and sidecar.stat().st_size > 0


# ---------------------------------------------------------------------------
# Determinism end-to-end: same seed → identical drv list
# ---------------------------------------------------------------------------


def test_run_eval_task_determinism_same_seed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs with the same ``variant_seed`` produce identical
    variants[] lists AND identical ``drvs=`` arguments to
    ``make_wrapper_drv_from_paths``. Each run uses a fresh out_dir so
    no resume short-circuit hides drift between runs.

    Asserting wrapper-call equality (rather than just the synthetic
    ``matrix_aggregate_drv`` path) is what makes this test
    non-tautological: the stub returns ``/nix/store/wrap-{name}.drv``
    by construction, so matching paths only prove the binary name
    didn't change. The genuine determinism evidence is that the worker
    fed the wrapper the SAME sorted leaf list (same order, same drvs)
    on both runs.
    """
    wrapper = _install_wrapper_stub(monkeypatch)
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
    # The matrix aggregate drv depends on the sorted leaf set; same
    # seed → same leaves → same aggregate identifier. (This equality
    # is tautological at the stub layer — see below for the real
    # worker-side determinism assertion.)
    assert a["matrix_aggregate_drv"] == b["matrix_aggregate_drv"]
    # Worker-side determinism: both runs pushed the SAME ordered drvs
    # list (toolchain aggregate + sorted leaves) into the wrapper
    # helper. The wrapper-output equality above is tautological because
    # the stub keys solely off ``name``; this assertion is what proves
    # the run-to-run determinism lives in the worker, not the stub.
    assert len(wrapper.calls) == 2
    assert wrapper.calls[0]["drvs"] == wrapper.calls[1]["drvs"]
    # Also pin the surrounding kwargs so a future regression that
    # reshuffles name/system silently can't pass this test.
    assert wrapper.calls[0]["name"] == wrapper.calls[1]["name"]
    assert wrapper.calls[0]["system"] == wrapper.calls[1]["system"]


def test_run_eval_task_determinism_different_seed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different seed → different drv list (with enough samples to
    avoid the small-group degenerate case)."""
    _install_wrapper_stub(monkeypatch)
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
    """The single bulk nix-eval-jobs invocation returning rc=1 raises
    RuntimeError so the framework harness surfaces ErrorType::Errored.

    The exception message must include enough context (which binary
    attr) for operator triage.
    """
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    # drv_map seeds the auto-derived _meta entry for x86_64.O0 so the
    # in-worker filter keeps at least one cell — otherwise nix-eval-jobs
    # is never dispatched and the bulk_eval_fail arm never fires.
    runner = _EvalJobsStub(
        drv_map={"x86_64": {"O0": "/nix/store/aaa.drv"}},
        bulk_eval_fail="eval-time OOM: out of memory",
    )
    sender = _make_broadcast_sender()

    with pytest.raises(RuntimeError) as exc_info:
        run_eval_task(payload, tmp_path, sender, run_subprocess=runner)

    msg = str(exc_info.value)
    assert "nix-eval-jobs" in msg
    # The bulk eval targets the binary attr (no per-arch suffix).
    assert "dataset.x86_64-linux.hello" in msg
    assert "rc=1" in msg
    assert "eval-time OOM" in msg
    # No archive on failure — re-execution must re-eval.
    assert not (tmp_path / "hello.nix-archive").exists()
    # No broadcasts fired (we failed before any leaves were extracted).
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
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broadcast timeout is non-fatal: we record ``status=timeout``
    on the returned summary and continue. The flood-fill protocol is
    best-effort.
    """
    _install_wrapper_stub(monkeypatch)
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
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nix-store --query --requisites`` returning rc=1 surfaces as
    RuntimeError — retry-pass eligible. No archive on failure.
    """
    _install_wrapper_stub(monkeypatch)
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
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nix-store --export`` rc=1 surfaces as RuntimeError; the
    temporary archive file is removed so re-execution sees a clean
    out_dir.
    """
    _install_wrapper_stub(monkeypatch)
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


def test_run_eval_task_wrapper_failure_leaves_no_archive(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If :func:`make_wrapper_drv_from_paths` raises (e.g. nix-instantiate
    OOM during the matrix-aggregate build), the failure must propagate
    as :class:`RuntimeError`-shaped exit (matching the framework's
    Errored/retry-pass mapping for nix subprocess failures) and the
    output archive must NOT exist — re-execution must re-run the whole
    pipeline. Defensive: any ``.tmp`` partial archive must also be
    absent, even though Step 6 hasn't run, so a future atomic-rename
    refactor cannot regress this invariant silently.
    """
    def _raise(*args, **kwargs):
        raise RuntimeError("synthetic nix-instantiate failure")

    monkeypatch.setattr(
        "template_graph.make_sum_drv.make_wrapper_drv_from_paths",
        _raise,
    )
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    runner = _EvalJobsStub(
        drv_map={"x86_64": {"O0": "/nix/store/aaa.drv"}},
    )
    sender = _make_broadcast_sender()

    with pytest.raises(RuntimeError, match="synthetic nix-instantiate failure"):
        run_eval_task(payload, tmp_path, sender, run_subprocess=runner)

    archive = tmp_path / "hello.nix-archive"
    assert not archive.exists()
    assert not archive.with_suffix(".nix-archive.tmp").exists()


def test_run_eval_task_empty_kept_drvs_writes_empty_archive(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All archs returning zero drvs produces an empty archive (zero
    bytes) with ``variant_drvs=[]`` AND ``matrix_aggregate_drv=None``
    — a valid "binary has no plannable variants" outcome the
    dependency_graph worker will skip. The wrapper builder is NOT
    invoked when there are no leaves (would otherwise raise
    ValueError: "at least one drv path is required").
    """
    wrapper = _install_wrapper_stub(monkeypatch)
    payload = _make_payload(archs=["x86_64"], suffixes=["O0"])
    # Empty drv map for the requested arch → bulk_drvs ends up empty.
    runner = _EvalJobsStub(drv_map={"x86_64": {}})
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )
    assert result["variant_drvs"] == []
    assert result["variants"] == []
    assert result["matrix_aggregate_drv"] is None
    # The wrapper helper must NOT have been called — it would have
    # raised ValueError on an empty drv list.
    assert wrapper.calls == []
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
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``variant_sample`` is set we MUST query ``_meta`` (one
    call per arch) to drive the sampling. Without that the worker
    would have no compiler/opt metadata to group by. The bulk
    ``nix-eval-jobs`` invocation still happens exactly once, after
    sampling is done.
    """
    _install_wrapper_stub(monkeypatch)
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

    # _meta lookup happened (one per arch).
    meta_calls = [
        c for c in runner.calls
        if c and c[0] == "nix" and any("_meta." in p for p in c)
    ]
    assert len(meta_calls) == 1
    # Bulk eval ran exactly once.
    eval_calls = [c for c in runner.calls if c and c[0] == "nix-eval-jobs"]
    assert len(eval_calls) == 1
    # 4 groups × sample_size=1 → 4 variants kept.
    assert len(result["variants"]) == 4
    # Each broadcast enqueued once.
    assert sender.enqueue_broadcast.call_count == 4


def test_run_eval_task_calls_meta_even_when_no_sampling(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``variant_sample`` STILL triggers one ``_meta`` lookup per arch.

    Filter relocation (``is_supported`` + ``is_known_bad_combo`` moved
    from the submitter into ``_sample_per_arch``) means every run
    needs the meta dict to drive the filter, sample or not. With
    ``variant_sample`` unset the worker bypasses the rng sampler and
    forwards the FULL filtered set to the bulk eval — but ``_meta``
    has already been read once per arch by then.
    """
    _install_wrapper_stub(monkeypatch)
    payload = _make_payload(
        archs=["x86_64", "aarch64"], suffixes=["O0", "O2"],
    )
    runner = _EvalJobsStub(
        drv_map={
            "x86_64": {"O0": "/nix/store/a.drv", "O2": "/nix/store/b.drv"},
            "aarch64": {"O0": "/nix/store/c.drv", "O2": "/nix/store/d.drv"},
        }
    )
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )
    # One _meta lookup per arch — variant_sample unset does NOT skip
    # them anymore, because the filter chain still needs the meta dict.
    meta_calls = [
        c for c in runner.calls
        if c and c[0] == "nix" and any("#_meta." in p for p in c)
    ]
    assert len(meta_calls) == 2
    arches_queried = sorted(c[-1].rsplit(".", 1)[-1] for c in meta_calls)
    assert arches_queried == ["aarch64", "x86_64"]
    # The full filtered set is forwarded to the bulk eval (no rng
    # down-sampling): 2 archs × 2 surviving suffixes = 4 variants.
    assert len(result["variants"]) == 4
    # Exactly one bulk nix-eval-jobs invocation.
    eval_calls = [c for c in runner.calls if c and c[0] == "nix-eval-jobs"]
    assert len(eval_calls) == 1


# ---------------------------------------------------------------------------
# Bulk per-binary eval shape
# ---------------------------------------------------------------------------


def test_bulk_eval_per_arch_suffix_filter_is_distinct_per_arch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-arch suffix filter must be encoded INDEPENDENTLY per
    arch: deterministic sampling can produce different suffix subsets
    for different archs (the seed string includes the arch), so the
    bulk eval's --select must respect that asymmetry.
    """
    _install_wrapper_stub(monkeypatch)
    # Different drvs per arch; sample_size=1 over a 2-group meta picks
    # one per (compiler, opt) group → different suffix selected per
    # arch because the rng is seeded with the arch.
    meta_x86 = {
        "A": {"compiler": "gcc15", "optimization": "O0"},
        "B": {"compiler": "gcc15", "optimization": "O0"},
        "C": {"compiler": "clang19", "optimization": "O2"},
        "D": {"compiler": "clang19", "optimization": "O2"},
    }
    meta_aarch = dict(meta_x86)
    payload = _make_payload(
        archs=["x86_64", "aarch64"],
        suffixes=["A", "B", "C", "D"],
        variant_sample=1,
        variant_seed="s",
    )
    runner = _EvalJobsStub(
        drv_map={
            "x86_64": {k: f"/nix/store/x-{k}.drv" for k in meta_x86},
            "aarch64": {k: f"/nix/store/a-{k}.drv" for k in meta_aarch},
        },
        meta_map={"x86_64": meta_x86, "aarch64": meta_aarch},
    )
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )
    # We expect one variant per (arch, group) = 4 total (2 archs ×
    # 2 groups × sample=1).
    assert len(result["variants"]) == 4
    # Both archs are represented in the broadcast set.
    arches = {v["arch"] for v in result["variants"]}
    assert arches == {"x86_64", "aarch64"}
    # Single bulk eval call.
    eval_calls = [c for c in runner.calls if c and c[0] == "nix-eval-jobs"]
    assert len(eval_calls) == 1


def test_bulk_eval_drops_archs_with_no_sampled_suffixes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arch whose meta has zero suffixes in scope is omitted from
    the --select expression entirely — the worker must not splice an
    empty inner ``{ }`` block that would degenerate the filter.
    """
    _install_wrapper_stub(monkeypatch)
    payload = _make_payload(
        archs=["x86_64", "aarch64"],
        suffixes=["O0"],
        variant_sample=4,
        variant_seed="s",
    )
    # x86_64 has the suffix; aarch64 has none in scope (meta empty).
    runner = _EvalJobsStub(
        drv_map={"x86_64": {"O0": "/nix/store/x.drv"}},
        meta_map={
            "x86_64": {"O0": {"compiler": "gcc15", "optimization": "O0"}},
            "aarch64": {},
        },
    )
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )
    # Only x86_64 produced a variant.
    assert len(result["variants"]) == 1
    assert result["variants"][0]["arch"] == "x86_64"
    # The --select expression must not name aarch64 at all (empty
    # block would still leak the arch key into the outer
    # intersectAttrs and confuse downstream readers).
    eval_calls = [c for c in runner.calls if c and c[0] == "nix-eval-jobs"]
    assert len(eval_calls) == 1
    select_expr = eval_calls[0][4]
    assert '"x86_64"' in select_expr
    assert '"aarch64"' not in select_expr


# ---------------------------------------------------------------------------
# Matrix-aggregate construction
# ---------------------------------------------------------------------------


def test_matrix_aggregate_carries_toolchain_and_sorted_leaves(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-binary ``matrix-<binary>`` aggregate must:

    1. take the phase-1 toolchain aggregate drv as its FIRST inputDrv;
    2. take the SORTED leaf list as the remaining inputDrvs;
    3. carry the binary-derived name ``matrix-<binary>``;
    4. carry the payload's ``sys`` as ``system``.

    These are the four invariants downstream phase 3 keys off when
    refcount-sorting the ``nix-store --query --tree`` output.
    """
    wrapper = _install_wrapper_stub(monkeypatch)
    payload = _make_payload(
        binary="busybox",
        sys_name="aarch64-linux",
        archs=["x86_64", "aarch64"],
        suffixes=["O0"],
        toolchain_aggregate_drv="/nix/store/specific-toolchains.drv",
    )
    # Reverse-order drv table; the worker must sort before passing on.
    drv_x = "/nix/store/zzz-x.drv"
    drv_a = "/nix/store/aaa-a.drv"
    runner = _EvalJobsStub(
        drv_map={
            "x86_64": {"O0": drv_x},
            "aarch64": {"O0": drv_a},
        },
    )
    sender = _make_broadcast_sender()
    result = run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )

    assert len(wrapper.calls) == 1
    call = wrapper.calls[0]
    # (1) toolchain aggregate first.
    assert call["drvs"][0] == "/nix/store/specific-toolchains.drv"
    # (2) sorted leaves follow.
    assert call["drvs"][1:] == sorted([drv_x, drv_a])
    # (3) name derived from binary.
    assert call["name"] == "matrix-busybox"
    # (4) system from payload sys.
    assert call["system"] == "aarch64-linux"
    # No extra positional args are passed (the helper does not accept
    # ``bash_path`` or other kwargs beyond drvs/name/system).
    assert call["extra_nix_args"] is None

    # Result dict surfaces the wrapper output as matrix_aggregate_drv.
    assert result["matrix_aggregate_drv"] == "/nix/store/wrap-matrix-busybox.drv"


def test_matrix_aggregate_is_export_seed_for_archive(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregate drv (not the raw leaves) is what we feed into
    ``nix-store --query --requisites``. Closure expansion then carries
    the toolchain aggregate + every leaf transitively, so the single
    ``nix-store --export`` produces an archive that imports the whole
    drv graph on the primary.
    """
    _install_wrapper_stub(monkeypatch)
    payload = _make_payload(
        archs=["x86_64"], suffixes=["O0", "O2"],
    )
    runner = _EvalJobsStub(
        drv_map={"x86_64": {
            "O0": "/nix/store/o0.drv",
            "O2": "/nix/store/o2.drv",
        }},
    )
    sender = _make_broadcast_sender()
    run_eval_task(
        payload, tmp_path, sender, run_subprocess=runner, now=lambda: 1.0,
    )
    req_calls = [
        c for c in runner.calls
        if c[:3] == ["nix-store", "--query", "--requisites"]
    ]
    assert len(req_calls) == 1
    # Exactly one seed: the matrix aggregate. The raw leaves are NOT
    # passed directly — they would be redundant (the aggregate carries
    # them transitively).
    assert req_calls[0][3:] == ["/nix/store/wrap-matrix-hello.drv"]


# ---------------------------------------------------------------------------
# Nix-marked round-trip — guard the transitive-export contract
# ---------------------------------------------------------------------------


@pytest.mark.nix
def test_matrix_archive_round_trips_leaves(tmp_path: pathlib.Path) -> None:
    """Round-trip a synthetic matrix-<binary> aggregate through
    nix-store --export / --import in an isolated store, verify every
    leaf path is present in the imported store post-import.

    Guards against a future regression where wrapper_drv.nix loses its
    string-context side-channel (``refs = builtins.toString drvs``) and
    exports drop leaves silently. The production flow (eval_worker
    ``_export_kept_closure`` / ``_export_matrix_archive``) is
    ``nix-store --query --requisites <aggregate>`` followed by
    ``nix-store --export <requisites...>``; this test mirrors that
    sequence end-to-end against three cheap nixpkgs leaves.
    """
    import os
    import shutil
    import subprocess

    for tool in ("nix-instantiate", "nix-store"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not in PATH")

    from template_graph.make_sum_drv import make_wrapper_drv_from_paths

    def _instantiate(expr: str) -> str:
        proc = subprocess.run(
            ["nix-instantiate", "-E", expr],
            capture_output=True, text=True, check=True,
        )
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        assert lines, f"nix-instantiate produced no output for {expr!r}"
        return lines[-1].strip()

    # Three cheap, distinct, non-bash leaves. We avoid using `bash` as a
    # leaf because ``wrapper_drv.nix`` already pulls bash in as its
    # builder, and the collapsed reference would hide a leaf-drop bug.
    leaf_drvs = [
        _instantiate("(import <nixpkgs> {}).coreutils"),
        _instantiate("(import <nixpkgs> {}).findutils"),
        _instantiate("(import <nixpkgs> {}).gnused"),
    ]
    for path in leaf_drvs:
        assert path.startswith("/nix/store/") and path.endswith(".drv"), (
            f"unexpected leaf path shape: {path!r}"
        )

    matrix_agg = make_wrapper_drv_from_paths(
        drvs=leaf_drvs,
        name="matrix-synthetic",
        system="x86_64-linux",
    )
    assert matrix_agg.startswith("/nix/store/") and matrix_agg.endswith(".drv")

    # Direct-reference sanity check in the primary store: this is the
    # property the aggregate is supposed to carry — exporting / importing
    # the closure cannot magically reintroduce a leaf that the wrapper
    # itself failed to reference. If THIS fails, the regression sits in
    # ``wrapper_drv.nix`` or ``make_wrapper_drv_from_paths`` and the
    # round-trip below would only be a noisier symptom.
    primary_refs = subprocess.run(
        ["nix-store", "--query", "--references", matrix_agg],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for leaf in leaf_drvs:
        assert leaf in primary_refs, (
            f"leaf {leaf} missing from aggregate references in primary "
            f"store — wrapper_drv.nix likely lost its string-context "
            f"side-channel"
        )

    # End-to-end round-trip: enumerate closure, export, import into an
    # isolated sandbox store, requery references. Mirrors what phase 3
    # does on the primary after pulling the per-binary archive from a
    # secondary.
    requisites = subprocess.run(
        ["nix-store", "--query", "--requisites", matrix_agg],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert matrix_agg in requisites, (
        "aggregate must appear in its own --requisites output"
    )

    archive_path = tmp_path / "synthetic.nix-archive"
    with archive_path.open("wb") as fp:
        subprocess.run(
            ["nix-store", "--export", *requisites],
            stdout=fp, check=True,
        )
    assert archive_path.stat().st_size > 0, "empty archive produced"

    sandbox_root = tmp_path / "store"
    sandbox_root.mkdir()
    store_uri = f"local?root={sandbox_root}"
    with archive_path.open("rb") as fp:
        subprocess.run(
            ["nix-store", "--store", store_uri, "--import"],
            stdin=fp, check=True, capture_output=True,
        )

    sandbox_refs = subprocess.run(
        ["nix-store", "--store", store_uri,
         "--query", "--references", matrix_agg],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for leaf in leaf_drvs:
        assert leaf in sandbox_refs, (
            f"leaf {leaf} not present in imported store — "
            f"nix-store --export dropped a transitively-referenced "
            f"inputDrv from the aggregate's closure"
        )


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


# ---------------------------------------------------------------------------
# _sample_per_arch — support_table + known-bad filter chain
# ---------------------------------------------------------------------------


_FILTER_TEST_TABLE = """\
| Compiler   | i686      | aarch64   | mips64el  |
|------------|-----------|-----------|-----------|
| gcc15      | OK        | OK        | OK        |
| clang19    | OK        | OK        | OK        |
| gcc4_6     | OK        | n/a       | OK        |
"""


@pytest.fixture
def _filter_table(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Install a deterministic support_table on disk for the filter tests.

    The real ``table.md`` lives at the flake root and is not visible
    from pytest's cwd (``python/``). We write a synthetic table to
    ``tmp_path``, repoint :func:`default_table_path` at it, and clear
    the lru_cache so ``_sample_per_arch``'s lazy ``load_support_table()``
    call picks up the synthetic table.
    """
    from compiler_suit_runner import support_table as _support_table
    table_path = tmp_path / "table.md"
    table_path.write_text(_FILTER_TEST_TABLE, encoding="utf-8")
    monkeypatch.setattr(
        _support_table, "default_table_path", lambda *a, **kw: table_path,
    )
    _support_table.load_support_table.cache_clear()
    yield
    _support_table.load_support_table.cache_clear()


class TestSamplePerArchFilters:
    """Coverage for the filter logic relocated into ``_sample_per_arch``.

    Each test stubs :func:`eval_worker._eval_meta_for_arch` via
    monkeypatch so we feed the filter chain a fully-controlled meta
    dict; the underlying nix-eval subprocess is never invoked. The
    ``_filter_table`` fixture pins a deterministic support_table on
    disk so ``is_supported`` returns the expected verdict regardless
    of pytest's cwd.
    """

    @staticmethod
    def _patch_meta(
        monkeypatch: pytest.MonkeyPatch,
        meta_by_arch: dict[str, dict[str, dict]],
    ) -> list[str]:
        """Install a stub for ``_eval_meta_for_arch``; record archs asked.

        Returns the (mutable) list the stub appends to on each call —
        tests can assert order / multiplicity if they care.
        """
        seen: list[str] = []

        def _stub(sys_name, binary, arch, *, flake_ref, run_subprocess):
            seen.append(arch)
            return meta_by_arch.get(arch, {})

        monkeypatch.setattr(eval_worker, "_eval_meta_for_arch", _stub)
        return seen

    @staticmethod
    def _call(
        archs: list[str],
        suffixes: list[str],
        variant_sample: Optional[int],
        variant_seed: Optional[str],
    ) -> dict[str, list[str]]:
        return eval_worker._sample_per_arch(
            archs,
            suffixes,
            variant_sample,
            variant_seed,
            sys_name="x86_64-linux",
            binary="hello",
            flake_ref=".",
            run_subprocess=lambda argv: (b"", b"", 0),
        )

    def test_drops_suffix_with_unsupported_compiler(
        self, monkeypatch: pytest.MonkeyPatch, _filter_table,
    ) -> None:
        """``gcc4_6 + aarch64`` is ``n/a`` per table.md → dropped.

        ``gcc15 + aarch64`` is ``OK`` so the second suffix survives.
        """
        self._patch_meta(monkeypatch, {
            "aarch64": {
                "good": {"compiler": "gcc15", "optimization": "O0"},
                "bad": {"compiler": "gcc4_6", "optimization": "O0"},
            },
        })
        out = self._call(["aarch64"], [], None, None)
        assert out == {"aarch64": ["good"]}

    def test_drops_suffix_with_known_bad_combo(
        self, monkeypatch: pytest.MonkeyPatch, _filter_table,
    ) -> None:
        """``sanitizer + O0`` is a known flag-vs-flag conflict; the
        suffix is dropped even though its (compiler, arch) pair is
        supported. The benign sibling survives.
        """
        self._patch_meta(monkeypatch, {
            "x86_64": {
                "good": {"compiler": "gcc15", "optimization": "O2"},
                "bad": {
                    "compiler": "gcc15",
                    "optimization": "O0",
                    "sanitizer": "san-address",
                },
            },
        })
        out = self._call(["x86_64"], [], None, None)
        assert out == {"x86_64": ["good"]}

    def test_variant_sample_zero_returns_filtered_full_set(
        self, monkeypatch: pytest.MonkeyPatch, _filter_table,
    ) -> None:
        """``variant_sample=0`` short-circuits the rng sampler — the
        worker still runs the support / known-bad filter and returns
        the FULL filtered set (every suffix the filter kept).
        """
        self._patch_meta(monkeypatch, {
            "x86_64": {
                f"S{i:03d}": {"compiler": "gcc15", "optimization": "O0"}
                for i in range(5)
            },
        })
        out = self._call(["x86_64"], [], 0, "seed-x")
        assert out == {"x86_64": [f"S{i:03d}" for i in range(5)]}

    def test_variant_sample_positive_samples_from_filtered_set(
        self, monkeypatch: pytest.MonkeyPatch, _filter_table,
    ) -> None:
        """``variant_sample > 0`` path: rejected entries from the raw
        ``_meta`` set MUST NOT appear in the sampled output. Sampling
        itself happens per-``(compiler, opt)`` group, so this test
        guards the filter-before-sample invariant rather than the
        absolute output cardinality (a future swap of the two would
        let a known-bad suffix sneak through).
        """
        # 6 entries split 4 survivors / 2 filtered-out (sanitiser+O0).
        meta = {
            "G1": {"compiler": "gcc15", "optimization": "O0"},
            "G2": {"compiler": "gcc15", "optimization": "O2"},
            "C1": {"compiler": "clang19", "optimization": "O0"},
            "C2": {"compiler": "clang19", "optimization": "O2"},
            # Known-bad: sanitiser + O0.
            "X1": {
                "compiler": "gcc15", "optimization": "O0",
                "sanitizer": "san-address",
            },
            # Unsupported: gcc4_6 on aarch64 below.
            "X2": {"compiler": "gcc4_6", "optimization": "O0"},
        }
        self._patch_meta(monkeypatch, {"aarch64": meta})
        out = self._call(["aarch64"], [], 2, "seed-y")
        kept = out["aarch64"]
        # rng down-samples each (compiler, opt) group to at most 2 —
        # but the four survivor groups have one suffix each, so all
        # four survive when sample_size >= 1.
        assert set(kept) == {"G1", "G2", "C1", "C2"}
        # Critically: the rejected entries are NOT in the result.
        assert "X1" not in kept
        assert "X2" not in kept

    def test_legacy_ceiling_honoured(
        self, monkeypatch: pytest.MonkeyPatch, _filter_table,
    ) -> None:
        """Non-empty ``suffixes`` (legacy payload shape) caps the
        result to that set: a meta-only suffix (``z``) is dropped even
        though it passes the filter. The ceiling is NOT a bypass — a
        ceiling-listed suffix that fails the filter is still dropped.
        """
        self._patch_meta(monkeypatch, {
            "x86_64": {
                "x": {"compiler": "gcc15", "optimization": "O0"},
                # 'y' is in the ceiling but fails the filter
                # (sanitiser + O0).
                "y": {
                    "compiler": "gcc15", "optimization": "O0",
                    "sanitizer": "san-address",
                },
                # 'z' would pass the filter but is NOT in the ceiling.
                "z": {"compiler": "gcc15", "optimization": "O2"},
            },
        })
        out = self._call(["x86_64"], ["x", "y"], None, None)
        # 'z' dropped by the ceiling; 'y' dropped by is_known_bad_combo;
        # only 'x' survives.
        assert out == {"x86_64": ["x"]}


# ---------------------------------------------------------------------------
# parse_payload — suffix-tolerance
# ---------------------------------------------------------------------------


class TestParsePayloadSuffixesTolerance:
    """parse_payload now treats absent / None / empty-list ``suffixes``
    as the same canonical empty-list payload shape.

    Older submitters wrote a full suffix list; the modern submitter
    leaves the field unset and the worker derives the list from
    ``_meta`` per arch. The parser must accept both shapes — and it
    must still reject obvious type errors so a typo in a future header
    refactor surfaces at parse time, not deep inside the eval pipeline.
    """

    def test_parse_payload_accepts_suffixes_absent(self) -> None:
        payload = _make_payload()
        payload.pop("suffixes")
        parsed = parse_payload(payload)
        assert parsed["suffixes"] == []

    def test_parse_payload_accepts_suffixes_none(self) -> None:
        payload = _make_payload()
        payload["suffixes"] = None
        parsed = parse_payload(payload)
        assert parsed["suffixes"] == []

    def test_parse_payload_accepts_suffixes_empty_list(self) -> None:
        payload = _make_payload(suffixes=[])
        parsed = parse_payload(payload)
        assert parsed["suffixes"] == []

    def test_parse_payload_rejects_suffixes_non_list_non_none(self) -> None:
        payload = _make_payload()
        payload["suffixes"] = "not_a_list"
        with pytest.raises(ValueError, match="suffixes"):
            parse_payload(payload)


# ---------------------------------------------------------------------------
# _resolve_flake_ref / _as_installable — path: prefix for store subdirs
# ---------------------------------------------------------------------------


class TestResolveFlakeRef:
    """Regression coverage for the ``path:`` flake-ref prefix.

    ``nix eval /nix/store/<hash>/sub/dir#attr`` without the prefix
    strips the trailing components ("searching up" for the nearest
    ``flake.nix``) and fails on the bare store root. Prefixing the
    absolute path with ``path:`` makes both ``nix eval`` and
    ``nix-eval-jobs`` accept the subdirectory as the flake location.
    """

    def test_non_absolute_flake_ref_unchanged(self) -> None:
        """``github:foo/bar`` and other non-absolute refs pass through."""
        assert eval_worker._resolve_flake_ref("github:foo/bar") == (
            "github:foo/bar"
        )

    def test_already_prefixed_flake_ref_unchanged(self) -> None:
        """A pre-prefixed ``path:`` ref must not be double-prefixed."""
        assert eval_worker._resolve_flake_ref("path:/foo/bar") == (
            "path:/foo/bar"
        )
        # _as_installable directly is idempotent on the prefixed form.
        assert eval_worker._as_installable("path:/foo/bar") == (
            "path:/foo/bar"
        )

    def test_absolute_path_gets_path_prefix(self) -> None:
        """Bare absolute paths gain the ``path:`` prefix so nix tools
        treat them as direct flake locations rather than searching up.
        """
        assert eval_worker._resolve_flake_ref("/some/absolute/path") == (
            "path:/some/absolute/path"
        )
        assert eval_worker._as_installable("/foo/bar") == "path:/foo/bar"

    def test_dot_with_env_var_pointing_at_tmp_flake(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``.`` + ``CSR_FLAKE_DIR`` pointing at a dir with ``flake.nix``
        resolves to ``path:<that-dir>`` — the in-container short-circuit
        that avoids nix-eval-jobs re-copying the flake into its sandbox.
        """
        (tmp_path / "flake.nix").write_text("{ outputs = _: {}; }\n")
        monkeypatch.setenv("CSR_FLAKE_DIR", str(tmp_path))
        assert eval_worker._resolve_flake_ref(".") == f"path:{tmp_path}"

    def test_dot_without_env_and_no_container_flake_returns_dot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No env var + no ``/app/flake`` available → return ``.`` verbatim.

        This is the developer-host default (pytest run from the repo
        root): the eval call inherits cwd and nix resolves ``.`` itself.
        """
        monkeypatch.delenv("CSR_FLAKE_DIR", raising=False)
        # Point the container-fallback at a path guaranteed not to exist
        # so the second branch in _resolve_flake_ref also misses.
        monkeypatch.setattr(
            eval_worker, "_CONTAINER_FLAKE_ROOT", "/nonexistent-app-flake",
        )
        assert eval_worker._resolve_flake_ref(".") == "."


# ---------------------------------------------------------------------------
# _eval_meta_for_arch — --no-eval-cache to avoid SQLite contention
# ---------------------------------------------------------------------------


class TestEvalMetaForArch:
    """Regression coverage for the ``--no-eval-cache`` flag.

    Multiple workers inside one secondary container share
    ``/root/.cache/nix/eval-cache-v6/*.sqlite``. Without
    ``--no-eval-cache`` two concurrent ``nix eval`` invocations serialise
    on the SQLite write lock and one fails with "database is busy".
    """

    def test_argv_contains_no_eval_cache_flag(self) -> None:
        captured: list[list[str]] = []

        def _run(argv: list[str]) -> tuple[bytes, bytes, int]:
            captured.append(list(argv))
            return b'{"O0": {"compiler": "gcc15", "optimization": "O0"}}', b"", 0

        result = eval_worker._eval_meta_for_arch(
            "x86_64-linux", "hello", "x86_64",
            flake_ref="path:/some/flake",
            run_subprocess=_run,
        )
        assert result == {"O0": {"compiler": "gcc15", "optimization": "O0"}}
        assert len(captured) == 1
        argv = captured[0]
        assert "--no-eval-cache" in argv

    def test_argv_order_matches_spec(self) -> None:
        """The exact argv prefix is the wire contract with ``nix eval``:
        ``nix eval --extra-experimental-features 'nix-command flakes'
        --no-eval-cache --json <flake>#_meta....``. Any reorder or
        accidental drop would surface here.
        """
        captured: list[list[str]] = []

        def _run(argv: list[str]) -> tuple[bytes, bytes, int]:
            captured.append(list(argv))
            return b"{}", b"", 0

        eval_worker._eval_meta_for_arch(
            "x86_64-linux", "hello", "aarch64",
            flake_ref="path:/some/flake",
            run_subprocess=_run,
        )
        argv = captured[0]
        # The fixed prefix; the trailing positional is the installable.
        assert argv[:6] == [
            "nix",
            "eval",
            "--extra-experimental-features",
            "nix-command flakes",
            "--no-eval-cache",
            "--json",
        ]
        # And the installable lands as the last token, identifying the
        # arch slot we asked for.
        assert argv[-1] == "path:/some/flake#_meta.x86_64-linux.hello.aarch64"

    def test_nonzero_exit_raises_runtime_error(self) -> None:
        """Subprocess failures map to RuntimeError (retry-pass eligible)."""

        def _run(argv: list[str]) -> tuple[bytes, bytes, int]:
            return b"", b"boom", 1

        with pytest.raises(RuntimeError, match="boom"):
            eval_worker._eval_meta_for_arch(
                "x86_64-linux", "hello", "x86_64",
                flake_ref=".",
                run_subprocess=_run,
            )
