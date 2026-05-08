"""Peer-mesh format + reachability assertions for multi-secondary runs.

This module is the home of the structured invariants the multi-node
clean-path test (T7) needs on top of the standard 7-invariant harness:

* parsing of the per-secondary ``_substituters.secondary-N.txt`` files
  emitted by :class:`compiler_suit_runner.peer_cache.PeerListWatcher`,
* shape assertions over the assembled mesh (each secondary lists exactly
  ``N - 1`` peer URLs, URL format matches the worker's harmonia, optional
  submitter URL recognised separately),
* per-URL HTTP reachability probes via the gateway-ProxyJump SSH wrappers
  on :class:`compiler_suit_runner.tests.slurm.cluster_probe.ClusterProbe`.

The parsers are file-only and consume the locally-mounted run-log tree;
the reachability probes need a live :class:`ClusterProbe`. T4's basic
peer-mesh sanity (count + per-file shape) is intentionally not folded
in here -- it lives inline in T4 and exercises a strictly weaker
contract. T7 extends that to:

* per-file URL count == ``N - 1`` (excluding the file's own secondary
  AND the submitter, when published);
* per-URL host matches the worker's ``connection_info/secondary-M.info``
  hostname;
* per-URL port matches the harmonia bind port (5000 in the current image
  layout); when a per-secondary harmonia port is recorded under
  ``connection_info`` we read it from there instead;
* HTTP 200 + ``Priority`` header on ``<url>nix-cache-info`` for every
  peer URL, fetched via ``curl --max-time 5`` from the corresponding
  worker (submitter URL is excluded from the reachability probe -- the
  submitter listens on the gateway, not on a worker).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from compiler_suit_runner.tests.slurm.cluster_probe import ClusterProbe


__all__ = [
    "DEFAULT_HARMONIA_PORT",
    "MeshAssertionError",
    "MeshShape",
    "ProbeResult",
    "SubmitterEntry",
    "SubstitutersEntry",
    "assert_mesh_shape",
    "parse_mesh",
    "parse_substituters_file",
    "probe_substituter_reachability",
    "read_connection_info",
]


# Default harmonia port baked into the image-side ``HarmoniaProcess``
# bind; secondaries listen on ``http://<host>:5000/`` unless a future
# image bump changes the port. See
# :class:`compiler_suit_runner.peer_cache.HarmoniaProcess`.
DEFAULT_HARMONIA_PORT = 5000

# Hostname of the submitter-published peer URL. The submitter publishes
# ``hostname=localhost`` (see
# :meth:`compiler_suit_runner.peer_cache.SubmitterPeer._publish_peer_file`)
# so the per-compute-node SSH-R fan-out is reachable as
# ``http://localhost:<gateway_port>``. We treat ANY entry whose URL host
# is ``localhost`` (or ``127.0.0.1``) as the submitter, regardless of
# port -- the submitter port is config-dependent (default 5005) and may
# differ between dispatches.
_SUBMITTER_HOSTS = frozenset({"localhost", "127.0.0.1"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MeshAssertionError(AssertionError):
    """Raised when the parsed mesh fails a shape assertion.

    Inherits from :class:`AssertionError` so a test that wraps the
    assertion call gets the same failure surface as a bare ``assert``;
    explicit subclass lets callers catch it without swallowing the
    weaker ``AssertionError``.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class SubstitutersEntry:
    """Parsed contents of one ``_substituters.secondary-N.txt`` file.

    Mirrors the canonical 4-line shape produced by
    :func:`compiler_suit_runner.peer_cache._write_substituters_file`:
    one ``--extra-substituters`` line followed by one URL line (multiple
    URLs are space-separated within that single argv arg), then
    ``--extra-trusted-public-keys`` and the matching keys line.
    Empty / single-peer files (the N=1 path) are still parseable: the
    URL/key lines may be missing entirely, in which case both
    :attr:`urls` and :attr:`keys` are empty tuples.
    """

    secondary_id: str
    """The ``secondary-N`` identifier this file belongs to (parsed
    from the filename ``_substituters.secondary-<N>.txt``)."""

    path: Path
    """Filesystem path of the parsed file, retained for diagnostics."""

    urls: tuple[str, ...] = ()
    """Peer substituter URLs in file order. May contain duplicates; the
    consumer is expected to dedupe via :func:`set`. Whitespace is
    stripped from each entry. Empty when the file has no peer URLs."""

    keys: tuple[str, ...] = ()
    """Trusted public-key strings (``name:base64...``) in file order.
    Aligned with :attr:`urls` only by convention -- nix accepts either
    ordering, so callers should not assume index-matching."""


