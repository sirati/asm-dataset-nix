# SLURM dispatch runbook

Recipe for running `compiler_suit_runner` end-to-end on SLURM — either against the local `slurm-test-env` flake-app cluster or the LMU CIP cluster — plus what to do when something breaks.

The intended audience is a fresh subagent or a future-you returning to this after a few weeks. **Follow the commands verbatim.** Do not re-explore the codebase to "figure out" the dispatch flags; this document is the source of truth, and if it disagrees with the framework that's a bug to file (see "When the runbook is wrong" at the end).

This runbook is **general** (covers both `slurm-test-env` and LMU CIP). For LMU-firm policy (15-job cap, partition default, `dataset/` output convention, pin-history) read `LMU_OPERATIONS.md` alongside.

For the slurm-root layout (where `out/`, `log/`, `image_bin/` live on the gateway) our dataset-generation operational root is `~/BIG/slurm/gen-binary-dataset/` — on LMU the absolute path is `/home/k/kruppb/BIG/slurm/gen-binary-dataset/`.

## Prerequisites

- Working `nix develop` shell from `/home/sirati/devel/nix/asm-dataset-nix`. All commands run inside `nix develop --no-write-lock-file --command bash -c "..."` unless explicitly stated otherwise.
- For LMU dispatches: `~/.ssh/config` reachable for `kruppb@remote.cip.ifi.lmu.de`. 1Password SSH agent must be unlocked — if the gateway returns `signing failed for ED25519 "LMU CIP SSH Key" from agent: communication with agent failed`, the agent is locked and only the user can unlock it.
- For `slurm-test-env` dispatches: bring the env up via `cd ~/devel/python/dynamic_runner/slurm-test-env && nix run .#up`; obtain the per-instance keypair path and SSH port (see `LMU_OPERATIONS.md` "Do NOT confuse with slurm-test-env mandates" table for the test-env-only flags).
- `flake.lock` pinned to a current `dynamic-runner` revision (see `LMU_OPERATIONS.md` pin-history for the validated tip). Bump with `nix flake update dynamic-runner`.
- The image is built and uploaded BY THE DISPATCH itself (layered-blob transfer; only changed layers re-upload). Do NOT manually `nix build .#dockerImage` before dispatching — redundant process.

## The dispatch command

`compiler_suit_runner submit` is the canonical dispatch entry point. It:
- enumerates toolchains locally, emits the `build_compilers` (optional) + `matrix_eval` manifests,
- launches the dynamic_runner primary,
- coordinates secondary containers that run `matrix_eval`, `dependency_graph`, and `build` workers,
- and lands per-variant tarballs at `<slurm-root>/out/dataset/<binary>/<variant-id>/`.

Canonical small-batch dispatch against the LMU cluster (one `hello` package across all compilers / archs / opt-levels, sampling 2 random flag-hardening combos per group, single secondary):

```bash
nix develop --no-write-lock-file --command bash -c '
  cd /home/sirati/devel/nix/asm-dataset-nix
  PYTHONPATH=python python -m compiler_suit_runner submit \
    --shared-fs /tmp/asm-suit-shared \
    --packages hello \
    --multi-computer slurm \
    --packaging podman \
    --jobs 1 \
    --gateway "ssh://kruppb@remote.cip.ifi.lmu.de" \
    --slurm-root-folder /home/k/kruppb/BIG/slurm/gen-binary-dataset \
    --slurm-partition Krater \
    --slurm-time-limit 60
' 2>&1 | tee /tmp/csr-dispatch-$(date +%s).log
```

Bump `--jobs` to scale; for full LMU dispatches use `--jobs 15` (the kruppb cap) — see `LMU_OPERATIONS.md` "15-job cap" for the orphan-scan ritual.

### Flag-by-flag rationale (do not omit any)

