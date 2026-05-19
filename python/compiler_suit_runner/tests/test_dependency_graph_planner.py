"""Unit tests for :mod:`compiler_suit_runner.dependency_graph_planner`.

The tests fabricate ``plan_from_tree_streaming``-shaped dicts directly
so they have no dependency on ``template_graph`` itself — the adapter
contract is "consume the streaming output's structural shape" and
that contract is what we exercise here.

Each test focuses on one slice of the adapter's responsibilities:

  * single-variant binary (no common-dep dedup);
  * multi-variant binary with one common-dep node shared across them;
  * toolchain ``set[(hash, name)]`` → ``set[str]`` shape translation;
  * cycle detection in a fabricated template;
  * empty input (no variants at all).
"""

from __future__ import annotations

import pytest

from compiler_suit_runner.dependency_graph_planner import (
    BinaryPlanInput,
    DependencyGraphCycleError,
    Phase4Descriptor,
    convert_toolchain_drvs,
    plan_phase4_for_binary,
    plan_phase4_from_graph,
    variant_label_key,
)


# ---------------------------------------------------------------------------
# Fabrication helpers
# ---------------------------------------------------------------------------


def _node(name: str, child_ids: list[int], *, is_toolchain: bool = False) -> dict:
    """Shape-mirror of ``template_graph.core.TemplateNode``."""
    return {
        "name": name,
        "child_ids": child_ids,
        "is_toolchain": is_toolchain,
    }


def _template(nodes: list[dict], root_id: int = 0) -> dict:
    """Shape-mirror of ``template_graph.core.Template``."""
    return {
        "nodes": nodes,
        "name_to_id": {n["name"]: i for i, n in enumerate(nodes)},
        "root_id": root_id,
        "template_built_from": [],
    }


def _variant_array(
    template_id: int,
    arch: str,
    variants: list[str],
    hashes: list[list],
) -> dict:
    """Shape-mirror of ``template_graph.core.VariantArray``."""
    return {
        "template_id": template_id,
        "arch": arch,
        "variants": variants,
        "hashes": hashes,
    }


