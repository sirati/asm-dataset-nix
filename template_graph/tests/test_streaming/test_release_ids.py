from template_graph.streaming import plan_from_tree_streaming
from .fixtures import Node, make_hash, render_tree, simple_variant


def test_versioned_id_has_separate_templates_and_meta_groups():
    from template_graph.tree_walker import parse_variant_path
    default, extra = "hello", "hello--v2_12_1--rabc"
    matrices = []
    for i, package_id in enumerate((default, extra)):
        matrices.append(Node(make_hash(10+i), f"matrix-{package_id}.drv", children=[
            simple_variant(package_id, arch, seed_base=100+i*100+j*10, children=[
                Node(make_hash(500+i*100+j*10), f"{package_id}-variant-1.0.drv")
            ]) for j, arch in enumerate(("x86_64", "aarch64"))
        ]))
    root = Node(make_hash(1), "sum-root.drv", children=[Node(make_hash(5), "toolchains.drv"), *matrices])
    result = plan_from_tree_streaming(render_tree(root))
    groups = {}
    for i, template in enumerate(result["templates"]):
        owner = parse_variant_path(template.nodes[template.root_id].name)[0]
        groups.setdefault(owner, set()).add(i)
    assert set(groups) == {default, extra}
    assert len(groups[default]) == len(groups[extra]) == 2
    for package_id in (default, extra):
        for meta in result["meta_templates"][package_id]:
            assert set(meta.template_id_per_arch.values()) <= groups[package_id]


def test_singleton_keeps_distinct_recipes_with_the_same_role():
    first = Node(make_hash(700), "libexample-1.0.drv")
    second = Node(make_hash(701), "libexample-2.0.drv")
    matrix = Node(make_hash(2), "matrix-hello.drv", children=[
        simple_variant("hello", "x86_64", seed_base=100, children=[first, second])
    ])
    root = Node(make_hash(1), "sum-root.drv", children=[Node(make_hash(5), "toolchains.drv"), matrix])
    result = plan_from_tree_streaming(render_tree(root))
    arr = next(iter(result["variant_arrays"].values()))
    stored = {ident for row in arr.hashes for ident in row if ident is not None}
    assert first.ident in stored and second.ident in stored
