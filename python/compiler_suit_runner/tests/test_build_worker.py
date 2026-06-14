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
    ITEM_CLASS_BUILD_COMMON_DEP,
    ITEM_CLASS_BUILD_VARIANT,
    ITEM_CLASS_TOOLCHAIN_VALIDATE,
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
        ITEM_CLASS_BUILD_COMMON_DEP,
        ITEM_CLASS_BUILD_VARIANT,
        ITEM_CLASS_TOOLCHAIN_VALIDATE,
    ],
)
def test_parse_build_manifest_accepts_all_known_classes(tmp_path, item_class):
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


_TUNNEL_BLIP_STDERR = (
    b"error: unable to download 'http://localhost:5005/nar/1abc.nar.zst':"
    b" Could not connect to server (7) Failed to connect to localhost"
    b" port 5005 after 0 ms\n"
    b"error: path '/nix/store/aaa-compiler-rt-libc-12.0.1' is required,"
    b" but there is no substituter that can build it\n"
    b"error: some references of path"
    b" '/nix/store/bbb-clang-wrapper-12.0.1' could not be realised\n"
)


def test_build_attr_retries_substituter_connect_failure_then_succeeds(
    monkeypatch, tmp_path,
):
    """A peer-substituter connect failure (dropped SSH forward) is
    TRANSIENT: build_attr retries on the backoff ladder and returns
    success once the forward is back, instead of surfacing the failure
    to the caller."""
    sleeps: list[float] = []
    monkeypatch.setattr(bw, "_retry_sleep", sleeps.append)

    outcomes = [
        (b"", _TUNNEL_BLIP_STDERR, 1),
        (b"", _TUNNEL_BLIP_STDERR, 1),
        (b"/nix/store/xxx-out\n", b"", 0),
    ]
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return outcomes.pop(0)

    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path,
        run_subprocess=runner,
    )
    success, stdout, _ = build_attr("hello", env)
    assert success is True
    assert stdout == b"/nix/store/xxx-out\n"
    assert len(calls) == 3
    # Backoff schedule 2/4s, each plus up to 25% jitter.
    assert len(sleeps) == 2
    assert 2.0 <= sleeps[0] <= 2.5
    assert 4.0 <= sleeps[1] <= 5.0


def test_build_attr_missing_path_without_connect_error_is_permanent(
    monkeypatch, tmp_path,
):
    """A genuine "no substituter that can build it" (no connect-failure
    marker in stderr) fails fast — no retries, no sleeps."""
    sleeps: list[float] = []
    monkeypatch.setattr(bw, "_retry_sleep", sleeps.append)

    stderr = (
        b"error: path '/nix/store/aaa-compiler-rt-libc-12.0.1' is"
        b" required, but there is no substituter that can build it\n"
        b"error: some references of path"
        b" '/nix/store/bbb-clang-wrapper-12.0.1' could not be realised\n"
    )
    runner = RecordingRunner(returncode=1, stderr=stderr)
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path,
        run_subprocess=runner,
    )
    success, _, out_stderr = build_attr("hello", env)
    assert success is False
    assert out_stderr == stderr
    assert len(runner.calls) == 1
    assert sleeps == []


def test_build_attr_substituter_retries_exhausted_fails(
    monkeypatch, tmp_path,
):
    """A connect failure that never heals exhausts the ladder and the
    failure is surfaced with the original stderr."""
    sleeps: list[float] = []
    monkeypatch.setattr(bw, "_retry_sleep", sleeps.append)

    runner = RecordingRunner(returncode=1, stderr=_TUNNEL_BLIP_STDERR)
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path,
        run_subprocess=runner,
    )
    success, _, out_stderr = build_attr("hello", env)
    assert success is False
    assert out_stderr == _TUNNEL_BLIP_STDERR
    assert len(runner.calls) == bw._SUBSTITUTER_MAX_ATTEMPTS
    assert len(sleeps) == bw._SUBSTITUTER_MAX_ATTEMPTS - 1
    # Full ladder 2/4/8/16/32/32s, each plus up to 25% jitter — sized
    # to outlast a forward rebuild.
    for slept, base in zip(sleeps, bw._SUBSTITUTER_BACKOFF_SECONDS):
        assert base <= slept <= base * 1.25


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


def test_build_worker_build_common_dep_uses_plain_nix_build(tmp_path):
    # The descriptor ships a bare store basename; the worker reconstructs
    # the absolute /nix/store/<ident> drv path. matrix_eval_out_dir is
    # left unset so the archive-import prelude no-ops, isolating the
    # path-reconstruction behaviour.
    ident = "bvds533r8bjg379a68m3skkpp1giy0hh-source.drv"
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
        name="some-common-dep",
        payload={"attr": ident, "binary": "hello"},
    )
    runner = RecordingRunner(stdout=b"/nix/store/bbb\n")
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
    assert result.item_class == ITEM_CLASS_BUILD_COMMON_DEP
    assert result.name == "some-common-dep"
    assert result.duration_seconds > 0.0
    argv = runner.calls[0]
    # Built by absolute drv path (`nix build /nix/store/<ident>^*`), not a
    # flake ref; and the bogus --skip-existing flag must NOT be present.
    assert f"/nix/store/{ident}^*" in argv
    assert "--skip-existing" not in argv


def test_build_worker_build_common_dep_non_drv_ident(tmp_path):
    # Regression: script/file common-dep idents that lack a ``.drv`` suffix
    # (e.g. ``builder.sh``, ``validate-pkg-config.sh``) must reconstruct the
    # absolute /nix/store/ path and build by store path — NOT fall through
    # to the flake-ref form (which needs a flake.nix on the secondary /app,
    # which is absent → ``could not find a flake.nix file`` NonRecoverable).
    ident = "119w10l5ic8izi6igxqpq0wdmccs9mbc-builder.sh"
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
        name="some-script-dep",
        payload={"attr": ident, "binary": "hello"},
    )
    runner = RecordingRunner(stdout=b"/nix/store/bbb\n")
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
    argv = runner.calls[0]
    # Built by absolute store path, NOT a flake ref, and NOT with ``^*``
    # (it is a realised store path, not a .drv).
    assert f"/nix/store/{ident}" in argv
    assert f"/nix/store/{ident}^*" not in argv
    assert f".#{ident}" not in argv


def test_build_worker_build_variant_copies_elf_folder(tmp_path):
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"hello": b"elf-bytes"})
    variant_dir = "hello__x86_64__gcc15__O2"
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_BUILD_VARIANT,
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


def test_build_worker_build_variant_uses_last_stdout_line(tmp_path):
    """If nix prints diagnostics first, the realised path is the LAST line."""
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"bin": b"v"})
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_BUILD_VARIANT,
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
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
        name="glibc",
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
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
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
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
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
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
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


def test_build_worker_build_variant_missing_elf_folder(tmp_path):
    out_dir = tmp_path / "nix-out"
    out_dir.mkdir()
    # No elf/ subdir placed inside — copy_elf_folder will raise.
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_BUILD_VARIANT,
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