def _variant_spec(label: str, pkg: str, arch: str, **extra) -> dict:
    """Minimal VariantSpec-like dict matching what the matrix
    enumerator emits."""
    base = {
        "label": label,
        "pkg": pkg,
        "arch": arch,
        "drv": f"/nix/store/aaaa-{pkg}-{arch}-{label}.drv",
        "variant_dir": f"{label}_dir",
        "metadata_name": f"{label}.json",
        "compiler_id": label.rsplit("-", 1)[0] if "-" in label else label,
        "compiler_family": "gcc",
        "compiler_version": "15",
        "optimization": label.rsplit("-", 1)[1] if "-" in label else "",
        "flag_set": "baseline",
        "hardening": "default",
        "sanitizer": "off",
        "march": "default",
        "tier": 1,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# convert_toolchain_drvs (shape translation)
# ---------------------------------------------------------------------------


class TestConvertToolchainDrvs:

    def test_tuple_set_to_string_set(self):
        """Streaming planner's native ``set[(hash, name)]`` → ``set[str]``."""
        raw = {
            ("abc123", "gcc-wrapper-15.drv"),
            ("def456", "clang-wrapper-19.drv"),
        }
        out = convert_toolchain_drvs(raw)
        assert out == {
            "abc123-gcc-wrapper-15.drv",
            "def456-clang-wrapper-19.drv",
        }

    def test_list_form_from_json_roundtrip(self):
        """JSON-serialised tuples come back as lists; still parseable."""
        raw = [["hash1", "name1.drv"], ["hash2", "name2.drv"]]
        out = convert_toolchain_drvs(raw)
        assert out == {"hash1-name1.drv", "hash2-name2.drv"}

    def test_already_string_form_passes_through(self):
        """Legacy callers that already have ``"<hash>-<name>"`` strings
        keep working."""
        out = convert_toolchain_drvs({"xx-foo.drv", "yy-bar.drv"})
        assert out == {"xx-foo.drv", "yy-bar.drv"}

    def test_mixed_input(self):
        out = convert_toolchain_drvs([
            ("h1", "n1.drv"),
            ["h2", "n2.drv"],
            "h3-n3.drv",
        ])
        assert out == {"h1-n1.drv", "h2-n2.drv", "h3-n3.drv"}

    def test_malformed_entries_dropped(self):
        """Garbage in the toolchain set must NOT crash conversion."""
        out = convert_toolchain_drvs([
            ("h1", "n1.drv"),
            None,
            42,
            ("only_one_field",),
            ["too", "many", "fields"],
            "no_dash_form",
        ])
        assert out == {"h1-n1.drv"}

    def test_empty_input(self):
        assert convert_toolchain_drvs(set()) == set()
        assert convert_toolchain_drvs([]) == set()


# ---------------------------------------------------------------------------
# variant_label_key (caller convenience)
# ---------------------------------------------------------------------------


class TestVariantLabelKey:

    def test_strips_hash_and_drv_suffix(self):
        # Nix store hashes are 32 chars; only then do we strip.
        path = "/nix/store/" + "a" * 32 + "-hello-x86_64-gcc15-O2.drv"
        assert variant_label_key(path) == "hello-x86_64-gcc15-O2"

    def test_bare_name_passes_through(self):
        assert variant_label_key("hello-O2") == "hello-O2"

    def test_drv_suffix_only(self):
        assert variant_label_key("hello-O2.drv") == "hello-O2"


# ---------------------------------------------------------------------------
# Single-variant binary (no common-dep dedup expected)
# ---------------------------------------------------------------------------


class TestSingleVariantBinary:
    """Single-variant arch has nothing to dedupe; we still emit a
    variant descriptor but zero common-dep descriptors.

    NB: the streaming planner's `_classify_pair` marks every node as
    ``common_dep`` for single-variant cells (trivially constant). We
    therefore expect common-dep descriptors equal to the count of
    non-toolchain nodes in the template — exercising the
    "single-variant binary" code path means asserting on that.
    """

    def test_emits_one_variant_descriptor(self):
        template = _template([
            _node("hello-root", [1]),
            _node("glibc", []),
        ])
        arr = _variant_array(
            template_id=0,
            arch="x86_64",
            variants=["gcc15-O2"],
            hashes=[
                [("rooth", "hello.drv")],
                [("glibch", "glibc.drv")],
            ],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "common_dep", 1: "common_dep"},
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec("gcc15-O2", "hello", "x86_64"),
        }
        descs = plan_phase4_for_binary("hello", streaming, variant_lookup)
        variants = [d for d in descs if d.kind == "build_variant"]
        common = [d for d in descs if d.kind == "build_common_dep"]
        assert len(variants) == 1
        # Both nodes were classified common_dep; both become common_dep
        # descriptors. The variant depends on both.
        assert len(common) == 2
        assert variants[0].payload["label"] == "gcc15-O2"
        assert variants[0].payload["pkg"] == "hello"
        assert set(variants[0].depends_on) == {c.task_id for c in common}


# ---------------------------------------------------------------------------
# Multi-variant binary with common-dep dedup
# ---------------------------------------------------------------------------


