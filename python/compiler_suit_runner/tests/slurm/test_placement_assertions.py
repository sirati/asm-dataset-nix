"""Unit tests for :mod:`compiler_suit_runner.tests.slurm.placement_assertions`.

Hermetic: synthesise a :class:`RunArtifacts`-shaped layout under a
``tmp_path`` and exercise each assertion's pass / fail branches.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from compiler_suit_runner.tests.slurm.invariants import RunArtifacts
from compiler_suit_runner.tests.slurm.placement_assertions import (
    assert_placement_files_present_and_nonempty,
    assert_targeted_nix_copy_in_secondary_logs,
    assert_validate_manifests_emitted,
    parse_placement_records,
    scan_nix_copy_invocations,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run_layout(tmp_path: pathlib.Path):
    """Build a minimal RunArtifacts-shaped directory tree.

    Returns ``(run_dir, shared_fs, manifests_dir, peers_dir)``. The
    test populates whichever subset each scenario needs.
    """
    run_dir = tmp_path / "run_20260514_120000"
    run_dir.mkdir()
    shared_fs = tmp_path / "shared"
    manifests_dir = shared_fs / "manifests"
    manifests_dir.mkdir(parents=True)
    peers_dir = shared_fs / "peers"
    peers_dir.mkdir()
    return run_dir, shared_fs, manifests_dir, peers_dir


def _write_validate_manifest(
    manifests_dir: pathlib.Path,
    arch: str,
    compiler: str,
    drv: str,
    outpath: str,
) -> pathlib.Path:
    p = manifests_dir / f"toolchain_validate__{arch}__{compiler}.json"
    p.write_text(json.dumps({
        "item_class": "phase2_toolchain_validate",
        "name": f"toolchain_validate__{arch}__{compiler}",
        "size": 0,
        "payload": {
            "drv": drv,
            "outpath": outpath,
            "validate_only": True,
        },
    }))
    return p


def _write_build_manifest(
    manifests_dir: pathlib.Path,
    arch: str,
    compiler: str,
) -> pathlib.Path:
    p = manifests_dir / f"toolchain__{arch}__{compiler}.json"
    p.write_text(json.dumps({
        "item_class": "phase2_toolchain",
        "name": f"toolchain__{arch}__{compiler}",
        "size": 0,
        "payload": {"attr": "x"},
    }))
    return p


def _write_paths_file(
    peers_dir: pathlib.Path, sid: str, records: list[dict]
) -> pathlib.Path:
    p = peers_dir / f"_paths_{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


# ---------------------------------------------------------------------------
# assert_validate_manifests_emitted
# ---------------------------------------------------------------------------


def test_validate_manifests_emitted_passes(run_layout):
    run_dir, shared_fs, manifests_dir, _peers_dir = run_layout
    _write_validate_manifest(
        manifests_dir, "aarch64", "gcc15",
        drv="/nix/store/tc.drv",
        outpath="/nix/store/tc-out",
    )
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    assert_validate_manifests_emitted(artifacts)  # no raise


def test_validate_manifests_emitted_fails_when_build_present(run_layout):
    """Even a single build manifest is a regression — the operator
    didn't pass ``--allow-toolchain-build``, so build manifests must
    not be on disk."""
    run_dir, shared_fs, manifests_dir, _peers_dir = run_layout
    _write_validate_manifest(
        manifests_dir, "aarch64", "gcc15",
        drv="/nix/store/tc.drv",
        outpath="/nix/store/tc-out",
    )
    _write_build_manifest(manifests_dir, "armv7l", "gcc11")
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    with pytest.raises(AssertionError, match="phase2_toolchain_validate"):
        assert_validate_manifests_emitted(artifacts)


def test_validate_manifests_emitted_fails_when_none_present(run_layout):
    run_dir, shared_fs, _manifests_dir, _peers_dir = run_layout
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    with pytest.raises(AssertionError, match="no phase2_toolchain_validate"):
        assert_validate_manifests_emitted(artifacts)


def test_validate_manifests_emitted_fails_when_outpath_missing(run_layout):
    """The validate worker needs the outpath in the payload; a
    manifest without it is broken."""
    run_dir, shared_fs, manifests_dir, _peers_dir = run_layout
    p = manifests_dir / "toolchain_validate__aarch64__gcc15.json"
    p.write_text(json.dumps({
        "item_class": "phase2_toolchain_validate",
        "name": "x",
        "size": 0,
        "payload": {"drv": "/nix/store/tc.drv"},  # no outpath
    }))
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    with pytest.raises(AssertionError, match="payload.outpath"):
        assert_validate_manifests_emitted(artifacts)


# ---------------------------------------------------------------------------
# assert_placement_files_present_and_nonempty
# ---------------------------------------------------------------------------


def test_placement_files_assertion_passes_when_toolchain_record_present(
    run_layout,
):
    run_dir, shared_fs, _manifests_dir, peers_dir = run_layout
    _write_paths_file(peers_dir, "sec1", [
        {
            "secondary_id": "sec1",
            "outpath": "/nix/store/tc-out",
            "drv_path": "/nix/store/tc.drv",
            "item_class": "toolchain",
        },
    ])
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    assert_placement_files_present_and_nonempty(artifacts)  # no raise


def test_placement_files_assertion_passes_with_common_dep(run_layout):
    run_dir, shared_fs, _manifests_dir, peers_dir = run_layout
    _write_paths_file(peers_dir, "sec1", [
        {
            "secondary_id": "sec1",
            "outpath": "/nix/store/glibc-out",
            "drv_path": "/nix/store/glibc.drv",
            "item_class": "common_dep",
        },
    ])
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    assert_placement_files_present_and_nonempty(artifacts)


def test_placement_files_assertion_fails_when_no_files(run_layout):
    run_dir, shared_fs, _manifests_dir, _peers_dir = run_layout
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    with pytest.raises(AssertionError, match="_paths_"):
        assert_placement_files_present_and_nonempty(artifacts)


def test_placement_files_assertion_fails_without_shared_fs(run_layout):
    """Without ``shared_fs`` plumbed in we can't even look at the
    placement files; that's an integration-test setup bug."""
    run_dir, _shared_fs, _manifests_dir, _peers_dir = run_layout
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=None)
    with pytest.raises(AssertionError, match="shared_fs"):
        assert_placement_files_present_and_nonempty(artifacts)