def test_build_worker_build_variant_missing_variant_dir(tmp_path):
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"x": b"v"})
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_BUILD_VARIANT,
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
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
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
    assert ITEM_CLASS_TOOLCHAIN_VALIDATE in bw.VALID_ITEM_CLASSES
    assert ITEM_CLASS_BUILD_COMMON_DEP in bw.VALID_ITEM_CLASSES
    assert ITEM_CLASS_BUILD_VARIANT in bw.VALID_ITEM_CLASSES


# ---------------------------------------------------------------------------
# toolchain_validate dispatch
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
        item_class=ITEM_CLASS_TOOLCHAIN_VALIDATE,
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
        item_class=ITEM_CLASS_TOOLCHAIN_VALIDATE,
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
        item_class=ITEM_CLASS_TOOLCHAIN_VALIDATE,
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
        item_class=ITEM_CLASS_TOOLCHAIN_VALIDATE,
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
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
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


def test_variant_prefetch_issues_targeted_copy_per_known_input(tmp_path):
    """Before invoking ``nix build`` for a variant, the worker
    pre-fetches every input the placement map knows about. Inputs
    NOT in the map are skipped (nix's native substituters handle them)."""
    out_dir = tmp_path / "nix-out"
    _make_elf_folder(out_dir, {"hello": b"v"})
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_BUILD_VARIANT,
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
        item_class=ITEM_CLASS_BUILD_VARIANT,
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


# ---------------------------------------------------------------------------
# Subprocess entry point — main + handle closure (unified dispatch)
#
# The framework picks ONE ``worker_module`` per secondary pool, so
# ``build_worker.main`` must dispatch both matrix_eval payloads
# (-> :func:`eval_worker.run_eval_task`) and the original build
# manifests (-> :func:`build_worker.build_worker`). The handle closure
# sniffs ``task.payload['item_class']`` to decide which branch fires.
# ---------------------------------------------------------------------------


def _run_build_worker_main_with_capture(
    monkeypatch,
    argv: list[str],
):
    """Invoke ``build_worker.main()`` with framework dependencies
    monkey-patched so the test never opens a real socket or thread.

    Returns ``(handle, run_mock)`` where ``handle`` is the closure
    the worker registered with the framework.
    """
    import sys as _sys
    import types as _types
    from unittest.mock import MagicMock

    fake_worker = _types.ModuleType("dynamic_runner.worker")

    class FakeTask:
        def __init__(
            self,
            payload=None,
            task_id: str = "matrix_eval__hello",
            relative_path: str = "",
            resolved_path: str = "",
        ) -> None:
            self.payload = payload or {}
            self.task_id = task_id
            self.relative_path = relative_path
            self.resolved_path = resolved_path
            self.publish_calls: list[tuple] = []

        def publish_all(self, *args):
            self.publish_calls.append(args)

    class FakeWorkerOutput:
        def __init__(self) -> None:
            pass

    class FakeNonRecoverable(Exception):
        pass

    class FakePublishError(Exception):
        pass

    run_mock = MagicMock()
    fake_worker.Task = FakeTask
    fake_worker.WorkerOutput = FakeWorkerOutput
    fake_worker.NonRecoverableError = FakeNonRecoverable
    fake_worker.PublishError = FakePublishError
    fake_worker.run = run_mock

    if "dynamic_runner" not in _sys.modules:
        _sys.modules["dynamic_runner"] = _types.ModuleType("dynamic_runner")
    monkeypatch.setitem(_sys.modules, "dynamic_runner.worker", fake_worker)

    monkeypatch.setattr(_sys, "argv", ["build_worker", *argv])

    rc = bw.main()
    assert rc == 0
    run_mock.assert_called_once()
    captured_handle = run_mock.call_args.args[0]
    return captured_handle, run_mock


def _matrix_eval_wrapper_payload(
    *,
    binary: str = "hello",
    sys_name: str = "x86_64-linux",
) -> dict:
    """Return a header_dict wrapper as :class:`SuitTask._header_to_task_info`
    would emit — ``payload`` field is the inner matrix_eval data."""
    return {
        "item_class": "matrix_eval",
        "name": f"matrix_eval__{binary}",
        "size": 1,
        "payload": {
            "binary": binary,
            "sys": sys_name,
            "archs": ["x86_64"],
            "suffixes": ["O0", "O2"],
            "attr": f"dataset.{sys_name}.{binary}",
        },
    }


def test_main_handle_dispatches_matrix_eval_to_run_eval_task(
    monkeypatch, tmp_path
):
    """A task whose ``item_class == 'matrix_eval'`` is routed to
    :func:`eval_worker.run_eval_task` with the inner payload and the
    matrix-eval-out-dir; the per-drv broadcast sender has been
    retired so no third argument is threaded through."""
    handle, _ = _run_build_worker_main_with_capture(
        monkeypatch,
        [
            "--socket-path", str(tmp_path / "sock"),
            "--flake-ref", ".",
            "--dataset-output-dir", str(tmp_path / "dataset"),
            "--shared-fs", str(tmp_path),
            "--matrix-eval-out-dir", str(tmp_path / "phase0"),
            "--secondary-id", "sec1",
            "--signing-public-key", "k:abc",
        ],
    )

    captured: dict = {}

    def _fake_run_eval(payload, *, out_dir, task):
        captured["payload"] = payload
        captured["out_dir"] = out_dir
        captured["task"] = task
        return {"ok": True}

    # Patch on the eval_worker module (build_worker.main late-imports
    # ``run_eval_task`` from there).
    from compiler_suit_runner.workers import eval_worker as ew

    monkeypatch.setattr(ew, "run_eval_task", _fake_run_eval)

    import sys as _sys
    fake_task_cls = _sys.modules["dynamic_runner.worker"].Task
    fake_output_cls = _sys.modules["dynamic_runner.worker"].WorkerOutput

    wrapper = _matrix_eval_wrapper_payload(binary="hello")
    task = fake_task_cls(payload=wrapper)
    output = handle(task)
    assert isinstance(output, fake_output_cls)
    # The inner payload was unwrapped before dispatch.
    assert captured["payload"] == wrapper["payload"]
    assert "broadcast_sender" not in captured
    assert captured["out_dir"] == tmp_path / "phase0"


