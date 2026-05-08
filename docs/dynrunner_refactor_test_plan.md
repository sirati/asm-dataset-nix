# dynrunner-refactor regression test plan

## Context

The `dynamic_runner` framework owner (`dynrunner-owner` peer on
claude-comm) has signalled an inbound *massive refactor*. asm-dataset-nix
is a primary downstream consumer with a comprehensive SLURM
test/invariant harness already on `main` (per
`docs/slurm_test_plan.md`). When the refactor lands, we are the
canary — we run the harness, find the bugs the framework's own tests
miss, and feed findings back to the peer.

This document is the playbook for that cycle. It is owned by the
asm-dataset-nix session that picks up the work; it is *not* a
to-do list to act on now.

## Trigger

The cycle starts when **either**:

- The `dynrunner-owner` peer sends a claude-comm message tagged with a
  refactor branch / rev / PR — typical body: "ready to test, branch
  `refactor/<X>`" or "rev `<sha>` cherry-picks all the changes, please
  run your harness".
- The `dynrunner-owner-slurm-test-env-owner` peer signals a parallel
  test-env update.

Both peers are on the global claude-comm registry. Watch
`mcp__claude-comm__list_previous_messages` filtered by peer for the
trigger message.

## Pre-flight (before touching anything)

1. Confirm `main` is clean and pushed:
   `git status --short` empty; `git rev-parse origin/main == HEAD`.
2. Confirm the slurm-test-env is up + reachable:
   `ssh -p 2244 -i .ssh-debug/id_ed25519 -o IdentitiesOnly=yes
   sirati@localhost 'sinfo -N'` returns 4 idle workers (or 3 if
   `slurm-worker1` is stuck DOWN+NOT_RESPONDING — bring up clean if so;
   memory `project_slurm_test_env.md` describes bring-up).
3. Capture the pre-bump dynamic-runner pin: `nix flake metadata --json
   | jq '.locks.nodes."dynamic-runner".locked.rev'`. Record in the
   findings doc.
4. Snapshot a baseline T1 wall-time for comparison post-bump:
   `nix develop -c pytest python/compiler_suit_runner/tests/slurm/
   test_t01_clean_tiny.py -v -m slurm_live` — should pass in ~50s
   when the cluster is warm-cached.

## Bumping the framework pin

```
nix flake update dynamic-runner --override-input dynamic-runner \
    github:sirati/dynamic-runner/<refactor-branch>
nix flake check  # may take several minutes
```

If `flake check` fails, that's the first finding — capture stderr,
relay to `dynrunner-owner` before going further. Do NOT patch around
breakage in this repo unless the peer asks for it.

If `flake check` passes, commit the bump on a side branch:

```
git checkout -b dynrunner-refactor-bump
git -c commit.gpgsign=true commit -S --signoff -m \
  "chore: bump dynamic-runner to <rev> for refactor testing"
```

Run the unit-test slice first (file-only, no live cluster):

```
nix develop -c pytest python/compiler_suit_runner/tests/ -x
```

Any unit-test failure here is a wire-shape mismatch — record + relay.

## Live-test matrix run order

Run sequentially (avoid cluster contention). Per-test caps already
encoded in each test file via `T<id>_TIMEOUT_S` env vars; cap is also
the parent's wall-clock budget. **Total budget if everything goes
clean**: ~2.5 hours. Schedule accordingly.

| # | Test | Cap | Why this order |
|---|---|---|---|
| 1 | `test_t01_clean_tiny` | 600s | Smoke. If T1 fails, every later test is suspect — stop and triage. |
| 2 | `test_t10_port_collision` | 300s | Fast; isolates the smoke16-class leak from anything else. |
| 3 | `test_t04_n2_clean` | 600s | Cheapest multi-secondary path; promotes the local primary disconnect that T6 also exercises. |
| 4 | `test_t06_submitter_disconnect` | 600s | Promotion-election variant (kills local primary driver, checks failover). |
| 5 | `test_t05_kill_secondary` | 1200s | SIGKILL'd-secondary cleanup; specialised invariants. |
| 6 | `test_t02_post_promotion_hang` | 600s | The open framework-bug reproducer. Captures py-spy stack on hang. **Most likely to hit a real bug.** |
| 7 | `test_t03_clean_medium` | 1200s | T3 hung pre-refactor in the primary↔secondary connect-retry loop. If T3 still hangs post-refactor, that's the same framework bug — capture it. |
| 8 | `test_t08_worker_oom` | 1500s | OOM-via-cgroup; tests SLURM proctrack cleanup. |
| 9 | `test_t07_n4_clean` | 1500s | Full N=4 mesh + per-URL reachability probe. |
| 10 | `test_t09_n4_load` | 1800s | Largest workload; surfaces contention-triggered races. |

