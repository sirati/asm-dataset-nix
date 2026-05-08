"""Unit tests for :mod:`peer_mesh_assertions`.

The file-only parsers are exercised against synthetic on-disk fixtures
(pytest ``tmp_path``); the :func:`probe_substituter_reachability`
helper is exercised against a hand-rolled :class:`ClusterProbe` mock so
the tests never touch the live cluster.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from compiler_suit_runner.tests.slurm.peer_mesh_assertions import (
    DEFAULT_HARMONIA_PORT,
    MeshAssertionError,
    MeshShape,
    SubmitterEntry,
    SubstitutersEntry,
    assert_mesh_shape,
    parse_mesh,
    parse_substituters_file,
    probe_substituter_reachability,
    read_connection_info,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_substituters(
    peers_dir: Path,
    secondary_id: str,
    urls: list[str],
    keys: list[str] | None = None,
) -> Path:
    """Drop a canonical 4-line ``_substituters.<sid>.txt`` under
    ``peers_dir``.

    Mirrors what :func:`compiler_suit_runner.peer_cache._write_substituters_file`
    emits at runtime: ``--extra-substituters\\n<urls>\\n``
    ``--extra-trusted-public-keys\\n<keys>\\n`` with a trailing newline.
    """
    peers_dir.mkdir(parents=True, exist_ok=True)
    target = peers_dir / f"_substituters.{secondary_id}.txt"
    if not urls and not keys:
        target.write_text("", encoding="utf-8")
        return target
    parts = ["--extra-substituters", " ".join(urls)]
    if keys is None:
        keys = ["asm-suit-cluster-test:KEY-A=", "asm-suit-cluster-test:KEY-B="]
    parts += ["--extra-trusted-public-keys", " ".join(keys)]
    target.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return target


def _write_connection_info(
    conn_dir: Path,
    secondary_id: str,
    hostname: str,
    *,
    extra: dict[str, str] | None = None,
) -> Path:
    """Write a ``connection_info/<secondary_id>.info`` file."""
    conn_dir.mkdir(parents=True, exist_ok=True)
    target = conn_dir / f"{secondary_id}.info"
    body = f"hostname={hostname}\ntunnel_port=33000\n"
    if extra:
        for k, v in extra.items():
            body += f"{k}={v}\n"
    target.write_text(body, encoding="utf-8")
    return target


def _make_run_dir(
    tmp_path: Path,
    *,
    n_secondaries: int,
    workers: list[str] | None = None,
    harmonia_port: int = DEFAULT_HARMONIA_PORT,
    submitter_url: str | None = None,
    write_connection_info: bool = True,
) -> Path:
    """Build a synthetic ``run_<TS>/`` directory.

    Each secondary's substituter file lists every OTHER secondary's
    harmonia URL plus (optionally) the submitter URL. ``workers``
    overrides the default ``slurm-worker{1..N}`` host names.
    """
    if workers is None:
        workers = [f"slurm-worker{i + 1}" for i in range(n_secondaries)]
    if len(workers) != n_secondaries:
        raise ValueError("workers must match n_secondaries")
    run_dir = tmp_path / "run_20260508_123456"
    run_dir.mkdir()
    peers_dir = run_dir / "peers"
    conn_dir = run_dir / "connection_info"
    for i in range(n_secondaries):
        sid = f"secondary-{i}"
        peer_urls = [
            f"http://{workers[j]}:{harmonia_port}"
            for j in range(n_secondaries) if j != i
        ]
        if submitter_url is not None:
            peer_urls.append(submitter_url)
        _write_substituters(peers_dir, sid, peer_urls)
        if write_connection_info:
            _write_connection_info(conn_dir, sid, workers[i])
    return run_dir


# ---------------------------------------------------------------------------
# parse_substituters_file
# ---------------------------------------------------------------------------


def test_parse_substituters_canonical_4line(tmp_path: Path) -> None:
    target = _write_substituters(
        tmp_path / "peers", "secondary-0",
        ["http://slurm-worker2:5000", "http://slurm-worker3:5000"],
        ["asm-suit-cluster-X:KEY1=", "asm-suit-cluster-X:KEY2="],
    )
    entry = parse_substituters_file(target)
    assert entry.secondary_id == "secondary-0"
    assert entry.path == target
    assert entry.urls == (
        "http://slurm-worker2:5000", "http://slurm-worker3:5000",
    )
    assert len(entry.keys) == 2


def test_parse_substituters_empty_file(tmp_path: Path) -> None:
    """An empty file is the no-peers state -- watcher hasn't seen
    anyone yet. Both URLs and keys are empty tuples."""
    peers = tmp_path / "peers"
    peers.mkdir()
    target = peers / "_substituters.secondary-0.txt"
    target.write_text("", encoding="utf-8")
    entry = parse_substituters_file(target)
    assert entry.urls == ()
    assert entry.keys == ()


def test_parse_substituters_two_line_file(tmp_path: Path) -> None:
    """Pre-newline / pre-keys read produces a 2-line file. Tolerated."""
    peers = tmp_path / "peers"
    peers.mkdir()
    target = peers / "_substituters.secondary-1.txt"
    target.write_text(
        "--extra-substituters\nhttp://slurm-worker3:5000\n",
        encoding="utf-8",
    )
    entry = parse_substituters_file(target)
    assert entry.urls == ("http://slurm-worker3:5000",)
    assert entry.keys == ()


def test_parse_substituters_bad_filename(tmp_path: Path) -> None:
    target = tmp_path / "garbage.txt"
    target.write_text("--extra-substituters\nhttp://x:5000\n")
    with pytest.raises(MeshAssertionError, match="does not match"):
        parse_substituters_file(target)


def test_parse_substituters_bad_first_sentinel(tmp_path: Path) -> None:
    peers = tmp_path / "peers"
    peers.mkdir()
    target = peers / "_substituters.secondary-0.txt"
    target.write_text("--something-else\nhttp://a:5000\n")
    with pytest.raises(MeshAssertionError, match="line 1"):
        parse_substituters_file(target)


def test_parse_substituters_bad_third_sentinel(tmp_path: Path) -> None:
    peers = tmp_path / "peers"
    peers.mkdir()
    target = peers / "_substituters.secondary-0.txt"
    target.write_text(
        "--extra-substituters\nhttp://a:5000\n--unexpected\nKEY\n",
    )
    with pytest.raises(MeshAssertionError, match="line 3"):
        parse_substituters_file(target)


def test_parse_substituters_trailing_blank_lines(tmp_path: Path) -> None:
    """Two trailing newlines must not trip the sentinel-position check."""
    peers = tmp_path / "peers"
    peers.mkdir()
    target = peers / "_substituters.secondary-0.txt"
    target.write_text(
        "--extra-substituters\n"
        "http://slurm-worker2:5000\n"
        "--extra-trusted-public-keys\n"
        "asm-suit-cluster-X:KEY=\n"
        "\n",
        encoding="utf-8",
    )
    entry = parse_substituters_file(target)
    assert entry.urls == ("http://slurm-worker2:5000",)
    assert entry.keys == ("asm-suit-cluster-X:KEY=",)


def test_parse_substituters_multiple_urls_one_line(tmp_path: Path) -> None:
    """The writer joins URLs with a single space; multi-URL line is
    the multi-peer happy path."""
    peers = tmp_path / "peers"
    peers.mkdir()
    target = peers / "_substituters.secondary-2.txt"
    target.write_text(
        "--extra-substituters\n"
        "http://slurm-worker1:5000 http://slurm-worker3:5000 "
        "http://slurm-worker4:5000\n"
        "--extra-trusted-public-keys\n"
        "asm-suit-cluster-X:KEY=\n",
        encoding="utf-8",
    )
    entry = parse_substituters_file(target)
    assert entry.urls == (
        "http://slurm-worker1:5000",
        "http://slurm-worker3:5000",
        "http://slurm-worker4:5000",
    )


# ---------------------------------------------------------------------------
# read_connection_info
# ---------------------------------------------------------------------------


def test_read_connection_info_basic(tmp_path: Path) -> None:
    target = tmp_path / "secondary-0.info"
    target.write_text(
        "hostname=slurm-worker3\ntunnel_port=44849\n",
        encoding="utf-8",
    )
    out = read_connection_info(target)
    assert out == {"hostname": "slurm-worker3", "tunnel_port": "44849"}


def test_read_connection_info_skips_blank_and_comments(tmp_path: Path) -> None:
    target = tmp_path / "secondary-0.info"
    target.write_text(
        "# a comment\n"
        "hostname=slurm-worker3\n"
        "\n"
        "  # indented comment\n"
        "harmonia_port=5000\n",
        encoding="utf-8",
    )
    out = read_connection_info(target)
    assert out == {"hostname": "slurm-worker3", "harmonia_port": "5000"}


def test_read_connection_info_missing_returns_empty(tmp_path: Path) -> None:
    out = read_connection_info(tmp_path / "no-such.info")
    assert out == {}


# ---------------------------------------------------------------------------
# parse_mesh
# ---------------------------------------------------------------------------


def test_parse_mesh_n4_clean(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, n_secondaries=4)
    mesh = parse_mesh(run_dir, n_secondaries=4)
    assert mesh.n_secondaries == 4
    assert {e.secondary_id for e in mesh.entries} == {
        "secondary-0", "secondary-1", "secondary-2", "secondary-3",
    }
    assert mesh.harmonia_port == DEFAULT_HARMONIA_PORT
    assert mesh.submitter is None
    assert mesh.hosts_by_secondary == {
        "secondary-0": "slurm-worker1",
        "secondary-1": "slurm-worker2",
        "secondary-2": "slurm-worker3",
        "secondary-3": "slurm-worker4",
    }
    # Each entry lists exactly the OTHER 3 workers.
    sid0 = next(e for e in mesh.entries if e.secondary_id == "secondary-0")
    assert set(sid0.urls) == {
        "http://slurm-worker2:5000",
        "http://slurm-worker3:5000",
        "http://slurm-worker4:5000",
    }


def test_parse_mesh_with_submitter(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path, n_secondaries=4,
        submitter_url="http://localhost:5005",
    )
    mesh = parse_mesh(run_dir, n_secondaries=4)
    assert mesh.submitter == SubmitterEntry(
        url="http://localhost:5005", port=5005,
    )


def test_parse_mesh_missing_peers_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_x"
    run_dir.mkdir()
    with pytest.raises(MeshAssertionError, match="peers/"):
        parse_mesh(run_dir, n_secondaries=4)


def test_parse_mesh_inconsistent_submitter(tmp_path: Path) -> None:
    """Two different localhost URLs is a writer-level bug."""
    run_dir = tmp_path / "run_x"
    peers = run_dir / "peers"
    _write_substituters(
        peers, "secondary-0",
        ["http://slurm-worker2:5000", "http://localhost:5005"],
    )
    _write_substituters(
        peers, "secondary-1",
        ["http://slurm-worker1:5000", "http://localhost:7777"],
    )
    with pytest.raises(MeshAssertionError, match="multiple submitter"):
        parse_mesh(run_dir, n_secondaries=2)


def test_parse_mesh_invalid_n(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, n_secondaries=2)
    with pytest.raises(ValueError, match=">= 1"):
        parse_mesh(run_dir, n_secondaries=0)


# ---------------------------------------------------------------------------
# assert_mesh_shape
# ---------------------------------------------------------------------------


def test_assert_mesh_shape_n4_clean(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, n_secondaries=4)
    mesh = parse_mesh(run_dir, n_secondaries=4)
    # Should not raise.
    assert_mesh_shape(mesh, n_secondaries=4)


def test_assert_mesh_shape_n3_degraded(tmp_path: Path) -> None:
    """Degraded-mode N=3 fallback (worker1 down). Each secondary
    expects 2 peer URLs; the assertion must accept that."""
    run_dir = _make_run_dir(
        tmp_path, n_secondaries=3,
        workers=["slurm-worker2", "slurm-worker3", "slurm-worker4"],
    )
    mesh = parse_mesh(run_dir, n_secondaries=3)
    assert_mesh_shape(mesh, n_secondaries=3)


def test_assert_mesh_shape_with_submitter(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path, n_secondaries=4,
        submitter_url="http://localhost:5005",
    )
    mesh = parse_mesh(run_dir, n_secondaries=4)
    assert_mesh_shape(mesh, n_secondaries=4)
    assert_mesh_shape(mesh, n_secondaries=4, require_submitter=True)


def test_assert_mesh_shape_require_submitter_missing(
    tmp_path: Path,
) -> None:
    run_dir = _make_run_dir(tmp_path, n_secondaries=4)  # no submitter
    mesh = parse_mesh(run_dir, n_secondaries=4)
    with pytest.raises(MeshAssertionError, match="submitter URL expected"):
        assert_mesh_shape(mesh, n_secondaries=4, require_submitter=True)


def test_assert_mesh_shape_wrong_count(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, n_secondaries=2)
    mesh = parse_mesh(run_dir, n_secondaries=2)
    with pytest.raises(MeshAssertionError, match="2 substituter"):
        assert_mesh_shape(mesh, n_secondaries=4)


def test_assert_mesh_shape_too_few_peer_urls(tmp_path: Path) -> None:
    """secondary-0's file lists only 1 peer URL when N=4 expects 3."""
    run_dir = tmp_path / "run_x"
    peers = run_dir / "peers"
    _write_substituters(peers, "secondary-0", ["http://slurm-worker2:5000"])
    _write_substituters(
        peers, "secondary-1",
        [
            "http://slurm-worker1:5000",
            "http://slurm-worker3:5000",
            "http://slurm-worker4:5000",
        ],
    )
    _write_substituters(
        peers, "secondary-2",
        [
            "http://slurm-worker1:5000",
            "http://slurm-worker2:5000",
            "http://slurm-worker4:5000",
        ],
    )
    _write_substituters(
        peers, "secondary-3",
        [
            "http://slurm-worker1:5000",
            "http://slurm-worker2:5000",
            "http://slurm-worker3:5000",
        ],
    )
    mesh = parse_mesh(run_dir, n_secondaries=4)
    with pytest.raises(MeshAssertionError, match="lists 1 peer URL"):
        assert_mesh_shape(mesh, n_secondaries=4)


