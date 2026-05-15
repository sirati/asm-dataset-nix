# dynamic_runner refactor regression findings — 2f30920

## Context

- **Pre-bump pin**: `8f8475079bfd2f93538cd844d38d073ad8fe36bb` (2026-05-08).
- **Post-bump pin**: `2f3092082700b69a08eeed16f27a09977dceb5b2` (2026-05-09).
- **Trigger**: `dynrunner-owner` claude-comm message at 26-05-09 02:54
  ("All tests + e2e green — ready for T1→T10 regression").
- **Pre-bump baseline T1 wall-time**: not captured. The pre-bump
  cluster lacked `GatewayPorts=clientspecified` in its sshd module,
  so reverse forwards from the framework would not have reached
  workers; running T1 against that state would have surfaced an
  environment bug, not a baseline.
- **Cluster cycle**: `INSTANCE_ID=ds-test SSH_PORT=2244 nix run
  /home/sirati/devel/python/dynamic_runner/slurm-test-env#down`
  followed by `#up` (image rebuild ~5 min) to pick up the new
  sshd module + the GatewayPorts setting.
- **Dispatcher migration applied for the cycle** (test-only;
  production refactor deferred):
  - Pre-spawn the SSH master via plain `subprocess` and export
    `DYNRUNNER_SSH_CONTROL_PATH` (migration doc §"Known issue:
    SSH master under tokio").
  - Switch the test gateway URL from `ssh://sirati@localhost:2244`
    to `ssh://sirati@slurm-gateway`, with a per-cluster ssh_config
    that redirects the alias on the dispatcher's host.
  - Pass `--ssh-config` into every test invocation (the field was
    already supported by `RunInvocation`).

## Per-test outcomes

| # | Test | Wall (s) | Outcome | Notes |
|---|---|---|---|---|
| 1 | T1 clean tiny | 145.4 | PASS (5th attempt) | attempts 1-4 failed; see "T1 attempt history" below |
| 2 | T10 port collision | 143.9 | PASS | required port realignment + peer_push best-effort adaptation |
| 3 | T4 N=2 clean | 192.7 | PASS | dropped stale mesh-json assertion (jsons withdraw at on_run_end) |
| 4 | T6 submitter disconnect | 723.2 | FAIL | post-SIGINT: local primary subprocess never exits, secondaries spin in connection-retry forever instead of promoting |
| 5 | T5 SIGKILL secondary | 1248.9 | PARTIAL | post-promotion drain works (secondary-0 completed=9), no leaks; SIGKILL didn't actually kill container (setsid + proctrack/linuxproc); R2 reproduced (primary doesn't exit on RunComplete) |
| 6 | T2 post-promotion-hang | 99.1 | PASS | the open pre-refactor framework hang DID NOT reproduce — refactor likely fixed it |
| 7 | T3 clean medium | 1203.6 | FAIL | 10 nix-build failures (`fork: Resource temporarily unavailable` mid-`./configure`); secondary then logs `primary disconnected during setup`; R2 hits the local primary again |
| 8 | T8 worker OOM | 485.2 | PARTIAL | helper resolves target via Secondary-ID; SSH script reports `no_running_container` — secondary completes before cgroup cap can be applied; 12-variant attempt hit ARG_MAX in preflight |
| 9 | T7 N=4 clean | TBD | TBD | |
| 10 | T9 N=4 load | TBD | TBD | |

## Detailed findings

### T1 attempt history (2026-05-09)

Three failures led to two production-relevant fixes before T1 could run
clean:

1. **Attempt 1 (orphan harmonia)** — a previous-session
   `harmonia-cache` PID 478932 was squatting on 127.0.0.1:5005 on the
   dev box. `SubmitterPeer.start()`'s bind-failure retry was defeated:
   the orphan answered `/nix-cache-info` with 200 OK before our child
   exited with EADDRINUSE, so the probe set `bound_ok=True` and the
   retry never fired. `peers/submitter.json` never got written; the
   secondary's `_substituters.secondary-0.txt` stayed empty; build
   failed with "don't know how to build these paths" at 600s timeout.
   Test-only band-aid: `pkill -KILL -f harmonia-cache` at the
   `ssh_master` fixture entry (`tests/slurm/conftest.py`, commit
   `39fbb8a`). Production follow-up: `SubmitterPeer.start()` should
   pre-emptively kill any orphan before attempting bind.