| Flag | Why |
|------|-----|
| `--shared-fs <path>` | Required. Local shared-FS root that holds `peers/`, `manifests/`, `partition/`, `dataset/`. The submit pre-flight creates the layout under this path; the framework forwards it as `--source <path>` to the runner so the per-binary `.tar.zst` artifacts land under `<path>/dataset/` locally AND on the gateway via the bind-mount. |
| `--packages <pkg…>` | Allowlist of packages to build, matched against the flake's package set. Omit to build the full default matrix (expensive); always pass an explicit list for ad-hoc dispatches. |
| `--archs <arch…>` | (Optional) Allowlist of architectures. Omit to use the flake's full arch matrix. Use for fast iteration: `--archs x86_64` for x86-only smoke tests. |
| `--multi-computer slurm` | Selects the SLURM dispatch pipeline. Don't use the deprecated `--slurm` flag. |
| `--packaging podman` | Required for SLURM; the cluster's wrapper uses rootless podman, not docker. |
| `--gateway ssh://kruppb@remote.cip.ifi.lmu.de` | LMU: always this hostname; never substitute the per-session FQDN (`beryll`, `amazonit`, …) the load balancer happens to land you on. Test-env: `ssh://kruppb@localhost:<SSH_PORT>`. |
| `--slurm-root-folder <gateway-abs>` | The gateway-side root for `image_bin/`, `out/`, `log/` subfolders. LMU: `/home/k/kruppb/BIG/slurm/gen-binary-dataset`. Test-env: per-instance, typically `/home/kruppb/slurm`. |
| `--jobs N` | Number of SLURM secondaries to spawn. Start at 1 for first validation; for LMU full runs use `--jobs 15` (kruppb cap); for test-env use `--jobs 1..4` (4-worker ceiling). |
| `--slurm-time-limit <hh:mm:ss\|N>` | sbatch `--time`. Accepts either `hh:mm:ss` or plain minutes. `60` minutes is comfortable for a single-package matrix; bump to `6:00:00` for full builds with `--build-compilers`. |
| `--slurm-partition <name>` | sbatch `--partition`. LMU firm: `Krater` (see `LMU_OPERATIONS.md`). Test-env: `debug`. |
| `--build-compilers` | (Optional) Build toolchain closures on secondaries instead of requiring them to be locally realised on the submitter. Default OFF (the submit pre-flight expects every toolchain output already in the local nix store). Turn ON when the local store is bare or when toolchain realisation would dominate dispatch time. |
| `--sys <attr>` | (Optional) Flake system attribute (default `x86_64-linux`). |
| `--slurm-cpus-per-task N` | (Optional) sbatch `--cpus-per-task`. LMU default 14 (don't override); test-env requires `2` (the 2-CPU rootless-podman nodes). |
| `--debug-testbuild <binary>` | (Optional) Inject a Phase 1.5 `toolchain_validate` smoke step between `build_compilers` and `matrix_eval` for the named binary. Only meaningful with `--build-compilers`. |
| `--enable-ssh-debug` | (Optional) Stand up the SSH-debug overlay so secondaries can be inspected mid-run. Adds the `ssh_debug` orchestrator coordination layer. |

### What you do NOT need

- You do NOT manually create dirs on the gateway. The dispatcher creates `image_bin/`, `out/`, `log/`, `log/run_<ts>/`, `log/run_<ts>/connection_info/` itself.
- You do NOT manually upload the image. It's transferred via layered-blob upload from the local nix store the first time it differs.
- You do NOT pass `--source` / `--output` to `compiler_suit_runner`. The submit wrapper auto-forwards `--source <shared_fs>` and `--output <shared_fs>/dataset` to the framework. (The `dataset/` subdirectory under `shared_fs` is the local mirror; on the gateway the binaries land at `<slurm-root>/out/dataset/`.)
- You do NOT pass `--skip-image-build` unless you've verified the gateway image already matches the local build (rare).
- You do NOT pass test-env-only flags on LMU dispatches (`--cores 2`, `--ssh-identity-file`, `--slurm-cpus-per-task 2`, `--slurm-partition debug`) — see `LMU_OPERATIONS.md` "Do NOT confuse" table.

## Output layout — we WRITE; we don't `--source-already-staged`

`compiler_suit_runner` is a producer, not a consumer: the secondaries build per-variant binary tarballs and lay them out under

```
<slurm-root>/out/dataset/<binary>/<variant-id>/<binary>          # the produced binary
<slurm-root>/out/dataset/<binary>/<variant-id>.json              # sibling sidecar (variant axes + provenance)
```

This `<binary>/<variant-id>/` path shape is the dataset's stable output contract (also recorded in `LMU_OPERATIONS.md` "Our `out/dataset/` output convention"). Treat it as load-bearing — anything that indexes the produced dataset keys off this layout — before changing it for a new variant axis.

We never pass `--source-already-staged` on our submit side — we have no input corpus to bind-mount; every artifact is produced by the matrix_eval and build workers.

## Watching a running dispatch

**The correct pattern: the Monitor's command IS the dispatch — nothing wraps it.** Put `--important-stdio-only` directly on the `compiler_suit_runner submit` invocation and run THAT as the Monitor command. Under `--important-stdio-only` the submitter's stdout carries only wake-worthy events (secondary connect / `primary changed` promotion / per-phase `phase complete` / `run complete` / failures) plus a ~10-min summary; the verbose log is kept out of stdout. Each stdout line becomes one Monitor event and the process exits on its own.

```text
Monitor(command = "cd <repo> && nix develop --no-write-lock-file --command bash -c \
  'PYTHONPATH=python python -m compiler_suit_runner submit <flags…> --important-stdio-only'",
        persistent = true)
```

`--important-stdio-only` is a **dynamic_runner CLI flag** (there is no equivalent env var). `compiler_suit_runner submit` accepts it and forwards it to the framework; the framework drops it from the secondary argv, so grid workers keep FULL logs.

**FORBIDDEN:**
- Backgrounding the dispatch and pointing a SEPARATE `Monitor 'tail -f out.log'` at its log — two moving parts where one suffices; the Monitor must BE the dispatch.
- Wrapping the dispatch in a babysitter poll-loop (`while/until squeue|sacct|ps …; sleep N; done`), a `timeout`, a `tee`, or any pipe.
- A `squeue`/`sacct` poll-loop Monitor to "watch teardown."
- Omitting `--important-stdio-only` and monitoring raw verbose stdout — it floods the event stream and the Monitor auto-stops.

**Verify completion by GATEWAY OUTPUT FILES, never the local exit.** Under the peer overlay the submitter RELOCATES primary authority to a compute peer (`primary changed primary=secondary-N`), becomes an observer, and exits — so the Monitor ending is NOT completion, and the relocated primary's narration no longer reaches the submitter stdout. Therefore:
- On relocated runs the dispatcher's terminal `run complete: N succeeded` line and the exit accounting can read ALL-ZEROS even on full success. Never rely on them either way.
- Confirm success ONLY by the produced artifacts on the gateway: per-variant binaries + `.json` sidecars under `<slurm-root>/out/dataset/<binary>/<variant-id>/`, with FRESH mtimes (compare against the gateway's own `date` — stale output from a prior run looks identical otherwise; `find … -mmin -15` is the cheap freshness gate). `squeue` empty = clean teardown.
- The relocated primary's full per-role logs land durably at `<slurm-root>/log/run_<ts>/<secondary_id>/{primary,secondary}.log`; read those for post-promotion detail. Per-secondary SLURM stdio is at `slurm_<jobid>.{out,err}`.

Cluster-side liveness check from a wake-up script (every ~60s; LOCAL ticker, not an SSH-poll loop):

```bash
ssh kruppb@remote.cip.ifi.lmu.de \
  "sacct -u kruppb --starttime $TEST_START_ISO --format JobID,State,Elapsed,ExitCode -P" \
  | head -20
```

(For test-env, replace the gateway with `kruppb@localhost -p <SSH_PORT>` and note that `slurmdbd` is disabled — fall back to `squeue` polling.)

## Inspecting a running secondary

When a SLURM job is in state `R`, find its node and inspect the container:

```bash
NODE=$(ssh kruppb@remote.cip.ifi.lmu.de "squeue -u kruppb -h -o %N -j <jobid>")
ssh -A -J kruppb@remote.cip.ifi.lmu.de kruppb@${NODE}.cip.ifi.lmu.de \
  'ASM_DIR=$(ls -dt /tmp/asm-* | head -1) && \
   podman --root $ASM_DIR/storage --runroot $ASM_DIR/run ps; \
   podman --root $ASM_DIR/storage --runroot $ASM_DIR/run logs --tail 50 $(podman --root $ASM_DIR/storage --runroot $ASM_DIR/run ps -q | head -1)'
```

`-A` (agent forwarding) and `-J` (ProxyJump through gateway) are both required on LMU; compute nodes are not directly reachable. On `slurm-test-env`, the worker container is directly accessible on the podman bridge — no ProxyJump needed.

Gateway-side structured logs (per secondary): `<slurm-root>/log/run_<ts>/slurm_<jobid>.{out,err}`.

## Cleanup after a run

```bash
# Whether the run succeeded or failed
ssh kruppb@remote.cip.ifi.lmu.de 'squeue -u kruppb'              # any leftover jobs?
ssh kruppb@remote.cip.ifi.lmu.de 'scancel <stuck-jobid>'         # only if stuck
pkill -f 'ssh.*-J kruppb.*-R'                                    # local stray reverse tunnels
pkill -f 'python.*-m compiler_suit_runner'                       # local primary if it didn't exit
```

**LMU 15-job cap:** never `scancel -u kruppb` without first checking ownership against the shared cap — see `LMU_OPERATIONS.md` "15-job cap" for the orphan-scan ritual. Filter by job-name or by explicit job-ID list (our dispatch job names use the `asm-suit-` prefix).

`scancel` on the controller does NOT by itself propagate a kill to the podman container on the compute node; the wrapper spawns a `setsid -f` watchdog that polls `squeue -j $SLURM_JOB_ID` and runs `podman kill` + `rm -f` when the job disappears, so this is handled wrapper-side. If you see a leaked container on a compute node despite a cleared `squeue`, the watchdog failed — file with `dynrunner-owner`.

If the watchdog hasn't fired and a container is leaked, fall back to:

```bash
ssh -A -J kruppb@remote.cip.ifi.lmu.de kruppb@${NODE}.cip.ifi.lmu.de \
  'pgrep -fa "compiler_suit_runner.*secondary" | awk "{print \$1}" | xargs -r kill -TERM'
```

## When the runbook is wrong

If the dispatch errors out with a message that contradicts what's documented here, the cause is one of:

1. **Framework regression in `dynamic_runner`** — report it to the `dynrunner-owner` peer.
2. **Recipe drift here** — a flag was renamed, a default changed, etc. Update this file alongside the bumped commit.
3. **Cluster-side change** — wrapper script regenerated, BIG paths moved, podman version changed. Re-derive the gateway layout via `ssh kruppb@remote.cip.ifi.lmu.de 'ls ~/BIG/slurm/gen-binary-dataset/'` and update.

In all three cases the fix is durable: update the runbook (or upstream) so the next person doesn't repeat the diagnosis.

## slurm-test-env vs LMU CIP — differences worth knowing

`slurm-test-env` is a legitimate SLURM testing environment: it runs real `slurmctld` + `slurmd`, with a per-instance shared `/home` (network-share-shaped) and per-worker `/tmp` (individual scratch), so SLURM-protocol semantics, job submission, scheduling, multi-node srun, and dispatch flow all behave like the production cluster. A green dispatch here is meaningful evidence that the framework's SLURM path works.

The differences below are "things that look the same but aren't quite" — useful for interpreting results, not blockers on declaring green:

- **I1 — Partitions.** Test-env has only `debug`; LMU CIP has `All`, `AMD`, `NvidiaAll`, `Krater`, `Abaki`. Frameworks must pass `--slurm-partition debug` for test-env (LMU's firm choice is `Krater`).
- **I2 — Scale.** Test-env runs 4 workers (ceiling 16); LMU CIP has 131 nodes (40 in our `Krater` partition). Race conditions or arithmetic that only manifests at scale is unlikely to surface locally.
- **I3 — `/home` backing.** Test-env's `/home` is a host bind-mount; LMU CIP's is NFS. Strong-consistency operations behave the same; NFS-specific failure modes (`ESTALE`, soft-mount recovery, lock-daemon issues) are NFS-only.
- **I4 — SSH topology.** LMU CIP requires `-J kruppb@gateway-fqdn` for compute-node access; test-env exposes workers on a podman network with direct routability. Frameworks that consistently use `-J` work both places; anything that hardcodes direct compute-node ssh would fail on real cluster.
- **I5 — Accounting.** Test-env disables `slurmdbd` (`sacct` returns empty); LMU CIP has it. Frameworks that poll `sacct` need a non-`sacct` fallback for test-env mode (use `squeue` polling).
- **I6 — Munge key.** Test-env bakes a fixed key at image build; LMU CIP rotates. Anything that caches auth state across rotations is LMU-specific.
- **I7 — SSH key path.** Test-env uses per-instance keypairs via `--ssh-identity-file`; LMU CIP uses a 1Password agent (locked-agent failure modes are LMU-only). Both go through the same `--ssh-config` / `--ssh-identity-file` framework primitives.
- **I8 — Image-load latency.** Test-env podman load is rootless + slow: ~50–90 s per worker, sequential because each worker reads the same 1.5 GB tarball from the shared `/home` bind-mount. LMU CIP is faster (warm-cached, real NFS bandwidth). The framework auto-scales the per-secondary setup deadline to cover this (see `LMU_OPERATIONS.md` "Setup deadline auto-scales"). If a scenario needs more headroom, the knob is `--unconfigured-deadline-secs N`.

In practice: iterate locally on `slurm-test-env`, then do a small `--jobs 1` confirmation run on LMU CIP before scaling up. The local→cluster delta is small enough that this is a sanity check, not a redo.
