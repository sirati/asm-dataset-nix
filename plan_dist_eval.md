# Cluster-side dispatch architecture — K=3 replication (in-flight) + distributed eval pipeline (NEW)

## Plan organisation

This plan now covers two related-but-separate workstreams:

- **Part A (existing, open):** K=3 toolchain replication + Q1–Q4 framework asks. Consumer foundation landed; waiting on framework batches. Sections preserved verbatim below.
- **Part B (NEW):** Distributed evaluation pipeline — move `nix eval` from the local submitter to the cluster, restructure as Phase -1 (bootstrap) → Phase 0 (distributed eval) → Phase 1 (planner) → existing build phases. Includes a new framework ask (Q5: dynamic task injection at phase boundary).

Part B *uses* Part A's primitives (peer_push, K=3 replication, harmonia federation) but doesn't depend on Part A's framework asks landing first. The two can ship in parallel.

---

# Part A — Cluster-wide K=3 toolchain replication (existing open work)

## Status (2026-05-15, post round-trip 2)

**Consumer-side foundation: LANDED.** All 13 implementation tasks from
the original plan are merged on `dynrunner-refactor-bump`; 429 unit
tests pass. Replication coordination plane is **active in degraded
mode** (NFS-poll fallback for peer-death, no surface for
Unfulfillable/preferred/observer) until framework batches land.

**Framework asks: LOCKED. Wire-format committed by `dynrunner-owner`.**
Round-trip 2 closed with all four design decisions confirmed; see
final design summary below. Sequencing announced:

- **Phase 0b primitives (in flight now)**: Q1 `fail_permanent` API.
- **Batch D+E (queued behind Phase 0b)**: Q4 `peer_lifecycle_listener`
  + Q3 outpath-blind matcher hook (land together).
- **Phase 4 (gated on Q3 wire)**: Q2 runtime
  `update_preferred_secondaries` API.

`dynrunner-owner` will ping per ask as each lands mergeable on master.

### What's running now (degraded mode, no upstream support required)

- K=3 receive-side cascade via `peer_push` HTTP and 4 handshake
  endpoints (`/peer/path-offer|accept|reject|cancel`), 1 s timeout,
  `already-targeted` reject coordination.
- Repair-on-death (NFS-poll fallback, 5–60 s convergence).
- `--replication-k` / `--no-observer-as-holder` CLI flags wired into
  `SuitTaskConfig`.
- `preferred_secondaries` field emitted on every variant manifest
  (currently empty; remains useful as a seed channel even with the
  locked Q2 runtime API — see Ask 2 wire-in below).
- Stub integration test `test_t11_k3_replication.py` covers
  happy-path cascade.

### What's NOT yet working (gated on framework)

1. **Fast peer-death detection** → gated on Q4.
2. **Permanent-dep-missing surface** → gated on Q1.
3. **Placement-aware scheduling** → gated on Q2.
4. **Observer-driven recovery** → gated on Q3.
5. **Cascade latency in subprocess flow** → P1 (consumer polish).
6. **Death-test / race-test integration coverage** → gated on Q4.

---

## Locked framework contract (final design after round-trip 2)

This section is the source of truth for what to expect from
upstream. Every per-ask wire-in below references these shapes — do
not assume the original ask shape; use these.

### Q1 — Unfulfillable: `fail_permanent` + cascade-fail + consumer re-inject

**STATUS: LOCKED (round-trip 4, 2026-05-15, all 7 sub-questions
confirmed).** Wire-level pieces land in Batch C (~5 min from then),
state-machine pieces land in the immediate follow-up batch (next
morning), matcher hook lands in Batch E3. Consumer wire-in deferred
until all three are merged; one consolidated commit then.

**State machine (locked)**:

- `TaskState::Unfulfillable { reason }` — discrete enum variant,
  distinct from generic `TaskState::Failed`. Discriminant-based
  dispatch (no string-parsing on reasons).
- `TaskState::Blocked { on: TaskHash }` — discrete variant for
  cascade-failed dependents. When `T` enters `Unfulfillable`,
  dependents move to `Blocked { on: T }` (NOT `Failed{NonRecoverable}`).
- `TaskCompleted(T)` apply rule walks `dependents_of[T]` and
  transitions every `Blocked { on: T }` dependent to `Pending` —
  event-driven, not retry-pass wall-clock.
- `ErrorType::Unfulfillable { reason: BoundedString<2048> }` on the
  wire (in `TaskFailed` mutations) maps to `TaskState::Unfulfillable`
  in the apply rule.

**Single counter per state transition** (shared between
consumer-explicit `ReinjectTask` and matcher-auto-fire; both target
the same `Unfulfillable → Pending` transition). Separate untouched
retry-pass budget for `Errored → Pending`.

- Primary API: `primary.fail_permanent(task_hash, ErrorType::Unfulfillable { reason })`.
- Framework auto-cascades: every dependent variant gets
  `Failed{NonRecoverable}` with reason `upstream-failed`. We do NOT
  walk dependents ourselves.
- Recovery path: `PrimaryCommand::ReinjectTask(hash)` transitions
  `Failed{NonRecoverable, Unfulfillable} → Pending`. Charged against
  `unfulfillable_reinject_remaining[hash]` counter.
- **API tightening**: `ReinjectTask(hash)` returns oneshot Err if
  task is not in `Failed{NonRecoverable, Unfulfillable}`. Cannot be
  used to unwind a permanent non-resource error. **If we want
  retry semantics, we MUST use `ErrorType::Errored` (recoverable) at
  fail time** — don't try to re-classify a non-recoverable failure
  via reinject.
- Matcher state filter (Q3): matcher fires ONLY on
  `Failed{Unfulfillable}`, not broader `Failed{NonRecoverable}` class.
- Tuning surfaces:
  - CLI flag `--unfulfillable-reinject-max-per-task=N`.
  - PyO3 setter `primary_handle.set_unfulfillable_reinject_max_per_task(Option<u32>)`
    callable before `.run()`.
  - **Default is `None` (unbounded)**. Framework provides primitive;
    consumer caps it from our side if we have a flap-tolerance
    threshold.
- Each fire emits a structured log event — surface at INFO in our
  own logs so operators see flap rates even unbounded.
- Retry-pass budget for `Errored → Pending` transitions is untouched
  by Unfulfillable resolution.

**Matcher contract (locked, lands in Batch E3)**:

- Signature: `Fn(TaskInfoView, holdings: HashMap<PeerId, HashSet<String>>) -> bool`.
- `TaskInfoView` exposes at minimum `path`, `task_hash`, `reason`
  (read-only borrow).
- Invoked **once per `Failed{Unfulfillable}` task per batch of
  holdings changes**. Concurrent `PeerResourceHoldingsUpdated`
  mutations applied in the same dispatcher tick are coalesced
  before matchers fire.
- State filter is strict: `task.state == TaskState::Unfulfillable`
  only. No fires for `Running` / `Pending` / other `Failed` variants —
  suppressed at dispatcher level (no log noise on flap).
- Exception handling: `log.warn` + skip the affected task, continue
  with others in the batch. Matcher itself is not disabled by a
  per-task exception.

**Implementation sequencing**:
1. **Batch C (in flight)** — wire pieces:
   - `ErrorType::Unfulfillable { reason: BoundedString<2048> }`
   - `PrimaryHandle` command channel with `FailPermanent`,
     `ReinjectTask`, `UpdatePreferredSecondaries`.
   - INTERIM: framework treats task as generic `Failed{NonRecoverable}`;
     `ReinjectTask` accepts any `Failed{NonRecoverable}`; dependents
     go to `Failed` (no Blocked yet).
2. **State-machine follow-up (next morning, 2026-05-16)** —
   discrete `TaskState::Unfulfillable` + `TaskState::Blocked { on }`
   + cascade-resume on `TaskCompleted` apply rule.
3. **Batch E3 (matcher hook)** — depends on (2).

Consumer wire-in (our side) **deferred** until all three are merged.
Single consolidated commit will land:
- repair-worker `fail_permanent(hash, Unfulfillable)` callsite
- ReinjectTask plumbing through ReplicationContext
- matcher registration + holding_matcher.py
- peer_lifecycle_listener wiring (Q4)

### Q2 — preferred_secondaries: runtime API + soft + CONSUMER MUST BATCH

- Primary API: `primary.update_preferred_secondaries(task_hash, Vec<secondary_id>)`.
- Wire: `ClusterMutation::TaskPreferredSecondariesUpdated`.
- Semantics: **soft only**. Scheduler prefers listed secondaries
  when free, falls back to any. Never blocks dispatch.
