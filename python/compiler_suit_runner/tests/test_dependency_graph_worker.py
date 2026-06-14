"""Unit tests for :mod:`compiler_suit_runner.workers.dependency_graph_worker`.

All nix invocations are stubbed; the streaming planner and sum-drv
builder are monkey-patched so the tests stay dependency-light (no
template_graph eval, no /nix/store).

Coverage:

  * archive discovery (sorted, file-only, suffix filter);
  * variant-lookup derivation from the matrix aggregate drv
    (filter, label compose, collision detection);
  * import_archive happy + missing-file + subprocess-failure paths,
    including stdout-path capture;
  * --toolchain-task-id parser;
  * end-to-end run_dependency_graph_task with stubbed planner +
    sum-drv builder + subprocess;
  * Phase 6.1 lax-mode default: ``plan_total`` defaults to lax=True so
    a calibration-mismatch tree records violations rather than raising.
"""

from __future__ import annotations

import pathlib
from typing import Optional

import pytest

from compiler_suit_runner.workers import dependency_graph_worker as dgw
from compiler_suit_runner.dependency_graph_planner import Phase4Descriptor


# ---------------------------------------------------------------------------
# Fake framework task (Wave-1 send_message capture)
# ---------------------------------------------------------------------------


class _FakeStreamTask:
    """Minimal stand-in for the framework ``Task`` handle.

    ``run_dependency_graph_task`` requires a ``task`` with the Wave-1
    ``send_message(topic, data)`` API for the streamed-spawn handoff;
    this fake just captures ``(topic, bytes)`` tuples so the existing
    tests stay green (streaming behaviour is asserted elsewhere).
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes]] = []

    def send_message(self, topic: str, data: bytes) -> None:
        self.messages.append((topic, data))


# ---------------------------------------------------------------------------
# Subprocess stub
# ---------------------------------------------------------------------------


class _SubprocessStub:
    """Argv-sniffing stub configurable per command kind.

    Each invocation records the argv; the stub returns
    ``(stdout, stderr, rc)`` according to the first matching response
    registered for the argv prefix.

    ``_stdin_aware = True`` signals to ``import_archive`` that this
    stub wants the synthetic ``<<N bytes>>`` argv form instead of a
    real stdin file descriptor; production wrappers omit the attribute
    so they take the real ``subprocess.run`` with stdin streaming.
    """

    _stdin_aware = True

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.path_info_present: set[str] = set()
        self.path_info_rc: Optional[int] = None
        self.import_rc = 0
        self.import_stderr = b""
        # Default stdout returned for every ``nix-store --import``
        # call. Override with ``import_stdout_queue`` to feed
        # per-archive responses in invocation order.
        self.import_stdout = b""
        self.import_stdout_queue: list[bytes] = []
        # Per-call return-code queue for ``nix-store --import`` (consumed
        # in invocation order). With toolchain-first import the first
        # import is the shared ``toolchains.drv.archive``; a queue lets a
        # test pass that import then fail the per-binary one. Falls back
        # to the scalar ``import_rc`` when exhausted/empty.
        self.import_rc_queue: list[int] = []
        self.tree_stdout = b""
        self.tree_rc = 0

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        if argv[:2] == ["nix", "path-info"]:
            store_path = argv[-1]
            if self.path_info_rc is not None:
                return b"", b"", self.path_info_rc
            present = store_path in self.path_info_present
            return b"", b"", 0 if present else 1
        if argv[:2] == ["nix-store", "--import"]:
            if self.import_stdout_queue:
                stdout = self.import_stdout_queue.pop(0)
            else:
                stdout = self.import_stdout
            rc = (
                self.import_rc_queue.pop(0)
                if self.import_rc_queue else self.import_rc
            )
            return stdout, self.import_stderr, rc
        if argv[:3] == ["nix-store", "--query", "--tree"]:
            return self.tree_stdout, b"", self.tree_rc
        raise AssertionError(f"unexpected argv to stub: {argv!r}")


# ---------------------------------------------------------------------------
# discover_archives
# ---------------------------------------------------------------------------


class TestDiscoverArchives:

    def test_empty_dir(self, tmp_path: pathlib.Path):
        assert dgw.discover_archives(tmp_path) == []

    def test_missing_dir(self, tmp_path: pathlib.Path):
        assert dgw.discover_archives(tmp_path / "no-such-dir") == []

    def test_only_archive_suffix(self, tmp_path: pathlib.Path):
        (tmp_path / "matrix-hello.drv.archive").write_bytes(b"x")
        # Non-matching files: wrong prefix, wrong suffix, legacy name,
        # bare metadata file, and a directory.
        (tmp_path / "hello.txt").write_bytes(b"x")
        (tmp_path / "hello.nix-archive").write_bytes(b"x")
        (tmp_path / "matrix-hello.drv").write_bytes(b"x")
        (tmp_path / "_meta.json").write_bytes(b"x")
        (tmp_path / "subdir").mkdir()
        out = dgw.discover_archives(tmp_path)
        assert [p.name for p in out] == ["matrix-hello.drv.archive"]

    def test_sorted_by_name(self, tmp_path: pathlib.Path):
        (tmp_path / "matrix-zebra.drv.archive").write_bytes(b"x")
        (tmp_path / "matrix-hello.drv.archive").write_bytes(b"x")
        (tmp_path / "matrix-alpha.drv.archive").write_bytes(b"x")
        out = dgw.discover_archives(tmp_path)
        assert [
            dgw.binary_from_archive_name(p) for p in out
        ] == ["alpha", "hello", "zebra"]


# ---------------------------------------------------------------------------
# Variant-lookup derivation from a matrix aggregate drv
# ---------------------------------------------------------------------------


def _variant_drv(
    binary: str, arch: str, opt: str = "O0", comp: str = "gcc15",
    *, hash_prefix: str,
    inner: str = "baseline-default-san-off-march-default",
) -> str:
    """Build a synthetic ``/nix/store/<hash>-<...>-elf-folder.drv`` path.

    Mirrors :func:`template_graph.tests.test_streaming.fixtures.variant_name`
    so the produced post-hash basename is accepted by
    :func:`parse_variant_path`. ``hash_prefix`` is short-form (any
    length) and is right-padded with ``a`` to the 32-char base32 hash
    width expected by ``_post_hash_basename``.
    """
    h = (hash_prefix + "a" * 32)[:32]
    name = f"{binary}-{arch}-{comp}-{opt}-{inner}-elf-folder.drv"
    return f"/nix/store/{h}-{name}"


# ---------------------------------------------------------------------------
# derive_variant_lookup_from_aggregate
# ---------------------------------------------------------------------------


class _RefsStub:
    """Argv-sniffing stub for ``nix-store --query --references <drv>``.

    Returns the configured ``(stdout, stderr, rc)`` for every
    ``nix-store --query --references`` argv; raises on any other argv
    so the production helper can't accidentally fan out into extra
    nix calls.
    """

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        rc: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        if argv[:3] == ["nix-store", "--query", "--references"]:
            return self.stdout, self.stderr, self.rc
        raise AssertionError(f"unexpected argv to refs stub: {argv!r}")


class TestDeriveVariantLookupFromAggregate:

    AGG_DRV = (
        "/nix/store/ffffffffffffffffffffffffffffffff-matrix-hello.drv"
    )

    def test_filters_elf_folder_and_parses_variants(self):
        v_hello = _variant_drv("hello", "x86_64", "O0", hash_prefix="hh1")
        v_world = _variant_drv("hello", "aarch64", "O2", hash_prefix="ww1")
        toolchain = (
            "/nix/store/cccccccccccccccccccccccccccccccc-toolchains.drv"
        )
        bash_drv = (
            "/nix/store/dddddddddddddddddddddddddddddddd-bash-5.2.drv"
        )
        stub = _RefsStub(
            stdout=(
                v_hello + "\n"
                + toolchain + "\n"
                + v_world + "\n"
                + bash_drv + "\n"
            ).encode("utf-8"),
        )
        lookup = dgw.derive_variant_lookup_from_aggregate(
            self.AGG_DRV, run_subprocess=stub,
        )
        # Exactly the two elf-folder leaves landed in the lookup;
        # toolchain + bash references were filtered out.
        assert len(lookup) == 2
        h_label = (
            "hello__x86_64__gcc15-O0-baseline-default-san-off-march-default"
        )
        w_label = (
            "hello__aarch64__gcc15-O2-baseline-default-san-off-march-default"
        )
        assert ("x86_64", h_label) in lookup
        assert ("aarch64", w_label) in lookup
        from compiler_suit_runner.preflight import (  # noqa: PLC0415
            _short_dataset_name,
        )
        expected_vd = _short_dataset_name(
            compiler_id="gcc15", arch="x86_64", optimization="O0",
            full_label=h_label,
        )
        assert lookup[("x86_64", h_label)] == {
            "drv": v_hello,
            "pkg": "hello",
            "arch": "x86_64",
            "label": h_label,
            "suffix": "gcc15-O0-baseline-default-san-off-march-default",
            "compiler_id": "gcc15",
            "compiler_family": "gcc",
            "compiler_version": "15",
            "optimization": "O0",
            "variant_dir": expected_vd,
            "metadata_name": f"{expected_vd}.json",
            "toolchain_outpath": "",
        }
        # variant_dir / compiler_id MUST be non-empty so the spawned
        # build_variant task is complete (the build worker hard-fails
        # on an empty payload.variant_dir).
        assert lookup[("x86_64", h_label)]["variant_dir"]
        assert lookup[("x86_64", h_label)]["compiler_id"] == "gcc15"
        # The helper made exactly one nix-store call.
        assert len(stub.calls) == 1
        assert stub.calls[0] == [
            "nix-store", "--query", "--references", self.AGG_DRV,
        ]

    def test_only_non_elf_folder_references_yields_empty_lookup(self):
        stub = _RefsStub(
            stdout=(
                "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc.drv\n"
                "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-bash-5.2.drv\n"
                "/nix/store/cccccccccccccccccccccccccccccccc-toolchains.drv\n"
            ).encode("utf-8"),
        )
        lookup = dgw.derive_variant_lookup_from_aggregate(
            self.AGG_DRV, run_subprocess=stub,
        )
        assert lookup == {}

    def test_collision_raises_value_error(self):
        v_a = _variant_drv("hello", "x86_64", "O0", hash_prefix="aa1")
        v_b = _variant_drv("hello", "x86_64", "O0", hash_prefix="bb2")
        # Same (binary, arch, comp, opt, inner) — distinct hashes only
        # (which the lookup deliberately does NOT separate on).
        stub = _RefsStub(
            stdout=(v_a + "\n" + v_b + "\n").encode("utf-8"),
        )
        with pytest.raises(ValueError, match="duplicate variant key"):
            dgw.derive_variant_lookup_from_aggregate(
                self.AGG_DRV, run_subprocess=stub,
            )

    def test_subprocess_failure_surfaces_runtime_error(self):
        stub = _RefsStub(
            stdout=b"", stderr=b"no such path", rc=1,
        )
        with pytest.raises(RuntimeError, match="no such path"):
            dgw.derive_variant_lookup_from_aggregate(
                self.AGG_DRV, run_subprocess=stub,
            )

    def test_unparseable_variant_skipped_with_warning(self, caplog):
        import logging  # noqa: PLC0415
        bad = (
            "/nix/store/cccccccccccccccccccccccccccccccc-"
            "not-a-variant-elf-folder.drv"
        )
        good = _variant_drv("hello", "x86_64", "O0", hash_prefix="hh1")
        stub = _RefsStub(
            stdout=(bad + "\n" + good + "\n").encode("utf-8"),
        )
        with caplog.at_level(
            logging.WARNING,
            logger="compiler_suit_runner.dependency_graph_worker",
        ):
            lookup = dgw.derive_variant_lookup_from_aggregate(
                self.AGG_DRV, run_subprocess=stub,
            )
        # The good variant landed; the unparseable one was dropped.
        assert len(lookup) == 1
        assert any(
            "unparseable variant drv" in r.message
            for r in caplog.records
        )

    def test_blank_lines_in_stdout_ignored(self):
        v = _variant_drv("hello", "x86_64", "O0", hash_prefix="hh1")
        stub = _RefsStub(
            stdout=("\n" + v + "\n\n").encode("utf-8"),
        )
        lookup = dgw.derive_variant_lookup_from_aggregate(
            self.AGG_DRV, run_subprocess=stub,
        )
        assert len(lookup) == 1


class TestVariantSpawnPayloadEndToEnd:
    """The phase-3 → phase-4 spawn chain: a variant leaf in the matrix
    aggregate must become a ``build_variant`` TaskInfo whose payload
    carries every key the build worker reads — at minimum a non-empty
    ``variant_dir`` (the worker hard-fails on
    ``build_variant manifest missing 'payload.variant_dir'``) and a
    non-empty ``compiler_id`` (so ``_classify`` yields a
    ``"<compiler>-<arch>"`` affinity, never a leading-dash
    ``"-<arch>"``).

    Exercises the real chain end-to-end:
    ``derive_variant_lookup_from_aggregate`` -> ``_variant_descriptor``
    -> ``headers_from_descriptors`` -> ``_header_to_task_info``.
    """

    AGG_DRV = (
        "/nix/store/ffffffffffffffffffffffffffffffff-matrix-hello.drv"
    )

    def _build_taskinfo_for(self, drv: str):
        from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415,E501
            headers_from_descriptors,
        )
        from compiler_suit_runner.dependency_graph_planner.descriptors import (  # noqa: PLC0415,E501
            _variant_descriptor,
        )
        from compiler_suit_runner.suit_task import (  # noqa: PLC0415
            _header_to_task_info,
        )

        stub = _RefsStub(stdout=(drv + "\n").encode("utf-8"))
        lookup = dgw.derive_variant_lookup_from_aggregate(
            self.AGG_DRV, run_subprocess=stub,
        )
        assert len(lookup) == 1
        (arch, label), spec = next(iter(lookup.items()))
        descriptor = _variant_descriptor(
            binary="hello",
            arch=arch,
            sys_name="x86_64-linux",
            label=label,
            variant_spec=spec,
            depends_on=(),
        )
        headers = headers_from_descriptors([descriptor])
        assert len(headers) == 1
        return _header_to_task_info(headers[0]), spec

    def test_payload_carries_variant_dir_and_compiler(self):
        drv = _variant_drv(
            "hello", "x86_64", "O2", comp="clang20", hash_prefix="hh1",
        )
        ti, spec = self._build_taskinfo_for(drv)
        # _header_to_task_info wraps the header into the same outer
        # ``{item_class, name, size, payload}`` dict discover_items
        # emits; the build worker reads ``manifest.get("payload")`` to
        # get the inner variant payload. Assert on that inner dict.
        assert ti.payload["item_class"] == "build_variant"
        payload = ti.payload["payload"]
        # The key the build worker hard-requires.
        assert payload["variant_dir"]
        assert payload["variant_dir"] == spec["variant_dir"]
        # Compiler axes the affinity + sidecar read.
        assert payload["compiler_id"] == "clang20"
        assert payload["compiler_family"] == "clang"
        assert payload["pkg"] == "hello"
        assert payload["arch"] == "x86_64"
        assert payload["optimization"] == "O2"
        assert payload["metadata_name"] == f"{spec['variant_dir']}.json"
        # ``attr`` is reconstructed by headers_from_descriptors and the
        # build drv path is threaded through unchanged.
        assert payload["attr"] == (
            f"dataset.x86_64-linux.hello.x86_64.{payload['label']}"
        )
        assert payload["drv"] == drv

    def test_affinity_is_compiler_dash_arch(self):
        drv = _variant_drv(
            "hello", "aarch64", "O0", comp="gcc15", hash_prefix="bb2",
        )
        ti, _spec = self._build_taskinfo_for(drv)
        # The bug was ``"-aarch64"`` (empty compiler prefix); assert the
        # compiler half is present and non-empty.
        assert ti.affinity_id == "gcc15-aarch64"
        assert not ti.affinity_id.startswith("-")

    def test_toolchain_outpath_flows_from_map_to_build_variant_payload(self):
        """toolchain_outpaths_map supplied to derive_variant_lookup_from_aggregate
        causes the build_variant descriptor payload to carry a non-empty
        toolchain_outpath — proving the full streamed-spawn pipeline threads
        the outpath from cli.py → dep_graph payload → variant descriptor → TaskInfo.
        """
        from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
            headers_from_descriptors,
        )
        from compiler_suit_runner.dependency_graph_planner.descriptors import (  # noqa: PLC0415
            _variant_descriptor,
        )
        from compiler_suit_runner.suit_task import (  # noqa: PLC0415
            _header_to_task_info,
        )

        drv = _variant_drv("hello", "x86_64", "O2", comp="gcc15", hash_prefix="tc1")
        fake_outpath = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-gcc15"
        tc_outpaths_map = {"x86_64/gcc15": fake_outpath}

        stub = _RefsStub(stdout=(drv + "\n").encode("utf-8"))
        lookup = dgw.derive_variant_lookup_from_aggregate(
            self.AGG_DRV,
            run_subprocess=stub,
            toolchain_outpaths_map=tc_outpaths_map,
        )
        assert len(lookup) == 1
        (arch, label), spec = next(iter(lookup.items()))
        # The spec must carry the outpath.
        assert spec["toolchain_outpath"] == fake_outpath

        descriptor = _variant_descriptor(
            binary="hello",
            arch=arch,
            sys_name="x86_64-linux",
            label=label,
            variant_spec=spec,
            depends_on=(),
        )
        # The descriptor payload must carry it through to the wire protocol.
        assert descriptor.payload["toolchain_outpath"] == fake_outpath

        headers = headers_from_descriptors([descriptor])
        ti = _header_to_task_info(headers[0])
        # And the TaskInfo.payload (what the build worker sees) must carry it.
        assert ti.payload["payload"]["toolchain_outpath"] == fake_outpath


def lookup_key_for(drv_path: str) -> str:
    """Reconstruct the planner-form label for a synthetic variant drv.

    Test helper: parses the post-hash basename and re-derives the
    ``"<binary>__<arch>__<suffix>"`` label so assertions can be
    written without hard-coding the inner-axis tokens.
    """
    from template_graph.tree_walker import VARIANT_SUFFIX, parse_variant_path

    # _post_hash_basename mirror: strip "/nix/store/<32 chars>-".
    body = drv_path[len("/nix/store/") + 32 + 1:]
    binary, arch, _comp, _opt = parse_variant_path(body)
    suffix = body[len(binary) + 1 + len(arch) + 1 : -len(VARIANT_SUFFIX)]
    return f"{binary}__{arch}__{suffix}"


# ---------------------------------------------------------------------------
# is_path_locally_present
# ---------------------------------------------------------------------------


class TestIsPathLocallyPresent:

    def test_present(self):
        stub = _SubprocessStub()
        stub.path_info_present.add("/nix/store/aaa")
        assert dgw.is_path_locally_present(
            "/nix/store/aaa", run_subprocess=stub,
        ) is True

    def test_absent(self):
        stub = _SubprocessStub()
        assert dgw.is_path_locally_present(
            "/nix/store/aaa", run_subprocess=stub,
        ) is False


# ---------------------------------------------------------------------------
# import_archive (injected runner branch only — the direct subprocess
# branch needs a real /nix/store)
# ---------------------------------------------------------------------------


class TestImportArchive:

    def test_missing_archive(self, tmp_path: pathlib.Path):
        archive = tmp_path / "matrix-missing.drv.archive"
        stub = _SubprocessStub()
        ok, err, imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is False
        assert b"archive not found" in err
        assert imported == []
        # No subprocess invocation — short-circuit on missing file.
        assert stub.calls == []

    def test_present_archive_success(self, tmp_path: pathlib.Path):
        archive = tmp_path / "matrix-x.drv.archive"
        archive.write_bytes(b"fake-archive-bytes")
        stub = _SubprocessStub()
        stub.import_rc = 0
        stub.import_stdout = (
            b"/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-hello.drv\n"
            b"/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-glibc.drv\n"
        )
        ok, err, imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is True
        assert err == b""
        # Subprocess was invoked with --import.
        assert any(c[:2] == ["nix-store", "--import"] for c in stub.calls)
        # Stdout is parsed line-by-line into the imported_paths return.
        assert imported == [
            "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-hello.drv",
            "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-glibc.drv",
        ]

    def test_subprocess_failure(self, tmp_path: pathlib.Path):
        archive = tmp_path / "matrix-x.drv.archive"
        archive.write_bytes(b"junk")
        stub = _SubprocessStub()
        stub.import_rc = 1
        stub.import_stderr = b"corrupt archive"
        stub.import_stdout = b"/nix/store/partial.drv\n"
        ok, err, imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is False
        assert b"corrupt" in err
        # Failure path: imported_paths is empty regardless of partial
        # stdout from the failed subprocess.
        assert imported == []

    def test_empty_stdout_yields_empty_paths(
        self, tmp_path: pathlib.Path,
    ):
        archive = tmp_path / "matrix-x.drv.archive"
        archive.write_bytes(b"x")
        stub = _SubprocessStub()
        stub.import_stdout = b""
        ok, _err, imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is True
        assert imported == []

    def test_blank_lines_in_stdout_skipped(
        self, tmp_path: pathlib.Path,
    ):
        archive = tmp_path / "matrix-x.drv.archive"
        archive.write_bytes(b"x")
        stub = _SubprocessStub()
        stub.import_stdout = (
            b"\n/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-x.drv\n\n"
        )
        _ok, _err, imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert imported == [
            "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-x.drv",
        ]


# ---------------------------------------------------------------------------
# import_archive transient-failure retry (EAGAIN respawn-loop fix)
#
# Production evidence: under fork/pids pressure spawning
# ``nix-store --import`` raises ``[Errno 11] Resource temporarily
# unavailable``; treating that as permanent killed workers in a respawn
# loop. import_archive now retries transient failures with backoff;
# permanent failures still fail fast. The sleep hook is patched so no
# test ever actually sleeps.
# ---------------------------------------------------------------------------


class TestImportArchiveRetry:

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            archive as archive_mod,
        )
        self.archive_mod = archive_mod
        self.sleeps: list[float] = []
        monkeypatch.setattr(archive_mod, "_retry_sleep", self.sleeps.append)

    def test_transient_stderr_rc_retries_then_succeeds(
        self, tmp_path: pathlib.Path,
    ):
        archive = tmp_path / "toolchains.drv.archive"
        archive.write_bytes(b"x")
        stub = _SubprocessStub()
        stub.import_rc_queue = [1, 0]
        stub.import_stderr = (
            b"error: unable to fork: Resource temporarily unavailable"
        )
        stub.import_stdout = b"/nix/store/aaa-x.drv\n"
        ok, _err, imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is True
        assert imported == ["/nix/store/aaa-x.drv"]
        import_calls = [
            c for c in stub.calls if c[:2] == ["nix-store", "--import"]
        ]
        assert len(import_calls) == 2
        # One backoff sleep: base 1s plus up to 25% jitter.
        assert len(self.sleeps) == 1
        assert 1.0 <= self.sleeps[0] <= 1.25

    def test_cannot_fork_stderr_retries(self, tmp_path: pathlib.Path):
        archive = tmp_path / "matrix-x.drv.archive"
        archive.write_bytes(b"x")
        stub = _SubprocessStub()
        stub.import_rc_queue = [1, 0]
        stub.import_stderr = b"nix-store: cannot fork"
        ok, _err, _imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is True
        assert len(self.sleeps) == 1

    def test_permanent_rc_failure_does_not_retry(
        self, tmp_path: pathlib.Path,
    ):
        archive = tmp_path / "matrix-x.drv.archive"
        archive.write_bytes(b"x")
        stub = _SubprocessStub()
        stub.import_rc = 1
        stub.import_stderr = b"error: corrupt archive"
        ok, err, imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is False
        assert b"corrupt" in err
        assert imported == []
        import_calls = [
            c for c in stub.calls if c[:2] == ["nix-store", "--import"]
        ]
        assert len(import_calls) == 1
        assert self.sleeps == []

    def test_nix_does_not_exist_does_not_retry(
        self, tmp_path: pathlib.Path,
    ):
        archive = tmp_path / "matrix-x.drv.archive"
        archive.write_bytes(b"x")
        stub = _SubprocessStub()
        stub.import_rc = 1
        stub.import_stderr = (
            b"error: path '/nix/store/aaa-x.drv' does not exist"
        )
        ok, _err, _imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is False
        assert self.sleeps == []

    def test_missing_archive_does_not_retry(self, tmp_path: pathlib.Path):
        archive = tmp_path / "matrix-missing.drv.archive"
        stub = _SubprocessStub()
        ok, err, _imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is False
        assert b"archive not found" in err
        assert stub.calls == []
        assert self.sleeps == []

    def test_retries_exhausted_returns_failure(
        self, tmp_path: pathlib.Path,
    ):
        archive = tmp_path / "toolchains.drv.archive"
        archive.write_bytes(b"x")
        stub = _SubprocessStub()
        stub.import_rc = 1
        stub.import_stderr = b"Resource temporarily unavailable"
        ok, err, imported = dgw.import_archive(
            archive, run_subprocess=stub,
        )
        assert ok is False
        assert b"Resource temporarily unavailable" in err
        assert imported == []
        import_calls = [
            c for c in stub.calls if c[:2] == ["nix-store", "--import"]
        ]
        # 1 initial attempt + 5 retries.
        assert len(import_calls) == 6
        # Backoff schedule 1/2/4/8/16s, each plus up to 25% jitter.
        assert len(self.sleeps) == 5
        for base, actual in zip((1.0, 2.0, 4.0, 8.0, 16.0), self.sleeps):
            assert base <= actual <= base * 1.25

    def test_transient_oserror_direct_branch_retries_then_succeeds(
        self, tmp_path: pathlib.Path,
    ):
        """EAGAIN from spawning ``nix-store --import`` (the production
        respawn-loop signature) is retried; the third attempt succeeds."""
        import errno  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        archive = tmp_path / "toolchains.drv.archive"
        archive.write_bytes(b"fake")
        attempts: list[int] = []

        def _fake_run(argv, **_kwargs):
            attempts.append(1)
            if len(attempts) <= 2:
                raise OSError(
                    errno.EAGAIN, "Resource temporarily unavailable",
                )

            class _Proc:
                stdout = b"/nix/store/aaa-x.drv\n"
                stderr = b""
                returncode = 0

            return _Proc()

        with patch.object(self.archive_mod.subprocess, "run", _fake_run):
            ok, _err, imported = self.archive_mod.import_archive(archive)
        assert ok is True
        assert imported == ["/nix/store/aaa-x.drv"]
        assert len(attempts) == 3
        assert len(self.sleeps) == 2
        assert 1.0 <= self.sleeps[0] <= 1.25
        assert 2.0 <= self.sleeps[1] <= 2.5

    def test_permanent_oserror_direct_branch_does_not_retry(
        self, tmp_path: pathlib.Path,
    ):
        import errno  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        archive = tmp_path / "toolchains.drv.archive"
        archive.write_bytes(b"fake")
        attempts: list[int] = []

        def _fake_run(argv, **_kwargs):
            attempts.append(1)
            raise OSError(errno.EACCES, "Permission denied")

        with patch.object(self.archive_mod.subprocess, "run", _fake_run):
            ok, err, _imported = self.archive_mod.import_archive(archive)
        assert ok is False
        assert b"Permission denied" in err
        assert len(attempts) == 1
        assert self.sleeps == []


# ---------------------------------------------------------------------------
# resolve_tool / default_run_subprocess (torn-PATH hardening)
# ---------------------------------------------------------------------------


class TestResolveTool:
    """Workers can be respawned with a torn-down PATH (respawn-env);
    bare nix tool names must still resolve to an executable path.

    The worker container is a nix-built layered image with no
    ``/bin/nix-store``, so when which AND /bin both miss the resolver
    must glob ``/nix/store/*/bin/<name>`` (once per process per name,
    cached) and prefer the nix package's own store path.
    """

    @pytest.fixture(autouse=True)
    def _clean_tool_cache(self):
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        saved = dict(subproc._TOOL_CACHE)
        subproc._TOOL_CACHE.clear()
        yield
        subproc._TOOL_CACHE.clear()
        subproc._TOOL_CACHE.update(saved)

    @staticmethod
    def _bin_exists(path):
        return str(path).startswith("/bin/")

    @staticmethod
    def _nothing_exists(_path):
        return False

    def test_returns_which_result_when_on_path(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        with patch.object(
            subproc.shutil, "which",
            return_value="/nix/store/abc-nix/bin/nix-store",
        ):
            assert subproc.resolve_tool("nix-store") == (
                "/nix/store/abc-nix/bin/nix-store"
            )

    def test_falls_back_to_bin_when_not_on_path(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(subproc.os.path, "exists", self._bin_exists):
            assert subproc.resolve_tool("nix-store") == "/bin/nix-store"
            assert subproc.resolve_tool("nix") == "/bin/nix"

    def test_store_glob_prefers_nix_package_path(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(
                    subproc.os.path, "exists", self._nothing_exists,
                ), \
                patch.object(
                    subproc.glob, "glob",
                    return_value=[
                        "/nix/store/xyz-system-path/bin/nix-store",
                        "/nix/store/abc-nix-2.18/bin/nix-store",
                        "/nix/store/def-foo/bin/nix-store",
                    ],
                ):
            assert subproc.resolve_tool("nix-store") == (
                "/nix/store/abc-nix-2.18/bin/nix-store"
            )

    def test_store_glob_first_sorted_when_no_preferred(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(
                    subproc.os.path, "exists", self._nothing_exists,
                ), \
                patch.object(
                    subproc.glob, "glob",
                    return_value=[
                        "/nix/store/zzz-bar/bin/jq",
                        "/nix/store/aaa-foo/bin/jq",
                    ],
                ):
            assert subproc.resolve_tool("jq") == "/nix/store/aaa-foo/bin/jq"

    def test_everything_misses_returns_bare_name(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(
                    subproc.os.path, "exists", self._nothing_exists,
                ), \
                patch.object(subproc.glob, "glob", return_value=[]):
            assert subproc.resolve_tool("nix-store") == "nix-store"

    def test_store_glob_cached_per_tool_name(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        glob_calls: list[str] = []

        def _fake_glob(pattern):
            glob_calls.append(pattern)
            return ["/nix/store/abc-nix-2.18/bin/nix-store"]

        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(
                    subproc.os.path, "exists", self._nothing_exists,
                ), \
                patch.object(subproc.glob, "glob", _fake_glob):
            first = subproc.resolve_tool("nix-store")
            second = subproc.resolve_tool("nix-store")
        assert first == second == "/nix/store/abc-nix-2.18/bin/nix-store"
        assert glob_calls == ["/nix/store/*/bin/nix-store"]

    def test_store_glob_miss_cached_too(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        glob_calls: list[str] = []

        def _fake_glob(pattern):
            glob_calls.append(pattern)
            return []

        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(
                    subproc.os.path, "exists", self._nothing_exists,
                ), \
                patch.object(subproc.glob, "glob", _fake_glob):
            assert subproc.resolve_tool("nix-store") == "nix-store"
            assert subproc.resolve_tool("nix-store") == "nix-store"
        assert glob_calls == ["/nix/store/*/bin/nix-store"]

    def test_import_time_which_snapshot_survives_torn_path(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        # PATH intact at import: the snapshot seeds the cache ...
        with patch.object(
            subproc.shutil, "which",
            return_value="/nix/store/abc-nix-2.18/bin/nix-store",
        ):
            subproc._snapshot_path_tools(("nix-store",))
        # ... so after PATH is torn mid-process the cached path wins
        # without any glob.
        def _no_glob(_pattern):
            raise AssertionError("glob must not run; snapshot cached")

        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(
                    subproc.os.path, "exists", self._nothing_exists,
                ), \
                patch.object(subproc.glob, "glob", _no_glob):
            assert subproc.resolve_tool("nix-store") == (
                "/nix/store/abc-nix-2.18/bin/nix-store"
            )

    def test_snapshot_does_not_cache_misses(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        # Torn env at import time: the snapshot must not poison the
        # cache; a later resolve still reaches the store glob.
        with patch.object(subproc.shutil, "which", return_value=None):
            subproc._snapshot_path_tools(("nix-store",))
        assert "nix-store" not in subproc._TOOL_CACHE
        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(
                    subproc.os.path, "exists", self._nothing_exists,
                ), \
                patch.object(
                    subproc.glob, "glob",
                    return_value=["/nix/store/abc-nix-2.18/bin/nix-store"],
                ):
            assert subproc.resolve_tool("nix-store") == (
                "/nix/store/abc-nix-2.18/bin/nix-store"
            )

    def test_paths_with_slash_pass_through(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        with patch.object(subproc.shutil, "which", return_value=None):
            assert subproc.resolve_tool("/usr/bin/nix") == "/usr/bin/nix"
            assert subproc.resolve_tool("./nix-store") == "./nix-store"

    def test_default_run_subprocess_execs_resolved_argv0(self):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            subproc,
        )
        calls: list[list[str]] = []

        def _fake_run(argv, **_kwargs):
            calls.append(list(argv))

            class _Proc:
                stdout = b"out"
                stderr = b"err"
                returncode = 0

            return _Proc()

        with patch.object(subproc.shutil, "which", return_value=None), \
                patch.object(subproc.os.path, "exists", self._bin_exists), \
                patch.object(subproc.subprocess, "run", _fake_run):
            stdout, stderr, rc = subproc.default_run_subprocess(
                ["nix-store", "--query", "--tree", "/nix/store/x.drv"],
            )
        assert (stdout, stderr, rc) == (b"out", b"err", 0)
        assert calls == [[
            "/bin/nix-store", "--query", "--tree", "/nix/store/x.drv",
        ]]

    def test_import_archive_direct_branch_resolves_nix_store(
        self, tmp_path: pathlib.Path,
    ):
        from unittest.mock import patch  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
            archive as archive_mod,
        )
        archive = tmp_path / "toolchains.drv.archive"
        archive.write_bytes(b"fake")
        calls: list[list[str]] = []

        def _fake_run(argv, **_kwargs):
            calls.append(list(argv))

            class _Proc:
                stdout = b"/nix/store/aaa-x.drv\n"
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
        ), patch.object(archive_mod.subprocess, "run", _fake_run):
            ok, _err, imported = archive_mod.import_archive(archive)
        assert ok is True
        assert imported == ["/nix/store/aaa-x.drv"]
        assert calls == [["/bin/nix-store", "--import"]]


# ---------------------------------------------------------------------------
# _parse_task_id_mappings (CLI helper)
# ---------------------------------------------------------------------------


class TestParseTaskIdMappings:

    def test_happy(self):
        out = dgw._parse_task_id_mappings([
            "abc-gcc15.drv=toolchain_aarch64_gcc15",
            "def-clang19.drv=toolchain_x86_64_clang19",
        ])
        assert out == {
            "abc-gcc15.drv": "toolchain_aarch64_gcc15",
            "def-clang19.drv": "toolchain_x86_64_clang19",
        }

    def test_missing_equals_skipped(self):
        out = dgw._parse_task_id_mappings(["bad-no-eq", "ok=t"])
        assert out == {"ok": "t"}

    def test_empty_ident_or_id_skipped(self):
        out = dgw._parse_task_id_mappings(["=foo", "bar="])
        assert out == {}


# ---------------------------------------------------------------------------
# End-to-end run_dependency_graph_task with stubbed sum-drv + planner
# ---------------------------------------------------------------------------


class TestRunDependencyGraphTask:
    """End-to-end ``run_dependency_graph_task`` exercises with the
    D.1b aggregate-drv entry-point signature.

    Phase 3 no longer rediscovers leaves from the archive import
    stdout: the watcher hands the worker one ``matrix-<binary>``
    aggregate drv per binary plus one ``toolchains`` aggregate drv,
    and the variant_lookup comes from D.1a's
    ``derive_variant_lookup_from_aggregate``. Tests below mock that
    helper to keep the suite pure-Python; the regression test below
    additionally asserts ``_run_nix_instantiate`` is called at most
    once (inside ``make_sum_drv_from_paths``) so future drift back to
    per-leaf eval is caught immediately.
    """

    _TC_AGG = "/nix/store/zzzz-toolchains.drv"

    def _seed_archive(
        self,
        matrix_dir: pathlib.Path,
        binary: str,
    ) -> pathlib.Path:
        """Drop a placeholder ``matrix-<binary>.drv.archive`` in ``matrix_dir``.

        Phase 3 still imports the archive (so the leaf closure
        materialises in the local store for the tree walk), but the
        kept-drv list no longer comes from the import stdout — D.1a's
        ``derive_variant_lookup_from_aggregate`` owns it now.

        Also seeds the shared ``toolchains.drv.archive`` (toolchain
        dedup): the worker imports it FIRST (toolchain-first) and
        fatally raises if it is missing/zero-byte. The eval worker is
        its producer in production; here we drop a non-empty placeholder
        so the toolchain-first import step succeeds.
        """
        self._seed_toolchain_archive(matrix_dir)
        archive = matrix_dir / f"matrix-{binary}.drv.archive"
        archive.write_bytes(b"fake")
        return archive

    @staticmethod
    def _seed_toolchain_archive(matrix_dir: pathlib.Path) -> pathlib.Path:
        """Drop a non-empty placeholder ``toolchains.drv.archive``.

        Idempotent (overwrites): seeding it once per binary in a
        multi-binary test re-writes the identical placeholder, mirroring
        the production race where N eval workers write the same file.
        """
        tc_archive = matrix_dir / "toolchains.drv.archive"
        tc_archive.write_bytes(b"fake-toolchain")
        return tc_archive

    def _matrix_agg(self, binary: str, *, hash_prefix: str = "ma") -> str:
        """Synthetic aggregate drv path for ``binary``."""
        h = (hash_prefix + "a" * 32)[:32]
        return f"/nix/store/{h}-matrix-{binary}.drv"

    def _stub_lookup_for(self, binary: str) -> dict[tuple[str, str], dict]:
        """Build a minimal variant_lookup the planner expects.

        The shape is ``{(arch, label): variant_spec}`` and only the
        ``drv`` / ``arch`` / ``label`` / ``suffix`` keys are inspected
        by downstream code in these tests. The synthetic leaf drv is
        not actually walked because ``build_sum_drv_multi`` is
        monkey-patched.
        """
        arch = "x86_64"
        suffix = "gcc15-O0-baseline-default-san-off-march-default"
        label = f"{binary}__{arch}__{suffix}"
        h = ("vv" + "a" * 32)[:32]
        return {
            (arch, label): {
                "drv": f"/nix/store/{h}-{binary}-{arch}-{suffix}-elf-folder.drv",
                "arch": arch,
                "label": label,
                "suffix": suffix,
            },
        }

    def _patch_derive(
        self,
        monkeypatch,
        lookups: dict[str, dict[tuple[str, str], dict]],
    ) -> None:
        """Install a stub for ``archive.derive_variant_lookup_from_aggregate``
        keyed by aggregate-drv path.

        ``raising=False`` keeps the patch working pre-D.1a (the
        function lands in the sibling task); once D.1a merges this
        becomes a normal monkeypatch.setattr.
        """
        from compiler_suit_runner.workers.dependency_graph_worker import (
            archive as _archive_mod,
        )

        def fake(agg_drv: str, **_kw) -> dict[tuple[str, str], dict]:
            return lookups.get(agg_drv, {})

        monkeypatch.setattr(
            _archive_mod, "derive_variant_lookup_from_aggregate",
            fake, raising=False,
        )

    def test_empty_matrix_dir_writes_empty_graph(
        self, tmp_path: pathlib.Path,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        result = dgw.run_dependency_graph_task(
            task=_FakeStreamTask(),
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/aaaa-bash",
            toolchain_aggregate_drv=self._TC_AGG,
            binary="hello",
            matrix_drv=self._matrix_agg("hello"),
        )
        assert result.binary_count == 0
        assert result.descriptor_count == 0
        # ``output_path`` is the human-readable summary; it carries the
        # descriptor-derived summary fields.
        summary_text = result.output_path.read_text()
        assert "binary_count: 0" in summary_text
        assert "descriptor_count: 0" in summary_text

    def test_empty_toolchain_aggregate_raises(
        self, tmp_path: pathlib.Path,
    ):
        """Phase 3 cannot run without the producer's toolchain
        aggregate drv — an empty string raises ValueError loudly so a
        watcher misconfig surfaces immediately instead of crashing
        deep inside make_sum_drv_from_paths.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        with pytest.raises(ValueError, match="toolchain_aggregate_drv"):
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/aaaa-bash",
                toolchain_aggregate_drv="",
                binary="hello",
                matrix_drv=self._matrix_agg("hello"),
            )

    def test_empty_matrix_aggregate_drv_raises(
        self, tmp_path: pathlib.Path,
    ):
        """Same loud-fail for the matrix side: an empty
        ``matrix_drv`` means the bulk-eval producer emitted nothing
        for this binary in this run.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        with pytest.raises(ValueError, match="matrix_drv"):
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/aaaa-bash",
                toolchain_aggregate_drv=self._TC_AGG,
                binary="hello",
                matrix_drv="",
            )

    def test_empty_binary_raises(
        self, tmp_path: pathlib.Path,
    ):
        """Per-binary dispatch requires the binary name to be non-empty
        so error messages and the matrix-<binary> sum-drv keys remain
        identifiable."""
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        with pytest.raises(ValueError, match="binary"):
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/aaaa-bash",
                toolchain_aggregate_drv=self._TC_AGG,
                binary="",
                matrix_drv=self._matrix_agg("hello"),
            )

    def test_end_to_end_single_binary(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        matrix_agg = self._matrix_agg("hello", hash_prefix="hh")
        hello_lookup = self._stub_lookup_for("hello")
        self._patch_derive(monkeypatch, {matrix_agg: hello_lookup})

        stub = _SubprocessStub()
        stub.tree_stdout = b"/nix/store/sum-drv.drv\n"

        # Monkey-patch the multi-binary sum-drv builder + planner to
        # avoid touching template_graph or /nix/store. Both calls are
        # recorded so we can assert on argument shape.
        sum_drv_calls: list[dict] = []
        plan_calls: list[dict] = []

        def fake_build_sum_drv_multi(*, bash_path, toolchain_drvs,
                                       matrix_drvs, system):
            sum_drv_calls.append({
                "bash_path": bash_path,
                "toolchain_drvs": list(toolchain_drvs),
                "matrix_drvs": {k: list(v) for k, v in matrix_drvs.items()},
                "system": system,
            })
            return "/nix/store/sum-drv-multi"

        def fake_plan_total(*, sum_drv, binaries, variant_lookups,
                             toolchain_task_ids, sys_name):
            plan_calls.append({
                "sum_drv": sum_drv,
                "binaries": list(binaries),
                "variant_lookups": {
                    b: dict(lookup)
                    for b, lookup in variant_lookups.items()
                },
                "toolchain_task_ids": dict(toolchain_task_ids),
                "sys_name": sys_name,
            })
            return [Phase4Descriptor(
                kind="build_variant",
                task_id=f"bv_{b}",
                name=f"build_variant__{b}",
                payload={"binary": b},
                depends_on=(),
            ) for b in binaries]

        monkeypatch.setattr(dgw, "build_sum_drv_multi", fake_build_sum_drv_multi)
        monkeypatch.setattr(dgw, "plan_total", fake_plan_total)

        result = dgw.run_dependency_graph_task(
            task=_FakeStreamTask(),
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/aaaa-bash",
            toolchain_aggregate_drv=self._TC_AGG,
            binary="hello",
            matrix_drv=matrix_agg,
            toolchain_task_ids={
                "zzzz-gcc15.drv": "x86_64-linux__aarch64__gcc15",
            },
            sys_name="x86_64-linux",
            run_subprocess=stub,
        )
        assert result.binary_count == 1
        assert result.descriptor_count == 1
        # sum_drv builder saw length-1 lists for both sides — the
        # post-Phase-A.2 invariant the path-form helper requires.
        assert len(sum_drv_calls) == 1
        assert sum_drv_calls[0]["toolchain_drvs"] == [self._TC_AGG]
        assert sum_drv_calls[0]["matrix_drvs"] == {
            "matrix-hello": [matrix_agg],
        }
        # Planner saw the toolchain task ids and the binary list.
        assert plan_calls[0]["toolchain_task_ids"] == {
            "zzzz-gcc15.drv": "x86_64-linux__aarch64__gcc15",
        }
        assert plan_calls[0]["binaries"] == ["hello"]
        # Variant lookup the planner sees is the stubbed one
        # (proves derive_variant_lookup_from_aggregate is wired in).
        assert plan_calls[0]["variant_lookups"] == {"hello": hello_lookup}
        # nix-store --import was invoked (leaves need to be local for
        # the tree walk inside build_sum_drv_multi / query_drv_tree).
        assert any(c[:2] == ["nix-store", "--import"] for c in stub.calls)
        # The human-readable summary was written.
        assert "descriptor_count: 1" in result.output_path.read_text()

    def test_import_runs_unconditionally(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """The worker still imports every archive — the leaves the
        aggregate drv references must be resident locally before
        ``nix-store --query --tree`` runs. Even when the aggregate
        itself is "locally present" the import still fires (we do not
        probe-and-skip on the closure).
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        matrix_agg = self._matrix_agg("hello", hash_prefix="sk")
        self._patch_derive(
            monkeypatch, {matrix_agg: self._stub_lookup_for("hello")},
        )

        stub = _SubprocessStub()
        stub.path_info_present.add(matrix_agg)
        stub.tree_stdout = b"/nix/store/sum.drv\n"

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: "/nix/store/sum.drv",
        )
        monkeypatch.setattr(
            dgw, "plan_total",
            lambda **kw: [],
        )

        dgw.run_dependency_graph_task(
            task=_FakeStreamTask(),
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            binary="hello",
            matrix_drv=matrix_agg,
            run_subprocess=stub,
        )
        # --import runs twice even though the aggregate is "present":
        # once for the toolchain-first ``toolchains.drv.archive`` and
        # once for the per-binary ``matrix-hello.drv.archive``. The
        # closure must be resident locally for the tree walk; we do not
        # probe-and-skip.
        import_calls = [
            c for c in stub.calls if c[:2] == ["nix-store", "--import"]
        ]
        assert len(import_calls) == 2

    def test_import_failure_raises(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        matrix_agg = self._matrix_agg("hello", hash_prefix="if")
        self._patch_derive(
            monkeypatch, {matrix_agg: self._stub_lookup_for("hello")},
        )

        stub = _SubprocessStub()
        # Toolchain-first import succeeds (rc=0); the per-binary import
        # then fails (rc=1) — proves the per-binary import-failure path
        # still surfaces under toolchain-first import ordering.
        stub.import_rc_queue = [0, 1]
        stub.import_stderr = b"borked"

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: pytest.fail(
                "build_sum_drv_multi should not be reached after import failure"
            ),
        )
        with pytest.raises(dgw.DependencyGraphWorkerError) as excinfo:
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=self._TC_AGG,
                binary="hello",
                matrix_drv=matrix_agg,
                run_subprocess=stub,
            )
        # ``import_archive`` failure surfaces with the archive's stem
        # (one per archive on disk), not the task's ``binary`` kwarg.
        assert excinfo.value.binary == "hello"
        assert excinfo.value.stage == "import"

    def test_missing_toolchain_archive_raises(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """A missing (or zero-byte) shared ``toolchains.drv.archive``
        is fatal at the toolchain-first import step: the per-binary diff
        archives are un-importable without the toolchain closure. The
        error is tagged ``binary='<toolchain>'`` stage
        ``'toolchain_import'`` so a producer/transport regression surfaces
        loudly instead of crashing deep in the per-binary import.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        # Per-binary archive present, but NO toolchains.drv.archive.
        (matrix_dir / "matrix-hello.drv.archive").write_bytes(b"fake")
        matrix_agg = self._matrix_agg("hello", hash_prefix="mt")
        self._patch_derive(
            monkeypatch, {matrix_agg: self._stub_lookup_for("hello")},
        )
        stub = _SubprocessStub()
        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: pytest.fail(
                "build_sum_drv_multi must not be reached without the "
                "toolchain archive"
            ),
        )
        with pytest.raises(dgw.DependencyGraphWorkerError) as excinfo:
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=self._TC_AGG,
                binary="hello",
                matrix_drv=matrix_agg,
                run_subprocess=stub,
            )
        assert excinfo.value.binary == "<toolchain>"
        assert excinfo.value.stage == "toolchain_import"

    def test_toolchain_import_failure_raises(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """A non-zero rc on the toolchain-first ``nix-store --import``
        surfaces as ``DependencyGraphWorkerError(stage='toolchain_import')``
        before any per-binary archive is imported.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        matrix_agg = self._matrix_agg("hello", hash_prefix="tf")
        self._patch_derive(
            monkeypatch, {matrix_agg: self._stub_lookup_for("hello")},
        )
        stub = _SubprocessStub()
        # First (toolchain) import fails.
        stub.import_rc_queue = [1]
        stub.import_stderr = b"toolchain-import-borked"
        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: pytest.fail(
                "build_sum_drv_multi must not be reached after toolchain "
                "import failure"
            ),
        )
        with pytest.raises(dgw.DependencyGraphWorkerError) as excinfo:
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=self._TC_AGG,
                binary="hello",
                matrix_drv=matrix_agg,
                run_subprocess=stub,
            )
        assert excinfo.value.binary == "<toolchain>"
        assert excinfo.value.stage == "toolchain_import"

    def test_query_tree_failure_raises(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        matrix_agg = self._matrix_agg("hello", hash_prefix="qt")
        self._patch_derive(
            monkeypatch, {matrix_agg: self._stub_lookup_for("hello")},
        )
        stub = _SubprocessStub()

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: "/nix/store/sum.drv",
        )

        # Post-byte-stream refactor: ``nix-store --query --tree`` runs
        # inside :func:`stream_drv_tree` via ``subprocess.Popen`` —
        # the injected ``run_subprocess`` stub no longer sees it. Patch
        # :func:`stream_drv_tree` to raise the same ``RuntimeError`` the
        # production path raises on a non-zero exit; run.py must still
        # re-wrap it as ``DependencyGraphWorkerError(stage="query_tree")``.
        from compiler_suit_runner.workers.dependency_graph_worker import (
            sum_drv as _sum_drv_mod,
        )

        def fake_stream_drv_tree(_sum_drv_path: str):
            raise RuntimeError(
                f"nix-store --query --tree {_sum_drv_path} failed "
                f"(rc=1): borked"
            )

        monkeypatch.setattr(
            _sum_drv_mod, "stream_drv_tree", fake_stream_drv_tree,
        )

        with pytest.raises(dgw.DependencyGraphWorkerError) as excinfo:
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=self._TC_AGG,
                binary="hello",
                matrix_drv=matrix_agg,
                run_subprocess=stub,
            )
        assert excinfo.value.stage == "query_tree"

    @pytest.mark.parametrize("binary", ["hello", "world"])
    def test_per_binary_dispatch_runs_one_sum_drv_per_call(
        self, tmp_path: pathlib.Path, monkeypatch, binary: str,
    ):
        """Per-binary dispatch: each call assembles ONE sum-drv (the
        single ``matrix-<binary>`` wrapper, length-1 list) and runs ONE
        planner pass with the one-binary slice. Cross-binary template
        dedup is no longer this worker's responsibility — the framework
        runs one dependency_graph task per binary, and the streaming
        planner inside ``plan_total`` sees just that binary's tree.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, binary)
        agg = self._matrix_agg(binary, hash_prefix=binary[:2])
        self._patch_derive(monkeypatch, {agg: self._stub_lookup_for(binary)})

        stub = _SubprocessStub()
        stub.tree_stdout = b"/nix/store/sum-drv.drv\n"

        sum_drv_calls: list[dict] = []
        plan_calls: list[dict] = []

        def fake_build_sum_drv_multi(*, bash_path, toolchain_drvs,
                                       matrix_drvs, system):
            sum_drv_calls.append({
                "toolchain_drvs": list(toolchain_drvs),
                "matrix_drvs": {k: list(v) for k, v in matrix_drvs.items()},
            })
            return "/nix/store/sum-drv-multi"

        def fake_plan_total(*, sum_drv, binaries, variant_lookups,
                             toolchain_task_ids, sys_name):
            plan_calls.append({"binaries": list(binaries)})
            return [Phase4Descriptor(
                kind="build_variant", task_id=f"bv_{b}",
                name=f"build_variant__{b}",
                payload={"binary": b}, depends_on=(),
            ) for b in binaries]

        monkeypatch.setattr(dgw, "build_sum_drv_multi", fake_build_sum_drv_multi)
        monkeypatch.setattr(dgw, "plan_total", fake_plan_total)

        result = dgw.run_dependency_graph_task(
            task=_FakeStreamTask(),
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            binary=binary,
            matrix_drv=agg,
            run_subprocess=stub,
        )
        assert result.binary_count == 1
        assert result.descriptor_count == 1
        # Exactly ONE sum-drv build + ONE plan_total call per binary.
        assert len(sum_drv_calls) == 1
        assert len(plan_calls) == 1
        # Length-1 lists for both sides (post-Phase-A.2 invariant); the
        # single ``matrix-<binary>`` wrapper carries this binary's drv.
        assert sum_drv_calls[0]["toolchain_drvs"] == [self._TC_AGG]
        assert sum_drv_calls[0]["matrix_drvs"] == {
            f"matrix-{binary}": [agg],
        }
        # The single binary lands in plan_total's binaries list.
        assert plan_calls[0]["binaries"] == [binary]
        # The injected ``run_subprocess`` stub no longer sees the
        # ``nix-store --query --tree`` invocation — the planner pulls
        # that stream via :func:`stream_drv_tree` (a direct
        # ``subprocess.Popen``) instead. With ``plan_total`` patched out
        # here the planner never runs, so the stub records zero
        # tree-query calls.
        tree_calls = [
            c for c in stub.calls if c[:3] == ["nix-store", "--query", "--tree"]
        ]
        assert tree_calls == []

    def test_binary_with_empty_lookup_is_skipped(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """A binary whose aggregate drv yields an empty variant_lookup
        (D.1a returns ``{}``) is dropped before the sum-drv assembly —
        the ``make_sum_drv_from_paths`` contract forbids a zero-variant
        matrix wrapper. Under per-binary dispatch the worker returns a
        zero-binary, zero-descriptor result without invoking the
        sum-drv builder at all (the empty-lookup short-circuit path in
        :func:`run_dependency_graph_task`).
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        agg_hello = self._matrix_agg("hello", hash_prefix="eh")
        # hello → empty lookup → skipped.
        self._patch_derive(monkeypatch, {agg_hello: {}})

        stub = _SubprocessStub()
        stub.tree_stdout = b"/nix/store/sum.drv\n"

        sum_drv_calls: list[dict] = []

        def fake_build_sum_drv_multi(*, bash_path, toolchain_drvs,
                                       matrix_drvs, system):
            sum_drv_calls.append({
                "matrix_drvs": {k: list(v) for k, v in matrix_drvs.items()},
            })
            return "/nix/store/sum.drv"

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi", fake_build_sum_drv_multi,
        )
        monkeypatch.setattr(dgw, "plan_total", lambda **kw: [])

        result = dgw.run_dependency_graph_task(
            task=_FakeStreamTask(),
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            binary="hello",
            matrix_drv=agg_hello,
            run_subprocess=stub,
        )
        # Zero plannable binaries -> empty result.
        assert result.binary_count == 0
        assert result.descriptor_count == 0
        # The sum-drv builder never ran: a zero-variant wrapper cannot
        # be passed to the path-form helper.
        assert sum_drv_calls == []
        assert "descriptor_count: 0" in result.output_path.read_text()

    def test_no_nix_instantiate_outside_make_sum_drv(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """Regression guard: phase 3 must NOT call ``nix-instantiate``
        outside the single invocation inside ``make_sum_drv_from_paths``.

        We let the real :func:`build_sum_drv_multi` run, but stub
        :func:`template_graph.make_sum_drv._run_nix_instantiate` with a
        counter so we can assert it fires AT MOST ONCE across the
        whole phase. Any drift back to per-leaf eval (e.g. resurrecting
        ``nix-eval-jobs`` calls or a presence probe) trips this check.
        """
        from template_graph import make_sum_drv as _msd
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        agg_hello = self._matrix_agg("hello", hash_prefix="ni")
        self._patch_derive(
            monkeypatch, {agg_hello: self._stub_lookup_for("hello")},
        )

        nix_eval_calls: list[tuple] = []

        def fake_run_nix_instantiate(expr, *args, **kwargs):
            nix_eval_calls.append((expr,) + args)
            return "/nix/store/fake-sum.drv"

        monkeypatch.setattr(
            _msd, "_run_nix_instantiate", fake_run_nix_instantiate,
        )

        stub = _SubprocessStub()
        stub.tree_stdout = b"/nix/store/fake-sum.drv\n"

        # plan_total is patched to a no-op so the test stays
        # planner-independent — the regression is about
        # nix-instantiate invocations, not descriptor shape.
        monkeypatch.setattr(dgw, "plan_total", lambda **kw: [])

        dgw.run_dependency_graph_task(
            task=_FakeStreamTask(),
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            binary="hello",
            matrix_drv=agg_hello,
            run_subprocess=stub,
        )
        assert len(nix_eval_calls) <= 1, (
            f"phase 3 must invoke nix-instantiate AT MOST ONCE (the sum-drv "
            f"assembly inside make_sum_drv_from_paths); got "
            f"{len(nix_eval_calls)} calls — drift back to per-leaf eval"
        )

    def test_all_binaries_single_sum_drv_single_plan(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """Single all-binaries dispatch: passing ``matrix_drvs`` (a
        {binary: drv} mapping) assembles ONE sum-drv wrapping every
        binary's matrix aggregate and runs ONE plan_total pass over all
        of them — one descriptor list covering the whole run.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        self._seed_archive(matrix_dir, "busybox")
        agg_hello = self._matrix_agg("hello", hash_prefix="he")
        agg_bb = self._matrix_agg("busybox", hash_prefix="bb")
        self._patch_derive(monkeypatch, {
            agg_hello: self._stub_lookup_for("hello"),
            agg_bb: self._stub_lookup_for("busybox"),
        })

        stub = _SubprocessStub()
        stub.tree_stdout = b"/nix/store/sum-drv.drv\n"

        sum_drv_calls: list[dict] = []
        plan_calls: list[dict] = []

        def fake_build_sum_drv_multi(*, bash_path, toolchain_drvs,
                                      matrix_drvs, system):
            sum_drv_calls.append({
                "toolchain_drvs": list(toolchain_drvs),
                "matrix_drvs": {k: list(v) for k, v in matrix_drvs.items()},
            })
            return "/nix/store/sum-drv-multi"

        def fake_plan_total(*, sum_drv, binaries, variant_lookups,
                            toolchain_task_ids, sys_name):
            plan_calls.append({"binaries": list(binaries)})
            return [Phase4Descriptor(
                kind="build_variant", task_id=f"bv_{b}",
                name=f"build_variant__{b}",
                payload={"binary": b}, depends_on=(),
            ) for b in binaries]

        monkeypatch.setattr(dgw, "build_sum_drv_multi", fake_build_sum_drv_multi)
        monkeypatch.setattr(dgw, "plan_total", fake_plan_total)

        result = dgw.run_dependency_graph_task(
            task=_FakeStreamTask(),
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            matrix_drvs={"hello": agg_hello, "busybox": agg_bb},
            run_subprocess=stub,
        )
        assert result.binary_count == 2
        assert result.descriptor_count == 2
        # ONE sum-drv build + ONE plan_total call spanning both binaries.
        assert len(sum_drv_calls) == 1
        assert len(plan_calls) == 1
        assert sum_drv_calls[0]["toolchain_drvs"] == [self._TC_AGG]
        assert sum_drv_calls[0]["matrix_drvs"] == {
            "matrix-busybox": [agg_bb],
            "matrix-hello": [agg_hello],
        }
        # Both binaries land in the single plan_total pass (sorted).
        assert plan_calls[0]["binaries"] == ["busybox", "hello"]

    def test_matrix_drvs_and_single_binary_are_mutually_exclusive(
        self, tmp_path: pathlib.Path,
    ):
        """Passing both ``matrix_drvs`` and the single-binary pair is a
        usage error (loud ValueError, not silent precedence)."""
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        with pytest.raises(ValueError, match="either matrix_drvs OR"):
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=self._TC_AGG,
                matrix_drvs={"hello": self._matrix_agg("hello")},
                binary="hello",
                matrix_drv=self._matrix_agg("hello"),
            )

    def test_empty_matrix_drvs_raises(self, tmp_path: pathlib.Path):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        with pytest.raises(ValueError, match="matrix_drvs is empty"):
            dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=self._TC_AGG,
                matrix_drvs={},
            )


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------


