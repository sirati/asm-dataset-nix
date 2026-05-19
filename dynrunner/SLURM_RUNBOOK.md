# SLURM dispatch runbook

Recipe for running `compiler_suit_runner` end-to-end on SLURM — either against the local `slurm-test-env` flake-app cluster or the LMU CIP cluster — plus what to do when something breaks.

The intended audience is a fresh subagent or a future-you returning to this after a few weeks. **Follow the commands verbatim.** Do not re-explore the codebase to "figure out" the dispatch flags; this document is the source of truth, and if it disagrees with the framework that's a bug to file (see "When the runbook is wrong" at the end).

This runbook is **general** (covers both `slurm-test-env` and LMU CIP). For LMU-firm policy (15-job cap, partition default, `dataset/` output convention, pin-history) read `LMU_OPERATIONS.md` alongside.

## Prerequisites

- Working `nix develop` shell from `/home/sirati/devel/nix/asm-dataset-nix`. All commands run inside `nix develop --no-write-lock-file --command bash -c "..."` unless explicitly stated otherwise.
- For LMU dispatches: `~/.ssh/config` reachable for `kruppb@remote.cip.ifi.lmu.de`. 1Password SSH agent must be unlocked — if the gateway returns `signing failed for ED25519 "LMU CIP SSH Key" from agent: communication with agent failed`, the agent is locked and only the user can unlock it.
- For `slurm-test-env` dispatches: bring the env up via `cd ~/devel/python/dynamic_runner/slurm-test-env && nix run .#up`; obtain the per-instance keypair path and SSH port (see `LMU_OPERATIONS.md` "Do NOT confuse with slurm-test-env mandates" table for the test-env-only flags).
- `flake.lock` pinned to a `dynamic-runner` revision that contains the SLURM-path bug fixes (A–G + 1-9 + H-A/H-B + bumps through `8da909d` / `2552f7c` and later). Bump with `nix flake update dynamic-runner` and rebuild with `nix build --no-link .#dockerImage`.
- Image is rebuilt locally (`nix path-info .#dockerImage` → store path of the tar.gz). The runner uploads it via layered-blob transfer; only changed layers re-upload, so this is fast on iteration.

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
    --slurm-root-folder /home/k/kruppb/BIG/slurm \
    --slurm-partition Krater \
    --slurm-time-limit 60
' 2>&1 | tee /tmp/csr-dispatch-$(date +%s).log
```

Bump `--jobs` to scale; for full LMU dispatches use `--jobs 15` (the kruppb cap) — see `LMU_OPERATIONS.md` for the 15-job coordination protocol.

### Flag-by-flag rationale (do not omit any)

| Flag | Why |
|------|-----|
| `--shared-fs <path>` | Required. Local shared-FS root that holds `peers/`, `manifests/`, `partition/`, `dataset/`. The submit pre-flight creates the layout under this path; the framework forwards it as `--source <path>` to the runner so the per-binary `.tar.zst` artifacts land under `<path>/dataset/` locally AND on the gateway via the bind-mount. |
| `--packages <pkg…>` | Allowlist of packages to build, matched against the flake's package set. Omit to build the full default matrix (expensive); always pass an explicit list for ad-hoc dispatches. |
| `--archs <arch…>` | (Optional) Allowlist of architectures. Omit to use the flake's full arch matrix. Use for fast iteration: `--archs x86_64` for x86-only smoke tests. |
| `--multi-computer slurm` | Selects the SLURM dispatch pipeline. Don't use the deprecated `--slurm` flag. |
| `--packaging podman` | Required for SLURM; the cluster's wrapper uses rootless podman, not docker. |
| `--gateway ssh://kruppb@remote.cip.ifi.lmu.de` | LMU: always this hostname; never substitute the per-session FQDN (`beryll`, `amazonit`, …) the load balancer happens to land you on. Test-env: `ssh://kruppb@localhost:<SSH_PORT>`. |
| `--slurm-root-folder <gateway-abs>` | The gateway-side root for `image_bin/`, `out/`, `log/` subfolders. LMU: `/home/k/kruppb/BIG/slurm`. Test-env: per-instance, typically `/home/kruppb/slurm`. |
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
- You do NOT inspect the image with `python -c "import tarfile..."`. If you find yourself doing this, stop.
- You do NOT need `--skip-image-build` once you have a hash mismatch fixed. Use it ONLY when you've verified the gateway image already matches the local build (rare).
- You do NOT pass test-env-only flags on LMU dispatches (`--cores 2`, `--ssh-identity-file`, `--slurm-cpus-per-task 2`, `--slurm-partition debug`) — see `LMU_OPERATIONS.md` "Do NOT confuse" table.

