"""Tests for ``compiler_suit_runner.cachix_uploader``.

All subprocess invocations are stubbed via the module's injection
points; no real ``cachix`` or ``nix`` is ever executed.
"""

from __future__ import annotations

import os
import pathlib
import stat

import pytest

from compiler_suit_runner import cachix_uploader as cu


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_token(tmp_path: pathlib.Path, mode: int = 0o600) -> pathlib.Path:
    """Create a token file with the given mode."""
    token_path = tmp_path / "cachix-token"
    token_path.write_text("super-secret-token\n")
    os.chmod(token_path, mode)
    return token_path


def _make_config(token_path: pathlib.Path, **overrides) -> cu.UploaderConfig:
    base = dict(
        cache_name="asm-dataset-test",
        auth_token_file=token_path,
        poll_interval_seconds=0.0,
        max_retries=5,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=60.0,
    )
    base.update(overrides)
    return cu.UploaderConfig(**base)


class FakeClock:
    """Records every sleep duration; never actually sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self, seconds: float | None = None) -> float:
        if seconds is None:
            return self.now
        self.sleeps.append(seconds)
        self.now += seconds
        return self.now


class StubRunner:
    """Programmable subprocess runner.

    ``responses`` is a list of ``(stdout, stderr, returncode)`` tuples.
    Each call pops one. If exhausted, returns the last entry repeatedly.
    """

    def __init__(self, responses: list[tuple[str, str, int]]) -> None:
        assert responses, "StubRunner needs at least one response"
        self.responses = list(responses)
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, argv, *, env=None, timeout=None):
        self.calls.append((tuple(argv), {"env": env, "timeout": timeout}))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


# ---------------------------------------------------------------------------
# is_pushable
# ---------------------------------------------------------------------------


def test_is_pushable_accepts_normal_store_path():
    assert cu.is_pushable("/nix/store/abcdefghijklmnopqrstuvwxyz012345-hello-2.12")


def test_is_pushable_rejects_tar_zst_variant_tarball():
    path = "/nix/store/abcdefghijklmnopqrstuvwxyz012345-hello-x86_64-O2.tar.zst"
    assert not cu.is_pushable(path)


def test_is_pushable_rejects_non_store_name():
    assert not cu.is_pushable("/tmp/garbage")
    assert not cu.is_pushable("/nix/store/short-name")
    assert not cu.is_pushable("not-a-path-at-all")


def test_is_pushable_accepts_pathlib_input():
    p = pathlib.Path("/nix/store/abcdefghijklmnopqrstuvwxyz012345-glibc-2.40")
    assert cu.is_pushable(p)


def test_is_pushable_rejects_pathlib_tar_zst():
    p = pathlib.Path(
        "/nix/store/abcdefghijklmnopqrstuvwxyz012345-variant-O0.tar.zst"
    )
    assert not cu.is_pushable(p)


# ---------------------------------------------------------------------------
# push_one — happy path
# ---------------------------------------------------------------------------


def test_push_one_happy_path(tmp_path):
    token = _write_token(tmp_path)
    cfg = _make_config(token)
    runner = StubRunner([("ok\n", "", 0)])
    clock = FakeClock()

    path = "/nix/store/abcdefghijklmnopqrstuvwxyz012345-hello-2.12"
    result = cu.push_one(cfg, path, run_subprocess=runner, clock=clock)

    assert result.success is True
    assert result.attempts == 1
    assert result.error is None
    assert clock.sleeps == []
    # Confirms cachix CLI was invoked correctly.
    assert len(runner.calls) == 1
    argv, _ = runner.calls[0]
    assert argv == ("cachix", "push", "asm-dataset-test", path)


# ---------------------------------------------------------------------------
# push_one — permanent failure
# ---------------------------------------------------------------------------


def test_push_one_permanent_failure(tmp_path):
    token = _write_token(tmp_path)
    cfg = _make_config(token, max_retries=3, initial_backoff_seconds=1.0,
                       max_backoff_seconds=60.0)
    runner = StubRunner([("", "boom: cache offline", 2)])
    clock = FakeClock()

    path = "/nix/store/abcdefghijklmnopqrstuvwxyz012345-broken-pkg"
    result = cu.push_one(cfg, path, run_subprocess=runner, clock=clock)

    assert result.success is False
    assert result.attempts == 3
    assert result.error is not None
    assert "boom" in result.error
    # Backoff is invoked between retries: max_retries - 1 sleeps.
    assert len(clock.sleeps) == 2


# ---------------------------------------------------------------------------
# push_one — succeeds on retry
# ---------------------------------------------------------------------------


def test_push_one_succeeds_on_retry(tmp_path):
    token = _write_token(tmp_path)
    cfg = _make_config(token)
    runner = StubRunner([
        ("", "transient network error", 1),
        ("uploaded", "", 0),
    ])
    clock = FakeClock()

    path = "/nix/store/abcdefghijklmnopqrstuvwxyz012345-flaky-pkg"
    result = cu.push_one(cfg, path, run_subprocess=runner, clock=clock)

    assert result.success is True
    assert result.attempts == 2
    assert result.error is None
    # Exactly one sleep between the two attempts.
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx(cfg.initial_backoff_seconds)


# ---------------------------------------------------------------------------
# push_one — token file mode/exists checks
# ---------------------------------------------------------------------------


def test_push_one_rejects_world_readable_token(tmp_path):
    token = _write_token(tmp_path, mode=0o644)
    cfg = _make_config(token)
    runner = StubRunner([("", "", 0)])

    with pytest.raises(RuntimeError, match="insecure mode"):
        cu.push_one(
            cfg,
            "/nix/store/abcdefghijklmnopqrstuvwxyz012345-foo",
            run_subprocess=runner,
            clock=FakeClock(),
        )

    # Critically: cachix was never called.
    assert runner.calls == []


def test_push_one_rejects_missing_token(tmp_path):
    cfg = _make_config(tmp_path / "does-not-exist")
    runner = StubRunner([("", "", 0)])

    with pytest.raises(RuntimeError, match="not found"):
        cu.push_one(
            cfg,
            "/nix/store/abcdefghijklmnopqrstuvwxyz012345-foo",
            run_subprocess=runner,
            clock=FakeClock(),
        )

    assert runner.calls == []


def test_push_one_accepts_0400_token(tmp_path):
    token = _write_token(tmp_path, mode=0o400)
    cfg = _make_config(token)
    runner = StubRunner([("", "", 0)])

    result = cu.push_one(
        cfg,
        "/nix/store/abcdefghijklmnopqrstuvwxyz012345-foo",
        run_subprocess=runner,
        clock=FakeClock(),
    )
    assert result.success is True


# ---------------------------------------------------------------------------
# push_one — backoff schedule
# ---------------------------------------------------------------------------


def test_push_one_backoff_doubles_capped_at_max(tmp_path):
    token = _write_token(tmp_path)
    cfg = _make_config(
        token,
        max_retries=6,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=4.0,
    )
    # Always fail.
    runner = StubRunner([("", "still failing", 1)])
    clock = FakeClock()

    cu.push_one(
        cfg,
        "/nix/store/abcdefghijklmnopqrstuvwxyz012345-foo",
        run_subprocess=runner,
        clock=clock,
    )
    # 6 retries → 5 sleeps. Backoff: 1, 2, 4, 4, 4
    assert clock.sleeps == [1.0, 2.0, 4.0, 4.0, 4.0]


# ---------------------------------------------------------------------------
# push_one — 429 rate-limit handling
# ---------------------------------------------------------------------------


def test_push_one_handles_429_rate_limit_then_succeeds(tmp_path):
    token = _write_token(tmp_path)
    cfg = _make_config(token, max_retries=4)
    runner = StubRunner([
        ("", "HTTP 429: rate limited", 1),
        ("", "HTTP 429: rate limited", 1),
        ("uploaded", "", 0),
    ])
    clock = FakeClock()

    result = cu.push_one(
        cfg,
        "/nix/store/abcdefghijklmnopqrstuvwxyz012345-foo",
        run_subprocess=runner,
        clock=clock,
    )
    assert result.success is True
    assert result.attempts == 3
    assert len(clock.sleeps) == 2  # backed off twice before succeeding


def test_push_one_429_exhausts_retries(tmp_path):
    token = _write_token(tmp_path)
    cfg = _make_config(token, max_retries=3)
    # Always 429.
    runner = StubRunner([("", "HTTP 429 too many requests", 1)])
    clock = FakeClock()

    result = cu.push_one(
        cfg,
        "/nix/store/abcdefghijklmnopqrstuvwxyz012345-foo",
        run_subprocess=runner,
        clock=clock,
    )
    assert result.success is False
    assert result.attempts == 3
    assert "429" in (result.error or "")


# ---------------------------------------------------------------------------
# CachixUploader.run — single tick lifecycle
# ---------------------------------------------------------------------------


def test_uploader_filters_tarballs_and_pushes_only_pushable(tmp_path):
    """One tick discovers two paths: one pushable, one tarball.

    Drives ``run()`` directly (no thread) by setting the stop event after
    the first iteration via the injected clock.
    """
    token = _write_token(tmp_path)
    cfg = _make_config(token, poll_interval_seconds=30.0)

    pushable = "/nix/store/abcdefghijklmnopqrstuvwxyz012345-gcc-cross"
    tarball = "/nix/store/00000000000000000000000000000000-variant-O0.tar.zst"

    # The list_new_paths injection: first call returns the two paths,
    # second call (if reached) returns nothing new.
    call_counter = {"n": 0}

    def fake_list_new_paths(seen):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            current = {pushable, tarball}
            return current, current - seen
        return seen, set()

    runner = StubRunner([("", "", 0)])

    # Custom clock: on the post-tick sleep, signal stop so the loop ends.
    uploader_holder: dict = {}

    def stop_after_first_tick(seconds=None):
        if seconds is None:
            return 0.0
        # The push_one inside this tick won't sleep (success on first try).
        # Only the inter-tick poll-interval sleep should be observed here;
        # use it as the signal to stop.
        if seconds == cfg.poll_interval_seconds:
            uploader_holder["uploader"].stop()
        return 0.0

    uploader = cu.CachixUploader(
        cfg,
        list_new_paths=fake_list_new_paths,
        run_subprocess=runner,
        clock=stop_after_first_tick,
    )
    uploader_holder["uploader"] = uploader

    uploader.run()  # drive directly, not via .start()

    # Exactly one push: the pushable path. The tarball was filtered out.
    assert len(runner.calls) == 1
    argv, _ = runner.calls[0]
    assert argv == ("cachix", "push", cfg.cache_name, pushable)

    assert len(uploader.results) == 1
    res = uploader.results[0]
    assert res.store_path == pushable
    assert res.success is True


def test_uploader_stop_ends_loop(tmp_path):
    """Pre-set stop flag → run() returns after at most one tick."""
    token = _write_token(tmp_path)
    cfg = _make_config(token, poll_interval_seconds=0.0)

    def fake_list_new_paths(seen):
        return set(), set()

    runner = StubRunner([("", "", 0)])
    clock = FakeClock()

    uploader = cu.CachixUploader(
        cfg,
        list_new_paths=fake_list_new_paths,
        run_subprocess=runner,
        clock=clock,
    )
    uploader.stop()  # signal before run starts
    uploader.run()  # should exit immediately

    assert runner.calls == []
    assert len(uploader.results) == 0


def test_uploader_continues_when_list_new_paths_raises(tmp_path):
    """A list_new_paths exception is logged, not propagated."""
    token = _write_token(tmp_path)
    cfg = _make_config(token, poll_interval_seconds=0.0)

    call_counter = {"n": 0}

    def fake_list_new_paths(seen):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            raise OSError("nix is on fire")
        return set(), set()

    runner = StubRunner([("", "", 0)])
    uploader_holder: dict = {}

    def stop_after_two(seconds=None):
        if seconds is None:
            return 0.0
        if call_counter["n"] >= 2:
            uploader_holder["uploader"].stop()
        return 0.0

    uploader = cu.CachixUploader(
        cfg,
        list_new_paths=fake_list_new_paths,
        run_subprocess=runner,
        clock=stop_after_two,
    )
    uploader_holder["uploader"] = uploader
    uploader.run()

    # No pushes, no exception.
    assert runner.calls == []


def test_uploader_records_failed_pushes(tmp_path):
    """Failed pushes are recorded with success=False."""
    token = _write_token(tmp_path)
    cfg = _make_config(
        token,
        poll_interval_seconds=0.0,
        max_retries=2,
        initial_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
    )

    pushable = "/nix/store/abcdefghijklmnopqrstuvwxyz012345-broken"

    call_counter = {"n": 0}

    def fake_list_new_paths(seen):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return {pushable}, {pushable}
        return {pushable}, set()

    runner = StubRunner([("", "boom", 1)])
    uploader_holder: dict = {}

    def stop_after_first_tick(seconds=None):
        if seconds is None:
            return 0.0
        if call_counter["n"] >= 1:
            uploader_holder["uploader"].stop()
        return 0.0

    uploader = cu.CachixUploader(
        cfg,
        list_new_paths=fake_list_new_paths,
        run_subprocess=runner,
        clock=stop_after_first_tick,
    )
    uploader_holder["uploader"] = uploader
    uploader.run()

    assert len(uploader.results) == 1
    res = uploader.results[0]
    assert res.success is False
    assert res.attempts == cfg.max_retries
    assert "boom" in (res.error or "")