class TestCliParser:

    def test_required_args(self):
        parser = dgw._build_cli_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])  # missing required

    def test_full_invocation_parses(self):
        parser = dgw._build_cli_parser()
        args = parser.parse_args([
            "--matrix-eval-out-dir", "/tmp/me",
            "--bash-path", "/nix/store/aaaa-bash",
            "--toolchain-drv", "/nix/store/aaaa-toolchains.drv",
            "--binary", "hello",
            "--matrix-aggregate", "/nix/store/bbbb-matrix-hello.drv",
            "--toolchain-task-id", "tc1.drv=task1",
            "--sys-name", "aarch64-linux",
        ])
        assert args.matrix_eval_out_dir == "/tmp/me"
        # New single-value semantics; dest is ``toolchain_aggregate_drv``
        # to match the run_dependency_graph_task kwarg name.
        assert args.toolchain_aggregate_drv == (
            "/nix/store/aaaa-toolchains.drv"
        )
        # ``--matrix-aggregate`` lands on ``args.matrix_drv`` (the
        # ``dest`` aligned with ``run_dependency_graph_task``).
        assert args.binary == "hello"
        assert args.matrix_drv == "/nix/store/bbbb-matrix-hello.drv"
        assert args.toolchain_task_id == ["tc1.drv=task1"]
        assert args.sys_name == "aarch64-linux"

    def test_system_flag_default(self):
        """``--system`` defaults to ``x86_64-linux`` so the watcher's
        invocation works without an explicit value on the common case."""
        parser = dgw._build_cli_parser()
        args = parser.parse_args([
            "--matrix-eval-out-dir", "/tmp/me",
            "--bash-path", "/nix/store/aaaa-bash",
            "--toolchain-drv", "/nix/store/aaaa-toolchains.drv",
            "--binary", "hello",
            "--matrix-aggregate", "/nix/store/bbbb-matrix-hello.drv",
        ])
        assert args.sys_name == "x86_64-linux"

    def test_system_flag_overrides_default(self):
        """``--system aarch64-linux`` parses onto ``args.sys_name`` so
        the worker's ``run_dependency_graph_task`` sees the override."""
        parser = dgw._build_cli_parser()
        args = parser.parse_args([
            "--matrix-eval-out-dir", "/tmp/me",
            "--bash-path", "/nix/store/aaaa-bash",
            "--toolchain-drv", "/nix/store/aaaa-toolchains.drv",
            "--binary", "hello",
            "--matrix-aggregate", "/nix/store/bbbb-matrix-hello.drv",
            "--system", "aarch64-linux",
        ])
        assert args.sys_name == "aarch64-linux"

    def test_cli_parses_toolchain_drv_and_matrix_aggregate(self):
        """The watcher passes the pre-built aggregate drv paths via two
        single-value flags: ``--toolchain-drv <path>`` and
        ``--matrix-aggregate <path>`` (one binary per worker invocation).
        """
        parser = dgw._build_cli_parser()
        args = parser.parse_args([
            "--matrix-eval-out-dir", "/tmp/me",
            "--bash-path", "/nix/store/aaaa-bash",
            "--toolchain-drv", "/nix/store/x-t.drv",
            "--binary", "hello",
            "--matrix-aggregate", "/nix/store/y-mh.drv",
            "--system", "x86_64-linux",
        ])
        assert args.toolchain_aggregate_drv == "/nix/store/x-t.drv"
        assert args.binary == "hello"
        assert args.matrix_drv == "/nix/store/y-mh.drv"

    def test_cli_requires_matrix_aggregate_and_toolchain_drv(self):
        """Both single-value flags are required; omitting either is a
        hard SystemExit (argparse ``required=True``)."""
        parser = dgw._build_cli_parser()
        # Missing --toolchain-drv → argparse SystemExit at parse time.
        with pytest.raises(SystemExit):
            parser.parse_args([
                "--matrix-eval-out-dir", "/tmp/me",
                "--bash-path", "/nix/store/aaaa-bash",
                "--binary", "hello",
                "--matrix-aggregate", "/nix/store/y.drv",
            ])
        # Missing --matrix-aggregate → argparse SystemExit at parse time.
        with pytest.raises(SystemExit):
            parser.parse_args([
                "--matrix-eval-out-dir", "/tmp/me",
                "--bash-path", "/nix/store/aaaa-bash",
                "--toolchain-drv", "/nix/store/x-t.drv",
                "--binary", "hello",
            ])
        # Missing --binary → argparse SystemExit at parse time.
        with pytest.raises(SystemExit):
            parser.parse_args([
                "--matrix-eval-out-dir", "/tmp/me",
                "--bash-path", "/nix/store/aaaa-bash",
                "--toolchain-drv", "/nix/store/x-t.drv",
                "--matrix-aggregate", "/nix/store/y.drv",
            ])