def test_main_handle_dispatches_build_manifest_to_build_worker(
    monkeypatch, tmp_path
):
    """A task whose payload is NOT a matrix_eval wrapper (e.g. a
    toolchain manifest) is routed to the existing build_worker path,
    NOT to run_eval_task."""
    handle, _ = _run_build_worker_main_with_capture(
        monkeypatch,
        [
            "--socket-path", str(tmp_path / "sock"),
            "--flake-ref", ".",
            "--dataset-output-dir", str(tmp_path / "dataset"),
        ],
    )

    eval_called: list[bool] = []
    build_called: list[dict] = []

    from compiler_suit_runner.workers import eval_worker as ew

    def _fake_run_eval(*args, **kwargs):
        eval_called.append(True)
        return {}

    monkeypatch.setattr(ew, "run_eval_task", _fake_run_eval)

    def _fake_build_worker(manifest_path, env, *, manifest_data=None):
        build_called.append({
            "manifest_path": manifest_path,
            "manifest_data": manifest_data,
        })
        return bw.BuildWorkerResult(
            item_class=ITEM_CLASS_BUILD_COMMON_DEP,
            name="cd",
            success=True,
            duration_seconds=0.0,
            outpath="/nix/store/x",
        )

    monkeypatch.setattr(bw, "build_worker", _fake_build_worker)

    import sys as _sys
    fake_task_cls = _sys.modules["dynamic_runner.worker"].Task
    fake_output_cls = _sys.modules["dynamic_runner.worker"].WorkerOutput

    # A common-dep manifest wrapper (item_class is one of the build
    # classes). The handle closure must NOT treat this as matrix_eval.
    build_payload = {
        "item_class": ITEM_CLASS_BUILD_COMMON_DEP,
        "name": "cd",
        "payload": {"attr": "x.cd", "drv": "/nix/store/cd.drv"},
    }
    task = fake_task_cls(payload=build_payload, relative_path="m.json")
    output = handle(task)
    assert isinstance(output, fake_output_cls)
    assert not eval_called, "build manifest must NOT reach run_eval_task"
    assert len(build_called) == 1
    assert build_called[0]["manifest_data"] == build_payload


def test_main_handle_matrix_eval_runtime_error_becomes_non_recoverable(
    monkeypatch, tmp_path
):
    """When run_eval_task raises RuntimeError the handle re-raises as
    NonRecoverableError so the framework surfaces it as
    ``error:non_recoverable:``."""
    handle, _ = _run_build_worker_main_with_capture(
        monkeypatch,
        [
            "--socket-path", str(tmp_path / "sock"),
            "--flake-ref", ".",
            "--dataset-output-dir", str(tmp_path / "dataset"),
            "--shared-fs", str(tmp_path),
            "--matrix-eval-out-dir", str(tmp_path / "phase0"),
        ],
    )

    from compiler_suit_runner.workers import eval_worker as ew

    def _boom(payload, *, out_dir, task):
        raise RuntimeError("nix-eval-jobs fell over")

    monkeypatch.setattr(ew, "run_eval_task", _boom)

    import sys as _sys
    fake_mod = _sys.modules["dynamic_runner.worker"]
    Task = fake_mod.Task
    NonRecoverable = fake_mod.NonRecoverableError
    with pytest.raises(NonRecoverable) as exc_info:
        handle(Task(payload=_matrix_eval_wrapper_payload()))
    assert "nix-eval-jobs fell over" in str(exc_info.value)


def test_main_handle_matrix_eval_without_matrix_eval_out_dir_is_non_recoverable(
    monkeypatch, tmp_path
):
    """matrix_eval requires --matrix-eval-out-dir (shared bind-mounted
    marker dir). Receiving the task without that flag is a structural
    misconfiguration -> NonRecoverableError."""
    handle, _ = _run_build_worker_main_with_capture(
        monkeypatch,
        [
            "--socket-path", str(tmp_path / "sock"),
            "--flake-ref", ".",
            "--dataset-output-dir", str(tmp_path / "dataset"),
            "--shared-fs", str(tmp_path),
            # --matrix-eval-out-dir OMITTED
        ],
    )

    import sys as _sys
    fake_mod = _sys.modules["dynamic_runner.worker"]
    Task = fake_mod.Task
    NonRecoverable = fake_mod.NonRecoverableError
    with pytest.raises(NonRecoverable) as exc_info:
        handle(Task(payload=_matrix_eval_wrapper_payload()))
    assert "matrix-eval-out-dir" in str(exc_info.value)


# ---------------------------------------------------------------------------
# dependency_graph dispatch — archive root sourced from BuildWorkerEnv,
# NOT from the payload (container vs submitter-host path divergence
# would otherwise wipe phase 4 to 0/0; see fix-dep-graph-path-resolution).
# ---------------------------------------------------------------------------


def _dependency_graph_wrapper_payload(
    *,
    binary: str = "hello",
    sys_name: str = "x86_64-linux",
    tc_drv: str = "/nix/store/aaaa-toolchains.drv",
) -> dict:
    """Build the single all-binaries dep_graph wrapper as
    :meth:`SuitTask._task_info_from_header` would emit. The inner
    payload carries NO per-binary ``binary`` field and NO
    ``matrix_eval_out_dir``: the dispatch gathers each binary's
    matrix_aggregate_drv from ``task.predecessor_outputs`` (one
    ``matrix_eval__<binary>`` key per binary) and reads the archive
    root from the BuildWorkerEnv (synthesised from
    ``--matrix-eval-out-dir``).

    ``binary`` is retained only to keep call-sites terse; it is not
    embedded in the payload anymore.
    """
    del binary  # no longer embedded — dep_graph is a single task
    return {
        "item_class": "dependency_graph",
        "name": "dependency_graph",
        "size": 1,
        "payload": {
            "sys": sys_name,
            "toolchain_aggregate_drv": tc_drv,
        },
    }


def test_main_handle_dependency_graph_uses_env_matrix_eval_out_dir(
    monkeypatch, tmp_path
):
    """dep_graph dispatch must resolve ``matrix_eval_out_dir`` from the
    BuildWorkerEnv (populated from ``--matrix-eval-out-dir``), i.e. the
    container view, NOT from the payload."""
    container_archive_root = tmp_path / "app_out_network_matrix_eval"
    container_archive_root.mkdir()
    handle, _ = _run_build_worker_main_with_capture(
        monkeypatch,
        [
            "--socket-path", str(tmp_path / "sock"),
            "--flake-ref", ".",
            "--dataset-output-dir", str(tmp_path / "dataset"),
            "--shared-fs", str(tmp_path),
            "--matrix-eval-out-dir", str(container_archive_root),
        ],
    )

    captured: dict = {}

    def _fake_run_dg(*, matrix_eval_out_dir, **kwargs):
        captured["matrix_eval_out_dir"] = matrix_eval_out_dir
        captured.update(kwargs)
        return None

    from compiler_suit_runner.workers.dependency_graph_worker import (
        run as dg_run,
    )

    monkeypatch.setattr(dg_run, "run_dependency_graph_task", _fake_run_dg)
    monkeypatch.setattr(
        bw, "_resolve_bash_store_path_default",
        lambda: "/nix/store/fake-bash",
    )

    import sys as _sys
    fake_mod = _sys.modules["dynamic_runner.worker"]
    Task = fake_mod.Task

    wrapper = _dependency_graph_wrapper_payload()
    task = Task(payload=wrapper, task_id="dependency_graph")
    # Single all-binaries task: one matrix_eval predecessor per binary.
    # The matrix_eval task_id is the bare binary (phase-local id), so
    # the predecessor_outputs key IS the binary name directly.
    task.predecessor_outputs = {
        "hello": {
            "matrix_aggregate_drv": {"value": "/nix/store/m-hello.drv"},
        },
        "busybox": {
            "matrix_aggregate_drv": {"value": "/nix/store/m-busybox.drv"},
        },
    }

    handle(task)

    assert captured["matrix_eval_out_dir"] == container_archive_root
    # The dispatch gathers EVERY matrix_eval predecessor's aggregate
    # drv into a {binary: drv} mapping and hands the full set to the
    # worker — no single ``binary`` / ``matrix_drv`` anymore.
    assert "binary" not in captured
    assert "matrix_drv" not in captured
    assert captured["matrix_drvs"] == {
        "hello": "/nix/store/m-hello.drv",
        "busybox": "/nix/store/m-busybox.drv",
    }
    assert captured["toolchain_aggregate_drv"] == (
        "/nix/store/aaaa-toolchains.drv"
    )


