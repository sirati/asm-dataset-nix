# SLURM test-env: comprehensive test & invariant-checking plan

## Context

We've been hitting a class of bugs in the dynamic_runner SLURM integration that
are hard to reproduce because they leak state between runs. The recent symptoms:

- **Smoke16 leaked containers**: a secondary on `slurm-worker1` (10.89.1.3)
  kept a `harmonia-cache` (PID 67094), peer_push server (port 5050), secondary
  coordinator + 33 build_workers and conmon (PID 66972) alive **after** SLURM
  thought the job was finished. Containers had `setsid()`'d themselves (own
  session ids), so SLURM's `proctrack/linuxproc` couldn't find them via PPID
  walk and `task/none` provides no cgroup safety net.
- **Smoke17/18**: subsequent runs failed with
  `OSError: [Errno 98] Address already in use` on harmonia/peer_push because
  the previous run's processes were still bound to those ports.
- **Smoke19**: clean state path works after manual cleanup → confirms the leak,
  not a new bug.
- **Suspected upstream bug**: post-promotion-with-failure secondary hangs
  without exiting. Worker failure kills the secondary (it should not — task
  failed, secondary should drain and exit). User explicitly flagged: "a worker
  failed. that means the task failed. this should never kill the secondary.
  that the secondary got stuck however is a bug."

We have no systematic way to:
1. Verify each run leaves a clean cluster.
2. Reproduce the post-promotion-with-failure hang reliably.
3. Test multi-node behavior (peer mesh, substituter list, promotion
   election) — only N=1 has been exercised so far.

This plan adds (a) a per-run invariant harness that consumes the locally
mounted `/home/sirati/.local/state/slurm-test-env/...` log tree, (b) a
test matrix covering N=1,2,4 secondaries × workload size × failure injection,
and (c) reproducer recipes for the open framework hang. **All artefacts live
under `python/compiler_suit_runner/tests/slurm/` in this repo (matches the
existing test convention under `python/compiler_suit_runner/tests/`); no
framework edits.**

The slurm-test-env's `/home` is bind-mounted at
`/home/sirati/.local/state/slurm-test-env/ds-test/home/sirati/`, so all log
reads use Read/Grep directly — no SSH needed. SSH (`-p 2244` + key in
`.ssh-debug/id_ed25519`) is only required for `squeue`/`sinfo`/process
inspection on workers and for cluster-state probing.

Confirmed cluster topology (2026-05-08): 4 worker nodes
(`slurm-worker{1..4}`), all idle, partition `debug*`. Multi-node up to N=4 is
viable.

## Critical files & paths

### Existing (read-only references)

- `python/compiler_suit_runner/cli.py:566-580` — `cmd_run` builds the
  framework `args` namespace; `args.jobs` becomes `num_secondaries`.
- `python/compiler_suit_runner/preflight.py:288` — `run_nix_eval` (used
  by preflight; relevant for T7 preflight-timeout repro).
- `python/compiler_suit_runner/peer_push.py:222` — `_ThreadingHTTPServer`
  bind site. T6 verifies peer_push releases the port.
- Framework (vendored, do NOT edit; only inspect):
  - `dynamic_runner/packaging/job_manager.py:117-339` — slurm_script
    template, `trap cleanup EXIT TERM HUP INT` (does not call `podman stop`).
  - `dynamic_runner/packaging/pipeline.py:118` — `num_secondaries =
    args.jobs`.
  - `dynamic_runner/packaging/slurm_config.py` — `SlurmConfig` dataclass.

### New files (this plan creates)

All paths are relative to the repo root. The base directory is
`python/compiler_suit_runner/tests/slurm/` (abbreviated `<base>/` below).
**Per-test files are split** (one file per matrix row) so parallel subagents
own disjoint files and need no merge conflict resolution on `test_matrix.py`.