# ---------------------------------------------------------------------------
# Phase 6.1 lax-mode default smoke test
# ---------------------------------------------------------------------------


class TestPlanTotalLaxDefault:
    """The worker's ``plan_total`` is the integration point that drives
    the streaming planner. Phase 6.1 settled on ``lax=True`` as the
    production default: shape inconsistencies are recorded in
    ``streaming_result["violations"]`` rather than raised. The trade is
    intentional — worst case the planner emits redundant rebuilds for
    affected templates, but the run does NOT crash on a single
    calibration mismatch.
    """

    def test_calibration_mismatch_does_not_raise_under_lax_default(
        self, monkeypatch,
    ):
        """A calibration-shape-mismatch tree (same-name children with
        different counts across two variants) raises ``TreeWalkError``
        under ``lax=False`` but is absorbed under ``lax=True``. We
        invoke ``plan_total`` with the production default and assert
        that:

        1. the call returns normally (would have raised in strict mode);
        2. the streaming_result captured inside the worker carries at
           least one recorded violation.

        ``plan_phase4_from_graph`` is monkeypatched to a capture-stub
        that records the streaming_result it received so the test can
        inspect ``violations`` without depending on the descriptor
        adapter doing useful work on the synthetic tree.
        """
        from template_graph.tests.test_streaming.fixtures import (
            Node, make_hash, render_tree, simple_variant,
        )

        def variant(seed: int, opt: str, n_kids: int) -> Node:
            kids = [
                Node(
                    hash=make_hash(seed + 100 + i),
                    name=f"lib-thing-{1 + i}.0.drv",
                )
                for i in range(n_kids)
            ]
            return simple_variant(
                "hello", "x86_64", seed_base=seed,
                comp="gcc15", opt=opt, children=kids,
            )

        root = Node(
            hash=make_hash(0), name="sum-root.drv",
            children=[
                Node(hash=make_hash(1), name="toolchains.drv"),
                Node(
                    hash=make_hash(2), name="matrix-hello.drv",
                    children=[
                        variant(10, "O0", n_kids=2),
                        variant(20, "O1", n_kids=3),
                    ],
                ),
            ],
        )
        tree_text = render_tree(root)

        captured: list[dict] = []

        from compiler_suit_runner import dependency_graph_planner as _dgp

        def fake_plan_phase4_from_graph(inputs, *, sys_name):
            for inp in inputs:
                captured.append(inp.streaming_result)
            return []

        monkeypatch.setattr(
            _dgp, "plan_phase4_from_graph", fake_plan_phase4_from_graph,
        )

        # The planner now pulls a tuple stream via stream_drv_tree; the
        # test injects an in-memory equivalent derived from the synthetic
        # tree_text so we never spawn ``nix-store``.
        import io  # noqa: PLC0415
        from template_graph.tree_walker import (  # noqa: PLC0415
            drv_tree_stream,
        )
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: E501, PLC0415
            sum_drv as _sum_drv_mod,
        )

        def fake_stream_drv_tree(_sum_drv_path: str):
            return drv_tree_stream(io.BytesIO(tree_text.encode("utf-8")))

        monkeypatch.setattr(
            _sum_drv_mod, "stream_drv_tree", fake_stream_drv_tree,
        )

        # plan_total imports the planner adapter at call time so the
        # monkeypatch above must hit the module attribute; the import
        # inside plan_total re-uses the patched module.
        from compiler_suit_runner.workers.dependency_graph_worker.plan import (  # noqa: E501
            plan_total,
        )

        # No exception — calibration mismatch is recorded, not raised.
        result = plan_total(
            sum_drv="/nix/store/dummy-sum.drv",
            binaries=["hello"],
            variant_lookups={"hello": {}},
            toolchain_task_ids={},
            sys_name="x86_64-linux",
        )
        assert result == []
        assert captured, "plan_phase4_from_graph received no inputs"

        # The streaming planner ran in lax mode → at least one violation
        # was recorded across the per-binary slices. Violations live on
        # the ORIGINAL streaming_result; the per-binary slices in
        # _slice_streaming_result do not currently propagate them, so
        # we inspect the planner output directly via a second pass.
        from template_graph.streaming import plan_from_tree_streaming
        direct = plan_from_tree_streaming(tree_text, lax=True)
        violations = direct.get("violations", [])
        assert len(violations) > 0, (
            f"expected at least one shape violation under lax=True; "
            f"got violations={violations!r}"
        )
        kinds = {v.get("kind") for v in violations}
        assert "calibration-same-name-count-mismatch" in kinds, (
            f"expected calibration-same-name-count-mismatch kind; "
            f"got kinds={kinds}"
        )

    def test_strict_mode_still_raises_when_requested(self, monkeypatch):
        """The lax default is overridable: passing ``lax=False`` to
        ``plan_total`` restores strict-mode behavior and the same
        calibration-mismatch tree raises.
        """
        from template_graph.tests.test_streaming.fixtures import (
            Node, make_hash, render_tree, simple_variant,
        )
        from template_graph.tree_walker import TreeWalkError

        def variant(seed: int, opt: str, n_kids: int) -> Node:
            kids = [
                Node(
                    hash=make_hash(seed + 100 + i),
                    name=f"lib-thing-{1 + i}.0.drv",
                )
                for i in range(n_kids)
            ]
            return simple_variant(
                "hello", "x86_64", seed_base=seed,
                comp="gcc15", opt=opt, children=kids,
            )

        root = Node(
            hash=make_hash(0), name="sum-root.drv",
            children=[
                Node(hash=make_hash(1), name="toolchains.drv"),
                Node(
                    hash=make_hash(2), name="matrix-hello.drv",
                    children=[
                        variant(10, "O0", n_kids=2),
                        variant(20, "O1", n_kids=3),
                    ],
                ),
            ],
        )
        tree_text = render_tree(root)

        import io  # noqa: PLC0415
        from template_graph.tree_walker import (  # noqa: PLC0415
            drv_tree_stream,
        )
        from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: E501, PLC0415
            sum_drv as _sum_drv_mod,
        )

        def fake_stream_drv_tree(_sum_drv_path: str):
            return drv_tree_stream(io.BytesIO(tree_text.encode("utf-8")))

        monkeypatch.setattr(
            _sum_drv_mod, "stream_drv_tree", fake_stream_drv_tree,
        )

        from compiler_suit_runner.workers.dependency_graph_worker.plan import (  # noqa: E501
            plan_total,
        )

        with pytest.raises(
            TreeWalkError,
            match=r"calibration pair same-name child count mismatch",
        ):
            plan_total(
                sum_drv="/nix/store/dummy-sum.drv",
                binaries=["hello"],
                variant_lookups={"hello": {}},
                toolchain_task_ids={},
                sys_name="x86_64-linux",
                lax=False,
            )