def test_main_handle_dependency_graph_ignores_legacy_payload_field(
    monkeypatch, tmp_path
):
    """Forward-compat: if a legacy payload still carries a stale
    ``matrix_eval_out_dir`` (e.g. submitter-host path), the worker
    must IGNORE it and use the env-derived container path. This
    guards the regression that drove phase build to 0/0."""
    container_archive_root = tmp_path / "container_view"
    container_archive_root.mkdir()
    legacy_host_path = "/home/some-user/BIG/slurm/asm-dataset/dataset/_matrix_eval"

    handle, _ = _run_build_worker_main_with_capture(
        monkeypatch,
        [
            "--socket-path", str(tmp_path / "sock"),
            "--flake-ref", ".",
            "--dataset-output-dir", str(tmp_path / "dataset"),
            "--shared-fs", str(tmp_path),
            "--matrix-eval-out-dir", str(container_archive_root),
        ],
    )

    captured: dict = {}

    def _fake_run_dg(*, matrix_eval_out_dir, **kwargs):
        captured["matrix_eval_out_dir"] = matrix_eval_out_dir
        return None

    from compiler_suit_runner.workers.dependency_graph_worker import (
        run as dg_run,
    )

    monkeypatch.setattr(dg_run, "run_dependency_graph_task", _fake_run_dg)
    monkeypatch.setattr(
        bw, "_resolve_bash_store_path_default",
        lambda: "/nix/store/fake-bash",
    )

    import sys as _sys
    fake_mod = _sys.modules["dynamic_runner.worker"]
    Task = fake_mod.Task

    wrapper = _dependency_graph_wrapper_payload()
    # Inject a stale host-path field a legacy submitter might still emit.
    wrapper["payload"]["matrix_eval_out_dir"] = legacy_host_path
    task = Task(payload=wrapper, task_id="dependency_graph")
    task.predecessor_outputs = {
        "matrix_eval__hello": {
            "matrix_aggregate_drv": {"value": "/nix/store/m-hello.drv"},
        },
    }

    handle(task)

    # The env path wins; the legacy host path is discarded.
    assert captured["matrix_eval_out_dir"] == container_archive_root
    assert str(captured["matrix_eval_out_dir"]) != legacy_host_path


def test_main_handle_dependency_graph_without_matrix_eval_out_dir_is_non_recoverable(
    monkeypatch, tmp_path
):
    """A dep_graph task arriving at a worker started without
    ``--matrix-eval-out-dir`` is a structural misconfig:
    NonRecoverableError, not a silent degrade."""
    handle, _ = _run_build_worker_main_with_capture(
        monkeypatch,
        [
            "--socket-path", str(tmp_path / "sock"),
            "--flake-ref", ".",
            "--dataset-output-dir", str(tmp_path / "dataset"),
            "--shared-fs", str(tmp_path),
            # --matrix-eval-out-dir OMITTED on purpose.
        ],
    )

    import sys as _sys
    fake_mod = _sys.modules["dynamic_runner.worker"]
    Task = fake_mod.Task
    NonRecoverable = fake_mod.NonRecoverableError

    wrapper = _dependency_graph_wrapper_payload()
    task = Task(payload=wrapper, task_id="dependency_graph")
    task.predecessor_outputs = {
        "matrix_eval__hello": {
            "matrix_aggregate_drv": {"value": "/nix/store/m-hello.drv"},
        },
    }
    with pytest.raises(NonRecoverable) as exc_info:
        handle(task)
    assert "matrix-eval-out-dir" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Per-binary matrix-eval archive import (build_variant prelude)
#
# Phase 4 (build_variant) on each secondary imports the per-binary
# ``matrix-<binary>.drv.archive`` exactly once per worker process. The
# first ``build_variant`` task for binary X triggers the import; every
# subsequent ``build_variant`` task for the SAME binary on the SAME
# process skips the import (idempotent via ``_imported_binaries``).
# Switching to binary Y triggers a fresh import for ``matrix-Y.drv.archive``.
#
# The archive itself is opaque bytes here — we patch ``import_archive``
# so the test never shells out to ``nix-store``; we assert only on the
# call multiplicity + the archive path argv.
# ---------------------------------------------------------------------------


def _reset_imported_binaries() -> None:
    """Clear the module-level import caches so tests stay hermetic."""
    bw._imported_binaries.clear()
    bw._toolchain_imported = False
    bw._common_archive_imported = False
    bw._imported_toolchain_out_paths.clear()


def _make_variant_manifest(
    tmp_path: pathlib.Path, *, pkg: str, variant_dir: str,
) -> pathlib.Path:
    """Write a build_variant manifest with the given pkg / variant_dir."""
    return _write_manifest(
        tmp_path / f"{variant_dir}.json",
        item_class=ITEM_CLASS_BUILD_VARIANT,
        name=variant_dir,
        payload={
            "attr": f"dataset.x86_64-linux.{pkg}.x86_64.gcc15.O2",
            "variant_dir": variant_dir,
            "pkg": pkg,
        },
    )


def test_ensure_binary_archive_imported_calls_import_once_per_binary(
    monkeypatch, tmp_path,
):
    """First call for binary ``hello`` triggers ``import_archive``; the
    second call for the same binary is a no-op (cache hit). Switching
    to ``world`` triggers a fresh import against matrix-world.drv.archive.
    """
    _reset_imported_binaries()
    calls: list[pathlib.Path] = []

    def _fake_import(archive, *, run_subprocess=None):
        calls.append(archive)
        return True, b"", ["/nix/store/x"]

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()

    # First call: import fires.
    bw.ensure_binary_archive_imported("hello", out_dir)
    assert calls == [out_dir / "matrix-hello.drv.archive"]
    assert "hello" in bw._imported_binaries

    # Second call for the SAME binary: no new import.
    bw.ensure_binary_archive_imported("hello", out_dir)
    assert calls == [out_dir / "matrix-hello.drv.archive"]

    # Switching binary: fresh import against matrix-world.drv.archive.
    bw.ensure_binary_archive_imported("world", out_dir)
    assert calls == [
        out_dir / "matrix-hello.drv.archive",
        out_dir / "matrix-world.drv.archive",
    ]

    _reset_imported_binaries()


