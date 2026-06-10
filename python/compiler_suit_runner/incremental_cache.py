"""Local on-disk memoization cache for the submitter's enumeration steps.

``cmd_submit`` runs two pure enumeration functions whose results are
fully determined by the repo state (flake.lock + git rev + git diff)
plus a small subset of the invocation axes:

* :func:`compiler_suit_runner.preflight.enumerate_toolchains_only` —
  the expensive one (~5 min of nix-eval-jobs over the cross-toolchain
  map plus the aggregate-drv instantiate); reads ``(sys_name, archs)``.
* :func:`compiler_suit_runner.preflight.enumerate_variants` — cheap
  (1-2 nix evals); reads ``(sys_name, packages, archs, variant_sample,
  variant_seed)``.

The cache memoizes EXACTLY those two return values, each under its own
independently derived key. Nothing else is persisted: stage selection,
manifest emission, the per-binary metadata flatten, the local toolchain
availability check and the dispatch config are all rebuilt from
``args`` on every invocation, so a cache hit reaches dispatch with the
same state a miss builds — by construction, there is only one
state-building path. (The previous architecture persisted
manifest-replay inputs in a ``manifests.tar`` entry and ran a separate
cache-hit codepath that silently diverged from the miss path: zero
matrix_eval/dependency_graph tasks planned, the toolchain-archive
upload skipped, suppressed manifest classes re-emitted. That entry
format is gone; any legacy entry on disk is simply never consulted.)

Cache layout::

    <cache_root>/toolchains/<key>/entry.json
    <cache_root>/variants/<key>/entry.json

``entry.json`` carries a ``version`` field; an entry with an unknown
version or any shape mismatch is treated as a miss (loudly logged,
never raised from the cache layer), so no legacy or corrupt entry can
ever be replayed. Writes are atomic at the entry-dir level: stage into
``<key>.tmp.<pid>/`` first, then ``os.replace`` makes the final
directory visible. Partial writes never leave a half-cached entry that
a lookup would consider complete.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
from typing import Callable, ClassVar, Optional, Sequence, Union

DEFAULT_CACHE_ROOT = pathlib.Path.home() / ".cache" / "compiler_suit_runner"

logger = logging.getLogger(__name__)

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
class ToolchainAxes:
    """The invocation-axis subset ``enumerate_toolchains_only`` reads.

    * ``sys_name`` — ``--system``: flake system attribute the
      cross-toolchain map is evaluated against.
    * ``archs`` — ``--archs``: restricts the toolchain enumeration.

    ``archs`` uses ``None`` for "all" (the CLI default); explicit lists
    are canonicalized via :func:`_canonical_str_tuple` so flag ordering
    and duplicates do not change the key. Axes that only shape the
    DOWNSTREAM state (``--build-compilers``, ``--debug-testbuild``,
    ``--no-toolchain-dedup``) are deliberately absent: that state is
    derived from ``args`` on every invocation and never cached.
    """

    KIND: ClassVar[str] = "toolchains"

    sys_name: str = "x86_64-linux"
    archs: Optional[tuple[str, ...]] = None

    @classmethod
    def from_values(
        cls,
        *,
        sys_name: str = "x86_64-linux",
        archs: Optional[Sequence[str]] = None,
    ) -> "ToolchainAxes":
        return cls(
            sys_name=str(sys_name),
            archs=_canonical_str_tuple(archs),
        )

    def canonical_bytes(self) -> bytes:
        """Deterministic, order-stable serialization for hashing.

        JSON with sorted keys and fixed separators; tuples serialize as
        JSON arrays in their (already canonical) order, ``None`` as
        ``null`` — distinct from any explicit list. The ``kind``
        discriminator keeps a toolchains key from ever colliding with a
        variants key built from coincidentally identical fields.
        """
        payload = {"kind": self.KIND, **dataclasses.asdict(self)}
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class VariantAxes:
    """The invocation-axis subset ``enumerate_variants`` reads.

    * ``sys_name`` — ``--system``: flake system attribute the dataset
      map is evaluated against.
    * ``packages`` — ``--packages``: selects which binaries are
      enumerated for matrix_eval.
    * ``archs`` — ``--archs``: restricts the per-binary arch list.
    * ``variant_sample`` / ``variant_seed`` — ``--variant-sample`` /
      ``--variant-seed``: baked verbatim into each binary's metadata by
      the enumeration, so they are part of the memoized value's inputs.

    Canonicalization matches :class:`ToolchainAxes` (``None`` = "all",
    sorted de-duplicated tuples for explicit lists).
    """

    KIND: ClassVar[str] = "variants"

    sys_name: str = "x86_64-linux"
    packages: Optional[tuple[str, ...]] = None
    archs: Optional[tuple[str, ...]] = None
    variant_sample: int = 0
    variant_seed: str = "42"

    @classmethod
    def from_values(
        cls,
        *,
        sys_name: str = "x86_64-linux",
        packages: Optional[Sequence[str]] = None,
        archs: Optional[Sequence[str]] = None,
        variant_sample: int = 0,
        variant_seed: str = "42",
    ) -> "VariantAxes":
        return cls(
            sys_name=str(sys_name),
            packages=_canonical_str_tuple(packages),
            archs=_canonical_str_tuple(archs),
            variant_sample=int(variant_sample),
            variant_seed=str(variant_seed),
        )

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for hashing (see
        :meth:`ToolchainAxes.canonical_bytes`)."""
        payload = {"kind": self.KIND, **dataclasses.asdict(self)}
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")


