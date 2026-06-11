# LMU CIP operations notes

Companion to `SLURM_RUNBOOK.md`. The runbook explains **how to dispatch**; this document captures the **firm LMU-specific operational policy, shared-cap constraint, and the running pin-lineage observation log** that the runbook intentionally keeps out of its flag table.

Our dataset-generation operational root on LMU is `/home/k/kruppb/BIG/slurm/gen-binary-dataset/` (`~/BIG/slurm/gen-binary-dataset/`) — that is the `--slurm-root-folder`, under which `out/`, `log/`, and `image_bin/` live.

If you are a fresh subagent or a future-me reading this after some weeks: read `SLURM_RUNBOOK.md` first for the canonical recipe, then read this file for the LMU-specific firm overrides and operational constraints.

## Firm LMU CLI overrides

| Flag | LMU value | Why firm |
|---|---|---|
| `--slurm-partition Krater` | `Krater` (40-node partition) | Framework default is `All` (131 nodes, union of all rock-named nodes). `All` works — sbatch accepts it, jobs run — but is the wrong placement for our dispatches. User flagged this 2026-05-15 after a Tier-3 run landed on `All`. The 40-node `Krater` partition is comfortably more than our 15-job cap, so concurrency is not constrained. |
| `--jobs 15` | `15` (kruppb cap) | kruppb has a 15-parallel-task quota across all kruppb SLURM jobs at LMU. Pre-flight orphan-scan is mandatory or the next run won't get full quota. |
| `--slurm-time-limit 1440` | `1440` minutes = **24h** (firm) | sbatch `--time` in minutes. Set the runtime/timeout to **24h** for LMU dispatches: a full compiler × arch × opt matrix has long-tail per-variant builds (old-GCC cross-toolchains, heavy variants) that blow past a 60-minute cap and get SLURM-killed mid-run. 24h gives ample headroom; runaway jobs still auto-terminate at the limit. Pass `--slurm-time-limit 1440` (or `24:00:00`). |

### Setup deadline auto-scales — no manual override needed

Since `ba889cd`, the framework auto-computes the per-secondary setup deadline as `max(60, num_secondaries * 15)` (`crates/dynrunner-slurm/src/pipeline.rs::compute_setup_deadline_secs`). The 15s/secondary slope was calibrated against LMU Krater empirical observation. For `--jobs 15` you get 225s automatically; for `--jobs 32` you get 480s. The 60s floor covers `--jobs 1..4`.

The old `--slurm-setup-deadline-secs` override flag was removed; if a cluster genuinely slower than LMU ever needs more headroom, the current knob is `--unconfigured-deadline-secs N`. On LMU Krater the auto-scaled value is the validated default — don't override.

## Do NOT confuse with slurm-test-env mandates

The following are **slurm-test-env-only** flags. They are NOT LMU policy and should NOT be carried over to LMU dispatches:

| Test-env-only flag | Test-env reason | What LMU does instead |
|---|---|---|
| `--cores 2` | Test-env nodes are 2-CPU rootless-podman boxes; framework auto-detect would spawn way more workers than the cgroup allows. Required for every test-env smoke (memory `feedback_test_worker_cap.md`). | Do NOT pass `--cores 2` on LMU. LMU compute nodes are 14-CPU per `--slurm-cpus-per-task`; let the framework default size workers to the node, or pass a value matched to the node's cpus-per-task. Forcing `--cores 2` on LMU cripples production throughput. |
| `--ssh-identity-file <key>` | Test-env generates per-instance keypairs at provision time; the user dispatches as `kruppb@localhost:<SSH_PORT>` using that key, so the file path is the only auth route. | LMU's canonical auth is the 1Password SSH agent. Do NOT pass `--ssh-identity-file` for LMU dispatches; let the framework use the agent. `ssh-add -L` returning "no identities" is normal 1Password behaviour — auth still works via per-prompt approval. |
| `--slurm-cpus-per-task 2` | Test-env framework-default `--slurm-cpus-per-task 14` fails sbatch on the 2-CPU test-env nodes (memory `slurm_test_env_sbatch_flags.md`). | LMU uses the framework default `14`. Do NOT override `--slurm-cpus-per-task` on LMU. |
| `--slurm-partition debug` | Test-env only has the `debug` partition. | LMU uses `--slurm-partition Krater` (see firm table above). |