def test_ensure_binary_archive_imported_noop_when_out_dir_is_none(
    monkeypatch, tmp_path,
):
    """A ``None`` ``matrix_eval_out_dir`` (unit-test default + legacy
    callers) makes the import a no-op so existing fixtures keep working
    without needing to thread an archive root through every test.
    """
    _reset_imported_binaries()

    def _fake_import(archive, *, run_subprocess=None):
        raise AssertionError(f"import should not run; got {archive!r}")

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    # Must NOT raise; must NOT mark the binary imported.
    bw.ensure_binary_archive_imported("hello", None)
    assert "hello" not in bw._imported_binaries
    _reset_imported_binaries()


def test_ensure_binary_archive_imported_raises_on_import_failure(
    monkeypatch, tmp_path,
):
    """When ``import_archive`` returns ``(False, ...)`` we propagate
    :class:`RuntimeError`; the framework wraps that into
    ``ErrorType::Errored`` (retry-pass) at the worker harness."""
    _reset_imported_binaries()

    def _fake_import(archive, *, run_subprocess=None):
        return False, b"archive corrupt", []

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    with pytest.raises(RuntimeError) as exc_info:
        bw.ensure_binary_archive_imported("hello", out_dir)
    msg = str(exc_info.value)
    assert "matrix-hello.drv.archive" in msg
    assert "archive corrupt" in msg
    # On failure the binary is NOT marked imported — a retry can try again.
    assert "hello" not in bw._imported_binaries
    _reset_imported_binaries()


def test_ensure_toolchain_archive_imported_rides_out_transient_failure(
    monkeypatch, tmp_path,
):
    """A transient EAGAIN-style import failure (production respawn-loop
    signature) is retried INSIDE ``import_archive`` — the prelude
    succeeds on the second attempt instead of raising RuntimeError and
    killing the worker. Goes through the REAL ``import_archive`` (only
    the sleep hook + subprocess stub are injected)."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )

    monkeypatch.setattr(bw, "_toolchain_imported", False)
    sleeps: list[float] = []
    monkeypatch.setattr(archive_mod, "_retry_sleep", sleeps.append)

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    (out_dir / "toolchains.drv.archive").write_bytes(b"fake-archive")

    rcs = [1, 0]
    calls: list[list[str]] = []

    def _stub(argv):
        calls.append(list(argv))
        rc = rcs.pop(0)
        if rc != 0:
            return b"", b"error: Resource temporarily unavailable", rc
        return b"/nix/store/aaa-toolchain.drv\n", b"", rc

    _stub._stdin_aware = True

    bw.ensure_toolchain_archive_imported(out_dir, run_subprocess=_stub)

    assert bw._toolchain_imported is True
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_ensure_toolchain_archive_imported_permanent_failure_still_raises(
    monkeypatch, tmp_path,
):
    """A permanent import failure (corrupt archive) fails fast — no
    retry sleeps — and still escalates to RuntimeError as before."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )

    monkeypatch.setattr(bw, "_toolchain_imported", False)
    sleeps: list[float] = []
    monkeypatch.setattr(archive_mod, "_retry_sleep", sleeps.append)

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    (out_dir / "toolchains.drv.archive").write_bytes(b"junk")

    calls: list[list[str]] = []

    def _stub(argv):
        calls.append(list(argv))
        return b"", b"error: corrupt archive", 1

    _stub._stdin_aware = True

    with pytest.raises(RuntimeError) as exc_info:
        bw.ensure_toolchain_archive_imported(out_dir, run_subprocess=_stub)
    assert "toolchains.drv.archive" in str(exc_info.value)
    assert "corrupt archive" in str(exc_info.value)
    assert len(calls) == 1
    assert sleeps == []
    assert bw._toolchain_imported is False


def test_ensure_binary_archive_imported_imports_first_time(
    monkeypatch, tmp_path,
):
    """A first call to ensure_binary_archive_imported for binary X imports
    the archive; subsequent calls for the same binary are no-ops.
    (The gate SuitTask.import_action calls this exactly once per machine
    per binary when build tasks are assigned to a secondary.)"""
    _reset_imported_binaries()
    captured: list[tuple[str, pathlib.Path]] = []

    def _fake_import(archive, *, run_subprocess=None):
        captured.append(("import", archive))
        return True, b"", []

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    (out_dir / "matrix-hello.drv.archive").write_bytes(b"DRV")

    # First call: archive is imported.
    bw.ensure_binary_archive_imported("hello", out_dir)
    assert captured == [("import", out_dir / "matrix-hello.drv.archive")]
    assert "hello" in bw._imported_binaries

    # Second call: cache short-circuits; no re-import.
    bw.ensure_binary_archive_imported("hello", out_dir)
    assert len(captured) == 1, "should not re-import the same binary"
    _reset_imported_binaries()


def test_build_worker_build_common_dep_does_not_import_archives(monkeypatch, tmp_path):
    """A ``build_common_dep`` task no longer triggers any archive import from
    the per-process prelude — all imports are now secondary-affine gate actions
    (SuitTask.import_action).  The worker just rebuilds the absolute drv path
    and calls ``nix build``."""
    _reset_imported_binaries()
    captured: list[tuple[str, pathlib.Path]] = []

    def _fake_import(archive, *, run_subprocess=None):
        captured.append(("import", archive))
        return True, b"", []

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    ident = "bvds533r8bjg379a68m3skkpp1giy0hh-source.drv"
    manifest = _write_manifest(
        tmp_path / "m.json",
        item_class=ITEM_CLASS_BUILD_COMMON_DEP,
        name="cd",
        payload={"attr": ident, "binary": "hello"},
    )
    runner = RecordingRunner(stdout=b"/nix/store/out\n")
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        matrix_eval_out_dir=out_dir,
    )
    result = build_worker(manifest, env)
    assert result.success is True
    # No archive import from the per-process prelude.
    assert captured == [], (
        f"build_worker should not import archives (gate-driven now): {captured}"
    )
    # The drv path is still reconstructed and passed to nix build.
    assert f"/nix/store/{ident}^*" in runner.calls[0]
    _reset_imported_binaries()


def test_ensure_binary_archive_imported_idempotent_same_binary(
    monkeypatch, tmp_path,
):
    """The second call to ensure_binary_archive_imported for the SAME binary
    MUST NOT re-trigger import_archive — the cache lives in _imported_binaries.
    The gate fires this only once per secondary per binary, so cache hits
    only arise when the prelude was called directly (legacy / test paths)."""
    _reset_imported_binaries()
    captured: list[pathlib.Path] = []

    def _fake_import(archive, *, run_subprocess=None):
        captured.append(archive)
        return True, b"", []

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    (out_dir / "matrix-hello.drv.archive").write_bytes(b"DRV")

    # Call twice for the same binary; only one archive import must occur.
    bw.ensure_binary_archive_imported("hello", out_dir)
    bw.ensure_binary_archive_imported("hello", out_dir)

    assert captured == [out_dir / "matrix-hello.drv.archive"], (
        "must import only once per process per binary"
    )
    _reset_imported_binaries()