SubentryAxes = Union[ToolchainAxes, VariantAxes]


@dataclasses.dataclass(frozen=True)
class InputHashInputs:
    """The sources combined into a cache key.

    Three repo-state inputs (bytes-or-strings to keep the hash
    deterministic) plus the canonical serialization of the sub-entry's
    axis subset (:class:`ToolchainAxes` / :class:`VariantAxes`).
    """

    flake_lock: bytes  # contents of flake.lock
    git_rev: str  # git rev-parse HEAD output (40-char hex, stripped)
    git_diff: bytes  # git diff worktree contents (may be empty)
    # Canonical bytes of the sub-entry axes (``canonical_bytes()``).
    # Defaults to empty so the repo state can be collected once and the
    # per-sub-entry keys derived via ``dataclasses.replace`` (see
    # :func:`compute_subentry_key`).
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


def compute_subentry_key(
    repo_inputs: InputHashInputs, axes: SubentryAxes
) -> str:
    """Derive one sub-entry's cache key from the repo state plus the
    axis subset the memoized enumeration actually reads.

    The repo state is collected ONCE per invocation
    (:func:`collect_input_hash_inputs`); each sub-entry key rides its
    own ``axes.canonical_bytes()`` (which embeds a kind discriminator)
    in the ``invocation`` slot of the hash input.
    """
    return compute_input_hash(
        dataclasses.replace(repo_inputs, invocation=axes.canonical_bytes())
    )


def _default_run_subprocess(
    cmd: list[str], *, cwd: pathlib.Path
) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, check=False)


def _default_read_bytes(path: pathlib.Path) -> bytes:
    return path.read_bytes()