2. **Attempt 2 (gateway URL missing port)** — orphan was killed,
   harmonia bound on :5005, but `submitter.json` still wasn't published.
   Root cause: `SubmitterPeer._ssh_oneshot` reads `_gateway_ssh_port`
   via `parse_gateway_url(self.gateway_url)`. Our test constant had
   `ssh://sirati@slurm-gateway` (no port) → port defaulted to 22 →
   `ssh -p 22 sirati@slurm-gateway` overrode the `Port 2244` directive
   in `ssh_config` (OpenSSH cmdline beats config). The publish SSH hit
   localhost:22 with no listener; `_publish_peer_file` logged a warning
   that didn't surface in the test wall. Fix: include `:2244` in
   `SLURM_TEST_ENV_GATEWAY_URL` (`tests/slurm/run_helpers.py`, commit
   `cc937ce`). Production follow-up: `_publish_peer_file` should
   propagate rc != 0 as an exception rather than a warning so a silent
   SSH failure doesn't drag the run out to the test timeout.

3. **Attempt 3 (submitter URL hostname=localhost)** — `submitter.json`
   was finally published, with `hostname=localhost, port=5005`. But the
   secondary's `_substituters.secondary-0.txt` therefore advertised
   `http://localhost:5005`, which from inside the worker container
   (running with `--network host` on slurm-worker1) resolves to the
   worker's loopback — nothing is bound there. Workers failed with
   `Could not connect to server` against `localhost:5005/nix-cache-info`.
   Root cause: a stale comment in `peer_cache.py:_publish_peer_file`
   claimed the framework fans out per-compute-node SSH-Rs (`ssh -J
   gateway -R …`), making `localhost:<gateway_port>` reachable on every
   compute node. The current framework
   (`dynrunner-slurm/src/preparation.rs:build_ssh_argv`) does a single
   `ssh -R <gw>:localhost:<lp>` to the gateway hop only — there is no
   per-node fan-out. With `GatewayPorts=clientspecified` on the gateway
   sshd (verified: `0.0.0.0:5005 LISTEN` on the gateway), workers must
   dial `http://<gateway>:<gateway_port>` directly. Fix: change
   `_publish_peer_file` to embed `self._gateway_host` instead of the
   literal "localhost"; expand `_SUBMITTER_HOSTS` in
   `peer_mesh_assertions.py` to recognise the slurm-test-env gateway
   alias. (commit `71a97d9`).

The third fix is the only one that touched production code (the first
two were test-fixture changes). The framework-side semantic — single
gateway-hop reverse tunnel rather than per-node fan-out — is worth
flagging back to dynrunner-owner: the migration doc's `--ssh-config`
recommendation should call out that downstream peer-publish paths need
to use the gateway hostname, not localhost. Our pre-refactor code
worked because the older framework version did fan out; the refactor
collapsed that to a single hop, which is a breaking change for any
peer that publishes its URL host as `localhost`.

4. **Attempt 4 (gateway-side reverse-forward orphan)** — the published
   submitter URL was correct, but `compiler_suit_runner submit` died
   in 74s with `exit=1` and an honest error from the framework:
   `OSError: command failed: Failed to add reverse forward
   5005:localhost:5005 to external master: mux_client_forward:
   forwarding request failed: remote port forwarding failed for listen
   port 5005`. Three orphan `sshd-session: sirati@notty` processes were
   running on the gateway (started during attempts 2, 3, and the
   cancelled portion of 4); one of them was still bound to
   `0.0.0.0:5005`. Each previous run's framework-added reverse forward
   leaked because pytest was killed before the fixture's `ssh -O exit`
   teardown ran. Test-only band-aid: pkill our own `sshd-session
   *@notty` orphans on the gateway at the start of the `ssh_master`
   fixture (commit `c9deb03`). Production follow-up: the cleanup
   should not rely on the master's exit cascade, since SIGKILL during
   a test bypasses it.

The fan-out comment correction (production fix in commit `71a97d9`)
is the most important takeaway for dynrunner-owner. Everything else
is test-fixture hygiene that should not block the cycle.