## SSH topology — why hand-rolled `-R` will silently fail

LMU's gateway has `GatewayPorts no`. A single-port `-R` bind on the public gateway interface does NOT work. The framework opens SSH ProxyJump through the gateway to each compute node (`ssh -J kruppb@remote.cip.ifi.lmu.de kruppb@<rock-node>`) and binds the `-R` reverse tunnel on the compute node's localhost. This is automatic; you do not configure it.

But it means: if you try to hand-roll a "let me just expose the primary on the gateway" topology it WILL silently fail. Compute nodes are not directly reachable; both `-A` (agent forwarding) and `-J` (ProxyJump) are required.

Gateway hostname is always `kruppb@remote.cip.ifi.lmu.de`. Never substitute the per-session FQDN the load balancer happens to land you on (`beryll`, `amazonit`, …) — `hostname -f` on the remote returns the load-balanced name, which is not the framework's expected hostname.

## Pre-flight ritual (mandatory every dispatch)

```bash
# 1. Orphan scan — 15-job cap means stale jobs starve the next dispatch
ssh kruppb@remote.cip.ifi.lmu.de "squeue -u kruppb --format='%i %P %j %T %M %l %R'"

# 2. If anything's there from a prior session that's not actively yours:
#    EITHER scancel specific job IDs (preferred when sharing the cap)
ssh kruppb@remote.cip.ifi.lmu.de "scancel <jobid>"
#    OR clear everything (only if you're sure nothing legitimate is running)
ssh kruppb@remote.cip.ifi.lmu.de "scancel -u kruppb"

# 3. Verify the gateway can reach your authorized key
ssh -i ~/.ssh/id_ed25519 kruppb@remote.cip.ifi.lmu.de true
```

If `ssh-add -L` says "no identities" that is the 1Password agent's design — auth still works via per-prompt approval. Do not pass `--ssh-identity-file` on LMU — that flag is for slurm-test-env's per-instance keypairs (see "Do NOT confuse" table above).

## Watching a running dispatch (60s LOCAL wake-tick + sacct fallback)

The watcher runs a 60-second wake-tick loop. The tick itself is **local** — a plain `sleep 60`, NOT an SSH-poll loop (per-tick SSH would thrash the 1Password agent):

```bash
while true; do date -Iseconds; sleep 60; done
```

On every wake:

1. **Check local progress evidence**:
   - dispatch log file's line count / size grew since last tick (filtered `tail` is fine; do NOT pull raw lines into context)
   - primary's stdout shows new `|P|` state-transition lines or `Completed: N/M` counter advanced
   - output dir entry count grew under `<slurm-root>/out/dataset/<binary>/<variant-id>/` (new per-variant binaries + `.json` sidecars landing as the build workers complete)

   Useful filter for the dispatch log (do NOT tail raw into context):

   ```bash
   tail -f /tmp/dispatch-*.log | grep -E --line-buffered \
     'Phase [0-9]|Job submitted|Secondary connected|Worker [0-9]+|TaskCompleted|TaskFailed|NonRecoverable|Traceback|connection refused|Completed:'
   ```
2. **If progress evidence is present**: continue watching.
3. **If no progress evidence**: ONE SSH check (not a poll loop) — `ssh kruppb@remote.cip.ifi.lmu.de "sacct -u kruppb --starttime <test-start-iso> --format=JobID,State,Elapsed,ExitCode -P"`. Compare against last tick's sacct output.
4. **Terminal failure criterion**: sacct shows every secondary's job + its `.batch` step in a terminal state (`FAILED`, `CANCELLED`, `TIMEOUT`, `COMPLETED`) AND no progress evidence was ever observed → the run is dead. Collect gateway log paths, sacct output, last observed local-log lines; proceed to cleanup.
5. **Stuck-but-still-running**: sacct shows `RUNNING` but the job hasn't advanced past mesh-formation for >10 ticks (~10 min) — same as terminal for decision purposes.