@dataclasses.dataclass(frozen=True, slots=True)
class SubmitterEntry:
    """Submitter peer entry parsed out of a substituters file.

    The submitter publishes its own ``submitter.json`` peer file (see
    :class:`compiler_suit_runner.peer_cache.SubmitterPeer`) which the
    secondaries' ``PeerListWatcher`` picks up and folds into every
    secondary's substituters file. The URL hostname is ``localhost``
    (the per-node SSH-R fan-out target); we surface it as a separate
    field so :func:`assert_mesh_shape` can exclude it from the per-file
    "exactly ``N-1`` peer URLs" check without the caller threading the
    submitter URL through manually.
    """

    url: str
    """Full ``http://localhost:<port>`` URL the submitter advertised."""

    port: int
    """Port the submitter listens on (gateway-side; per-node SSH-R)."""


@dataclasses.dataclass(frozen=True, slots=True)
class MeshShape:
    """Structured view of one run's peer mesh.

    Built by :func:`parse_mesh` from the per-secondary substituter files
    + ``connection_info/secondary-N.info`` records. Carries everything
    :func:`assert_mesh_shape` and :func:`probe_substituter_reachability`
    need without re-walking the run dir.
    """

    n_secondaries: int
    """Number of ``_substituters.secondary-N.txt`` files found
    under ``peers/``. The caller is expected to compare this against
    the dispatched ``--jobs`` value separately; :func:`parse_mesh`
    reports what it sees on disk."""

    entries: tuple[SubstitutersEntry, ...]
    """Per-secondary parsed substituter file. Ordered by
    ``secondary_id`` (lexicographic) so iteration is deterministic."""

    hosts_by_secondary: Mapping[str, str]
    """Mapping from ``secondary-N`` -> hostname read from
    ``connection_info/secondary-N.info``. Missing files are absent
    from the map; callers walking ``entries`` MUST handle the missing
    case (the ``connection_info/`` write is best-effort: a worker that
    crashed before publishing won't have one)."""

    harmonia_port: int
    """Harmonia bind port the test will assert against. Read from
    ``connection_info/secondary-N.info`` if a ``harmonia_port=`` key
    is present in any of those files; otherwise
    :data:`DEFAULT_HARMONIA_PORT`."""

    submitter: SubmitterEntry | None
    """Submitter URL extracted from any of the substituter files, or
    ``None`` if no entry referenced ``localhost``. Multiple entries
    referencing the same submitter URL collapse to a single value;
    inconsistent ports raise :class:`MeshAssertionError` from
    :func:`assert_mesh_shape`."""


@dataclasses.dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of one substituter URL reachability probe.

    Returned by :func:`probe_substituter_reachability` (one entry per
    peer URL, per substituters file the URL appears in). The test asserts
    ``error is None`` and ``http_status == 200`` for every result; the
    ``has_priority_header`` field is a softer signal (older
    nix-serve emits a slightly different header set) used for diagnostics
    when the harder assertions trip.
    """

    secondary_id: str
    """``secondary-N`` ID of the substituters file this URL came from."""

    url: str
    """Probed peer URL (verbatim from the substituters file)."""

    http_status: int | None
    """HTTP status code parsed from curl's response headers, or
    ``None`` if curl failed before receiving any response."""

    has_priority_header: bool
    """True iff the response carried a ``Priority:`` header.
    nix-cache-info responses include it; misconfigured proxies or a
    stale port-grabber typically don't."""

    error: str | None
    """Short human-readable error string when the probe failed (curl
    timeout, ssh connection refused, parse error). ``None`` on success.
    Probe failures are surfaced as data so the test assertion can list
    every failure rather than aborting on the first."""


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------


