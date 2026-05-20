"""Direct unit tests for :mod:`template_graph.cowalk._role_merge`.

The cross-arch ``MetaTemplate`` construction stack consumes two
primitives from :mod:`template_graph.cowalk._role_merge`:

* :func:`build_merged_keymap` — folds per-arch templates by
  ``(role, enforce)`` key with class-priority merging,
* :func:`canonical_walk_order` — DFS the merged keymap from the
  canonical root key, deduplicating the child-key union across archs.

These tests exercise both primitives without going through the
streaming planner so the contract is pinned down independently of
planner-internal classification heuristics. Templates and
``VariantArray`` instances are constructed directly.
"""

from __future__ import annotations

from template_graph.cowalk._role_merge import (
    build_merged_keymap,
    canonical_walk_order,
)
from template_graph.graph import Template, TemplateNode, VariantArray
from template_graph.tests.test_streaming.fixtures import make_hash_bytes


def _mk_template(
    nodes: list[tuple[str, list[int], bool, object]],
    root_id: int = 0,
    built_from: str = "v0",
) -> Template:
    """Build a ``Template`` from a list of ``(name, child_ids,
    is_toolchain, enforce)`` tuples. Indices in ``nodes`` become node
    ids; ``name_to_id`` maps each role name to its first occurrence.
    """
    tnodes: list[TemplateNode] = []
    name_to_id: dict[str, int] = {}
    for name, children, is_tc, enforce in nodes:
        tnodes.append(
            TemplateNode(
                name=name, child_ids=list(children),
                is_toolchain=is_tc, enforce=enforce,
            )
        )
        name_to_id.setdefault(name, len(tnodes) - 1)
    return Template(
        nodes=tnodes,
        name_to_id=name_to_id,
        root_id=root_id,
        template_built_from=[built_from],
    )


def _mk_variant_array(
    template_id: int, arch: str, n_nodes: int,
    drv_for_nid: dict[int, tuple[bytes, str]],
) -> VariantArray:
    """Single-variant ``VariantArray``. ``drv_for_nid`` supplies the
    variant-0 ``(hash, name)`` tuple per node id; unmentioned node
    ids get ``None``. ``hash`` is bytes (planner-side type).
    """
    hashes: list[list] = []
    for nid in range(n_nodes):
        hashes.append([drv_for_nid.get(nid)])
    return VariantArray(
        template_id=template_id, arch=arch,
        variants=["v0"], hashes=hashes,
    )


