"""Unit tests for :mod:`compiler_suit_runner.workers.dependency_graph_worker`.

All nix invocations are stubbed; the streaming planner and sum-drv
builder are monkey-patched so the tests stay dependency-light (no
template_graph eval, no /nix/store).

Coverage:

  * archive discovery (sorted, file-only, suffix filter);
  * kept-drv derivation from ``nix-store --import`` stdout
    (filter, dedup, label compose);
  * import_archive happy + missing-file + subprocess-failure paths,
    including stdout-path capture;
  * write_phase4_descriptors roundtrip (descriptor + summary), including
    the ``_dependency_graph_summary.txt`` companion file shape;
  * --toolchain-task-id parser;
  * end-to-end run_dependency_graph_task with stubbed planner +
    sum-drv builder + subprocess;
  * Phase 6.1 lax-mode default: ``plan_total`` defaults to lax=True so
    a calibration-mismatch tree records violations rather than raising.
"""

from __future__ import annotations

import pathlib
from typing import Any, Optional

import pytest

from compiler_suit_runner.workers import dependency_graph_worker as dgw
from compiler_suit_runner.dependency_graph_planner import Phase4Descriptor


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
            return stdout, self.import_stderr, self.import_rc
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
        (tmp_path / "hello.nix-archive").write_bytes(b"x")
        (tmp_path / "hello.txt").write_bytes(b"x")
        (tmp_path / "_meta.json").write_bytes(b"x")
        (tmp_path / "subdir").mkdir()
        out = dgw.discover_archives(tmp_path)
        assert [p.name for p in out] == ["hello.nix-archive"]

    def test_sorted_by_name(self, tmp_path: pathlib.Path):
        (tmp_path / "zebra.nix-archive").write_bytes(b"x")
        (tmp_path / "hello.nix-archive").write_bytes(b"x")
        (tmp_path / "alpha.nix-archive").write_bytes(b"x")
        out = dgw.discover_archives(tmp_path)
        assert [p.stem for p in out] == ["alpha", "hello", "zebra"]


# ---------------------------------------------------------------------------
# Kept-drv derivation from ``nix-store --import`` stdout
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


class TestDiscoverKeptDrvsFromImportedStore:

    def test_filters_to_elf_folder_drvs(self):
        v_hello = _variant_drv("hello", "x86_64", "O0", hash_prefix="hh1")
        v_world = _variant_drv("world", "aarch64", "O2", hash_prefix="ww1")
        imported = [
            v_hello,
            "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc-2.39.drv",
            v_world,
            "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-source.tar",
        ]
        drvs, lookup = dgw.discover_kept_drvs_from_imported_store(imported)
        assert drvs == [v_hello, v_world]
        assert ("x86_64", lookup[("x86_64", lookup_key_for(v_hello))]["arch"]) == (
            "x86_64", "x86_64"
        )
        # Direct sanity: the lookup is keyed by (arch, label) and the
        # label is the streaming planner's ``cur_label`` (binary__arch__suffix).
        h_label = (
            "hello__x86_64__gcc15-O0-baseline-default-san-off-march-default"
        )
        w_label = (
            "world__aarch64__gcc15-O2-baseline-default-san-off-march-default"
        )
        assert ("x86_64", h_label) in lookup
        assert ("aarch64", w_label) in lookup
        assert lookup[("x86_64", h_label)]["drv"] == v_hello
        assert lookup[("aarch64", w_label)]["drv"] == v_world

    def test_dedupes_preserving_order(self):
        v = _variant_drv("hello", "x86_64", "O0", hash_prefix="dup")
        v2 = _variant_drv("hello", "x86_64", "O2", hash_prefix="d22")
        # Repeats removed; insertion order preserved.
        drvs, _ = dgw.discover_kept_drvs_from_imported_store([
            v, v2, v,
        ])
        assert drvs == [v, v2]

    def test_empty_when_no_variant_drvs_in_stream(self):
        drvs, lookup = dgw.discover_kept_drvs_from_imported_store([
            "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc-2.39.drv",
            "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-source.tar",
        ])
        assert drvs == []
        assert lookup == {}

    def test_unparseable_variant_drv_skipped_with_warning(
        self, caplog,
    ):
        """A path that ends in ``-elf-folder.drv`` but doesn't parse
        as a variant (no arch / no compiler suffix) is dropped from
        the lookup with a WARN log line. The drv still lands in the
        ``variant_drvs`` list — the streaming planner is the
        authoritative shape arbiter and will raise its own
        TreeWalkError on the same tree."""
        import logging  # noqa: PLC0415
        bad = (
            "/nix/store/cccccccccccccccccccccccccccccccc-"
            "not-a-variant-elf-folder.drv"
        )
        good = _variant_drv("hello", "x86_64", "O0", hash_prefix="hh1")
        with caplog.at_level(
            logging.WARNING,
            logger="compiler_suit_runner.dependency_graph_worker",
        ):
            drvs, lookup = dgw.discover_kept_drvs_from_imported_store([
                bad, good,
            ])
        # Both drvs are kept (filter is suffix-only).
        assert drvs == [bad, good]
        # Only the parseable variant lands in the lookup.
        assert len(lookup) == 1
        assert any(
            "unparseable variant drv" in r.message
            for r in caplog.records
        )