def test_ensure_binary_archive_imported_per_binary_key(
    monkeypatch, tmp_path,
):
    """ensure_binary_archive_imported uses the binary as the cache key:
    a second binary triggers a fresh import (matrix-world.drv.archive)
    even after matrix-hello.drv.archive was already imported."""
    _reset_imported_binaries()
    captured: list[pathlib.Path] = []

    def _fake_import(archive, *, run_subprocess=None):
        captured.append(archive)
        return True, b"", []

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    (out_dir / "matrix-hello.drv.archive").write_bytes(b"DRV")
    (out_dir / "matrix-world.drv.archive").write_bytes(b"DRV")

    bw.ensure_binary_archive_imported("hello", out_dir)
    bw.ensure_binary_archive_imported("world", out_dir)

    assert captured == [
        out_dir / "matrix-hello.drv.archive",
        out_dir / "matrix-world.drv.archive",
    ]
    _reset_imported_binaries()


def test_ensure_binary_archive_imported_failure_raises(
    monkeypatch, tmp_path,
):
    """A failed import_archive for matrix-<binary>.drv.archive raises
    RuntimeError, so the gate (SuitTask.import_action) surfaces it as a
    gate failure (framework marks all dependent build tasks as failed)."""
    _reset_imported_binaries()

    def _fake_import(archive, *, run_subprocess=None):
        return False, b"missing archive", []

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    (out_dir / "matrix-hello.drv.archive").write_bytes(b"data")

    with pytest.raises(RuntimeError, match="matrix-hello.drv.archive"):
        bw.ensure_binary_archive_imported("hello", out_dir)
    _reset_imported_binaries()


def test_build_worker_build_variant_does_not_import_archives_from_prelude(
    monkeypatch, tmp_path,
):
    """build_worker for build_variant no longer imports any archive from
    _run_import_prelude — drv archives are now secondary-affine gate actions
    (SuitTask.import_action) fired before any dependent build task runs."""
    _reset_imported_binaries()
    captured: list[pathlib.Path] = []

    def _fake_import(archive, *, run_subprocess=None):
        captured.append(archive)
        return True, b"", []

    monkeypatch.setattr(
        "compiler_suit_runner.workers.dependency_graph_worker"
        ".archive.import_archive",
        _fake_import,
    )

    out_dir = tmp_path / "phase0"
    out_dir.mkdir()
    nix_out = tmp_path / "nix-out"
    _make_elf_folder(nix_out, {"hello": b"v"})
    manifest = _make_variant_manifest(
        tmp_path, pkg="hello", variant_dir="hello__x86_64__gcc15__O2",
    )
    runner = RecordingRunner(stdout=f"{nix_out}\n".encode("utf-8"))
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "dataset",
        run_subprocess=runner,
        matrix_eval_out_dir=out_dir,
    )
    result = build_worker(manifest, env)
    assert result.success is True
    # No archive import triggered by build_worker; gate drives it.
    assert captured == [], (
        f"build_worker should not import archives (gate-driven): {captured}"
    )
    _reset_imported_binaries()


# ---------------------------------------------------------------------------
# Torn-PATH hardening (respawn-env): the default runner resolves argv[0]
# ---------------------------------------------------------------------------


class TestDefaultRunnerResolvesTool:

    def test_default_run_subprocess_resolves_argv0(self):
        from unittest.mock import patch

        calls: list[list[str]] = []

        def _fake_run(argv, **_kwargs):
            calls.append(list(argv))

            class _Proc:
                stdout = b""
                stderr = b""
                returncode = 0

            return _Proc()

        with patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".subproc.shutil.which",
            return_value=None,
        ), patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".subproc.os.path.exists",
            lambda path: str(path).startswith("/bin/"),
        ), patch.object(bw.subprocess, "run", _fake_run):
            bw._default_run_subprocess(["nix-store", "--import"])
        assert calls == [["/bin/nix-store", "--import"]]

    def test_resolve_bash_store_path_default_resolves_nix(self):
        from unittest.mock import patch

        calls: list[list[str]] = []

        def _fake_run(argv, **_kwargs):
            calls.append(list(argv))

            class _Proc:
                stdout = b"/nix/store/bbb-bash\n"
                stderr = b""
                returncode = 0

            return _Proc()

        with patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".subproc.shutil.which",
            return_value=None,
        ), patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".subproc.os.path.exists",
            lambda path: str(path).startswith("/bin/"),
        ), patch.object(bw.subprocess, "run", _fake_run):
            out = bw._resolve_bash_store_path_default()
        assert out == "/nix/store/bbb-bash"
        assert calls == [[
            "/bin/nix", "eval", "--raw", "nixpkgs#bash.outPath",
        ]]


# ---------------------------------------------------------------------------
# ensure_common_archive_imported
# ---------------------------------------------------------------------------


def test_ensure_common_archive_imported_happy(
    monkeypatch, tmp_path,
):
    """A present archive is imported and the guard flips."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_common_imported(monkeypatch)
    (tmp_path / "toolchains.common.archive").write_bytes(b"NIX_EXPORT:common")
    imported: list = []

    def _fake_import(archive, *, run_subprocess=None):
        imported.append(pathlib.Path(archive))
        return True, b"", ["/nix/store/glibc"]

    monkeypatch.setattr(archive_mod, "import_archive", _fake_import)
    bw.ensure_common_archive_imported(tmp_path)
    assert bw._common_archive_imported is True
    assert imported == [tmp_path / "toolchains.common.archive"]
def test_ensure_common_archive_imported_idempotent(
    monkeypatch, tmp_path,
):
    """Second call is a no-op (guard already set)."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_common_imported(monkeypatch)
    (tmp_path / "toolchains.common.archive").write_bytes(b"NIX_EXPORT:common")
    call_count: list = []
    monkeypatch.setattr(
        archive_mod, "import_archive",
        lambda a, *, run_subprocess=None: call_count.append(1) or (True, b"", []),
    )
    bw.ensure_common_archive_imported(tmp_path)
    bw.ensure_common_archive_imported(tmp_path)
    assert len(call_count) == 1


def test_ensure_common_archive_imported_none_dir_is_noop(monkeypatch):
    """``matrix_eval_out_dir=None`` is a safe no-op; guard stays False."""
    _reset_common_imported(monkeypatch)
    bw.ensure_common_archive_imported(None)
    assert bw._common_archive_imported is False


# ---------------------------------------------------------------------------
# ensure_toolchain_out_archive_imported
# ---------------------------------------------------------------------------


def _reset_toolchain_out_imported(monkeypatch) -> None:
    """Reset per-process per-toolchain import set between tests."""
    monkeypatch.setattr(bw, "_imported_toolchain_out_paths", set())


