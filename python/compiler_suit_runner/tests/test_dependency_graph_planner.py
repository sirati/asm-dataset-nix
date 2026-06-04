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


def _meta_template(
    template_id_per_arch: dict,
    role_at_node: list,
    cross_arch_classification: list,
    drv_per_node: list,
) -> dict:
    """Shape-mirror of ``template_graph.graph.MetaTemplate`` as a dict;
    the meta pass reads via ``getattr`` so a Mapping with the same
    attribute names works as a stand-in for unit tests (consistent with
    the existing template / VariantArray fabrication helpers)."""

    class _Mt:
        def __init__(self):
            self.template_id_per_arch = dict(template_id_per_arch)
            self.role_at_node = tuple(role_at_node)
            self.cross_arch_classification = tuple(cross_arch_classification)
            self.drv_per_node = tuple(drv_per_node)
            self.enforce_at_node = tuple(None for _ in role_at_node)
            self.class_letter_at_node = tuple(None for _ in role_at_node)
    return _Mt()


# ``parse_variant_path`` expects the post-hash basename to end with
# ``-baseline-default-san-off-march-default-elf-folder.drv``; appending
# the canonical tail to ``<pkg>-<arch>-<label>`` produces a synthesised
# drv path the per-variant toolchain wiring can decode back to
# ``(arch, comp, opt)`` when ``label`` is the canonical ``<comp>-<opt>``
# shape. Non-conforming labels still produce a value; the wiring's
# ``TreeWalkError`` swallow keeps those tests robust.
_VARIANT_DRV_TAIL = (
    "-baseline-default-san-off-march-default-elf-folder.drv"
)


