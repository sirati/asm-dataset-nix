"""Unit tests for :mod:`compiler_suit_runner.workers.build_compilers_worker`.

All nix subprocess calls are stubbed; no real /nix/store interaction.
Tests cover:

  * payload parsing (happy + every shape-error path);
  * resume short-circuit when the archive already exists;
  * realise success → export success → result OK;
  * realise failure → result error with "nix build" in the message
    (signals retry-eligible to the main-loop classifier);
  * export-step ``nix-store --query --requisites`` failure → result
    error;
  * export-step ``nix-store --export`` failure → result error +
    archive cleanup;
  * archive_path_for layout matches the documented contract;
  * end-to-end via run_build_compilers_task with the injected runner.
"""

from __future__ import annotations

import pathlib
from typing import Any, Optional

import pytest

from compiler_suit_runner.workers import build_compilers_worker as bcw


# ---------------------------------------------------------------------------
# Stubbed subprocess runner
# ---------------------------------------------------------------------------


class _NixSubprocessStub:
    """Argv-sniffing stub for the worker's nix invocations.

    Each call is recorded so tests can assert on argv shape; the
    return value is keyed off ``argv[0]`` + the first non-flag arg.

    Tests configure responses by writing into ``responses`` directly.
    Unknown argv combinations raise so the test author notices any
    code path that escapes the stub.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        # Default responses (overridden per test):
        self.build_stdout = b""
        self.build_stderr = b""
        self.build_rc = 0
        self.req_stdout = b""
        self.req_stderr = b""
        self.req_rc = 0
        self.export_stdout = b""
        self.export_stderr = b""
        self.export_rc = 0

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        if argv[:2] == ["nix", "build"]:
            return self.build_stdout, self.build_stderr, self.build_rc
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            return self.req_stdout, self.req_stderr, self.req_rc
        if argv[:2] == ["nix-store", "--export"]:
            return self.export_stdout, self.export_stderr, self.export_rc
        raise AssertionError(f"unexpected argv to stub: {argv!r}")


@pytest.fixture
def out_network(tmp_path: pathlib.Path) -> pathlib.Path:
    """A fresh per-test out_network root."""
    root = tmp_path / "out-network"
    root.mkdir()
    return root


@pytest.fixture
def env(out_network: pathlib.Path) -> bcw.BuildCompilersEnv:
    return bcw.BuildCompilersEnv(
        flake_ref="path:/dummy",
        out_network=out_network,
        run_subprocess=_NixSubprocessStub(),
    )


# ---------------------------------------------------------------------------
# parse_manifest_payload
# ---------------------------------------------------------------------------


class TestParseManifestPayload:

    def test_minimal_drv(self):
        out = bcw.parse_manifest_payload({
            "sys": "x86_64-linux",
            "arch": "aarch64",
            "compiler_label": "gcc15",
            "drv": "/nix/store/abc-toolchain-aarch64-gcc15.drv",
        })
        assert out["arch"] == "aarch64"
        assert out["drv"].endswith(".drv")
        assert out["attr"] is None

    def test_attr_fallback(self):
        out = bcw.parse_manifest_payload({
            "sys": "x86_64-linux",
            "arch": "aarch64",
            "compiler_label": "gcc15",
            "attr": "_crossToolchainMap.x86_64-linux.aarch64.gcc15",
        })
        assert out["drv"] is None
        assert out["attr"].startswith("_crossToolchainMap")

    def test_both_drv_and_attr(self):
        out = bcw.parse_manifest_payload({
            "sys": "x86_64-linux",
            "arch": "aarch64",
            "compiler_label": "gcc15",
            "drv": "/nix/store/abc-x.drv",
            "attr": "some.attr",
        })
        assert out["drv"].endswith(".drv")
        assert out["attr"] == "some.attr"

    @pytest.mark.parametrize("bad", [None, 123, [], "raw_string"])
    def test_non_dict_payload(self, bad):
        with pytest.raises(ValueError, match="must be a dict"):
            bcw.parse_manifest_payload(bad)

    def test_missing_sys(self):
        with pytest.raises(ValueError, match="invalid 'sys'"):
            bcw.parse_manifest_payload({
                "arch": "aarch64", "compiler_label": "gcc15", "drv": "/x.drv",
            })

    def test_missing_arch(self):
        with pytest.raises(ValueError, match="invalid 'arch'"):
            bcw.parse_manifest_payload({
                "sys": "x86_64-linux", "compiler_label": "gcc15", "drv": "/x.drv",
            })

    def test_missing_compiler_label(self):
        with pytest.raises(ValueError, match="invalid 'compiler_label'"):
            bcw.parse_manifest_payload({
                "sys": "x86_64-linux", "arch": "aarch64", "drv": "/x.drv",
            })

    def test_drv_must_end_in_drv(self):
        with pytest.raises(ValueError, match="invalid 'drv'"):
            bcw.parse_manifest_payload({
                "sys": "x86_64-linux", "arch": "aarch64",
                "compiler_label": "gcc15",
                "drv": "/nix/store/abc-x",  # no .drv suffix
            })

    def test_missing_both_drv_and_attr(self):
        with pytest.raises(ValueError, match="at least one of"):
            bcw.parse_manifest_payload({
                "sys": "x86_64-linux", "arch": "aarch64",
                "compiler_label": "gcc15",
            })


# ---------------------------------------------------------------------------
# archive_path_for
# ---------------------------------------------------------------------------


class TestArchivePathFor:

    def test_layout(self, tmp_path: pathlib.Path):
        out = bcw.archive_path_for(tmp_path, "aarch64", "gcc15")
        assert out == tmp_path / "_build_compilers" / "aarch64__gcc15.nix-archive"

    def test_funny_compiler_label_preserved(self, tmp_path: pathlib.Path):
        # Underscores in compiler_label are preserved; we use ``__`` as
        # the separator between arch and compiler_label.
        out = bcw.archive_path_for(tmp_path, "x86_64", "clang_17")
        assert out.name == "x86_64__clang_17.nix-archive"


# ---------------------------------------------------------------------------
# realise_toolchain
# ---------------------------------------------------------------------------


class TestRealiseToolchain:

    def test_build_by_drv_argv_shape(self, env):
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_stdout = b"/nix/store/aaa-out\n/nix/store/bbb-lib\n"
        ok, outpaths, _stdout, _stderr = bcw.realise_toolchain(
            {"sys": "x86_64-linux", "arch": "aarch64",
             "compiler_label": "gcc15",
             "drv": "/nix/store/zzz-gcc15.drv", "attr": None},
            env,
        )
        assert ok is True
        assert outpaths == ["/nix/store/aaa-out", "/nix/store/bbb-lib"]
        argv = stub.calls[0]
        assert argv[:4] == ["nix", "build", "--no-link", "--print-out-paths"]
        # Last positional is the drv-with-all-outputs spec.
        assert argv[-1] == "/nix/store/zzz-gcc15.drv^*"

    def test_build_by_attr_when_no_drv(self, env):
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_stdout = b"/nix/store/aaa-out\n"
        ok, outpaths, _stdout, _stderr = bcw.realise_toolchain(
            {"sys": "x86_64-linux", "arch": "aarch64",
             "compiler_label": "gcc15",
             "drv": None, "attr": "some.attr"},
            env,
        )
        assert ok is True
        assert outpaths == ["/nix/store/aaa-out"]
        argv = stub.calls[0]
        # Last positional uses the flake_ref#attr form.
        assert argv[-1] == "path:/dummy#some.attr"

    def test_build_failure_propagates(self, env):
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_rc = 1
        stub.build_stderr = b"error: a thing went wrong"
        ok, outpaths, _stdout, stderr = bcw.realise_toolchain(
            {"sys": "x86_64-linux", "arch": "aarch64",
             "compiler_label": "gcc15",
             "drv": "/nix/store/z.drv", "attr": None},
            env,
        )
        assert ok is False
        assert outpaths == []
        assert b"a thing went wrong" in stderr

    def test_substituters_file_spliced(
        self, env, tmp_path: pathlib.Path,
    ):
        subs = tmp_path / "subs.list"
        subs.write_text(
            "--extra-substituters\nhttp://peer:1234\n"
            "--extra-trusted-public-keys\nkey=base64\n",
            encoding="utf-8",
        )
        env.substituters_file = subs
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_stdout = b"/nix/store/aaa\n"
        bcw.realise_toolchain(
            {"sys": "x86_64-linux", "arch": "aarch64",
             "compiler_label": "gcc15",
             "drv": "/nix/store/z.drv", "attr": None},
            env,
        )
        argv = stub.calls[0]
        # The four substituter lines all appear between the
        # --print-out-paths flag and the trailing drv spec.
        assert "--extra-substituters" in argv
        assert "http://peer:1234" in argv
        assert "--extra-trusted-public-keys" in argv


# ---------------------------------------------------------------------------
# export_closure
# ---------------------------------------------------------------------------


class TestExportClosure:

    def test_happy_path_writes_archive(
        self, tmp_path: pathlib.Path,
    ):
        stub = _NixSubprocessStub()
        stub.req_stdout = (
            b"/nix/store/aaa-out\n/nix/store/bbb-dep\n"
            b"/nix/store/zzz-gcc15.drv\n"
        )
        stub.export_stdout = b"BINARY-ARCHIVE-CONTENTS"
        archive = tmp_path / "subdir" / "out.nix-archive"

        ok, _req_err, _exp_err = bcw.export_closure(
            archive, ["/nix/store/aaa-out", "/nix/store/zzz-gcc15.drv"],
            run_subprocess=stub,
        )
        assert ok is True
        assert archive.read_bytes() == b"BINARY-ARCHIVE-CONTENTS"

        # The export argv carries the requisites closure (every path on
        # stdout from the --query --requisites call), not the seeds.
        export_call = [c for c in stub.calls
                       if c[:2] == ["nix-store", "--export"]][0]
        assert "/nix/store/aaa-out" in export_call
        assert "/nix/store/bbb-dep" in export_call
        assert "/nix/store/zzz-gcc15.drv" in export_call

    def test_requisites_query_failure(self, tmp_path: pathlib.Path):
        stub = _NixSubprocessStub()
        stub.req_rc = 1
        stub.req_stderr = b"oh no"
        archive = tmp_path / "out.nix-archive"
        ok, req_err, _exp_err = bcw.export_closure(
            archive, ["/nix/store/x"], run_subprocess=stub,
        )
        assert ok is False
        assert b"oh no" in req_err
        assert not archive.exists()

    def test_no_requisites_returned(self, tmp_path: pathlib.Path):
        """Successful query but empty stdout shouldn't accidentally
        produce an empty archive."""
        stub = _NixSubprocessStub()
        stub.req_rc = 0
        stub.req_stdout = b""
        archive = tmp_path / "out.nix-archive"
        ok, _req_err, _exp_err = bcw.export_closure(
            archive, ["/nix/store/x"], run_subprocess=stub,
        )
        assert ok is False
        assert not archive.exists()

    def test_export_failure_cleans_tmp(self, tmp_path: pathlib.Path):
        stub = _NixSubprocessStub()
        stub.req_stdout = b"/nix/store/aaa\n"
        stub.export_rc = 1
        stub.export_stderr = b"oh no"
        archive = tmp_path / "out.nix-archive"

        ok, _req_err, exp_err = bcw.export_closure(
            archive, ["/nix/store/aaa"], run_subprocess=stub,
        )
        assert ok is False
        assert b"oh no" in exp_err
        assert not archive.exists()
        # tmp file also cleaned up.
        assert not archive.with_suffix(".nix-archive.tmp").exists()

    def test_empty_seed_paths(self, tmp_path: pathlib.Path):
        stub = _NixSubprocessStub()
        archive = tmp_path / "out.nix-archive"
        ok, _req_err, _exp_err = bcw.export_closure(
            archive, [], run_subprocess=stub,
        )
        assert ok is False
        assert stub.calls == []  # never invoked the subprocess


# ---------------------------------------------------------------------------
# run_build_compilers_task (end-to-end with stubs)
# ---------------------------------------------------------------------------


def _payload(**overrides) -> dict:
    base = {
        "sys": "x86_64-linux",
        "arch": "aarch64",
        "compiler_label": "gcc15",
        "drv": "/nix/store/aaaa-toolchain-aarch64-gcc15.drv",
    }
    base.update(overrides)
    return base


class TestRunBuildCompilersTask:

    def test_happy_path(self, env):
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_stdout = b"/nix/store/aaa-out\n/nix/store/bbb-lib\n"
        stub.req_stdout = b"/nix/store/aaa-out\n/nix/store/bbb-lib\n/nix/store/aaaa-toolchain-aarch64-gcc15.drv\n"
        stub.export_stdout = b"NAR"

        result = bcw.run_build_compilers_task(
            _payload(), env, name="build_compilers__aarch64__gcc15",
        )
        assert result.success is True
        assert result.archive_path is not None
        assert result.archive_path.read_bytes() == b"NAR"
        assert (
            result.archive_path.name == "aarch64__gcc15.nix-archive"
        )
        assert result.outpaths == (
            "/nix/store/aaa-out", "/nix/store/bbb-lib",
        )

    def test_resume_short_circuit(self, env):
        """Pre-existing non-empty archive → fast no-op, no subprocess."""
        stub: _NixSubprocessStub = env.run_subprocess
        archive = bcw.archive_path_for(env.out_network, "aarch64", "gcc15")
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"previous archive")

        result = bcw.run_build_compilers_task(_payload(), env, name="x")
        assert result.success is True
        assert result.archive_path == archive
        # No nix build / export invocations.
        assert stub.calls == []

    def test_realise_failure(self, env):
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_rc = 1
        stub.build_stderr = b"nix-eval blew up\nline 2 of error"

        result = bcw.run_build_compilers_task(_payload(), env, name="x")
        assert result.success is False
        # Message must read 'nix build' so the main-loop classifier
        # routes it to ErrorType::Errored (retry-eligible).
        assert "nix build" in (result.error or "").lower()
        assert result.archive_path is None
        # Stderr excerpt captured for triage.
        assert result.nix_log_excerpt is not None

    def test_realise_returns_zero_outpaths(self, env):
        """nix build rc=0 but no out-paths printed → failure result."""
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_stdout = b"   \n\n"
        result = bcw.run_build_compilers_task(_payload(), env, name="x")
        assert result.success is False
        assert "no out-paths" in (result.error or "")

    def test_export_failure(self, env):
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_stdout = b"/nix/store/aaa\n"
        stub.req_stdout = b"/nix/store/aaa\n"
        stub.export_rc = 1
        stub.export_stderr = b"export borked"

        result = bcw.run_build_compilers_task(_payload(), env, name="x")
        assert result.success is False
        assert "closure export failed" in (result.error or "")
        # Realise succeeded so outpaths still populated for triage.
        assert result.outpaths == ("/nix/store/aaa",)
        # The export-side stderr made it into the excerpt.
        assert "export borked" in (result.nix_log_excerpt or "")

    def test_requisites_failure_surfaces_stderr_in_excerpt(self, env):
        """When ``nix-store --query --requisites`` fails (the FIRST
        sub-call of export_closure), its stderr must still reach the
        result excerpt — previously only the export-side stderr was
        surfaced, hiding the actual error. Observed on LMU 2026-05-21
        as the silent ``closure export failed`` failure mode."""
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_stdout = b"/nix/store/aaa\n"
        # Requisites query fails first; no export call follows.
        stub.req_rc = 1
        stub.req_stderr = b"error: path '/nix/store/aaa' is not valid"

        result = bcw.run_build_compilers_task(_payload(), env, name="x")
        assert result.success is False
        assert "closure export failed" in (result.error or "")
        # The requisites-side stderr is preserved verbatim in the
        # excerpt so the operator can see what nix-store complained
        # about.
        assert "not valid" in (result.nix_log_excerpt or ""), \
            f"got excerpt: {result.nix_log_excerpt!r}"

    def test_bad_payload_shape(self, env):
        result = bcw.run_build_compilers_task(
            {"sys": "x86_64-linux"},  # missing arch / compiler_label / drv|attr
            env, name="x",
        )
        assert result.success is False
        assert "manifest parse failed" in (result.error or "")

    def test_drv_field_included_in_export_seed(self, env):
        """The drv path is appended to seed paths so the export closure
        also captures the drv file itself (needed for downstream
        ``nix-store --query --tree`` on the primary)."""
        stub: _NixSubprocessStub = env.run_subprocess
        stub.build_stdout = b"/nix/store/out-aaa\n"
        stub.req_stdout = b"/nix/store/out-aaa\n"
        stub.export_stdout = b"NAR"
        result = bcw.run_build_compilers_task(_payload(), env, name="x")
        assert result.success is True
        # Find the --query --requisites call.
        req_calls = [c for c in stub.calls
                     if c[:3] == ["nix-store", "--query", "--requisites"]]
        assert req_calls
        seed_args = req_calls[0][3:]
        assert "/nix/store/out-aaa" in seed_args
        assert "/nix/store/aaaa-toolchain-aarch64-gcc15.drv" in seed_args


# ---------------------------------------------------------------------------
# parse_archive_sidecar (forward-compat helper)
# ---------------------------------------------------------------------------


class TestParseArchiveSidecar:

    def test_missing_sidecar_returns_empty(self, tmp_path: pathlib.Path):
        archive = tmp_path / "x.nix-archive"
        assert bcw.parse_archive_sidecar(archive) == {}

    def test_present_sidecar_parsed(self, tmp_path: pathlib.Path):
        archive = tmp_path / "x.nix-archive"
        sidecar = archive.with_suffix(".nix-archive.json")
        sidecar.write_text('{"hello": "world"}', encoding="utf-8")
        out = bcw.parse_archive_sidecar(archive)
        assert out == {"hello": "world"}

    def test_malformed_sidecar_returns_empty(self, tmp_path: pathlib.Path):
        archive = tmp_path / "x.nix-archive"
        sidecar = archive.with_suffix(".nix-archive.json")
        sidecar.write_text("not json", encoding="utf-8")
        assert bcw.parse_archive_sidecar(archive) == {}

    def test_non_dict_top_level(self, tmp_path: pathlib.Path):
        archive = tmp_path / "x.nix-archive"
        sidecar = archive.with_suffix(".nix-archive.json")
        sidecar.write_text('["a", "b"]', encoding="utf-8")
        assert bcw.parse_archive_sidecar(archive) == {}


# ---------------------------------------------------------------------------
# Torn-PATH hardening (respawn-env): bare nix argv[0] gets resolved
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
        ), patch.object(bcw.subprocess, "run", _fake_run):
            bcw._default_run_subprocess(["nix", "build", "--no-link"])
        assert calls == [["/bin/nix", "build", "--no-link"]]

    def test_export_closure_direct_branch_resolves_nix_store(
        self, tmp_path: pathlib.Path,
    ):
        """The streaming export path (run_subprocess=None) execs the
        resolved nix-store, not the bare name."""
        from unittest.mock import patch

        calls: list[list[str]] = []

        def _fake_run(argv, **kwargs):
            calls.append(list(argv))

            class _Proc:
                stdout = (
                    b"/nix/store/aaa-x.drv\n"
                    if argv[1] == "--query" else b""
                )
                stderr = b""
                returncode = 0

            return _Proc()

        archive = tmp_path / "toolchain.nix-archive"
        with patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".subproc.shutil.which",
            return_value=None,
        ), patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".subproc.os.path.exists",
            lambda path: str(path).startswith("/bin/"),
        ), patch.object(bcw.subprocess, "run", _fake_run):
            ok, _req_err, _exp_err = bcw.export_closure(
                archive, ["/nix/store/aaa-x.drv"],
            )
        assert ok is True
        # Both the requisites query (via the default runner) and the
        # direct streaming export resolved argv[0].
        assert [c[0] for c in calls] == ["/bin/nix-store", "/bin/nix-store"]
        assert calls[1][:2] == ["/bin/nix-store", "--export"]