**Run command** (per test):

```
nix develop -c pytest python/compiler_suit_runner/tests/slurm/test_t<NN>_*.py \
    -v -m slurm_live --tb=short
```

Between tests: verify squeue empty + cluster_probe.cleanup() ran (the
fixture handles this; verify by `ssh ... squeue --me` showing no
rows).

## Findings format

For each test, capture:

- **Outcome**: pass / fail / hang / skip-for-cluster-down.
- **Wall time** vs. the pre-bump baseline (T1 first).
- **Failed invariants** (verbatim from `_format_results` in the test).
- **Slurm log dir**: `/home/sirati/.local/state/slurm-test-env/ds-test/
  home/sirati/slurm/log/run_<TS>/` — preserve unless space pressures.
- **Dev-box harmonia.log tail**: `tail -50
  ~/.cache/asm-dataset-nix-runner/harmonia.log` — substitution-related
  context.
- **py-spy artifact** (T2 only): `python/compiler_suit_runner/tests/
  slurm/artifacts/t02_<TS>_pyspy.txt`.

Aggregate into a single report file: `docs/dynrunner_refactor_findings_<rev>.md`.
Use the same dev-box, peer mesh, substituter URL, etc. context the
existing `docs/slurm_test_plan.md` already documents.

## Communicating with dynrunner-owner

- **One mid-cycle ack**: after T1 passes (or fails), send a short
  status: "T1 passed/failed at rev <X>, continuing with T2..T10". Keeps
  the peer informed without spamming.
- **Per-failure ping**: when a live test fails, send a short
  reproducer summary — invariant text, slurm log path, py-spy if
  any. Don't dump full logs over the channel; reference paths.
- **End-of-run summary**: aggregate findings, summary stats (X/10 live
  tests passed, Y bugs surfaced), and the findings doc path.

## Environment hygiene

- The dev-box harmonia is started/stopped per-dispatch by SubmitterPeer
  (commit 17e35be made the lifecycle robust). Verify no orphan
  harmonia between tests: `pgrep -af harmonia-cache` should return
  empty between test_t<N> and test_t<N+1>. If not, the orphan-fix
  regressed — flag immediately.
- The slurm-test-env's `slurm_<N>.err` files can grow without bound if
  the framework's command-relay subshell starts spinning (peer-flagged
  bug; tokenizer hit it on 2026-05-08). Watch host disk usage between
  tests: `df -h /home/sirati/.local | tail -1`. If a file >1 GB
  appears, kill the relay (`podman exec slurm-workerN-ds-test pkill -f
  slurm_script`) and `podman unshare rm` the file.
- If `slurm-worker1` is in DOWN+NOT_RESPONDING after a hang, bring
  the cluster down + up cleanly:
  `INSTANCE_ID=ds-test SSH_PORT=2244 nix run \
  /home/sirati/devel/python/dynamic_runner/slurm-test-env#down`,
  then `#up`, then `#provision-user -- sirati .ssh-debug/id_ed25519.pub`.
  The `--purge` flag on bring-up wipes the simulated /home — usually
  unwanted (loses the previous run-log dirs); skip unless explicitly
  asked.

## Stop conditions

Halt the cycle (and ping `dynrunner-owner`) if any of:

- T1 hangs or fails on a non-test-bug invariant.
- A worker container starts leaking disk via the relay-spin bug
  (recurrence of the 2026-05-08 issue; needs framework fix before
  continuing).
- The dev-box harmonia orphan-fix regresses (post-test `pgrep -af
  harmonia-cache` returns rows).
- More than 3 of the 10 live tests fail in ways that look correlated
  (suggests a single root cause; better to triage that one bug than
  iterate on each test).

## After the cycle

- Push the bump branch + findings doc.
- If the refactor is shipping: update `docs/slurm_test_plan.md`
  inline (timing baselines, any test-shape changes the refactor
  required).
- If the refactor is reverted: `git checkout main && git branch -D
  dynrunner-refactor-bump`, restore the pre-bump pin, and write a
  short post-mortem in the findings doc.
