"""Tests for ``compiler_suit_runner.workers.partition_worker``.

The nix subprocess is always stubbed; tests run in milliseconds.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from compiler_suit_runner.partition import read_shard_outputs
from compiler_suit_runner.workers.partition_worker import (
    PHASE_1A_ITEM_CLASS,
    PartitionWorkerResult,
    WorkerEnv,
    extract_input_drvs,
    parse_manifest_payload,
    partition_worker,
    show_drv_recursive,
)


# ---------------------------------------------------------------------------
# Helpers


def _write_manifest(
    path: pathlib.Path,
    *,
    pkg: str = "hello",
    arch: str = "x86_64",
    variants: list[dict] | None = None,
    item_class: str = PHASE_1A_ITEM_CLASS,
) -> pathlib.Path:
    if variants is None:
        variants = [
            {
                "label": "hello-x86_64-gcc15-O2",
                "drv": "/nix/store/aaa-hello.drv",
                "tarball_name": "hello-x86_64-gcc15-O2.tar.zst",
                "compiler_id": "gcc15",
                "tier": 1,
            }
        ]
    payload = {
        "item_class": item_class,
        "pkg": pkg,
        "arch": arch,
        "variants": variants,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _FakeClock:
    """Monotonically advancing clock; each call increments by 1.0."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        current = self.now
        self.now += 1.0
        return current


class _StubRunner:
    """Stub for ``WorkerEnv.run_subprocess``.

    Maps drv path (the last element of the cmd) to a synthetic
    ``nix derivation show --recursive`` output. Records every call so
    tests can assert on call count / order.
    """

    def __init__(
        self,
        responses: dict[str, dict],
        *,
        fail_drv: str | None = None,
    ) -> None:
        self.responses = responses
        self.fail_drv = fail_drv
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(cmd))
        drv = cmd[-1]
        if drv == self.fail_drv:
            return b"", b"boom: simulated failure\n", 1
        if drv not in self.responses:
            return b"", f"unknown drv {drv!r}\n".encode(), 1
        body = json.dumps(self.responses[drv]).encode()
        return body, b"", 0


# ---------------------------------------------------------------------------
# parse_manifest_payload


def test_parse_manifest_payload_happy(tmp_path: pathlib.Path) -> None:
    manifest = _write_manifest(
        tmp_path / "shard.json",
        pkg="hello",
        arch="aarch64",
        variants=[
            {
                "label": "hello-aarch64-gcc15-O0",
                "drv": "/nix/store/aaa.drv",
                "tarball_name": "hello-aarch64-gcc15-O0.tar.zst",
                "compiler_id": "gcc15",
                "tier": 1,
            },
            {
                "label": "hello-aarch64-gcc15-O2",
                "drv": "/nix/store/bbb.drv",
                "tarball_name": "hello-aarch64-gcc15-O2.tar.zst",
                "compiler_id": "gcc15",
                "tier": 1,
            },
        ],
    )
    pkg, arch, variants = parse_manifest_payload(manifest)
    assert pkg == "hello"
    assert arch == "aarch64"
    assert [v["label"] for v in variants] == [
        "hello-aarch64-gcc15-O0",
        "hello-aarch64-gcc15-O2",
    ]
    # pkg/arch are propagated from the header onto every variant
    assert all(v["pkg"] == "hello" and v["arch"] == "aarch64" for v in variants)


def test_parse_manifest_payload_rejects_wrong_item_class(
    tmp_path: pathlib.Path,
) -> None:
    manifest = _write_manifest(
        tmp_path / "shard.json", item_class="phase3_variant"
    )
    with pytest.raises(ValueError, match="phase1a_partition"):
        parse_manifest_payload(manifest)


