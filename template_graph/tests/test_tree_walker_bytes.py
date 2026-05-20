"""Byte-level parser tests: ``parse_line_bytes`` + ``drv_tree_stream``.

The byte parser is the streaming planner's hot path: it derives
``depth`` from the indent byte count minus twice the number of
``\\xe2`` lead bytes (each box-drawing connector is one codepoint
encoded as three bytes starting with ``\\xe2``), then slices out the
32-char hash and post-hash drv name without decoding the indent at
all. These tests pin the byte arithmetic and cross-check the bytes
parser against the canonical str-mode ``_parse_line`` over the
phase-3 corpus.
"""

from __future__ import annotations

import io
import itertools
from pathlib import Path

import pytest

from template_graph.tree_walker import (
    _parse_line,
    drv_tree_stream,
    parse_line_bytes,
)


_HASH = b"abc123def456ghi789jkl012mno345pq"  # 32 chars
assert len(_HASH) == 32
_NAME = "foo.drv"
_STORE = b"/nix/store/" + _HASH + b"-" + _NAME.encode("ascii")


# ─── 1. Table-driven depth ────────────────────────────────────────


# Hand-picked indent prefixes covering depths 0..8 plus the two
# 30-byte indent strings that disambiguate depth via the e2 byte
# count (5 connectors → depth 5; 7 connectors → depth 4).
_DEPTH_TABLE = [
    ("", 0),
    ("├───", 1),
    ("└───", 1),
    ("│   ├───", 2),
    ("    └───", 2),
    ("│   │   ├───", 3),
    ("│       └───", 3),
    ("    │   │   ├───", 4),
    ("│   │   │   ├───", 4),
    ("│   │   │   │   ├───", 5),
    ("            │   ├───", 5),
    ("│   │   │   │   │   └───", 6),
    ("│   │   │   │   │   │   ├───", 7),
    ("│   │   │   │   │   │   │   ├───", 8),
]


@pytest.mark.parametrize("indent,expected_depth", _DEPTH_TABLE)
def test_table_depth(indent: str, expected_depth: int) -> None:
    line = indent.encode("utf-8") + _STORE
    depth, h, name, br = parse_line_bytes(line)
    assert depth == expected_depth
    assert h == _HASH
    assert name == _NAME
    assert br is False


def test_30byte_indent_disambiguation() -> None:
    """Two indents have IDENTICAL byte length (30) but DIFFERENT depths.

    ``    | <space> | <space> | ├───`` (10 spaces + 1 connector +
    1 connector = pattern below) carries 5 e2 lead bytes → depth 5.
    ``│   │   │   ├───`` carries 7 e2 lead bytes → depth 4.
    """
    indent_a = "            │   ├───"  # 5×e2, depth 5
    indent_b = "│   │   │   ├───"      # 7×e2, depth 4
    ba = indent_a.encode("utf-8")
    bb = indent_b.encode("utf-8")
    assert len(ba) == len(bb) == 30
    assert ba.count(b"\xe2") == 5
    assert bb.count(b"\xe2") == 7
    assert parse_line_bytes(ba + _STORE)[0] == 5
    assert parse_line_bytes(bb + _STORE)[0] == 4


# ─── 2. Generative depth ──────────────────────────────────────────


@pytest.mark.parametrize("depth", list(range(1, 9)))
def test_generative_depth(depth: int) -> None:
    """Enumerate every ``itertools.product`` combo of indent/connector
    segments for the given depth and assert the parser returns it.

    For depth ``d`` the line has ``(d-1)`` indent segments
    (``"    "`` or ``"│   "``) followed by one connector
    (``"├───"`` or ``"└───"``). 2**d combos in total (max 256 at
    d=8); fast.
    """
    indents = ["    ", "│   "]
    connectors = ["├───", "└───"]
    combos = 0
    for prefix in itertools.product(indents, repeat=depth - 1):
        for conn in connectors:
            indent_str = "".join(prefix) + conn
            line = indent_str.encode("utf-8") + _STORE
            d, h, name, br = parse_line_bytes(line)
            assert d == depth, (indent_str, d, depth)
            assert h == _HASH
            assert name == _NAME
            assert br is False
            combos += 1
    assert combos == 2 ** depth


# ─── 3. Backref ───────────────────────────────────────────────────


_BACKREF_INDENTS = [
    ("", 0),
    ("│   ├───", 2),
    ("│   │   │   │   ├───", 5),
]


@pytest.mark.parametrize("indent,expected_depth", _BACKREF_INDENTS)
def test_backref(indent: str, expected_depth: int) -> None:
    line = indent.encode("utf-8") + _STORE + b" [...]"
    depth, h, name, br = parse_line_bytes(line)
    assert depth == expected_depth
    assert h == _HASH
    assert name == _NAME  # suffix stripped
    assert br is True


# ─── 4. Hash / name extraction ────────────────────────────────────


def test_hash_is_bytes_length_32_and_name_is_str() -> None:
    line = "├───".encode("utf-8") + b"/nix/store/" + _HASH + b"-hello-2.12.drv"
    depth, h, name, br = parse_line_bytes(line)
    assert isinstance(h, bytes)
    assert len(h) == 32
    assert h == _HASH
    assert isinstance(name, str)
    assert name == "hello-2.12.drv"
    assert depth == 1
    assert br is False