_SECONDARY_ID_RE = re.compile(r"^_substituters\.(secondary-\d+)\.txt$")


def parse_substituters_file(path: Path) -> SubstitutersEntry:
    """Parse one ``_substituters.secondary-N.txt`` file into a record.

    The 4-line canonical shape (see module docstring) is emitted by
    :func:`compiler_suit_runner.peer_cache._write_substituters_file`,
    one argv element per line. The parser tolerates several deviations:

    * trailing blank lines (the writer always appends a trailing
      newline; some editors add another),
    * a missing trailing-newline file (truncated or pre-newline read),
    * a 2-line file (``--extra-substituters`` + URLs, no keys block) --
      pruned during early bring-up before the keys are seeded,
    * an empty file -- the watcher wrote zero peers; both URLs and
      keys are returned empty.

    URL/key lines are split on whitespace; empty tokens are dropped.
    Anything that does not match the canonical sentinel ordering raises
    :class:`MeshAssertionError` because it indicates a writer-format
    drift the test slice should catch loudly.
    """
    name = path.name
    m = _SECONDARY_ID_RE.match(name)
    if m is None:
        raise MeshAssertionError(
            f"file name {name!r} does not match "
            f"'_substituters.secondary-N.txt'",
        )
    secondary_id = m.group(1)
    text = path.read_text(encoding="utf-8")
    # Strip purely-empty trailing lines so a writer that accidentally
    # appended a second '\n' doesn't trip the sentinel-position check.
    raw_lines = text.splitlines()
    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()
    if not raw_lines:
        return SubstitutersEntry(
            secondary_id=secondary_id, path=path, urls=(), keys=(),
        )
    # Locate sentinels. The writer always emits ``--extra-substituters``
    # before any URL line and ``--extra-trusted-public-keys`` before
    # the keys line; if either sentinel is missing we treat the whole
    # file as a malformed write.
    if raw_lines[0] != "--extra-substituters":
        raise MeshAssertionError(
            f"{path.name}: line 1 is {raw_lines[0]!r}, expected "
            "'--extra-substituters'",
        )
    urls_line = raw_lines[1] if len(raw_lines) >= 2 else ""
    urls = tuple(t for t in urls_line.split() if t)
    keys: tuple[str, ...] = ()
    if len(raw_lines) >= 3:
        if raw_lines[2] != "--extra-trusted-public-keys":
            raise MeshAssertionError(
                f"{path.name}: line 3 is {raw_lines[2]!r}, expected "
                "'--extra-trusted-public-keys'",
            )
        keys_line = raw_lines[3] if len(raw_lines) >= 4 else ""
        keys = tuple(t for t in keys_line.split() if t)
    return SubstitutersEntry(
        secondary_id=secondary_id, path=path, urls=urls, keys=keys,
    )