def _variant_spec(label: str, pkg: str, arch: str, **extra) -> dict:
    """Minimal VariantSpec-like dict matching what the matrix
    enumerator emits."""
    base = {
        "label": label,
        "pkg": pkg,
        "arch": arch,
        "drv": (
            f"/nix/store/aaaa-{pkg}-{arch}-{label}{_VARIANT_DRV_TAIL}"
        ),
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
        toolchain_task_ids = {
            "ccwh-gcc-wrapper-15.drv":
                "x86_64-linux__x86_64__gcc15",
        }
        descs = plan_phase4_for_binary(
            "hello",
            streaming,
            variant_lookup,
            toolchain_task_ids=toolchain_task_ids,
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 1
        # Toolchain dep is cross-phase: it lands in
        # build_compilers_depends_on, NOT the intra-phase depends_on.
        assert (
            "x86_64-linux__x86_64__gcc15"
            in variants[0].build_compilers_depends_on
        )
        assert "x86_64-linux__x86_64__gcc15" not in variants[0].depends_on

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
        # Common-dep deps from the root may still be present in the
        # intra-phase depends_on, but the cross-phase field is empty.
        assert variants[0].build_compilers_depends_on == ()

    def test_unified_wrapper_role_wires_per_variant_compiler(self):
        """A toolchain node whose role unifies multiple compiler
        versions (e.g. ``wrapped-compiler-suit.drv``) must wire EACH
        variant to its own compiler's ``build_compilers`` task,
        not the union of every compiler that shares the cell. The
        role-collapsed template node hides the per-variant compiler
        choice, but the variant's drv basename carries it -- so
        per-variant resolution recovers it via ``parse_variant_path``.
        The toolchain id lands in the cross-phase
        ``build_compilers_depends_on`` field.
        """
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
            "g14h-wrapped-compiler-suit.drv":
                "x86_64-linux__x86_64__gcc14",
            "g15h-wrapped-compiler-suit.drv":
                "x86_64-linux__x86_64__gcc15",
        }
        descs = plan_phase4_for_binary(
            "hello", streaming, variant_lookup,
            toolchain_task_ids=toolchain_task_ids,
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 2
        by_label = {v.payload["label"]: v for v in variants}
        # Each variant gets EXACTLY its own compiler's task_id, not the
        # union -- the per-variant resolver reads ``(arch, comp)`` off
        # the variant drv basename. The id lands in the cross-phase
        # build_compilers_depends_on field.
        g14_id = "x86_64-linux__x86_64__gcc14"
        g15_id = "x86_64-linux__x86_64__gcc15"
        assert g14_id in by_label["gcc14-O2"].build_compilers_depends_on
        assert g15_id not in by_label["gcc14-O2"].build_compilers_depends_on
        assert g15_id in by_label["gcc15-O2"].build_compilers_depends_on
        assert g14_id not in by_label["gcc15-O2"].build_compilers_depends_on

    def test_two_compilers_one_cell_wire_independently(self):
        """Sharp regression for the original cell-level conflation
        bug: two DISTINCT wrappers (gcc14 + clang20, cross-family,
        distinct hashes) in the same cell must each wire ONLY to
        their own ``build_compilers`` task. Pre-fix, both wrappers
        got unioned into every variant; the negative assertions
        below are what that bug violated."""
        g14_hash = "0000000000000000000000000000gcc14"  # 32-char-style
        cl20_hash = "00000000000000000000000000clang20"
        template = _template([
            _node("hello-root", [1, 2]),
            _node("gcc-wrapper-14.3.0.drv", [], is_toolchain=True),
            _node("clang-wrapper-20.1.8.drv", [], is_toolchain=True),
        ])
        arr = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc14-O2", "clang20-O2"],
            hashes=[[("rg", "hello.drv"), ("rc", "hello.drv")], [], []],
        )
        g14_id = "x86_64-linux__x86_64__gcc14"
        cl20_id = "x86_64-linux__x86_64__clang20"
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {(0, "x86_64"): {0: "common_dep"}},
            "toolchain_drvs": {
                (g14_hash, "gcc-wrapper-14.3.0.drv"),
                (cl20_hash, "clang-wrapper-20.1.8.drv"),
            },
            "toolchain_node_ids_per_template": {0: [1, 2]},
            "arch_indep_deps": {},
        }
        variant_lookup = {
            ("x86_64", "gcc14-O2"): _variant_spec("gcc14-O2", "hello", "x86_64"),
            ("x86_64", "clang20-O2"): _variant_spec("clang20-O2", "hello", "x86_64"),
        }
        descs = plan_phase4_for_binary(
            "hello", streaming, variant_lookup,
            toolchain_task_ids={
                f"{g14_hash}-gcc-wrapper-14.3.0.drv": g14_id,
                f"{cl20_hash}-clang-wrapper-20.1.8.drv": cl20_id,
            },
        )
        by_label = {
            v.payload["label"]: v
            for v in descs if v.kind == "build_variant"
        }
        assert len(by_label) == 2
        assert g14_id in by_label["gcc14-O2"].build_compilers_depends_on
        assert cl20_id not in by_label["gcc14-O2"].build_compilers_depends_on
        assert cl20_id in by_label["clang20-O2"].build_compilers_depends_on
        assert g14_id not in by_label["clang20-O2"].build_compilers_depends_on

    def test_unknown_toolchain_task_id_skipped(self):
        """Per-variant wiring composes the bare ``<sys>__<arch>__<comp>``
        build_compilers task_id from the variant drv and looks it up in
        ``toolchain_task_ids``'s VALUE set. A task_id not present in that
        set (e.g. an operator-provided toolchain the framework never
        built) yields no dep -- empty toolchain_task_ids maps to no
        toolchain wiring at all. The old
        ``toolchain_node_ids_per_template`` gating is gone; this test
        asserts the value-set lookup directly.
        """
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
        # Phase-1 emitted a DIFFERENT compiler's task; the variant's
        # composed id ``x86_64-linux__x86_64__gcc15`` isn't present, so
        # no wiring fires.
        toolchain_task_ids = {
            "otherh-clang-wrapper-19.drv":
                "x86_64-linux__x86_64__clang19",
        }
        descs = plan_phase4_for_binary(
            "hello", streaming, variant_lookup,
            toolchain_task_ids=toolchain_task_ids,
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 1
        # No matching toolchain task → empty cross-phase field.
        assert variants[0].build_compilers_depends_on == ()


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
# List-coerced streaming result (legacy JSON-roundtripped shape). The
# planner stays tolerant of this form so callers that route the
# streaming result through any list-coercing serialisation layer keep
# working; the production worker now pickles dataclasses directly, but
# the shape-tolerance has independent value.
# ---------------------------------------------------------------------------


class TestJsonRoundtrippedInput:
    """The planner consumes both the native streaming result and a
    list-coerced form (tuples become lists, dict keys become strings
    ("3|x86_64"), node ids become string keys in the classification
    dict). This used to be the on-disk worker shape; today it's a
    backwards-compat guarantee for the planner adapter."""

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
                "ccwh-gcc-wrapper-15.drv":
                    "x86_64-linux__x86_64__gcc15",
            },
        )
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 1
        assert (
            "x86_64-linux__x86_64__gcc15"
            in variants[0].build_compilers_depends_on
        )


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