# ─── 5. Validation hard-errors ────────────────────────────────────


def test_misaligned_indent_raises() -> None:
    # 3 spaces (offset 3) before the store prefix → not divisible by 4.
    line = b"   /nix/store/" + _HASH + b"-foo.drv"
    with pytest.raises(ValueError):
        parse_line_bytes(line)


def test_indent_without_connector_raises() -> None:
    # 4 spaces, zero e2 connectors → no connector but offset > 0.
    line = b"    /nix/store/" + _HASH + b"-foo.drv"
    with pytest.raises(ValueError):
        parse_line_bytes(line)


def test_missing_store_prefix_raises() -> None:
    line = "├───".encode("utf-8") + b"/some/other/path/" + _HASH + b"-foo.drv"
    with pytest.raises(ValueError):
        parse_line_bytes(line)


def test_missing_store_prefix_no_indent_raises() -> None:
    line = b"just-some-bytes"
    with pytest.raises(ValueError):
        parse_line_bytes(line)


# ─── 6. Corpus parity ─────────────────────────────────────────────


_FIXTURE_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "python"
    / "compiler_suit_runner"
    / "tests"
    / "fixtures"
    / "phase3_debug_cases"
)


def _fixture_tree_files() -> list[Path]:
    if not _FIXTURE_ROOT.is_dir():
        return []
    return sorted(_FIXTURE_ROOT.glob("*/tree.txt"))


_FIXTURE_TREES = _fixture_tree_files()


@pytest.mark.skipif(
    not _FIXTURE_TREES,
    reason=f"no phase3_debug_cases fixtures under {_FIXTURE_ROOT}",
)
@pytest.mark.parametrize(
    "tree_path",
    _FIXTURE_TREES,
    ids=[p.parent.name for p in _FIXTURE_TREES],
)
def test_corpus_parity_with_parse_line(tree_path: Path) -> None:
    text = tree_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln]
    assert lines, f"empty fixture: {tree_path}"
    for ln in lines:
        s_depth, s_hash, s_name, s_br = _parse_line(ln)
        b_depth, b_hash, b_name, b_br = parse_line_bytes(
            ln.encode("utf-8")
        )
        assert s_depth == b_depth, ln
        assert s_name == b_name, ln
        assert s_br == b_br, ln
        # Both parsers return bytes for the hash after the str→bytes
        # migration; this parity check pins that.
        assert isinstance(s_hash, bytes), ln
        assert s_hash == b_hash, ln


def test_corpus_parity_at_least_six_fixtures() -> None:
    """Guard so we notice if the fixture set shrinks."""
    assert len(_FIXTURE_TREES) >= 6, (
        f"expected >=6 phase3 fixtures, got {len(_FIXTURE_TREES)} "
        f"under {_FIXTURE_ROOT}"
    )


# ─── 7. drv_tree_stream ───────────────────────────────────────────


def test_stream_matches_per_line_parse_minimal() -> None:
    """Hand-built corpus: a 3-line tree with root + child + backref."""
    lines = [
        b"/nix/store/" + _HASH + b"-sum-root.drv",
        b"\xe2\x94\x9c\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80"
        b"/nix/store/" + _HASH + b"-toolchains.drv",
        b"\xe2\x94\x94\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80"
        b"/nix/store/" + _HASH + b"-matrix-hello.drv [...]",
    ]
    blob = b"\n".join(lines) + b"\n"
    by_line = [parse_line_bytes(ln) for ln in lines]
    by_stream = list(drv_tree_stream(io.BytesIO(blob)))
    assert by_stream == by_line
    # Spot-check shape.
    assert [t[0] for t in by_stream] == [0, 1, 1]
    assert by_stream[-1][3] is True


def test_stream_without_trailing_newline() -> None:
    """drv_tree_stream must accept lines that the last record lacks
    a terminating newline (BytesIO splits at '\\n' but iterating a
    file object can yield the last partial line)."""
    lines = [
        b"/nix/store/" + _HASH + b"-root.drv\n",
        b"\xe2\x94\x9c\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80"
        b"/nix/store/" + _HASH + b"-child.drv",  # no \n
    ]
    out = list(drv_tree_stream(iter(lines)))
    assert len(out) == 2
    # Bytes parser strips trailing \n via 0x0a check, last line has none.
    assert out[0][2] == "root.drv"
    assert out[1][2] == "child.drv"
    assert out[1][0] == 1


@pytest.mark.skipif(
    not _FIXTURE_TREES,
    reason=f"no phase3_debug_cases fixtures under {_FIXTURE_ROOT}",
)
def test_stream_matches_per_line_parse_corpus() -> None:
    """Concatenate every fixture (with trailing newlines) into one
    BytesIO and assert ``drv_tree_stream`` yields the same tuple list
    as parsing each line individually."""
    chunks: list[bytes] = []
    expected: list[tuple[int, bytes, str, bool]] = []
    for tree in _FIXTURE_TREES:
        for ln in tree.read_text(encoding="utf-8").splitlines():
            if not ln:
                continue
            raw = ln.encode("utf-8")
            chunks.append(raw + b"\n")
            expected.append(parse_line_bytes(raw))
    streamed = list(drv_tree_stream(io.BytesIO(b"".join(chunks))))
    assert streamed == expected