- **CRITICAL — consumer must batch.** Framework does NOT debounce.
  Fire ONE update per toolchain with the full converged holder set,
  triggered ~5 s post-`TaskCompleted` (after K=3 cascade settles —
  our `peer_push` protocol is the source of truth for "all K
  replicas confirmed"). This saves K-1 mutations per outpath and
  collapses K × dep_count fanout to dep_count per toolchain.
- Static `TaskInfo.preferred_secondaries` field may also exist as a
  seed-only channel (we already emit it; see P2 polish for primary
  pre-population).

### Q3 — ObserverHasPaths: outpath-blind + consumer matcher hook

- Wire: `ClusterMutation::PeerResourceHoldingsUpdated` and
  `ClusterMutation::PeerJoined`. Framework stores opaque
  `HashSet<String>` per peer; never parses path semantics.
- Snapshot envelope carries `peer_holdings: HashMap<PeerId, HashSet<String>>`
  so cross-primary handoff survives via observer announcer task
  re-broadcasting on `PrimaryChanged`.
- Consumer-supplied matcher: `Fn(failed_task, holdings) -> bool`.
  Framework invokes on every holdings-change event.
- **State filter (confirmed)**: matcher fires ONLY on tasks in
  `Failed{NonRecoverable}` (via `fail_permanent`). Cascade-fail
  dependents (`upstream-failed`) are filtered at the dispatcher;
  they cannot match against holdings.
- On matcher-true: framework auto-fires
  `PrimaryCommand::ReinjectTask(hash)`. Charges the
  `unfulfillable_reinject_max_per_task` counter (pending
  round-trip 3 confirmation; per-task-hash monotonic).
- Cascade-dependent recovery is automatic: matcher-true on the
  ROOT task → root reinjects → root completes → cascade-fail
  dependents auto-clear via existing retry-pass. Don't try to match
  dependents directly.

### Q4 — peer_lifecycle_listener: constructor-kwarg + duck-typed methods

- Constructor kwarg on every manager pyclass:
  `peer_lifecycle_listener=<obj>`.
- Duck-typed methods (missing → log + skip):
  - `on_peer_removed(secondary_id: str, cause: dict)`.
  - `on_peer_added(secondary_id: str)`.
- `cause` is structured:
  - `{"kind": "KeepaliveMiss"}`
  - `{"kind": "MassDeathEscalation"}`
  - `{"kind": "FatalError", "reason": "<truncated to 1024 bytes>"}`
- Constructor-only registration (no setter) so setup-phase events
  fire correctly.
- Dispatched off the apply path via dedicated dispatcher task —
  our callback latency does not block CRDT throughput.
- **Identity model**: respawn allocates a fresh monotonic
  `secondary-N` id; dead id stays dead forever. No `-r<N>` suffix.
  `on_peer_added` fires for both initial setup AND post-respawn.

---

## Re-plan per upstream release (consumer wire-in)

Plan blocks below are ordered by upstream's announced sequencing:
Q1 first (Phase 0b primitives in flight), then Q4+Q3 together
(batch D+E), then Q2 (Phase 4, gated on Q3 wire).

### When Q1 ships (`fail_permanent` API + Unfulfillable counter)

Lands in **Batch C** (task #98 = C1, `PrimaryHandle` + Phase 0b
primitives); queued behind Batches A and B currently in flight.

**Work**:

1. **Add `mark_task_unfulfillable: Callable[[str, str], None]` to
   `ReplicationContext`** (default `None` for fallback).
   - In `peer_replication.ReplicationRepairWorker`, after a repair
     `push_attempt` returns with 0 candidates AND we believe primary
     is disconnected (or the toolchain holder count is 0 globally),
     call `mark_task_unfulfillable(task_hash, reason_str)`.
   - `reason_str` shape: `f"toolchain outpath={outpath} dead_holders={sorted(last_known)}"`.
     Format must be Q3-matcher-parseable (outpaths extracted at
     matcher fire time).
   - File: `python/compiler_suit_runner/peer_replication.py`.

2. **Bind the callable through `suit_task.on_run_start`** so it
   resolves to `primary.fail_permanent(hash, ErrorType::Unfulfillable {
   reason })` on the primary's coordinator handle (no-op on
   secondary-side managers; only the primary owns the ledger).
   - File: `python/compiler_suit_runner/suit_task.py`.
   - **Critical**: when binding, ensure we only ever pass
     `ErrorType::Unfulfillable` here — never `Errored`. The two
     ErrorType variants are not interchangeable; `Unfulfillable` is
     for resource-availability, `Errored` is for retry-pass-eligible
     execution failures. Our toolchain-missing case is canonically
     Unfulfillable.

3. **Surface `--unfulfillable-reinject-max-per-task=N`** through
   `SuitTaskConfig` (default `None` = unbounded, matching framework
   default). Apply via
   `primary_handle.set_unfulfillable_reinject_max_per_task(Option<u32>)`
   at coordinator construction.
   - File: `python/compiler_suit_runner/cli.py` (flag),
     `python/compiler_suit_runner/suit_task.py` (apply).
   - **Do NOT** also surface `--error-retry-max-per-task` here —
     that's the existing retry-pass budget; orthogonal to this work.

4. **Surface flap-rate signal**. Subscribe to framework's structured
   log events for reinject fires; surface in our own logs at INFO so
   operators see flap rates even at unbounded default.

5. **Map toolchain task_hash from outpath**. The repair worker
   currently works in outpath-space; needs a `outpath →
   toolchain_task_hash` lookup. Wire via a new
   `ReplicationContext.lookup_task_hash_for_outpath` callable
   populated at `on_run_start` from the manifest's toolchain
   `TaskInfo` records.

6. **Test**: extend `test_t11_k3_replication.py` (or new T13) —
   kill ALL secondary holders + verify `fail_permanent(Unfulfillable)`
   fires + verify cascade-fail propagates to dependent variants
   (count via `outcome_counts.fail_final`). Verify
   `Failed{Unfulfillable} → Pending` transition counter increments
   on `ReinjectTask` and that `ReinjectTask` on a non-Unfulfillable
   `Failed{NonRecoverable}` task returns oneshot Err (negative-path).

### When Q4 + Q3 ship (batch D+E together)

These land in the same upstream batch, so wire them together.

**Work — Q4 first** (peer death detection):

1. **Construct the listener object** in `suit_task.on_run_start`.
   Pass as `peer_lifecycle_listener=<obj>` kwarg to every manager
   pyclass instantiation we control.
   - The object exposes `on_peer_removed` (routes to
     `ReplicationRepairWorker.on_peer_removed`) and
     `on_peer_added` (routes to a new `register_observer_paths` if
     it turns out an observer is joining, otherwise NO-OP).
   - File: `python/compiler_suit_runner/suit_task.py` — add
     `_PeerLifecycleListener` class with the duck-typed surface.

2. **Demote the placement-watcher diff-callback to backstop**: keep
   it registered but log at INFO when it catches a death (so we can
   tell whether Q4 hook or NFS-poll caught it first). The diff
   path still triggers cascade-on-addition (we need that for the
   subprocess flow until P1 lands).
   - File: `python/compiler_suit_runner/peer_replication.py`
     `ReplicationRepairWorker.on_diff`.

3. **Map `cause` to behaviour**: `KeepaliveMiss` and
   `MassDeathEscalation` → standard repair. `FatalError` → repair
   AND log the reason; if all toolchain holders die with FatalError,
   include that in the Q1 `fail_permanent` reason string.

**Work — Q3 next** (observer-driven recovery):

4. **Implement the matcher hook**. Consumer-side `Fn(failed_task,
   holdings) -> bool`:

   ```python
   def matcher(failed_task, holdings):
       outpaths = extract_outpaths_from_unfulfillable_reason(failed_task.reason)
       return any(op in holdings for op in outpaths)
   ```

   - Outpath extraction parses the structured reason string emitted
     by step 1 of Q1 wire-in.
   - Register via the framework API (TBD — pyclass constructor
     kwarg, similar shape to Q4).
   - File: `python/compiler_suit_runner/peer_replication.py` or
     new `python/compiler_suit_runner/holding_matcher.py`.

5. **Observer-side emitter**. When a local observer attaches via
   `--observer-join-from-peer-info-dir`, enumerate the toolchain
   outpaths in its local store (via `nix path-info --json` on the
   persisted toolchain-drvs list) and publish via the framework's
   `PeerResourceHoldingsUpdated` mutation API.
   - File: new `python/compiler_suit_runner/observer.py` (or
     extend `cli.py` observer subcommand). Reuse
     `peer_paths_fetch.is_path_locally_valid` for store check.

6. **`fetch_from_peer` integration**: observer's harmonia listener
   URL becomes a fetch target once registered in the placement
   map. The matcher-true → auto-reinject path means the
   re-injected task lands on a secondary which then fetches from
   the observer.
   - File: `peer_paths_fetch.py` — observer registered as a normal
     peer; no special-casing needed if its URL is added to the
     peer list at registration time.