# ---------------------------------------------------------------------------
# MetaTemplate-driven cross_arch / family build_common_dep emission (5.4)
# ---------------------------------------------------------------------------


class TestMetaTemplateCrossArchEmission:
    """A MetaTemplate position classified ``cross_arch_common_dep`` emits
    ONE ``build_common_dep__cross_arch__<ident>`` descriptor; variants
    in every covered arch list that task in their ``depends_on``. The
    per-cell common_dep emission for the same ident is suppressed to
    avoid duplicate dispatch."""

    def _streaming(self, libfoo_ident: tuple[str, str]):
        # Two archs, each with one variant; the libfoo node is the
        # cross-arch shared dep — same ident across both. Per-cell
        # classification keeps libfoo as common_dep so we can prove
        # the meta-pass replaces (not augments) per-cell emission for
        # this ident.
        tmpl_x = _template([
            _node("hello-x86_64-root", [1]),
            _node("libfoo.drv", []),
        ])
        tmpl_a = _template([
            _node("hello-aarch64-root", [1]),
            _node("libfoo.drv", []),
        ])
        arr_x = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O2"],
            hashes=[
                [("rxh", "hello-x86_64.drv")],
                [libfoo_ident],
            ],
        )
        arr_a = _variant_array(
            template_id=1, arch="aarch64",
            variants=["gcc15-O2"],
            hashes=[
                [("rah", "hello-aarch64.drv")],
                [libfoo_ident],
            ],
        )
        meta = _meta_template(
            template_id_per_arch={"x86_64": 0, "aarch64": 1},
            role_at_node=["hello-elf-folder.drv", "libfoo.drv"],
            cross_arch_classification=[
                "variant_specific", "cross_arch_common_dep",
            ],
            drv_per_node=[None, libfoo_ident],
        )
        streaming = {
            "templates": [tmpl_x, tmpl_a],
            "variant_arrays": {
                (0, "x86_64"): arr_x,
                (1, "aarch64"): arr_a,
            },
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
                (1, "aarch64"): {0: "variant_specific", 1: "common_dep"},
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {},
            "meta_templates": {"hello": [meta]},
        }
        variant_lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "x86_64",
            ),
            ("aarch64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "aarch64",
            ),
        }
        return streaming, variant_lookup

    def test_emits_one_cross_arch_descriptor(self):
        ident = ("shareh", "libfoo.drv")
        streaming, lookup = self._streaming(ident)
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        cross = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.task_id.startswith("build_common_dep__cross_arch__")
        ]
        assert len(cross) == 1, [d.task_id for d in cross]
        assert cross[0].task_id == "build_common_dep__cross_arch__shareh-libfoo.drv"
        assert cross[0].payload["arch"] == "cross_arch"
        assert cross[0].payload["ident"] == "shareh-libfoo.drv"
        assert cross[0].payload["node_name"] == "libfoo.drv"

    def test_both_arch_variants_depend_on_cross_arch_task(self):
        ident = ("shareh", "libfoo.drv")
        streaming, lookup = self._streaming(ident)
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        cross_id = "build_common_dep__cross_arch__shareh-libfoo.drv"
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 2
        for v in variants:
            assert cross_id in v.depends_on, (v.task_id, v.depends_on)

    def test_per_cell_duplicate_suppressed(self):
        """The per-cell ``build_common_dep__<ident>`` task is dropped in
        favour of the meta-level ``build_common_dep__cross_arch__<ident>``
        so the descriptor list carries no duplicate dispatch for the
        same content-addressed sub-drv."""
        ident = ("shareh", "libfoo.drv")
        streaming, lookup = self._streaming(ident)
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        per_cell_dup = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.task_id == "build_common_dep__shareh-libfoo.drv"
        ]
        assert per_cell_dup == [], [d.task_id for d in per_cell_dup]

    def test_descriptor_order_meta_before_per_cell(self):
        """Meta-level entries sit between arch-indep and per-cell blocks
        in the returned list."""
        ident = ("shareh", "libfoo.drv")
        streaming, lookup = self._streaming(ident)
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        # First non-variant kind that's cross_arch should come before
        # any variant_specific or per-cell common_dep with a concrete
        # arch.
        meta_idx = next(
            (i for i, d in enumerate(descs)
             if d.kind == "build_common_dep"
             and d.payload.get("arch") == "cross_arch"),
            None,
        )
        variant_idx = next(
            (i for i, d in enumerate(descs) if d.kind == "build_variant"),
            None,
        )
        assert meta_idx is not None
        assert variant_idx is not None
        assert meta_idx < variant_idx


