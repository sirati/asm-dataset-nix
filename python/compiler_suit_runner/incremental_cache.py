"""Local on-disk cache for the runner's pre-flight outputs.

Caches partition.json, manifests, and _meta keyed by an input_hash that
captures the flake state (flake.lock + git rev + git diff) PLUS the
invocation axes that shape what the pre-flight produces (packages,
archs, variant sampling, sys_name, build-compilers mode, ...).
Re-running the runner on an unchanged flake with the SAME invocation
should NOT redo the ~5-min local pre-flight + ~hours of cluster Phase
1a/1b; a different invocation (e.g. a different ``--packages`` set)
must MISS even on identical repo state — without the axes in the key,
a 16-binary dispatch can silently reuse a prior nano run's entry and
plan only the nano's tasks.

Cache layout::

    <cache_root>/<input_hash>/
        partition.json
        manifests.tar
        meta.json

All writes are atomic at the entry-dir level: writes go into
``<cache_root>/<input_hash>.tmp/`` first, then ``os.replace`` makes the
final directory visible. Partial writes never leave a half-cached entry
that ``.lookup`` would consider complete.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
from typing import Callable, Optional, Sequence

DEFAULT_CACHE_ROOT = pathlib.Path.home() / ".cache" / "compiler_suit_runner"

# Type aliases for the optional dependency-injection seams used in tests.
RunSubprocess = Callable[..., "subprocess.CompletedProcess[bytes]"]
ReadBytes = Callable[[pathlib.Path], bytes]


def _canonical_str_tuple(
    values: Optional[Sequence[str]],
) -> Optional[tuple[str, ...]]:
    """Canonicalize a CLI multi-value axis: sorted, de-duplicated tuple.

    ``None`` (axis not constrained, i.e. "all") is preserved as-is and
    stays distinct from an explicit empty/short list.
    """
    if values is None:
        return None
    return tuple(sorted(set(values)))


@dataclasses.dataclass(frozen=True)
class InvocationAxes:
    """The invocation parameters that shape the pre-flight outputs.

    Every CLI axis that changes what the pre-flight / matrix planning
    produces MUST be represented here, otherwise two different
    invocations on the same repo state collide on one cache entry
    (the observed failure: a 16-binary run cache-hitting a prior nano
    run's entry and planning only the nano's tasks).

    Axes and why they are included:

    * ``packages`` — ``--packages``: selects which binaries are
      enumerated for matrix_eval.
    * ``archs`` — ``--archs``: restricts both the toolchain enumeration
      and the per-binary variant matrix.
    * ``variant_sample`` — ``--variant-sample``: down-samples the
      variant matrix.
    * ``variant_seed`` — ``--variant-seed``: reshuffles the sample.
    * ``sys_name`` — ``--system``: flake system attribute everything is
      evaluated against.
    * ``build_compilers`` — ``--build-compilers``: flips manifest
      emission between ``build_compilers`` and ``toolchain_validate``
      classes and is persisted as ``allow_toolchain_build`` in the
      cached preflight descriptor.
    * ``debug_testbuild`` — ``--debug-testbuild``: adds the
      build_compilers stage / validation binary.
    * ``toolchain_dedup`` — ``--no-toolchain-dedup`` /
      ``CSR_TOOLCHAIN_DEDUP``: changes the per-binary matrix_eval
      payloads (full-closure vs diff archives).

    Deliberately excluded: ``--jobs`` (num_workers no longer affects
    emitted manifests), ``--flake`` (the repo state behind the flake ref
    is already captured by flake.lock + git rev + git diff), and
    ``--max-variants`` (deprecated no-op).

    ``packages`` / ``archs`` use ``None`` for "all" (the CLI default);
    explicit lists are canonicalized via :func:`_canonical_str_tuple`
    so flag ordering and duplicates do not change the hash.
    """

    packages: Optional[tuple[str, ...]] = None
    archs: Optional[tuple[str, ...]] = None
    variant_sample: int = 0
    variant_seed: str = "42"
    sys_name: str = "x86_64-linux"
    build_compilers: bool = False
    debug_testbuild: Optional[str] = None
    toolchain_dedup: bool = True

    @classmethod
    def from_values(
        cls,
        *,
        packages: Optional[Sequence[str]] = None,
        archs: Optional[Sequence[str]] = None,
        variant_sample: int = 0,
        variant_seed: str = "42",
        sys_name: str = "x86_64-linux",
        build_compilers: bool = False,
        debug_testbuild: Optional[str] = None,
        toolchain_dedup: bool = True,
    ) -> "InvocationAxes":
        """Build axes from raw CLI values, canonicalizing the
        order-insensitive multi-value fields."""
        return cls(
            packages=_canonical_str_tuple(packages),
            archs=_canonical_str_tuple(archs),
            variant_sample=int(variant_sample),
            variant_seed=str(variant_seed),
            sys_name=str(sys_name),
            build_compilers=bool(build_compilers),
            debug_testbuild=(
                str(debug_testbuild) if debug_testbuild is not None else None
            ),
            toolchain_dedup=bool(toolchain_dedup),
        )

    def canonical_bytes(self) -> bytes:
        """Deterministic, order-stable serialization for hashing.

        JSON with sorted keys and fixed separators; tuples serialize as
        JSON arrays in their (already canonical) order, ``None`` as
        ``null`` — distinct from any explicit list.
        """
        return json.dumps(
            dataclasses.asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class InputHashInputs:
    """The sources combined into the cache key.

    Three repo-state inputs (bytes-or-strings to keep the hash
    deterministic) plus the canonical serialization of the invocation
    axes (see :class:`InvocationAxes`).
    """

    flake_lock: bytes  # contents of flake.lock
    git_rev: str  # git rev-parse HEAD output (40-char hex, stripped)
    git_diff: bytes  # git diff worktree contents (may be empty)
    # Canonical bytes of the invocation axes (InvocationAxes
    # .canonical_bytes()). Defaults to empty for callers that key on
    # repo state only (legacy / tests).
    invocation: bytes = b""


def compute_input_hash(inputs: InputHashInputs) -> str:
    """Return ``sha256`` hex of length-prefixed concatenation of the
    inputs.

    Length prefixes prevent boundary-confusion attacks (e.g. a flake.lock
    ending where ``git_rev`` starts producing the same hash as a different
    split). Format::

        sha256( b"flake_lock:" + len(flake_lock).to_bytes(8,'big') + flake_lock
              + b"git_rev:"    + len(git_rev_bytes).to_bytes(8,'big') + git_rev_bytes
              + b"git_diff:"   + len(git_diff).to_bytes(8,'big') + git_diff
              + b"invocation:" + len(invocation).to_bytes(8,'big') + invocation )
    """
    h = hashlib.sha256()
    git_rev_bytes = inputs.git_rev.encode("utf-8")

    h.update(b"flake_lock:")
    h.update(len(inputs.flake_lock).to_bytes(8, "big"))
    h.update(inputs.flake_lock)

    h.update(b"git_rev:")
    h.update(len(git_rev_bytes).to_bytes(8, "big"))
    h.update(git_rev_bytes)

    h.update(b"git_diff:")
    h.update(len(inputs.git_diff).to_bytes(8, "big"))
    h.update(inputs.git_diff)

    h.update(b"invocation:")
    h.update(len(inputs.invocation).to_bytes(8, "big"))
    h.update(inputs.invocation)

    return h.hexdigest()


def _default_run_subprocess(
    cmd: list[str], *, cwd: pathlib.Path
) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, check=False)


def _default_read_bytes(path: pathlib.Path) -> bytes:
    return path.read_bytes()


def collect_input_hash_inputs(
    repo_root: pathlib.Path,
    *,
    invocation: Optional["InvocationAxes"] = None,
    run_subprocess: Optional[RunSubprocess] = None,
    read_bytes: Optional[ReadBytes] = None,
) -> InputHashInputs:
    """Collect the inputs for ``compute_input_hash``.

    Reads ``flake.lock`` from disk; calls ``git rev-parse HEAD`` and
    ``git diff`` via ``run_subprocess`` (default: :func:`subprocess.run`).
    If git is missing or ``repo_root`` is not a git repo, raises
    :class:`RuntimeError`. Use ``read_bytes`` (default
    :meth:`pathlib.Path.read_bytes`) for testability.

    ``invocation`` carries the invocation axes (:class:`InvocationAxes`)
    that shape the pre-flight outputs; when omitted, the key covers repo
    state only (legacy behaviour — callers caching pre-flight artifacts
    MUST pass it).
    """
    if run_subprocess is None:
        run_subprocess = _default_run_subprocess
    if read_bytes is None:
        read_bytes = _default_read_bytes

    flake_lock_path = repo_root / "flake.lock"
    try:
        flake_lock = read_bytes(flake_lock_path)
    except OSError as e:
        raise RuntimeError(
            f"failed to read flake.lock at {flake_lock_path}: {e}"
        ) from e

    try:
        rev_proc = run_subprocess(
            ["git", "rev-parse", "HEAD"], cwd=repo_root
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"git not found: {e}") from e

    if rev_proc.returncode != 0:
        stderr = (rev_proc.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(
            f"git rev-parse HEAD failed in {repo_root} "
            f"(returncode={rev_proc.returncode}): {stderr.strip()}"
        )

    git_rev = (rev_proc.stdout or b"").decode("utf-8", errors="replace").strip()

    try:
        diff_proc = run_subprocess(["git", "diff"], cwd=repo_root)
    except FileNotFoundError as e:
        raise RuntimeError(f"git not found: {e}") from e

    if diff_proc.returncode != 0:
        stderr = (diff_proc.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(
            f"git diff failed in {repo_root} "
            f"(returncode={diff_proc.returncode}): {stderr.strip()}"
        )

    git_diff = diff_proc.stdout or b""

    return InputHashInputs(
        flake_lock=flake_lock,
        git_rev=git_rev,
        git_diff=git_diff,
        invocation=(
            invocation.canonical_bytes() if invocation is not None else b""
        ),
    )


@dataclasses.dataclass
class CacheEntry:
    """One materialized cache entry on disk."""

    input_hash: str
    partition_path: pathlib.Path
    manifests_archive: pathlib.Path
    meta_path: pathlib.Path

    @property
    def is_complete(self) -> bool:
        """True iff all three files exist."""
        return (
            self.partition_path.is_file()
            and self.manifests_archive.is_file()
            and self.meta_path.is_file()
        )


def _fsync_dir(path: pathlib.Path) -> None:
    """Best-effort fsync of a directory so the rename is durable.

    On platforms where ``O_DIRECTORY`` opening for fsync is unsupported,
    silently skip. The atomicity of the rename itself is what matters
    for the cache; fsync is for crash durability.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class IncrementalCache:
    """File-based cache of pre-flight outputs.

    File layout (per the plan)::

        <cache_root>/<input_hash>/
            partition.json
            manifests.tar
            meta.json

    All writes are atomic at the entry-dir level (write to
    ``<hash>.tmp/``, then :func:`os.replace`); partial writes never
    leave a half-cached entry that :meth:`lookup` would consider
    complete.
    """

    PARTITION_NAME = "partition.json"
    MANIFESTS_NAME = "manifests.tar"
    META_NAME = "meta.json"

    def __init__(self, cache_root: pathlib.Path = DEFAULT_CACHE_ROOT) -> None:
        self.cache_root = pathlib.Path(cache_root)

    # ------------------------------------------------------------------ paths

    def _entry_dir(self, input_hash: str) -> pathlib.Path:
        return self.cache_root / input_hash

    def _entry_for(self, input_hash: str) -> CacheEntry:
        d = self._entry_dir(input_hash)
        return CacheEntry(
            input_hash=input_hash,
            partition_path=d / self.PARTITION_NAME,
            manifests_archive=d / self.MANIFESTS_NAME,
            meta_path=d / self.META_NAME,
        )

    # ------------------------------------------------------------------ api

    def lookup(self, input_hash: str) -> Optional[CacheEntry]:
        """Return the cache entry for ``input_hash`` if all three files
        exist; otherwise ``None``.

        ``None`` is returned both when the entry directory does not
        exist at all and when only some of the three files are present
        (the latter shouldn't happen under normal use given the atomic
        store, but we defensively treat it as a miss).
        """
        entry = self._entry_for(input_hash)
        if not self._entry_dir(input_hash).is_dir():
            return None
        if not entry.is_complete:
            return None
        return entry

    def store(
        self,
        input_hash: str,
        partition_path: pathlib.Path,
        manifests_dir: pathlib.Path,
        meta_path: pathlib.Path,
    ) -> CacheEntry:
        """Store the pre-flight outputs for ``input_hash``.

        Copies ``partition_path`` and ``meta_path`` into the entry
        directory and packs ``manifests_dir`` into ``manifests.tar``
        (uncompressed; manifests are tiny). The write is atomic:
        everything is staged into a sibling ``.tmp`` directory and then
        renamed.

        If another process already populated the target directory while
        we were writing, the existing one is preserved and the tmp
        directory is cleaned up.

        Returns the resulting :class:`CacheEntry` (existing or new).
        """
        partition_path = pathlib.Path(partition_path)
        manifests_dir = pathlib.Path(manifests_dir)
        meta_path = pathlib.Path(meta_path)

        self.cache_root.mkdir(parents=True, exist_ok=True)

        target_dir = self._entry_dir(input_hash)
        # Use a hash-suffixed tmp dir so concurrent stores don't collide
        # at the tmp level either. The pid keeps multiple in-flight
        # stores in the same process from overwriting each other (rare
        # but possible if a caller does it).
        tmp_dir = self.cache_root / f"{input_hash}.tmp.{os.getpid()}"

        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)

        try:
            # Copy partition.json and meta.json.
            shutil.copyfile(partition_path, tmp_dir / self.PARTITION_NAME)
            shutil.copyfile(meta_path, tmp_dir / self.META_NAME)

            # Pack manifests_dir into manifests.tar (uncompressed).
            manifests_archive = tmp_dir / self.MANIFESTS_NAME
            with tarfile.open(manifests_archive, mode="w") as tf:
                # arcname="manifests" keeps the archive self-describing
                # without leaking absolute paths from the caller's FS.
                if manifests_dir.exists():
                    tf.add(str(manifests_dir), arcname="manifests")
                # If the dir doesn't exist, an empty tar is fine — the
                # caller passed no manifests. Don't fail loudly here;
                # the real correctness check is at lookup time.

            _fsync_dir(tmp_dir)

            # Atomic rename. os.replace() is atomic on the same FS but
            # will REPLACE an existing target on POSIX. We want
            # store-doesn't-clobber semantics, so race-check first via
            # os.rename which fails if target is a non-empty dir on
            # Linux, OR check explicitly. We use the explicit check
            # because it's portable and the race window is small.
            if target_dir.exists():
                # Someone else got here first. Discard our tmp dir.
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return self._entry_for(input_hash)

            try:
                os.replace(str(tmp_dir), str(target_dir))
            except OSError:
                # Lost the race between the .exists() check and the
                # rename. Clean up tmp and return existing entry.
                shutil.rmtree(tmp_dir, ignore_errors=True)
                if target_dir.exists():
                    return self._entry_for(input_hash)
                raise

            _fsync_dir(self.cache_root)
        except BaseException:
            # On any failure during staging, clean up tmp_dir so we
            # don't leave half-baked staging dirs around.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        return self._entry_for(input_hash)

    def invalidate(self, input_hash: str) -> None:
        """Remove the entry for ``input_hash``. Idempotent."""
        target = self._entry_dir(input_hash)
        if target.exists():
            shutil.rmtree(target)

    def clear(self) -> int:
        """Remove all cache entries.

        Returns the count of entries removed. Non-entry files at the
        root (e.g. tmp staging dirs left over from an aborted run, or
        unrelated junk) are ignored in the count but the entire root is
        wiped.
        """
        if not self.cache_root.exists():
            return 0
        # Count only entry-shaped subdirectories: ones whose name does
        # not contain ``.tmp`` and which are directories. This makes
        # the count reflect "real" cache entries even if tmp dirs are
        # lingering.
        count = 0
        for child in self.cache_root.iterdir():
            if child.is_dir() and ".tmp" not in child.name:
                count += 1
        shutil.rmtree(self.cache_root)
        return count