## Cleanup after a dispatch (success OR failure)

The cleanup steps below run on the parent side (or on the subagent only with explicit parent ack). Run all four; skipping any leaves stale state that breaks the next dispatch:

```bash
# 1. Orphan-scan the gateway — must see only OUR active jobs or empty
ssh kruppb@remote.cip.ifi.lmu.de "squeue -u kruppb"
# 2. scancel orphans (shared cap; check ownership before nuking)
ssh kruppb@remote.cip.ifi.lmu.de "scancel <jobid>"   # specific
ssh kruppb@remote.cip.ifi.lmu.de "scancel -u kruppb" # all (only if sure)
# 3. Kill local stray SSH reverse-tunnel processes from prior dispatches.
#    Stale tunnels block port allocation and produce confusing
#    "address in use" errors that look like framework bugs.
pkill -f 'ssh.*-J kruppb.*-R'
# 4. Kill local primary process if it did not exit cleanly
pkill -f 'python.*-m compiler_suit_runner'
```

Note: `scancel` does NOT propagate to nested podman containers on compute nodes — conmon double-forks to host systemd. Post-`a12f84a` the wrapper spawns a `setsid -f` watchdog that polls `squeue -j $SLURM_JOB_ID` and runs `podman kill` + `rm -f` when the job disappears, so this is now handled wrapper-side. If you see a leaked container on a compute node despite a cleared `squeue`, the watchdog failed — file with `dynrunner-owner`.

## Our `out/dataset/` output convention

We write to our own dedicated slurm-root, `~/BIG/slurm/gen-binary-dataset/` (`/home/k/kruppb/BIG/slurm/gen-binary-dataset/`). The build workers lay produced binaries out under the gateway-side `<slurm-root>/out/dataset/`:

```
/home/k/kruppb/BIG/slurm/gen-binary-dataset/out/dataset/<binary-name>/<variant-id>/<binary>
/home/k/kruppb/BIG/slurm/gen-binary-dataset/out/dataset/<binary-name>/<variant-id>.json   # sibling sidecar
```

`compiler_suit_runner` is a producer: every artifact under this tree is generated by the matrix_eval and build workers; we never pass `--source-already-staged`.

The `<binary>/<variant-id>/` path shape is the dataset's stable output contract — treat it as load-bearing, since anything that indexes the produced dataset keys off this layout. Before changing it for a new variant axis, update both this section and `SLURM_RUNBOOK.md`'s "Output layout".

## 15-job cap

kruppb has a 15-parallel-task SLURM quota at LMU. `--jobs 15` is our full-run ceiling (see the firm CLI overrides table). The quota may be shared with other kruppb jobs, so:

1. **Run a mandatory pre-flight orphan-scan** (`squeue -u kruppb`) before every dispatch — stale jobs from a prior session starve the next run of its full quota.
2. **Never `scancel -u kruppb` blindly.** If `squeue` shows entries you don't recognise, scancel them by explicit job-ID after confirming they're orphans, not a legitimately-running kruppb job.

## dynamic_runner pin policy

Our flake input `dynamic-runner` is bare (`github:sirati/dynamic-runner`) — `flake.lock` tracks the actual rev. Upgrade with `nix flake update dynamic-runner`. Do NOT manually `nix build .#dockerImage` afterwards — the dispatch builds and layered-blob-uploads the image itself; a manual pre-build is redundant process.

### Pin history (Tier-3 LMU green markers)