## Output layout — we WRITE; we don't `--source-already-staged`

`compiler_suit_runner` is a producer, not a consumer: the secondaries build per-variant binary tarballs and lay them out under

```
<slurm-root>/out/dataset/<binary>/<variant-id>/<binary>          # the produced binary
<slurm-root>/out/dataset/<binary>/<variant-id>.json              # sibling sidecar (variant axes + provenance)
```

This is the **divergent layout** documented in `LMU_OPERATIONS.md` "Shared `/home/k/kruppb/BIG/slurm/out/` convention" — `asm-tokenizer` reads from this directory tree as its `--source-already-staged` input. If you ever need to change the `<binary>/<variant-id>/` layout (e.g. for a new variant axis), coordinate with `asm-tokenizer` first.

We never pass `--source-already-staged` on our submit side — we have no input corpus to bind-mount; every artifact is produced by the matrix_eval and build workers.

## Watching a running dispatch

The dispatcher emits structured log lines prefixed `INFO | HH:MM:SS |P|` (primary) and `|S0|` (secondary 0). Useful greps for a Monitor (do NOT tail the raw log into your context):

```bash
# Filter monitor — emit only state transitions and clear failure modes
tail -f /tmp/csr-dispatch-*.log | \
  grep -E --line-buffered \
    'Phase [0-9]|build_compilers|matrix_eval|dependency_graph|build_variant|Job submitted|Secondary connected|Worker [0-9]+|TaskCompleted|TaskFailed|NonRecoverable|Traceback|connection refused|Completed:'
```

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

**LMU 15-job cap:** never `scancel -u kruppb` without first checking ownership against the shared cap — see `LMU_OPERATIONS.md` "15-job cap coordination" for the protocol. Filter by job-name or by explicit job-ID list (job names use the `asm-suit-` prefix for our dispatches; `asm-tokenizer-` for the peer).

`scancel` on the controller does NOT propagate a kill to the podman container on the compute node — historical bug; post-`a12f84a` the wrapper spawns a `setsid -f` watchdog that polls `squeue -j $SLURM_JOB_ID` and runs `podman kill` + `rm -f` when the job disappears, so this is now handled wrapper-side. If you see a leaked container on a compute node despite a cleared `squeue`, the watchdog failed — file with `dynrunner-owner`.

If the watchdog hasn't fired (older pin or wrapper regression), fall back to:

```bash
ssh -A -J kruppb@remote.cip.ifi.lmu.de kruppb@${NODE}.cip.ifi.lmu.de \
  'pgrep -fa "compiler_suit_runner.*secondary" | awk "{print \$1}" | xargs -r kill -TERM'
```

## When the runbook is wrong

If the dispatch errors out with a message that contradicts what's documented here, the bug is one of:

1. **Framework regression in `dynamic_runner`** — file:line + legacy diff (`git show cab668ba^:dynamic_batch/<path>` in the framework repo) → bug report to peer Claude on `dynrunner-owner` channel. Bugs A–G all came from the 2026-04-28 Rust port + packaging refactor; further regressions of the same shape are likely.
2. **Recipe drift here** — flag was renamed, default changed, etc. Update this file alongside the bumped commit.
3. **Cluster-side change** — wrapper script regenerated, BIG paths moved, podman version changed. Re-derive the gateway layout via `ssh kruppb@remote.cip.ifi.lmu.de 'ls ~/BIG/slurm/'` and update.