def test_ensure_toolchain_out_archive_imported_happy(
    monkeypatch, tmp_path,
):
    """A present delta archive is imported and the tc_id added to the set."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_toolchain_out_imported(monkeypatch)
    tc_id = "abc123"
    archive_name = f"toolchains.{tc_id}.out.archive"
    (tmp_path / archive_name).write_bytes(b"NIX_EXPORT:delta")
    imported: list = []

    def _fake_import(archive, *, run_subprocess=None):
        imported.append(pathlib.Path(archive))
        return True, b"", ["/nix/store/abc123-gcc"]

    monkeypatch.setattr(archive_mod, "import_archive", _fake_import)
    bw.ensure_toolchain_out_archive_imported(tc_id, tmp_path)
    assert tc_id in bw._imported_toolchain_out_paths
    assert imported == [tmp_path / archive_name]


def test_ensure_toolchain_out_archive_imported_idempotent(
    monkeypatch, tmp_path,
):
    """A second call for the same tc_id is a no-op."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_toolchain_out_imported(monkeypatch)
    tc_id = "abc123"
    archive_name = f"toolchains.{tc_id}.out.archive"
    (tmp_path / archive_name).write_bytes(b"NIX_EXPORT:delta")
    call_count: list = []
    monkeypatch.setattr(
        archive_mod, "import_archive",
        lambda a, *, run_subprocess=None: call_count.append(1) or (True, b"", []),
    )
    bw.ensure_toolchain_out_archive_imported(tc_id, tmp_path)
    bw.ensure_toolchain_out_archive_imported(tc_id, tmp_path)
    assert len(call_count) == 1


def test_ensure_toolchain_out_archive_imported_none_tc_id_is_noop(
    monkeypatch, tmp_path,
):
    """A None tc_id (legacy manifest / empty gate id) is a safe no-op."""
    _reset_toolchain_out_imported(monkeypatch)
    bw.ensure_toolchain_out_archive_imported(None, tmp_path)
    assert bw._imported_toolchain_out_paths == set()


def test_ensure_toolchain_out_archive_imported_none_dir_is_noop(
    monkeypatch,
):
    """A None matrix_eval_out_dir (legacy fixtures) is a safe no-op."""
    _reset_toolchain_out_imported(monkeypatch)
    bw.ensure_toolchain_out_archive_imported("abc123", None)
    assert bw._imported_toolchain_out_paths == set()


def test_ensure_toolchain_out_archive_imported_common_once_then_per_toolchain(
    monkeypatch, tmp_path,
):
    """Calling common+toolchain in order: COMMON imported once, then per-
    toolchain delta imported for each distinct tc_id."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_common_imported(monkeypatch)
    _reset_toolchain_out_imported(monkeypatch)
    tc_id1 = "abc123"
    tc_id2 = "def456"
    archive_name1 = f"toolchains.{tc_id1}.out.archive"
    archive_name2 = f"toolchains.{tc_id2}.out.archive"
    (tmp_path / "toolchains.common.archive").write_bytes(b"COMMON")
    (tmp_path / archive_name1).write_bytes(b"DELTA1")
    (tmp_path / archive_name2).write_bytes(b"DELTA2")
    imported: list = []
    monkeypatch.setattr(
        archive_mod, "import_archive",
        lambda a, *, run_subprocess=None: imported.append(pathlib.Path(a).name) or (True, b"", []),
    )
    bw.ensure_common_archive_imported(tmp_path)
    bw.ensure_toolchain_out_archive_imported(tc_id1, tmp_path)
    bw.ensure_toolchain_out_archive_imported(tc_id2, tmp_path)
    # Call common a second time — must be idempotent.
    bw.ensure_common_archive_imported(tmp_path)
    assert imported.count("toolchains.common.archive") == 1
    assert archive_name1 in imported
    assert archive_name2 in imported


# ---------------------------------------------------------------------------
# ensure_common_archive_imported + ensure_toolchain_out_archive_imported
# HARD-FAIL behaviour tests
# ---------------------------------------------------------------------------


def _reset_common_imported(monkeypatch) -> None:
    """Reset per-process common-archive guard between tests."""
    monkeypatch.setattr(bw, "_common_archive_imported", False)
    bw._imported_toolchain_out_paths.clear()


def test_ensure_common_archive_imported_happy(monkeypatch, tmp_path):
    """A present common archive is imported and the guard flips."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_common_imported(monkeypatch)
    (tmp_path / "toolchains.common.archive").write_bytes(b"NIX_EXPORT:common")
    imported: list = []

    def _fake_import(archive, *, run_subprocess=None):
        imported.append(pathlib.Path(archive))
        return True, b"", ["/nix/store/glibc"]

    monkeypatch.setattr(archive_mod, "import_archive", _fake_import)
    bw.ensure_common_archive_imported(tmp_path)
    assert bw._common_archive_imported is True
    assert imported == [tmp_path / "toolchains.common.archive"]


def test_ensure_common_archive_imported_absent_raises(monkeypatch, tmp_path):
    """An absent common archive raises RuntimeError (HARD FAIL)."""
    _reset_common_imported(monkeypatch)
    with pytest.raises(RuntimeError, match="toolchains.common.archive"):
        bw.ensure_common_archive_imported(tmp_path)


def test_ensure_common_archive_imported_zero_byte_raises(monkeypatch, tmp_path):
    """A zero-byte common archive raises RuntimeError."""
    _reset_common_imported(monkeypatch)
    (tmp_path / "toolchains.common.archive").write_bytes(b"")
    with pytest.raises(RuntimeError, match="zero-byte"):
        bw.ensure_common_archive_imported(tmp_path)


def test_ensure_common_archive_imported_import_failure_raises(monkeypatch, tmp_path):
    """A failed import_archive for common raises RuntimeError (no soft-fail)."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_common_imported(monkeypatch)
    (tmp_path / "toolchains.common.archive").write_bytes(b"corrupt")
    monkeypatch.setattr(
        archive_mod, "import_archive",
        lambda a, *, run_subprocess=None: (False, b"corrupt archive", []),
    )
    with pytest.raises(RuntimeError, match="failed to import"):
        bw.ensure_common_archive_imported(tmp_path)


def test_ensure_toolchain_out_archive_imported_absent_raises(monkeypatch, tmp_path):
    """A missing per-toolchain delta archive raises RuntimeError (HARD FAIL)."""
    _reset_common_imported(monkeypatch)
    tc_id = "abc123hhhhhhhhhhhhhhhhhhhhhhhh"
    with pytest.raises(RuntimeError):
        bw.ensure_toolchain_out_archive_imported(tc_id, tmp_path)


def test_ensure_toolchain_out_archive_imported_zero_byte_raises(monkeypatch, tmp_path):
    """A zero-byte delta archive raises RuntimeError."""
    _reset_common_imported(monkeypatch)
    tc_id = "abc123hhhhhhhhhhhhhhhhhhhhhhhh"
    delta_name = f"toolchains.{tc_id}.out.archive"
    (tmp_path / delta_name).write_bytes(b"")
    with pytest.raises(RuntimeError, match="zero-byte"):
        bw.ensure_toolchain_out_archive_imported(tc_id, tmp_path)


def test_ensure_toolchain_out_archive_imported_import_failure_raises(
    monkeypatch, tmp_path,
):
    """A failed import for the delta archive raises RuntimeError (no soft-fail)."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_common_imported(monkeypatch)
    tc_id = "abc123hhhhhhhhhhhhhhhhhhhhhhhh"
    delta_name = f"toolchains.{tc_id}.out.archive"
    (tmp_path / delta_name).write_bytes(b"data")
    monkeypatch.setattr(
        archive_mod, "import_archive",
        lambda a, *, run_subprocess=None: (False, b"import failed", []),
    )
    with pytest.raises(RuntimeError, match="failed to import"):
        bw.ensure_toolchain_out_archive_imported(tc_id, tmp_path)