def read_connection_info(
    path: Path,
) -> Mapping[str, str]:
    """Parse a ``connection_info/secondary-N.info`` file into a dict.

    The file is a flat ``key=value`` text file with no quoting. We
    return an empty mapping on any read / parse error; the file is
    best-effort produced by the framework and individual missing keys
    must not trip the higher-level assertions.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def parse_mesh(
    run_log_dir: Path, n_secondaries: int,
) -> MeshShape:
    """Build a :class:`MeshShape` from a run-log directory.

    ``run_log_dir`` is the root of a single run, typically
    ``<SLURM_TEST_ENV_LOG_ROOT>/run_<TS>``. We scan
    ``run_log_dir/peers/`` for the per-secondary substituter files and
    ``run_log_dir/connection_info/`` for the per-secondary host/port
    records; the union populates :class:`MeshShape`.

    ``n_secondaries`` is informational here -- we DO check that we found
    the expected number of substituter files, but a mismatch is reported
    via :func:`assert_mesh_shape` rather than raised from the parser
    (the parser stays purely descriptive so it can be reused for
    diagnostics on partial / failed runs).
    """
    if n_secondaries < 1:
        raise ValueError(
            f"n_secondaries must be >= 1, got {n_secondaries!r}",
        )
    peers_dir = run_log_dir / "peers"
    if not peers_dir.is_dir():
        raise MeshAssertionError(
            f"peers/ dir missing under {run_log_dir!s}; "
            "peer mesh did not initialise",
        )
    entries: list[SubstitutersEntry] = []
    for entry in sorted(peers_dir.iterdir()):
        if not entry.is_file():
            continue
        if _SECONDARY_ID_RE.match(entry.name) is None:
            continue
        entries.append(parse_substituters_file(entry))

    # Connection-info dir is optional (legacy runs / partial writes).
    hosts_by_secondary: dict[str, str] = {}
    harmonia_port: int = DEFAULT_HARMONIA_PORT
    conn_dir = run_log_dir / "connection_info"
    if conn_dir.is_dir():
        for info in sorted(conn_dir.iterdir()):
            if not info.is_file():
                continue
            m = re.match(r"^(secondary-\d+)\.info$", info.name)
            if m is None:
                continue
            sec_id = m.group(1)
            data = read_connection_info(info)
            host = data.get("hostname")
            if host:
                hosts_by_secondary[sec_id] = host
            port_s = data.get("harmonia_port")
            if port_s:
                try:
                    harmonia_port = int(port_s)
                except ValueError:
                    # Keep the default -- a malformed override should
                    # not silently become 0 or trip every URL probe.
                    pass

    # Submitter detection. The submitter URL appears verbatim across
    # every secondary's substituters file (the watcher just lists every
    # peer including the submitter). We collapse to one entry; if we
    # see two different submitter URLs (e.g. localhost:5005 and
    # localhost:7777 in the same run, which would be a writer bug) we
    # surface that as a parse-time failure.
    submitter: SubmitterEntry | None = None
    seen_urls: set[str] = set()
    for entry in entries:
        for url in entry.urls:
            if not _is_submitter_url(url):
                continue
            seen_urls.add(url)
    if len(seen_urls) > 1:
        raise MeshAssertionError(
            "multiple submitter URLs observed in substituter files: "
            + ", ".join(sorted(seen_urls)),
        )
    if seen_urls:
        url = next(iter(seen_urls))
        port = _port_from_url(url) or 0
        submitter = SubmitterEntry(url=url, port=port)
    return MeshShape(
        n_secondaries=len(entries),
        entries=tuple(entries),
        hosts_by_secondary=dict(hosts_by_secondary),
        harmonia_port=harmonia_port,
        submitter=submitter,
    )


def _is_submitter_url(url: str) -> bool:
    """True iff ``url`` looks like a submitter peer URL.

    The submitter publishes ``hostname=localhost``; the watcher emits
    ``http://localhost:<gateway_port>`` (no trailing slash) into
    every secondary's substituters file. ``127.0.0.1`` is accepted as
    an alias for forward-compat with a future submitter-side change.
    """
    parsed = urlparse(url)
    return parsed.hostname in _SUBMITTER_HOSTS


def _port_from_url(url: str) -> int | None:
    """Extract the explicit port from ``url``, or ``None`` if absent."""
    parsed = urlparse(url)
    return parsed.port


# ---------------------------------------------------------------------------
# Shape assertions
# ---------------------------------------------------------------------------


def assert_mesh_shape(
    mesh: MeshShape,
    n_secondaries: int,
    *,
    require_submitter: bool = False,
    expected_harmonia_port: int | None = None,
) -> None:
    """Hard-assert the parsed mesh matches the expected N-secondary shape.

    Checks performed:

    * the number of substituter files matches ``n_secondaries``;
    * each file lists exactly ``n_secondaries - 1`` peer URLs with
      ``host`` matching another secondary's ``connection_info`` host
      and port matching :attr:`MeshShape.harmonia_port` (default 5000);
    * the file does NOT list its own secondary's URL (self-referential
      substitution would deadlock harmonia);
    * URLs are unique per file (the watcher emits each peer once);
    * if any substituters file mentions a localhost URL, the same
      ``localhost:<port>`` appears in every other file too -- the
      submitter is global, not per-secondary;
    * when ``require_submitter=True``, asserts at least one localhost
      URL appears (used by tests that publish the submitter peer
      explicitly and want to verify it landed in the mesh).

    On any failure raises :class:`MeshAssertionError` with a
    diagnostic message naming the offending file and URL.
    """
    if n_secondaries < 1:
        raise ValueError(
            f"n_secondaries must be >= 1, got {n_secondaries!r}",
        )
    expected_port = (
        expected_harmonia_port
        if expected_harmonia_port is not None
        else mesh.harmonia_port
    )
    if mesh.n_secondaries != n_secondaries:
        raise MeshAssertionError(
            f"mesh has {mesh.n_secondaries} substituter file(s); "
            f"expected {n_secondaries}",
        )

    # Build the canonical "expected peer URL" set: one URL per
    # secondary other than the file's own. We use the connection_info
    # mapping when available; entries without a connection_info record
    # are still asserted to APPEAR but their host string is treated as
    # any hostname (the test surfaces the missing hostname separately).
    for entry in mesh.entries:
        sec_id = entry.secondary_id
        # Split URLs into "peer harmonia" vs "submitter" for the count
        # check; the N-1 expectation excludes the submitter.
        peer_urls = tuple(u for u in entry.urls if not _is_submitter_url(u))
        sub_urls = tuple(u for u in entry.urls if _is_submitter_url(u))

        # Uniqueness within the peer list -- the watcher emits each
        # peer exactly once; duplicates indicate either a writer bug
        # or a spurious announce/withdraw race.
        if len(peer_urls) != len(set(peer_urls)):
            raise MeshAssertionError(
                f"{entry.path.name}: duplicate peer URLs {peer_urls!r}",
            )

        expected_peer_count = n_secondaries - 1
        if len(peer_urls) != expected_peer_count:
            raise MeshAssertionError(
                f"{entry.path.name}: lists {len(peer_urls)} peer URL(s); "
                f"expected {expected_peer_count} for N={n_secondaries}. "
                f"URLs: {peer_urls!r}",
            )

        # Submitter consistency: same URL across every file (or absent
        # everywhere). When require_submitter is set, every file must
        # carry the submitter URL.
        if mesh.submitter is not None:
            if not sub_urls:
                if require_submitter:
                    raise MeshAssertionError(
                        f"{entry.path.name}: submitter URL "
                        f"{mesh.submitter.url!r} expected but missing",
                    )
            elif set(sub_urls) != {mesh.submitter.url}:
                raise MeshAssertionError(
                    f"{entry.path.name}: submitter URLs {sub_urls!r} "
                    f"do not match canonical {mesh.submitter.url!r}",
                )
        elif require_submitter:
            raise MeshAssertionError(
                f"{entry.path.name}: submitter URL expected but no "
                "localhost entry present in any substituters file",
            )

        own_host = mesh.hosts_by_secondary.get(sec_id)
        peer_hosts: set[str] = set()
        for url in peer_urls:
            parsed = urlparse(url)
            if parsed.scheme != "http":
                raise MeshAssertionError(
                    f"{entry.path.name}: URL {url!r} has scheme "
                    f"{parsed.scheme!r}; expected 'http'",
                )
            host = parsed.hostname or ""
            port = parsed.port
            if not host:
                raise MeshAssertionError(
                    f"{entry.path.name}: URL {url!r} has no host",
                )
            if port != expected_port:
                raise MeshAssertionError(
                    f"{entry.path.name}: URL {url!r} port {port!r} != "
                    f"expected harmonia port {expected_port}",
                )
            if own_host is not None and host == own_host:
                raise MeshAssertionError(
                    f"{entry.path.name}: lists its own host {host!r} "
                    "as a peer; PeerListWatcher should have excluded it",
                )
            if host in peer_hosts:
                raise MeshAssertionError(
                    f"{entry.path.name}: host {host!r} appears in "
                    "multiple peer URLs",
                )
            peer_hosts.add(host)

        # When connection_info covers the full N, assert the peer set
        # is exactly "every other secondary's host". We don't enforce
        # this when connection_info is partial (a worker may have
        # crashed before publishing); the test's caller chooses whether
        # that's a hard fail via the standard 7 invariants.
        known_hosts = {
            h for sid, h in mesh.hosts_by_secondary.items() if sid != sec_id
        }
        if known_hosts and len(known_hosts) >= expected_peer_count:
            missing = known_hosts - peer_hosts
            if missing:
                raise MeshAssertionError(
                    f"{entry.path.name}: peer URLs miss expected "
                    f"host(s) {sorted(missing)!r}; saw {sorted(peer_hosts)!r}",
                )
        # Cross-check submitter URL consistency outside the per-file
        # loop is enforced by the global ``submitter`` field above.


# ---------------------------------------------------------------------------
# Reachability probes
# ---------------------------------------------------------------------------


_HTTP_STATUS_RE = re.compile(r"^HTTP/[0-9.]+\s+(\d{3})\b")
_PRIORITY_HEADER_RE = re.compile(r"^[Pp]riority:")


def probe_substituter_reachability(
    probe: ClusterProbe,
    mesh: MeshShape,
    *,
    timeout_per_url_s: float = 5.0,
    skip_submitter: bool = True,
) -> list[ProbeResult]:
    """Curl each peer URL via SSH-into-its-worker; return per-URL results.

    For every ``(secondary_id, url)`` combination in ``mesh.entries``:

    * resolve the worker hostname via :attr:`MeshShape.hosts_by_secondary`
      lookup against the URL's hostname (we SSH to the worker that
      RUNS that harmonia, not from it -- the harmonia answers on its
      worker's loopback);
    * run ``curl -sS -o /dev/null -D - --max-time <T> <url>nix-cache-info``
      remotely -- ``-D -`` writes the response headers to stdout, ``-o
      /dev/null`` drops the body (we don't care about contents). The
      first line is the status line we parse for HTTP code.

    Submitter URLs are skipped by default (``skip_submitter=True``):
    the submitter's harmonia listens on the dev-box, NOT on a worker,
    and the per-compute-node SSH-R fan-out makes ``localhost:<port>``
    a different endpoint depending on which compute-node you're on.
    Tests that want to assert submitter reachability should use
    :class:`compiler_suit_runner.peer_cache.SubmitterPeer` health
    probes directly.

    Errors at any step (worker not in :attr:`hosts_by_secondary`, ssh
    timeout, curl exit non-zero, missing status line) land in the
    :class:`ProbeResult.error` field; the test loops over the results
    and asserts each. Network errors are NOT raised.
    """
    # Build a host -> probing-worker map: for each peer URL we SSH to
    # the SECONDARY that hosts that URL. We index by hostname so the
    # lookup is symmetric ("URL host slurm-worker3" -> "ssh slurm-worker3").
    host_to_secondary: dict[str, str] = {
        host: sid for sid, host in mesh.hosts_by_secondary.items()
    }

    results: list[ProbeResult] = []
    for entry in mesh.entries:
        for url in entry.urls:
            if skip_submitter and _is_submitter_url(url):
                continue
            results.append(
                _probe_one_url(
                    probe=probe,
                    secondary_id=entry.secondary_id,
                    url=url,
                    host_to_secondary=host_to_secondary,
                    timeout_per_url_s=timeout_per_url_s,
                ),
            )
    return results


def _probe_one_url(
    *,
    probe: ClusterProbe,
    secondary_id: str,
    url: str,
    host_to_secondary: Mapping[str, str],
    timeout_per_url_s: float,
) -> ProbeResult:
    """SSH-into-worker + curl the URL; build a :class:`ProbeResult`.

    Pulled out of the loop body so the loop's own control flow stays
    flat and unit-tests can call this directly with a mocked probe.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return ProbeResult(
            secondary_id=secondary_id, url=url, http_status=None,
            has_priority_header=False,
            error="URL has no host component",
        )
    # The URL host IS the worker hostname (slurm-worker{1..4}); the
    # gateway-side DNS resolves them and ``ClusterProbe.worker_ssh``
    # ProxyJumps through it. We retain the ``host_to_secondary`` map
    # for diagnostics in the error path / future extensions, but the
    # SSH target is always the URL hostname directly.
    _ = host_to_secondary  # kept for future enrichment; see docstring
    target_host = host
    # ``curl -D -`` dumps headers to stdout; ``-o /dev/null`` drops
    # the body. The trailing slash on the URL is preserved verbatim;
    # ``nix-cache-info`` is appended without a separator because the
    # framework's URL form does NOT carry a trailing slash (see
    # ``PeerInfo.substituter_url``).
    if not url.endswith("/"):
        target_url = f"{url}/nix-cache-info"
    else:
        target_url = f"{url}nix-cache-info"
    cmd = (
        f"curl -sS -o /dev/null -D - --max-time "
        f"{int(timeout_per_url_s)} {_quote(target_url)}"
    )
    # Use the gateway timeout floor + a small margin so the SSH wrapper
    # doesn't abort before curl times out.
    ssh_timeout = max(timeout_per_url_s + 5.0, 10.0)
    try:
        cp = probe.worker_ssh(
            target_host, cmd, timeout=ssh_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - normalize to error string
        return ProbeResult(
            secondary_id=secondary_id, url=url, http_status=None,
            has_priority_header=False,
            error=f"ssh/curl failed: {type(exc).__name__}: {exc}",
        )
    if cp.returncode != 0:
        return ProbeResult(
            secondary_id=secondary_id, url=url, http_status=None,
            has_priority_header=False,
            error=(
                f"curl exit={cp.returncode} "
                f"stderr={cp.stderr.strip()[:200]!r}"
            ),
        )
    status, has_priority = _parse_curl_response_headers(cp.stdout)
    if status is None:
        return ProbeResult(
            secondary_id=secondary_id, url=url, http_status=None,
            has_priority_header=has_priority,
            error=(
                "no HTTP status line in curl output: "
                f"{cp.stdout.strip()[:200]!r}"
            ),
        )
    return ProbeResult(
        secondary_id=secondary_id, url=url, http_status=status,
        has_priority_header=has_priority,
        error=None,
    )


def _parse_curl_response_headers(
    stdout: str,
) -> tuple[int | None, bool]:
    """Extract ``(status, has_priority_header)`` from ``curl -D -`` output.

    curl emits one header block per response; with redirects there may
    be several. We use the LAST status line (the final response) and
    look for ``Priority:`` anywhere in the same block.
    """
    last_status: int | None = None
    has_priority = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HTTP_STATUS_RE.match(line)
        if m is not None:
            try:
                last_status = int(m.group(1))
            except ValueError:
                last_status = None
            # Reset priority flag when a new response starts: only the
            # final response's headers are reported.
            has_priority = False
            continue
        if _PRIORITY_HEADER_RE.match(line):
            has_priority = True
    return last_status, has_priority


def _quote(s: str) -> str:
    """Shell-quote a string for inclusion in a remote-shell command.

    We use :mod:`shlex` rather than open-coding because the SSH client
    passes the command through a remote ``bash -c``; an unquoted URL
    with shell metacharacters (``?``, ``&``) would split or expand.
    """
    import shlex

    return shlex.quote(s)


def _ensure_iterable(values: Iterable[str] | None) -> tuple[str, ...]:
    """Defensive coercion helper used in tests; lives here so the
    module surface is testable without leaking internals."""
    if values is None:
        return ()
    return tuple(values)