In all three cases the fix is durable: update the runbook (or upstream) so the next person doesn't repeat the diagnosis.

## Bug history (for context — do not re-debug)

The following are framework-side bugs observed during `dynamic_runner`'s 2026-04-28 packaging refactor and the 2026-05-04 `--source-already-staged` feature push. They're catalogued for diagnosis-reuse; the fixes are already in the pin.

| Bug | Symptom | Fix commit (in `dynamic_runner`) |
|-----|---------|-----------------------------------|
| A | Rust URL parser rejected hostnames in secondary connect address | `070f015` |
| B | tilde paths shlex-quoted preventing bash expansion | `070f015` |
| C | `pipeline.py` skipped `gateway.setup_port_forwarding()` | `b07f5e7` |
| D | SSH tunnel direction `-L` (legacy was `-R`) + filename + format mismatches | `cf0b6ca` |
| E | Rust primary bound `127.0.0.1:0` instead of caller-supplied `primary_quic_port` | `c009339` |
| F | `in_docker` checked `/.dockerenv` (docker-only); podman has `/run/.containerenv`; fix uses `/app/src-network` mountpoint as runtime-agnostic sentinel | `0d91947` |
| G | `_collect_binaries` and `_drive_rust_primary` independently called `find_matching_binaries`, possibly disagreeing → "Queued 0 StageFile notifications" with non-empty corpus | `edde265` |
| H-B | `setup.rs:130 handle_initial_assignment` lacked the `dispatch.rs:50` fail-loud guard for `resolved_path.is_none()`; cache miss silently passed primary's local path to the worker → first-attempt Recoverable, second-attempt NonRecoverable through the operational loop. Fixed by factoring the predicate into `report_unresolvable_task` and calling from both setup.rs and dispatch.rs. | `76500ac` |
| H-A / K | `secondary/setup.rs:wait_for_setup` had no `MessageType::StageFile` match arm — StageFile messages arriving between PeerInfo and InitialAssignment fell to `other =>` and were silently dropped. Fixed by inlining `staged_files` records into the `InitialAssignment` message; the secondary now registers staged files atomically with the assignment batch, eliminating the ordering hazard category-wide rather than just the specific arm. | `1cc3b69` |
| J | `pipeline.py` calls `notify_stage_file(rel, rel)` but never uploads binaries; legacy's `_distribute_files` ZIP-batched + SCP'd. ~~**Resolved by clarifying the contract**~~ — auto-upload was later reintroduced (`pipeline.py:248` calls `upload_source_binaries` unconditionally when not `--source-already-staged`). That restoration carried a Python twin of Bug B; see "2026-05-08 upload-resolve" section below. | _won't fix (then reintroduced — see post-2026-05-08)_ |
| L | Cross-run tunnel cleanup `pkill -f 'ssh.*-L.*localhost'` matched nothing after Bug D's `-L`→`-R` flip. Trivial two-character fix to use `-R`. | `5848803` |
| M | `StageFile.file_hash` field carries `compute_task_hash` (DefaultHasher on path+identifier, 16-char hex) but `staging.rs` verifies via `compute_file_hash` (SHA256 of contents, 64-char hex). Hash schemes never match → every stage fails "hash mismatch". Fix: split the wire field into `file_hash` (task identity) + `content_hash` (SHA256). | `86887b9` |
| N | New `compute_file_content_hash` PyO3 function registered on `_native` but not re-exported in `python/dynamic_runner/__init__.py`. `pipeline.py` does `import dynamic_runner as _rs` and accesses `_rs.compute_file_content_hash` → AttributeError → primary aborts before sbatch submission. Trivial one-line fix in `__init__.py`. | _historical — resolved in subsequent bump_ |