- **`328a78e`** (2026-05-15) — first Tier-3 GREEN end-to-end on LMU Krater `--jobs 15`. Includes the 11-commit fix lineage: sync-walk-aware discovery (`be3e2e9`), args-forwarding through phase chain (`1670e7a`), scale-aware setup-deadline (`ba889cd`), SSH-tunnel stagger + retry on MaxStartups (`d4ad1b7`), chain-gate (`76fe930`), peer-bus ClusterMutation arm (`ad71e83`), peer-repoll on PromotePrimary (`cd729fe`), originator flush rendezvous (`328a78e`). See memory `tier3_green_at_328a78e.md`.
- **`8ecd382`** (post-rebase) — upstream rebased/re-merged the DAG; `328a78e` is no longer a literal ancestor of main, but `8ecd382` is the equivalent merge (same merge title "Merge handoff/fix-runcomplete-writer-flush-race", same tree hash `834f643a036eec15dc315aa955c12e7fb362d345`). Functionally identical.
- **`2552f7c`** (2026-05-15) — current main tip at last check. Beyond `8ecd382` it adds: PyO3 codec migration (`2a31304`), secondary subprocess lifecycle migration (`365b649`), PodmanExecWorkerFactory migration (`8315a13`), SLURM submit_job + preparation migration (`612cfe3`, `01849ca`), ErrorType::Unfulfillable wire variant (`a581939`). **Not yet validated on LMU end-to-end** (as of 2026-05-15) — we are the canary.

### Lineage check rule of thumb

When asked to verify "is pin X on the same lineage as pin Y":

```bash
git -C ~/devel/python/dynamic_runner merge-base --is-ancestor Y X && echo OK || echo "not ancestor — check tree hashes"
git -C ~/devel/python/dynamic_runner show -s --format='%h %s' Y
git -C ~/devel/python/dynamic_runner show -s --format='%h %s' X
# If "not ancestor" — upstream may have rebased. Compare tree hashes to find the equivalent merge:
git -C ~/devel/python/dynamic_runner log --oneline --all | grep -F "<merge title>"
git -C ~/devel/python/dynamic_runner rev-parse Y^{tree}
git -C ~/devel/python/dynamic_runner rev-parse <candidate>^{tree}
```

Same tree hash on the same merge-title = same merge re-applied post-rebase = functionally equivalent.

## What you do NOT do

- You do NOT hand-roll `sbatch` + `ssh kruppb@<rock-node>` + `podman run` for an LMU dispatch. The framework owns all of that. The user has flagged this multiple times.
- You do NOT manually create gateway directories. The dispatcher creates `image_bin/`, `out/`, `log/`, `log/run_<ts>/`, `log/run_<ts>/connection_info/` itself.
- You do NOT manually upload the image. It's layered-blob-uploaded from the local nix store; only changed layers re-upload.
- You do NOT inspect the image with `python -c "import tarfile..."`. If you find yourself reaching for this, stop and re-read the runbook.
- You do NOT substitute the gateway hostname with the load-balanced FQDN `hostname -f` returns on the remote.
- You do NOT `scancel` jobs without first checking who owns the cap (15-job quota may be shared with other kruppb jobs).
- You do NOT change the `<slurm-root>/out/dataset/<binary>/<variant-id>/` output layout casually — it is our stable output contract; update both this file and the runbook if a new variant axis requires it.

## Reference paths

Files in this repo:
- `dynrunner/SLURM_RUNBOOK.md` — canonical dispatch recipe + flag-by-flag rationale + debugging recipes.
- `dynrunner/LMU_OPERATIONS.md` — this document.

Companion repos (sibling clones expected at the same parent dir):
- `~/devel/python/dynamic_runner/slurm-test-env/README.md` — slurm-test-env flake-app docs (45 lines). `nix run .#{up,down,smoke-test,provision-user,reboot-node}` from that directory. `INSTANCE_ID=<tag> SSH_PORT=<port>` scopes each instance.
- `~/devel/python/dynamic_runner/` — the framework source; check `git log --oneline -- python/` for recent Python-surface changes when bumping the flake input.

## When this document is wrong

Same protocol as `SLURM_RUNBOOK.md`: if a dispatch contradicts what's documented here, the bug is either (1) framework regression (escalate to `dynrunner-owner` peer), (2) recipe drift in this file (update alongside the bumped commit), or (3) LMU cluster-side change (re-derive the gateway layout and update).

In all three cases the fix is durable: update this file or upstream so the next person does not repeat the diagnosis.
