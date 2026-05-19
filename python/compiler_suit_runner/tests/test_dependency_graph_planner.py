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
# Arch-indep dep emission (per-binary build_common_dep__arch_indep tasks)
# ---------------------------------------------------------------------------


class TestArchIndepDepEmission:
    """``arch_indep_deps[binary]`` contains depth-2 matrix children that
    aren't variant entry-points: source tarballs, fetched archives,
    patches, setup-hook scripts, and the occasional non-toolchain
    arch-indep helper drv. The planner emits one
    ``build_common_dep__arch_indep__<binary>__<ident>`` task per
    NON-source-terminal ident; source-terminal idents (recognised by
    ``_is_source_terminal_role``) get NO task — nix's substituter
    materialises them at build time."""

    def _build_streaming(
        self,
        binary: str,
        indep_idents: list[tuple[str, str]],
    ) -> tuple[dict, dict[tuple[str, str], dict]]:
        template = _template([
            _node(f"{binary}-root", []),
        ])
        arr = _variant_array(
            template_id=0,
            arch="x86_64",
            variants=["gcc15-O2"],
            hashes=[[(f"rh-{binary}", f"{binary}.drv")]],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific"},
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {binary: set(indep_idents)},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", binary, "x86_64",
            ),
        }
        return streaming, variant_lookup

    def test_source_terminal_idents_skipped(self):
        """A hello-fixture style mix: a ``-source.drv`` and a
        ``.tar.gz.drv`` are both source-terminal-roled and must NOT
        emit tasks; a plain ``<binary>-helper.drv`` is a regular
        arch-indep dep and must emit one
        ``build_common_dep__arch_indep__hello__<ident>`` task."""
        indep = [
            ("srch", "hello-2.12-source.drv"),
            ("tarh", "hello-2.12.tar.gz.drv"),
            ("helph", "hello-helper.drv"),
        ]
        streaming, lookup = self._build_streaming("hello", indep)
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        arch_indep = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.payload.get("arch") == "arch_indep"
        ]
        # Only the non-source-terminal ident becomes a task.
        assert len(arch_indep) == 1, [d.task_id for d in arch_indep]
        helper = arch_indep[0]
        assert helper.task_id == (
            "build_common_dep__arch_indep__hello__helph-hello-helper.drv"
        )
        assert helper.payload["binary"] == "hello"
        assert helper.payload["arch"] == "arch_indep"
        assert helper.payload["ident"] == "helph-hello-helper.drv"

    def test_emitted_task_wired_into_every_variant(self):
        """Every variant of every arch for this binary lists the
        emitted ``build_common_dep__arch_indep`` task in its
        ``depends_on``."""
        indep = [("helph", "hello-helper.drv")]
        streaming, lookup = self._build_streaming("hello", indep)
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        arch_indep = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.payload.get("arch") == "arch_indep"
        ]
        assert len(arch_indep) == 1
        ai_id = arch_indep[0].task_id
        variants = [d for d in descs if d.kind == "build_variant"]
        assert variants, "expected at least one variant descriptor"
        for v in variants:
            assert ai_id in v.depends_on, (v.task_id, v.depends_on)

    def test_all_source_terminal_emits_nothing(self):
        """When every arch-indep ident is source-terminal-roled, no
        task is emitted and variant ``depends_on`` carries no
        arch-indep task ids."""
        indep = [
            ("srch", "hello-2.12-source.drv"),
            ("patch", "hello-fix.patch"),
            ("hook", "hello-setup-hook.sh"),
        ]
        streaming, lookup = self._build_streaming("hello", indep)
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        arch_indep = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.payload.get("arch") == "arch_indep"
        ]
        assert arch_indep == []
        variants = [d for d in descs if d.kind == "build_variant"]
        for v in variants:
            assert not any(
                dep.startswith("build_common_dep__arch_indep__")
                for dep in v.depends_on
            ), v

    def test_idents_for_other_binary_ignored(self):
        """Arch-indep deps stay scoped to the binary they live under."""
        streaming, lookup = self._build_streaming(
            "hello", [("helph", "hello-helper.drv")],
        )
        # Inject a non-hello bucket; it must NOT bleed into the hello
        # plan.
        streaming["arch_indep_deps"]["busybox"] = {
            ("xh", "busybox-helper.drv"),
        }
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        arch_indep = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.payload.get("arch") == "arch_indep"
        ]
        idents = {d.payload["ident"] for d in arch_indep}
        assert idents == {"helph-hello-helper.drv"}, idents


# ---------------------------------------------------------------------------
# Toolchain ident wiring (set[(hash, name)] → variant depends_on)
# ---------------------------------------------------------------------------


class TestToolchainWiring:
    """A variant whose template has an ``is_toolchain=True`` node with
    a known ident in the row should get that toolchain's task_id wired
    into its ``depends_on``.

    Exercises the ``(hash, name)`` → ``"<hash>-<name>"`` translation
    on the FAST path (not via :func:`convert_toolchain_drvs`)."""

    def test_toolchain_task_id_wired_into_variant(self):
        template = _template([
            _node("variant-root", [1]),
            # Toolchain marker — streaming planner usually skips
            # storing the ident, but the cowalk can stash it; we
            # exercise the path where it IS stored so the planner
            # can resolve it to a task_id.
            _node("cc-wrapper", [], is_toolchain=True),
        ])
        arr = _variant_array(
            template_id=0,
            arch="x86_64",
            variants=["gcc15-O0"],
            hashes=[
                [("rooth", "hello.drv")],
                [("ccwh", "gcc-wrapper-15.drv")],
            ],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "common_dep"},
            },
            "toolchain_drvs": {("ccwh", "gcc-wrapper-15.drv")},
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
        omitted from depends_on (the caller's contract is to provide
        the map for every toolchain it cares about)."""
        template = _template([
            _node("root", [1]),
            _node("cc-wrapper", [], is_toolchain=True),
        ])
        arr = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O0"],
            hashes=[
                [("rh", "hello.drv")],
                [("unknownh", "cc-wrapper.drv")],
            ],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "common_dep"},
            },
            "toolchain_drvs": {("unknownh", "cc-wrapper.drv")},
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

    def test_two_binaries_concatenated_in_name_order(self):
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
        binaries_seen = [d.payload.get("binary") or d.payload.get("pkg") for d in descs]
        # busybox descriptors come first.
        busybox_indices = [i for i, b in enumerate(binaries_seen) if b == "busybox"]
        hello_indices = [i for i, b in enumerate(binaries_seen) if b == "hello"]
        assert busybox_indices, descs
        assert hello_indices, descs
        assert max(busybox_indices) < min(hello_indices)


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