# Phase 6.1b: per-category counters + summary log
# ---------------------------------------------------------------------------


class TestDependencyGraphCounters:
    """Smoke tests for the counter aggregation + summary log surface.

    Counters are derived from the streaming-planner result dict +
    descriptor list; the helper :func:`compute_dependency_graph_counters`
    is exported so the worker (and tests) can call it without re-running
    the planner. The fixture below fabricates a 2-binary streaming
    result + descriptor list that exercises every counter field.
    """

    def _two_binary_streaming_result(self) -> dict:
        """Streaming-result-shaped dict with non-zero counter inputs
        for every category the summary log surfaces.
        """
        # Two templates (one per binary) so ``templates=2``.
        return {
            "templates": ["t0", "t1"],
            "variant_arrays": {},
            "common_deps_per_arch_template": {},
            "toolchain_drvs": set(),
            "arch_indep_deps": {"hello": set(), "world": set()},
            "stdenv_subtrees": {("h0", "stdenv-1.drv"): {}},
            # Two meta-templates for ``hello``, one for ``world``;
            # summing across binaries should give 3.
            "meta_templates": {
                "hello": ["mt0", "mt1"], "world": ["mt2"],
            },
            "source_terminal_skipped": 5,
            "violations": [
                {"kind": "test", "matrix": "hello"},
                {"kind": "test", "matrix": "world"},
            ],
        }

    def _two_binary_descriptors(self) -> list[Phase4Descriptor]:
        """Descriptor list covering each common-dep category + a few
        ``build_variant`` records (one wired to a toolchain task)."""
        return [
            Phase4Descriptor(
                kind="build_common_dep",
                task_id="build_common_dep__cross_arch__hh-shared.drv",
                name="build_common_dep__hello__cross_arch__shared.drv",
                payload={"binary": "hello", "arch": "cross_arch"},
                depends_on=(),
            ),
            Phase4Descriptor(
                kind="build_common_dep",
                task_id="build_common_dep__family__x86__zz-lib.drv",
                name="build_common_dep__hello__family__x86__lib.drv",
                payload={"binary": "hello", "arch": "family__x86"},
                depends_on=(),
            ),
            Phase4Descriptor(
                kind="build_common_dep",
                task_id="build_common_dep__arch_indep__world__aa-src.tar",
                name="build_common_dep__arch_indep__world__src.tar",
                payload={"binary": "world", "arch": "arch_indep"},
                depends_on=(),
            ),
            Phase4Descriptor(
                kind="build_common_dep",
                task_id="build_common_dep__bb-glibc.drv",
                name="build_common_dep__hello__x86_64__glibc.drv",
                payload={"binary": "hello", "arch": "x86_64"},
                depends_on=(),
            ),
            Phase4Descriptor(
                kind="build_variant",
                task_id="build_variant__x86_64-linux__hello__gcc15-O2",
                name="build_variant__hello__gcc15-O2",
                payload={"binary": "hello"},
                build_compilers_depends_on=("x86_64-linux__x86_64__gcc15",),
            ),
            Phase4Descriptor(
                kind="build_variant",
                task_id="build_variant__x86_64-linux__world__gcc15-O0",
                name="build_variant__world__gcc15-O0",
                payload={"binary": "world"},
                depends_on=(),
            ),
        ]

    def test_compute_counters_populated_for_two_binaries(self):
        from compiler_suit_runner.workers.dependency_graph_worker import (
            compute_dependency_graph_counters,
        )
        counters = compute_dependency_graph_counters(
            streaming_result=self._two_binary_streaming_result(),
            descriptors=self._two_binary_descriptors(),
            binaries=["hello", "world"],
        )
        assert counters["templates"] == 2
        assert counters["meta_templates"] == 3
        assert counters["variants"] == 2
        assert counters["common_deps_cross_arch"] == 1
        assert counters["common_deps_family"] == 1
        assert counters["common_deps_uni_arch"] == 1
        assert counters["common_deps_arch_indep"] == 1
        assert counters["source_terminal_skipped"] == 5
        # Only one of the two build_variant descriptors carries a
        # toolchain task in its build_compilers_depends_on.
        assert counters["toolchain_wired"] == 1
        assert counters["stdenv_subtrees"] == 1
        assert counters["violations"] == 2

    def test_compute_counters_empty_inputs(self):
        from compiler_suit_runner.workers.dependency_graph_worker import (
            compute_dependency_graph_counters,
        )
        counters = compute_dependency_graph_counters(
            streaming_result={},
            descriptors=[],
            binaries=[],
        )
        # Every field defaults to 0 so callers that hand it a stub
        # streaming dict (e.g. monkeypatched plan_total paths) still
        # get a stable shape.
        assert counters == {
            "templates": 0,
            "meta_templates": 0,
            "variants": 0,
            "common_deps_cross_arch": 0,
            "common_deps_family": 0,
            "common_deps_uni_arch": 0,
            "common_deps_arch_indep": 0,
            "source_terminal_skipped": 0,
            "toolchain_wired": 0,
            "stdenv_subtrees": 0,
            "violations": 0,
        }

    def test_result_carries_counter_fields(
        self, tmp_path: pathlib.Path, monkeypatch, caplog,
    ):
        """End-to-end smoke: the worker's ``DependencyGraphResult``
        carries the counter fields populated from the monkeypatched
        planner output, and the summary log line fires. The patched
        ``plan_total`` returns the full 2-binary descriptor fixture so
        the counter aggregation surfaces every category, even though
        this worker run only plans one binary."""
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        (matrix_dir / "matrix-hello.drv.archive").write_bytes(b"x")
        # Toolchain-first import requires a non-empty toolchains.drv.archive
        # (the eval worker produces it in production).
        (matrix_dir / "toolchains.drv.archive").write_bytes(b"x")
        agg_hello = "/nix/store/" + "rh" + "a" * 30 + "-matrix-hello.drv"
        tc_agg = "/nix/store/zzzz-toolchains.drv"

        # Stub D.1a's derive_variant_lookup_from_aggregate so the
        # worker sees a non-empty lookup (otherwise the binary would
        # be skipped before planning).
        from compiler_suit_runner.workers.dependency_graph_worker import (
            archive as _archive_mod,
        )
        lookups = {
            agg_hello: {
                ("x86_64", "hello__x86_64__stub"): {
                    "drv": "/nix/store/dummy-hello.drv",
                    "arch": "x86_64", "label": "hello__x86_64__stub",
                    "suffix": "stub",
                },
            },
        }
        monkeypatch.setattr(
            _archive_mod, "derive_variant_lookup_from_aggregate",
            lambda agg, **_kw: lookups.get(agg, {}), raising=False,
        )

        stub = _SubprocessStub()
        stub.tree_stdout = b"/nix/store/sum.drv\n"

        descriptors = self._two_binary_descriptors()

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi", lambda **kw: "/nix/store/sum.drv",
        )
        monkeypatch.setattr(
            dgw, "plan_total",
            lambda **kw: descriptors,
        )

        import logging  # noqa: PLC0415
        with caplog.at_level(
            logging.INFO,
            logger="compiler_suit_runner.dependency_graph_worker",
        ):
            result = dgw.run_dependency_graph_task(
                task=_FakeStreamTask(),
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=tc_agg,
                binary="hello",
                matrix_drv=agg_hello,
                run_subprocess=stub,
            )

        # Counter fields exist + default to descriptor-derived values
        # for the monkeypatched plan_total path (streaming-result-only
        # counters degrade to 0 because the patch hid the planner).
        # Per-binary dispatch: ``binary_count`` is 1 for this run.
        assert result.binary_count == 1
        assert result.descriptor_count == len(descriptors)
        assert result.variants == 2
        assert result.toolchain_wired == 1
        assert result.common_deps_cross_arch == 1
        assert result.common_deps_family == 1
        assert result.common_deps_uni_arch == 1
        assert result.common_deps_arch_indep == 1
        # Streaming-result-derived counters are 0 on the monkeypatched
        # plan_total path -- the patch shadows the planner so the
        # worker can only count descriptor categories.
        assert result.templates == 0
        assert result.meta_templates == 0
        assert result.stdenv_subtrees == 0
        assert result.source_terminal_skipped == 0
        assert result.violations == 0
        # Summary log line fired at INFO level.
        summary_records = [
            r for r in caplog.records
            if r.message.startswith("dependency_graph: binaries=")
        ]
        assert len(summary_records) == 1
        assert summary_records[0].levelno == logging.INFO

    def test_violations_dump_fires_at_warn(self, caplog):
        """The WARN-level violation dump fires on non-empty entries."""
        import logging  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker import (
            summary as _summary_mod,
        )
        with caplog.at_level(
            logging.WARNING,
            logger="compiler_suit_runner.dependency_graph_worker",
        ):
            _summary_mod.emit_violations_log([
                {"kind": "shape", "matrix": "hello", "node": "x"},
                {"kind": "shape", "matrix": "world", "node": "y"},
            ])
        warn_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "violation" in r.message
        ]
        assert len(warn_records) == 1, [r.message for r in caplog.records]