class TestMultiVariantCommonDep:
    """Two variants of one (binary, arch) share a common dep — the
    planner should emit exactly one ``build_common_dep`` descriptor
    and both variants must depend on it."""

    def _build(self) -> tuple[dict, dict[tuple[str, str], dict]]:
        # Template: root → [shared_lib, per_variant_obj]
        # node 0 = root (variant_specific — root drv differs per variant)
        # node 1 = shared_lib (common_dep — same hash across both variants)
        # node 2 = per_variant_obj (variant_specific — different hash per variant)
        template = _template([
            _node("hello-root", [1, 2]),
            _node("shared-lib", []),
            _node("hello-obj", []),
        ])
        arr = _variant_array(
            template_id=0,
            arch="x86_64",
            variants=["gcc15-O0", "gcc15-O2"],
            hashes=[
                # root differs per variant
                [("rO0h", "hello-O0.drv"), ("rO2h", "hello-O2.drv")],
                # shared-lib: same hash in both
                [("shareh", "shared-lib.drv"), ("shareh", "shared-lib.drv")],
                # obj differs per variant
                [("objO0h", "hello-O0-obj.drv"), ("objO2h", "hello-O2-obj.drv")],
            ],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {
                    0: "variant_specific",
                    1: "common_dep",
                    2: "variant_specific",
                },
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O0"): _variant_spec("gcc15-O0", "hello", "x86_64"),
            ("x86_64", "gcc15-O2"): _variant_spec("gcc15-O2", "hello", "x86_64"),
        }
        return streaming, variant_lookup

    def test_one_common_dep_two_variants(self):
        streaming, lookup = self._build()
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        variants = [d for d in descs if d.kind == "build_variant"]
        common = [d for d in descs if d.kind == "build_common_dep"]
        assert len(common) == 1, [d.task_id for d in common]
        assert len(variants) == 2
        assert common[0].payload["node_name"] == "shared-lib"
        # The common_dep ident must be the (hash, name) we stored.
        assert common[0].payload["ident"] == "shareh-shared-lib.drv"
        # Both variants depend on that one common-dep task_id.
        cd_id = common[0].task_id
        for v in variants:
            assert cd_id in v.depends_on, v

    def test_ordering_is_deterministic(self):
        """Two consecutive plan calls must produce identical descriptor
        sequences (descriptor equality is structural via dataclass)."""
        streaming, lookup = self._build()
        a = plan_phase4_for_binary("hello", streaming, lookup)
        b = plan_phase4_for_binary("hello", streaming, lookup)
        assert a == b
        # Common-deps come first, then variants (matches plan ordering).
        kinds = [d.kind for d in a]
        assert kinds == ["build_common_dep", "build_variant", "build_variant"]


# ---------------------------------------------------------------------------
# Toolchain ident wiring (set[(hash, name)] → variant depends_on)
# ---------------------------------------------------------------------------