7. **Test (T13)**: full disaster-recovery — kill all 4 holders,
   `fail_permanent` fires (Q1 wire-in), attach observer with
   toolchain pre-realised, matcher returns true on the toolchain
   task, framework auto-reinjects, secondary fetches from observer,
   cascade-fail dependents auto-clear.

### When Q2 ships (`update_preferred_secondaries` runtime API)

Phase 4 in upstream sequencing; gated on Q3 wire being in place.

**Work**:

1. **Implement the batched updater**. After K=3 cascade settles for
   a toolchain (signal: `ReplicationSender.on_path_have` for that
   outpath fires with `len(holders) >= K`), wait additional debounce
   (~2 s) for any K+1 race, then fire ONE
   `primary.update_preferred_secondaries(toolchain_task_hash,
   converged_holders)` per toolchain.
   - File: `python/compiler_suit_runner/peer_replication.py`
     `ReplicationSender` — add `_settle_timer` per outpath and
     `_on_settle` handler that calls the bound updater.
   - The updater callable is bound through `ReplicationContext`,
     analogous to `mark_task_unfulfillable` (Q1).

2. **Pre-populate static field via P2** (already planned; see
   below). `preferred_secondaries` on the variant `TaskInfo`
   becomes the SEED preference for first-dispatch decisions; the
   runtime API takes over after cascade settles.

3. **Test (T14)**: assert that variant tasks land on
   toolchain-holders ≥ 90 % of the time. Requires a small workload
   (~100 variants) and K=3 already converged.

---

## Pure consumer-side polish (no upstream dependency)

### P1 — Localhost cascade wake (latency optimisation)

Build worker subprocess writes its placement file but can't directly
wake the manager's placement watcher; cascade fires only on next
tick (5–60 s). Use Approach B — build_worker POSTs the standard
`path-have` event to its OWN manager's listener; manager's
`_on_path_have` handler detects "my own secondary_id" and runs
cascade with item_class from the wire record.

- File: `python/compiler_suit_runner/peer_paths.py` (optional
  self-push), `python/compiler_suit_runner/suit_task.py`
  (handler tweak).

### P2 — `query_initial_toolchain_placement` includes submitter

Today returns `{op: []}`. Detect when the primary's local store has
the outpath and seed `["submitter"]` so the variant `TaskInfo`'s
`preferred_secondaries` seed (Q2 wire-in step 2) has a valid
starting preference.

- File: `python/compiler_suit_runner/preflight.py`. Effort: trivial.

### P3 — Sticky rejection window in `ReplicationSender`

Today, on `path-reject`, the sender immediately re-attempts and can
re-pick the same rejecter. For `disk-full`/`already-have` rejects
this wastes a round-trip. Track per-`(outpath, target_sid)` reject
timestamps; skip candidates whose reject is < 30 s old. Clear on
cluster-wide convergence.

- File: `python/compiler_suit_runner/peer_replication.py`.

### P4 — Death-test infrastructure

Injection helper that (1) waits for placement to converge to K=3,
(2) `scancel`s one holder's SLURM job, (3) re-checks placement
after a bounded window. Blocked on Q4 for the convergence window
to be sub-tick.

- File: new
  `python/compiler_suit_runner/tests/slurm/reproducers/inject_holder_death.py`.

### P5 — Bounded peer-push retry on reply path

`push_to_peer` returns `False` on any transport error. For
path-accept/reject replies a transient drop leads to spurious 1 s
sender timeouts. Add one retry with 100 ms backoff for accept/reject
ONLY; offers stay one-shot.

- File: `python/compiler_suit_runner/peer_replication.py`.

---

## Out of scope (still deferred)

- **K parameter tuning**. K=3 is hardcoded; revisit on disk
  pressure or replication-storm symptoms.
- **Common-dep replication**. K=3 applies to toolchains only.
- **Framework-side path replication**. Consumer owns replication
  policy; framework owns task state + scheduling. Hard line.
- **Observer mid-run path-discovery**. Q3's join-time announcement
  is the contract; mid-run gains are stretch.
- **Cross-cluster federation**. K applies to a single cluster.
- **TaskInfo static `preferred_secondaries` field** as the PRIMARY
  scheduling signal. With Q2 locked as runtime API, the static
  field is a seed-only channel; do not over-invest in it.

---

## File map (where to look when each ask lands)

| Upstream feature                | Consumer wire-in point                                          |
|--------------------------------|------------------------------------------------------------------|
| Q1 — `fail_permanent`          | `peer_replication.py` (Repair callsite + ctx), `suit_task.py` (binding), `cli.py` (`--reinject-max-per-task`) |
| Q2 — runtime preferred update  | `peer_replication.py` `ReplicationSender` (batched settle timer + updater); P2 in `preflight.py` for seed |
| Q3 — outpath-blind matcher     | new `holding_matcher.py` (matcher fn), new `observer.py` (emitter), `peer_paths_fetch.py` (observer-as-peer) |
| Q4 — peer_lifecycle_listener   | `suit_task.py` (`_PeerLifecycleListener` class + ctor kwarg)    |
| Local: P1 (localhost wake)     | `peer_paths.py` + `suit_task.py`'s `_on_path_have`               |
| Local: P3 (sticky rejects)     | `peer_replication.py` `ReplicationSender`                       |
| Local: P4 (death-test injector)| new `tests/slurm/reproducers/inject_holder_death.py`            |
| Local: P5 (retry on reply)     | `peer_replication.py`                                           |

---

## Verification once everything lands

Re-run the slurm-test-env integration suite end-to-end:

1. **T03/T07 baseline** pass — no regression in single- or
   4-secondary runs.
2. **T11 happy-path**: ≥ K=3 holders post-run.
3. **T12-death-1**: kill one holder; repair-on-death restores
   K=3 within < 5 s of `scancel` (Q4 latency).
4. **T12-death-2**: kill two holders simultaneously; placement
   converges at 3 or 4 holders (race tolerance), no infinite
   handshake loops.
5. **T13-unfulfillable+observer**: kill ALL holders + primary
   disconnect; verify `Failed{NonRecoverable}` ledger transition
   (Q1) + cascade-fail to dependents. Attach observer with
   toolchain in local store; verify matcher-true → auto-reinject
   → cascade clear (Q3).
6. **T14-affinity**: with `update_preferred_secondaries` honoured
   (Q2), variant tasks land on toolchain-holders ≥ 90 % of the
   time. Verify ONE update per toolchain (not K × dep_count).

T03/T07/T11 already exist; T12/T13/T14 are new tests under
`python/compiler_suit_runner/tests/slurm/`.

---

# Part B — Distributed evaluation pipeline (NEW)

## IMMEDIATE SCOPE (2026-05-16) — template-graph algorithm only

Everything below — Phase 0 redesign, file deltas for `preflight.py` /
`manifest_gen.py` / `eval_worker.py` / `cli.py` / submitter wiring /
test rewrites / verification — is **deferred** until the template-
graph algorithm is implemented and validated.

**Active deliverable:** a standalone, locally-testable Python module
implementing the algorithm described under **Phase 1 (corrected)**
below. Hard asserts stay in (debugging signal). No integration with
`suit_task.py` / framework / cluster dispatch yet.

**Files in scope:**

- **NEW `python/compiler_suit_runner/template_graph.py`** —
  self-contained module exposing the algorithm. Pure functions only;
  no imports from `suit_task.py`, `peer_*`, framework, or any cluster-
  side dependency. Imports allowed: standard library +
  `drv_graph.read_full_drv_closure` (the only outside touchpoint, and
  itself self-contained since it just shells out to
  `nix show-derivation --recursive`).
  
  Public surface:
  - `Template` and `TemplateNode` dataclasses.
  - `VariantArray` dataclass.
  - `build_template_from_closure(root_drv, closure, toolchain_drvs)
     -> (Template, root_id)`.
  - `find_or_register_template(templates, candidate) -> int` (id).
  - `cowalk_and_index(template, root_id, drv_path, closure, arr,
     variant_index, toolchain_drvs)` — populates `arr.hashes` in
     place; raises on any assertion failure.
  - `assert_arch_invariants(arr, template) -> dict` returning
    `{node_id: "common_dep" | "variant_specific"}` (raises on any
    invariant break).
  - `plan_phase1_graph(variants_by_binary_arch, toolchain_drvs)
     -> {templates, variant_arrays, placement, common_deps_per_arch}`
     — orchestrates: build template per `(binary, arch)`, reuse where
     shape matches, cowalk all variants, assert invariants. **Does
     not** call `spawn_tasks` or talk to the framework. Returns the
     fully-populated in-memory result.