# ---------------------------------------------------------------------------
# stream_drv_tree
# ---------------------------------------------------------------------------


class _FakePopen:
    """Minimal ``subprocess.Popen`` stand-in for ``stream_drv_tree``.

    Holds a ``BytesIO`` stdout / ``BytesIO`` stderr and a configurable
    ``returncode``; ``wait()`` returns it. The constructor records the
    argv it was called with so the test can assert the argv shape.
    """

    last_argv: Optional[list[str]] = None

    def __init__(
        self,
        stdout_bytes: bytes,
        stderr_bytes: bytes = b"",
        returncode: int = 0,
    ) -> None:
        import io  # noqa: PLC0415
        self.stdout = io.BytesIO(stdout_bytes)
        self.stderr = io.BytesIO(stderr_bytes)
        self._rc = returncode

    def wait(self) -> int:
        return self._rc


def _make_popen_factory(
    stdout_bytes: bytes,
    stderr_bytes: bytes = b"",
    returncode: int = 0,
):
    """Build a ``subprocess.Popen``-compatible factory that records argv."""
    calls: list[list[str]] = []

    def _factory(argv, **_kwargs):
        calls.append(list(argv))
        return _FakePopen(stdout_bytes, stderr_bytes, returncode)

    return _factory, calls


class TestStreamDrvTree:

    _HASH = b"abc123def456ghi789jkl012mno345pq"  # 32 chars

    def _corpus_bytes(self) -> bytes:
        """3-line tree: root + child + backref leaf (mirrors the
        template_graph drv_tree_stream minimal corpus)."""
        lines = [
            b"/nix/store/" + self._HASH + b"-sum-root.drv",
            b"\xe2\x94\x9c\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80"
            b"/nix/store/" + self._HASH + b"-toolchains.drv",
            b"\xe2\x94\x94\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80"
            b"/nix/store/" + self._HASH + b"-matrix-hello.drv [...]",
        ]
        return b"\n".join(lines) + b"\n"

    def test_yields_tuples_matching_drv_tree_stream(self):
        from unittest.mock import patch  # noqa: PLC0415
        import io  # noqa: PLC0415
        from template_graph.tree_walker import drv_tree_stream  # noqa: PLC0415

        corpus = self._corpus_bytes()
        expected = list(drv_tree_stream(io.BytesIO(corpus)))
        # Sanity: corpus shape is what the docstring promises.
        assert len(expected) == 3
        assert expected[-1][3] is True  # backref leaf
        factory, calls = _make_popen_factory(corpus)
        with patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".sum_drv.subprocess.Popen",
            side_effect=factory,
        ), patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".subproc.shutil.which",
            return_value=None,
        ), patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".subproc.os.path.exists",
            lambda path: str(path).startswith("/bin/"),
        ):
            got = list(dgw.stream_drv_tree("/nix/store/zzzz-sum.drv"))
        assert got == expected
        # argv[0] is resolved (torn-PATH fallback → /bin/nix-store).
        assert calls == [[
            "/bin/nix-store", "--query", "--tree",
            "/nix/store/zzzz-sum.drv",
        ]]

    def test_nonzero_exit_raises_runtime_error_with_stderr(self):
        from unittest.mock import patch  # noqa: PLC0415

        factory, _calls = _make_popen_factory(
            stdout_bytes=b"",
            stderr_bytes=b"no such derivation\n",
            returncode=1,
        )
        with patch(
            "compiler_suit_runner.workers.dependency_graph_worker"
            ".sum_drv.subprocess.Popen",
            side_effect=factory,
        ):
            with pytest.raises(RuntimeError, match="no such derivation"):
                list(dgw.stream_drv_tree("/nix/store/zzzz-sum.drv"))


