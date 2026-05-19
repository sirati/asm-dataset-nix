"""Unit tests for :mod:`compiler_suit_runner.workers.dependency_graph_worker`.

All nix invocations are stubbed; the streaming planner and sum-drv
builder are monkey-patched so the tests stay dependency-light (no
template_graph eval, no /nix/store).

Coverage:

  * archive discovery (sorted, file-only, suffix filter);
  * sidecar JSON reader (present / missing / malformed);
  * matrix_eval header reader (defensive secondary discovery);
  * kept-drv discovery precedence (sidecar > header > empty + warn);
  * import_archive happy + missing-file + subprocess-failure paths;
  * write_dependency_graph_json roundtrip (dataclass + dict descriptors);
  * --toolchain-task-id parser;
  * end-to-end run_dependency_graph_task with stubbed planner +
    sum-drv builder + subprocess.
"""

from __future__ import annotations

import dataclasses
import json
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
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.path_info_present: set[str] = set()
        self.path_info_rc: Optional[int] = None
        self.import_rc = 0
        self.import_stderr = b""
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
            return b"", self.import_stderr, self.import_rc
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
# Kept-drv discovery (sidecar + header)
# ---------------------------------------------------------------------------


class TestKeptDrvDiscovery:

    def test_sidecar_variant_drvs(self, tmp_path: pathlib.Path):
        archive = tmp_path / "hello.nix-archive"
        archive.write_bytes(b"")
        sidecar = archive.with_suffix(".nix-archive.json")
        sidecar.write_text(
            json.dumps({"variant_drvs": [
                "/nix/store/aaa-hello-O0.drv",
                "/nix/store/bbb-hello-O2.drv",
            ]}),
            encoding="utf-8",
        )
        drvs, lookup = dgw.discover_kept_drvs(archive, None)
        assert drvs == [
            "/nix/store/aaa-hello-O0.drv",
            "/nix/store/bbb-hello-O2.drv",
        ]
        assert lookup == {}

    def test_sidecar_with_variant_lookup(self, tmp_path: pathlib.Path):
        """Sidecar with full ``variants`` list populates variant_lookup."""
        archive = tmp_path / "hello.nix-archive"
        archive.write_bytes(b"")
        sidecar = archive.with_suffix(".nix-archive.json")
        sidecar.write_text(
            json.dumps({"variants": [
                {"label": "gcc15-O0", "arch": "x86_64",
                 "drv": "/nix/store/aaa-hello-O0.drv",
                 "compiler_id": "gcc15", "optimization": "O0"},
                {"label": "gcc15-O2", "arch": "x86_64",
                 "drv": "/nix/store/bbb-hello-O2.drv"},
            ]}),
            encoding="utf-8",
        )
        drvs, lookup = dgw.discover_kept_drvs(archive, None)
        assert set(drvs) == {
            "/nix/store/aaa-hello-O0.drv",
            "/nix/store/bbb-hello-O2.drv",
        }
        assert lookup[("x86_64", "gcc15-O0")]["compiler_id"] == "gcc15"
        assert lookup[("x86_64", "gcc15-O2")]["drv"].endswith("O2.drv")

    def test_matrix_eval_header_post_b1a(self, tmp_path: pathlib.Path):
        archive = tmp_path / "hello.nix-archive"
        archive.write_bytes(b"")
        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()
        header = {
            "item_class": "matrix_eval",
            "name": "matrix_eval__hello",
            "size": 0,
            "payload": {
                "binary": "hello",
                "variant_drvs": [
                    "/nix/store/aaa-hello-O0.drv",
                ],
            },
        }
        (manifest_dir / "matrix_eval__hello.json").write_text(
            json.dumps(header), encoding="utf-8",
        )
        drvs, _lookup = dgw.discover_kept_drvs(archive, manifest_dir)
        assert drvs == ["/nix/store/aaa-hello-O0.drv"]

    def test_legacy_phase0_eval_header_ignored(
        self, tmp_path: pathlib.Path,
    ):
        """Per A6's hard cutover, a leftover legacy
        ``phase0_eval__<binary>.json`` is NOT consulted — only the new
        ``matrix_eval__<binary>.json`` name is recognised. Operators
        with pre-rename run state on disk must re-issue under the new
        run_id namespace."""
        archive = tmp_path / "hello.nix-archive"
        archive.write_bytes(b"")
        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()
        header = {
            "item_class": "phase0_eval",
            "name": "phase0_eval__hello",
            "size": 0,
            "payload": {
                "variant_drvs": ["/nix/store/legacy-hello.drv"],
            },
        }
        (manifest_dir / "phase0_eval__hello.json").write_text(
            json.dumps(header), encoding="utf-8",
        )
        drvs, _lookup = dgw.discover_kept_drvs(archive, manifest_dir)
        assert drvs == []

    def test_no_source_returns_empty(
        self, tmp_path: pathlib.Path, caplog,
    ):
        archive = tmp_path / "hello.nix-archive"
        archive.write_bytes(b"")
        drvs, lookup = dgw.discover_kept_drvs(archive, None)
        assert drvs == []
        assert lookup == {}

    def test_sidecar_preferred_over_header(
        self, tmp_path: pathlib.Path,
    ):
        """Sidecar wins when both are present."""
        archive = tmp_path / "hello.nix-archive"
        archive.write_bytes(b"")
        sidecar = archive.with_suffix(".nix-archive.json")
        sidecar.write_text(
            json.dumps({"variant_drvs": ["/nix/store/aaa-sidecar.drv"]}),
            encoding="utf-8",
        )
        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()
        (manifest_dir / "matrix_eval__hello.json").write_text(
            json.dumps({
                "payload": {
                    "variant_drvs": ["/nix/store/bbb-header.drv"],
                },
            }),
            encoding="utf-8",
        )
        drvs, _ = dgw.discover_kept_drvs(archive, manifest_dir)
        assert drvs == ["/nix/store/aaa-sidecar.drv"]

    def test_invalid_drv_entries_dropped(self, tmp_path: pathlib.Path):
        """Non-string / non-.drv entries are filtered."""
        archive = tmp_path / "hello.nix-archive"
        archive.write_bytes(b"")
        sidecar = archive.with_suffix(".nix-archive.json")
        sidecar.write_text(
            json.dumps({"variant_drvs": [
                "/nix/store/aaa-hello.drv",
                "/nix/store/bbb-no-suffix",  # dropped
                123,                          # dropped
                None,                         # dropped
            ]}),
            encoding="utf-8",
        )
        drvs, _ = dgw.discover_kept_drvs(archive, None)
        assert drvs == ["/nix/store/aaa-hello.drv"]


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
        ok, err = dgw.import_archive(archive, run_subprocess=stub)
        assert ok is False
        assert b"archive not found" in err
        # No subprocess invocation — short-circuit on missing file.
        assert stub.calls == []

    def test_present_archive_success(self, tmp_path: pathlib.Path):
        archive = tmp_path / "x.nix-archive"
        archive.write_bytes(b"fake-archive-bytes")
        stub = _SubprocessStub()
        stub.import_rc = 0
        ok, err = dgw.import_archive(archive, run_subprocess=stub)
        assert ok is True
        assert err == b""
        # Subprocess was invoked with --import.
        assert any(c[:2] == ["nix-store", "--import"] for c in stub.calls)

    def test_subprocess_failure(self, tmp_path: pathlib.Path):
        archive = tmp_path / "x.nix-archive"
        archive.write_bytes(b"junk")
        stub = _SubprocessStub()
        stub.import_rc = 1
        stub.import_stderr = b"corrupt archive"
        ok, err = dgw.import_archive(archive, run_subprocess=stub)
        assert ok is False
        assert b"corrupt" in err


