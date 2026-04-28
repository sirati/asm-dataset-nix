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
    ITEM_CLASS_PHASE3_VARIANT,
    BuildWorkerEnv,
    BuildWorkerResult,
    build_attr,
    build_worker,
    copy_tarball,
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


class StubPeerWatcher:
    """Mimics PeerListWatcher.extra_args without spinning a thread."""

    def __init__(self, extra_args: list[str]) -> None:
        self._extra_args = list(extra_args)

    @property
    def extra_args(self) -> list[str]:
        return list(self._extra_args)


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
    watcher = StubPeerWatcher(peer_args)
    runner = RecordingRunner()
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path,
        peer_watcher=watcher,
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
# copy_tarball
# ---------------------------------------------------------------------------


def test_copy_tarball_from_directory(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    src = out_dir / "build-output.tar.zst"
    src.write_bytes(b"zstd-payload")
    dest_dir = tmp_path / "dataset"
    name = "hello__x86_64__gcc15__O2.tar.zst"
    final = copy_tarball(out_dir, dest_dir, name)
    assert final == dest_dir / name
    assert final.read_bytes() == b"zstd-payload"


def test_copy_tarball_from_file(tmp_path):
    src = tmp_path / "single.tar.zst"
    src.write_bytes(b"x")
    dest_dir = tmp_path / "dataset"
    name = "renamed.tar.zst"
    final = copy_tarball(src, dest_dir, name)
    assert final.read_bytes() == b"x"
    assert (dest_dir / name).is_file()


def test_copy_tarball_creates_dest_dir(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "a.tar.zst").write_bytes(b"data")
    dest_dir = tmp_path / "deep" / "nested" / "dataset"
    final = copy_tarball(out_dir, dest_dir, "x.tar.zst")
    assert final.exists()
    assert dest_dir.is_dir()


def test_copy_tarball_no_tarball_raises(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "not-a-tarball.txt").write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        copy_tarball(out_dir, tmp_path / "dataset", "x.tar.zst")


def test_copy_tarball_overwrites_stale_tmp(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "a.tar.zst").write_bytes(b"new")
    dest_dir = tmp_path / "dataset"
    dest_dir.mkdir()
    # Simulate a leftover from a crashed prior run.
    stale = dest_dir / "x.tar.zst.tmp"
    stale.write_bytes(b"old-stale")
    final = copy_tarball(out_dir, dest_dir, "x.tar.zst")
    assert final.read_bytes() == b"new"


def test_copy_tarball_atomic_replace(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "a.tar.zst").write_bytes(b"v2")
    dest_dir = tmp_path / "dataset"
    dest_dir.mkdir()
    # Pre-existing file at the final destination.
    (dest_dir / "x.tar.zst").write_bytes(b"v1")
    final = copy_tarball(out_dir, dest_dir, "x.tar.zst")
    assert final.read_bytes() == b"v2"


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


def test_build_worker_phase3_variant_copies_tarball(tmp_path):
    out_dir = tmp_path / "nix-out"
    out_dir.mkdir()
    (out_dir / "build.tar.zst").write_bytes(b"variant-data")
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="hello__x86_64__gcc15__O2",
        payload={
            "attr": "dataset.x86_64-linux.hello.x86_64.gcc15.O2",
            "tarball_name": "hello__x86_64__gcc15__O2.tar.zst",
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
    expected = dataset / "hello__x86_64__gcc15__O2.tar.zst"
    assert result.output_path == expected
    assert expected.read_bytes() == b"variant-data"


def test_build_worker_phase3_variant_uses_last_stdout_line(tmp_path):
    """If nix prints diagnostics first, the realised path is the LAST line."""
    out_dir = tmp_path / "nix-out"
    out_dir.mkdir()
    (out_dir / "x.tar.zst").write_bytes(b"v")
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="v",
        payload={"attr": "v", "tarball_name": "v.tar.zst"},
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
    assert result.output_path == tmp_path / "dataset" / "v.tar.zst"


# ---------------------------------------------------------------------------
# build_worker — peer integration
# ---------------------------------------------------------------------------


def test_build_worker_with_peer_watcher_includes_peer_args(tmp_path):
    peer_args = [
        "--extra-substituters",
        "http://h:5000",
        "--extra-trusted-public-keys",
        "k",
        "--substitute-on-destination",
    ]
    watcher = StubPeerWatcher(peer_args)
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
        peer_watcher=watcher,
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


def test_build_worker_phase3_missing_tarball(tmp_path):
    out_dir = tmp_path / "nix-out"
    out_dir.mkdir()
    # No *.tar.zst placed inside.
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="nope",
        payload={"attr": "x", "tarball_name": "nope.tar.zst"},
    )
    runner = RecordingRunner(stdout=f"{out_dir}\n".encode("utf-8"))
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert "tarball copy failed" in (result.error or "")


def test_build_worker_phase3_missing_tarball_name(tmp_path):
    out_dir = tmp_path / "nix-out"
    out_dir.mkdir()
    (out_dir / "x.tar.zst").write_bytes(b"v")
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_PHASE3_VARIANT,
        name="v",
        payload={"attr": "x"},  # no tarball_name
    )
    runner = RecordingRunner(stdout=f"{out_dir}\n".encode("utf-8"))
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
    )
    result = build_worker(manifest, env)
    assert result.success is False
    assert "tarball_name" in (result.error or "")


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
    assert hasattr(bw, "copy_tarball")
    assert hasattr(bw, "build_worker")
    assert ITEM_CLASS_PHASE2_TOOLCHAIN in bw.VALID_ITEM_CLASSES
    assert ITEM_CLASS_PHASE2_COMMON_DEP in bw.VALID_ITEM_CLASSES
    assert ITEM_CLASS_PHASE3_VARIANT in bw.VALID_ITEM_CLASSES