def test_assert_mesh_shape_self_referential(tmp_path: Path) -> None:
    """A secondary that lists its own host as a peer must trip the
    assertion -- harmonia would self-deadlock."""
    run_dir = tmp_path / "run_x"
    peers = run_dir / "peers"
    conn = run_dir / "connection_info"
    _write_substituters(
        peers, "secondary-0",
        [
            "http://slurm-worker1:5000",  # self!
            "http://slurm-worker3:5000",
        ],
    )
    _write_substituters(
        peers, "secondary-1",
        [
            "http://slurm-worker1:5000",
            "http://slurm-worker3:5000",
        ],
    )
    _write_substituters(
        peers, "secondary-2",
        [
            "http://slurm-worker1:5000",
            "http://slurm-worker2:5000",
        ],
    )
    _write_connection_info(conn, "secondary-0", "slurm-worker1")
    _write_connection_info(conn, "secondary-1", "slurm-worker2")
    _write_connection_info(conn, "secondary-2", "slurm-worker3")
    mesh = parse_mesh(run_dir, n_secondaries=3)
    with pytest.raises(MeshAssertionError, match="own host"):
        assert_mesh_shape(mesh, n_secondaries=3)


def test_assert_mesh_shape_wrong_port(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path, n_secondaries=2, harmonia_port=6000,
    )
    mesh = parse_mesh(run_dir, n_secondaries=2)
    # mesh.harmonia_port is read from connection_info; we passed no
    # ``harmonia_port=`` in the file so it falls back to default 5000.
    # The URLs were written with port 6000 -> mismatch.
    with pytest.raises(MeshAssertionError, match="port"):
        assert_mesh_shape(mesh, n_secondaries=2)