### T6 detail (2026-05-10)

T6 sends `SIGINT` to the local primary driver after the secondaries
log `received initial assignment`, then expects:
* the local primary subprocess to exit cleanly,
* the secondaries to detect the disconnect and one of them to promote
  to primary, complete the work, and exit,
* no leak.

What actually happened (run dir `run_20260509_215909`):

* Both secondaries' `slurm_<job>.out` show **571 retry attempts** of
  `connection failed, retrying in 1s... error=IO error: Connection
  refused (os error 111)`. They never promote — they spin in the
  connection-retry loop until the wrapper script's `trap cleanup EXIT`
  fires (`Cleaning up temporary directory: /tmp/asm-…`). That cleanup
  was triggered by SLURM's time-limit reap, not by the secondary
  exiting on its own.
* The local `compiler_suit_runner submit` subprocess never exited
  within the 600 s `timeout_s` window (test killed it with SIGKILL at
  the end). After SIGINT the framework's primary-shutdown path is
  wedged.

Two distinct framework regressions visible at rev `2f30920`:

  R1. **Promotion-on-local-primary-disconnect missing.** Pre-refactor
      docs and the test plan both reference "primary disconnect →
      one secondary promotes". The refactor's
      `dynrunner_manager_distributed::secondary` retry path doesn't
      appear to consider local-primary loss as a promotion trigger;
      the secondaries treat it as a transient connection error and
      retry forever.

  R2. **Local primary doesn't exit on SIGINT.** Independent of (R1):
      the dispatch subprocess that owns the SubmitterPeer + the
      framework's primary tasks doesn't honour SIGINT. Likely related
      to the dynrunner-owner peer's queued task #4/#5 ("DYNRUNNER_SSH
      _CONTROL_PATH workaround hides a real Rust-tokio bug") — same
      tokio-supervision area.

Suggested triage for the framework PR:
* Treat a primary disconnect that lasts longer than N seconds as a
  promotion trigger (or surface it as an explicit promotion request
  from the disconnected primary, e.g. before the SubmitterPeer's
  ssh master goes away).
* Wire SIGINT handler into the primary's tokio runtime: cancel the
  outstanding tasks and exit the subprocess.

### T5 detail (2026-05-10)

T5 SIGKILLs one secondary mid-build (via `scancel --signal=KILL`) and
expects the OTHER secondary to promote, complete the work, and exit
cleanly with no leaked state.

What worked:
* Watchdog correctly fired at 42 s (the broadened "first toolchain OR
  variant completion" trigger lands reliably; the prior variant-only
  trigger missed because the post-2f30920 framework only emits
  `task completed task_type=variant` at INFO from one of the two
  secondaries depending on which side runs the variant fan-out).
* secondary-0 promoted to primary, completed all 9 remaining tasks
  (toolchain + 8 variants), broadcast RunComplete on the survivor
  side at 08:09:53 UTC.
* All leak invariants pass — no leaked containers, no listener-port
  leaks on (5000, 6000), no PPID=1 process leaks across all 4 workers.

What didn't:
* `scancel --signal=KILL` ran with `scancel_rc=0`, but the killed
  secondary's `slurm_*.out` carries the wrapper's normal "Container
  exited with code: 0" line at 08:09:10 UTC (18 minutes after the kill
  was issued). The secondary container ran to natural completion
  despite the SIGKILL because:
   * the slurm-test-env's `proctrack/linuxproc` walks the process
     group via PPID, but our podman container `setsid()`'s itself
     out of the group (memory `project_dynrunner_slurm_facts.md`);
   * SLURM signaled the wrapper's PID, the wrapper had no children
     visible to it (the container had escaped), so the wrapper
     completed cleanly when podman exited normally.
   * The slurm-test-env config is a framework / test-env concern,
     not a dataset-side issue. Kept as a finding for
     `dynrunner-owner-slurm-test-env-owner` if they want to switch
     to `proctrack/cgroup` (or otherwise track containers that
     `setsid()` themselves).