class TestToolchainWiring:
    """A template with a toolchain node listed in
    ``toolchain_node_ids_per_template`` is wired by matching the
    TemplateNode's role-name to entries in ``toolchain_drvs`` and
    looking those idents up in ``toolchain_task_ids``.

    The cowalk short-circuits toolchain subtrees so ``arr.hashes`` rows
    at toolchain node_ids are empty; the role-name → ident join is
    the canonical wiring path (E6 in the dependency-graph plan)."""

    def test_toolchain_task_id_wired_into_variant(self):
        template = _template([
            _node("variant-root", [1]),
            # Toolchain marker — the cowalk records the node but
            # short-circuits the subtree, so ``arr.hashes`` carries
            # no ident at this position. The wiring uses the role
            # name + ``toolchain_drvs`` instead.
            _node("gcc-wrapper-15.drv", [], is_toolchain=True),
        ])
        arr = _variant_array(
            template_id=0,
            arch="x86_64",
            variants=["gcc15-O0"],
            hashes=[
                [("rooth", "hello.drv")],
                # Toolchain row left empty by the cowalk
                # short-circuit; wiring resolves it via the name
                # lookup below.
                [],
            ],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "common_dep"},
            },
            "toolchain_drvs": {("ccwh", "gcc-wrapper-15.drv")},
            "toolchain_node_ids_per_template": {0: [1]},
            "arch_indep_deps": {},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O0"): _variant_spec("gcc15-O0", "hello", "x86_64"),
        }
        toolchain_task_ids = {"ccwh-gcc-wrapper-15.drv": "build_compilers__x86_64__gcc15"}
        descs = plan_phase4_for_binary(
            "hello",
            streaming,
            variant_lookup,
            toolchain_task_ids=toolchain_task_ids,
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 1
        assert "build_compilers__x86_64__gcc15" in variants[0].depends_on

    def test_unknown_toolchain_ident_is_skipped(self):
        """A toolchain ident that's not in the task-id map is silently
        omitted from depends_on. Operator-provided toolchains (which
        the framework didn't build) legitimately have no task_id —
        their absence must NOT produce a phantom dep."""
        template = _template([
            _node("root", [1]),
            _node("cc-wrapper.drv", [], is_toolchain=True),
        ])
        arr = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O0"],
            hashes=[
                [("rh", "hello.drv")],
                [],
            ],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "common_dep"},
            },
            "toolchain_drvs": {("unknownh", "cc-wrapper.drv")},
            "toolchain_node_ids_per_template": {0: [1]},
            "arch_indep_deps": {},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O0"): _variant_spec("gcc15-O0", "hello", "x86_64"),
        }
        descs = plan_phase4_for_binary(
            "hello", streaming, variant_lookup,
            toolchain_task_ids={},
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 1
        # No toolchain mapping known → no toolchain dep in variant.
        # Common-dep deps from the root may still be present.
        assert all(not dep.startswith("build_compilers__") for dep in variants[0].depends_on)

    def test_unified_wrapper_role_wires_all_matching_compilers(self):
        """A toolchain node whose role unifies multiple compiler
        versions (e.g. ``wrapped-compiler-suit.drv``) must wire ALL
        matching ``toolchain_drvs`` idents into every variant's
        ``depends_on``. Per-variant compiler selection is not visible
        in the role-collapsed template, so over-wiring is the safe
        choice (the variant waits on extra ``build_compilers__*``
        tasks that would have been built regardless)."""
        template = _template([
            _node("hello-root", [1]),
            _node(
                "wrapped-compiler-suit.drv", [], is_toolchain=True,
            ),
        ])
        arr = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc14-O2", "gcc15-O2"],
            hashes=[
                [("rO2g14", "hello-O2.drv"), ("rO2g15", "hello-O2.drv")],
                [[], []],
            ],
        )
        # ``toolchain_drvs`` carries the post-hash drv name for each
        # underlying compiler wrapper.
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "common_dep"},
            },
            "toolchain_drvs": {
                ("g14h", "wrapped-compiler-suit.drv"),
                ("g15h", "wrapped-compiler-suit.drv"),
            },
            "toolchain_node_ids_per_template": {0: [1]},
            "arch_indep_deps": {},
        }
        variant_lookup = {
            ("x86_64", "gcc14-O2"): _variant_spec("gcc14-O2", "hello", "x86_64"),
            ("x86_64", "gcc15-O2"): _variant_spec("gcc15-O2", "hello", "x86_64"),
        }
        toolchain_task_ids = {
            "g14h-wrapped-compiler-suit.drv": "build_compilers__x86_64__gcc14",
            "g15h-wrapped-compiler-suit.drv": "build_compilers__x86_64__gcc15",
        }
        descs = plan_phase4_for_binary(
            "hello", streaming, variant_lookup,
            toolchain_task_ids=toolchain_task_ids,
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 2
        for v in variants:
            assert "build_compilers__x86_64__gcc14" in v.depends_on, v
            assert "build_compilers__x86_64__gcc15" in v.depends_on, v

    def test_no_toolchain_node_map_no_wiring(self):
        """Without ``toolchain_node_ids_per_template`` in the streaming
        snapshot, toolchain wiring is a no-op even if ``is_toolchain``
        is set on a node — the new wiring path is the SOLE source of
        truth (legacy ``arr.hashes`` scrape was removed). Existing
        common-dep wiring must still fire."""
        template = _template([
            _node("hello-root", [1]),
            _node("gcc-wrapper-15.drv", [], is_toolchain=True),
        ])
        arr = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O0"],
            hashes=[[("rh", "hello.drv")], []],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "common_dep"},
            },
            "toolchain_drvs": {("ccwh", "gcc-wrapper-15.drv")},
            # toolchain_node_ids_per_template intentionally absent.
            "arch_indep_deps": {},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O0"): _variant_spec("gcc15-O0", "hello", "x86_64"),
        }
        toolchain_task_ids = {
            "ccwh-gcc-wrapper-15.drv": "build_compilers__x86_64__gcc15",
        }
        descs = plan_phase4_for_binary(
            "hello", streaming, variant_lookup,
            toolchain_task_ids=toolchain_task_ids,
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 1
        assert "build_compilers__x86_64__gcc15" not in variants[0].depends_on


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    """The DAG-cycle guard is purely defensive; we exercise it with a
    fabricated cycle that nix would never produce."""

    def test_self_loop_raises(self):
        template = _template([
            _node("self-loop", [0]),  # child id points back to itself
        ])
        streaming = {
            "templates": [template],
            "variant_arrays": {},
            "common_deps_per_arch_template": {},
        }
        with pytest.raises(DependencyGraphCycleError):
            plan_phase4_for_binary("anything", streaming, {})

    def test_two_node_cycle_raises(self):
        # 0 -> 1 -> 0
        template = _template([
            _node("a", [1]),
            _node("b", [0]),
        ])
        streaming = {
            "templates": [template],
            "variant_arrays": {},
            "common_deps_per_arch_template": {},
        }
        with pytest.raises(DependencyGraphCycleError):
            plan_phase4_for_binary("anything", streaming, {})

    def test_three_node_cycle_via_back_edge(self):
        # 0 -> 1 -> 2 -> 1 (back-edge into still-GREY node)
        template = _template([
            _node("a", [1]),
            _node("b", [2]),
            _node("c", [1]),
        ])
        streaming = {
            "templates": [template],
            "variant_arrays": {},
            "common_deps_per_arch_template": {},
        }
        with pytest.raises(DependencyGraphCycleError):
            plan_phase4_for_binary("anything", streaming, {})

    def test_diamond_is_not_a_cycle(self):
        """Two paths from root to one shared leaf is a valid DAG; the
        guard must NOT misfire on cross-edges into already-BLACK nodes."""
        # 0 -> [1, 2]; 1 -> [3]; 2 -> [3]
        template = _template([
            _node("root", [1, 2]),
            _node("left", [3]),
            _node("right", [3]),
            _node("leaf", []),
        ])
        streaming = {
            "templates": [template],
            "variant_arrays": {},
            "common_deps_per_arch_template": {},
        }
        # Should not raise.
        plan_phase4_for_binary("anything", streaming, {})


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


class TestEmptyInput:

    def test_completely_empty_streaming_result(self):
        descs = plan_phase4_for_binary("hello", {
            "templates": [],
            "variant_arrays": {},
            "common_deps_per_arch_template": {},
        }, {})
        assert descs == []

    def test_template_with_no_variant_array(self):
        """A template exists but no variant array references it — no
        descriptors emitted."""
        template = _template([_node("root", [])])
        descs = plan_phase4_for_binary("hello", {
            "templates": [template],
            "variant_arrays": {},
            "common_deps_per_arch_template": {},
        }, {})
        assert descs == []

    def test_variant_array_with_unknown_labels(self):
        """The variant_lookup is missing entries for the array's labels
        — those variants are silently dropped."""
        template = _template([_node("root", [])])
        arr = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O0"],
            hashes=[[("rh", "hello.drv")]],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "common_dep"},
            },
        }
        descs = plan_phase4_for_binary("hello", streaming, {})
        # No variant_lookup → no variant descriptors. The common-dep
        # still emits because it's variant-independent.
        kinds = [d.kind for d in descs]
        assert "build_variant" not in kinds
        assert kinds.count("build_common_dep") == 1