# ---------------------------------------------------------------------------
# write_dependency_graph_json
# ---------------------------------------------------------------------------


class TestWriteDependencyGraphJson:

    def test_dataclass_descriptors_roundtrip(
        self, tmp_path: pathlib.Path,
    ):
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
        out = dgw.write_dependency_graph_json(
            tmp_path / "_dependency_graph.json", descriptors,
        )
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert "phase4_descriptors" in loaded
        descs = loaded["phase4_descriptors"]
        assert len(descs) == 2
        assert descs[0]["kind"] == "build_common_dep"
        assert descs[1]["depends_on"] == ["cd_1"]  # tuple → list in JSON

    def test_dict_descriptors_roundtrip(self, tmp_path: pathlib.Path):
        out = dgw.write_dependency_graph_json(
            tmp_path / "out.json",
            [{"kind": "x", "task_id": "y", "depends_on": ()}],
        )
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded["phase4_descriptors"][0]["kind"] == "x"

    def test_empty_list_writes_valid_file(self, tmp_path: pathlib.Path):
        out = dgw.write_dependency_graph_json(
            tmp_path / "out.json", [],
        )
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded == {"phase4_descriptors": []}


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
        kept_drvs: list[str],
    ) -> pathlib.Path:
        archive = matrix_dir / f"{binary}.nix-archive"
        archive.write_bytes(b"fake")
        sidecar = archive.with_suffix(".nix-archive.json")
        sidecar.write_text(
            json.dumps({"variant_drvs": kept_drvs}),
            encoding="utf-8",
        )
        return archive

    def test_empty_matrix_dir_writes_empty_graph(
        self, tmp_path: pathlib.Path,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        result = dgw.run_dependency_graph_task(
            matrix_eval_out_dir=matrix_dir,
            manifest_dir=None,
            bash_path="/nix/store/aaaa-bash",
            toolchain_drvs=["/nix/store/zzzz-gcc15.drv"],
        )
        assert result.binary_count == 0
        assert result.descriptor_count == 0
        loaded = json.loads(result.output_path.read_text(encoding="utf-8"))
        assert loaded == {"phase4_descriptors": []}

    def test_end_to_end_single_binary(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(
            matrix_dir, "hello",
            ["/nix/store/aaa-hello-O0.drv", "/nix/store/bbb-hello-O2.drv"],
        )

        stub = _SubprocessStub()
        # No paths present locally → import must run.
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
            manifest_dir=None,
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
            "matrix-hello": [
                "/nix/store/aaa-hello-O0.drv",
                "/nix/store/bbb-hello-O2.drv",
            ],
        }
        # Planner saw the toolchain task ids and the binary list.
        assert plan_calls[0]["toolchain_task_ids"] == {
            "zzzz-gcc15.drv": "build_compilers__aarch64__gcc15",
        }
        assert plan_calls[0]["binaries"] == ["hello"]
        # nix-store --import was invoked (paths not locally present).
        assert any(c[:2] == ["nix-store", "--import"] for c in stub.calls)
        # _dependency_graph.json was written.
        loaded = json.loads(result.output_path.read_text(encoding="utf-8"))
        assert len(loaded["phase4_descriptors"]) == 1
        assert loaded["phase4_descriptors"][0]["task_id"] == "bv_hello"

    def test_skip_import_when_all_locally_present(
        self, tmp_path: pathlib.Path, monkeypatch,
    ):
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        self._seed_archive(
            matrix_dir, "hello", ["/nix/store/aaa-hello.drv"],
        )

        stub = _SubprocessStub()
        stub.path_info_present.add("/nix/store/aaa-hello.drv")
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
            manifest_dir=None,
            bash_path="/nix/store/bash",
            toolchain_drvs=["/nix/store/tc.drv"],
            run_subprocess=stub,
        )
        # No --import call because all drvs were already present.
        assert not any(
            c[:2] == ["nix-store", "--import"] for c in stub.calls
        )

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
                manifest_dir=None,
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
        self._seed_archive(
            matrix_dir, "hello", ["/nix/store/aaa-hello.drv"],
        )
        stub = _SubprocessStub()
        stub.path_info_present.add("/nix/store/aaa-hello.drv")
        stub.tree_rc = 1

        monkeypatch.setattr(
            dgw, "build_sum_drv_multi",
            lambda **kw: "/nix/store/sum.drv",
        )
        with pytest.raises(dgw.DependencyGraphWorkerError) as excinfo:
            dgw.run_dependency_graph_task(
                matrix_eval_out_dir=matrix_dir,
                manifest_dir=None,
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
        self._seed_archive(matrix_dir, "hello", ["/nix/store/h-hello.drv"])
        self._seed_archive(matrix_dir, "world", ["/nix/store/w-world.drv"])

        stub = _SubprocessStub()
        stub.path_info_present.update(
            ["/nix/store/h-hello.drv", "/nix/store/w-world.drv"]
        )
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
            manifest_dir=None,
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
        """An archive with neither sidecar nor header → skip the binary
        and proceed with the rest."""
        matrix_dir = tmp_path / "_matrix_eval"
        matrix_dir.mkdir()
        # ``hello`` has no sidecar / header → skipped.
        (matrix_dir / "hello.nix-archive").write_bytes(b"x")
        # ``world`` has a sidecar.
        self._seed_archive(
            matrix_dir, "world", ["/nix/store/world.drv"],
        )

        stub = _SubprocessStub()
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
            manifest_dir=None,
            bash_path="/nix/store/bash",
            toolchain_drvs=["/nix/store/tc.drv"],
            run_subprocess=stub,
        )
        # Only world was planned.
        assert result.binary_count == 1
        loaded = json.loads(result.output_path.read_text(encoding="utf-8"))
        assert {d["task_id"] for d in loaded["phase4_descriptors"]} == {
            "bv_world",
        }


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
            "--manifest-dir", "/tmp/manifests",
            "--sys-name", "aarch64-linux",
        ])
        assert args.matrix_eval_out_dir == "/tmp/me"
        assert args.toolchain_drv == [
            "/nix/store/tc1.drv", "/nix/store/tc2.drv",
        ]
        assert args.toolchain_task_id == ["tc1.drv=task1"]
        assert args.sys_name == "aarch64-linux"
