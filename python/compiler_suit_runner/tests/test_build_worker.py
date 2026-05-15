"""Tests for the build_worker module.

Hermetic: no real nix build is invoked, no real time elapses. Subprocess
execution is replaced with a recording stub so we can assert on argv
shape; the wall clock is replaced with a monotonically increasing fake.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from compiler_suit_runner.workers import build_worker as bw
from compiler_suit_runner.workers.build_worker import (
    ITEM_CLASS_PHASE2_COMMON_DEP,
    ITEM_CLASS_PHASE2_TOOLCHAIN,
    ITEM_CLASS_PHASE2_TOOLCHAIN_VALIDATE,
    ITEM_CLASS_PHASE3_VARIANT,
    BuildWorkerEnv,
    BuildWorkerResult,
    build_attr,
    build_worker,
    copy_elf_folder,
    parse_build_manifest,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Monotonically increasing clock; advances by ``step`` per read."""

    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        v = self.now
        self.now += self.step
        return v


def _write_substituters_file(tmp_path, lines: list[str]):
    """Helper: write a substituters file the build worker can read."""
    target = tmp_path / "_substituters.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


class RecordingRunner:
    """Subprocess stub that records argv and returns programmable output."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        return self.stdout, self.stderr, self.returncode


def _write_manifest(
    path: pathlib.Path, *, item_class: str, name: str, payload: dict
) -> pathlib.Path:
    path.write_text(
        json.dumps(
            {"item_class": item_class, "name": name, "payload": payload}
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# parse_build_manifest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item_class",
    [
        ITEM_CLASS_PHASE2_TOOLCHAIN,
        ITEM_CLASS_PHASE2_COMMON_DEP,
        ITEM_CLASS_PHASE3_VARIANT,
    ],
)
def test_parse_build_manifest_accepts_all_three_classes(tmp_path, item_class):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=item_class,
        name="demo",
        payload={"attr": "x"},
    )
    parsed = parse_build_manifest(manifest)
    assert parsed["item_class"] == item_class
    assert parsed["name"] == "demo"
    assert parsed["payload"] == {"attr": "x"}


def test_parse_build_manifest_rejects_unknown_class(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class="phase4_quantum",
        name="bogus",
        payload={"attr": "x"},
    )
    with pytest.raises(ValueError, match="unknown item_class"):
        parse_build_manifest(manifest)


def test_parse_build_manifest_rejects_missing_class(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"name": "no-class", "payload": {}}), "utf-8")
    with pytest.raises(ValueError, match="unknown item_class"):
        parse_build_manifest(p)


def test_parse_build_manifest_rejects_invalid_json(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_build_manifest(p)


def test_parse_build_manifest_rejects_non_object(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_build_manifest(p)


def test_parse_build_manifest_missing_file(tmp_path):
    with pytest.raises(ValueError, match="cannot read build manifest"):
        parse_build_manifest(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# build_attr
# ---------------------------------------------------------------------------


def test_build_attr_argv_basic(tmp_path):
    runner = RecordingRunner(stdout=b"/nix/store/xxx-out\n")
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path,
        run_subprocess=runner,
    )
    success, stdout, _ = build_attr("hello", env)
    assert success is True
    assert stdout == b"/nix/store/xxx-out\n"
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == "nix"
    assert argv[1] == "build"
    assert "--no-link" in argv
    assert "--print-out-paths" in argv
    assert argv[-1] == ".#hello"


def test_build_attr_includes_peer_args(tmp_path):
    peer_args = [
        "--extra-substituters",
        "http://node1:5000 http://node2:5000",
        "--extra-trusted-public-keys",
        "k1 k2",
        "--substitute-on-destination",
    ]
    substituters = _write_substituters_file(tmp_path, peer_args)
    runner = RecordingRunner()
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path,
        substituters_file=substituters,
        run_subprocess=runner,
    )
    build_attr("foo", env)
    argv = runner.calls[0]
    for a in peer_args:
        assert a in argv
    # Peer args must appear before the trailing attr-spec.
    attr_index = argv.index(".#foo")
    for a in peer_args:
        assert argv.index(a) < attr_index


def test_build_attr_appends_extra_args(tmp_path):
    runner = RecordingRunner()
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path,
        run_subprocess=runner,
    )
    build_attr("foo", env, extra_args=["--skip-existing", "-L"])
    argv = runner.calls[0]
    assert "--skip-existing" in argv
    assert "-L" in argv
    # Caller extra args must be ahead of the attr-spec but after the
    # baseline flags.
    assert argv.index("--skip-existing") < argv.index(".#foo")


def test_build_attr_returns_failure_on_nonzero(tmp_path):
    runner = RecordingRunner(returncode=1, stderr=b"boom")
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path,
        run_subprocess=runner,
    )
    success, _, stderr = build_attr("foo", env)
    assert success is False
    assert stderr == b"boom"


def test_build_attr_uses_custom_flake_ref(tmp_path):
    runner = RecordingRunner()
    env = BuildWorkerEnv(
        flake_ref="git+https://example/repo?rev=deadbeef",
        dataset_output_dir=tmp_path,
        run_subprocess=runner,
    )
    build_attr("dataset.x86_64-linux.hello.x86_64.gcc15.O2", env)
    argv = runner.calls[0]
    assert argv[-1] == (
        "git+https://example/repo?rev=deadbeef#"
        "dataset.x86_64-linux.hello.x86_64.gcc15.O2"
    )


# ---------------------------------------------------------------------------
# copy_elf_folder
# ---------------------------------------------------------------------------


def _make_elf_folder(out_dir: pathlib.Path, files: dict[str, bytes]) -> None:
    """Build a fake mkBinaryFolder out_path: ``out_dir/elf/<name>`` files."""
    elf_dir = out_dir / "elf"
    elf_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (elf_dir / name).write_bytes(data)


def test_copy_elf_folder_basic(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_elf_folder(out_dir, {"hello": b"elf-bytes-hello", "world": b"world"})
    dest_dir = tmp_path / "dataset"
    variant_dir = "hello__x86_64__gcc15__O2"
    staged = copy_elf_folder(out_dir, dest_dir, variant_dir)
    expected_dir = dest_dir / variant_dir
    assert sorted(p.name for p in staged) == ["hello", "world"]
    assert (expected_dir / "hello").read_bytes() == b"elf-bytes-hello"
    assert (expected_dir / "world").read_bytes() == b"world"


def test_copy_elf_folder_creates_dest_dir(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_elf_folder(out_dir, {"x": b"data"})
    dest_dir = tmp_path / "deep" / "nested" / "dataset"
    staged = copy_elf_folder(out_dir, dest_dir, "v")
    assert staged
    assert (dest_dir / "v" / "x").read_bytes() == b"data"


def test_copy_elf_folder_no_elf_dir_raises(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # No elf/ subdir created — only meta.json is present.
    (out_dir / "meta.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        copy_elf_folder(out_dir, tmp_path / "dataset", "v")


def test_copy_elf_folder_out_path_not_a_dir_raises(tmp_path):
    out_path = tmp_path / "not-a-dir"
    out_path.write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        copy_elf_folder(out_path, tmp_path / "dataset", "v")


def test_copy_elf_folder_dereferences_symlinks(tmp_path):
    """ELF entries are symlinks into /nix/store; we want the bytes copied."""
    real_target = tmp_path / "real" / "binary"
    real_target.parent.mkdir(parents=True)
    real_target.write_bytes(b"real-elf-bytes")
    out_dir = tmp_path / "out"
    elf_dir = out_dir / "elf"
    elf_dir.mkdir(parents=True)
    (elf_dir / "binary").symlink_to(real_target)
    dest_dir = tmp_path / "dataset"
    staged = copy_elf_folder(out_dir, dest_dir, "v")
    final = dest_dir / "v" / "binary"
    assert final in staged
    # Must be regular bytes, not a symlink — destination FS may not be able
    # to resolve into /nix/store.
    assert not final.is_symlink()
    assert final.read_bytes() == b"real-elf-bytes"


def test_copy_elf_folder_overwrites_stale_tmp(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_elf_folder(out_dir, {"x": b"new"})
    dest_dir = tmp_path / "dataset" / "v"
    dest_dir.mkdir(parents=True)
    # Simulate a leftover from a crashed prior run.
    (dest_dir / "x.tmp").write_bytes(b"old-stale")
    staged = copy_elf_folder(out_dir, tmp_path / "dataset", "v")
    assert (dest_dir / "x").read_bytes() == b"new"
    assert staged == [dest_dir / "x"]


def test_copy_elf_folder_atomic_replace(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_elf_folder(out_dir, {"x": b"v2"})
    dest_dir = tmp_path / "dataset" / "v"
    dest_dir.mkdir(parents=True)
    # Pre-existing file at the final destination.
    (dest_dir / "x").write_bytes(b"v1")
    copy_elf_folder(out_dir, tmp_path / "dataset", "v")
    assert (dest_dir / "x").read_bytes() == b"v2"


# ---------------------------------------------------------------------------
# build_worker — happy paths
# ---------------------------------------------------------------------------


def test_build_worker_phase2_toolchain_happy(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN,
        name="gcc15-x86_64-linux-aarch64",
        payload={"attr": "_crossToolchainMap.x86_64-linux.aarch64.gcc15"},
    )
    runner = RecordingRunner(stdout=b"/nix/store/aaa-toolchain\n")
    clock = FakeClock(start=10.0, step=2.5)
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        clock=clock,
    )
    result = build_worker(manifest, env)
    assert isinstance(result, BuildWorkerResult)
    assert result.success is True
    assert result.error is None
    assert result.output_path is None
    assert result.item_class == ITEM_CLASS_PHASE2_TOOLCHAIN
    assert result.name == "gcc15-x86_64-linux-aarch64"
    assert result.duration_seconds > 0.0
    # Toolchains do NOT pass --skip-existing.
    argv = runner.calls[0]
    assert "--skip-existing" not in argv


def test_build_worker_phase2_common_dep_passes_skip_existing(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_COMMON_DEP,
        name="some-common-dep",
        payload={"attr": "_drvDeps.foo"},
    )
    runner = RecordingRunner(stdout=b"/nix/store/bbb\n")
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
    )
    result = build_worker(manifest, env)
    assert result.success is True
    argv = runner.calls[0]
    assert "--skip-existing" in argv


def test_build_worker_phase3_variant_copies_elf_folder(tmp_path):
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"hello": b"elf-bytes"})
    variant_dir = "hello__x86_64__gcc15__O2"
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name=variant_dir,
        payload={
            "attr": "dataset.x86_64-linux.hello.x86_64.gcc15.O2",
            "variant_dir": variant_dir,
            "pkg": "hello",
        },
    )
    runner = RecordingRunner(
        stdout=f"some-warning\n{out_dir}\n".encode("utf-8"),
    )
    dataset = tmp_path / "dataset"
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=dataset,
        run_subprocess=runner,
    )
    result = build_worker(manifest, env)
    assert result.success is True
    expected_subdir = dataset / "hello" / variant_dir
    assert result.output_path == expected_subdir
    assert (expected_subdir / "hello").read_bytes() == b"elf-bytes"


def test_build_worker_phase3_variant_uses_last_stdout_line(tmp_path):
    """If nix prints diagnostics first, the realised path is the LAST line."""
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"bin": b"v"})
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="v",
        payload={"attr": "v", "variant_dir": "v", "pkg": "p"},
    )
    runner = RecordingRunner(
        stdout=f"warning: some-warning\n{out_dir}\n\n".encode("utf-8"),
    )
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
    )
    result = build_worker(manifest, env)
    assert result.success is True
    assert result.output_path == tmp_path / "dataset" / "p" / "v"


# ---------------------------------------------------------------------------
# build_worker — peer integration
# ---------------------------------------------------------------------------


def test_build_worker_with_substituters_file_includes_peer_args(tmp_path):
    peer_args = [
        "--extra-substituters",
        "http://h:5000",
        "--extra-trusted-public-keys",
        "k",
        "--substitute-on-destination",
    ]
    substituters = _write_substituters_file(tmp_path, peer_args)
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN,
        name="gcc15",
        payload={"attr": "x"},
    )
    runner = RecordingRunner()
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        substituters_file=substituters,
        run_subprocess=runner,
    )
    build_worker(manifest, env)
    argv = runner.calls[0]
    for a in peer_args:
        assert a in argv


# ---------------------------------------------------------------------------
# build_worker — failure paths
# ---------------------------------------------------------------------------


def test_build_worker_failure_captures_stderr(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN,
        name="bad",
        payload={"attr": "broken"},
    )
    err_lines = [f"line {i}" for i in range(200)]
    err_bytes = ("\n".join(err_lines)).encode("utf-8")
    runner = RecordingRunner(returncode=1, stderr=err_bytes)
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        log_excerpt_lines=80,
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert result.error is not None
    assert "non-zero" in result.error
    assert result.nix_log_excerpt is not None
    # Must keep the *tail* of the log.
    assert "line 199" in result.nix_log_excerpt
    # And cap to ~80 lines.
    assert result.nix_log_excerpt.count("\n") < 80 + 5
    assert "line 0" not in result.nix_log_excerpt


def test_build_worker_does_not_raise_on_nonzero(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN,
        name="bad",
        payload={"attr": "broken"},
    )
    runner = RecordingRunner(returncode=42, stderr=b"oops")
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
    )
    # Must not raise.
    result = build_worker(manifest, env)
    assert result.success is False


def test_build_worker_does_not_raise_on_subprocess_exception(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN,
        name="x",
        payload={"attr": "x"},
    )

    def crash(_argv: list[str]) -> tuple[bytes, bytes, int]:
        raise OSError("simulated launch failure")

    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=crash,
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert result.error is not None
    assert "crashed" in result.error


def test_build_worker_phase3_missing_elf_folder(tmp_path):
    out_dir = tmp_path / "nix-out"
    out_dir.mkdir()
    # No elf/ subdir placed inside — copy_elf_folder will raise.
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="nope",
        payload={"attr": "x", "variant_dir": "nope", "pkg": "p"},
    )
    runner = RecordingRunner(stdout=f"{out_dir}\n".encode("utf-8"))
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert "elf folder copy failed" in (result.error or "")


def test_build_worker_phase3_missing_variant_dir(tmp_path):
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"x": b"v"})
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="v",
        payload={"attr": "x"},  # no variant_dir
    )
    runner = RecordingRunner(stdout=f"{out_dir}\n".encode("utf-8"))
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert "variant_dir" in (result.error or "")


def test_build_worker_bad_manifest_returns_failure(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{ not json", encoding="utf-8")
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
    )
    result = build_worker(bad, env)
    assert result.success is False
    assert "manifest parse failed" in (result.error or "")


def test_build_worker_missing_payload_attr(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN,
        name="x",
        payload={},  # no attr
    )
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert "attr" in (result.error or "")


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_exports():
    assert hasattr(bw, "BuildWorkerResult")
    assert hasattr(bw, "BuildWorkerEnv")
    assert hasattr(bw, "parse_build_manifest")
    assert hasattr(bw, "build_attr")
    assert hasattr(bw, "copy_elf_folder")
    assert hasattr(bw, "build_worker")
    assert ITEM_CLASS_PHASE2_TOOLCHAIN in bw.VALID_ITEM_CLASSES
    assert ITEM_CLASS_PHASE2_TOOLCHAIN_VALIDATE in bw.VALID_ITEM_CLASSES
    assert ITEM_CLASS_PHASE2_COMMON_DEP in bw.VALID_ITEM_CLASSES
    assert ITEM_CLASS_PHASE3_VARIANT in bw.VALID_ITEM_CLASSES


# ---------------------------------------------------------------------------
# phase2_toolchain_validate dispatch
# ---------------------------------------------------------------------------


class _ScriptedRunner:
    """argv-keyed scripted run_subprocess.

    Each call appends to ``calls`` and consumes one tuple from ``script``.
    The script is a list of ``(stdout, stderr, rc)`` tuples consumed in
    order, regardless of argv — matches the wire shape of
    ``peer_paths_fetch.fetch_from_peer`` / ``is_path_locally_valid``
    where every call is a distinct nix subcommand."""

    def __init__(self, script: list[tuple[bytes, bytes, int]]) -> None:
        self._script = list(script)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        if not self._script:
            raise AssertionError(
                f"runner script exhausted at call #{len(self.calls)}: "
                f"argv={argv}"
            )
        return self._script.pop(0)


def test_validate_toolchain_records_placement_when_already_local(tmp_path):
    """If ``nix path-info`` says we already have it, the worker
    records the placement and returns success without fetching."""
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN_VALIDATE,
        name="toolchain_validate__aarch64__gcc15",
        payload={
            "drv": "/nix/store/tc.drv",
            "outpath": "/nix/store/aaa-toolchain",
            "validate_only": True,
        },
    )
    # Only ``nix path-info`` runs; no ``nix copy``.
    runner = _ScriptedRunner([(b"valid\n", b"", 0)])
    shared_fs = tmp_path / "shared"
    shared_fs.mkdir()
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        shared_fs=shared_fs,
        secondary_id="sec1",
    )
    result = build_worker(manifest, env)
    assert result.success is True
    assert result.outpath == "/nix/store/aaa-toolchain"
    assert result.drv == "/nix/store/tc.drv"
    # Exactly one call (path-info); no copy.
    assert len(runner.calls) == 1
    assert "path-info" in runner.calls[0]

    # Placement recorded locally so the next variant finds us.
    from compiler_suit_runner.peer_paths import list_self_placements
    placements = list_self_placements(shared_fs, "sec1")
    assert len(placements) == 1
    assert placements[0].outpath == "/nix/store/aaa-toolchain"
    assert placements[0].item_class == "toolchain"


def test_validate_toolchain_fetches_from_peer_when_missing(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN_VALIDATE,
        name="toolchain_validate__aarch64__gcc15",
        payload={
            "drv": "/nix/store/tc.drv",
            "outpath": "/nix/store/aaa-toolchain",
        },
    )
    # path-info: miss → nix copy: hit.
    runner = _ScriptedRunner(
        [
            (b"", b"", 1),  # path-info: not local
            (b"", b"", 0),  # nix copy: succeeded
        ]
    )
    shared_fs = tmp_path / "shared"
    shared_fs.mkdir()
    # Seed the on-disk placement gossip so _resolve_placements finds
    # sec2 as a candidate (the in-process watcher is not started in this test).
    (shared_fs / "peers").mkdir()
    (shared_fs / "peers" / "sec2.json").write_text(
        json.dumps({
            "secondary_id": "sec2",
            "hostname": "node2",
            "port": 5002,
            "public_key": "k2:Z",
        })
    )
    (shared_fs / "peers" / "_paths_sec2.jsonl").write_text(
        json.dumps({
            "secondary_id": "sec2",
            "outpath": "/nix/store/aaa-toolchain",
        }) + "\n"
    )
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        shared_fs=shared_fs,
        secondary_id="sec1",
    )
    result = build_worker(manifest, env)
    assert result.success is True, result.error
    # One path-info + one nix copy.
    assert len(runner.calls) == 2
    copy_argv = runner.calls[1]
    assert "copy" in copy_argv
    # ``--no-substituters`` is intentionally NOT here: it's invalid
    # for the ``copy`` subcommand. The ``--from`` URL pin already
    # restricts the source so no fanout occurs.
    assert "--no-substituters" not in copy_argv
    assert "--no-check-sigs" in copy_argv
    # Source was sec2 (only candidate in map).
    assert any("node2" in a for a in copy_argv)


def test_validate_toolchain_fails_when_no_peer_has_it(tmp_path):
    """A toolchain that's missing locally AND not in the placement
    map is fatal — every variant downstream needs it. The worker
    surfaces a clear error for the operator."""
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN_VALIDATE,
        name="toolchain_validate__aarch64__gcc15",
        payload={
            "drv": "/nix/store/tc.drv",
            "outpath": "/nix/store/aaa-toolchain",
        },
    )
    runner = _ScriptedRunner([(b"", b"", 1)])  # path-info miss; no copy
    shared_fs = tmp_path / "shared"
    shared_fs.mkdir()
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        shared_fs=shared_fs,
        secondary_id="sec1",
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert "no peer in the placement map" in (result.error or "")
    assert result.outpath == "/nix/store/aaa-toolchain"


def test_validate_toolchain_missing_outpath_in_payload(tmp_path):
    """The primary's manifest emission MUST carry ``outpath``; if it
    didn't (drv eval succeeded but outpath lookup failed), the worker
    cannot do a targeted fetch and must surface the gap rather than
    silently fall through to nix's substituters."""
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN_VALIDATE,
        name="v",
        payload={"drv": "/nix/store/tc.drv"},  # no outpath
    )
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert "outpath" in (result.error or "")