- **NEW `python/compiler_suit_runner/tests/test_template_graph.py`** —
  unit tests using **manually constructed** .drv files as fixtures.
  Tests exercise the algorithm end-to-end without nix-show-derivation
  in the loop (the closure dict is constructed by hand). At least one
  golden-fixture test that DOES invoke `read_full_drv_closure` against
  a small handful of real .drv files committed under
  `tests/fixtures/drvs/` (or generated by a tiny `nix eval` step in a
  fixture-prep script). Cases to cover are the same as the
  `test_phase1_planner.py` rewrite list in the **Redesign delta**
  section below — but ported to call `template_graph.*` directly
  rather than `plan_phase1`.

- **(MAYBE) extend `drv_graph.py`** with `read_full_drv_closure` if
  not already present. Already listed in **Redesign delta** under
  "Phase 1 side". Lift this single helper out of the deferred work
  because the algorithm needs it.

**Out of scope for this iteration:**

- `phase1_planner.py` rewrite (its `plan_phase1` orchestration that
  calls `spawn_tasks` stays untouched until the algorithm is proven).
- `eval_worker.py` redesign (no drvs-in-out, no symlink emission yet).
- `preflight.py` / `manifest_gen.py` / `cli.py` payload-shape edits.
- Submitter, framework, test_t20/21/22/23 changes.
- Reverting `9e17d1d` (the background-agent pre-sampling commit).

The point: build + test the algorithm in isolation, on file-system
fixtures the user can inspect by hand. Once it's right, wire it into
the cluster pipeline. The detailed designs for those next steps are
captured below for continuity but are not the active TODO.

## REDESIGN 2026-05-16 — Phase 0 architecture corrected

The original Phase 0 design (sections below, pre-redesign) drifted from
the user's stated intent during implementation. Two specific drifts and
their corrections, recovered verbatim from the original plan-creation
messages and the latest user feedback:

1. **Submitter was pre-expanding the cartesian product of every variant
   suffix into the manifest payload (~8.6 MB per binary).** User intent:
   submitter sends only `{binary, sys, archs, sample_size, seed}`
   (~200 B per binary). All cartesian-product expansion, filtering, and
   sampling happens on the cluster worker.

2. **Worker was running one `nix-eval-jobs` call per `(binary, arch)`
   pair (per-arch loop in `run_eval_task`).** User intent (verbatim):
   *"i said one eval per binary, not one eval per compiler variant of a
   binary!"* — ONE `nix-eval-jobs` call per binary covering ALL archs.
   The flake fragment `.#dataset.<sys>.<binary>` has shape
   `{arch: {suffix: variant}}` — nix-eval-jobs walks recursive attrsets
   and emits one JSON record per leaf, so one invocation suffices.

3. **Worker was writing `out/<binary>/_phase0/manifest.json` as the
   Phase 0 output marker.** User intent (verbatim):
   *"the drv files for a matrix should be placed in the out. you
   somehow came up with manifest files that i have never talked about
   ... these manifest files are completely 100% unneeded. the drv
   contains the whole graph for all variants we want to build. from
   that we can build the in memory tasks and find the common
   dependencies and build the graph"* — Phase 0's NFS output is the
   set of `.drv` symlinks in `out/<binary>/`, nothing else. Phase 1
   walks `out/*/*.drv` directly via `nix show-derivation` and builds
   the graph in-memory; no intermediate JSON.

The redesign is documented in the **Phase 0 (corrected)** and **Phase 1
(corrected)** sections below, which supersede the original Phase 0/1
sections. Open design points #1 (per-arch granularity) and #4 (drv
cycle check) carry over; #5 (Phase 0 task failure) carries over.

## Context

Today the local submitter does ALL of the eval work before any sbatch
jobs fire:

1. `enumerate_variants` — per-`(pkg, arch)` `nix eval _meta.<sys>.<pkg>.<arch>` + `nix-eval-jobs` to instantiate sampled drvs (preflight.py:609-816).
2. `enumerate_toolchains` + `eval_toolchain_drvs` — resolve drv paths for the 317-toolchain set (preflight.py:824-999).
3. `check_toolchains_locally` + `build_toolchains_locally` — 317 sequential `nix path-info` probes + serialised local build of any missing outputs (preflight.py:1142-1233).
4. `emit_all_manifests` — one monolithic `ManifestSet` covering every phase (manifest_gen.py:539-600), shipped to the framework in one call.

This works for one small package but scales poorly:

- For ~100 packages the local eval alone is 1–3 hours wall-clock on a 16-core submitter, all bottlenecked by single-threaded `nix-eval-jobs` worker processes.
- The `nix path-info` per-drv subprocess loop is O(n) with subprocess startup overhead per drv — a few minutes on its own for 317 toolchains.
- A failed run cannot resume eval: every restart re-evaluates from scratch.
- The submitter has to be online for the whole eval window; disconnecting kills the run before any sbatch jobs even land.

The new architecture pushes eval to the cluster as task work and keeps the submitter to a thin bootstrap role:

- **Phase -1 (bootstrap):** submitter eval's the **toolchain drv set only** (cheap, ~5 s), pushes those `.drv` files to one secondary, that secondary cascades the drvs to ALL peers via a broadcast primitive (size-based: drvs are 10–50 KB, so flood-fill is cheaper than K=3). In parallel, submitter starts pushing the realised toolchain **outputs** (which are large) via the existing K=3 path-offer mechanism so by the time eval finishes the outputs are pre-cached.
- **Phase 0 (distributed eval):** one task per **binary** (e.g., `hello`, `busybox`, `zlib`, `openssh`). Each task runs ONE `nix-eval-jobs --flake .#dataset.<sys>.<binary>` invocation (no arch suffix; nix-eval-jobs walks the `{arch: {suffix: variant}}` attrset and emits one JSON record per leaf), produces all kept variant `.drv` files in the local /nix/store, **broadcasts** each drv to all peers, and symlinks them under `<shared_fs>/out/<binary>/<arch>__<suffix>.drv`. No JSON manifest is written; the symlink directory IS the resume marker.
- **Phase 1 (NEW: primary computes graph):** a promoted secondary (the primary) globs `<shared_fs>/out/*/*.drv`, runs `nix show-derivation --recursive` on each variant entry-point .drv to walk the full build graph, identifies **common deps** (any drv shared by ≥ 2 variants — refcount ≥ 2, NOT the current refcount ≥ 10 threshold), and emits Phase 1 TaskInfo set with dependency wiring via the new framework primitive (Q5).
- **Phases 2+ (build):** existing variant build flow runs unchanged; deps from Phase 1's graph guarantee correct ordering.

## Status (2026-05-15)

**Phase status:** all design; no implementation has started. Today's
state is the legacy local-eval flow which served us through hello/busybox
testing but won't scale to 100s of packages.

**Framework ask:** one new ask (Q5) needs to land before Part B can
fully ship; the rest is consumer-side work. Filed as part of this
plan.

## Locked-or-proposed framework contract

### Q5 (NEW) — Dynamic task injection at phase boundary

**Today** the framework loads a single static `ManifestSet` at `coord.run()`
init (cli.py:1284) and has no API to inject new tasks after that point.
Phase 1 task graph depends on Phase 0 outputs, so we need runtime
injection.

**Ask:**

- Primary API: `primary.spawn_tasks(tasks: Vec<TaskInfo>) -> Result<()>`.
  - Each `TaskInfo` carries its `task_depends_on` referencing earlier task hashes (Phase 0 task hashes or other spawned-task hashes).
  - Framework integrates spawned tasks into the existing DAG, fires scheduler.
  - Idempotent if the same `task_hash` is spawned twice (no-op the second).
- Wire: `ClusterMutation::TasksSpawned { tasks }`.
- Trigger: callable from primary at any point during `coord.run()`. No phase-boundary semantics encoded in the framework; consumer decides when to call.
- Concurrency: must be safe to call from a non-framework thread (e.g., a `Phase 0 quiesce` callback running in Python).

**State machine interaction:**