def test_placement_files_assertion_fails_when_only_variant_records(
    run_layout,
):
    """Variant placements are out-of-scope for the current refactor;
    if the file exists but only carries variants, we treat the run
    as not having exercised the new path."""
    run_dir, shared_fs, _manifests_dir, peers_dir = run_layout
    _write_paths_file(peers_dir, "sec1", [
        {
            "secondary_id": "sec1",
            "outpath": "/nix/store/v-out",
            "drv_path": "/nix/store/v.drv",
            "item_class": "variant",
        },
    ])
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    with pytest.raises(AssertionError, match="toolchain or"):
        assert_placement_files_present_and_nonempty(artifacts)


def test_parse_placement_records_tolerates_bad_lines(tmp_path: pathlib.Path):
    p = tmp_path / "_paths_sec1.jsonl"
    p.write_text("\n".join([
        "",
        "{ not json",
        json.dumps({"secondary_id": "sec1", "outpath": "/nix/store/a"}),
        json.dumps([1, 2]),
        json.dumps({"secondary_id": "sec1", "outpath": "/nix/store/b"}),
    ]) + "\n")
    recs = parse_placement_records(p)
    assert len(recs) == 2
    assert {r["outpath"] for r in recs} == {"/nix/store/a", "/nix/store/b"}