def test_assert_mesh_shape_explicit_expected_port(tmp_path: Path) -> None:
    """Caller can override the expected port -- T7 will read it from
    a future connection_info field, but the override stays as the
    explicit override path."""
    run_dir = _make_run_dir(
        tmp_path, n_secondaries=2, harmonia_port=6000,
    )
    mesh = parse_mesh(run_dir, n_secondaries=2)
    assert_mesh_shape(
        mesh, n_secondaries=2, expected_harmonia_port=6000,
    )


def test_assert_mesh_shape_duplicate_peer_url(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_x"
    peers = run_dir / "peers"
    _write_substituters(
        peers, "secondary-0",
        [
            "http://slurm-worker2:5000",
            "http://slurm-worker2:5000",
        ],
    )
    _write_substituters(
        peers, "secondary-1",
        ["http://slurm-worker1:5000"],
    )
    _write_substituters(
        peers, "secondary-2",
        ["http://slurm-worker1:5000"],
    )
    mesh = parse_mesh(run_dir, n_secondaries=3)
    with pytest.raises(MeshAssertionError, match="duplicate peer URL"):
        assert_mesh_shape(mesh, n_secondaries=3)


def test_assert_mesh_shape_invalid_n(tmp_path: Path) -> None:
    mesh = MeshShape(
        n_secondaries=0,
        entries=(),
        hosts_by_secondary={},
        harmonia_port=5000,
        submitter=None,
    )
    with pytest.raises(ValueError, match=">= 1"):
        assert_mesh_shape(mesh, n_secondaries=0)


# ---------------------------------------------------------------------------
# probe_substituter_reachability (mocked ClusterProbe)
# ---------------------------------------------------------------------------


class _FakeProbe:
    """Hand-rolled :class:`ClusterProbe` mock returning canned curl
    outputs keyed by ``(worker, url)``.

    ``responses[(worker, url)]`` is a tuple ``(returncode, stdout, stderr)``;
    missing keys default to a successful 200 with a ``Priority`` header so
    the happy-path tests don't have to enumerate every URL.
    """

    def __init__(
        self,
        responses: dict[tuple[str, str], tuple[int, str, str]] | None = None,
        *,
        raise_on: tuple[str, str] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.raise_on = raise_on
        self.calls: list[tuple[str, str]] = []

    def worker_ssh(
        self,
        worker: str,
        cmd: str,
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # Extract URL from the curl command for keyed lookup.
        # ``cmd`` ends with the quoted URL; we strip the quotes.
        url = cmd.rsplit(" ", 1)[-1].strip("'\"")
        self.calls.append((worker, url))
        if self.raise_on is not None and self.raise_on == (worker, url):
            raise subprocess.TimeoutExpired(cmd, timeout or 5.0)
        rc, stdout, stderr = self.responses.get(
            (worker, url),
            (
                0,
                "HTTP/1.1 200 OK\r\nPriority: 30\r\n\r\n",
                "",
            ),
        )
        return subprocess.CompletedProcess(
            args=["ssh", worker, cmd],
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
        )


def _build_n4_mesh() -> MeshShape:
    """Build an N=4 :class:`MeshShape` programmatically (no on-disk
    fixture needed for the probe path)."""
    workers = ["slurm-worker1", "slurm-worker2", "slurm-worker3", "slurm-worker4"]
    entries = []
    for i, w in enumerate(workers):
        peer_urls = tuple(
            f"http://{workers[j]}:5000" for j in range(4) if j != i
        )
        entries.append(
            SubstitutersEntry(
                secondary_id=f"secondary-{i}",
                path=Path(f"/tmp/_substituters.secondary-{i}.txt"),
                urls=peer_urls,
                keys=("asm-suit-cluster-X:KEY=",),
            ),
        )
    return MeshShape(
        n_secondaries=4,
        entries=tuple(entries),
        hosts_by_secondary={
            f"secondary-{i}": w for i, w in enumerate(workers)
        },
        harmonia_port=5000,
        submitter=None,
    )


def test_probe_substituter_reachability_happy(monkeypatch: Any) -> None:
    mesh = _build_n4_mesh()
    probe = _FakeProbe()
    results = probe_substituter_reachability(probe, mesh)
    # 4 secondaries x 3 peer URLs = 12 probes.
    assert len(results) == 12
    assert all(r.error is None for r in results)
    assert all(r.http_status == 200 for r in results)
    assert all(r.has_priority_header for r in results)
    # Every probed worker is one of the four; we never SSH into the
    # secondary that OWNS the URL (the URL is on a peer's worker).
    assert {w for w, _ in probe.calls} == {
        "slurm-worker1", "slurm-worker2", "slurm-worker3", "slurm-worker4",
    }


def test_probe_substituter_reachability_skips_submitter() -> None:
    """The submitter URL points at the dev-box, not a worker; probing
    it via worker_ssh would mis-target. Default skip_submitter=True."""
    workers = ["slurm-worker1", "slurm-worker2"]
    entries = []
    for i, w in enumerate(workers):
        peer_urls = tuple(
            f"http://{workers[j]}:5000" for j in range(2) if j != i
        ) + ("http://localhost:5005",)
        entries.append(
            SubstitutersEntry(
                secondary_id=f"secondary-{i}",
                path=Path(f"/tmp/_substituters.secondary-{i}.txt"),
                urls=peer_urls,
                keys=(),
            ),
        )
    mesh = MeshShape(
        n_secondaries=2, entries=tuple(entries),
        hosts_by_secondary={
            "secondary-0": "slurm-worker1",
            "secondary-1": "slurm-worker2",
        },
        harmonia_port=5000,
        submitter=SubmitterEntry(url="http://localhost:5005", port=5005),
    )
    probe = _FakeProbe()
    results = probe_substituter_reachability(probe, mesh)
    # 2 secondaries x 1 peer URL each = 2 probes; the submitter URL
    # is excluded.
    assert len(results) == 2
    assert all("localhost" not in r.url for r in results)


def test_probe_substituter_reachability_curl_nonzero() -> None:
    mesh = _build_n4_mesh()
    bad_url = "http://slurm-worker2:5000"
    probe = _FakeProbe(
        responses={
            ("slurm-worker2", f"{bad_url}/nix-cache-info"): (
                7,
                "",
                "curl: (7) Failed to connect",
            ),
        },
    )
    results = probe_substituter_reachability(probe, mesh)
    bad_results = [r for r in results if r.url == bad_url]
    assert bad_results
    for r in bad_results:
        assert r.error is not None and "exit=7" in r.error


def test_probe_substituter_reachability_404_status() -> None:
    mesh = _build_n4_mesh()
    bad_url = "http://slurm-worker3:5000"
    probe = _FakeProbe(
        responses={
            ("slurm-worker3", f"{bad_url}/nix-cache-info"): (
                0,
                "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n",
                "",
            ),
        },
    )
    results = probe_substituter_reachability(probe, mesh)
    bad_results = [r for r in results if r.url == bad_url]
    assert bad_results
    for r in bad_results:
        assert r.error is None
        assert r.http_status == 404
        assert r.has_priority_header is False


def test_probe_substituter_reachability_ssh_exception() -> None:
    mesh = _build_n4_mesh()
    bad_url = "http://slurm-worker4:5000"
    probe = _FakeProbe(
        raise_on=("slurm-worker4", f"{bad_url}/nix-cache-info"),
    )
    results = probe_substituter_reachability(probe, mesh)
    bad_results = [r for r in results if r.url == bad_url]
    assert bad_results
    for r in bad_results:
        assert r.error is not None
        assert "TimeoutExpired" in r.error


def test_probe_substituter_reachability_no_status_line() -> None:
    mesh = _build_n4_mesh()
    bad_url = "http://slurm-worker1:5000"
    probe = _FakeProbe(
        responses={
            ("slurm-worker1", f"{bad_url}/nix-cache-info"): (
                0, "garbage non-http output\n", "",
            ),
        },
    )
    results = probe_substituter_reachability(probe, mesh)
    bad_results = [r for r in results if r.url == bad_url]
    for r in bad_results:
        assert r.error is not None and "no HTTP status" in r.error


def test_probe_substituter_url_already_trailing_slash() -> None:
    """When a future writer emits trailing-slash URLs, the curl command
    must not double up the slash."""
    entry = SubstitutersEntry(
        secondary_id="secondary-0",
        path=Path("/tmp/_substituters.secondary-0.txt"),
        urls=("http://slurm-worker2:5000/",),
        keys=(),
    )
    mesh = MeshShape(
        n_secondaries=1, entries=(entry,),
        hosts_by_secondary={"secondary-0": "slurm-worker1"},
        harmonia_port=5000, submitter=None,
    )
    probe = _FakeProbe()
    probe_substituter_reachability(probe, mesh)
    # Exactly one call, with the URL ending in
    # ``/nix-cache-info`` (no double slash).
    assert len(probe.calls) == 1
    _, url = probe.calls[0]
    assert url == "http://slurm-worker2:5000/nix-cache-info"


# ---------------------------------------------------------------------------
# Real-world fixture cross-check
# ---------------------------------------------------------------------------


def test_real_world_n2_substituter_format(tmp_path: Path) -> None:
    """Mirror the real on-disk format observed in
    ``run_20260508_173611/peers/_substituters.secondary-0.txt`` so a
    future writer-side change to ``_write_substituters_file`` trips
    one of our parsers."""
    target = tmp_path / "_substituters.secondary-0.txt"
    target.write_text(
        textwrap.dedent(
            """\
            --extra-substituters
            http://slurm-worker4:5000
            --extra-trusted-public-keys
            asm-suit-cluster-20260508T153636:nyaWSqeULmRhOaKL6gOV6eXKmgRBYqbtQZdW0czI/ps=
            """
        ),
        encoding="utf-8",
    )
    entry = parse_substituters_file(target)
    assert entry.urls == ("http://slurm-worker4:5000",)
    assert entry.keys == (
        "asm-suit-cluster-20260508T153636:"
        "nyaWSqeULmRhOaKL6gOV6eXKmgRBYqbtQZdW0czI/ps=",
    )