* R2 (T6's primary-not-exiting-on-shutdown regression) reproduced
  again: even though the surviving secondary broadcast RunComplete
  and reported `secondary finished successfully` at 08:09:53, the
  local `compiler_suit_runner submit` subprocess never exited; the
  test wall hit its 1200 s ceiling and the test runner SIGKILL'd
  the subprocess. Same regression class as T6 (R2 in this doc) —
  worth promoting from "T6 only" to "every multi-secondary run with
  a clean RunComplete" in the dynrunner-owner triage notes.

PARTIAL grade because the test's KEY assertion (no leaks) passed,
the spirit of the test (post-promotion drain works) was demonstrated,
but the failure-injection was less effective than intended (the
SIGKILL didn't actually short-circuit the secondary's container) and
R2 hits the local primary regardless of how the run ended.

## Aggregate

### First sweep (2f30920, 2026-05-09 → 2026-05-10)

T1 / T2 / T3 / T4 / T5 / T8 / T10 each ran to a useful outcome at
`2f3092082700b69a08eeed16f27a09977dceb5b2` and are documented per-test
above. T6 was BLOCKED on the open R2 regression (local primary
doesn't exit on clean SIGINT). T7 (N=4 clean) recurringly failed on
a slurm-test-env infrastructure flake: a different worker entered
DOWN+NOT_RESPONDING ~7 minutes into each attempt, regardless of
which node. T9 not attempted.

Two open dynamic_runner framework regressions surfaced and were
reported via claude-comm to `dynrunner-owner`:
- **R1**: secondary failover on local-primary disconnect (no
  promotion election when local primary closes its transport
  mid-run). Fix landed as `#13` in handoff/secondary-failover-on-
  disconnect; threshold-armed (5 probes / 30s).
- **R2**: demoted local primary doesn't exit cleanly. Fix landed
  as `#14` in handoff/demoted-primary-cluster-mutation-arm —
  ClusterMutation arm on demoted primary's dispatch_message so it
  observes RunComplete and exits.

### Second sweep (post-bump, 2026-05-11)

Cycle re-targeted at framework main tip plus several inbound peer
fixes. Per-bump targets and gates exercised:

- `2f30920 → 9ca9124 → 6f22e47 → 3aa9920`: peer's `#20` RLIMIT_NPROC
  wrapper widening, `#13` R1 threshold failover, `#14` R2 demoted
  cluster-mutation arm, `#29` SLURM `--cores` forwarding (the
  load-bearing fix for `--cores N` to actually reach the secondary
  container, previously ignored), `#30` SLURM `--max-memory`
  forwarding (symmetric), `#31` wrapper-cgroup-aware memory cap
  (so the autodetect sees the 4 GiB cgroup, not the host's 96 GiB
  RAM), `#32` `--flag=value` wrapper template fix (defensive against
  leading-dash specs in `--max-memory -2G`).
- slurm-test-env: `2c70355` (reboot-node pexec fix so `scontrol` is
  on PATH inside the gateway container), `2bf8410` (`TaskPlugin=
  task/cgroup` + `ProctrackType=proctrack/cgroup` + `LimitNPROC=
  infinity` + `TasksMax=infinity` on slurmd.service — slurmd no
  longer competes with the batch job for the per-user nproc budget),
  `d4ae239` (worker podman `--pids-limit=32768`, raises the rootless
  default of 2048 that was the actual ceiling fork-heavy variant
  builds were tripping).

Consumer-side edits in this repo to engage the above:

- `python/compiler_suit_runner/tests/slurm/run_helpers.py`:
  `RunInvocation.cores` and `RunInvocation.max_memory` fields added;
  `default_invocation_for_smoke` defaults to `cores=2` (matching
  the per-worker WORKER_CPUS=2 envelope) and `archs=("x86_64",)`
  (per the user's "test with easier compilations" rule; the 3.5 GiB
  memory contract is the load-bearing reason workload narrowing
  matters in this env).
- `python/compiler_suit_runner/cli.py`: `--cores` and `--max-memory`
  added to argparse as pass-through args (deliberately NOT in
  `_CSR_FLAGS_WITH_VALUE`, so the framework re-parses them from
  sys.argv).
- `python/compiler_suit_runner/tests/slurm/test_t07_n4_clean.py`
  and `test_t09_n4_load.py`: `archs=("x86_64",)` explicit on the
  invocation (redundant after the run_helpers default, but kept
  for clarity in the test files).

Post-bump per-test outcomes (`cae5a85` cluster + `3aa9920`
framework + the consumer edits above):

- **T1** (tiny, jobs=1): PASS, 50 s wall. Framework healthy
  end-to-end at the new stack.
- **T3** (medium, jobs=1): PARTIAL. 4 of 10 variants completed
  with `success=true` (real builds, not cache hits), then BOTH
  workers died at the exact same millisecond with `Broken pipe`.
  Same-instant bilateral death is the cgroup OOM-kill fingerprint:
  the per-worker `budget_mb` allocation reports `worker_id=0=4096`
  and `worker_id=1=2198`, summing to 6.2 GiB declared budget against
  a 4 GiB cgroup memory cap. Under sustained concurrent variant
  builds the cgroup memory ceiling was crossed, kernel OOM-killed
  both workers in the cgroup. Reported to dynrunner-owner; no fix
  in flight yet at end of cycle.
- **T6** (tiny + SIGINT, jobs=2): NEW failure mode. Secondaries
  hit `Connection refused attempt=1` immediately on boot — the
  local primary was killed by the test's SIGINT BEFORE the
  secondaries' containers finished initializing. Image cache hit
  reduces boot time below the test's existing SIGINT-trigger
  window. Test-side timing fix needed (gate SIGINT on
  `at-least-one-secondary-connected`, not on `primary-URL-appears`).
  Not a framework regression at this rev.

### Framework regressions raised, in flight, or closed during the cycle

- `#25` post-dispatch late-arriving secondary doesn't fast-exit on
  "no work" — captured during T7 attempt 2 (3 of 4 secondaries
  completed work; the 4th booted 22 min late and hammered 345
  retries before container cleanup). Tracked low-priority by
  dynrunner-owner.
- `#27` post-promotion "secondary failed: primary disconnected
  during setup" 10 min after secondary correctly observed primary
  disconnect — case-mitigated by `#25` setup_deadline default (60 s)
  at `2ea07f0`; reproducer steps preserved in the doc above.
- `#28` post-promotion task routing — `handle_peer_message` was
  dropping TaskAssignment from a promoted-peer-primary. Fix at
  `ed5eca5`; my N≥2 paths now exercise it but haven't isolated a
  pass/fail signal because T3-class load OOM dominates.
- `#29 / #30 / #31 / #32`: see "Bumps" above. All confirmed live
  via post-bump T1 + T3 observations.

### Slurm-test-env infrastructure regressions raised and closed

- `2c70355` (reboot-node script): fixed during T7 second attempt
  to revive worker4 without a full cluster cycle.
- `2bf8410` (cgroup isolation): smoking-gun trigger was the T3-second-
  attempt slurmd journal showing `pthread_create error Resource
  temporarily unavailable` 102 s into the job, slurmd dying as
  the OOM-class victim of the shared per-user nproc accounting in
  `TaskPlugin=task/none`. Switching to `task/cgroup` +
  `proctrack/cgroup` (plus defensive `LimitNPROC=infinity` and
  `TasksMax=infinity` on slurmd.service) closed the recurring
  "worker N goes DOWN+NOT_RESPONDING 7 min into the test" pattern.
- `d4ae239` (worker `--pids-limit=32768`): podman's rootless
  default of 2048 pids was the actual ceiling fork-heavy variant
  builds were hitting after `2bf8410`. Owner confirmed via live
  inspection of `/sys/fs/cgroup/pids.max` inside the worker.

### What this cycle did NOT close

- **T3 / T5 / T7 / T8 / T9 against medium / large workloads**: all
  blocked by the budget_mb math observation under load. Tiny
  workloads (T1, T2, T4, T6, T10) exercise the same code paths
  with single-variant fan-out and avoid the OOM kill, but the
  multi-variant rows that the test matrix exists to cover stay
  yellow until either the framework's per-worker memory allocation
  divides cleanly across the cgroup or the workload narrows further
  per the user's "test with easier compilations" rule.
- **T7 post-promotion 4-way mesh assertions**: never validated. T7's
  shape requires a multi-variant workload to keep the mesh active
  long enough for the peer-mesh-reachability probe to land.
- **T9 load contention**: never attempted at the new stack; same
  blocker as T3.