class TestMetaTemplateFamilyEmission:
    """A MetaTemplate position classified ``family_common_dep`` emits one
    ``build_common_dep__family__<family>__<ident>`` task per family;
    archs in each family wire their variants to the matching family
    task."""

    def test_two_family_idents_emit_two_tasks(self):
        x86_ident = ("xh", "libfam.drv")
        arm_ident = ("ah", "libfam.drv")
        tmpl_x = _template([
            _node("hello-x86_64-root", [1]),
            _node("libfam.drv", []),
        ])
        tmpl_a = _template([
            _node("hello-aarch64-root", [1]),
            _node("libfam.drv", []),
        ])
        arr_x = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O2"],
            hashes=[
                [("rxh", "hello-x86_64.drv")],
                [x86_ident],
            ],
        )
        arr_a = _variant_array(
            template_id=1, arch="aarch64",
            variants=["gcc15-O2"],
            hashes=[
                [("rah", "hello-aarch64.drv")],
                [arm_ident],
            ],
        )
        meta = _meta_template(
            template_id_per_arch={"x86_64": 0, "aarch64": 1},
            role_at_node=["hello-elf-folder.drv", "libfam.drv"],
            cross_arch_classification=[
                "variant_specific", "family_common_dep",
            ],
            drv_per_node=[None, {"x86": x86_ident, "arm": arm_ident}],
        )
        streaming = {
            "templates": [tmpl_x, tmpl_a],
            "variant_arrays": {
                (0, "x86_64"): arr_x,
                (1, "aarch64"): arr_a,
            },
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
                (1, "aarch64"): {0: "variant_specific", 1: "common_dep"},
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {},
            "meta_templates": {"hello": [meta]},
        }
        lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "x86_64",
            ),
            ("aarch64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "aarch64",
            ),
        }
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        family = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.task_id.startswith("build_common_dep__family__")
        ]
        ids = {d.task_id for d in family}
        assert ids == {
            "build_common_dep__family__x86__xh-libfam.drv",
            "build_common_dep__family__arm__ah-libfam.drv",
        }
        variants = {v.payload["arch"]: v for v in descs if v.kind == "build_variant"}
        assert (
            "build_common_dep__family__x86__xh-libfam.drv"
            in variants["x86_64"].depends_on
        )
        assert (
            "build_common_dep__family__arm__ah-libfam.drv"
            in variants["aarch64"].depends_on
        )
        # Per-cell duplicate emission is suppressed for both family idents.
        per_cell_dups = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.task_id in (
                "build_common_dep__xh-libfam.drv",
                "build_common_dep__ah-libfam.drv",
            )
        ]
        assert per_cell_dups == []