def test_parse_placement_records_missing_file_returns_empty(
    tmp_path: pathlib.Path,
):
    assert parse_placement_records(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# assert_targeted_nix_copy_in_secondary_logs
# ---------------------------------------------------------------------------


def test_scan_nix_copy_extracts_url_and_path():
    lines = [
        "warning: nothing to see here",
        "+ nix --extra-experimental-features 'nix-command flakes' copy "
        "--from http://node2:5002 --no-check-sigs "
        "/nix/store/aaa-toolchain",
        "another unrelated line",
        "+ nix copy --from http://node3:5003 --no-substituters "
        "--no-check-sigs /nix/store/bbb-toolchain",
    ]
    hits = scan_nix_copy_invocations(lines)
    assert len(hits) == 2
    assert hits[0] == {
        "url": "http://node2:5002", "path": "/nix/store/aaa-toolchain",
    }
    assert hits[1] == {
        "url": "http://node3:5003", "path": "/nix/store/bbb-toolchain",
    }


def test_scan_nix_copy_skips_lines_without_no_check_sigs():
    """``--no-check-sigs`` is the targeted-fetch discriminator;
    lines without it are not from the validate/pre-fetch path
    and we intentionally don't count them."""
    lines = [
        # Missing ``--no-check-sigs`` -> not the targeted-fetch path.
        "+ nix copy --from http://node2:5002 /nix/store/aaa-toolchain",
    ]
    hits = scan_nix_copy_invocations(lines)
    assert hits == []


def test_targeted_nix_copy_assertion_passes_on_clean_log(run_layout):
    run_dir, shared_fs, _manifests_dir, _peers_dir = run_layout
    (run_dir / "slurm_42.out").write_text(
        "boot...\n"
        "+ nix copy --from http://node2:5002 --no-substituters "
        "--no-check-sigs /nix/store/aaa-toolchain\n"
        "done.\n"
    )
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    assert_targeted_nix_copy_in_secondary_logs(artifacts)  # no raise


def test_targeted_nix_copy_assertion_fails_when_no_copy_found(run_layout):
    run_dir, shared_fs, _manifests_dir, _peers_dir = run_layout
    (run_dir / "slurm_42.out").write_text("nothing useful here\n")
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    with pytest.raises(AssertionError, match="--no-check-sigs"):
        assert_targeted_nix_copy_in_secondary_logs(artifacts)


def test_targeted_nix_copy_assertion_fails_when_no_logs(run_layout):
    run_dir, shared_fs, _manifests_dir, _peers_dir = run_layout
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    with pytest.raises(AssertionError, match="slurm_"):
        assert_targeted_nix_copy_in_secondary_logs(artifacts)


def test_targeted_nix_copy_assertion_passes_with_some_retries(run_layout):
    """Allowed: a few paths needed multiple peers because the first
    candidate failed. Not allowed: every path needed multiple peers
    (placement map is broken)."""
    run_dir, shared_fs, _manifests_dir, _peers_dir = run_layout
    (run_dir / "slurm_42.out").write_text(
        # Path A: clean fetch from node2.
        "+ nix copy --from http://node2:5002 --no-substituters "
        "--no-check-sigs /nix/store/aaa\n"
        # Path B: retry — first node3 fails, then node4 succeeds.
        "+ nix copy --from http://node3:5003 --no-substituters "
        "--no-check-sigs /nix/store/bbb\n"
        "+ nix copy --from http://node4:5004 --no-substituters "
        "--no-check-sigs /nix/store/bbb\n"
    )
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    assert_targeted_nix_copy_in_secondary_logs(artifacts)  # no raise


def test_targeted_nix_copy_assertion_fails_when_every_path_needs_multiple_peers(
    run_layout,
):
    run_dir, shared_fs, _manifests_dir, _peers_dir = run_layout
    (run_dir / "slurm_42.out").write_text(
        # Path A: two sources.
        "+ nix copy --from http://node2:5002 --no-substituters "
        "--no-check-sigs /nix/store/aaa\n"
        "+ nix copy --from http://node3:5003 --no-substituters "
        "--no-check-sigs /nix/store/aaa\n"
        # Path B: two sources.
        "+ nix copy --from http://node2:5002 --no-substituters "
        "--no-check-sigs /nix/store/bbb\n"
        "+ nix copy --from http://node3:5003 --no-substituters "
        "--no-check-sigs /nix/store/bbb\n"
    )
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    with pytest.raises(AssertionError, match="placement map looks broken"):
        assert_targeted_nix_copy_in_secondary_logs(artifacts)
