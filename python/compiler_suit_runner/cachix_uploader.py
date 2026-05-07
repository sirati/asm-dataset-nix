"""Cachix federation uploader.

Watches the local nix store for newly-added paths and pushes them to a
Cachix cache. Variant elf-folder outputs (``*-elf-folder`` produced by
``mkBinaryFolder``) are filtered out — they ARE the dataset and must
not leak into the public binary cache.

The uploader is a daemon thread; on each tick it diffs the current
store against the previous tick's snapshot, filters via
:func:`is_pushable`, and shells out to ``cachix push`` with
exponential backoff on failure.

All process-touching surfaces (subprocess execution, ``nix path-info``,
clock) are dependency-injected so tests never spawn ``cachix`` or
``nix`` for real.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import logging
import os
import pathlib
import re
import stat
import subprocess
import threading
import time
from typing import Callable, Deque, Optional

logger = logging.getLogger(__name__)

# A nix store entry name: 32 lowercase hash chars, dash, then a non-empty name.
_STORE_NAME_RE = re.compile(r"^[a-z0-9]{32}-.+")

# RunSubprocess(argv, *, timeout=None) -> (stdout, stderr, returncode)
RunSubprocess = Callable[..., tuple[str, str, int]]

# Clock: clock() -> monotonic seconds; clock(s) -> sleep s seconds (or
# test-equivalent advance).
Clock = Callable[..., float]

# ListNewPaths(seen) -> (current_paths, newly_added)
ListNewPaths = Callable[[set[str]], tuple[set[str], set[str]]]


def is_pushable(store_path: "str | pathlib.Path") -> bool:
    """Return True iff ``store_path`` should be pushed to the public cache.

    Reject paths whose basename ends with ``-elf-folder`` (these are the
    ``mkBinaryFolder`` variant outputs — the dataset itself).

    Reject paths whose basename does not match the standard nix store
    convention (32 lowercase hex/base32 chars + ``-`` + name).
    """
    name = pathlib.Path(store_path).name
    if name.endswith("-elf-folder"):
        return False
    if not _STORE_NAME_RE.match(name):
        return False
    return True


@dataclasses.dataclass
class UploaderConfig:
    """Static configuration for the Cachix uploader."""

    cache_name: str
    auth_token_file: pathlib.Path
    poll_interval_seconds: float = 30.0
    max_retries: int = 5
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0


@dataclasses.dataclass
class UploadResult:
    """Outcome of a single ``cachix push`` invocation (after retries)."""

    store_path: str
    success: bool
    attempts: int
    error: Optional[str] = None


def _default_run_subprocess(
    argv: list[str], *, timeout: Optional[float] = None
) -> tuple[str, str, int]:
    """Run ``argv`` and return ``(stdout, stderr, returncode)``."""
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _default_clock(seconds: Optional[float] = None) -> float:
    """Return monotonic seconds, or sleep ``seconds`` and return current time."""
    if seconds is None:
        return time.monotonic()
    time.sleep(seconds)
    return time.monotonic()


def _check_token_file(token_file: pathlib.Path) -> str:
    """Read the auth token, asserting permissive modes are absent.

    Raises :class:`RuntimeError` if the token file is missing or its
    permission mode is not 0400 / 0600.
    """
    if not token_file.exists():
        raise RuntimeError(f"Cachix auth token file not found: {token_file}")
    st = token_file.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode not in (0o400, 0o600):
        raise RuntimeError(
            f"Cachix auth token file {token_file} has insecure mode {oct(mode)}; "
            f"expected 0400 or 0600"
        )
    return token_file.read_text().strip()


def list_new_paths(seen: set[str]) -> tuple[set[str], set[str]]:
    """Return ``(current_paths, newly_added)`` from the local nix store.

    Tries ``nix path-info --all --json`` first (modern nix); on failure
    falls back to ``nix-store --gc --print-live`` (older nix). If
    neither succeeds (e.g. nix not installed), returns ``(set(), set())``.
    """
    current: set[str] = set()

    try:
        proc = subprocess.run(
            ["nix", "path-info", "--all", "--json"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        proc = None

    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            current = set(data.keys())
        elif isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and "path" in entry:
                    current.add(entry["path"])
                elif isinstance(entry, str):
                    current.add(entry)

    if not current:
        try:
            proc2 = subprocess.run(
                ["nix-store", "--gc", "--print-live"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return set(), set()
        if proc2.returncode != 0:
            return set(), set()
        for line in proc2.stdout.splitlines():
            line = line.strip()
            if line:
                current.add(line)

    newly_added = current - seen
    return current, newly_added


def push_one(
    config: UploaderConfig,
    store_path: str,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
    clock: Optional[Clock] = None,
) -> UploadResult:
    """Push a single store path to Cachix with exponential backoff.

    Reads and validates ``config.auth_token_file`` before any push. The
    token is supplied to the ``cachix`` CLI via the ``CACHIX_AUTH_TOKEN``
    environment variable (the conventional channel; never as an argv).

    Retries up to ``config.max_retries`` times. Backoff starts at
    ``initial_backoff_seconds`` and doubles each retry, capped at
    ``max_backoff_seconds``. 429 (rate-limit) responses are treated as
    retryable.
    """
    run = run_subprocess if run_subprocess is not None else _default_run_subprocess
    sleep_fn = clock if clock is not None else _default_clock

    # Read token (raises RuntimeError on missing/insecure file).
    token = _check_token_file(config.auth_token_file)

    backoff = config.initial_backoff_seconds
    last_error: Optional[str] = None

    argv = ["cachix", "push", config.cache_name, store_path]

    for attempt in range(1, config.max_retries + 1):
        # Pass the token via env. We tunnel via a wrapper kwarg so test
        # injectors can introspect it; default _default_run_subprocess
        # ignores env (the real cachix CLI inherits from os.environ which
        # is set by the caller). We set env on the process here.
        try:
            stdout, stderr, returncode = _invoke_cachix(run, argv, token)
        except Exception as exc:  # noqa: BLE001 - subprocess failure is broad
            last_error = f"subprocess failed: {exc}"
            returncode = 1
            stderr = str(exc)
        else:
            if returncode == 0:
                return UploadResult(
                    store_path=store_path,
                    success=True,
                    attempts=attempt,
                    error=None,
                )
            last_error = (stderr or stdout or "").strip() or f"exit {returncode}"

        # Don't sleep after the final attempt.
        if attempt >= config.max_retries:
            break

        sleep_fn(backoff)
        backoff = min(backoff * 2.0, config.max_backoff_seconds)

    return UploadResult(
        store_path=store_path,
        success=False,
        attempts=config.max_retries,
        error=last_error,
    )


def _invoke_cachix(
    run: RunSubprocess, argv: list[str], token: str
) -> tuple[str, str, int]:
    """Adapter that calls ``run`` with the token in env when supported.

    The real ``_default_run_subprocess`` does not accept ``env``, so we
    set ``CACHIX_AUTH_TOKEN`` on ``os.environ`` for the duration of the
    call. Test injectors can ignore the env entirely.
    """
    # Probe whether the injected ``run`` accepts env=. Attempt env-aware
    # call; if it raises TypeError, fall back to environ mutation.
    try:
        return run(argv, env={**os.environ, "CACHIX_AUTH_TOKEN": token})
    except TypeError:
        pass

    prev = os.environ.get("CACHIX_AUTH_TOKEN")
    os.environ["CACHIX_AUTH_TOKEN"] = token
    try:
        return run(argv)
    finally:
        if prev is None:
            os.environ.pop("CACHIX_AUTH_TOKEN", None)
        else:
            os.environ["CACHIX_AUTH_TOKEN"] = prev


class CachixUploader(threading.Thread):
    """Daemon thread that periodically pushes new paths to Cachix.

    The thread polls the local store on each tick (interval =
    ``config.poll_interval_seconds``), filters via :func:`is_pushable`,
    and runs :func:`push_one` on the survivors. Results are appended to
    :attr:`results` (bounded :class:`collections.deque`). The thread
    never raises out — failures are logged and the loop continues until
    :meth:`stop` is called.
    """

    #: Maximum number of UploadResult entries kept in :attr:`results`.
    DEFAULT_RESULTS_CAP = 4096

    def __init__(
        self,
        config: UploaderConfig,
        *,
        list_new_paths: Optional[ListNewPaths] = None,
        run_subprocess: Optional[RunSubprocess] = None,
        clock: Optional[Clock] = None,
        results_cap: int = DEFAULT_RESULTS_CAP,
    ) -> None:
        super().__init__(name=f"CachixUploader[{config.cache_name}]", daemon=True)
        self._config = config
        self._list_new_paths = (
            list_new_paths if list_new_paths is not None else globals()["list_new_paths"]
        )
        self._run_subprocess = run_subprocess
        self._clock = clock if clock is not None else _default_clock
        self._stop_event = threading.Event()
        self._seen: set[str] = set()
        self.results: Deque[UploadResult] = collections.deque(maxlen=results_cap)

    def stop(self) -> None:
        """Signal the loop to exit at the next iteration boundary."""
        self._stop_event.set()

    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def run(self) -> None:  # noqa: D401 - threading.Thread.run override
        """Tick until :meth:`stop` is called.

        Each tick:
        1. Calls the injected ``list_new_paths(seen)``.
        2. Filters via :func:`is_pushable`.
        3. Calls :func:`push_one` for each survivor.
        4. Sleeps ``config.poll_interval_seconds`` (via injected clock).
        """
        while not self._stop_event.is_set():
            try:
                current, newly_added = self._list_new_paths(self._seen)
            except Exception as exc:  # noqa: BLE001 - never raise out of run()
                logger.warning("CachixUploader: list_new_paths failed: %s", exc)
                current, newly_added = self._seen, set()

            self._seen = current

            for path in sorted(newly_added):
                if not is_pushable(path):
                    continue
                try:
                    result = push_one(
                        self._config,
                        path,
                        run_subprocess=self._run_subprocess,
                        clock=self._clock,
                    )
                except Exception as exc:  # noqa: BLE001 - log + continue
                    logger.warning(
                        "CachixUploader: push_one(%s) raised: %s", path, exc
                    )
                    result = UploadResult(
                        store_path=str(path),
                        success=False,
                        attempts=0,
                        error=f"raised: {exc}",
                    )
                self.results.append(result)
                if not result.success:
                    logger.warning(
                        "CachixUploader: failed to push %s after %d attempts: %s",
                        path,
                        result.attempts,
                        result.error,
                    )

            if self._stop_event.is_set():
                break
            # Use the injected clock for sleeping so tests can advance time.
            self._clock(self._config.poll_interval_seconds)