class TestMetaTemplateShortCircuits:
    """Source-terminal roles and toolchain roles must NOT emit
    meta-level descriptors (plan §E2 branches 1 and 2)."""

    def test_source_terminal_role_emits_no_descriptor(self):
        # ``hello-2.12.tar.gz.drv`` matches the source-terminal pattern.
        tar_ident = ("tarh", "hello-2.12.tar.gz.drv")
        tmpl = _template([
            _node("hello-x86_64-root", [1]),
            _node("hello-2.12.tar.gz.drv", []),
        ])
        arr = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O2"],
            hashes=[
                [("rh", "hello.drv")],
                [tar_ident],
            ],
        )
        meta = _meta_template(
            template_id_per_arch={"x86_64": 0},
            role_at_node=[
                "hello-elf-folder.drv", "hello-2.12.tar.gz.drv",
            ],
            cross_arch_classification=[
                "variant_specific", "cross_arch_common_dep",
            ],
            drv_per_node=[None, tar_ident],
        )
        streaming = {
            "templates": [tmpl],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {},
            "meta_templates": {"hello": [meta]},
        }
        lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "x86_64",
            ),
        }
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        # No meta-level task for the tarball ident.
        cross = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.payload.get("arch") == "cross_arch"
        ]
        assert cross == []
        # Per-cell emission is also suppressed (ident in
        # meta_skip_idents) so the substituter fetches without a
        # framework task.
        all_common = [d for d in descs if d.kind == "build_common_dep"]
        idents = {d.payload["ident"] for d in all_common}
        assert "tarh-hello-2.12.tar.gz.drv" not in idents

    def test_toolchain_role_wires_existing_task_id(self):
        """A cross_arch position whose role is a toolchain name wires
        the matching ``build_compilers`` task ids (into the cross-phase
        build_compilers_depends_on field) without minting a new
        descriptor."""
        cc_ident = ("ccwh", "gcc-wrapper-15.drv")
        tmpl_x = _template([
            _node("hello-x86_64-root", [1]),
            _node("gcc-wrapper-15.drv", [], is_toolchain=True),
        ])
        arr_x = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O2"],
            hashes=[
                [("rxh", "hello-x86_64.drv")],
                [],
            ],
        )
        meta = _meta_template(
            template_id_per_arch={"x86_64": 0},
            role_at_node=[
                "hello-elf-folder.drv", "gcc-wrapper-15.drv",
            ],
            cross_arch_classification=[
                "variant_specific", "cross_arch_common_dep",
            ],
            drv_per_node=[None, cc_ident],
        )
        streaming = {
            "templates": [tmpl_x],
            "variant_arrays": {(0, "x86_64"): arr_x},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific"},
            },
            "toolchain_drvs": {cc_ident},
            "toolchain_node_ids_per_template": {0: [1]},
            "arch_indep_deps": {},
            "meta_templates": {"hello": [meta]},
        }
        lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "x86_64",
            ),
        }
        toolchain_task_ids = {
            "ccwh-gcc-wrapper-15.drv":
                "x86_64-linux__x86_64__gcc15",
        }
        descs = plan_phase4_for_binary(
            "hello", streaming, lookup,
            toolchain_task_ids=toolchain_task_ids,
        )
        cross = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.payload.get("arch") == "cross_arch"
        ]
        assert cross == []
        variants = [d for d in descs if d.kind == "build_variant"]
        assert len(variants) == 1
        assert (
            "x86_64-linux__x86_64__gcc15"
            in variants[0].build_compilers_depends_on
        )