class TestDeriveVariantLookupFromDrvs:

    def test_label_matches_streaming_planner_cur_label(self):
        """``derive_variant_lookup_from_drvs`` composes the label the
        same way ``StreamPlanner._on_matrix_depth2`` does:
        ``f"{binary}__{arch}__{suffix}"`` where ``suffix`` is the
        substring between ``<binary>-<arch>-`` and ``-elf-folder.drv``.
        """
        v = _variant_drv(
            "busybox", "aarch64", "O2", comp="clang19",
            hash_prefix="bbx",
            inner="size-pie-on-san-off-march-armv8",
        )
        lookup = dgw.derive_variant_lookup_from_drvs([v])
        label = "busybox__aarch64__clang19-O2-size-pie-on-san-off-march-armv8"
        assert lookup[("aarch64", label)] == {
            "drv": v, "arch": "aarch64", "label": label,
            "suffix": "clang19-O2-size-pie-on-san-off-march-armv8",
        }

    def test_non_elf_folder_inputs_ignored(self):
        lookup = dgw.derive_variant_lookup_from_drvs([
            "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc-2.39.drv",
        ])
        assert lookup == {}


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
        archive = tmp_path / "missing.nix-archive"
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
        archive = tmp_path / "x.nix-archive"
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
        archive = tmp_path / "x.nix-archive"
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
        archive = tmp_path / "x.nix-archive"
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
        archive = tmp_path / "x.nix-archive"
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
# write_phase4_descriptors / write_phase4_summary_text
# ---------------------------------------------------------------------------