def collect_input_hash_inputs(
    repo_root: pathlib.Path,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
    read_bytes: Optional[ReadBytes] = None,
) -> InputHashInputs:
    """Collect the repo-state inputs for the cache keys.

    Reads ``flake.lock`` from disk; calls ``git rev-parse HEAD`` and
    ``git diff`` via ``run_subprocess`` (default: :func:`subprocess.run`).
    If git is missing or ``repo_root`` is not a git repo, raises
    :class:`RuntimeError`. Use ``read_bytes`` (default
    :meth:`pathlib.Path.read_bytes`) for testability.

    The returned :class:`InputHashInputs` carries ``invocation=b""``;
    callers derive the per-sub-entry keys via
    :func:`compute_subentry_key`.
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
    )


# ---------------------------------------------------------------------------
# Sub-entry payload (de)serialization + shape validation
# ---------------------------------------------------------------------------


# Required keys of each per-binary metadata dict as produced by
# ``enumerate_variants`` (see its docstring's return shape).
_VARIANT_META_REQUIRED_KEYS = ("archs", "sample_size", "sample_seed", "tier")


def _toolchains_payload(
    tc_pairs: Sequence[tuple[str, str]],
    tc_drvs: dict[tuple[str, str], str],
    tc_aggregate_drv: str,
    *,
    version: int,
) -> dict:
    """Serialize ``enumerate_toolchains_only``'s return triple.

    Tuples become JSON arrays in their original order so the round-trip
    is exact (``enumerate_toolchains_only`` already emits sorted pairs;
    we preserve whatever order it produced rather than re-sorting).
    """
    return {
        "version": version,
        "kind": ToolchainAxes.KIND,
        "tc_pairs": [[arch, compiler] for (arch, compiler) in tc_pairs],
        "tc_drvs": [
            [arch, compiler, drv]
            for (arch, compiler), drv in tc_drvs.items()
        ],
        "tc_aggregate_drv": str(tc_aggregate_drv),
    }


def _toolchains_from_payload(
    payload: dict,
) -> tuple[tuple[tuple[str, str], ...], dict[tuple[str, str], str], str]:
    """Validate + deserialize a toolchains entry payload.

    Raises :class:`ValueError` on any shape mismatch; the cache layer
    converts that into a logged miss.
    """
    pairs_raw = payload.get("tc_pairs")
    drvs_raw = payload.get("tc_drvs")
    aggregate = payload.get("tc_aggregate_drv")
    if not isinstance(pairs_raw, list):
        raise ValueError("'tc_pairs' missing or not a list")
    if not isinstance(drvs_raw, list):
        raise ValueError("'tc_drvs' missing or not a list")
    if not isinstance(aggregate, str):
        raise ValueError("'tc_aggregate_drv' missing or not a string")

    pairs: list[tuple[str, str]] = []
    for entry in pairs_raw:
        if not (
            isinstance(entry, list)
            and len(entry) == 2
            and all(isinstance(x, str) for x in entry)
        ):
            raise ValueError(f"malformed 'tc_pairs' entry: {entry!r}")
        pairs.append((entry[0], entry[1]))

    drvs: dict[tuple[str, str], str] = {}
    for entry in drvs_raw:
        if not (
            isinstance(entry, list)
            and len(entry) == 3
            and all(isinstance(x, str) for x in entry)
        ):
            raise ValueError(f"malformed 'tc_drvs' entry: {entry!r}")
        drvs[(entry[0], entry[1])] = entry[2]

    return tuple(pairs), drvs, aggregate


def _variants_payload(
    per_binary_meta_raw: dict[str, dict], *, version: int
) -> dict:
    """Serialize ``enumerate_variants``'s return dict (JSON-native)."""
    return {
        "version": version,
        "kind": VariantAxes.KIND,
        "per_binary_meta_raw": dict(per_binary_meta_raw),
    }