class TestMetaTemplateNoOpCases:

    def test_uni_arch_common_dep_no_meta_emission(self):
        """``uni_arch_common_dep`` positions are handled per-cell; the
        meta pass MUST NOT emit a duplicate task here."""
        x_ident = ("xh", "libfoo.drv")
        a_ident = ("ah", "libfoo.drv")
        tmpl_x = _template([
            _node("hello-x86_64-root", [1]),
            _node("libfoo.drv", []),
        ])
        tmpl_a = _template([
            _node("hello-aarch64-root", [1]),
            _node("libfoo.drv", []),
        ])
        arr_x = _variant_array(
            0, "x86_64", ["gcc15-O2"],
            [[("rxh", "hello-x86_64.drv")], [x_ident]],
        )
        arr_a = _variant_array(
            1, "aarch64", ["gcc15-O2"],
            [[("rah", "hello-aarch64.drv")], [a_ident]],
        )
        meta = _meta_template(
            template_id_per_arch={"x86_64": 0, "aarch64": 1},
            role_at_node=["hello-elf-folder.drv", "libfoo.drv"],
            cross_arch_classification=[
                "variant_specific", "uni_arch_common_dep",
            ],
            drv_per_node=[
                None,
                {"x86_64": x_ident, "aarch64": a_ident},
            ],
        )
        streaming = {
            "templates": [tmpl_x, tmpl_a],
            "variant_arrays": {
                (0, "x86_64"): arr_x, (1, "aarch64"): arr_a,
            },
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
                (1, "aarch64"): {0: "variant_specific", 1: "common_dep"},
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {},
            "meta_templates": {"hello": [meta]},
        }
        lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec("gcc15-O2", "hello", "x86_64"),
            ("aarch64", "gcc15-O2"): _variant_spec("gcc15-O2", "hello", "aarch64"),
        }
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        # No cross_arch / family descriptor; per-cell handles A/C.
        meta_lvl = [
            d for d in descs
            if d.kind == "build_common_dep"
            and (
                d.payload.get("arch") == "cross_arch"
                or str(d.payload.get("arch", "")).startswith("family__")
            )
        ]
        assert meta_lvl == []
        # Per-cell emissions both fire — one per arch.
        per_cell = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.payload.get("arch") in {"x86_64", "aarch64"}
        ]
        assert len(per_cell) == 2

    def test_missing_meta_templates_key_is_a_noop(self):
        """If the streaming result has no ``meta_templates`` key (older
        snapshot), the planner falls back to per-cell-only emission and
        produces the same shape as pre-5.4."""
        template = _template([
            _node("root", [1]),
            _node("shared-lib.drv", []),
        ])
        arr = _variant_array(
            0, "x86_64", ["gcc15-O2"],
            [
                [("rh", "hello.drv")],
                [("shareh", "shared-lib.drv")],
            ],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
            },
            # NO meta_templates key.
        }
        lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "x86_64",
            ),
        }
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        per_cell = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.task_id == "build_common_dep__shareh-shared-lib.drv"
        ]
        assert len(per_cell) == 1


# ---------------------------------------------------------------------------
# priority_hint on meta-level common_dep descriptors (Phase 6.2 / plan §E7)
# ---------------------------------------------------------------------------


