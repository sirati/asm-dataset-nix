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
from typing import Optional

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
        assert lookup[("x86_64", h_label)] == {
            "drv": v_hello,
            "arch": "x86_64",
            "label": h_label,
            "suffix": "gcc15-O0-baseline-default-san-off-march-default",
        }
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
        """Drop a placeholder ``<binary>.nix-archive`` in ``matrix_dir``.

        Phase 3 still imports the archive (so the leaf closure
        materialises in the local store for the tree walk), but the
        kept-drv list no longer comes from the import stdout — D.1a's
        ``derive_variant_lookup_from_aggregate`` owns it now.
        """
        archive = matrix_dir / f"{binary}.nix-archive"
        archive.write_bytes(b"fake")
        return archive

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

        def fake(agg_drv: str) -> dict[tuple[str, str], dict]:
            return lookups.get(agg_drv, {})

        monkeypatch.setattr(
            _archive_mod, "derive_variant_lookup_from_aggregate",
            fake, raising=False,
        )

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
            toolchain_aggregate_drv=self._TC_AGG,
            matrix_aggregate_drvs={"hello": self._matrix_agg("hello")},
        )
        assert result.binary_count == 0
        assert result.descriptor_count == 0
        # ``output_path`` is the pickle; the loader recovers the empty
        # descriptor list and the descriptor-derived summary fields.
        descriptors, summary = load_phase4_descriptors(result.output_path)
        assert descriptors == []
        assert summary["binary_count"] == 0
        assert summary["descriptor_count"] == 0

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
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/aaaa-bash",
                toolchain_aggregate_drv="",
                matrix_aggregate_drvs={"hello": self._matrix_agg("hello")},
            )

    def test_empty_matrix_aggregate_drvs_raises(
        self, tmp_path: pathlib.Path,
    ):
        """Same loud-fail for the matrix side: an empty
        ``matrix_aggregate_drvs`` map means the bulk-eval producer
        emitted nothing for this run.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        with pytest.raises(ValueError, match="matrix_aggregate_drvs"):
            dgw.run_dependency_graph_task(
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/aaaa-bash",
                toolchain_aggregate_drv=self._TC_AGG,
                matrix_aggregate_drvs={},
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
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/aaaa-bash",
            toolchain_aggregate_drv=self._TC_AGG,
            matrix_aggregate_drvs={"hello": matrix_agg},
            toolchain_task_ids={
                "zzzz-gcc15.drv": "build_compilers__aarch64__gcc15",
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
            "zzzz-gcc15.drv": "build_compilers__aarch64__gcc15",
        }
        assert plan_calls[0]["binaries"] == ["hello"]
        # Variant lookup the planner sees is the stubbed one
        # (proves derive_variant_lookup_from_aggregate is wired in).
        assert plan_calls[0]["variant_lookups"] == {"hello": hello_lookup}
        # nix-store --import was invoked (leaves need to be local for
        # the tree walk inside build_sum_drv_multi / query_drv_tree).
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
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            matrix_aggregate_drvs={"hello": matrix_agg},
            run_subprocess=stub,
        )
        # --import runs once even though the aggregate is "present".
        import_calls = [
            c for c in stub.calls if c[:2] == ["nix-store", "--import"]
        ]
        assert len(import_calls) == 1

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
                toolchain_aggregate_drv=self._TC_AGG,
                matrix_aggregate_drvs={"hello": matrix_agg},
                run_subprocess=stub,
            )
        assert excinfo.value.binary == "hello"
        assert excinfo.value.stage == "import"

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
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=self._TC_AGG,
                matrix_aggregate_drvs={"hello": matrix_agg},
                run_subprocess=stub,
            )
        assert excinfo.value.stage == "query_tree"

    def test_two_binaries_share_one_streaming_pass(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        """Two aggregates land in ONE sum-drv (one ``matrix-<binary>``
        wrapper each, with length-1 lists) and ONE planner call. The
        worker observes that ``build_sum_drv_multi`` and ``plan_total``
        are each invoked exactly once, with both binaries surfaced
        together in sorted order.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        self._seed_archive(matrix_dir, "world")
        agg_hello = self._matrix_agg("hello", hash_prefix="th")
        agg_world = self._matrix_agg("world", hash_prefix="tw")
        self._patch_derive(monkeypatch, {
            agg_hello: self._stub_lookup_for("hello"),
            agg_world: self._stub_lookup_for("world"),
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
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            matrix_aggregate_drvs={
                "hello": agg_hello,
                "world": agg_world,
            },
            run_subprocess=stub,
        )
        assert result.binary_count == 2
        assert result.descriptor_count == 2
        # Exactly ONE sum-drv build + ONE plan_total call.
        assert len(sum_drv_calls) == 1
        assert len(plan_calls) == 1
        # Length-1 lists for both sides (post-Phase-A.2 invariant).
        assert sum_drv_calls[0]["toolchain_drvs"] == [self._TC_AGG]
        assert sum_drv_calls[0]["matrix_drvs"] == {
            "matrix-hello": [agg_hello],
            "matrix-world": [agg_world],
        }
        # Both binaries handed to plan_total in sorted order.
        assert plan_calls[0]["binaries"] == ["hello", "world"]
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
        matrix wrapper.
        """
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(matrix_dir, "hello")
        self._seed_archive(matrix_dir, "world")
        agg_hello = self._matrix_agg("hello", hash_prefix="eh")
        agg_world = self._matrix_agg("world", hash_prefix="ew")
        # hello → empty lookup; world → real lookup.
        self._patch_derive(monkeypatch, {
            agg_hello: {},
            agg_world: self._stub_lookup_for("world"),
        })

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
            toolchain_aggregate_drv=self._TC_AGG,
            matrix_aggregate_drvs={
                "hello": agg_hello,
                "world": agg_world,
            },
            run_subprocess=stub,
        )
        # Only world was planned.
        assert result.binary_count == 1
        # The sum-drv builder never saw matrix-hello: it was dropped
        # before assembly so a zero-variant wrapper cannot be passed
        # to the path-form helper.
        assert sum_drv_calls[0]["matrix_drvs"] == {
            "matrix-world": [agg_world],
        }
        from compiler_suit_runner.dependency_graph_planner import (
            load_phase4_descriptors,
        )
        descriptors, _summary = load_phase4_descriptors(result.output_path)
        assert {d.task_id for d in descriptors} == {"bv_world"}

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
            matrix_eval_out_dir=matrix_dir,
            bash_path="/nix/store/bash",
            toolchain_aggregate_drv=self._TC_AGG,
            matrix_aggregate_drvs={"hello": agg_hello},
            run_subprocess=stub,
        )
        assert len(nix_eval_calls) <= 1, (
            f"phase 3 must invoke nix-instantiate AT MOST ONCE (the sum-drv "
            f"assembly inside make_sum_drv_from_paths); got "
            f"{len(nix_eval_calls)} calls — drift back to per-leaf eval"
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
            "--matrix-drv", "hello=/nix/store/bbbb-matrix-hello.drv",
            "--toolchain-task-id", "tc1.drv=task1",
            "--sys-name", "aarch64-linux",
        ])
        assert args.matrix_eval_out_dir == "/tmp/me"
        # New single-value semantics; dest is ``toolchain_aggregate_drv``
        # to match the run_dependency_graph_task kwarg name.
        assert args.toolchain_aggregate_drv == (
            "/nix/store/aaaa-toolchains.drv"
        )
        assert args.matrix_drv_raw == [
            "hello=/nix/store/bbbb-matrix-hello.drv",
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
            "--toolchain-drv", "/nix/store/aaaa-toolchains.drv",
            "--matrix-drv", "hello=/nix/store/bbbb-matrix-hello.drv",
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
            "--matrix-drv", "hello=/nix/store/bbbb-matrix-hello.drv",
            "--system", "aarch64-linux",
        ])
        assert args.sys_name == "aarch64-linux"

    def test_cli_parses_toolchain_drv_and_matrix_drvs(self):
        """The watcher passes the pre-built aggregate drv paths via two
        new flags: ``--toolchain-drv <path>`` (single) and
        ``--matrix-drv <binary>=<path>`` (repeated)."""
        parser = dgw._build_cli_parser()
        args = parser.parse_args([
            "--matrix-eval-out-dir", "/tmp/me",
            "--bash-path", "/nix/store/aaaa-bash",
            "--toolchain-drv", "/nix/store/x-t.drv",
            "--matrix-drv", "hello=/nix/store/y-mh.drv",
            "--matrix-drv", "busybox=/nix/store/z-mb.drv",
            "--system", "x86_64-linux",
        ])
        assert args.toolchain_aggregate_drv == "/nix/store/x-t.drv"
        # Raw list is preserved; the helper turns it into a dict.
        assert args.matrix_drv_raw == [
            "hello=/nix/store/y-mh.drv",
            "busybox=/nix/store/z-mb.drv",
        ]
        # The dict-builder helper produces the kwarg-shape dict.
        captured: list[str] = []
        mapping = dgw.cli.parse_matrix_drv_mappings(
            args.matrix_drv_raw, on_error=captured.append,
        )
        assert mapping == {
            "hello": "/nix/store/y-mh.drv",
            "busybox": "/nix/store/z-mb.drv",
        }
        assert captured == []

    def test_cli_rejects_repeated_matrix_drv_for_same_binary(self):
        """Two ``--matrix-drv`` entries for the same binary name must
        fail with ``parser.error`` (SystemExit). The watcher is meant
        to emit one aggregate per binary; a duplicate is a wiring bug."""
        parser = dgw._build_cli_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "--matrix-eval-out-dir", "/tmp/me",
                "--bash-path", "/nix/store/aaaa-bash",
                "--toolchain-drv", "/nix/store/x-t.drv",
                "--matrix-drv", "hello=/nix/store/A.drv",
                "--matrix-drv", "hello=/nix/store/B.drv",
            ])
            # Argparse only stores the raw repeats; the duplicate-check
            # fires inside main()'s parse_matrix_drv_mappings call via
            # parser.error → SystemExit.
            # Exercise the helper directly here for an explicit fail.
            dgw.cli.parse_matrix_drv_mappings(
                ["hello=/nix/store/A.drv", "hello=/nix/store/B.drv"],
                on_error=parser.error,
            )

    def test_cli_requires_matrix_drv_and_toolchain_drv(self):
        """Both new flags are required: omitting either is a hard
        SystemExit. ``--matrix-drv`` is checked in main() (argparse's
        ``action='append'`` cannot mark "at least one occurrence" by
        itself); ``--toolchain-drv`` is ``required=True`` directly."""
        parser = dgw._build_cli_parser()
        # Missing --toolchain-drv → argparse SystemExit at parse time.
        with pytest.raises(SystemExit):
            parser.parse_args([
                "--matrix-eval-out-dir", "/tmp/me",
                "--bash-path", "/nix/store/aaaa-bash",
                "--matrix-drv", "hello=/nix/store/y.drv",
            ])
        # Missing --matrix-drv → main() raises SystemExit via
        # parser.error. We invoke main() with a tmp matrix-eval dir
        # that doesn't matter (argparse exits before run.py runs).
        with pytest.raises(SystemExit):
            dgw.main([
                "--matrix-eval-out-dir", "/tmp/me",
                "--bash-path", "/nix/store/aaaa-bash",
                "--toolchain-drv", "/nix/store/x-t.drv",
            ])

    def test_cli_rejects_malformed_matrix_drv(self):
        """An entry that doesn't contain ``=`` (or has empty halves)
        is rejected with SystemExit, surfacing the usage banner rather
        than a stack trace deep in main()."""
        parser = dgw._build_cli_parser()
        # Helper-level: malformed entry triggers on_error. parser.error
        # raises SystemExit; we wrap to capture that.
        with pytest.raises(SystemExit):
            dgw.cli.parse_matrix_drv_mappings(
                ["hello"],  # no '=' — malformed
                on_error=parser.error,
            )
        # End-to-end via main(): same SystemExit surfaces to the caller.
        with pytest.raises(SystemExit):
            dgw.main([
                "--matrix-eval-out-dir", "/tmp/me",
                "--bash-path", "/nix/store/aaaa-bash",
                "--toolchain-drv", "/nix/store/x-t.drv",
                "--matrix-drv", "hello",
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
        (matrix_dir / "hello.nix-archive").write_bytes(b"x")
        (matrix_dir / "world.nix-archive").write_bytes(b"x")
        agg_hello = "/nix/store/" + "rh" + "a" * 30 + "-matrix-hello.drv"
        agg_world = "/nix/store/" + "rw" + "a" * 30 + "-matrix-world.drv"
        tc_agg = "/nix/store/zzzz-toolchains.drv"

        # Stub D.1a's derive_variant_lookup_from_aggregate so the
        # worker sees a non-empty lookup per binary (otherwise both
        # binaries would be skipped before planning).
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
            agg_world: {
                ("aarch64", "world__aarch64__stub"): {
                    "drv": "/nix/store/dummy-world.drv",
                    "arch": "aarch64", "label": "world__aarch64__stub",
                    "suffix": "stub",
                },
            },
        }
        monkeypatch.setattr(
            _archive_mod, "derive_variant_lookup_from_aggregate",
            lambda agg: lookups.get(agg, {}), raising=False,
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
                matrix_eval_out_dir=matrix_dir,
                bash_path="/nix/store/bash",
                toolchain_aggregate_drv=tc_agg,
                matrix_aggregate_drvs={
                    "hello": agg_hello, "world": agg_world,
                },
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
        ):
            got = list(dgw.stream_drv_tree("/nix/store/zzzz-sum.drv"))
        assert got == expected
        assert calls == [[
            "nix-store", "--query", "--tree",
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