def _variants_from_payload(payload: dict) -> dict[str, dict]:
    """Validate + deserialize a variants entry payload.

    Raises :class:`ValueError` on any shape mismatch; the cache layer
    converts that into a logged miss.
    """
    raw = payload.get("per_binary_meta_raw")
    if not isinstance(raw, dict):
        raise ValueError("'per_binary_meta_raw' missing or not a dict")
    out: dict[str, dict] = {}
    for pkg, meta in raw.items():
        if not isinstance(pkg, str):
            raise ValueError(f"non-string binary key: {pkg!r}")
        if not isinstance(meta, dict):
            raise ValueError(f"binary {pkg!r} metadata is not a dict")
        missing = [k for k in _VARIANT_META_REQUIRED_KEYS if k not in meta]
        if missing:
            raise ValueError(
                f"binary {pkg!r} metadata missing keys {missing}"
            )
        archs = meta.get("archs")
        if not isinstance(archs, list) or not all(
            isinstance(a, str) for a in archs
        ):
            raise ValueError(
                f"binary {pkg!r} 'archs' is not a list of strings"
            )
        out[pkg] = meta
    return out


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
    """File-based memoization store for the enumeration results.

    File layout::

        <cache_root>/toolchains/<key>/entry.json
        <cache_root>/variants/<key>/entry.json

    All writes are atomic at the entry-dir level (stage into
    ``<key>.tmp.<pid>/``, then :func:`os.replace`); partial writes never
    leave a half-cached entry that a lookup would consider complete.
    Lookups NEVER raise: any unreadable / unparseable / wrong-version /
    wrong-shape entry is loudly logged and treated as a miss, so legacy
    entries (the retired ``<cache_root>/<hash>/manifests.tar`` format)
    can never be replayed.
    """

    ENTRY_VERSION = 1
    ENTRY_NAME = "entry.json"
    TOOLCHAINS_NAMESPACE = ToolchainAxes.KIND
    VARIANTS_NAMESPACE = VariantAxes.KIND
    _NAMESPACES = (TOOLCHAINS_NAMESPACE, VARIANTS_NAMESPACE)

    def __init__(self, cache_root: pathlib.Path = DEFAULT_CACHE_ROOT) -> None:
        self.cache_root = pathlib.Path(cache_root)

    # ------------------------------------------------------------------ paths

    def _subentry_dir(self, namespace: str, key: str) -> pathlib.Path:
        return self.cache_root / namespace / key

    # ------------------------------------------------------- generic sub-entry

    def _discard_invalid_subentry(self, namespace: str, key: str) -> None:
        """Best-effort removal of an invalid entry dir.

        Without this, the store path's keep-the-existing-entry race
        guard would preserve a corrupt/legacy entry forever and every
        invocation would re-enumerate; discarding lets the re-store
        after the forced miss self-heal the entry.
        """
        shutil.rmtree(self._subentry_dir(namespace, key), ignore_errors=True)

    def _load_subentry(self, namespace: str, key: str) -> Optional[dict]:
        """Return the parsed + version-checked entry payload, or ``None``.

        ``None`` covers: entry absent, unreadable, unparseable JSON,
        non-dict payload, or a ``version`` other than
        :attr:`ENTRY_VERSION`. Everything but plain absence is logged,
        and the invalid entry dir is discarded so the re-store after
        the forced miss can replace it.
        """
        entry_dir = self._subentry_dir(namespace, key)
        entry_path = entry_dir / self.ENTRY_NAME
        try:
            raw = entry_path.read_text()
        except FileNotFoundError:
            if entry_dir.is_dir():
                # Entry dir present but no entry.json — e.g. a legacy
                # (manifests.tar-era) entry shape. Never replayed.
                logger.warning(
                    "incremental cache: %s entry %s has no %s"
                    " (legacy/partial entry); treating as miss",
                    namespace, key, self.ENTRY_NAME,
                )
                self._discard_invalid_subentry(namespace, key)
            return None
        except OSError as exc:
            logger.warning(
                "incremental cache: unreadable %s entry %s (%s);"
                " treating as miss",
                namespace, key, exc,
            )
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "incremental cache: corrupt %s entry %s (%s);"
                " treating as miss",
                namespace, key, exc,
            )
            self._discard_invalid_subentry(namespace, key)
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != self.ENTRY_VERSION
        ):
            logger.warning(
                "incremental cache: %s entry %s has unsupported"
                " version/shape %r; treating as miss",
                namespace,
                key,
                payload.get("version") if isinstance(payload, dict) else None,
            )
            self._discard_invalid_subentry(namespace, key)
            return None
        return payload

    def _store_subentry(
        self, namespace: str, key: str, payload: dict
    ) -> None:
        """Atomically persist ``payload`` as ``<namespace>/<key>/entry.json``.

        If another process already populated the target directory while
        we were writing, the existing one is preserved and the tmp
        directory is cleaned up (the memoized value for one key is
        identical by construction, so keeping the first writer is safe).
        """
        ns_dir = self.cache_root / namespace
        ns_dir.mkdir(parents=True, exist_ok=True)

        target_dir = self._subentry_dir(namespace, key)
        # Pid-suffixed tmp dir so concurrent stores don't collide at the
        # tmp level either.
        tmp_dir = ns_dir / f"{key}.tmp.{os.getpid()}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)

        try:
            (tmp_dir / self.ENTRY_NAME).write_text(
                json.dumps(payload, indent=2, sort_keys=True)
            )
            _fsync_dir(tmp_dir)

            if target_dir.exists():
                # Someone else got here first. Discard our tmp dir.
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return
            try:
                os.replace(str(tmp_dir), str(target_dir))
            except OSError:
                # Lost the race between the .exists() check and the
                # rename. Keep the existing entry.
                shutil.rmtree(tmp_dir, ignore_errors=True)
                if target_dir.exists():
                    return
                raise
            _fsync_dir(ns_dir)
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    # ------------------------------------------------------------- toolchains

    def lookup_toolchains(
        self, key: str
    ) -> Optional[
        tuple[tuple[tuple[str, str], ...], dict[tuple[str, str], str], str]
    ]:
        """Return the memoized ``enumerate_toolchains_only`` triple
        ``(tc_pairs, tc_drvs, tc_aggregate_drv)``, or ``None`` (miss).

        Any validation failure is loudly logged and degrades to a miss;
        this method never raises.
        """
        payload = self._load_subentry(self.TOOLCHAINS_NAMESPACE, key)
        if payload is None:
            return None
        try:
            return _toolchains_from_payload(payload)
        except ValueError as exc:
            logger.warning(
                "incremental cache: invalid toolchains entry %s (%s);"
                " treating as miss",
                key, exc,
            )
            self._discard_invalid_subentry(self.TOOLCHAINS_NAMESPACE, key)
            return None

    def store_toolchains(
        self,
        key: str,
        tc_pairs: Sequence[tuple[str, str]],
        tc_drvs: dict[tuple[str, str], str],
        tc_aggregate_drv: str,
    ) -> None:
        """Persist ``enumerate_toolchains_only``'s return triple."""
        self._store_subentry(
            self.TOOLCHAINS_NAMESPACE,
            key,
            _toolchains_payload(
                tc_pairs, tc_drvs, tc_aggregate_drv,
                version=self.ENTRY_VERSION,
            ),
        )

    # --------------------------------------------------------------- variants

    def lookup_variants(self, key: str) -> Optional[dict[str, dict]]:
        """Return the memoized ``enumerate_variants`` dict, or ``None``.

        Any validation failure is loudly logged and degrades to a miss;
        this method never raises.
        """
        payload = self._load_subentry(self.VARIANTS_NAMESPACE, key)
        if payload is None:
            return None
        try:
            return _variants_from_payload(payload)
        except ValueError as exc:
            logger.warning(
                "incremental cache: invalid variants entry %s (%s);"
                " treating as miss",
                key, exc,
            )
            self._discard_invalid_subentry(self.VARIANTS_NAMESPACE, key)
            return None

    def store_variants(
        self, key: str, per_binary_meta_raw: dict[str, dict]
    ) -> None:
        """Persist ``enumerate_variants``'s return dict."""
        self._store_subentry(
            self.VARIANTS_NAMESPACE,
            key,
            _variants_payload(
                per_binary_meta_raw, version=self.ENTRY_VERSION,
            ),
        )

    # ------------------------------------------------------------ maintenance

    def invalidate(self, key: str) -> None:
        """Remove ``key``'s sub-entries from every namespace. Idempotent.

        Also removes a same-named legacy top-level entry dir (the
        retired ``<cache_root>/<hash>/`` format) so ``clear-cache
        --hash`` keeps working against pre-migration cache roots.
        """
        for namespace in self._NAMESPACES:
            target = self._subentry_dir(namespace, key)
            if target.exists():
                shutil.rmtree(target)
        if key not in self._NAMESPACES:
            legacy = self.cache_root / key
            if legacy.exists():
                shutil.rmtree(legacy)

    def clear(self) -> int:
        """Remove the entire cache root.

        Returns the count of entries removed: sub-entries under the
        namespace dirs plus any legacy top-level entry dirs. Tmp staging
        dirs and stray files are wiped but not counted.
        """
        if not self.cache_root.exists():
            return 0
        count = 0
        for child in self.cache_root.iterdir():
            if not child.is_dir() or ".tmp" in child.name:
                continue
            if child.name in self._NAMESPACES:
                count += sum(
                    1
                    for sub in child.iterdir()
                    if sub.is_dir() and ".tmp" not in sub.name
                )
            else:
                # Legacy (pre-sub-entry) top-level entry dir.
                count += 1
        shutil.rmtree(self.cache_root)
        return count