def test_parse_manifest_payload_rejects_missing_pkg(
    tmp_path: pathlib.Path,
) -> None:
    manifest = tmp_path / "shard.json"
    manifest.write_text(
        json.dumps(
            {
                "item_class": PHASE_1A_ITEM_CLASS,
                "arch": "x86_64",
                "variants": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'pkg'"):
        parse_manifest_payload(manifest)


def test_parse_manifest_payload_rejects_malformed_variant(
    tmp_path: pathlib.Path,
) -> None:
    manifest = _write_manifest(
        tmp_path / "shard.json",
        variants=[{"label": "x", "drv": "/nix/store/x.drv"}],
    )
    with pytest.raises(ValueError, match="missing field"):
        parse_manifest_payload(manifest)


# ---------------------------------------------------------------------------
# extract_input_drvs


def test_extract_input_drvs_array_schema() -> None:
    """Older inputDrvs schema: ``{drv: [outputs...]}``."""
    show = {
        "/nix/store/root.drv": {
            "inputDrvs": {
                "/nix/store/a.drv": ["out"],
                "/nix/store/b.drv": ["out"],
            },
        },
        "/nix/store/a.drv": {"inputDrvs": {}},
        "/nix/store/b.drv": {"inputDrvs": {}},
    }
    inputs = extract_input_drvs(show, "/nix/store/root.drv")
    assert inputs == ["/nix/store/a.drv", "/nix/store/b.drv"]


def test_extract_input_drvs_nested_schema() -> None:
    """Newer inputDrvs schema: ``{drv: {dynamicOutputs:..., outputs:...}}``."""
    show = {
        "/nix/store/root.drv": {
            "inputDrvs": {
                "/nix/store/a.drv": {
                    "dynamicOutputs": {},
                    "outputs": ["out"],
                },
                "/nix/store/b.drv": {
                    "dynamicOutputs": {},
                    "outputs": ["out"],
                },
            },
        },
        "/nix/store/a.drv": {"inputDrvs": {}},
        "/nix/store/b.drv": {"inputDrvs": {}},
    }
    inputs = extract_input_drvs(show, "/nix/store/root.drv")
    assert inputs == ["/nix/store/a.drv", "/nix/store/b.drv"]


def test_extract_input_drvs_three_level_transitive() -> None:
    """A chain root -> a -> b -> c surfaces all three transitive deps."""
    show = {
        "/nix/store/root.drv": {
            "inputDrvs": {"/nix/store/a.drv": ["out"]},
        },
        "/nix/store/a.drv": {
            "inputDrvs": {"/nix/store/b.drv": ["out"]},
        },
        "/nix/store/b.drv": {
            "inputDrvs": {"/nix/store/c.drv": ["out"]},
        },
        "/nix/store/c.drv": {"inputDrvs": {}},
    }
    inputs = extract_input_drvs(show, "/nix/store/root.drv")
    assert inputs == [
        "/nix/store/a.drv",
        "/nix/store/b.drv",
        "/nix/store/c.drv",
    ]


def test_extract_input_drvs_excludes_root_and_dedupes() -> None:
    """Diamond graph: root -> {a, b}, a -> c, b -> c. Root is excluded; c once."""
    show = {
        "/nix/store/root.drv": {
            "inputDrvs": {
                "/nix/store/a.drv": ["out"],
                "/nix/store/b.drv": ["out"],
            },
        },
        "/nix/store/a.drv": {
            "inputDrvs": {"/nix/store/c.drv": ["out"]},
        },
        "/nix/store/b.drv": {
            "inputDrvs": {"/nix/store/c.drv": ["out"]},
        },
        "/nix/store/c.drv": {"inputDrvs": {}},
    }
    inputs = extract_input_drvs(show, "/nix/store/root.drv")
    assert inputs == [
        "/nix/store/a.drv",
        "/nix/store/b.drv",
        "/nix/store/c.drv",
    ]


def test_extract_input_drvs_handles_cycle() -> None:
    """A cycle a <-> b must not loop forever."""
    show = {
        "/nix/store/root.drv": {
            "inputDrvs": {"/nix/store/a.drv": ["out"]},
        },
        "/nix/store/a.drv": {
            "inputDrvs": {"/nix/store/b.drv": ["out"]},
        },
        "/nix/store/b.drv": {
            "inputDrvs": {"/nix/store/a.drv": ["out"]},
        },
    }
    inputs = extract_input_drvs(show, "/nix/store/root.drv")
    assert inputs == ["/nix/store/a.drv", "/nix/store/b.drv"]


# ---------------------------------------------------------------------------
# show_drv_recursive


def test_show_drv_recursive_invokes_nix_with_correct_args(
    tmp_path: pathlib.Path,
) -> None:
    response = {"/nix/store/x.drv": {"inputDrvs": {}}}
    runner = _StubRunner({"/nix/store/x.drv": response})
    env = WorkerEnv(
        raw_partition_dir=tmp_path,
        flake_ref=".",
        run_subprocess=runner,
    )
    out = show_drv_recursive("/nix/store/x.drv", env)
    assert out == response
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[0] == "nix"
    assert "derivation" in cmd
    assert "show" in cmd
    assert "--recursive" in cmd
    assert cmd[-1] == "/nix/store/x.drv"


def test_show_drv_recursive_raises_on_failure(
    tmp_path: pathlib.Path,
) -> None:
    runner = _StubRunner({}, fail_drv="/nix/store/x.drv")
    env = WorkerEnv(
        raw_partition_dir=tmp_path,
        flake_ref=".",
        run_subprocess=runner,
    )
    with pytest.raises(RuntimeError, match="boom"):
        show_drv_recursive("/nix/store/x.drv", env)


# ---------------------------------------------------------------------------
# partition_worker


def test_partition_worker_end_to_end(tmp_path: pathlib.Path) -> None:
    """3 variants, each with a different input set; one shared input."""
    drvs = {
        "/nix/store/v1.drv": {
            "/nix/store/v1.drv": {
                "inputDrvs": {
                    "/nix/store/shared.drv": ["out"],
                    "/nix/store/only-v1.drv": ["out"],
                }
            },
            "/nix/store/shared.drv": {"inputDrvs": {}},
            "/nix/store/only-v1.drv": {"inputDrvs": {}},
        },
        "/nix/store/v2.drv": {
            "/nix/store/v2.drv": {
                "inputDrvs": {
                    "/nix/store/shared.drv": ["out"],
                    "/nix/store/only-v2.drv": ["out"],
                }
            },
            "/nix/store/shared.drv": {"inputDrvs": {}},
            "/nix/store/only-v2.drv": {"inputDrvs": {}},
        },
        "/nix/store/v3.drv": {
            "/nix/store/v3.drv": {
                "inputDrvs": {
                    "/nix/store/shared.drv": ["out"],
                    "/nix/store/only-v3.drv": ["out"],
                }
            },
            "/nix/store/shared.drv": {"inputDrvs": {}},
            "/nix/store/only-v3.drv": {"inputDrvs": {}},
        },
    }
    runner = _StubRunner(drvs)

    manifest = _write_manifest(
        tmp_path / "shard.json",
        pkg="hello",
        arch="aarch64",
        variants=[
            {
                "label": "hello-aarch64-gcc15-O0",
                "drv": "/nix/store/v1.drv",
                "tarball_name": "hello-aarch64-gcc15-O0.tar.zst",
                "compiler_id": "gcc15",
                "tier": 1,
            },
            {
                "label": "hello-aarch64-gcc15-O2",
                "drv": "/nix/store/v2.drv",
                "tarball_name": "hello-aarch64-gcc15-O2.tar.zst",
                "compiler_id": "gcc15",
                "tier": 1,
            },
            {
                "label": "hello-aarch64-gcc15-O3",
                "drv": "/nix/store/v3.drv",
                "tarball_name": "hello-aarch64-gcc15-O3.tar.zst",
                "compiler_id": "gcc15",
                "tier": 1,
            },
        ],
    )

    env = WorkerEnv(
        raw_partition_dir=tmp_path / "raw",
        flake_ref=".",
        run_subprocess=runner,
        clock=_FakeClock(),
    )
    result = partition_worker(manifest, env)

    assert isinstance(result, PartitionWorkerResult)
    assert result.error is None
    assert result.shard_name == "hello__aarch64"
    assert result.variant_count == 3
    # `--recursive` returns the full transitive sub-graph in one call;
    # we expect exactly one nix call per variant root.
    assert result.nix_calls == 3
    assert result.duration_seconds > 0
    assert result.output_path is not None
    assert result.output_path.exists()

    # Round-trip the on-disk shard.
    [shard] = read_shard_outputs(tmp_path / "raw")
    assert shard.shard_name == "hello__aarch64"
    assert shard.variant_to_input_drvs == {
        "hello-aarch64-gcc15-O0": [
            "/nix/store/only-v1.drv",
            "/nix/store/shared.drv",
        ],
        "hello-aarch64-gcc15-O2": [
            "/nix/store/only-v2.drv",
            "/nix/store/shared.drv",
        ],
        "hello-aarch64-gcc15-O3": [
            "/nix/store/only-v3.drv",
            "/nix/store/shared.drv",
        ],
    }


def test_partition_worker_failing_nix_call_records_error(
    tmp_path: pathlib.Path,
) -> None:
    runner = _StubRunner({}, fail_drv="/nix/store/v1.drv")
    manifest = _write_manifest(
        tmp_path / "shard.json",
        pkg="hello",
        arch="x86_64",
        variants=[
            {
                "label": "hello-x86_64-gcc15-O0",
                "drv": "/nix/store/v1.drv",
                "tarball_name": "hello-x86_64-gcc15-O0.tar.zst",
                "compiler_id": "gcc15",
                "tier": 1,
            }
        ],
    )
    env = WorkerEnv(
        raw_partition_dir=tmp_path / "raw",
        flake_ref=".",
        run_subprocess=runner,
        clock=_FakeClock(),
    )

    # Must not raise.
    result = partition_worker(manifest, env)

    assert result.error is not None
    assert "boom" in result.error
    assert result.output_path is None
    # No partial shard JSON written.
    assert not (tmp_path / "raw" / "hello__x86_64.json").exists()
    # The shard_name is still set from the manifest's pkg/arch.
    assert result.shard_name == "hello__x86_64"


def test_partition_worker_invalid_manifest_records_error(
    tmp_path: pathlib.Path,
) -> None:
    """Top-level item_class mismatch surfaces as a non-raising error."""
    bad_manifest = _write_manifest(
        tmp_path / "shard.json", item_class="phase3_variant"
    )
    runner = _StubRunner({})
    env = WorkerEnv(
        raw_partition_dir=tmp_path / "raw",
        flake_ref=".",
        run_subprocess=runner,
        clock=_FakeClock(),
    )

    result = partition_worker(bad_manifest, env)
    assert result.error is not None
    assert "phase1a_partition" in result.error
    assert result.output_path is None
    assert result.nix_calls == 0
    assert runner.calls == []


def test_partition_worker_creates_output_dir(tmp_path: pathlib.Path) -> None:
    """``raw_partition_dir`` is created on demand."""
    drvs = {
        "/nix/store/v1.drv": {
            "/nix/store/v1.drv": {"inputDrvs": {}},
        }
    }
    runner = _StubRunner(drvs)
    manifest = _write_manifest(
        tmp_path / "shard.json",
        pkg="busybox",
        arch="armv7l",
        variants=[
            {
                "label": "busybox-armv7l-gcc15-Os",
                "drv": "/nix/store/v1.drv",
                "tarball_name": "busybox-armv7l-gcc15-Os.tar.zst",
                "compiler_id": "gcc15",
                "tier": 1,
            }
        ],
    )
    output_dir = tmp_path / "deeply" / "nested" / "raw"
    assert not output_dir.exists()

    env = WorkerEnv(
        raw_partition_dir=output_dir,
        flake_ref=".",
        run_subprocess=runner,
    )
    result = partition_worker(manifest, env)

    assert result.error is None
    assert output_dir.exists()
    assert (output_dir / "busybox__armv7l.json").exists()