# ---------------------------------------------------------------------------
# _run_import_prelude — all imports now delegated to secondary-affine gates
# ---------------------------------------------------------------------------


def test_run_import_prelude_is_noop_for_build_variant(
    monkeypatch, tmp_path,
):
    """_run_import_prelude is a no-op for build_variant: ALL archive imports
    (toolchains.drv.archive, matrix-<binary>.drv.archive, toolchains.common.archive,
    per-toolchain delta archives, and build_deps.out.archive) are now handled
    ONCE PER SECONDARY NODE via the secondary-affine import gate
    (SuitTask.import_action).  The per-process prelude performs no I/O."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_common_imported(monkeypatch)
    bw._toolchain_imported = False
    bw._imported_binaries.clear()

    call_order: list[str] = []

    def _fake_import(archive, *, run_subprocess=None):
        call_order.append(pathlib.Path(archive).name)
        return True, b"", []

    monkeypatch.setattr(archive_mod, "import_archive", _fake_import)

    outpath = "/nix/store/abc123hhhhhhhhhhhhhhhhhhhhhhhh-gcc15"

    (tmp_path / "toolchains.drv.archive").write_bytes(b"DRV")
    (tmp_path / "matrix-hello.drv.archive").write_bytes(b"BINARY")

    from compiler_suit_runner.workers.build_worker import BuildWorkerEnv, _run_import_prelude
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "ds",
        matrix_eval_out_dir=tmp_path,
    )
    payload = {"pkg": "hello", "toolchain_outpath": outpath}
    _run_import_prelude("build_variant", payload, env)

    # All archives are now gate-driven; the per-process prelude does nothing.
    assert call_order == [], (
        f"_run_import_prelude should be a no-op but imported: {call_order}"
    )


def test_run_import_prelude_is_noop_for_build_common_dep(
    monkeypatch, tmp_path,
):
    """_run_import_prelude is a no-op for build_common_dep: all archive imports
    are handled by the secondary-affine gate (SuitTask.import_action)."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_common_imported(monkeypatch)
    bw._toolchain_imported = False
    bw._imported_binaries.clear()

    call_order: list[str] = []

    def _fake_import(archive, *, run_subprocess=None):
        call_order.append(pathlib.Path(archive).name)
        return True, b"", []

    monkeypatch.setattr(archive_mod, "import_archive", _fake_import)

    (tmp_path / "toolchains.drv.archive").write_bytes(b"DRV")
    (tmp_path / "matrix-hello.drv.archive").write_bytes(b"BINARY")

    from compiler_suit_runner.workers.build_worker import BuildWorkerEnv, _run_import_prelude
    env = BuildWorkerEnv(
        flake_ref=".",
        dataset_output_dir=tmp_path / "ds",
        matrix_eval_out_dir=tmp_path,
    )
    payload = {"binary": "hello"}  # no toolchain_outpath
    _run_import_prelude("build_common_dep", payload, env)

    # All archives are now gate-driven; the per-process prelude does nothing.
    assert call_order == [], (
        f"_run_import_prelude should be a no-op but imported: {call_order}"
    )


# ---------------------------------------------------------------------------
# ensure_build_deps_archive_imported — HARD-FAIL behaviour tests
# ---------------------------------------------------------------------------


def _reset_build_deps_imported(monkeypatch) -> None:
    """Reset per-process build-deps-archive guard between tests."""
    monkeypatch.setattr(bw, "_build_deps_archive_imported", False)


def test_ensure_build_deps_archive_imported_happy(monkeypatch, tmp_path):
    """A present build_deps.out.archive is imported and the guard flips."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_build_deps_imported(monkeypatch)
    (tmp_path / "build_deps.out.archive").write_bytes(b"NIX_EXPORT:deps")
    imported: list = []

    def _fake_import(archive, *, run_subprocess=None):
        imported.append(pathlib.Path(archive).name)
        return True, b"", ["/nix/store/glibc"]

    monkeypatch.setattr(archive_mod, "import_archive", _fake_import)
    bw.ensure_build_deps_archive_imported(tmp_path)
    assert bw._build_deps_archive_imported is True
    assert imported == ["build_deps.out.archive"]


def test_ensure_build_deps_archive_imported_idempotent(monkeypatch, tmp_path):
    """Calling ensure_build_deps_archive_imported twice imports the archive only once."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_build_deps_imported(monkeypatch)
    (tmp_path / "build_deps.out.archive").write_bytes(b"NIX_EXPORT:deps")
    call_count = [0]

    def _fake_import(archive, *, run_subprocess=None):
        call_count[0] += 1
        return True, b"", []

    monkeypatch.setattr(archive_mod, "import_archive", _fake_import)
    bw.ensure_build_deps_archive_imported(tmp_path)
    bw.ensure_build_deps_archive_imported(tmp_path)
    assert call_count[0] == 1, "archive should be imported exactly once"


def test_ensure_build_deps_archive_imported_none_dir_is_noop(monkeypatch):
    """None matrix_eval_out_dir is a no-op (legacy fixtures)."""
    _reset_build_deps_imported(monkeypatch)
    bw.ensure_build_deps_archive_imported(None)
    assert bw._build_deps_archive_imported is False


def test_ensure_build_deps_archive_imported_absent_raises(monkeypatch, tmp_path):
    """An absent build_deps.out.archive raises RuntimeError (HARD FAIL)."""
    _reset_build_deps_imported(monkeypatch)
    with pytest.raises(RuntimeError, match="build_deps.out.archive"):
        bw.ensure_build_deps_archive_imported(tmp_path)


def test_ensure_build_deps_archive_imported_zero_byte_raises(monkeypatch, tmp_path):
    """A zero-byte build_deps.out.archive raises RuntimeError."""
    _reset_build_deps_imported(monkeypatch)
    (tmp_path / "build_deps.out.archive").write_bytes(b"")
    with pytest.raises(RuntimeError, match="zero-byte"):
        bw.ensure_build_deps_archive_imported(tmp_path)


def test_ensure_build_deps_archive_imported_import_failure_raises(monkeypatch, tmp_path):
    """A failed import for build_deps.out.archive raises RuntimeError."""
    from compiler_suit_runner.workers.dependency_graph_worker import (
        archive as archive_mod,
    )
    _reset_build_deps_imported(monkeypatch)
    (tmp_path / "build_deps.out.archive").write_bytes(b"corrupt")
    monkeypatch.setattr(
        archive_mod, "import_archive",
        lambda a, *, run_subprocess=None: (False, b"corrupt archive", []),
    )
    with pytest.raises(RuntimeError, match="failed to import"):
        bw.ensure_build_deps_archive_imported(tmp_path)