def test_role_merge_keymap_groups_by_role_and_enforce():
    """Two templates with the same role string but different
    ``enforce`` tuples must produce TWO keys in the merged keymap;
    same role + same enforce must collapse to ONE key.
    """
    # Template A: root → gcc-runtime.drv with enforce ("triple", "x86_64-linux").
    tmpl_a = _mk_template(
        nodes=[
            ("hello-elf-folder.drv", [1], False, None),
            ("gcc-runtime.drv", [], False, ("triple", "x86_64-linux")),
        ],
        built_from="x86_64-v0",
    )
    # Template B: root → gcc-runtime.drv with DIFFERENT enforce.
    tmpl_b = _mk_template(
        nodes=[
            ("hello-elf-folder.drv", [1], False, None),
            ("gcc-runtime.drv", [], False, ("triple", "aarch64-linux")),
        ],
        built_from="aarch64-v0",
    )
    # Template C: same role + same enforce as A → must collapse.
    tmpl_c = _mk_template(
        nodes=[
            ("hello-elf-folder.drv", [1], False, None),
            ("gcc-runtime.drv", [], False, ("triple", "x86_64-linux")),
        ],
        built_from="i686-v0",
    )

    drv_rt_x86 = (make_hash_bytes(101), "gcc-runtime-14.0.drv")
    drv_rt_arm = (make_hash_bytes(102), "gcc-runtime-14.0.drv")

    arr_a = _mk_variant_array(0, "x86_64", 2, {1: drv_rt_x86})
    arr_b = _mk_variant_array(1, "aarch64", 2, {1: drv_rt_arm})
    arr_c = _mk_variant_array(0, "i686", 2, {1: drv_rt_x86})

    by_arch = {
        "x86_64": (tmpl_a, {1: "common_dep"}, arr_a),
        "aarch64": (tmpl_b, {1: "common_dep"}, arr_b),
        "i686": (tmpl_c, {1: "common_dep"}, arr_c),
    }
    canonical_root_role = "hello-elf-folder.drv"
    merged, key_class, _key_optional = build_merged_keymap(
        by_arch, canonical_root_role,
    )

    # Two distinct enforce tuples → two distinct keys for the same
    # role string.
    gcc_keys = [k for k in merged if k[0] == "gcc-runtime.drv"]
    assert len(gcc_keys) == 2, gcc_keys
    enforces = {k[1] for k in gcc_keys}
    assert enforces == {
        ("triple", "x86_64-linux"), ("triple", "aarch64-linux"),
    }

    # x86 enforce key has BOTH x86_64 and i686 archs (same enforce).
    x86_key = ("gcc-runtime.drv", ("triple", "x86_64-linux"))
    assert set(merged[x86_key]) == {"x86_64", "i686"}
    # aarch64 enforce key has only aarch64.
    arm_key = ("gcc-runtime.drv", ("triple", "aarch64-linux"))
    assert set(merged[arm_key]) == {"aarch64"}

    # Class priority: both contributing rows are common_dep → key_class
    # must be common_dep at both keys.
    assert key_class[x86_key] == "common_dep"
    assert key_class[arm_key] == "common_dep"


def test_canonical_walk_order_is_deterministic_and_structural():
    """``canonical_walk_order`` must produce identical output for the
    same input on repeat calls, AND must produce a STRUCTURALLY
    parallel walk for a structurally-equal template that uses
    different node ids and arch keys.

    The walk is DFS from the canonical root key with deduplicated
    child-key union across archs; both properties are part of its
    contract.
    """
    # Template shape: root → [a, b] ; a → [c] ; b → [c]. Diamond DAG.
    tmpl_x = _mk_template(
        nodes=[
            ("hello-elf-folder.drv", [1, 2], False, None),  # 0: root
            ("libalpha.drv", [3], False, None),             # 1: a
            ("libbeta.drv", [3], False, None),              # 2: b
            ("libshared.drv", [], False, None),             # 3: c
        ],
    )
    arr_x = _mk_variant_array(0, "x86_64", 4, {})
    by_arch = {"x86_64": (tmpl_x, {}, arr_x)}
    canonical_root_role = "hello-elf-folder.drv"
    merged, _kc, _ko = build_merged_keymap(by_arch, canonical_root_role)
    canonical_root_key = (canonical_root_role, None)

    order_1 = canonical_walk_order(merged, canonical_root_key)
    order_2 = canonical_walk_order(merged, canonical_root_key)
    assert order_1 == order_2

    expected = (
        ("hello-elf-folder.drv", None),
        ("libalpha.drv", None),
        ("libshared.drv", None),
        ("libbeta.drv", None),
    )
    assert order_1 == expected

    # Same shape via a different template instance (different node
    # ids and a different arch key) must yield the SAME ordering.
    tmpl_y = _mk_template(
        nodes=[
            ("hello-elf-folder.drv", [1, 2], False, None),
            ("libalpha.drv", [3], False, None),
            ("libbeta.drv", [3], False, None),
            ("libshared.drv", [], False, None),
        ],
    )
    arr_y = _mk_variant_array(1, "aarch64", 4, {})
    merged_y, _kc, _ko = build_merged_keymap(
        {"aarch64": (tmpl_y, {}, arr_y)}, canonical_root_role,
    )
    order_y = canonical_walk_order(merged_y, canonical_root_key)
    assert order_y == expected