(N1) **Resolved 2026-05-04** — outputs DO land in the durable `/app/out-network` mount; earlier "rm-rf'd into /app/out-tmp" diagnosis was stale post-`fb1df86`. `8da909d` additionally fixed the directory-mirroring (worker's `--source` now matches the bind-mount root, so `relative_to(source_dir)` succeeds and outputs mirror the source layout under `<slurm-root>/out/`). (N2) `--secondary-quic-port` argv flag is parsed but ignored — the secondary unconditionally binds `0.0.0.0:0`; peers learn the real port via `CertExchange`.

All bugs above were introduced by the 2026-04-28 packaging refactor (`1f8d0a1`). Pre-refactor working baseline is `cab668ba^:dynamic_batch/...`.

## 2026-05-04 dispatch-path bug lineage (post-PR3 `--source-already-staged`)

PR3 (`eb69a80 feat(slurm): --source-already-staged`) shipped underspecified — the feature path had multiple unexercised dispatch sites. Eight follow-up bugs landed within the same day. `compiler_suit_runner` does NOT pass `--source-already-staged` (we are producers, not consumers — see "Output layout" above), so most of these manifested only on the asm-tokenizer side; they're catalogued here for diagnosis-reuse if a downstream consumer ever reads from our `out/dataset/`:

| # | Commit | Symptom | Root cause |
|---|--------|---------|------------|
| 1 | `144b9da` | `find: unknown predicate -L` on SSH discovery | GNU `find` requires `-L` *before* the path, not after. |
| 2 | `bf1ce02` | "not pre-staged" + hash-machinery 16-char vs SHA256 64-char mismatch | Pre-staged mode skips `StageFile` so cache is empty; secondary's hash-based resolver couldn't match. New `resolve_pre_staged` path. |
| 3 | `a344b0e` | "not pre-staged at /home/...gateway-abs/..." even after bf1ce02 | Wire's `local_path` was the gateway-absolute path; secondary's `src_network.join(local_path)` dropped LHS (Path::join with absolute RHS). Primary now strips `source_pre_staged_root` before wire emit. |
| 4 | `059f132` | `TypeError: source_pre_staged kwarg unexpected` | `pipeline.py` passed `source_pre_staged=bool(...)` but Rust pyclass had renamed to `source_pre_staged_root: Option<PathBuf>`. |
| 5 | `76d074a` | `cargoHash` mismatch in `nix/wheel.nix` | `38596aa`'s test-fixture commit added `tempfile` to `Cargo.lock` without bumping the recorded hash. |
| 6 | `796feff` | `NameError: slurm_config` in `_drive_rust_primary` | `059f132`'s patch referenced a variable not in scope. |
| 7 | `217093c` | Initial-batch tasks fail NonRecoverable while operational-loop tasks resolve correctly | `primary/assignment.rs:127` didn't use `wire_local_path`; only `task.rs` and `lifecycle.rs` were patched in `a344b0e`. |
| 8 | `8658c5b` | 219/235 Recoverable "Not a valid binary file" with gateway-abs paths | SLURM-promoted-secondary's self-assign path (`secondary/slurm.rs:224-236`) called the hash-verifying resolver instead of branching on `pre_staged_mode`. Refactor extracted `resolve_for_dispatch` helper called from all 4 dispatch sites. |
| 9 | `8da909d` | Outputs land at `<slurm-root>/out/src-network/<file>` instead of mirroring source layout | `_dispatch_secondary` passed `cfg.src_tmp` (not `cfg.src_network`) as worker's `--source`; worker's `relative_to(source_dir)` fell through to the parent-name fallback. **Affects us directly** — this is the bug that made `out/dataset/<binary>/<variant>/` land in the right place. Pin must be at `8da909d` or later for the output convention to hold. |

End-to-end dispatch confirmed green at `8da909d` against 235 minigzipsh on `/home/k/kruppb/BIG/Dataset-1/zlib/` (asm-tokenizer side); for `compiler_suit_runner`, see `LMU_OPERATIONS.md` pin-history for our own GREEN markers.

## 2026-05-08 upload-resolve bug (post-Bug-J auto-upload restoration)

After Bug J's "won't fix" verdict the auto-upload mechanism was restored: `pipeline.py:248` now calls `upload_source_binaries` unconditionally when `--source-already-staged` is not given. Since `compiler_suit_runner` does NOT use `--source-already-staged`, we DO take the upload path — `--source <shared_fs>` is forwarded automatically by the submit wrapper, so the per-binary `.tar.zst` artifacts flow through `upload_source_binaries`. The restored implementation carries a Python twin of the Rust Bug B (`relative_to` resolved against the wrong root):

| # | Symptom | Root cause | Status |
|---|---------|------------|--------|
| 10 | `Source-binary upload complete (0/N files)` despite full StageFile enumeration → every task `java.io.IOException: File not found: /app/src-network/<rel>` Recoverable, eventually NonRecoverable. | `dynamic_runner/packaging/job_manager.py:upload_source_binaries`: `Path(binary.path).resolve()` resolves relative `binary.path` against `os.getcwd()` (dispatcher CWD) instead of `src_root`, so every `relative_to(src_root)` throws `ValueError` and falls into the "skipping upload" branch. Tasks that emit relative paths trip this. | Fixed at `d5d0604` (now on main). 3103/3103 upload confirmed on the patched re-dispatch. |
| 11 | Wrapper-script "command relay" subshell CPU-spins to the tune of ~16 K iter/sec after the wrapper's EXIT trap removes the FIFO it was reading from. ~1.4 GB/h of identical `cmd.sock: No such file or directory` lines into slurm-stderr; SLURM job already returned 0 so no upstream notices. | `dynamic_runner/packaging/job_manager.py:305-340` emits a backgrounded `while true; do read -r CMD < "$cmd_socket"; ...; done &`. Under normal operation `read` blocks on the FIFO. When the EXIT trap deletes `$RNDTMP` (and thus the FIFO), the redirect fails with ENOENT immediately on every iteration → busy-spin. | Fixed at `90ba235` (now on main): cleanup trap kills `$CMD_RELAY_PID` BEFORE `rm -rf`; loop changed to `while [ -p "$cmd_socket" ]` for defense-in-depth. |
| 12 | Wrapper aborts on `podman load` failure but the .out file shows no error indication — it just stops mid-Phase-1 between "Loading image into container runtime..." and "Cleaning up temporary directory" with no clue why. | `set -e` IS triggering correctly (load returned non-zero), but the load command's stderr was being swallowed and no explicit error marker was logged before the trap fired. Diagnostic regression, not behavioural. | Fixed at `733559c` (now on main): load wrapped in `if ! <load>; then echo ERROR; exit 1; fi`, surfaces visible "ERROR: image load failed" marker before trap. |

Adjacent (non-blocking) findings from the same dispatch:
- **Misleading log message** at `job_manager.py:97-102` — printed `local` (pre-resolve, the relative path) but compared `local.resolve()` (CWD-rooted), so users see `Binary X is not under --source root /home/.../src` — looks impossible. Patch `d5d0604` also prints both raw and resolved paths.
- **conmon-detached scancel leak** extends the cleanup section: scancel + dispatcher SIGTERM did not propagate to the nested asm-tokenizer container because conmon's by-design double-fork reparents to host systemd. Fixed at `a12f84a` (in main via `eedd7da`/`733559c`): wrapper now spawns a `setsid -f` detached watchdog that polls `squeue -j $SLURM_JOB_ID` once/sec and runs `podman kill`+`rm -f` against the container by name when the job disappears.
- **Disconnect-race warning at gateway exit** ("Control socket connect: No such file or directory" at `ssh_gateway.py:144-145`). Root cause: `pkill -f 'ssh.*-R.*localhost'` was matching the master because per-secondary forwards use `-R 0.0.0.0:port:localhost:port`. Fixed at `c399f5a` (in main via `eedd7da`/`733559c`): reorder + tighten regex to `ssh.*-R [0-9]+:localhost`.
- **`ssh_gateway.py:96` connect-failure error message** now hints at `--ssh-config` — fixed at `178a3af` (in main).
- **`ssh_gateway.py:275-277` dead "(can be made configurable)" comment** removed at `d53d4fe` (in main).

## Open framework issues observed in run-7

- **Heartbeat clock not reset at connection-establishment.** Primary's heartbeat monitor checks `last_seen_s` against a fixed clock that started at primary startup, not at each secondary's connection. Containers that take >threshold to start + handshake (~38s for the LMU SLURM wrapper) are dropped immediately when the operational loop begins, with their in-flight tasks requeued to peers. The dropped secondary still completes its already-assigned batch independently before being isolated. Not blocking for small smoke runs (sec-0 picked up the requeue), but production-relevant for larger clusters with staggered scheduling. Filed with peer.

## slurm-test-env vs LMU CIP — differences worth knowing

`slurm-test-env` is a legitimate SLURM testing environment: it runs real `slurmctld` + `slurmd`, with a per-instance shared `/home` (network-share-shaped) and per-worker `/tmp` (individual scratch), so SLURM-protocol semantics, job submission, scheduling, multi-node srun, and dispatch flow all behave like the production cluster. A green dispatch here is meaningful evidence that the framework's SLURM path works.

The differences below are "things that look the same but aren't quite" — useful for interpreting results, not blockers on declaring green:

- **I1 — Partitions.** Test-env has only `debug`; LMU CIP has `All`, `AMD`, `NvidiaAll`, `Krater`, `Abaki`. Frameworks must pass `--slurm-partition debug` for test-env (LMU's firm choice is `Krater`).
- **I2 — Scale.** Test-env runs 4 workers (ceiling 16); LMU CIP has 131 nodes (40 in our `Krater` partition). Race conditions or arithmetic that only manifests at scale (e.g. the run-7 heartbeat-clock issue documented above) is unlikely to surface locally.
- **I3 — `/home` backing.** Test-env's `/home` is a host bind-mount; LMU CIP's is NFS. Strong-consistency operations behave the same; NFS-specific failure modes (`ESTALE`, soft-mount recovery, lock-daemon issues) are NFS-only.
- **I4 — SSH topology.** LMU CIP requires `-J kruppb@gateway-fqdn` for compute-node access; test-env exposes workers on a podman network with direct routability. Frameworks that consistently use `-J` work both places; anything that hardcodes direct compute-node ssh would fail on real cluster.
- **I5 — Accounting.** Test-env disables `slurmdbd` (`sacct` returns empty); LMU CIP has it. Frameworks that poll `sacct` need a non-`sacct` fallback for test-env mode — confirmed working in this campaign (we used `squeue` polling instead).
- **I6 — Munge key.** Test-env bakes a fixed key at image build; LMU CIP rotates. Anything that caches auth state across rotations is LMU-specific.
- **I7 — SSH key path.** Test-env uses per-instance keypairs via `--ssh-identity-file`; LMU CIP uses a 1Password agent (locked-agent failure modes are LMU-only). Both go through the same `--ssh-config` / `--ssh-identity-file` framework primitives.
- **I8 — Image-load latency.** Test-env podman load is rootless + slow: ~50–90 s per worker, sequential because each worker reads the same 1.5 GB tarball from the shared `/home` bind-mount. LMU CIP is faster (warm-cached, real NFS bandwidth). The framework auto-scales the per-secondary setup deadline as `max(60, num_secondaries * 15)` (since `ba889cd`) — on test-env with `--jobs 4` you get 60 s, which is shorter than the slowest worker's image load. The first-to-connect secondary then sees its setup deadline elapse with "no primary, no peers" while the primary is healthy but still waiting for the other 3 secondaries to register; it cold-exits, then the primary's later setup-bootstrap broadcast fails for that dead secondary with `channel closed` and the whole coordinator aborts. Result: `Completed: 0  Failed: 0  Stranded: 0`, exit code 0, empty output. **Always pass `--slurm-setup-deadline-secs 600` on test-env smokes** to avoid this race; LMU CIP doesn't need the override (the auto-scaled value covers its faster image load — see `LMU_OPERATIONS.md` "Setup deadline auto-scales — do NOT pass `--slurm-setup-deadline-secs`").

In practice: iterate locally on `slurm-test-env` (with the I8 override), then do a small-`--jobs 1` confirmation run on LMU CIP before scaling up. The local→cluster delta is small enough that this is a sanity check, not a redo.