# ---------------------------------------------------------------------------
# Common-dep success hook + variant pre-fetch
# ---------------------------------------------------------------------------


def test_common_dep_success_records_placement(tmp_path):
    """After a successful common-dep build, the worker must append a
    placement record so peers can fetch this dep from us instead of
    rebuilding."""
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_COMMON_DEP,
        name="glibc",
        payload={
            "drv": "/nix/store/glibc.drv",
            "label": "glibc",
            "attr": "/nix/store/glibc.drv",
        },
    )
    realised = "/nix/store/abc-glibc-2.38"
    runner = RecordingRunner(stdout=f"{realised}\n".encode("utf-8"))
    shared_fs = tmp_path / "shared"
    shared_fs.mkdir()
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        shared_fs=shared_fs,
        secondary_id="sec1",
    )
    result = build_worker(manifest, env)
    assert result.success is True
    assert result.outpath == realised

    from compiler_suit_runner.peer_paths import list_self_placements
    placements = list_self_placements(shared_fs, "sec1")
    assert len(placements) == 1
    assert placements[0].outpath == realised
    assert placements[0].item_class == "common_dep"


def test_toolchain_build_success_records_placement_as_toolchain(tmp_path):
    """Opt-in toolchain build path also records placement, with the
    ``toolchain`` item_class so future fetches can prefer toolchain
    candidates appropriately if we ever want that policy."""
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE2_TOOLCHAIN,
        name="gcc15-aarch64",
        payload={"attr": "x", "drv": "/nix/store/tc.drv"},
    )
    realised = "/nix/store/xyz-toolchain"
    runner = RecordingRunner(stdout=f"{realised}\n".encode("utf-8"))
    shared_fs = tmp_path / "shared"
    shared_fs.mkdir()
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        shared_fs=shared_fs,
        secondary_id="sec1",
    )
    result = build_worker(manifest, env)
    assert result.success is True

    from compiler_suit_runner.peer_paths import list_self_placements
    placements = list_self_placements(shared_fs, "sec1")
    assert len(placements) == 1
    assert placements[0].item_class == "toolchain"