- Spawned tasks enter as `Pending` (or `Blocked { on }` if their deps aren't all `Completed` yet).
- They participate in cascade-fail (Part A Q1) and re-injection (Part A Q3) just like submit-time tasks.

**Consumer use:** at Phase 0 quiesce (all `phase0_eval_*` tasks `Completed`), our promoted-primary calls `primary.spawn_tasks(...)` with the Phase 1 set + dep graph. After that, scheduling proceeds normally.

**Smaller alternatives we considered:**

- *Static all-the-way (no Q5)*: Phase 1 task set would have to be enumerated at submit time, which requires knowing every variant's drvs — defeats the purpose.
- *Two `coord.run()` calls*: rejected per memory note `dynamic_batch framework constraints` — `RustPrimaryCoordinator.run(binaries)` is single-use.
- *Pre-allocate a "max tasks" placeholder pool*: ugly, requires knowing an upper bound, leaks task slots when actual count is less.

Q5 is the cleanest shape.

## Architecture detail per phase

### Phase -1 — bootstrap (submitter-driven)

Done locally on the submitter before any cluster work fires.

**Steps:**

1. Eval toolchain drvs locally — `nix-eval-jobs _crossToolchainMap.<sys>` (preflight.py:824-908, already implemented). Output: 317 `.drv` paths.
2. Eval matrix `_meta` per package — `nix eval _meta.<sys>.<pkg>` for each package on user's list. Cheap (≪ 1 s per package). Gives the per-package suffix list so Phase 0 tasks know what they're producing.
3. Push toolchain drvs to first secondary via new `path-broadcast-offer` endpoint (see below). Receiver fan-outs to all peers.
4. **In parallel** with (3): start pushing realised toolchain *outputs* via existing K=3 `ReplicationSender.push_attempt` — the warmup pipeline so by the time Phase 0 finishes, every secondary that will build variants has its toolchain outputs already present.
5. Compute Phase 0 task list — one `TaskInfo` per binary — and submit to framework. These are the only static tasks the submitter creates; everything else is spawned via Q5 at Phase 0 quiesce.

**Submitter role after Phase -1:** continues to serve harmonia federation (existing `SubmitterPeer` role) but does no more dispatch work. A secondary takes over as primary via the existing promoted-primary path (the T1 smoke confirmed this works on the current pin: `this secondary has been promoted to primary epoch=1`).

### Phase 0 (corrected) — distributed eval, one task per binary

One framework task per binary, with a payload-tiny manifest header and
a drvs-only NFS output.

**Submitter contract (Phase -1 emission, per binary):**

The submitter emits one `phase0_eval__<binary>.json` header with payload:

```json
{
  "binary": "hello",
  "sys": "x86_64-linux",
  "archs": ["x86_64-linux", "aarch64-linux", "armv7l-linux", "ppc64-linux", "riscv64-linux", "i686-linux"],
  "sample_size": 2,
  "seed": "42"
}
```

~200 B per binary. No `suffixes` field, no `attr` field, no per-arch
cartesian expansion. The `archs` list is the active subset from
`architectures.nix` — config-level data only.

**Worker action** (one task per binary, runs on assigned secondary):

```python
def run_eval_task(payload, out_dir, broadcast_sender) -> dict:
    binary = payload["binary"]; sys_name = payload["sys"]
    archs = payload["archs"]; sample_size = payload["sample_size"]
    seed = payload["seed"]
    binary_out = out_dir / binary

    # 1. Resume short-circuit (no JSON marker — directory IS the marker)
    if binary_out.exists() and any(binary_out.glob("*.drv")):
        return {"status": "resumed", "drv_count": <count>}

    # 2. Cheap metadata read — ONE `nix eval` for the whole binary
    meta = nix_eval_json(f".#_meta.{sys_name}.{binary}")
    # → {arch: {suffix: {compiler, compilerFamily, optimization, hardening, sanitizer, ...}}}

    # 3. Build keep-set in Python (filtering + sampling, all on cluster)
    keep = build_keep_set(
        meta=meta, archs=archs, sample_size=sample_size, seed=seed,
        support_table=load_support_table(),
        is_known_bad_combo=is_known_bad_combo,
    )
    # → {arch: set[suffix]}  restricted to surviving variants

    # 4. ONE nix-eval-jobs call for the whole binary
    #    The flake fragment .#dataset.<sys>.<binary> evaluates to
    #    {arch: {suffix: variant}}; nix-eval-jobs walks that 2-level
    #    nested attrset and emits one JSON record per (arch, suffix)
    #    leaf, with attrPath = [arch, suffix] and drvPath populated.
    records = nix_eval_jobs(
        flake_ref=f".#dataset.{sys_name}.{binary}",
        workers=NIX_EVAL_JOBS_WORKERS,
    )

    # 5. Filter records against keep-set in Python; symlink + broadcast
    binary_out.mkdir(parents=True, exist_ok=True)
    for r in records:
        arch, suffix = r.attr_path  # 2 elements: [arch, suffix]
        if suffix in keep.get(arch, set()):
            broadcast_sender.broadcast(r.drv_path)
            link = binary_out / f"{arch}__{suffix}.drv"
            link.symlink_to(r.drv_path)  # /nix/store/<hash>-pkg.drv

    return {"status": "completed", "drv_count": <count of symlinks>}
```

**Trade-off (v1):** no `--select` filter on `nix-eval-jobs`. The worker
evaluates ALL variants of the binary then filters records in Python.
The cost is extra Nix evaluation (no derivation builds — just .drvPath
resolution); the benefit is zero argv-size pressure, no impure-mode
flag, no temp-file management. If profiling shows eval time is the
bottleneck (large packages × many archs), fold the keep-set into a
`--select` lambda — see deferred design point.

**Output to NFS:** `<shared_fs>/out/<binary>/<arch>__<suffix>.drv`
symlinks. Each symlink points to `/nix/store/<hash>-pkgname.drv` on
the worker node; the symlink is visible on every peer (NFS) and
resolves to a valid path on every peer that has received the
broadcast drv. NO `manifest.json`. NO `_phase0/` subdir.

**Resume:** on re-submit, `binary_out` directory existence + non-empty
`*.drv` symlink set is the resume marker. Worker returns `resumed`
without re-evaluating.

**Broadcast policy:** unchanged — drvs are small (10–50 KB), flood-fill
via `/peer/path-broadcast-offer`. Receiver forwards to ALL other peers
(minus sender, minus self).

### Phase 1 (corrected) — template-graph dedup algorithm

Triggered at Phase 0 quiesce. Runs on the promoted primary.

**Key observation that drives the algorithm:** a Nix derivation hash is
content-addressed over its inputs, so the dep set of one variant of
binary B has FUNDAMENTALLY DIFFERENT HASHES from another variant of B
that uses a different toolchain or flags — even though the
underlying packages and the graph SHAPE are identical. Conversely, a
SOURCE drv (e.g. `mirror://gnu/hello-2.12.tar.gz` fetch) has the SAME
hash across all variants because it doesn't depend on the toolchain
at all. The algorithm exploits this: build a *template* of the graph
STRUCTURE once per (arch, binary), index per-variant hashes into
arrays at each template node, and identify common deps by hash
equality across the array.

**Input:** the symlink tree under `<shared_fs>/out/`. Each binary's
subdirectory contains one symlink per kept variant, named
`<arch>__<suffix>.drv`, pointing into `/nix/store/<hash>-pkg.drv`.

**Data structures:**

```python
# A template graph captures the SHAPE (package names + DAG edges) shared
# by a family of variants. Nodes are identified by package name; revisits
# during construction link to the same node (DAG, not tree).
class TemplateNode:
    name: str                          # package name (no hash, no version variability)
    child_ids: list[int]               # ordered children, indices into Template.nodes
    is_toolchain: bool                 # True → don't store hashes (toolchain id'd by (arch, compiler))
    visit_flag: bool                   # per-cowalk; reset before each walk

class Template:
    nodes: list[TemplateNode]          # node[0] is the variant root (binary's top drv)
    name_to_id: dict[str, int]         # name-set used during construction for revisit-detection

# Per (template, arch) group: a flat array of hashes parallel to template.nodes,
# one entry per variant in that arch group.
class VariantArray:
    template_id: int
    arch: str
    variants: list[VariantKey]         # variant_index → (compiler, flags) identifier
    # node_id → list of variant_count hash strings (toolchain nodes carry empty/None)
    hashes: list[list[Optional[str]]]

# Top-level mapping: every variant's drv → which (template, arch) array it lives in.
variant_to_placement: dict[variant_drv_path, (template_id, arch, variant_index)]
```

**Algorithm:**

```python
def plan_phase1(out_dir: Path, toolchain_drvs: set[str],
                spawn_tasks: Callable[[list[TaskInfo]], None]) -> None:
    templates: list[Template] = []
    variant_arrays: dict[(int, str), VariantArray] = {}   # (template_id, arch) → array
    placement: dict[str, tuple[int, str, int]] = {}       # variant_drv → (tmpl_id, arch, idx)

    # Group variants by (binary, arch) per symlink tree
    by_binary_arch: dict[(str, str), list[(suffix, drv_path)]] = ...

    for (binary, arch), variants in by_binary_arch.items():
        # Step 1: derive template from the FIRST variant of this (binary, arch).
        first_suffix, first_drv = variants[0]
        first_closure = read_full_drv_closure(first_drv)  # nix show-derivation --recursive

        template, root_id = build_template_from_closure(
            root_drv=first_drv, closure=first_closure, toolchain_drvs=toolchain_drvs,
        )
        # Try matching against existing templates (cross-binary, cross-arch reuse)
        tmpl_id = find_or_register_template(templates, template)

        # Allocate variant array for this (template, arch)
        array_key = (tmpl_id, arch)
        if array_key not in variant_arrays:
            variant_arrays[array_key] = VariantArray(
                template_id=tmpl_id, arch=arch, variants=[],
                hashes=[[] for _ in templates[tmpl_id].nodes],
            )
        arr = variant_arrays[array_key]

        # Step 2: cowalk every variant of this (binary, arch) against the template
        for suffix, drv_path in variants:
            variant_index = len(arr.variants)
            arr.variants.append((suffix,))
            placement[drv_path] = (tmpl_id, arch, variant_index)

            # Reset visit flags before cowalk
            for n in templates[tmpl_id].nodes: n.visit_flag = False

            cowalk_and_index(
                template=templates[tmpl_id],
                root_id=root_id,
                drv_path=drv_path,
                closure=read_full_drv_closure(drv_path),
                arr=arr, variant_index=variant_index,
                toolchain_drvs=toolchain_drvs,
            )

        # Step 3: at end of arch group, every node's hash[node_id] array is fully populated
        # for this arch. Assert all entries distinct for non-toolchain non-terminal nodes
        # (hard error for now; mismatch likely means template-shape escape that needs
        # promoting to terminal).
        assert_distinct_across_variants(arr, templates[tmpl_id])

    # Step 4: common-dep identification — a node whose hash is identical for ALL
    # variant_indices in arr → "per-arch common dep" (build once per arch, reuse
    # across all suffixes of that arch). A node whose per-variant hash is identical
    # AND that hash also appears for the same logical node across OTHER (template,
    # arch) arrays → "cross-arch common dep" (no, those can't match by definition —
    # arch-specific store paths differ — so only PER-ARCH common deps exist beyond
    # toolchain).
    common_deps_per_arch_template: dict[(tmpl_id, arch, node_id), str_hash] = ...

    # Step 5: emit Phase 1 TaskInfo set:
    #  - one common_dep task per unique (arch, node_id) where the per-variant hash
    #    array is constant (i.e. the node's hash is independent of (compiler, flags)
    #    within the arch group). Task target = that drv.
    #  - one variant task per (binary, arch, suffix). depends_on = the toolchain task
    #    for (arch, compiler) + every common_dep task whose drv appears in this
    #    variant's transitive closure.
    spawn_tasks(build_phase1_taskinfo(templates, variant_arrays, placement,
                                       common_deps_per_arch_template, toolchain_drvs))
```

**Template construction (`build_template_from_closure`):**

Walk the variant root's transitive closure recursively, mapping each
unique package name to a TemplateNode. The "package name" is the drv's
`name` field stripped of version/hash variability (e.g.
`zlib-1.3.1` → `zlib`; toolchain drvs detected by membership in
`toolchain_drvs` get `is_toolchain=True`). During construction, the
`name_to_id` set deduplicates: revisit a name → link to the existing
TemplateNode (DAG semantics). Ordered edges (children list) are
crucial — the cowalk relies on stable ordering of input drvs.

**Co-walk (`cowalk_and_index`):**

Parallel descent of (template node, actual drv) pairs:

- If `template_node.visit_flag` is True → DAG revisit; assert the
  hash we *would* place here matches the hash already stored at
  `arr.hashes[node_id][variant_index]`. (Within-variant DAG-revisit
  safety.)
- Else, set `visit_flag = True`. Then:
  - If `is_toolchain` → skip storing (toolchain identified by `(arch,
    compiler)` lookup; no per-variant slot needed).
  - Else → store `arr.hashes[node_id][variant_index] = drv_hash`,
    recurse paired children.
- Hard error if child counts differ between template and actual drv
  closure — template-shape escape worth a developer's attention
  before silently working around it.

**End-of-arch assertions** (`assert_arch_invariants`):

Once every variant of `(binary, arch)` has been cowalked, for each
non-toolchain node in the template:

- If the node is a **leaf** (no children in the template):
  hashes across variants must be **all equal** — this is by
  definition a per-arch common dep (source fetch / bootstrap drv
  that doesn't take toolchain inputs). Promote to the
  `common_deps_per_arch_template` map. Hard error if not all equal
  (signals template-shape escape).
- If the node is **internal** (has children):
  hashes across variants must be **all distinct** — if any pair
  shares a hash, two variants of different (compiler, flags) ended
  up with byte-identical deps, which is impossible under content-
  addressing unless the children also collapse (in which case THEY
  should have been the matching leaves). Hard error for now;
  promote-to-terminal if a real case shows up.

**Template reuse (`find_or_register_template`):**

For a candidate new template, try cowalk-as-equivalence against each
existing template: walk both in lockstep on the package-name field; if
they match in shape and naming, reuse the existing template (this is
what enables one template across multiple binaries that share the
same dep tree shape, and across archs where the shape happens to
coincide). If no match, register a new template.

**Common-dep semantics:**

A node is a per-arch common dep iff
`all(h == arr.hashes[node_id][0] for h in arr.hashes[node_id])` over
all `variant_index` in the arch's array, AND the node is not a
toolchain. Such a node's drv is built ONCE per arch and shared with
every variant in that arch group. Toolchain leaves are already
broadcast via Part A's K=3 path; not respawned as Phase 1 tasks.

Refcount-≥-2 from the original (drifted) plan is implicitly satisfied
by this algorithm — any node that ends up with the same hash across
2+ variants is a per-arch common dep by construction.

**Drv reading:** `drv_graph.py:read_full_drv_closure(drv_path) -> dict`
uses `nix show-derivation --recursive`, returning the full transitive
closure as a JSON map `drv_path -> {name, inputDrvs, outputs, ...}`.
One subprocess invocation per variant root; nix caches across drvs so
later variants in the same arch group reuse the working set.

**Storage cost:**

For N variants per arch and an arch having T template nodes:
- Old (refcount-union): O(N × T) full drv-path strings.
- New (template arrays): 1 × T template-node objects (with name only)
  + N × T short hash strings.

For hello: T ≈ 50 nodes, N ≈ 100 (sample_size × archs × suffixes after
filtering) → 50 nodes + 5000 short hashes ≈ tens of KB per arch.
Negligible.

## Consumer-side work (Part B)

### New modules

- **`python/compiler_suit_runner/workers/eval_worker.py`** — Phase 0 worker (per-binary eval + drv broadcast + resume marker). Replaces the local eval portion of `preflight.py:enumerate_variants`.
- **`python/compiler_suit_runner/phase1_planner.py`** — Primary-side Phase 1 task graph synthesis from Phase 0 manifests + drv graph walk.
- **`python/compiler_suit_runner/drv_graph.py`** — small helper: `read_input_drvs(drv_path) -> set[str]` via `nix show-derivation`.

### Extended modules

- **`peer_push.py`** — add `/peer/path-broadcast-offer` endpoint + `_BoundHandler` callback + originator helper `push_path_broadcast_offer(target_url, ...)` + fan-out primitive `fan_out_broadcast_drv(peers, drv_path, drv_size)`. Receiver semantics: fetch drv → forward to all OTHER peers (excluding sender) → ack.
- **`peer_replication.py`** — add `BroadcastSender` (or extend `ReplicationSender` with a `broadcast=True` mode). The state machine is simpler than K=3: no K-accounting, no preferred-secondary tracking; just emit to all and track ack count. ~150 LOC.
- **`peer_cache.py`** — `SubmitterPeer` gains a `seed_toolchain_drvs(drv_set: set[str], first_secondary: str) -> None` method called in Phase -1 step 3. Existing harmonia + SSH-R behaviour preserved.
- **`preflight.py`** — split into two surfaces:
  - `enumerate_toolchains_only()` — keeps the existing toolchain enumeration (cheap, runs locally).
  - `enumerate_variants()` — gains a `defer_to_phase0=True` mode that returns just the per-binary metadata (suffix list, sample seed) without forcing drv instantiation.
  - The path-info batching opportunity (one `nix path-info drv1 drv2 ... drv317` call instead of 317 subprocesses) lands here as a side-fix.
- **`manifest_gen.py`** — add `phase0_eval_<binary>` item class. Modify `emit_all_manifests` to support multi-stage emission: Phase -1 + Phase 0 at submit time; Phase 1 + onwards emitted via Q5 by the primary.
- **`suit_task.py`** — register `phase1_planner.plan_phase1` as the Phase 0 quiesce callback. Use the framework's existing `task_completed` hook + a phase-counter to detect "all phase 0 tasks done".
- **`cli.py`** — new flag `--distributed-eval` (default OFF initially; flip to ON once stable). With it on, Phase 0 tasks are dispatched; with it off, current local-eval path runs unchanged. Migration knob.

### Resume support (corrected)

- **Phase 0 resume marker** = the `<shared_fs>/out/<binary>/` directory
  with `*.drv` symlinks. On re-submit, `run_eval_task` returns
  `status="resumed"` if the directory exists and is non-empty. No JSON
  file. The drvs themselves are recovered via the broadcast on first
  run; if a worker lost its /nix/store but the symlinks still resolve
  to other peers' stores, the NFS layer + harmonia federation handles
  fetch-on-demand.
- **Phase 1 resume** = walk the symlink tree again. `plan_phase1` is
  idempotent: spawning a task with an identical `task_hash` is a no-op
  per the Q5 contract. No `_phase1_graph.json` cache file.

### Interaction with Part A (K=3)

Part B reuses Part A's primitives:

- **Toolchain output replication** (Part A K=3): still used; runs in Phase -1 step 4 in parallel with drv broadcast.
- **Variant output replication** (Part A K=3): still used; applies to variants once they're built in Phase 2+.
- **peer_push + handshake protocol** (Part A): the new `path-broadcast-offer` endpoint extends the same auth + threading model.
- **Submitter peer + placement gossip** (Part A): unchanged. SubmitterPeer stays in the cluster mesh as a fetch source.
- **Q1 (Unfulfillable)** — Phase 0 eval tasks can fail-permanently with `ErrorType::Unfulfillable` if the flake source is missing on a secondary. The matcher then auto-reinjects if a peer broadcasts the flake source.
- **Q3 (matcher)** — applies uniformly to Phase 0 and build tasks.
- **Q4 (peer_lifecycle_listener)** — Phase 0 worker deaths get the same `on_peer_removed` signal; Phase 0 tasks become re-injectable.

The K=3 plan's verification tests (T03/T07/T11/T12/T13/T14) need a Phase 0 sibling test set:

- **T20-phase0-happy:** N=4 secondaries, 3 small packages, each Phase 0 task completes; Phase 1 graph contains all 3 binaries' variants.
- **T21-phase0-broadcast:** verify drv files reach all secondaries within X seconds of Phase 0 task completion.
- **T22-phase0-resume:** kill mid-run, re-submit, verify per-binary `out/<binary>/` symlink directories honoured (no re-eval, `status="resumed"` returned).
- **T23-phase1-common-dep:** 2 packages sharing one dep — verify Phase 1 emits exactly one common-dep task and both variants depend on it.

## Open design points (Part B)

These can be deferred until implementation starts; calling them out so they don't get forgotten:

1. **Per-arch granularity inside a Phase 0 task.** RESOLVED per user
   intent: one task per `(binary)`, ONE `nix-eval-jobs` invocation covering
   ALL archs at once (nix-eval-jobs walks the `{arch: {suffix: variant}}`
   nested attrset and emits one record per leaf). Per-arch splitting is
   NOT done. **Deferred sub-question:** if eval time of a single
   `nix-eval-jobs` over all archs is the bottleneck, fold the keep-set
   into a `--select` lambda to skip unsupported arch subtrees in nix —
   defer until profiling justifies the complexity.

2. **`--allow-toolchain-build` semantics under distributed.** With Part B, toolchain *outputs* are no longer built locally — they're built on the cluster as Phase 1 work (already true today as `phase2_toolchain`). The flag becomes "allow secondaries to build missing toolchains from source" with default ON. The local-build path (`build_toolchains_locally`) becomes dead code under `--distributed-eval`.

3. **Submitter exit timing.** Submitter stays alive throughout for harmonia federation. After Phase -1 dispatch, it has no dispatch work — only serves NAR fetches. When all tasks complete, framework signals run end; submitter then shuts down.

4. **Drv-graph cycles / safety check.** When walking `inputDrvs` we trust nix's invariant that drv graphs are DAGs. Add a defensive cycle check in `phase1_planner.plan_phase1` that bails out if a cycle is detected (shouldn't happen, but cheap insurance).

5. **Phase 0 task failure recovery.** If Phase 0 eval of `hello` fails on secondary A (e.g., flake source corruption), it should re-dispatch to secondary B. Existing retry-pass handles this (eval failures are `Errored`, not `Unfulfillable`). Document the distinction in eval_worker so it always emits `ErrorType::Errored` for transient failures.

## Redesign delta — exact file changes to land the corrected Phase 0/1

The implementation that landed in tasks #40–#62 followed the original
(drifted) Phase 0 design and now needs to be reshaped per the
**REDESIGN 2026-05-16** section above. Concrete deltas:

### Submitter side

- **`preflight.py:609–902` `enumerate_variants(defer_to_phase0=True)`**
  — return shape today: `{pkg: {archs, suffixes_by_arch, sample_size, seed, tier}}`.
  Return shape required: `{pkg: {archs, sample_size, seed, tier}}`.
  DROP `suffixes_by_arch` and any pre-sampling pass (lines ~716–772);
  the per-arch suffix lists are NOT to be computed locally.
- **`preflight.py` — extract `support_table` + `is_known_bad_combo`**
  into a sibling module `python/compiler_suit_runner/variant_filters.py`
  so `workers/eval_worker.py` can import them without dragging the
  whole local-eval-path heavy preflight surface. Keep the existing
  preflight names as thin re-exports for the local-eval path.
- **`manifest_gen.py:181–226` `make_phase0_eval_header`** — payload
  fields today: `{binary, sys, archs, suffixes, attr, variant_sample (opt), variant_seed (opt)}`.
  Required payload: `{binary, sys, archs, sample_size, seed}`. DROP
  `suffixes`, DROP `attr` (worker derives both flake refs from
  `(sys, binary)`). Drop optionals; `sample_size` + `seed` are required.
- **`manifest_gen.py:582–633` `emit_phase0_eval_manifests`** — adjust
  for new metadata shape.
- **`cli.py:1106–1121` distributed-eval branch** — the flattening loop
  drops `suffixes_by_arch` handling; produces `{binary: {archs, sample_size, seed}}`.

### Worker side

- **`workers/eval_worker.py:169–218` `parse_payload`** — accept the
  new payload shape (no `suffixes`, no `attr`). Adjust validation.
- **`workers/eval_worker.py:233–304` `_eval_jobs_for_arch`** — REMOVE.
  Replace with `_eval_jobs_for_binary(sys, binary, run_subprocess)`:
  one `nix-eval-jobs --flake .#dataset.<sys>.<binary>` call; emits
  records with `attrPath=[arch, suffix]`.
- **`workers/eval_worker.py:307–361` `_eval_meta_for_arch`** — RENAME
  to `_eval_meta_for_binary(sys, binary, run_subprocess)`: one
  `nix eval --json .#_meta.<sys>.<binary>` call returning
  `{arch: {suffix: meta_entry}}`.
- **`workers/eval_worker.py:114–136` `sample_suffix_attrs`** — KEEP;
  semantics unchanged. Called by the new `build_keep_set` helper to
  sample N per `(compiler, arch, opt)` group.
- **`workers/eval_worker.py:415–590` `run_eval_task`** — rewrite per
  the pseudocode in the Phase 0 (corrected) section above. Specifically:
  - DROP the per-arch loop (lines ~501–532).
  - DROP all writes to `out/<binary>/_phase0/manifest.json`.
  - REPLACE the marker check (lines ~487–491) with directory-exists +
    non-empty `*.drv` glob check on `out/<binary>/`.
  - ADD `build_keep_set(meta, archs, sample_size, seed, ...)` helper
    that applies `variant_filters.support_table` + `is_known_bad_combo`
    + `sample_suffix_attrs` and returns `{arch: set[suffix]}`.
  - ADD symlink emission: `out_dir/<binary>/<arch>__<suffix>.drv → drv_path`.
  - Worker task output dict: `{status, drv_count, broadcast_pending}`
    only — no `variants` list, no `manifest_path`.

### Phase 1 side

- **`phase1_planner.py:79–114` `read_phase0_manifests`** — REPLACE
  with `walk_phase0_symlinks(out_dir) -> dict[(binary, arch), list[(suffix, drv_path)]]`.
  Glob `out/*/*.drv`, resolve each symlink to its `/nix/store/<hash>.drv`
  target, parse the link name for `(arch, suffix)`, group by `(binary, arch)`.
- **`phase1_planner.py:187–331` `plan_phase1`** — full rewrite around
  the template-graph algorithm in the Phase 1 (corrected) section.
  Drop the refcount-union scaffolding. New entry points:
  - `build_template_from_closure(root_drv, closure, toolchain_drvs)` —
    construct a `Template` (DAG of `TemplateNode`s keyed by package
    name) from one variant's transitive closure. Uses a `name_to_id`
    dict during construction so revisits link to the same node.
  - `find_or_register_template(templates, candidate)` — cowalk
    candidate against each existing template on package-name only;
    return the existing id on shape match, else append.
  - `cowalk_and_index(template, root_id, drv_path, closure, arr,
    variant_index, toolchain_drvs)` — parallel descent placing
    `drv_hash` at `arr.hashes[node_id][variant_index]`. Uses
    `template_node.visit_flag` for DAG revisit detection +
    equality-on-revisit assertion. Hard error on child-count
    mismatch.
  - `assert_arch_invariants(arr, template)` — per-node sanity check
    at end-of-arch: non-toolchain leaves must carry **all-equal**
    hashes (→ common dep), non-toolchain internals must carry
    **all-distinct** hashes (→ variant-specific). Hard error on
    template-shape escape.
  - `build_phase1_taskinfo(templates, variant_arrays, placement,
    common_deps_map, toolchain_drvs)` — emit
    `(common_dep_tasks, variant_tasks)` for `spawn_tasks(...)`.
    Variant task `depends_on` = toolchain task for
    `(arch, compiler)` ∪ every common-dep task whose drv appears
    in this variant's closure.
- **`drv_graph.py`** — ADD `read_full_drv_closure(drv_path) -> dict`
  using `nix show-derivation --recursive`. Returns
  `{drv_path: {name, inputDrvs, outputs, ...}}`. The existing
  `read_input_drvs(drv_path)` stays available for callers that only
  need immediate inputs. ADD `derivation_package_name(drv_record)`
  helper to extract the version-stripped package name used as the
  template node key (e.g. `zlib-1.3.1.drv` → `zlib`).
- **`suit_task.py` Phase 0 quiesce watcher** — adapt to the new
  trigger: instead of polling for `out/<binary>/_phase0/manifest.json`
  files, poll for `out/<binary>/` directories with non-empty drv
  symlink set, matched against the expected binary set from the
  Phase 0 task manifests dispatched at submit time.

### Tests

- **`test_preflight.py:757–847`** — update defer_to_phase0 tests for
  new return shape (no `suffixes_by_arch`).
- **`test_manifest_gen.py:633–826`** — update phase0_eval_header tests:
  payload < 1 KB; no `suffixes` field; no `attr` field; required
  `sample_size` + `seed`.
- **`test_eval_worker.py:296–680`** — major rewrite. Mock ONE
  `nix-eval-jobs` invocation (not 6 per binary); mock ONE `_meta`
  read; assert symlinks created under `out/<binary>/`; assert NO
  `manifest.json` written; assert directory-resume semantics.
- **`test_build_worker.py:1096–1289`** — dispatch tests still pass
  with new payload shape.
- **`test_phase1_planner.py:286–331`** — full rewrite. Replace
  `test_read_phase0_manifests_*` with `test_walk_phase0_symlinks_*`
  (glob walk on symlink tree). ADD:
  - `test_build_template_from_closure_dag_revisits_link_same_node`
    (when a package is referenced twice, only one TemplateNode is
    created).
  - `test_build_template_marks_toolchain_leaves` (toolchain drvs from
    a fixture set are flagged `is_toolchain=True`).
  - `test_cowalk_places_hash_at_correct_array_index` (single variant
    pass populates `arr.hashes[node_id][0]`).
  - `test_cowalk_revisit_assertion_passes_on_matching_hash` (DAG
    revisit sets visit_flag once, second visit no-ops only if hash
    matches).
  - `test_cowalk_hard_error_on_child_count_mismatch` (deliberately
    mismatched closure raises).
  - `test_find_or_register_template_reuses_on_shape_match` (two
    variants of same arch produce one template).
  - `test_find_or_register_template_creates_new_on_shape_diff`
    (cross-arch shape divergence forces a second template).
  - `test_common_dep_identified_when_hash_constant_across_array`
    (per-arch common-dep detection).
  - `test_no_common_dep_when_hashes_differ`.
  - `test_phase1_taskinfo_depends_on_toolchain_plus_common_deps`
    (variant task's depends_on covers toolchain + relevant common
    deps only).
  - `test_assert_arch_invariants_leaf_constant_hash_promotes_to_common_dep`
    (non-toolchain leaf with all-equal hashes → recorded as
    common dep).
  - `test_assert_arch_invariants_internal_constant_hash_hard_errors`
    (non-toolchain internal node with constant hash → raises).
  - `test_assert_arch_invariants_leaf_divergent_hash_hard_errors`
    (non-toolchain leaf with non-constant hashes → raises).
- **`test_t20_phase0_happy.py:257–440`** — assertion updates: ONE
  `nix-eval-jobs` spawn per binary; per-binary symlink directories
  exist; no `_phase0/manifest.json` files.
- **`test_t21_phase0_broadcast.py`** — no change (broadcast emits
  one drv per kept variant either way).
- **`test_t22_phase0_resume.py:317–380`** — change resume marker
  assertion from manifest.json existence to symlink directory
  existence.

## File map (Part B additions)

| Area | File | New / extend |
|---|---|---|
| Broadcast primitive | `peer_push.py` | extend (new endpoint + helpers) |
| Broadcast sender | `peer_replication.py` | extend (new sender class) |
| Bootstrap | `peer_cache.py` `SubmitterPeer.seed_toolchain_drvs` | extend |
| Toolchain-only preflight | `preflight.py` | refactor (split) |
| Phase 0 worker | `workers/eval_worker.py` | new |
| Phase 1 planner | `phase1_planner.py` | new |
| Drv graph walk | `drv_graph.py` | new |
| Phase wiring | `suit_task.py` | extend |
| Manifest emission | `manifest_gen.py` | extend |
| CLI flag | `cli.py` `--distributed-eval` | extend |
| Tests | `tests/slurm/test_t20_phase0_*.py` | new |

## Implementation sequencing (Part B)

1. **Q5 framework ask** — file with `dynrunner-owner` early; framework work runs in parallel with consumer work.
2. **`path-broadcast-offer` primitive** — pure peer_push extension, can ship + unit-test independently.
3. **`BroadcastSender` in peer_replication** — extends existing module; can ship + unit-test independently.
4. **`SubmitterPeer.seed_toolchain_drvs`** — wires (2) and (3) into the submitter bootstrap. Stub Phase 0/1 callbacks for now.
5. **`eval_worker` + Phase 0 manifest emission** — depends on (4) being wired.
6. **`phase1_planner` + Q5 plumbing** — gated on Q5 landing; until then a stub returns the legacy local-graph (and the legacy local-eval path runs).
7. **End-to-end integration test T20** — exercises the full chain on slurm-test-env.
8. **Flip `--distributed-eval` default** — only after T20 + T21 + T22 + T23 green.

Consumer-side work is sequenced 1 → 8; framework Q5 lands in parallel between steps 1 and 6. The local-eval path stays default until step 8, so existing dispatches are unaffected by Part B implementation work.

## Verification (Part B)

End-to-end on slurm-test-env (4 secondaries):

1. `compiler_suit_runner submit --distributed-eval --packages hello,busybox` — verify: 2 Phase 0 tasks dispatch, drvs broadcast to all 4 secondaries, Phase 1 emits common-dep + variant graph, builds complete, dataset/ populated for both packages.
2. T20-phase0-happy: 3 packages, no failures, all phases complete in <5 min.
3. T21-phase0-broadcast: drv-broadcast latency < 2 s across 4 secondaries.
4. T22-phase0-resume: kill at Phase 1 start, re-submit, `out/<binary>/` symlink directories honoured (no re-eval), total wall < 30 s vs. fresh run baseline.
5. T23-phase1-common-dep: 2 packages with intentionally-shared dep, one common-dep task emitted, both variants reference it.

LMU validation (after slurm-test-env green): same dispatch shape with 15 secondaries + Krater partition + 5–10 packages including busybox/zlib/openssh.

## Out of scope (Part B)

- **Mixing Part B with Part A's framework asks before they ship.** Part B's Q5 is orthogonal to Q1–Q4; can ship before or after them.
- **Phase 0 distributing across multiple secondaries for ONE package.** Granularity is one task per binary; we don't split a single package's eval across multiple workers. (Nix-eval-jobs internally parallelises across cores within a worker.)
- **Persistent Phase 0 cache across runs without resume marker.** Resume relies on `<shared_fs>/out/<binary>/` symlink directory being intact. No fancy hashing-of-input-state.
- **Framework owning the Phase 0/1 phase concept.** Phase numbering is a consumer convention; framework sees them as tasks with deps.
- **Migrating Part A to Part B's broadcast for toolchain outputs.** Toolchain outputs are large (hundreds of MB); K=3 stays the right policy. Only drvs (small) use broadcast.
