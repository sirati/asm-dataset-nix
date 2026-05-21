"""Tests for placeholder_enumerator.

PH-B: submit-time build_variant placeholder enumeration. The module
turns a per-binary metadata dict + toolchain pairs + support table into
K-sized placeholder build_variant headers ready for the framework to
dispatch behind a per-binary dependency_graph__<binary> dependency.
"""

from __future__ import annotations

from compiler_suit_runner.placeholder_enumerator import (
    DEFAULT_PLACEHOLDER_K,
    build_variant_placeholder_name,
    build_variant_placeholder_task_id,
    compute_placeholder_k,
    enumerate_build_variant_placeholders,
    make_build_variant_placeholder_header,
)


class TestComputePlaceholderK:
    def test_variant_sample_zero_falls_back_to_default(self):
        assert compute_placeholder_k(0) == DEFAULT_PLACEHOLDER_K

    def test_variant_sample_none_falls_back_to_default(self):
        assert compute_placeholder_k(None) == DEFAULT_PLACEHOLDER_K

    def test_variant_sample_positive_wins(self):
        assert compute_placeholder_k(7) == 7
        assert compute_placeholder_k(150) == 150


class TestPlaceholderNamesAndIds:
    def test_task_id_shape(self):
        tid = build_variant_placeholder_task_id(
            "x86_64-linux", "hello", "gcc15", "armv7l", 3,
        )
        assert tid == (
            "build_variant__x86_64-linux__hello__gcc15__armv7l__slot3"
        )

    def test_name_shape_no_sys(self):
        name = build_variant_placeholder_name(
            "hello", "gcc15", "armv7l", 3,
        )
        assert name == "build_variant__hello__gcc15__armv7l__slot3"


class TestMakeBuildVariantPlaceholderHeader:
    def test_header_payload_and_metadata(self):
        h = make_build_variant_placeholder_header(
            binary="hello",
            sys_name="x86_64-linux",
            compiler="gcc15",
            arch="x86_64",
            slot_idx=5,
            manifest_path=(
                "/srv/asm/dataset/_matrix_eval/_manifests/"
                "hello__gcc15__x86_64.json"
            ),
        )
        assert h.item_class == "build_variant"
        assert h.name == "build_variant__hello__gcc15__x86_64__slot5"
        assert h.size == 0
        assert h.task_id == (
            "build_variant__x86_64-linux__hello__gcc15__x86_64__slot5"
        )
        assert h.task_depends_on == ("dependency_graph__hello",)
        assert h.payload["binary"] == "hello"
        assert h.payload["sys"] == "x86_64-linux"
        assert h.payload["compiler"] == "gcc15"
        assert h.payload["arch"] == "x86_64"
        assert h.payload["slot_idx"] == 5
        assert h.payload["placeholder"] is True
        assert h.payload["manifest_path"].endswith(
            "hello__gcc15__x86_64.json"
        )


class TestEnumerateBuildVariantPlaceholders:
    def _metadata(self, archs, variant_sample=None):
        return {
            "hello": {"archs": list(archs), "variant_sample": variant_sample},
        }

    def test_emits_k_per_supported_cell(self):
        meta = self._metadata(["x86_64", "aarch64"], variant_sample=3)
        pairs = [
            ("x86_64", "gcc15"),
            ("aarch64", "gcc15"),
            ("aarch64", "gcc4_6"),  # supported via table; below
        ]
        support = {
            ("gcc15", "aarch64"): "OK",
            ("gcc4_6", "aarch64"): "FAIL",
        }
        headers = list(
            enumerate_build_variant_placeholders(
                meta,
                sys_name="x86_64-linux",
                tc_pairs=pairs,
                support_table=support,
                matrix_eval_out_dir="/tmp/mev",
            )
        )
        # 3 slots × 2 supported cells = 6 (x86_64 native always OK;
        # aarch64+gcc15 OK; aarch64+gcc4_6 FAIL → dropped).
        assert len(headers) == 6
        # All are placeholders for binary=hello.
        for h in headers:
            assert h.payload["binary"] == "hello"
            assert h.payload["placeholder"] is True
            assert h.task_depends_on == ("dependency_graph__hello",)
        # Slot indices cover 0..k-1 within each cell.
        slot_indices_per_cell = {}
        for h in headers:
            key = (h.payload["compiler"], h.payload["arch"])
            slot_indices_per_cell.setdefault(key, set()).add(
                h.payload["slot_idx"]
            )
        assert slot_indices_per_cell == {
            ("gcc15", "x86_64"): {0, 1, 2},
            ("gcc15", "aarch64"): {0, 1, 2},
        }

    def test_arch_not_in_binary_skipped(self):
        meta = self._metadata(["x86_64"], variant_sample=2)
        pairs = [
            ("x86_64", "gcc15"),
            ("aarch64", "gcc15"),  # arch outside binary's set
        ]
        headers = list(
            enumerate_build_variant_placeholders(
                meta,
                sys_name="x86_64-linux",
                tc_pairs=pairs,
                support_table={},
                matrix_eval_out_dir="/tmp/mev",
            )
        )
        assert len(headers) == 2  # only x86_64 emits
        assert all(h.payload["arch"] == "x86_64" for h in headers)

    def test_empty_archs_yields_nothing(self):
        meta = {"hello": {"archs": [], "variant_sample": 5}}
        headers = list(
            enumerate_build_variant_placeholders(
                meta,
                sys_name="x86_64-linux",
                tc_pairs=[("x86_64", "gcc15")],
                support_table={},
                matrix_eval_out_dir="/tmp/mev",
            )
        )
        assert headers == []

    def test_manifest_path_format(self):
        meta = self._metadata(["x86_64"], variant_sample=1)
        headers = list(
            enumerate_build_variant_placeholders(
                meta,
                sys_name="x86_64-linux",
                tc_pairs=[("x86_64", "gcc15")],
                support_table={},
                matrix_eval_out_dir="/srv/mev",
            )
        )
        assert len(headers) == 1
        assert headers[0].payload["manifest_path"] == (
            "/srv/mev/_manifests/hello__gcc15__x86_64.json"
        )

    def test_falls_back_to_default_k_when_no_variant_sample(self):
        meta = self._metadata(["x86_64"], variant_sample=None)
        headers = list(
            enumerate_build_variant_placeholders(
                meta,
                sys_name="x86_64-linux",
                tc_pairs=[("x86_64", "gcc15")],
                support_table={},
                matrix_eval_out_dir="/tmp/mev",
            )
        )
        assert len(headers) == DEFAULT_PLACEHOLDER_K
