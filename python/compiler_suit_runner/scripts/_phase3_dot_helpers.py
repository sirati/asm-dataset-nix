"""Helpers for the phase-3 dot demo driver.

Split out so the driver script itself stays under the 300-line file
budget. Each helper is a stand-alone primitive (nix-eval shells,
per-binary sample, per-binary eval-task driver, parallel fan-out) the
driver composes.
"""

from __future__ import annotations

import concurrent.futures
import pathlib

from compiler_suit_runner._nix_eval_utils import (
    nix_eval_json,
    resolve_bash_drv_path,
)
from compiler_suit_runner.manifest_gen import make_matrix_eval_header
from compiler_suit_runner.peer_replication import BroadcastSender
from compiler_suit_runner.workers.eval_worker import (
    run_eval_task,
    sample_suffix_attrs,
)


# Calibration suffix family the phase3 smoke fixture uses; both
# baseline + noinline at san-off march-default ensure the streaming
# planner's classifier always sees a calibration pair per arch.
SUFFIX_MATCH_PATTERN = (
    ".*-(baseline-default|noinline-default)-san-off-march-default"
)


def resolve_bash_path() -> str:
    """Production probe: ``nix eval --raw nixpkgs#bash.outPath``.

    Thin wrapper over
    :func:`compiler_suit_runner._nix_eval_utils.resolve_bash_drv_path`
    kept under the original name so the phase3-dot-demo driver script
    keeps importing it as ``resolve_bash_path``.
    """
    return resolve_bash_drv_path()


def sampled_suffixes_for_binary(
    *,
    binary: str,
    archs: list[str],
    sample_size: int,
    sample_seed: str,
    sys_name: str,
    root: pathlib.Path,
) -> list[str]:
    """Per-arch sampled suffix union for one binary; same shortcut
    ``preflight.enumerate_variants`` takes (driver pre-samples, payload
    carries ``variant_sample=0``)."""
    apply_expr = (
        f'm: builtins.listToAttrs (builtins.map '
        f'(s: {{ name = s; value = m.${{s}}; }}) '
        f'(builtins.filter '
        f'(s: builtins.match "{SUFFIX_MATCH_PATTERN}" s != null) '
        f'(builtins.attrNames m)))'
    )
    union: set[str] = set()
    for arch in archs:
        meta = nix_eval_json(
            f"_meta.{sys_name}.{binary}.{arch}",
            root=root, apply=apply_expr,
        )
        if not isinstance(meta, dict):
            continue
        sampled = sample_suffix_attrs(
            meta, arch=arch,
            sample_size=sample_size, seed=sample_seed,
        )
        union.update(sampled.keys())
    return sorted(union)


def run_eval_for_binary(
    *,
    binary: str,
    archs: list[str],
    sys_name: str,
    sample_size: int,
    sample_seed: str,
    toolchain_aggregate_drv: str,
    archive_dir: pathlib.Path,
    flake_ref: str,
    root: pathlib.Path,
) -> dict:
    """Drive ``run_eval_task`` for one binary. BroadcastSender uses an
    empty peer-url provider so each broadcast ack's 0/0 success
    without network I/O while the sender thread + enqueue/wait cycle
    still run."""
    suffixes = sampled_suffixes_for_binary(
        binary=binary, archs=archs,
        sample_size=sample_size, sample_seed=sample_seed,
        sys_name=sys_name, root=root,
    )
    if not suffixes:
        raise RuntimeError(
            f"phase3-dot-demo: no suffixes survived sampling for "
            f"{binary!r} on archs={archs}"
        )
    header = make_matrix_eval_header(
        binary=binary, sys_name=sys_name, archs=archs,
        suffixes=suffixes,
        toolchain_aggregate_drv=toolchain_aggregate_drv,
        variant_sample=0, variant_seed=sample_seed,
    )
    sender = BroadcastSender(
        self_peer_id="phase3-dot-demo",
        peer_url_provider=lambda: [],
    )
    try:
        result = run_eval_task(
            payload=dict(header.payload),
            out_dir=archive_dir,
            broadcast_sender=sender,
            flake_ref=flake_ref,
            broadcast_timeout=2.0,
        )
    finally:
        sender.stop()
    if not result.get("matrix_aggregate_drv"):
        raise RuntimeError(
            f"phase3-dot-demo: run_eval_task produced no "
            f"matrix_aggregate_drv for {binary!r}; result={result!r}"
        )
    return result


def eval_all_binaries(
    *,
    binaries: list[str],
    archs: list[str],
    sys_name: str,
    sample_size: int,
    sample_seed: str,
    toolchain_aggregate_drv: str,
    archive_dir: pathlib.Path,
    flake_ref: str,
    root: pathlib.Path,
) -> dict[str, str]:
    """Run each binary's eval pipeline in parallel; collect aggregate
    drvs. Production dispatches one matrix_eval task per binary; the
    thread pool merely lifts that fan-out into the driver process."""
    matrix_aggregates: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(binaries)),
    ) as ex:
        futures = {
            ex.submit(
                run_eval_for_binary,
                binary=binary, archs=archs, sys_name=sys_name,
                sample_size=sample_size, sample_seed=sample_seed,
                toolchain_aggregate_drv=toolchain_aggregate_drv,
                archive_dir=archive_dir, flake_ref=flake_ref, root=root,
            ): binary for binary in binaries
        }
        for future in concurrent.futures.as_completed(futures):
            binary = futures[future]
            eval_result = future.result()
            matrix_aggregates[binary] = eval_result["matrix_aggregate_drv"]
            archive = archive_dir / f"{binary}.nix-archive"
            if not archive.is_file() or archive.stat().st_size == 0:
                raise RuntimeError(
                    f"phase3-dot-demo: missing/empty archive for "
                    f"{binary!r} at {archive!s}"
                )
    return matrix_aggregates