# ---------------------------------------------------------------------------
# Multi-binary aggregation entry point
# ---------------------------------------------------------------------------


class TestPlanFromGraphMultiBinary:

    def test_two_binaries_variants_in_name_order(self):
        def _make(binary: str):
            template = _template([_node(f"{binary}-root", [])])
            arr = _variant_array(
                0, "x86_64", [f"gcc15-{binary}"],
                [[(f"rh-{binary}", f"{binary}.drv")]],
            )
            return {
                "templates": [template],
                "variant_arrays": {(0, "x86_64"): arr},
                "common_deps_per_arch_template": {
                    (0, "x86_64"): {0: "common_dep"},
                },
            }, {
                ("x86_64", f"gcc15-{binary}"): _variant_spec(
                    f"gcc15-{binary}", binary, "x86_64"
                ),
            }

        sb, slookup = _make("busybox")
        hb, hlookup = _make("hello")
        # Pass in NON-alphabetical input order; expect alphabetical output.
        descs = plan_phase4_from_graph([
            BinaryPlanInput("hello", hb, hlookup),
            BinaryPlanInput("busybox", sb, slookup),
        ])
        # Common-deps come first, deduped across binaries and sorted by
        # task_id; variants come after, in binary-name order.
        variants = [d for d in descs if d.kind == "build_variant"]
        variant_binaries = [v.payload.get("pkg") for v in variants]
        busybox_idx = [i for i, b in enumerate(variant_binaries) if b == "busybox"]
        hello_idx = [i for i, b in enumerate(variant_binaries) if b == "hello"]
        assert busybox_idx, descs
        assert hello_idx, descs
        assert max(busybox_idx) < min(hello_idx)

    def test_shared_common_dep_collapses_across_binaries(self):
        """Two binaries whose templates both touch the SAME shared
        sub-drv (same ident) must produce ONE build_common_dep
        descriptor, and BOTH binaries' variants must depend on its
        task_id.
        """
        # Both binaries' templates have a node 1 classified as
        # common_dep with ident ``shareh-libshared.drv`` — that single
        # ident is the dedup pivot.
        def _make(binary: str):
            template = _template([
                _node(f"{binary}-root", [1]),
                _node("libshared", []),
            ])
            arr = _variant_array(
                template_id=0, arch="x86_64",
                variants=[f"gcc15-{binary}"],
                hashes=[
                    [(f"rooth-{binary}", f"{binary}.drv")],
                    [("shareh", "libshared.drv")],
                ],
            )
            return {
                "templates": [template],
                "variant_arrays": {(0, "x86_64"): arr},
                "common_deps_per_arch_template": {
                    (0, "x86_64"): {
                        0: "variant_specific",
                        1: "common_dep",
                    },
                },
            }, {
                ("x86_64", f"gcc15-{binary}"): _variant_spec(
                    f"gcc15-{binary}", binary, "x86_64",
                ),
            }

        hb, hlookup = _make("hello")
        sb, slookup = _make("busybox")
        descs = plan_phase4_from_graph([
            BinaryPlanInput("hello", hb, hlookup),
            BinaryPlanInput("busybox", sb, slookup),
        ])
        common = [d for d in descs if d.kind == "build_common_dep"]
        variants = [d for d in descs if d.kind == "build_variant"]
        # ONE descriptor for the shared sub-drv even though both
        # binaries' templates carry the same node.
        assert len(common) == 1, [d.task_id for d in common]
        cd_id = common[0].task_id
        assert cd_id == "build_common_dep__shareh-libshared.drv"
        # Both variants point at the dedup'd task_id.
        assert len(variants) == 2
        for v in variants:
            assert cd_id in v.depends_on, v

    def test_dedup_is_stable_across_runs(self):
        """Two consecutive plan calls with the same inputs (passed in
        differing order) produce the same descriptor list — common-deps
        sorted deterministically by task_id, variants in binary-name
        order.
        """
        def _make(binary: str):
            template = _template([
                _node(f"{binary}-root", [1]),
                _node("libshared", []),
            ])
            arr = _variant_array(
                template_id=0, arch="x86_64",
                variants=[f"gcc15-{binary}"],
                hashes=[
                    [(f"rooth-{binary}", f"{binary}.drv")],
                    [("shareh", "libshared.drv")],
                ],
            )
            return {
                "templates": [template],
                "variant_arrays": {(0, "x86_64"): arr},
                "common_deps_per_arch_template": {
                    (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
                },
            }, {
                ("x86_64", f"gcc15-{binary}"): _variant_spec(
                    f"gcc15-{binary}", binary, "x86_64",
                ),
            }

        hb, hlookup = _make("hello")
        sb, slookup = _make("busybox")
        a = plan_phase4_from_graph([
            BinaryPlanInput("hello", hb, hlookup),
            BinaryPlanInput("busybox", sb, slookup),
        ])
        b = plan_phase4_from_graph([
            BinaryPlanInput("busybox", sb, slookup),
            BinaryPlanInput("hello", hb, hlookup),
        ])
        assert a == b


# ---------------------------------------------------------------------------
# JSON-roundtripped streaming result (worker writes _dependency_graph.json,
# adapter reads it back).
# ---------------------------------------------------------------------------


class TestJsonRoundtrippedInput:
    """The plan calls for ``dependency_graph_worker`` to write a JSON
    snapshot; the planner consumes the same shape after a round-trip.
    Tuples become lists, dict keys become strings ("3|x86_64"), node
    ids become string keys in the classification dict."""

    def test_string_keys_and_list_idents(self):
        template = _template([_node("root", [])])
        # Hashes as 2-element LISTS (post-JSON form).
        arr = _variant_array(
            0, "x86_64", ["gcc15-O0"],
            [[["rh", "hello.drv"]]],
        )
        streaming = {
            "templates": [template],
            # Stringified compound key.
            "variant_arrays": {"0|x86_64": arr},
            "common_deps_per_arch_template": {"0|x86_64": {"0": "common_dep"}},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O0"): _variant_spec("gcc15-O0", "hello", "x86_64"),
        }
        descs = plan_phase4_for_binary("hello", streaming, variant_lookup)
        assert any(d.kind == "build_common_dep" for d in descs)
        assert any(d.kind == "build_variant" for d in descs)

    def test_toolchain_node_ids_with_string_keys(self):
        """JSON-roundtripped ``toolchain_node_ids_per_template`` has
        string outer keys (dict-keys → JSON object keys). The adapter
        must coerce them to int and still resolve the wiring."""
        template = _template([
            _node("root", [1]),
            _node("gcc-wrapper-15.drv", [], is_toolchain=True),
        ])
        arr = _variant_array(
            0, "x86_64", ["gcc15-O0"],
            [[["rh", "hello.drv"]], [[]]],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {"0|x86_64": arr},
            "common_deps_per_arch_template": {
                "0|x86_64": {"0": "common_dep"},
            },
            "toolchain_drvs": [["ccwh", "gcc-wrapper-15.drv"]],
            # Stringified outer key + list node_ids (post-JSON form).
            "toolchain_node_ids_per_template": {"0": [1]},
            "arch_indep_deps": {},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O0"): _variant_spec("gcc15-O0", "hello", "x86_64"),
        }
        descs = plan_phase4_for_binary(
            "hello", streaming, variant_lookup,
            toolchain_task_ids={
                "ccwh-gcc-wrapper-15.drv": "build_compilers__x86_64__gcc15",
            },
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 1
        assert "build_compilers__x86_64__gcc15" in variants[0].depends_on


# ---------------------------------------------------------------------------
# Descriptor sanity: frozen dataclass + stable equality
# ---------------------------------------------------------------------------


class TestPhase4DescriptorShape:

    def test_descriptor_is_frozen(self):
        d = Phase4Descriptor(
            kind="build_variant",
            task_id="t",
            name="n",
            payload={},
            depends_on=(),
        )
        with pytest.raises(dataclasses_exception()):
            d.kind = "build_common_dep"  # type: ignore[misc]


def dataclasses_exception():
    """Frozen-dataclass assignment raises ``dataclasses.FrozenInstanceError``;
    importing it indirectly keeps test imports tight."""
    import dataclasses
    return dataclasses.FrozenInstanceError