def test_variant_prefetch_issues_targeted_copy_per_known_input(tmp_path):
    """Before invoking ``nix build`` for a variant, the worker
    pre-fetches every input the placement map knows about. Inputs
    NOT in the map are skipped (nix's native substituters handle them)."""
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"hello": b"v"})
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="hello-x86_64-gcc15-O2",
        payload={
            "attr": "x",
            "variant_dir": "hello-x86_64-gcc15-O2",
            "pkg": "hello",
            "drv": "/nix/store/v.drv",
            "input_drvs": [
                "/nix/store/in-known.drv",
                "/nix/store/in-unknown.drv",
            ],
            "input_outpaths": {
                "/nix/store/in-known.drv": "/nix/store/in-known-out",
                "/nix/store/in-unknown.drv": "/nix/store/in-unknown-out",
            },
        },
    )
    # Script:
    #   1) fetch_from_peer for the known input — path-info miss → nix copy hit
    #      (the unknown input is skipped before path-info because it's not
    #      in the placement map at all).
    #   2) the variant build itself.
    runner = _ScriptedRunner(
        [
            (b"", b"", 1),  # path-info for in-known-out → not local
            (b"", b"", 0),  # nix copy for in-known-out  → success
            (f"{out_dir}\n".encode("utf-8"), b"", 0),  # variant nix build
        ]
    )
    shared_fs = tmp_path / "shared"
    (shared_fs / "peers").mkdir(parents=True)
    (shared_fs / "peers" / "sec2.json").write_text(
        json.dumps({
            "secondary_id": "sec2",
            "hostname": "node2",
            "port": 5002,
            "public_key": "k2:Z",
        })
    )
    # Only ``in-known-out`` is in the placement map.
    (shared_fs / "peers" / "_paths_sec2.jsonl").write_text(
        json.dumps({
            "secondary_id": "sec2",
            "outpath": "/nix/store/in-known-out",
        }) + "\n"
    )
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        shared_fs=shared_fs,
        secondary_id="sec1",
    )
    result = build_worker(manifest, env)
    assert result.success is True, result.error
    # Three calls total: path-info + nix copy + variant build.
    assert len(runner.calls) == 3
    # The targeted copy is for the known input (no fanout, single peer).
    copy_argv = runner.calls[1]
    assert "copy" in copy_argv
    assert "--no-check-sigs" in copy_argv
    # No ``--no-substituters`` — invalid for ``nix copy``; the
    # explicit ``--from`` URL is what pins the source.
    assert "--no-substituters" not in copy_argv
    # The realised variant out-path should be picked up correctly.
    assert result.output_path == tmp_path / "dataset" / "hello" / "hello-x86_64-gcc15-O2"


def test_variant_prefetch_skipped_without_placement_plumbing(tmp_path):
    """Single-process / cache-restore flows: no shared_fs, no
    pre-fetch — the worker must fall straight through to ``nix build``."""
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"hello": b"v"})
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="v",
        payload={
            "attr": "x",
            "variant_dir": "v",
            "pkg": "p",
            "input_drvs": ["/nix/store/d.drv"],
            "input_outpaths": {"/nix/store/d.drv": "/nix/store/d-out"},
        },
    )
    runner = _ScriptedRunner(
        [(f"{out_dir}\n".encode("utf-8"), b"", 0)]  # only the variant build
    )
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        # shared_fs left as None — no placement plumbing.
    )
    result = build_worker(manifest, env)
    assert result.success is True
    # Only the variant build ran; no path-info, no copy.
    assert len(runner.calls) == 1
    assert "build" in runner.calls[0]