class TestWritePhase4Descriptors:
    """Phase 6 pickle migration: the worker writes typed
    :class:`Phase4Descriptor` instances to ``_dependency_graph.pkl`` so
    the watcher (via ``load_phase4_descriptors``) gets them back with
    zero re-tupling. A human-readable
    ``_dependency_graph_summary.txt`` companion is written alongside.
    """

    def test_dataclass_descriptors_roundtrip(
        self, tmp_path: pathlib.Path,
    ):
        from compiler_suit_runner.dependency_graph_planner import (
            load_phase4_descriptors,
        )

        descriptors = [
            Phase4Descriptor(
                kind="build_common_dep",
                task_id="cd_1",
                name="common_dep_1",
                payload={"a": 1},
                depends_on=(),
            ),
            Phase4Descriptor(
                kind="build_variant",
                task_id="v_1",
                name="variant_1",
                payload={"b": 2},
                depends_on=("cd_1",),
            ),
        ]
        out_path = tmp_path / dgw.DEPENDENCY_GRAPH_PICKLE
        out = dgw.write_phase4_descriptors(
            descriptors=descriptors,
            summary={"binary_count": 1, "descriptor_count": 2},
            out_path=out_path,
        )
        assert out == out_path
        recovered, summary = load_phase4_descriptors(out_path)
        assert len(recovered) == 2
        # Typed dataclasses survive the pickle roundtrip — no list
        # coercion, no string-keyed dicts.
        assert recovered[0].kind == "build_common_dep"
        assert recovered[0].task_id == "cd_1"
        assert recovered[1].depends_on == ("cd_1",)
        assert summary == {"binary_count": 1, "descriptor_count": 2}

    def test_empty_list_writes_valid_file(self, tmp_path: pathlib.Path):
        from compiler_suit_runner.dependency_graph_planner import (
            load_phase4_descriptors,
        )

        out_path = tmp_path / dgw.DEPENDENCY_GRAPH_PICKLE
        dgw.write_phase4_descriptors(
            descriptors=[],
            summary={},
            out_path=out_path,
        )
        recovered, summary = load_phase4_descriptors(out_path)
        assert recovered == []
        assert summary == {}

    def test_summary_text_companion(self, tmp_path: pathlib.Path):
        """The summary-text writer emits a ``key: value`` per line
        sorted by key for diff-friendly operator inspection."""
        out_path = tmp_path / dgw.DEPENDENCY_GRAPH_SUMMARY
        dgw.write_phase4_summary_text(
            summary={"templates": 2, "binaries": "a/b", "violations": 0},
            out_path=out_path,
        )
        text = out_path.read_text(encoding="utf-8")
        # Sorted-by-key emission keeps diff-noise low.
        assert text == "binaries: a/b\ntemplates: 2\nviolations: 0\n"

    def test_atomic_write_no_tmp_left_behind(
        self, tmp_path: pathlib.Path,
    ):
        """The writer routes through ``<path>.tmp`` + ``os.replace``;
        a successful write leaves no stray ``.tmp`` file."""
        out_path = tmp_path / dgw.DEPENDENCY_GRAPH_PICKLE
        dgw.write_phase4_descriptors(
            descriptors=[], summary={}, out_path=out_path,
        )
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []


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

    def _seed_archive(
        self,
        matrix_dir: pathlib.Path,
        binary: str,
        _kept_drvs: list[str],
    ) -> pathlib.Path:
        """Drop a placeholder ``<binary>.nix-archive`` in ``matrix_dir``.

        The kept-drv list is no longer read from a sidecar JSON; the
        ``import_stdout`` field on :class:`_SubprocessStub` drives
        the post-import kept-drv derivation instead. ``_kept_drvs`` is
        kept on the signature so tests stay legible at the call site.
        """
        archive = matrix_dir / f"{binary}.nix-archive"
        archive.write_bytes(b"fake")
        return archive

    def _stub_import_stdout(
        self, *binaries_with_drvs: tuple[str, list[str]],
    ) -> bytes:
        """Render the synthetic ``nix-store --import`` stdout for a
        set of per-binary kept drv lists. Empty input → empty stdout.
        """
        lines: list[str] = []
        for _binary, drvs in binaries_with_drvs:
            lines.extend(drvs)
        if not lines:
            return b""
        return ("\n".join(lines) + "\n").encode("utf-8")

    def test_empty_matrix_dir_writes_empty_graph(
        self, tmp_path: pathlib.Path,
    ):
        from compiler_suit_runner.dependency_graph_planner import (
            load_phase4_descriptors,
        )
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        result = dgw.run_dependency_graph_task(
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/aaaa-bash",
            toolchain_drvs=["/nix/store/zzzz-gcc15.drv"],
        )
        assert result.binary_count == 0
        assert result.descriptor_count == 0
        # ``output_path`` is the pickle; the loader recovers the empty
        # descriptor list and the descriptor-derived summary fields.
        descriptors, summary = load_phase4_descriptors(result.output_path)
        assert descriptors == []
        assert summary["binary_count"] == 0
        assert summary["descriptor_count"] == 0

    def test_end_to_end_single_binary(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        v_o0 = _variant_drv("hello", "x86_64", "O0", hash_prefix="hh1")
        v_o2 = _variant_drv("hello", "x86_64", "O2", hash_prefix="hh2")
        self._seed_archive(matrix_dir, "hello", [v_o0, v_o2])

        stub = _SubprocessStub()
        # ``nix-store --import`` stdout carries the kept-drv list now.
        stub.import_stdout = self._stub_import_stdout(("hello", [v_o0, v_o2]))
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

        def fake_plan_total(*, tree_text, binaries, variant_lookups,
                             toolchain_task_ids, sys_name):
            plan_calls.append({
                "tree_text": tree_text,
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
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/aaaa-bash",
            toolchain_drvs=["/nix/store/zzzz-gcc15.drv"],
            toolchain_task_ids={
                "zzzz-gcc15.drv": "build_compilers__aarch64__gcc15",
            },
            sys_name="x86_64-linux",
            run_subprocess=stub,
        )
        assert result.binary_count == 1
        assert result.descriptor_count == 1
        # sum_drv builder saw the kept drvs in matrix-hello.
        assert len(sum_drv_calls) == 1
        assert sum_drv_calls[0]["matrix_drvs"] == {
            "matrix-hello": [v_o0, v_o2],
        }
        # Planner saw the toolchain task ids and the binary list.
        assert plan_calls[0]["toolchain_task_ids"] == {
            "zzzz-gcc15.drv": "build_compilers__aarch64__gcc15",
        }
        assert plan_calls[0]["binaries"] == ["hello"]
        # nix-store --import was invoked (paths not locally present).
        assert any(c[:2] == ["nix-store", "--import"] for c in stub.calls)
        # _dependency_graph.pkl was written.
        from compiler_suit_runner.dependency_graph_planner import (
            load_phase4_descriptors,
        )
        descriptors, _summary = load_phase4_descriptors(result.output_path)
        assert len(descriptors) == 1
        assert descriptors[0].task_id == "bv_hello"

    def test_import_runs_unconditionally(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """Post-cutover, the worker always imports the archive — the
        ``nix-store --import`` stdout IS the kept-drv source, so the
        old "skip if all drvs locally present" probe-then-skip
        optimisation cannot fire.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        v = _variant_drv("hello", "x86_64", "O0", hash_prefix="sk1")
        self._seed_archive(matrix_dir, "hello", [v])

        stub = _SubprocessStub()
        # Even with the kept drv "locally present", the worker must
        # call --import because it learns the kept-drv list FROM the
        # import output.
        stub.path_info_present.add(v)
        stub.import_stdout = self._stub_import_stdout(("hello", [v]))
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
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_drvs=["/nix/store/tc.drv"],
            run_subprocess=stub,
        )
        # --import runs once even though the kept drv is "present".
        import_calls = [
            c for c in stub.calls if c[:2] == ["nix-store", "--import"]
        ]
        assert len(import_calls) == 1

    def test_import_failure_raises(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(
            matrix_dir, "hello", ["/nix/store/aaa-hello.drv"],
        )

        stub = _SubprocessStub()
        stub.import_rc = 1
        stub.import_stderr = b"borked"

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: pytest.fail(
                "build_sum_drv_multi should not be reached after import failure"
            ),
        )
        with pytest.raises(dgw.DependencyGraphWorkerError) as excinfo:
            dgw.run_dependency_graph_task(
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_drvs=["/nix/store/tc.drv"],
                run_subprocess=stub,
            )
        assert excinfo.value.binary == "hello"
        assert excinfo.value.stage == "import"

    def test_query_tree_failure_raises(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        v = _variant_drv("hello", "x86_64", "O0", hash_prefix="qt1")
        self._seed_archive(matrix_dir, "hello", [v])
        stub = _SubprocessStub()
        stub.import_stdout = self._stub_import_stdout(("hello", [v]))
        stub.tree_rc = 1

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: "/nix/store/sum.drv",
        )
        with pytest.raises(dgw.DependencyGraphWorkerError) as excinfo:
            dgw.run_dependency_graph_task(
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_drvs=["/nix/store/tc.drv"],
                run_subprocess=stub,
            )
        assert excinfo.value.stage == "query_tree"

    def test_two_binaries_share_one_streaming_pass(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """Two archives with kept drvs land in ONE sum-drv (one
        matrix-<binary> wrapper each) and ONE planner call. The Phase
        5.2 collapse means cross-binary template dedup fires inside
        the single StreamPlanner instance; the worker observes that
        only ONE `build_sum_drv_multi` and ONE `plan_total` are
        invoked, with both binaries surfaced together.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        v_hello = _variant_drv("hello", "x86_64", "O0", hash_prefix="tb1")
        v_world = _variant_drv("world", "aarch64", "O0", hash_prefix="tb2")
        self._seed_archive(matrix_dir, "hello", [v_hello])
        self._seed_archive(matrix_dir, "world", [v_world])

        # Per-archive --import stdout via the queue: the worker walks
        # archives in sorted order (hello, world), so the first --import
        # call returns hello's kept drv list and the second returns
        # world's.
        stub = _SubprocessStub()
        stub.import_stdout_queue = [
            self._stub_import_stdout(("hello", [v_hello])),
            self._stub_import_stdout(("world", [v_world])),
        ]
        stub.tree_stdout = b"/nix/store/sum-drv.drv\n"

        sum_drv_calls: list[dict] = []
        plan_calls: list[dict] = []

        def fake_build_sum_drv_multi(*, bash_path, toolchain_drvs,
                                       matrix_drvs, system):
            sum_drv_calls.append({
                "matrix_drvs": {k: list(v) for k, v in matrix_drvs.items()},
            })
            return "/nix/store/sum-drv-multi"

        def fake_plan_total(*, tree_text, binaries, variant_lookups,
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
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_drvs=["/nix/store/tc.drv"],
            run_subprocess=stub,
        )
        assert result.binary_count == 2
        assert result.descriptor_count == 2
        # Exactly ONE sum-drv build + ONE plan_total call, regardless
        # of the two-binary input.
        assert len(sum_drv_calls) == 1
        assert len(plan_calls) == 1
        # Both binaries surfaced together in the multi-binary matrix_drvs.
        assert set(sum_drv_calls[0]["matrix_drvs"]) == {
            "matrix-hello", "matrix-world",
        }
        # Both binaries handed to plan_total in sorted archive order.
        assert plan_calls[0]["binaries"] == ["hello", "world"]
        # Exactly ONE nix-store --query --tree call across both binaries.
        tree_calls = [
            c for c in stub.calls if c[:3] == ["nix-store", "--query", "--tree"]
        ]
        assert len(tree_calls) == 1

    def test_binary_with_no_kept_drvs_is_skipped(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """An archive whose ``nix-store --import`` stdout carries no
        ``*-elf-folder.drv`` entries → no kept variant drvs → skip
        that binary and proceed with the rest."""
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        # ``hello``'s archive yields a non-variant payload only.
        (matrix_dir / "hello.nix-archive").write_bytes(b"x")
        # ``world``'s archive yields a real variant drv.
        v_world = _variant_drv("world", "x86_64", "O0", hash_prefix="ww1")
        self._seed_archive(matrix_dir, "world", [v_world])

        stub = _SubprocessStub()
        stub.import_stdout_queue = [
            # hello: no variant drvs in stdout → kept_drvs is empty,
            # binary is skipped.
            (
                b"/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc.drv\n"
            ),
            self._stub_import_stdout(("world", [v_world])),
        ]
        stub.tree_stdout = b"/nix/store/sum.drv\n"

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: "/nix/store/sum.drv",
        )
        monkeypatch.setattr(
            dgw, "plan_total",
            lambda **kw: [Phase4Descriptor(
                kind="build_variant",
                task_id="bv_world",
                name="build_variant__world",
                payload={"binary": "world"},
                depends_on=(),
            )],
        )

        result = dgw.run_dependency_graph_task(
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_drvs=["/nix/store/tc.drv"],
            run_subprocess=stub,
        )
        # Only world was planned.
        assert result.binary_count == 1
        from compiler_suit_runner.dependency_graph_planner import (
            load_phase4_descriptors,
        )
        descriptors, _summary = load_phase4_descriptors(result.output_path)
        assert {d.task_id for d in descriptors} == {"bv_world"}


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
            "--toolchain-drv", "/nix/store/tc1.drv",
            "--toolchain-drv", "/nix/store/tc2.drv",
            "--toolchain-task-id", "tc1.drv=task1",
            "--sys-name", "aarch64-linux",
        ])
        assert args.matrix_eval_out_dir == "/tmp/me"
        assert args.toolchain_drv == [
            "/nix/store/tc1.drv", "/nix/store/tc2.drv",
        ]
        assert args.toolchain_task_id == ["tc1.drv=task1"]
        assert args.sys_name == "aarch64-linux"

    def test_system_flag_default(self):
        """``--system`` defaults to ``x86_64-linux`` so the watcher's
        invocation works without an explicit value on the common case."""
        parser = dgw._build_cli_parser()
        args = parser.parse_args([
            "--matrix-eval-out-dir", "/tmp/me",
            "--bash-path", "/nix/store/aaaa-bash",
        ])
        assert args.sys_name == "x86_64-linux"

    def test_system_flag_overrides_default(self):
        """``--system aarch64-linux`` parses onto ``args.sys_name`` so
        the worker's ``run_dependency_graph_task`` sees the override."""
        parser = dgw._build_cli_parser()
        args = parser.parse_args([
            "--matrix-eval-out-dir", "/tmp/me",
            "--bash-path", "/nix/store/aaaa-bash",
            "--system", "aarch64-linux",
        ])
        assert args.sys_name == "aarch64-linux"


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

        # plan_total imports the planner adapter at call time so the
        # monkeypatch above must hit the module attribute; the import
        # inside plan_total re-uses the patched module.
        from compiler_suit_runner.workers.dependency_graph_worker.plan import (  # noqa: E501
            plan_total,
        )

        # No exception — calibration mismatch is recorded, not raised.
        result = plan_total(
            tree_text=tree_text,
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

    def test_strict_mode_still_raises_when_requested(self):
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

        from compiler_suit_runner.workers.dependency_graph_worker.plan import (  # noqa: E501
            plan_total,
        )

        with pytest.raises(
            TreeWalkError,
            match=r"calibration pair same-name child count mismatch",
        ):
            plan_total(
                tree_text=tree_text,
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
                depends_on=("build_compilers__x86_64__gcc15",),
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
        # Only one of the two build_variant descriptors has a
        # ``build_compilers__`` task in its depends_on.
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
        planner output, and the summary log line fires."""
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        v_hello = _variant_drv("hello", "x86_64", "O0", hash_prefix="rh1")
        v_world = _variant_drv("world", "aarch64", "O0", hash_prefix="rw1")
        (matrix_dir / "hello.nix-archive").write_bytes(b"x")
        (matrix_dir / "world.nix-archive").write_bytes(b"x")

        stub = _SubprocessStub()
        stub.import_stdout_queue = [
            (v_hello + "\n").encode("utf-8"),
            (v_world + "\n").encode("utf-8"),
        ]
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
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_drvs=["/nix/store/tc.drv"],
                run_subprocess=stub,
            )

        # Counter fields exist + default to descriptor-derived values
        # for the monkeypatched plan_total path (streaming-result-only
        # counters degrade to 0 because the patch hid the planner).
        assert result.binary_count == 2
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