class TestPriorityHintOnMetaDescriptors:
    """Cross-arch and per-family meta ``build_common_dep`` descriptors
    carry a positive ``priority_hint`` so the framework's scheduler
    picks them up before per-arch variants. Every other Phase 4 task
    (per-cell common_dep, arch_indep, build_variant) stays at the
    neutral default of ``0`` — the field is opt-in, never imposed on
    tasks that don't need a scheduling nudge."""

    def _two_arch_streaming(self, libfoo_ident: tuple[str, str]):
        tmpl_x = _template([
            _node("hello-x86_64-root", [1]),
            _node("libfoo.drv", []),
        ])
        tmpl_a = _template([
            _node("hello-aarch64-root", [1]),
            _node("libfoo.drv", []),
        ])
        arr_x = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O2"],
            hashes=[
                [("rxh", "hello-x86_64.drv")],
                [libfoo_ident],
            ],
        )
        arr_a = _variant_array(
            template_id=1, arch="aarch64",
            variants=["gcc15-O2"],
            hashes=[
                [("rah", "hello-aarch64.drv")],
                [libfoo_ident],
            ],
        )
        meta = _meta_template(
            template_id_per_arch={"x86_64": 0, "aarch64": 1},
            role_at_node=["hello-elf-folder.drv", "libfoo.drv"],
            cross_arch_classification=[
                "variant_specific", "cross_arch_common_dep",
            ],
            drv_per_node=[None, libfoo_ident],
        )
        streaming = {
            "templates": [tmpl_x, tmpl_a],
            "variant_arrays": {
                (0, "x86_64"): arr_x,
                (1, "aarch64"): arr_a,
            },
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
                (1, "aarch64"): {0: "variant_specific", 1: "common_dep"},
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {},
            "meta_templates": {"hello": [meta]},
        }
        lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "x86_64",
            ),
            ("aarch64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "aarch64",
            ),
        }
        return streaming, lookup

    def test_cross_arch_descriptor_has_positive_hint(self):
        streaming, lookup = self._two_arch_streaming(("shareh", "libfoo.drv"))
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        cross = next(
            d for d in descs
            if d.kind == "build_common_dep"
            and d.task_id.startswith("build_common_dep__cross_arch__")
        )
        assert cross.priority_hint > 0, cross.priority_hint

    def test_variant_descriptors_keep_default_zero_hint(self):
        streaming, lookup = self._two_arch_streaming(("shareh", "libfoo.drv"))
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        variants = [d for d in descs if d.kind == "build_variant"]
        assert variants, "two-arch fixture must emit variants"
        for v in variants:
            assert v.priority_hint == 0, (v.task_id, v.priority_hint)

    def test_uni_arch_per_cell_common_dep_has_zero_hint(self):
        """A single-arch shared dep (no meta-level emission) goes out
        with the default-0 hint so the field doesn't accidentally bias
        every common_dep — only the cross-arch / family ones do."""
        template = _template([
            _node("root", [1]),
            _node("shared-lib.drv", []),
        ])
        arr = _variant_array(
            0, "x86_64", ["gcc15-O2"],
            [
                [("rh", "hello.drv")],
                [("shareh", "shared-lib.drv")],
            ],
        )
        streaming = {
            "templates": [template],
            "variant_arrays": {(0, "x86_64"): arr},
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
            },
        }
        lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "x86_64",
            ),
        }
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        per_cell = next(
            d for d in descs
            if d.kind == "build_common_dep"
            and d.task_id == "build_common_dep__shareh-shared-lib.drv"
        )
        assert per_cell.priority_hint == 0

    def test_family_descriptors_have_positive_hint(self):
        """Family-level meta common_dep descriptors are equally shared
        within their family and get the same scheduling nudge."""
        x86_ident = ("xh", "libfam.drv")
        arm_ident = ("ah", "libfam.drv")
        tmpl_x = _template([
            _node("hello-x86_64-root", [1]),
            _node("libfam.drv", []),
        ])
        tmpl_a = _template([
            _node("hello-aarch64-root", [1]),
            _node("libfam.drv", []),
        ])
        arr_x = _variant_array(
            template_id=0, arch="x86_64",
            variants=["gcc15-O2"],
            hashes=[
                [("rxh", "hello-x86_64.drv")],
                [x86_ident],
            ],
        )
        arr_a = _variant_array(
            template_id=1, arch="aarch64",
            variants=["gcc15-O2"],
            hashes=[
                [("rah", "hello-aarch64.drv")],
                [arm_ident],
            ],
        )
        meta = _meta_template(
            template_id_per_arch={"x86_64": 0, "aarch64": 1},
            role_at_node=["hello-elf-folder.drv", "libfam.drv"],
            cross_arch_classification=[
                "variant_specific", "family_common_dep",
            ],
            drv_per_node=[None, {"x86": x86_ident, "arm": arm_ident}],
        )
        streaming = {
            "templates": [tmpl_x, tmpl_a],
            "variant_arrays": {
                (0, "x86_64"): arr_x,
                (1, "aarch64"): arr_a,
            },
            "common_deps_per_arch_template": {
                (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
                (1, "aarch64"): {0: "variant_specific", 1: "common_dep"},
            },
            "toolchain_drvs": set(),
            "arch_indep_deps": {},
            "meta_templates": {"hello": [meta]},
        }
        lookup = {
            ("x86_64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "x86_64",
            ),
            ("aarch64", "gcc15-O2"): _variant_spec(
                "gcc15-O2", "hello", "aarch64",
            ),
        }
        descs = plan_phase4_for_binary("hello", streaming, lookup)
        family = [
            d for d in descs
            if d.kind == "build_common_dep"
            and d.task_id.startswith("build_common_dep__family__")
        ]
        assert family, [d.task_id for d in descs]
        for d in family:
            assert d.priority_hint > 0, (d.task_id, d.priority_hint)

    def test_default_priority_hint_is_zero_on_phase4descriptor(self):
        """Direct constructor check: omitting the field defaults to
        ``0``, so callers (and round-tripped legacy JSON) that don't
        know about the new field stay neutral."""
        d = Phase4Descriptor(
            kind="build_common_dep",
            task_id="x",
            name="x",
            payload={},
        )
        assert d.priority_hint == 0

    def test_manifest_header_threads_priority_hint(self):
        """``headers_from_descriptors`` passes ``priority_hint`` through
        to the framework's :class:`ManifestHeader` (Phase 6.2 wiring)."""
        from compiler_suit_runner.dependency_graph_planner import (
            headers_from_descriptors,
        )

        d_hi = Phase4Descriptor(
            kind="build_common_dep",
            task_id="build_common_dep__cross_arch__h-libfoo.drv",
            name="build_common_dep__hello__cross_arch__libfoo.drv",
            payload={"sys": "x86_64-linux", "arch": "cross_arch"},
            priority_hint=10,
        )
        d_lo = Phase4Descriptor(
            kind="build_common_dep",
            task_id="build_common_dep__h-libfoo.drv",
            name="build_common_dep__hello__x86_64__libfoo.drv",
            payload={"sys": "x86_64-linux", "arch": "x86_64"},
        )
        headers = headers_from_descriptors([d_hi, d_lo])
        assert len(headers) == 2
        # Map by task_id so the assertions don't depend on iteration
        # order if a future change re-sorts ``headers_from_descriptors``.
        by_id = {h.task_id: h for h in headers}
        assert by_id[d_hi.task_id].priority_hint == 10
        assert by_id[d_lo.task_id].priority_hint == 0

    def test_priority_hint_roundtrips_via_pickle(self, tmp_path):
        """:func:`load_phase4_descriptors` recovers ``priority_hint``
        from the on-disk pickle so the watcher reads back what the
        dependency-graph worker wrote."""
        from compiler_suit_runner.dependency_graph_planner import (
            load_phase4_descriptors,
        )
        from compiler_suit_runner.workers.dependency_graph_worker.output import (
            DEPENDENCY_GRAPH_PICKLE,
            write_phase4_descriptors,
        )

        original = Phase4Descriptor(
            kind="build_common_dep",
            task_id="build_common_dep__cross_arch__h-libfoo.drv",
            name="build_common_dep__hello__cross_arch__libfoo.drv",
            payload={"sys": "x86_64-linux", "arch": "cross_arch"},
            priority_hint=10,
        )
        out_path = tmp_path / DEPENDENCY_GRAPH_PICKLE
        write_phase4_descriptors(
            descriptors=[original], summary={}, out_path=out_path,
        )
        recovered, _summary = load_phase4_descriptors(out_path)
        assert len(recovered) == 1
        assert recovered[0].priority_hint == 10