| Path | Purpose |
|------|---------|
| `<base>/__init__.py` | empty |
| `<base>/invariants.py` | per-run invariant checker (callable from CLI + pytest); checks 1–7 |
| `<base>/cluster_probe.py` | SSH-into-test-env helpers (squeue, sinfo, podman ps, port scan, cleanup) |
| `<base>/run_helpers.py` | wraps `python -m compiler_suit_runner run`; captures run_id, returns log dir, manages cache invalidation |
| `<base>/conftest.py` | shared pytest fixtures (cluster_probe handle, cleanup hook, log-mount path) |
| `<base>/test_t01_clean_tiny.py` | T1: N=1, tiny clean |
| `<base>/test_t02_post_promotion_hang.py` | T2: N=1, broken toolchain — the highest-value reproducer |
| `<base>/test_t03_clean_medium.py` | T3: N=1, medium clean |
| `<base>/test_t04_n2_clean.py` | T4: N=2 clean |
| `<base>/test_t05_kill_secondary.py` | T5: N=2, SIGKILL one secondary |
| `<base>/test_t06_submitter_disconnect.py` | T6: N=2, kill local primary driver |
| `<base>/test_t07_n4_clean.py` | T7: N=4 clean + peer-mesh assertions |
| `<base>/test_t08_worker_oom.py` | T8: N=4, one worker OOM |
| `<base>/test_t09_n4_load.py` | T9: N=4 load smoke |
| `<base>/test_t10_port_collision.py` | T10: N=1, port-bind collision injection |
| `<base>/reproducers/__init__.py` | empty |
| `<base>/reproducers/broken_toolchain.py` | flake-fragment generator + cleanup for T2 |
| `<base>/reproducers/inject_failures.py` | shared injection helpers (SIGKILL, port-grab, OOM-cgroup) |
| `<base>/peer_mesh_assertions.py` | parses `_substituters.secondary-N.txt`, asserts mesh shape |
| `<base>/artifacts/.gitkeep` | placeholder; pytest writes py-spy dumps + diagnostic snapshots here |
| `docs/slurm_test_plan.md` | persisted summary of this plan (so future loops don't lose context) |

**flake.nix addition** (single attribute under a hidden namespace, scoped
to T2 only):

| Path | Purpose |
|------|---------|
| `flake.nix` (or `lib/test_broken.nix` imported there) | exposes `_drvPaths.x86_64-linux.__test_broken__` — a derivation that fails its `buildPhase` deterministically with a known-stable stderr line for T2 to match against |

## Diagnostic infrastructure (use the local mount, not SSH)

`/home/sirati/.local/state/slurm-test-env/ds-test/home/sirati/slurm/log/run_<TS>/`
contains, per run:

| Filename | Source | What to check |
|---|---|---|
| `slurm_<jobid>.out` | sbatch stdout | Rust-side primary log; `secondary finished successfully`, `Container exited with code: 0` |
| `slurm_<jobid>.err` | sbatch stderr | image pull warnings, `Address already in use`, Python exceptions in suit_task |
| `harmonia-secondary-<n>.log` | inside container | binary cache server lifecycle |
| `nix-daemon-secondary-<n>.log` | inside container | nix-daemon errors during build |
| `peers/` | dynrunner | peer mesh join/leave events |
| `connection_info/secondary-<n>.info` | dynrunner | worker IP, QUIC/tunnel ports |
| `connection_info/_substituters.secondary-<n>.txt` | suit_task | binary-cache URL list seeded into nix.conf |
| `build-failures/<task_id>.log` | suit_task | per-failed-item drv-attr + stderr excerpt |
| `manifests/` | suit_task | per-variant emitted manifests |

`<base>/invariants.py` consumes these directly; no SSH on the read path.

## Per-run invariants (hard fails)

The harness asserts ALL of these at end-of-run; any failure flags the run as
broken even if the framework reported success:

1. **Clean exit**: `slurm_*.out` ends with `secondary finished successfully`
   AND `Container exited with code: 0`.
2. **No bind errors**: `slurm_*.err` contains zero matches for
   `Address already in use|EADDRINUSE`.
3. **Manifest count matches**: `len(manifests/) ==
   completed_variants_from_log` (parsed from `task completed ... task_type=variant`).
4. **No build-failure log unless expected**: `build-failures/` empty
   for clean-path tests; expected count for failure-injection tests.
5. **No leaked podman containers**: SSH probe `podman ps -a --root <PODMAN_STORAGE>`
   on each worker that ran a secondary returns no rows tagged with this run_id.
6. **No leaked listener ports**: SSH probe `ss -lntp` shows no harmonia /
   peer_push / nix-daemon listener bound by this run's UID after job exit.
7. **No leaked PPID=1 processes**: SSH probe — no orphaned bash/conmon
   /compiler_suit_runner with start-time within this run's window.

Invariants 5–7 are the ones that catch the smoke16-class leak. They run only
after `squeue --me` is empty (tested with a 60s poll), to avoid false-positive
on still-running jobs.

## Test matrix

Each row is a separate test; each test is one full run + invariant check.
"workload" is selected via the existing `--limit` / `--filter` selection args.

| ID | Secondaries (`--jobs`) | Workload | Failure injection | Expected outcome |
|----|------------------------|----------|-------------------|------------------|
| T1  | 1 | tiny (1 toolchain × 1 variant) | none | clean, completed=1 |
| T2  | 1 | tiny | toolchain fail (deliberately broken attr) | secondary drains, exits non-zero, **NO HANG** |
| T3  | 1 | medium (~10 variants) | none | clean, all manifests present |
| T4  | 2 | tiny | none | both secondaries clean; peer mesh formed |
| T5  | 2 | medium | SIGKILL secondary-1 mid-build | secondary-0 promotes, completes work, exits clean |
| T6  | 2 | tiny | submitter disconnect (kill local primary driver) | secondaries detect, one promotes, completes, exits |
| T7  | 4 | medium | none | 4-way peer mesh, all substituter lists list 3 peers, all complete |
| T8  | 4 | medium | one worker OOM-kill (cgroup limit on slurm-worker3) | other 3 finish; SLURM marks worker3 failed |
| T9  | 4 | large (~50 variants) | none | smoke for cache hit/miss patterns under contention |
| T10 | 1 | tiny | inject `Address already in use` (pre-bind port 5050) | secondary fails fast with clear error, run_id marked failed, NO leak |

T1, T3, T4, T7 are **clean-path tests**. T2, T5, T6, T8, T10 are **failure
injection** tests. T9 is a **load smoke**.

T2 is the open-question reproducer (post-promotion-with-failure hang). The
invariant harness sets a hard 600s timeout; if the secondary doesn't exit by
then, the harness attaches `py-spy dump --pid <secondary_pid>` and saves the
stack to `<base>/artifacts/t02_<TS>_pyspy.txt` for offline analysis.
This is the only path expected to capture the hang's stack.

T7 covers multi-node specifics:
- **peer-mesh formation**: `_substituters.secondary-<n>.txt` lists exactly N-1
  peer URLs.
- **substituter list correctness**: each peer URL resolvable from inside its
  worker's container.
- **promotion election**: when the local primary disconnects (it does, normally,
  after dispatch), exactly one secondary is logged as
  `promoted to primary epoch=1`.

## Failure-injection mechanics

| Injection | How |
|---|---|
| toolchain fail (T2) | add a `_drvPaths.x86_64-linux.<broken-attr>` whose underlying derivation has `unset $out` so `nix build` fails fast with deterministic stderr |
| SIGKILL secondary (T5) | watch for `task completed ... task_type=variant` in target secondary's `slurm_*.out`, then `scancel --signal=KILL <jobid>` (scoped to job-name pattern, NOT --user) |
| submitter disconnect (T6) | from the harness, `kill -INT $primary_driver_pid` after primary URL appears in connection_info |
| worker OOM (T8) | pre-create a cgroup with `memory.max=512M` on slurm-worker3, run secondary inside it (sbatch `--mem=512M` is sufficient — slurm sets the cgroup) |
| port-bind collision (T10) | start a placeholder netcat listener on slurm-worker1:5050 *before* dispatch, verify secondary fails with the expected error class, then close listener |

## Cleanup harness (mandatory between tests)

Every test calls `cluster_probe.cleanup()` at start AND end:

```python
# pseudocode
def cleanup():
    # SSH-scoped to test-env's kruppb account; filter by job-name pattern only
    ssh("scancel --jobname='asm-secondary-*' --user=$(whoami)")
    poll_until_empty("squeue --me", timeout=60)
    for worker in ["slurm-worker1", ..., "slurm-worker4"]:
        ssh_to_worker(worker, """
            podman --root /run/user/$(id -u)/storage \\
                   --runroot /run/user/$(id -u)/runtime \\
                   ps -aq | xargs -r podman stop --time=5
            podman ps -aq | xargs -r podman rm -f
            pkill -KILL -f 'compiler_suit_runner|harmonia-cache|peer_push' || true
        """)
    poll_until_empty("ss -lntp | grep -E ':5000|:5050'", timeout=30)
```

CRITICAL (per memory `feedback_scancel_scope.md`): scancel must be filtered by
job-name pattern, NEVER `--user=kruppb` — the kruppb account is shared with
the asm-tokenizer peer in the test env.

## Reproducer for post-promotion-with-failure hang (T2 deep-dive)

Goal: capture a Python stack of the hung secondary so we can either
patch our suit_task or open a precise upstream issue against
`dynrunner_manager_distributed::primary::lifecycle`. Implementation
breakdown lives in the C3.1 sub-sub-task plays below.

`<base>/test_t02_post_promotion_hang.py` orchestrates:
1. Generate a flake fragment with one variant whose toolchain is deliberately
   broken (the `__test_broken__` attr — see T2-α below).
2. Run `--jobs 1 --limit 1` against that broken attr.
3. Once secondary log shows `promoted to primary epoch=1` AND
   `local primary disconnected`, start a 600s timer.
4. If secondary process still alive at T+600s:
   - SSH to the worker, run `podman exec <cid> py-spy dump --pid 1`
     (the in-container PID-1 is the secondary's Python entry).
   - Save stack to `<base>/artifacts/`.
   - Also `podman exec <cid> ps auxf` to capture worker state.
5. Cleanup (kill container, rm).

This is the only diagnostic that catches the actual code path; without it
we'd be guessing at where the hang lives.

## Verification

End-to-end smoke (after implementing the harness) to be re-run on every
plan-related change. `<base>` = `python/compiler_suit_runner/tests/slurm/`.

```
# from repo root
nix develop -c pytest python/compiler_suit_runner/tests/slurm/test_t01_clean_tiny.py -v
nix develop -c pytest python/compiler_suit_runner/tests/slurm/test_t02_post_promotion_hang.py -v
nix develop -c pytest python/compiler_suit_runner/tests/slurm/test_t07_n4_clean.py -v
nix develop -c pytest python/compiler_suit_runner/tests/slurm/ -v   # full matrix
```

Pre-flight checks before each test (parent + every subagent runs these
via the conftest.py fixture):
- `ssh -p 2244 -i .ssh-debug/id_ed25519 -o IdentitiesOnly=yes sirati@localhost squeue --me` → empty.
- `ssh -p 2244 -i .ssh-debug/id_ed25519 -o IdentitiesOnly=yes sirati@localhost sinfo -N` →
  4 nodes idle.

Post-flight:
- `python -m compiler_suit_runner.tests.slurm.invariants /path/to/run_<TS>/` exits 0.

## Execution model: parallel subagents in isolated worktrees

Tasks split into **batches**. Within a batch, multiple subagents run **in
parallel**, each in its own `git worktree` on a disjoint branch. Between
batches, the parent (this session) reviews each subagent's diff and merges
sequentially into the local feature branch (and eventually into master).

**Subagent model**: `opus` (Opus 4.7 with 1M-context variant where
available). Pass `model: "opus"` to the Agent tool; mention "1M context"
in the prompt header so the runtime can route accordingly.

**Worktree convention**:
- Branch name: `subtask/<batch>-<short-name>` (e.g. `subtask/A-invariants`).
- Worktree path: managed by `Agent` with `isolation: "worktree"` — the
  tool auto-cleans if the subagent makes no changes; otherwise returns
  branch + path for parent merging.
- Each subagent is told: **commit signed** (`-S --signoff`), **no
  co-authored-by**, **no task-internal IDs in commit messages** (no "T2",
  "B1.2" etc. — describe the change, not the plan slot).
- Each subagent is encouraged to **further split** their subtask into
  self-contained sub-sub-tasks and spawn their own subagents (also in
  worktrees, with their own `--no-ff` merge cadence). Recursion limit:
  one extra level (don't go three deep).

**claude-comm channel** (`/home/sirati/devel/sh/claude-comm/README.md`):
- Open a channel **only** for high-coordination subagents (T2 repro and T7
  multi-node) where iteration may be needed. The parent listens on role
  `a`, the subagent connects on role `b`. Channel ID:
  `asm-slurm-<batch>-<short-name>` (e.g. `asm-slurm-C3.1-t02-hang`).
- For trivial subtasks (T10 port-bind, simple file-only invariants),
  skip the channel — the cost outweighs the value.
- Per memory `feedback_claude_comm_role.md`: parent verifies its role
  via `claude-comm status` + role-{a,b}.pid PID check before first send.

## Per-subtask merge / validate / push cadence

Applied **per-subagent return** (not per-batch). When parallel subagents in
the same batch finish at different times, parent processes them one at a
time in completion order; the next subagent's branch is naturally rebased
against the now-advanced master via step 3.

1. **Parent reviews diff**: `git diff main...subtask/<branch>` for the
   returning subagent's branch.
2. **Parent runs validation** in the subagent's worktree:
   - `nix develop -c ruff check python/`
   - `nix develop -c pytest python/compiler_suit_runner/tests/ -x`
     (skipped for purely non-Python changes)
   - `nix develop -c pytest python/compiler_suit_runner/tests/slurm/ -x
     -k '<the new test ids>'` — only the newly-added tests, against the
     real cluster.
   - `nix flake check` only if `flake.nix` touched.
3. **Merge master into subtask branch first** (in the worktree):
   `git merge master -S --signoff`.
   - If conflicts: parent resolves directly for trivial cases (per-test
     file split makes most conflicts trivial); for non-trivial conflicts,
     re-spawn subagent with merge context via the comm channel (or a
     fresh Agent call if no channel was open).
   - If merge brought changes in: re-run step 2.
4. **Merge subtask branch into local master**: `git merge --no-ff
   -S --signoff subtask/<branch>` so each batch boundary stays visible
   in `git log --graph`.
5. **Re-validate post-merge** on master: same commands as step 2 plus
   the FULL slurm test slice that's been merged so far (`pytest
   python/compiler_suit_runner/tests/slurm/`). Catches inter-test
   interference from cleanup/cache state.
6. **Push master to origin/master**: only if master strictly advanced
   AND step 5 passed. `git push origin master` (signed via ssh
   agent; never `--force`, never `--no-verify`). If push is rejected
   (origin advanced), `git pull --rebase --no-gpg-sign=false` first,
   re-validate, then push.
7. **Cleanup worktree**: `git worktree remove <path>` and
   `git branch -d subtask/<branch>` (use `-D` only after confirming the
   branch was merged via step 4 — `-d` will refuse otherwise, which is
   the desired safety check).

If validation fails at ANY step, parent does NOT merge or push; instead
parent surfaces the failure and either fixes it directly (trivial) or
re-spawns the subagent with the failing-validation output (non-trivial).

**Master must never carry uncommitted code.** If parent ever finds
uncommitted-but-complete code on master at the start of a cycle, parent
makes a checkpoint commit (signed, signoff, descriptive message — no
plan-slot IDs) before doing anything else.

**Commit message rules** (apply to parent commits AND subagent commits):
- No `Co-Authored-By:` trailer.
- No internal task IDs (no "T2", "B1.2", "C3.1" etc.).
- Describe the change in user-facing terms.

## Batch decomposition (parallel where possible)

**Batch 0 — bookkeeping (parent only, serial)**

Master currently has a large dirty tree (per `git status`):
- Modified: `develop.nix`, `flake.lock`, `flake.nix`, `lib/flags.nix`,
  `nix/docker-image.nix`, multiple `python/compiler_suit_runner/*.py`
  files (cli, manifest_gen, peer_cache, preflight, suit_task,
  workers/*), `pyproject.toml`, several test files.
- Added (untracked): `nix/ssh-debug-image.nix`,
  `python/compiler_suit_runner/ssh_debug.py`,
  `python/compiler_suit_runner/tests/test_runner_protocol.py`,
  `python/compiler_suit_runner/workers/_runner_protocol.py`, the entire
  `python/ssh_debug_runner/` package, and `.ssh-debug/id_ed25519.pub`.

Parent walks this tree **chunk by chunk** in the following groups (each
group becomes one signed commit, or stays uncommitted with a flag for
the user):

| Chunk | Files | Validation |
|---|---|---|
| 0a runner protocol module | `python/compiler_suit_runner/workers/_runner_protocol.py` + `tests/test_runner_protocol.py` + worker imports | `pytest tests/test_runner_protocol.py -x` |
| 0b ssh-debug runner | `python/ssh_debug_runner/**`, `python/compiler_suit_runner/ssh_debug.py`, `nix/ssh-debug-image.nix`, `.gitignore` adjustment, `.ssh-debug/id_ed25519.pub` | `pytest python/ssh_debug_runner/tests/` |
| 0c preflight + manifest_gen + peer_cache changes | the matching `.py` files + their test changes | `pytest tests/test_{preflight,manifest_gen,peer_cache}.py -x` |
| 0d suit_task / workers / cli plumbing | residual modifications to suit_task.py, build_worker.py, merge_worker.py, partition_worker.py, cli.py — only the parts that compose with 0a–0c | full `pytest python/compiler_suit_runner/tests/` |
| 0e flake / nix / dev-shell + pyproject | `flake.nix`, `flake.lock`, `develop.nix`, `pyproject.toml`, `nix/docker-image.nix`, `lib/flags.nix` | `nix flake check` |

For each chunk:
- Parent runs the chunk-scoped validation.
- If green: commit (`git commit -S --signoff -m "<concise description>"`)
  and continue.
- If red OR the chunk looks half-done (TODOs, debug prints, removed
  imports without replacement): parent **stops Batch 0** and surfaces
  the finding to the user — do NOT half-commit or "fix forward"; the
  user may have intentional WIP here.
- After all chunks committed: push to origin/master.

This is the only place the plan can stall waiting on the user. Every
later batch is autonomous.

**Batch A — skeleton harness (3-way parallel)**

`<base>/` = `python/compiler_suit_runner/tests/slurm/` throughout this section.

| Subtask | Files owned | claude-comm? |
|---|---|---|
| A1 invariant file-parsers | `<base>/__init__.py`, `<base>/invariants.py` (checks 1–4 only, file-only, NO SSH) | no |
| A2 cluster probes | `<base>/cluster_probe.py` (SSH wrappers: squeue, sinfo, ssh_to_worker, podman_ps, port_scan) | no |
| A3 run helpers | `<base>/run_helpers.py` + `<base>/conftest.py` (subprocess wrapper around `python -m compiler_suit_runner run`, captures run_id, returns log dir path; conftest exposes shared fixtures) | no |

All three are file-disjoint → can run fully parallel. Parent merges
A1, A2, A3 in order of completion.

**Batch A-followup (serial, 1 subagent)**
- A4: extend `invariants.py` with checks 5–7 (uses A2's cluster_probe);
  blocked by A1+A2.

**Batch B — test matrix scaffold + cleanup helper (2-way parallel,
blocked by A4)**
| Subtask | Files owned | claude-comm? |
|---|---|---|
| B1 T1 wiring | `<base>/test_t01_clean_tiny.py` (uses A3 run_helpers + A4 invariants) | no |
| B2 cleanup harness | extend `<base>/cluster_probe.py` with `cleanup()` (scancel by job-name pattern, podman cleanup, port-poll) | no |

After Batch B merges, parent verifies T1 passes against a real run
(`pytest <base>/test_t01_clean_tiny.py -v`). T1 must be green before
proceeding to Batch C.

**Batch C1 — clean-path tests (2-way parallel)**
| Subtask | Files owned | claude-comm? |
|---|---|---|
| C1.1 T3 medium clean | `<base>/test_t03_clean_medium.py` | no |
| C1.2 T4 N=2 clean | `<base>/test_t04_n2_clean.py` (basic peer-mesh sanity; full N=4 mesh assertions deferred to T7) | no |

Both files are disjoint; no conflict. The per-test split (one file per
matrix row, decided in "New files" above) is exactly the mechanism that
removes the parallel-edit hazard.

**Batch C2 — multi-node tests (2-way parallel, blocked by C1)**
| Subtask | Files owned | claude-comm? |
|---|---|---|
| C2.1 T7 N=4 multi-node | `<base>/test_t07_n4_clean.py` + new `<base>/peer_mesh_assertions.py` | **yes** (`asm-slurm-multinode`) |
| C2.2 T9 N=4 load smoke | `<base>/test_t09_n4_load.py` | no |

T7 gets a comm channel because peer-mesh assertions may need iteration
on the actual format of `_substituters.secondary-N.txt` (we've never
verified it directly with N=4).

**Batch C3 — failure injection (5-way parallel, blocked by C1; T2 also
unblocked after Batch B since it uses no multi-node primitives)**
| Subtask | Files owned | claude-comm? |
|---|---|---|
| C3.1 T2 post-promotion-hang repro | `<base>/test_t02_post_promotion_hang.py` + `<base>/reproducers/broken_toolchain.py` + `flake.nix` (or `lib/test_broken.nix`) for `_drvPaths.x86_64-linux.__test_broken__` | **yes** (`asm-slurm-t02-hang`) |
| C3.2 T5 SIGKILL secondary | `<base>/test_t05_kill_secondary.py` + new helpers in `<base>/reproducers/inject_failures.py` | no |
| C3.3 T6 submitter disconnect | `<base>/test_t06_submitter_disconnect.py` + helpers in `inject_failures.py` | no |
| C3.4 T8 worker OOM | `<base>/test_t08_worker_oom.py` + helpers in `inject_failures.py` | no |
| C3.5 T10 port-bind collision | `<base>/test_t10_port_collision.py` + helpers in `inject_failures.py` | no |

`inject_failures.py` is shared across C3.2..C3.5; subagents are told to
add their helper as a self-contained function and **not** edit the
others' helpers. Parent merges in completion order; first subagent
creates the file, others append. If conflict on `inject_failures.py`,
parent resolves trivially (each helper is a separate function).

T2 gets a comm channel — it's the highest-value test and the framework
bug it targets is open. Subagent likely needs back-and-forth on what
py-spy actually captures.

**Sub-sub-task plays for the complex Batch C tasks**

The decomposition below is part of THIS plan (per the directive: complex
plans must include sub-sub-task plays). Subagents may follow these as
written, or refine them — but they should not invent the sub-sub-task
boundaries from scratch.

### C2.1 T7 (N=4 multi-node clean) — 3 sub-sub-tasks

T7-α — `peer_mesh_assertions.py` module
- Parse `_substituters.secondary-N.txt` for each N in 0..3.
- Assert each file lists exactly N-1 unique peer URLs (3 entries for
  N=4).
- Assert URL format: `http://<host>:<port>/` where host matches the
  worker's connection_info IP and port matches the harmonia config
  (5000 in current image).
- Return a structured `MeshShape` dataclass for the test to assert on.
- Self-contained: no SSH; consumes only file artifacts.

T7-β — substituter URL reachability probe
- For each peer URL extracted by T7-α, SSH into the corresponding
  worker (via gateway ProxyJump) and `curl --max-time 5
  <peer-url>nix-cache-info`. Assert HTTP 200 and `Priority` header
  present.
- This requires the `cluster_probe` SSH wrapper from Batch A2.
- Returns a list of probe results for the test to assert on.

T7-γ — `test_t07_n4_clean.py` wiring
- Composes T7-α + T7-β with the standard run-and-validate flow:
  start cluster cleanup → run with `--jobs 4 --filter
  '<medium-workload-pattern>'` → wait for completion → run all 7
  invariants → run T7-α + T7-β assertions → cleanup.

T7 subagent gets a claude-comm channel
(`asm-slurm-multinode`); use it to ask parent for the actual
substituter URL format if it doesn't match the expected pattern.

### C3.1 T2 (post-promotion-with-failure hang reproducer) — 4 sub-sub-tasks

T2-α — broken toolchain flake fragment
- Add `_drvPaths.x86_64-linux.__test_broken__` to `flake.nix` (or a
  helper nix file imported there).
- Implementation: a derivation whose `buildPhase` is `echo TEST_BROKEN
  >&2; exit 1`. No dependencies; fails in <1s.
- Assert: `nix build '.#_drvPaths.x86_64-linux.__test_broken__'` fails
  with stderr containing `TEST_BROKEN`.

T2-β — `broken_toolchain.py` reproducer module
- Function `make_broken_run_args(base_args)` that returns a CLI args
  list pointing at the broken attr, scoped to a single variant so the
  run finishes (or hangs) fast.
- Function `expected_failure_signature()` returns the regex/string the
  build-failure log should match.

T2-γ — py-spy attach harness
- After kicking off the run, monitor the secondary's process state via
  the local mount + cluster_probe.
- On detected hang (timer expires AND `secondary finished` not in
  log): SSH into the worker, run `podman exec <cid> py-spy dump --pid
  1`, save to `<base>/artifacts/t02_<TS>_pyspy.txt`.
- Also `podman exec <cid> ps auxf` saved to
  `<base>/artifacts/t02_<TS>_ps.txt`.
- If py-spy unavailable in image: surface this as a follow-up; for
  now have the harness check and skip with a clear message (image-
  baking is out of scope here).

T2-δ — `test_t02_post_promotion_hang.py`
- Compose α + β + γ with a 600s hard timeout.
- Pass criteria: secondary exits non-zero (it failed) AND no hang
  AND the build-failure log matches T2-β's signature.
- Fail criteria: secondary still alive at T+600s OR exits with 0
  (false-positive masking the bug).

T2 subagent gets a claude-comm channel
(`asm-slurm-t02-hang`); use it for back-and-forth on what py-spy
captures (likely the highest-iteration test).

### C3.2 T5 (SIGKILL secondary) — 2 sub-sub-tasks

T5-α — kill-watchdog helper in `inject_failures.py`
- Watches a target secondary's `slurm_<jobid>.out` for the first
  `task completed ... task_type=variant`. On match: `scancel
  --signal=KILL --jobname='asm-secondary-*' <jobid>` (filtered
  by job-name AND jobid; never `--user=kruppb` — kruppb is shared
  per memory).

T5-β — `test_t05_kill_secondary.py`
- N=2 medium workload; spawn watchdog targeting secondary-1; assert
  secondary-0 promotes, completes the work, exits clean; assert no
  leak after cleanup.

### C3.3 T6 (submitter disconnect) — 2 sub-sub-tasks

T6-α — primary-driver-killer helper in `inject_failures.py`
- Polls the run for the local primary driver PID (parent process of
  `dynamic_runner.run_primary`) and `kill -INT` it after the first
  `received initial assignment` log line on a secondary.

T6-β — `test_t06_submitter_disconnect.py`
- N=2 tiny; trigger T6-α; assert exactly one secondary logs
  `promoted to primary epoch=N`; assert work completes; assert
  no leak.

### C3.4 T8 (worker OOM) — 3 sub-sub-tasks

T8-α — cgroup pre-create helper
- Pre-creates a `memory.max=512M` cgroup on slurm-worker3 (via SSH
  to that worker) named `asm-test-oom-<run_id>`.
- Tested: cgroup creation under rootless, slurm cgroup interaction.
- If sbatch's `--mem=512M` is sufficient (slurm sets the cgroup
  itself), drop α and use sbatch flag instead.

T8-β — sbatch flag plumbing OR direct cgroup-from-secondary helper
- Whichever path α resolves to.

T8-γ — `test_t08_worker_oom.py`
- N=4 medium with worker-3 constrained; assert the other 3 finish
  cleanly; assert worker-3's secondary fails (OOM-killed); assert no
  leak.

### C3.5 T10 (port-bind collision) — 2 sub-sub-tasks

T10-α — port-grabber helper
- Pre-binds netcat-listener on slurm-worker1:5050 BEFORE the secondary
  starts; cluster_probe.cleanup() releases it.

T10-β — `test_t10_port_collision.py`
- N=1; trigger α; assert run fails fast; assert NO leaked
  processes (this is the KEY assertion — the bug it tests is that the
  failed-bind path didn't clean up the half-started container).

Each Batch C3 subagent (other than C3.1) may spawn its own subagent for
its α and β/γ sub-sub-tasks IF it judges them worth parallelizing
(typically not — they're small). The subagent's discretion.

**Batch D — documentation (parallel with C, since file-disjoint)**
- D1: `docs/slurm_test_plan.md` — copy the plan file (this file) so it
  lives in the repo. Trivial; parent can do inline OR spawn a tiny
  subagent. No claude-comm channel.

## Subagent prompt template

When spawning each subagent, include:

```
Model: opus 4.7 (1M context preferred)
Worktree: isolation: "worktree" — your work lives on branch
    subtask/<batch>-<short-name>; commit signed (-S --signoff).
Commit messages: NO Co-Authored-By trailer; NO task-internal IDs
    (no "T2", "C3.1" etc.); describe the change in user-facing terms.

Goal: <specific subtask description>.

Inputs you have:
- This repo at the worktree path.
- The plan file at /home/sirati/.claude/plans/i-have-entered-plan-logical-kazoo.md.
- Plan section "<which section>" describes your subtask.
- Existing reference files: <list with paths>.

Self-validation before returning:
- ruff: `nix develop -c ruff check python/compiler_suit_runner`
- pytest your new tests (skip if SLURM env required and unavailable;
    return that case as a clear "needs live cluster" status).
- For SLURM-driven tests: the live cluster *is* available at
    ssh -p 2244 sirati@localhost (key in .ssh-debug/id_ed25519, IdentitiesOnly=yes).

Encouraged: split your subtask into self-contained sub-sub-tasks and
spawn your own subagents (one extra recursion level allowed). When you
do, give them their own worktrees too.

[If a claude-comm channel is open:]
Communication channel: claude-comm channel ID
    "asm-slurm-<batch>-<name>". Listen + send via
    /home/sirati/devel/sh/claude-comm/claude-comm. I will be on
    role <a|b>; you take role <b|a>. Use it for clarifying questions
    or to surface unexpected findings. Don't run `claude-comm listen`
    on the main channel; that's the parent's job.

Return: a short summary of what you changed, what you validated, and
any open follow-ups. Don't summarize the plan — I have it.
```

## Concrete order of operations (parent's playbook)

1. **TaskCreate everything** with `addBlockedBy` to encode batch
   dependencies. Sketch of the dependency graph:
   - Batch 0 chunks 0a..0e — chained sequentially (each blocks the next).
   - A1, A2, A3 — no blockers (after Batch 0 finishes).
   - A4 — blocked by A1 + A2.
   - B1, B2 — blocked by A4.
   - T1 verification (live cluster) — blocked by B1 + B2.
   - C1.1, C1.2 — blocked by T1 verification.
   - C3.1 (T2 repro) — blocked by B1 + B2 (does NOT need C1).
   - D1 (docs/slurm_test_plan.md) — no blockers (can run any time).
   - C2.1, C2.2 — blocked by C1.1 + C1.2.
   - C3.2, C3.3, C3.4, C3.5 — blocked by C1.1 + C1.2.
   - Final push — blocked by everything.
2. **Yield** with the exact phrase "ready when you are". DO NOT start
   work. The user may send additional clarifying messages before
   compaction; treat each as further plan input and update the plan
   file accordingly. Compaction is detected by the
   "session is being continued from a previous conversation" reminder
   at the top of the next user turn.
3. **Post-compaction**: re-read this plan file + TaskList; resume from
   the first pending unblocked task. Don't recap; just begin.
4. **Batch 0 bookkeeping** (sequential per chunk 0a → 0e). On any
   half-done finding, stop and ask the user. After Batch 0 fully
   committed, push master to origin/master once.
5. **Spawn Batch A** (3 parallel `Agent` calls in ONE message:
   A1, A2, A3 — `model: "opus"`, `isolation: "worktree"`).
6. As each subagent returns: per-subtask cadence (review → validate →
   merge master in → re-validate → merge into master → push).
7. **Spawn A4** (single subagent) after both A1 and A2 are merged.
8. **Spawn Batch B** (2 parallel) after A4 merged.
9. **Verify T1** against real cluster (`pytest <base>/test_t01*.py
   -v`). If T1 doesn't pass, fix forward in this session — don't
   spawn Batch C until T1 is green.
10. **Spawn the wide batch**: C1.1, C1.2, C3.1, D1 in one message
    (4-way parallel). C3.1 gets a claude-comm channel pre-armed by
    parent before the spawn (see "claude-comm setup" below).
11. After all four returned, processed, and merged: spawn the next
    wide batch — C2.1, C2.2, C3.2, C3.3, C3.4, C3.5 in one message
    (6-way parallel). C2.1 gets a claude-comm channel pre-armed.
12. After all merged: full `pytest python/compiler_suit_runner/tests/
    -v`; push master to origin/master.

### claude-comm setup (just before spawning subagents that need it)

For each channel-using subagent, the parent does these steps BEFORE
the `Agent` call so the channel is live when the subagent starts:

1. Pick a channel ID like `asm-slurm-t02-hang` (must match what's
   in the subagent's prompt).
2. Bash `run_in_background: true` running
   `/home/sirati/devel/sh/claude-comm/claude-comm listen --id <id>`.
3. Monitor the same listener (or its bg-task output file).
4. Verify own role via the JSONL `role-assigned` event AND
   `claude-comm status --id <id>` PID inspection (per memory
   `feedback_claude_comm_role.md`: on inherited channels parent
   may be role 'b').
5. Send a hello-context message containing the subagent's ID,
   subtask description, and worktree branch.
6. Spawn the subagent with the channel ID + parent's role embedded
   in the prompt.

After the subagent returns: send `goodbye` (or terminate the listener
with SIGTERM — claude-comm cleans up its FIFOs automatically).

### Yield protocol clarification

The phrase "ready when you are" at the end of the post-acceptance
TaskCreate turn marks the yield. After yield:
- If the user sends a non-compaction message: treat it as plan-
  refinement input; update plan + TaskList accordingly; do NOT
  begin execution.
- The next "session is being continued" system reminder (with the
  summary header) is the compaction signal. The first action after
  compaction: re-read the plan and TaskList, then begin Batch 0.

## What this plan does NOT do

- Edit framework code (`dynamic_runner/packaging/*`). The watchdog daemon
  proposal stays as a separate notes document; the test harness must work
  WITH the framework as-is so it can detect the leak class even when we
  don't own the fix.
- Add upstream PRs. T2's stack capture is the input we need before
  proposing one.
- Touch `flake.nix` toolchain attrs in a way that breaks the existing
  matrix; T2's broken attr lives under a hidden namespace
  (`_drvPaths.x86_64-linux.__test_broken__`).
- Deal with `IncrementalCache` (~/.cache/compiler_suit_runner/). Pre-existing
  bugs there are tracked separately; this plan keeps the tests
  cache-cold by clearing `~/.cache/compiler_suit_runner/` between matrix
  rows.
- Force-push, skip hooks, or skip signing. All commits + merges are
  `-S --signoff`; ssh-agent has the signing key for the duration of
  this session.