# ---------------------------------------------------------------------------
# Streamed-spawn handoff (the Wave-1 send_message transport)
# ---------------------------------------------------------------------------


class TestStreamedSpawnHandoff:
    """``run_dependency_graph_task`` streams its planned descriptors to
    the primary as :mod:`compiler_suit_runner.streamed_spawn` batch
    messages on ``task.send_message``: planner-order batches on
    SPAWN_TOPIC, then exactly one summary on SUMMARY_TOPIC carrying the
    authoritative total + per-kind counters.

    Setup mirrors :class:`TestRunDependencyGraphTask` (stubbed archive
    derive + sum-drv builder + planner so no nix / template_graph is
    touched); assertions decode the captured wire bytes.
    """

    _TC_AGG = "/nix/store/zzzz-toolchains.drv"
    _MATRIX_AGG = "/nix/store/" + "ss" + "a" * 30 + "-matrix-hello.drv"

    def _seed(self, tmp_path: pathlib.Path) -> pathlib.Path:
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        (matrix_dir / "toolchains.drv.archive").write_bytes(b"fake-toolchain")
        (matrix_dir / "matrix-hello.drv.archive").write_bytes(b"fake")
        return matrix_dir

    def _patch_pipeline(self, monkeypatch, descriptors) -> None:
        """Stub derive/sum-drv/planner so the worker plans exactly
        ``descriptors``."""
        from compiler_suit_runner.workers.dependency_graph_worker import (
            archive as _archive_mod,
        )

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
        monkeypatch.setattr(dgw, "plan_total", lambda **kw: list(descriptors))

    def _run(self, matrix_dir: pathlib.Path, task) -> None:
        dgw.run_dependency_graph_task(
            task=task,
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            binary="hello",
            matrix_drv=self._MATRIX_AGG,
            run_subprocess=_SubprocessStub(),
        )

    @staticmethod
    def _descriptors(n: int) -> list[Phase4Descriptor]:
        """``n`` descriptors over both kinds (kind alternates so the
        summary's per-kind Counter has something to count)."""
        out: list[Phase4Descriptor] = []
        for i in range(n):
            kind = "build_common_dep" if i % 2 else "build_variant"
            out.append(Phase4Descriptor(
                kind=kind,
                task_id=f"{kind}__{i}",
                name=f"{kind}__{i}",
                payload={"i": i},
                depends_on=(),
            ))
        return out

    def test_batches_in_planner_order_then_one_summary(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        from compiler_suit_runner.streamed_spawn import (  # noqa: PLC0415
            SPAWN_TOPIC,
            SUMMARY_TOPIC,
            decode_spawn_message,
        )

        matrix_dir = self._seed(tmp_path)
        planned = self._descriptors(5)
        self._patch_pipeline(monkeypatch, planned)
        task = _FakeStreamTask()
        self._run(matrix_dir, task)

        assert [t for t, _ in task.messages] == [SPAWN_TOPIC, SUMMARY_TOPIC]
        batch = decode_spawn_message(task.messages[0][1])
        assert batch["kind"] == "spawn_batch"
        assert batch["seq"] == 0
        # Planner order, full payload fidelity (dataclass equality).
        assert batch["descriptors"] == planned
        summary = decode_spawn_message(task.messages[1][1])
        assert summary == {
            "kind": "summary",
            "total": len(planned),
            "batches": 1,
            "counters": {"build_variant": 3, "build_common_dep": 2},
        }

    def test_count_cap_splits_into_multiple_batches(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        from compiler_suit_runner.streamed_spawn import (  # noqa: PLC0415
            MAX_BATCH_DESCRIPTORS,
            SPAWN_TOPIC,
            SUMMARY_TOPIC,
            decode_spawn_message,
        )

        matrix_dir = self._seed(tmp_path)
        planned = self._descriptors(MAX_BATCH_DESCRIPTORS + 50)
        self._patch_pipeline(monkeypatch, planned)
        task = _FakeStreamTask()
        self._run(matrix_dir, task)

        assert [t for t, _ in task.messages] == [
            SPAWN_TOPIC, SPAWN_TOPIC, SUMMARY_TOPIC,
        ]
        first = decode_spawn_message(task.messages[0][1])
        second = decode_spawn_message(task.messages[1][1])
        assert (first["seq"], second["seq"]) == (0, 1)
        assert len(first["descriptors"]) == MAX_BATCH_DESCRIPTORS
        assert len(second["descriptors"]) == 50
        # Order preserved across the batch boundary.
        assert first["descriptors"] + second["descriptors"] == planned
        summary = decode_spawn_message(task.messages[2][1])
        assert summary["total"] == len(planned)
        assert summary["batches"] == 2

    def test_empty_plan_sends_only_total_zero_summary(
        self, tmp_path: pathlib.Path,
    ):
        """No archives discovered → the empty-plan short-circuit still
        streams: NO spawn batches, exactly one total=0 summary so the
        primary's barrier sees a complete (empty) stream."""
        from compiler_suit_runner.streamed_spawn import (  # noqa: PLC0415
            SUMMARY_TOPIC,
            decode_spawn_message,
        )

        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        task = _FakeStreamTask()
        self._run(matrix_dir, task)
        assert [t for t, _ in task.messages] == [SUMMARY_TOPIC]
        assert decode_spawn_message(task.messages[0][1]) == {
            "kind": "summary", "total": 0, "batches": 0, "counters": {},
        }

    def test_planner_returning_zero_descriptors_sends_only_summary(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        from compiler_suit_runner.streamed_spawn import (  # noqa: PLC0415
            SUMMARY_TOPIC,
            decode_spawn_message,
        )

        matrix_dir = self._seed(tmp_path)
        self._patch_pipeline(monkeypatch, [])
        task = _FakeStreamTask()
        self._run(matrix_dir, task)
        assert [t for t, _ in task.messages] == [SUMMARY_TOPIC]
        assert decode_spawn_message(task.messages[0][1])["total"] == 0

    def test_task_without_send_message_raises_runtime_error(
        self, tmp_path: pathlib.Path,
    ):
        matrix_dir = self._seed(tmp_path)
        with pytest.raises(RuntimeError, match="send_message"):
            self._run(matrix_dir, object())

    def test_send_message_failure_propagates_and_fails_the_task(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """``task.send_message`` raising mid-stream must propagate out
        of ``run_dependency_graph_task`` (a partially-streamed plan
        fails the task loudly; it must not limp to a clean exit)."""

        class _Boom(Exception):
            pass

        class _ExplodingTask:
            def send_message(self, topic: str, data: bytes) -> None:
                raise _Boom("transport down")

        matrix_dir = self._seed(tmp_path)
        self._patch_pipeline(monkeypatch, self._descriptors(3))
        with pytest.raises(_Boom, match="transport down"):
            self._run(matrix_dir, _ExplodingTask())

    def test_streaming_narration_log_lines(
        self, tmp_path: pathlib.Path, monkeypatch, caplog,
    ):
        import logging  # noqa: PLC0415

        matrix_dir = self._seed(tmp_path)
        planned = self._descriptors(3)
        self._patch_pipeline(monkeypatch, planned)
        task = _FakeStreamTask()
        with caplog.at_level(
            logging.INFO,
            logger=(
                "compiler_suit_runner.workers.dependency_graph_worker.run"
            ),
        ):
            self._run(matrix_dir, task)
        messages = [rec.getMessage() for rec in caplog.records]
        batch_bytes = len(task.messages[0][1])
        assert (
            f"streamed spawn batch 0 (3 descriptors, {batch_bytes} bytes)"
            in messages
        )
        assert (
            "streamed spawn done: 3 descriptors in 1 batches; summary sent"
            in messages
        )


# ---------------------------------------------------------------------------
# _produce_build_deps_archive — unit tests
# ---------------------------------------------------------------------------
#
# These tests exercise the build_deps_local Phase 1 logic directly via
# the internal function, using a fully-controlled subprocess stub so no
# real nix store is accessed.
#
# The subprocess stub handles:
#   nix-store --query --references <variant.drv>  → input drv list
#   nix-store --query --outputs <input.drv>       → output paths
#   nix-store --query --requisites <outpath...>   → requisites closure
#   nix-store --export <path...>                  → archive bytes (via stdin)
#
# export_closure_exact is a separate helper that handles the atomic write;
# we monkeypatch preflight.export_build_deps_archive directly so we don't
# need to stub the Popen-level export machinery.


class _BuildDepsSubprocessStub:
    """Configurable stub for _produce_build_deps_archive's subprocess calls.

    Handles the full sequence of nix-store calls the function makes:
      1. ``--query --references <variant.drv>``  → input drv list
      2. ``--query --outputs <input.drv>``        → output outpaths (reads .drv)
      3. ``--realise <outpath…>``                 → realise/substitute outputs
      4. ``--query --requisites <outpath…>``      → full closure for export
    """

    def __init__(
        self,
        *,
        references_map: "dict[str, list[str]] | None" = None,
        outputs_map: "dict[str, list[str]] | None" = None,
        requisites_map: "dict[tuple, list[str]] | None" = None,
        realise_rc: int = 0,
        realise_stderr: bytes = b"",
    ) -> None:
        # variant.drv → list of input .drv references
        self.references_map: dict[str, list[str]] = references_map or {}
        # input.drv → list of output outpaths; None means .drv not in store (rc=1)
        self.outputs_map: dict[str, list[str]] = outputs_map or {}
        # (seed,) → requisites list (keyed by tuple of all argv seeds)
        self.requisites_map: dict[tuple, list[str]] = requisites_map or {}
        # return code and stderr for ``nix-store --realise`` calls
        self.realise_rc: int = realise_rc
        self.realise_stderr: bytes = realise_stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> "tuple[bytes, bytes, int]":
        self.calls.append(list(argv))
        if argv[:3] == ["nix-store", "--query", "--references"]:
            key = argv[3]
            refs = self.references_map.get(key, [])
            return ("\n".join(refs) + "\n").encode(), b"", 0
        if argv[:3] == ["nix-store", "--query", "--outputs"]:
            key = argv[3]
            out = self.outputs_map.get(key)
            if out is None:
                # The .drv itself is not in the local store (import failed).
                return b"", b"path not known\n", 1
            return ("\n".join(out) + "\n").encode(), b"", 0
        if argv[:2] == ["nix-store", "--realise"]:
            # Realise/substitute the listed output paths.
            outpaths = argv[2:]
            if self.realise_rc == 0:
                return ("\n".join(outpaths) + "\n").encode(), b"", 0
            return b"", self.realise_stderr, self.realise_rc
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            seeds = tuple(argv[3:])
            paths = self.requisites_map.get(seeds, [])
            return ("\n".join(paths) + "\n").encode(), b"", 0
        raise AssertionError(f"unexpected argv to _BuildDepsSubprocessStub: {argv!r}")


def _make_build_deps_descriptors(
    drv: str, kind: str = "build_variant",
) -> list:
    """Return a list containing one Phase4Descriptor with the given drv."""
    return [Phase4Descriptor(
        kind=kind,
        task_id=f"{kind}__0",
        name=f"{kind}__0",
        payload={"drv": drv, "pkg": "hello", "arch": "x86_64"},
        depends_on=(),
    )]


class TestProduceBuildDepsArchive:
    """Tests for ``_produce_build_deps_archive`` in the dep_graph worker."""

    _VARIANT_DRV = "/nix/store/" + "v" * 32 + "-hello-elf-folder.drv"
    _INPUT_DRV = "/nix/store/" + "i" * 32 + "-glibc.drv"
    _INPUT_OUTPATH = "/nix/store/" + "o" * 32 + "-glibc"
    _TC_OUTPATH = "/nix/store/" + "t" * 32 + "-gcc15"

    def _run(
        self,
        tmp_path: pathlib.Path,
        monkeypatch,
        *,
        descriptors=None,
        variant_lookups=None,
        runner=None,
        export_paths_out: "list | None" = None,
    ) -> None:
        """Helper: run _produce_build_deps_archive with stubbed export."""
        from compiler_suit_runner.workers.dependency_graph_worker.run import (  # noqa: PLC0415
            _produce_build_deps_archive,
        )
        from compiler_suit_runner import preflight as _pf  # noqa: PLC0415

        if descriptors is None:
            descriptors = _make_build_deps_descriptors(self._VARIANT_DRV)
        if variant_lookups is None:
            variant_lookups = {
                "hello": {
                    ("x86_64", "hello__x86_64__gcc15-O0"): {
                        "drv": self._VARIANT_DRV,
                        "toolchain_outpath": self._TC_OUTPATH,
                    },
                },
            }
        if runner is None:
            runner = _BuildDepsSubprocessStub(
                references_map={
                    self._VARIANT_DRV: [self._INPUT_DRV],
                },
                outputs_map={
                    self._INPUT_DRV: [self._INPUT_OUTPATH],
                },
                requisites_map={
                    (self._INPUT_OUTPATH,): [self._INPUT_OUTPATH],
                    (self._TC_OUTPATH,): [self._TC_OUTPATH],
                },
            )

        captured: list = []

        def _fake_export_build_deps(paths, out_dir, *, run_subprocess=None):
            captured.extend(paths)
            archive_path = out_dir / _pf.BUILD_DEPS_ARCHIVE_NAME
            archive_path.write_bytes(b"NIX_EXPORT:fake")
            return archive_path

        monkeypatch.setattr(_pf, "export_build_deps_archive", _fake_export_build_deps)

        _produce_build_deps_archive(
            descriptors=descriptors,
            variant_lookups=variant_lookups,
            matrix_eval_out_dir=tmp_path,
            runner=runner,
        )
        if export_paths_out is not None:
            export_paths_out.extend(captured)

    def test_happy_path_exports_input_outpath(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """Happy path: variant drv → input drv → output path → exported."""
        exported: list = []
        self._run(tmp_path, monkeypatch, export_paths_out=exported)
        # The input outpath (after toolchain subtraction) should be exported.
        assert self._INPUT_OUTPATH in exported

    def test_variant_pkg_drv_excluded_from_realise(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """The variant PACKAGE drv (``-variant-``) must not enter the build-deps
        set. mkBinaryFolder.nix interpolates the variant package's outputs into
        the elf-folder build script, so the variant package drv is an inputDrv
        of the elf-folder drv we query via ``--references``. Its OUTPUT is what
        the build phase PRODUCES (not pre-fetchable) — including it caused the
        #61 ``nix-store --realise`` hard gap. Regression for that filter.
        """
        variant_pkg_drv = "/nix/store/" + "p" * 32 + "-hello-variant-x86_64-1.0.drv"
        variant_pkg_out = "/nix/store/" + "q" * 32 + "-hello-variant-x86_64-1.0"
        runner = _BuildDepsSubprocessStub(
            references_map={
                # The elf-folder drv references the variant pkg drv AND a real
                # build input (glibc).
                self._VARIANT_DRV: [variant_pkg_drv, self._INPUT_DRV],
            },
            outputs_map={
                variant_pkg_drv: [variant_pkg_out],
                self._INPUT_DRV: [self._INPUT_OUTPATH],
            },
            requisites_map={
                (self._INPUT_OUTPATH,): [self._INPUT_OUTPATH],
                (self._TC_OUTPATH,): [self._TC_OUTPATH],
            },
        )
        exported: list = []
        self._run(tmp_path, monkeypatch, runner=runner, export_paths_out=exported)

        # The variant package drv must be filtered BEFORE output resolution —
        # its ``--query --outputs`` must never be called.
        assert [
            "nix-store", "--query", "--outputs", variant_pkg_drv,
        ] not in runner.calls
        # …so its output is never realised nor exported.
        realised = {
            p
            for c in runner.calls
            if c[:2] == ["nix-store", "--realise"]
            for p in c[2:]
        }
        assert variant_pkg_out not in realised, (
            f"variant package output {variant_pkg_out!r} must not be realised — "
            f"it is what the build phase produces. realised={realised!r}"
        )
        assert variant_pkg_out not in exported
        # The legitimate glibc input is still exported.
        assert self._INPUT_OUTPATH in exported

    def test_toolchain_outpath_subtracted(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """Toolchain outpath from variant_lookups is subtracted from export list."""
        # Include TC_OUTPATH in the input's requisites closure so it would appear
        # without subtraction.
        from compiler_suit_runner.workers.dependency_graph_worker.run import (  # noqa: PLC0415
            _produce_build_deps_archive,
        )
        from compiler_suit_runner import preflight as _pf  # noqa: PLC0415

        runner = _BuildDepsSubprocessStub(
            references_map={self._VARIANT_DRV: [self._INPUT_DRV]},
            outputs_map={self._INPUT_DRV: [self._INPUT_OUTPATH]},
            requisites_map={
                (self._INPUT_OUTPATH,): [self._INPUT_OUTPATH, self._TC_OUTPATH],
                (self._TC_OUTPATH,): [self._TC_OUTPATH],
            },
        )
        variant_lookups = {
            "hello": {
                ("x86_64", "lbl"): {
                    "drv": self._VARIANT_DRV,
                    "toolchain_outpath": self._TC_OUTPATH,
                },
            },
        }

        exported: list = []

        def _fake_export(paths, out_dir, *, run_subprocess=None):
            exported.extend(paths)
            p = out_dir / _pf.BUILD_DEPS_ARCHIVE_NAME
            p.write_bytes(b"x")
            return p

        monkeypatch.setattr(_pf, "export_build_deps_archive", _fake_export)
        _produce_build_deps_archive(
            descriptors=_make_build_deps_descriptors(self._VARIANT_DRV),
            variant_lookups=variant_lookups,
            matrix_eval_out_dir=tmp_path,
            runner=runner,
        )
        # TC path must have been subtracted.
        assert self._TC_OUTPATH not in exported
        # Input outpath is still present.
        assert self._INPUT_OUTPATH in exported

    def test_completeness_gate_raises_on_uncovered_outpath(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """If an input outpath is not in the requisites result, the completeness
        gate raises RuntimeError naming the offending path."""
        from compiler_suit_runner.workers.dependency_graph_worker.run import (  # noqa: PLC0415
            _produce_build_deps_archive,
        )
        from compiler_suit_runner import preflight as _pf  # noqa: PLC0415

        uncovered = "/nix/store/" + "x" * 32 + "-missing"
        runner = _BuildDepsSubprocessStub(
            references_map={self._VARIANT_DRV: [self._INPUT_DRV]},
            outputs_map={self._INPUT_DRV: [uncovered]},
            # Requisites does NOT include uncovered → completeness gate fails.
            requisites_map={
                (uncovered,): [],
                (self._TC_OUTPATH,): [self._TC_OUTPATH],
            },
        )

        with pytest.raises(RuntimeError, match="COMPLETENESS GATE FAILED"):
            _produce_build_deps_archive(
                descriptors=_make_build_deps_descriptors(self._VARIANT_DRV),
                variant_lookups={
                    "hello": {
                        ("x86_64", "lbl"): {
                            "drv": self._VARIANT_DRV,
                            "toolchain_outpath": self._TC_OUTPATH,
                        },
                    },
                },
                matrix_eval_out_dir=tmp_path,
                runner=runner,
            )

    def test_no_variant_drvs_logs_warning_and_skips(
        self, tmp_path: pathlib.Path, monkeypatch, caplog,
    ) -> None:
        """If descriptors and variant_lookups produce no variant drvs, a warning
        is logged and no archive is produced."""
        import logging  # noqa: PLC0415
        from compiler_suit_runner.workers.dependency_graph_worker.run import (  # noqa: PLC0415
            _produce_build_deps_archive,
        )
        from compiler_suit_runner import preflight as _pf  # noqa: PLC0415

        export_calls: list = []
        monkeypatch.setattr(
            _pf, "export_build_deps_archive",
            lambda *a, **kw: export_calls.append(a) or (tmp_path / _pf.BUILD_DEPS_ARCHIVE_NAME),
        )

        runner = _BuildDepsSubprocessStub()
        with caplog.at_level(logging.WARNING, logger=(
            "compiler_suit_runner.dependency_graph_worker.build_deps"
        )):
            _produce_build_deps_archive(
                descriptors=[],          # no variant descriptors
                variant_lookups={},      # no variant lookups
                matrix_eval_out_dir=tmp_path,
                runner=runner,
            )
        assert export_calls == [], "should not export when no variant drvs found"
        assert any("no variant drvs" in r.message for r in caplog.records)

    def test_references_failure_raises_runtime_error(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """A non-zero rc from nix-store --query --references raises RuntimeError."""
        from compiler_suit_runner.workers.dependency_graph_worker.run import (  # noqa: PLC0415
            _produce_build_deps_archive,
        )

        def _failing_runner(argv):
            if argv[:3] == ["nix-store", "--query", "--references"]:
                return b"", b"derivation not found\n", 1
            if argv[:3] == ["nix-store", "--query", "--requisites"]:
                return b"", b"", 0
            raise AssertionError(f"unexpected: {argv!r}")

        with pytest.raises(RuntimeError, match="--query --references"):
            _produce_build_deps_archive(
                descriptors=_make_build_deps_descriptors(self._VARIANT_DRV),
                variant_lookups={},
                matrix_eval_out_dir=tmp_path,
                runner=_failing_runner,
            )

    def test_realise_invoked_on_input_outpaths_before_requisites(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """``nix-store --realise`` must be called on the resolved input
        output paths BEFORE ``nix-store --query --requisites``.

        The dep_graph worker only holds the drv graph (imported archives),
        not the built outputs.  Without realisation the --requisites and
        export calls fail on the unrealised majority.  This test asserts:

        1. A ``--realise`` call appears in the stub's call list.
        2. The ``--realise`` call lists the input outpaths (not the drvs).
        3. The ``--realise`` call precedes any ``--query --requisites`` call.
        """
        from compiler_suit_runner.workers.dependency_graph_worker.run import (  # noqa: PLC0415
            _produce_build_deps_archive,
        )
        from compiler_suit_runner import preflight as _pf  # noqa: PLC0415

        runner = _BuildDepsSubprocessStub(
            references_map={self._VARIANT_DRV: [self._INPUT_DRV]},
            outputs_map={self._INPUT_DRV: [self._INPUT_OUTPATH]},
            requisites_map={
                (self._INPUT_OUTPATH,): [self._INPUT_OUTPATH],
                (self._TC_OUTPATH,): [self._TC_OUTPATH],
            },
        )

        captured_export: list = []

        def _fake_export(paths, out_dir, *, run_subprocess=None):
            captured_export.extend(paths)
            p = out_dir / _pf.BUILD_DEPS_ARCHIVE_NAME
            p.write_bytes(b"x")
            return p

        monkeypatch.setattr(_pf, "export_build_deps_archive", _fake_export)

        _produce_build_deps_archive(
            descriptors=_make_build_deps_descriptors(self._VARIANT_DRV),
            variant_lookups={
                "hello": {
                    ("x86_64", "hello__x86_64__gcc15-O0"): {
                        "drv": self._VARIANT_DRV,
                        "toolchain_outpath": self._TC_OUTPATH,
                    },
                },
            },
            matrix_eval_out_dir=tmp_path,
            runner=runner,
        )

        # Collect the positions of --realise and --query --requisites calls.
        realise_positions = [
            i for i, c in enumerate(runner.calls)
            if c[:2] == ["nix-store", "--realise"]
        ]
        requisites_positions = [
            i for i, c in enumerate(runner.calls)
            if c[:3] == ["nix-store", "--query", "--requisites"]
        ]

        assert realise_positions, (
            "expected at least one 'nix-store --realise' call; "
            f"got calls: {runner.calls!r}"
        )
        # The --realise call must list the input outpath (not the drv).
        realise_argv = runner.calls[realise_positions[0]]
        assert self._INPUT_OUTPATH in realise_argv, (
            f"--realise argv must include {self._INPUT_OUTPATH!r}; "
            f"got: {realise_argv!r}"
        )
        # --realise must precede --query --requisites.
        assert requisites_positions, (
            "expected at least one '--query --requisites' call"
        )
        assert realise_positions[0] < requisites_positions[0], (
            "--realise must be called before --query --requisites; "
            f"realise pos={realise_positions[0]}, "
            f"requisites pos={requisites_positions[0]}"
        )

    def test_realise_failure_raises_runtime_error(
        self, tmp_path: pathlib.Path, monkeypatch,
    ) -> None:
        """A non-zero rc from ``nix-store --realise`` raises RuntimeError
        naming the unrealisable paths.

        A build input that cannot be realised (neither in the store nor
        substitutable) is a hard gap; the feature must fail loud rather
        than silently skipping and producing an incomplete archive.
        """
        from compiler_suit_runner.workers.dependency_graph_worker.run import (  # noqa: PLC0415
            _produce_build_deps_archive,
        )

        runner = _BuildDepsSubprocessStub(
            references_map={self._VARIANT_DRV: [self._INPUT_DRV]},
            outputs_map={self._INPUT_DRV: [self._INPUT_OUTPATH]},
            realise_rc=1,
            realise_stderr=b"error: cannot substitute /nix/store/oo...-glibc",
        )

        with pytest.raises(RuntimeError, match="--realise failed"):
            _produce_build_deps_archive(
                descriptors=_make_build_deps_descriptors(self._VARIANT_DRV),
                variant_lookups={
                    "hello": {
                        ("x86_64", "lbl"): {
                            "drv": self._VARIANT_DRV,
                            "toolchain_outpath": self._TC_OUTPATH,
                        },
                    },
                },
                matrix_eval_out_dir=tmp_path,
                runner=runner,
            )
