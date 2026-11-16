# Code-quality review: distributed store sharing, ssh_debug_runner, template_graph

Scope:
- `python/compiler_suit_runner/` modules `peer_cache`, `peer_replication`, `peer_push`, `peer_paths`, `peer_paths_fetch`, `cachix_uploader`, `streamed_spawn`, `holding_matcher` and `ssh_debug`, plus their tests and `tests/slurm/`
- `python/ssh_debug_runner/`
- `template_graph/`

Checkout: `/tmp/cq-review/head` at 72dd7be. Paths are relative to the repo root, and `cps/` stands for `python/compiler_suit_runner/`.

## Top 5 issues (ranked)

1. **D4. K=3 replication does not hold K.** The sender ignores `path-accept`, and a 1 s offer timer fires during every real fetch. Each toolchain ends up offered to every peer, and the same peer can fetch the same path twice.
2. **D5. Replication does network and subprocess I/O under its locks and inside HTTP handler threads.** Two nodes wait on each other until their 2 s timeouts expire, and the reject message is lost.
3. **D1/D2/D3. The modules are split by history, not by concept.**
   - `peer_cache.py` holds nine unrelated components.
   - Placement read and write live in different modules that import each other's private names.
   - The whole mesh is assembled by about 560 lines of closures inside `SuitTask.on_run_start`.
4. **T2. template_graph works out a template's binary from the text before the first dash.** As a result `c-ares` is attributed to `c` and `zlib-ng` is merged into `zlib`, so the phase-4 descriptors and meta-templates for those binaries are wrong or empty.
5. **D6 and T1.**
   - D6: broadcast receive fetches inside the HTTP handler, and a failed fetch stays deduped forever.
   - T1: `template_graph` sits at the repo root but is an unpackaged runtime dependency of the runner, and its tests never run by default.

---

## A. Module boundaries and naming

### D1. `peer_cache.py` is a grab-bag of nine components
- Severity: high. Category: separation of concerns.
- Evidence: `cps/peer_cache.py` contains:
  - atomic NFS write helpers (131-192);
  - the cluster signing-key bootstrap (200-315);
  - peer announce and list (323-419);
  - substituter argument assembly (427-476);
  - `PeerListWatcher` (588-714);
  - `HarmoniaProcess` (727-897);
  - nix-daemon start and store-DB registration (905-1186);
  - `PathPlacementWatcher` and `PlacementDiff` (1194-1423);
  - `PeerNixConfWatcher` (1431-1548);
  - `SubmitterPeer` (1556-2374), an SSH client for the gateway with its own harmonia launcher and key generator. It is 820 lines on its own and lazily imports `peer_push` back (2007, 2305) to avoid an import cycle.
- Why it matters: every importer of `PeerInfo` loads 2.4k lines, including ssh_debug_runner and the workers. Unrelated changes collide in one file, and the file name says nothing about most of its contents.
- Fix: split by concept into `nfs_atomic.py`, `peer_discovery.py` (`PeerInfo`, announce/withdraw/list, `PeerListWatcher`, nix.conf and substituter args), `placement.py` (see D2), `local_daemons.py` (nix-daemon, DB registration, harmonia), `cluster_key.py` and `submitter_peer.py`.

### D2. The split into peer_paths, peer_paths_fetch, peer_push and peer_replication follows history
- Severity: medium. Category: naming, cohesion.
- Evidence:
  - The placement writer is in `cps/peer_paths.py:99-182` and imports peer_cache's private `_peers_dir` and `PATHS_FILE_PREFIX` (48-52).
  - The placement reader, `_read_all_placement_files`, and `PathPlacementWatcher` stay in `cps/peer_cache.py:1199-1423`. `cps/workers/build_worker.py:970-975` imports that private reader.
  - `peer_paths_fetch.py` wraps `nix copy`. Its only placement logic is candidate ordering.
  - `peer_push.py` holds the whole peer RPC layer: server, client and 10 message types, half of them not pushes.
  - `peer_replication.py` holds two unrelated protocols, K=3 handshakes and broadcast flood-fill.
  - The item-class constants sit in `peer_paths.py:66-80` "so test code can reference them".
- Why it matters: answering "how does a node learn who holds X" takes three files and private imports.
- Fix:
  - Give `placement.py` the record type, writer, reader, watcher and `PlacementClass` (D11).
  - Rename `peer_paths_fetch` to `store_transfer.py` and `peer_push` to `peer_rpc.py`.
  - Split `peer_replication` into `replication_k.py` and `broadcast.py`, after deleting the dead code listed in D17.

### D3. The peer mesh has no composition root; closures in `SuitTask.on_run_start` wire it together
- Severity: medium. Category: coupling.
- Evidence: `cps/suit_task.py:1961-2523` (about 560 lines) builds:
  - the two watchers, `ReplicationContext`, and the sender, receiver and repair worker;
  - `_PeerLifecycleListener` and `BroadcastReceiver`;
  - `PeerPushServer`, with 8 adapter closures that unpack dicts into positional calls (2251-2321);
  - closures that convert between push URLs and substituter URLs (2185-2215);
  - harmonia, nix-daemon, cachix, `PeerNixConfWatcher` and sshd.

  Each block sits inside `try/except Exception: log; set None`, and later blocks re-check those Nones (2089-2093, 2244-2249). `on_run_end` mirrors it with about 10 hand-ordered try blocks (2524-2655).
- Why it matters:
  - The mesh cannot start without a SuitTask, and nobody can unit-test the wiring.
  - Partial-failure states multiply. For example, the push server can be up while the receiver is None, and then every broadcast is silently rejected.
  - Start and stop order is implicit.
- Fix: add a `PeerMesh` (or `NodeServices`) class with `start(config)` and `stop()`. It owns the components and their order, and uses typed handler protocols (D10) instead of closures. `SuitTask` holds a single reference to it.

## B. Concurrency and protocol correctness

### D4. Accepted K=3 offers time out after 1 s and get re-offered
- Severity: high. Category: correctness, performance.
- Evidence:
  - The timeout is `DEFAULT_OFFER_TIMEOUT_SECONDS = 1.0` (`cps/peer_replication.py:79`). `cps/suit_task.py:2106-2128` does not override it.
  - The timer starts when the offer POST returns (437-443), and `on_accept` does nothing (271-280).
  - `on_timeout` releases the slot and calls `_push_attempt_locked` (296-308). That chooses new candidates, and the timed-out target is eligible again.
  - The receiver accepts a repeat offer from the same sender and starts a second fetch thread for the same outpath (626-638).
  - Tests only cover offers that get no reply (`cps/tests/test_peer_replication.py:255-279`).
- Why it matters:
  - A toolchain `nix copy` takes much longer than 1 s. Each second, every pending accepted offer times out and spawns new offers until no candidates are left.
  - So toolchains spread to all N peers, sometimes with two concurrent fetches on one peer.
  - The settle timer and `update_preferred_secondaries` (468-567) then publish holder sets that have nothing to do with K.
- Fix:
  - Model the slot states explicitly as `OFFERED` and `ACCEPTED`.
  - On accept, replace the 1 s timer with a fetch deadline of minutes, or rely on path-have and peer-removed.
  - The receiver should reject any offer while `_targeting[outpath]` is set, no matter who sent it.
  - Add a test where an accepted offer's fetch is slow.

### D5. Replication does network and subprocess I/O under its locks and inside HTTP handlers
- Severity: high. Category: concurrency.
- Evidence:
  - `ReplicationSender._push_attempt_locked` POSTs offers, with a 2 s timeout each, while holding the sender RLock (`cps/peer_replication.py:387-452`). Callers reach it under the lock from 263-269, 294 and 308. `on_path_have` POSTs cancels under the lock (339-360).
  - `ReplicationReceiver.on_offer` runs on the push server's handler thread. Before the handler returns, it runs `nix path-info` (614) and POSTs accept or reject (622, 632). The already-targeted reject goes out while the receiver lock is held (625-629).
  - `on_path_reject` calls `sender.on_reject` directly, which takes the sender lock (`cps/suit_task.py:2278-2284`).
  - Diff callbacks run `push_attempt` on the `PathPlacementWatcher` thread (`cps/peer_cache.py:1343-1349`, `cps/peer_replication.py:900-916`).
- Why it matters:
  - Node A holds its lock and POSTs an offer to node B. B's handler POSTs a reject back, and A's handler thread blocks on A's lock. Both requests then sit until their 2 s timeouts expire. A counts the offer as failed, and the reject comes late or not at all.
  - A peer death that touches 300 outpaths keeps the watcher thread blocked for minutes, so placement snapshots go stale.
- Fix: take decisions under the lock and do the I/O after releasing it. A cleaner design makes replication a single-threaded actor. Handlers enqueue typed events, and one worker thread owns the state and does the sending. That removes the RLock and the per-offer `threading.Timer` threads.

### D6. Broadcast receive fetches inside the HTTP handler, and a failed fetch stays deduped
- Severity: high. Category: correctness.
- Evidence:
  - `cps/peer_push.py:274-278` adds `broadcast_id` to the seen set before calling the callback.
  - `on_broadcast_offer` (`cps/peer_replication.py:1483-1526`) then runs `nix path-info` and `nix copy` before the HTTP response goes out.
  - Senders use `DEFAULT_PUSH_TIMEOUT = 2.0` (`cps/peer_push.py:115`), including `SubmitterPeer.seed_toolchain_drvs` (`cps/peer_cache.py:2351-2359`).
  - The class docstring says "the HTTP handler thread is therefore never blocked" (`cps/peer_replication.py:1327-1330`). That holds only for the fan-out.
- Why it matters:
  - Originators record most offers as failed, even when the fetch later succeeds.
  - If the first fetch fails, later forwards from peers that do hold the drv get `{"dedup": true}`, and that node never receives the drv through the flood.
- Fix:
  - Reply `202` immediately and run the fetch on the receiver's worker queue.
  - Keep per-id state (pending, done, failed), and let a later forwarder retry a failed id.

### D7. Serial fan-outs on the build path, and dead peers stay in the peer list
- Severity: medium. Category: performance, correctness.
- Evidence:
  - `fan_out_path_have`, `fan_out_announce` and `fan_out_withdraw` contact peers one at a time with a 2 s timeout each (`cps/peer_push.py:529-629`).
  - `record_self_has` runs that fan-out synchronously (`cps/peer_paths.py:170-178`). The build worker calls it after every toolchain or common-dep build (`cps/workers/build_worker.py:1005-1040`).
  - Only `withdraw_self` removes `peers/<id>.json`.
  - `prune_stale` (`cps/peer_cache.py:484-522`) has no callers, although the module docstring (19-20) says it does. It would not work anyway: `announce_self` writes the file once and never refreshes it, so live peers would look stale too.
  - On shutdown, the withdraw push goes out at `cps/suit_task.py:2547` and the file is deleted at 2645. Peers that wake on the push re-read the directory while our file still exists, so the push has no effect and they only notice on the next 60 s tick.
- Why it matters: every crashed or unreachable secondary adds up to 2 s to every build on every node for the rest of the run.
- Fix:
  - Fan out through a small shared executor or a fire-and-forget queue.
  - Delete the peer file before pushing the withdraw.
  - Drop dead peers on the framework's peer-removed hook, or heartbeat the file and prune by mtime.

### D8. Placement reads are O(total records) per event
- Severity: medium. Category: performance.
- Evidence:
  - `_refresh` re-reads and re-parses every `_paths_*.jsonl` file and recomputes a full diff (`cps/peer_cache.py:1333-1349`, 1372-1423). It runs on every tick and on every `path-have` push (`cps/suit_task.py:2251-2253`). The record carried in the push is thrown away (`cps/peer_push.py:223-230`).
  - `snapshot()` deep-copies every set (`cps/peer_cache.py:1277-1284`).
  - Sender and repair code call `get_placements()` to read a single key (`cps/peer_replication.py:329, 394, 528, 831, 876, 1002`).
  - `_default_metadata_lookup` re-reads and parses the node's whole JSONL file once per outpath (920-951, called per outpath at 905 and 962). A peer death touching P outpaths therefore costs P full-file reads.
- Fix:
  - Track a byte offset per file and parse only the appended lines.
  - Apply pushed records directly.
  - Expose `holders(outpath) -> frozenset` and an outpath-to-record index.

### D9. Thread and lifecycle sprawl
- Severity: medium. Category: concurrency design.
- Evidence:
  - Every offer creates a raw `threading.Timer` (`cps/peer_replication.py:437-443`). That bypasses the injected `timer_factory` (228-231), which only the settle timer uses.
  - Each accepted fetch gets its own thread (633-638), and each broadcast target gets one too (`cps/peer_push.py:842-856`). Threads that time out keep running after `join(timeout)` returns.
  - `BroadcastSender` and `BroadcastReceiver` duplicate the same queue, worker, start and stop scaffolding (`cps/peer_replication.py:1124-1223` and 1421-1617).
  - `ReplicationReceiver.stop` never joins its fetch threads (655-659).
  - `BroadcastSender._results` and `_events` grow without limit.
- Why it matters: nothing documents which threads exist, who owns them, or what `stop()` guarantees, so tests have to sleep and poll (D30).
- Fix: give each component one executor or worker thread, share a `QueueWorker` helper, and create every timer through one injected scheduler.

### D10. Wire messages are untyped dicts, and each schema is spelled out 3 to 5 times
- Severity: medium. Category: types, duplication.
- Evidence:
  - The PeerInfo JSON is written out separately in `announce_self` (`cps/peer_cache.py:335-340`), `_read_peer_file` (379-384), `SubmitterPeer._publish_peer_file` (2196-2201), `fan_out_announce` (`cps/peer_push.py:540-545`) and `do_POST` (214-219).
  - The placement record is written out in `record_self_has` (`cps/peer_paths.py:156-162`), `list_self_placements` (210-221), `_read_all_placement_files` (`cps/peer_cache.py:1416-1422`), `SubmitterPeer.publish_placements` (2153-2159) and the path-have handler (`cps/peer_push.py:224-229`).
  - `do_POST` builds dicts in an 18-branch if/elif chain (`cps/peer_push.py:202-320`, ruff C901 complexity 18). The receiving closures read the fields back with `.get(..., "")` (`cps/suit_task.py:2271-2290`, `cps/peer_replication.py:602-606`).
  - Placement inputs travel as `(outpath, drv, "toolchain")` tuples (`cps/cli.py:1926-1934`, `cps/peer_cache.py:1692`).
  - `seed_toolchain_drvs` returns a dict with an optional `skipped_unreachable` key (`cps/peer_cache.py:2299-2374`).
- Why it matters: renaming a field takes five coordinated edits, and a typo turns into an empty string instead of an error.
- Fix: in `peer_rpc.py`, define one frozen dataclass per message with `to_json` and `from_json`. Dispatch through a route table `{path: (Msg, handler)}` with typed handler protocols.

### D11. Magic strings, two `item_class` vocabularies, and URL arithmetic
- Severity: medium. Category: duplication, naming.
- Evidence:
  - The literal `"submitter"` appears in `cps/peer_paths_fetch.py:58`, `cps/peer_cache.py:2154, 2197, 2236` and `cps/preflight.py:2183`. The file name `_paths_submitter.jsonl` is hard-coded at `cps/peer_cache.py:2165` instead of using `paths_file_for`, and 2015 filters with `grep -v '^submitter\.json$'`.
  - Code passes item classes as raw strings, for example `"toolchain"` in `cps/workers/build_worker.py:1110, 1191` and `cps/cli.py:1934`. `"matrix_eval_drv"` is defined twice (`cps/peer_paths.py:80`, `cps/peer_replication.py:95`).
  - The comment at `cps/peer_paths.py:66-67` says these values "match the values build_worker writes into BuildWorkerResult.item_class". They do not: build_worker uses `toolchain_validate`, `build_common_dep` and `build_variant` (`cps/workers/build_worker.py:120-122`). Two different vocabularies share the field name `item_class`.
  - The push URL `f"http://{host}:{push_port_for(port)}"` is built in `cps/suit_task.py:2190-2196`, `cps/workers/eval_worker.py:1257-1259`, `cps/peer_cache.py:2041-2042` and `cps/peer_push.py:495-498`. `_push_url_to_substituter_url` then parses it back (`cps/suit_task.py:152-185`).
- Fix:
  - Define a `PlacementClass(StrEnum)` and a single `SUBMITTER_PEER_ID` in one module.
  - Rename one of the two `item_class` fields.
  - Add `PeerInfo.push_url()` and pass `PeerInfo` objects around instead of URL strings.

## C. Duplicated process launchers and key handling

### D12. Three harmonia launchers, two key generators, two sshd spawners
- Severity: medium. Category: duplication.
- Evidence:
  - Harmonia is launched by `HarmoniaProcess.start` (`cps/peer_cache.py:797-865`, with no readiness probe), by `SubmitterPeer.start` (1784-1872), and by `python/ssh_debug_runner/bootstrap.py:84-124`. Each one sets `SIGN_KEY_PATH(S)`, writes a TOML file, opens a log, and probes in its own way.
  - Keys come from `generate_signing_key` (`cps/peer_cache.py:225-315`) and from `_generate_submitter_signing_key` (1559-1617). The second runs `write_bytes` and then `chmod 0600` (1607-1608), so the secret is readable under the umask for a moment.
  - `start_sshd_detached` and `spawn_sshd_attached` (`cps/ssh_debug.py:171-232` and 235-296) differ only in `start_new_session` and their return value.
  - Several constants are defined more than once:
    - `PEER_CONF_PATH` in `cps/peer_cache.py:1431` and `bootstrap.py:65`.
    - `LOG_NETWORK_DIR` in `cps/ssh_debug.py:59` and `bootstrap.py:69`, plus a literal in `cps/suit_task.py:2405`.
    - `/tmp/ssh-debug` in `cps/ssh_debug.py:52`, `bootstrap.py:52` and `worker.py:69`.
- Fix:
  - One `launch_harmonia(bind, key, workers, log, argv_prefix) -> Popen` that includes the readiness probe.
  - One keypair helper that opens the secret with `O_CREAT|O_EXCL` and mode 0600.
  - One `_spawn_sshd(detached: bool)`.
  - Shared constants in a single module.

### D13. `preexec_fn` in a threaded process, and leaked log file handles
- Severity: low. Category: concurrency, resources.
- Evidence:
  - The `preexec_fn` in `start_nix_daemon` runs `import sys` and `print` after fork (`cps/peer_cache.py:999-1025, 1129`). Watcher threads are already running by then (`cps/suit_task.py:2060-2065` starts them, 2422-2425 calls `start_nix_daemon`). Ruff flags this as PLW1509.
  - Log handles passed to `Popen` are never closed in the parent: `cps/peer_cache.py:856, 1109-1111` and 1805 (one per retry), `cps/ssh_debug.py:199, 266`, and `bootstrap.py:108`.
- Fix:
  - Call `os.setpriority(PRIO_PROCESS, proc.pid, 11)` after `Popen` instead of using `preexec_fn`.
  - Wrap the open log file in `with` around `Popen`. The child keeps its own copy of the descriptor.

## D. Error handling that hides failure

### D14. Failures are logged at debug level or swallowed on critical paths
- Severity: medium. Category: error handling.
- Evidence:
  - `fetch_from_peer` returns `None` both for "already local" and for "every candidate failed" (`cps/peer_paths_fetch.py:187-188` vs 239). It logs failures only at debug (230-234). The build worker had to wrap the subprocess runner just to capture diagnostics: "fetch_from_peer logs at DEBUG (filtered in production), so without this the only signal is 'source is None'" (`cps/workers/build_worker.py:1121-1127`).
  - The receiver checks local validity and then calls `fetch_from_peer` with the default `check_local=True` (`cps/peer_replication.py:614, 717`). That runs `nix path-info` twice per offer.
  - `push_to_peer` logs at debug level, although its docstring says WARN (`cps/peer_push.py:477-480, 519-526`).
  - `_maybe_record_self_has`, `_resolve_placements` and `_resolve_peers` catch `Exception` and `pass` without logging (`cps/workers/build_worker.py:965-976, 986-999, 1043-1044`).
  - `start_nix_daemon` returns the PID even when the daemon never accepted a connection within 5 s, and it ignores a failed `nix-store --init` (`cps/peer_cache.py:1077-1089, 1142-1147`).
  - `HarmoniaProcess.start` returns before the server is serving.
- Why it matters: the system quietly falls back to building from source or to no replication, and the logs show nothing.
- Fix:
  - Return a result type: `AlreadyLocal`, `Fetched(sid)`, `Unknown` or `AllFailed(errors)`.
  - Log the final failure at WARNING with the last stderr.
  - Warn when a readiness check times out.
  - Every `except` should log something.

## E. Cachix uploader

### D15. The cachix token goes through a process-wide env mutation on a background thread
- Severity: medium. Category: correctness, global state.
- Evidence:
  - `_default_run_subprocess` has no `env` parameter (`cps/cachix_uploader.py:88-99`).
  - So `_invoke_cachix` always falls into the `TypeError` path, then sets and restores `os.environ["CACHIX_AUTH_TOKEN"]` (249-273). That happens on the uploader thread while other threads spawn nix, harmonia and workers, which can inherit the token.
  - A `TypeError` raised inside a custom runner also causes the push to run a second time.
  - The comment at 214-217 contradicts itself.
- Fix: give the default runner an `env=` parameter and delete the probing fallback.

### D16. The uploader polls the whole store and pushes one path at a time
- Severity: medium. Category: performance, correctness.
- Evidence:
  - Every 30 s it runs `nix path-info --all --json` (`cps/cachix_uploader.py:128-181`). The fallback, `nix-store --gc --print-live`, returns live paths, which is a different set from all paths.
  - `seen` starts empty, so the first tick treats the entire image store as new.
  - It runs one `cachix push` subprocess per path, serially, and re-reads the token file for each one (206, 335-362).
  - The privacy filter looks only at the basename (49-63), but `cachix push` uploads the full closure.
- Fix:
  - Use `cachix watch-store` or a post-build hook, or push in batches.
  - Seed `seen` from the first listing.
  - Check the closure (`nix path-info -r`), or document and test the rule that no pushable path references an `-elf-folder` output.

## F. Dead code and stale documentation

### D17. Production code that only tests keep alive
- Severity: medium. Category: dead code.
- Evidence:
  - `BroadcastSender`, `BroadcastResult` and `_BroadcastJob` (`cps/peer_replication.py:1039-1283`, about 245 lines) have no callers. `cps/workers/eval_worker.py:39-42` says the broadcast loop was removed, yet the tests remain at `cps/tests/test_peer_replication.py:1147-1340`.
  - `fan_out_path_gone` (`cps/peer_push.py:605-629`) is unused.
  - `has_nix_cli` (`cps/peer_paths_fetch.py:289-298`) is unused.
  - `assemble_substituter_env` and `prune_stale` (`cps/peer_cache.py:427-446, 484-522`) are unused.
  - The `substitute_on_destination` parameter of `build_nix_extra_args` (449-451) is ignored "for API stability", but `PeerListWatcher` still threads it through (613, 620, 698).
  - `ReplicationContext.reinject_task` is bound (`cps/suit_task.py:2122`) and never called (`cps/peer_replication.py:150-152`).
  - `eval_worker.read_peer_push_urls` and its alias (`cps/workers/eval_worker.py:1213-1268`) were "retained because consumers outside this module still reference it". There are no such consumers.
- Fix: delete this code and its tests.

### D18. Stale or contradictory docstrings and planning references
- Severity: low. Category: documentation.
- Evidence:
  - `SubmitterPeer`'s docstring says "fresh signing keypair ... one per dispatch" (`cps/peer_cache.py:1633-1635`). The key is actually persistent (1564-1583).
  - `PeerListWatcher`'s docstring says it writes `peers/_substituters.txt` (591-592). The file is per-secondary.
  - The `ITEM_CLASS_MATRIX_EVAL_DRV` and `BROADCAST_ITEM_CLASS` comments mention an eval_worker broadcast that is gone (`cps/peer_paths.py:75-80`, `cps/peer_replication.py:90-95`).
  - `bootstrap.py:33-35` says "we only add the nix.conf rewrite layer", but the code delegates to `PeerNixConfWatcher`.
  - `cps/ssh_debug.py:14-16` claims idempotence based on whether `sshd_config` exists. The code actually probes the port.
  - A TODO waits for `holding_matcher` "once #70 lands" (`cps/peer_replication.py:1018-1020`). It has landed.
  - Planning ids are scattered through the code: "Framework Ask #4", Q1 to Q4, "task #71".
  - `python/pyproject.toml:54-58` calls a sibling package "vendored".
- Fix: describe current code by its names and drop the plan ids.

## G. SubmitterPeer

### D19. Placements go to the gateway as one shell argument, and remote paths are not quoted
- Severity: medium. Category: correctness.
- Evidence:
  - `publish_placements` base64-encodes the whole JSONL into `echo {b64} | base64 -d > file` (`cps/peer_cache.py:2167-2172`).
  - About 317 toolchains at about 225 B each give about 95 KB of base64. Linux caps a single `sh -c` argument at 128 KiB (MAX_ARG_STRLEN).
  - Past that cap the command fails, and `_poll_loop` retries every 2 s forever (1944-1951, 1974-1985).
  - `_ssh_oneshot` already accepts `stdin_input` (2044-2046), but nothing uses it.
  - `slurm_root`, `run_id` and the remote `ls` output go into remote shell strings without quoting (1899-1902, 2008-2025, 2093-2095).
- Fix: send the body on stdin with `cat > tmp && mv tmp final`, quote with `shlex.quote`, and limit the retries.

### D20. The bind retry runs `pkill -KILL -f harmonia-cache`
- Severity: medium. Category: correctness.
- Evidence: `cps/peer_cache.py:1848-1866` kills every `harmonia-cache` process the user owns, including another concurrent dispatch's. The docstring at 1649-1652 says parallel dispatches are supported, and the comment claims the match avoids "killing unrelated services".
- Fix: write a PGID file under `_SUBMITTER_KEY_DIR` and kill only that process group, or pick a free local port.

### D21. `SubmitterPeer` is a five-flag poll loop whose setters take no lock
- Severity: low. Category: concurrency design.
- Evidence:
  - `_poll_loop` (`cps/peer_cache.py:1933-1990`) juggles `_run_id`, `_peer_published`, `_placements_published`, `_toolchain_seed_done` and two pending buffers.
  - `set_placements` and `set_pending_toolchain_seed_drvs` (1708-1735) can run on another thread with no lock.
  - Each 2 s tick opens a new ssh connection, and one tick can open as many as three (2012-2025, 2093).
  - Tests cover none of `publish_placements`, the pkill retry, or `_discover_first_secondary_url`.
- Fix:
  - Fix the pending data before `start()`.
  - Write the loop as a sequence of steps, each retried until it succeeds.
  - Reuse a single ControlPath.
  - Add tests with a fake `_ssh_oneshot`.

### D22. The push "auth" header carries a public value
- Severity: low. Category: naming, security.
- Evidence: `X-Cluster-PubKey` is compared against the public key (`cps/peer_push.py:178-182`). That key is published world-readable in `peers/<id>.json` (`cps/peer_cache.py:335-341`).
- Fix: call it a run-membership token in names and docs. If real authentication matters, sign the request with an HMAC keyed by the 0600 secret.

### D23. Handler state lives on handler subclasses created at runtime
- Severity: low. Category: global state.
- Evidence:
  - `_PushHandler` has class-level defaults `seen_broadcast_ids: set = set()` and a shared `Lock` (`cps/peer_push.py:160-161`).
  - `PeerPushServer` creates a new subclass per instance to bind 11 callbacks (372-416).
  - The `port` property returns the requested port rather than the bound one (420-422). That forces the tests to use `_free_port` (D30).
  - `_SUBMITTER_KEY_DIR` comes from `Path.home()` at import time (`cps/peer_cache.py:1556`).
- Fix: subclass `ThreadingHTTPServer` with a `context` attribute that handlers read through `self.server.context`, and return `server_address[1]` as the port.

## H. ssh_debug_runner

### D24. Hand-rolled argv surgery, and drift from compiler_suit_runner
- Severity: medium. Category: correctness, maintainability.
- Evidence:
  - `cmd_submit` reads `sys.argv` and ignores the `argv` passed to `main` (`python/ssh_debug_runner/cli.py:166-172`).
  - `--no-submitter-peer` and `--submitter-harmonia-port` are read but never declared or stripped (168-172, 103-122), so they reach the framework parser.
  - `_strip_my_args` drops every `submit` or `secondary` token, even one that is a flag value (114-115).
  - The global `sys.argv` is overwritten (180).
  - Secondary mode hard-codes `containers=2` (390-395).
  - `SubmitterPeer` is created without `identity_file` or `config_file` (193-200), unlike `cps/cli.py:1910-1918`. That brings back the agent-key and PerSourcePenalties failure described at `cps/peer_cache.py:2061-2067`.
  - None of the cli logic has tests. The only test file covers the worker.
- Fix: declare every flag in argparse, rebuild the framework argv from the `parse_known_args` remainder, and pass `args=` to `dynamic_runner.run` the way compiler_suit_runner does.

### D25. ssh_debug_runner imports the whole compiler_suit_runner package for node infrastructure
- Severity: low. Category: packaging, coupling.
- Evidence:
  - It imports `peer_cache` (`bootstrap.py:152-176`, `cli.py:193`) and `ssh_debug` (`cli.py:310`, `worker.py:55-58`).
  - `cps/ssh_debug.py:4-10` explains why the sshd helpers live in compiler_suit_runner.
  - `_diag` and `_ts` are copied three times (`bootstrap.py:72-81`, `worker.py:88-115`, `cps/ssh_debug.py:62-63`).
- Fix: move the node-level pieces into a small `cluster_node` package that both runners depend on: nix-daemon bootstrap, the harmonia launcher, peer discovery, the sshd helpers and diag logging.

### D26. The "graceful" sshd exit codes follow shell conventions
- Severity: low. Category: correctness.
- Evidence: `python/ssh_debug_runner/worker.py:85` treats `{0, 130, 143}` as graceful.
  - `Popen.returncode` is negative for a death by signal (-15, -2).
  - sshd's own handler exits 0 on SIGTERM and 255 on SIGINT.
  - So 130 and 143 can never occur, and a SIGINT is reported as `NonRecoverableError`.
  - `python/ssh_debug_runner/tests/test_worker_wire_format.py:30-36` makes the same assumption.
- Fix: accept `rc == 0`, plus `rc < 0` when the signal is SIGTERM or SIGINT.

## I. Smaller modules

### D27. `holding_matcher` uses a regex to recover data that was structured a moment earlier
- Severity: low. Category: types.
- Evidence:
  - The repair worker formats `f"toolchain outpath={outpath} dead_holders={last_known}"`, with the holder list in Python repr form (`cps/peer_replication.py:1021-1024`). It does not use `UNFULFILLABLE_REASON_TEMPLATE` (`cps/holding_matcher.py:60-62`).
  - The matcher then parses the outpath back out with a regex (67, 99).
- Fix: put `format_unfulfillable_reason(outpath, dead)` and `parse_unfulfillable_reason(s)` in one module and use them on both sides. If the framework ever gains structured metadata, switch to that.

### D28. `streamed_spawn.decode_spawn_message` returns tagged dicts
- Severity: low. Category: types.
- Evidence:
  - The decoder (`cps/streamed_spawn.py:440-529`) returns `{"kind": "spawn_batch", ...}` or `{"kind": "summary", ...}`, and the consumer branches on that string (`cps/suit_task.py:3136-3140`).
  - Counter validation is duplicated in `encode_summary` (406-415) and the decoder (511-522).
- Fix: return `SpawnBatch` and `SpawnSummary` frozen dataclasses and share a `_check_counters` helper. The rest of this module is solid.

## J. Tests

### D29. `tests/slurm` mixes several kinds of code in one directory
- Severity: medium. Category: test organisation.
- Evidence:
  - The directory holds five kinds of code:
    - 13 live end-to-end tests (`test_tNN_*.py`, marked `slurm_live`).
    - A harness library: `cluster_probe.py` (1126 lines), `invariants.py` (1056 lines, with an argparse CLI at 928-1055), `run_helpers.py`, `peer_mesh_assertions.py` and `placement_assertions.py`.
    - Unit tests for that harness, which run in the default suite: `test_cluster_probe.py` (1367 lines), `test_invariants.py` (1133 lines) and others.
    - Failure-injection helpers under the name `reproducers/` (`inject_failures.py` at 1899 lines, `broken_toolchain.py` at 795).
    - An `artifacts/` output directory inside the source tree (`test_t02_post_promotion_hang.py:108-109`).
  - Copy-paste is heavy:
    - Each of the 13 live tests defines its own `_resolve_timeout`, `_live_probe` and `_format_results`.
    - `_ANSI_RE` is copied 6 times and `_strip_ansi` 5 times.
    - 16 files hard-code `/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519`, and 15 hard-code the gateway `sirati@localhost` (port 2244).
  - The conftest `cluster_probe` fixture already builds a probe with the key (`conftest.py:230-259`). The comment that justifies building one per file is stale (`test_t04_n2_clean.py:90-97`).
  - The production flake carries the test-only attribute `__test_broken__` (`reproducers/broken_toolchain.py:9-12`).
- Fix:
  - Move the harness into a `slurm_testkit` package (or `tests/slurm/_harness/`) with its own unit tests.
  - Keep the live tests thin and fixture-driven: `timeout_from_env("T1")`, one live-probe fixture, and paths taken from env or conftest.
  - Write artifacts under the pytest basetemp.
  - Rename `reproducers/` to `fault_injection/`.

### D30. Peer unit tests depend on timing and pin dead code
- Severity: low. Category: tests.
- Evidence:
  - `_free_port` has a bind/close race (`cps/tests/test_peer_push.py:56-60`), caused by the port issue in D23.
  - `test_peer_replication.py` defines `_wait_for` twice (386 and 1134) and calls sleep 11 times.
  - Some tests cover dead code: `BroadcastSender`, `prune_stale` and `assemble_substituter_env` (`test_peer_cache.py:178-260`).
  - No test covers an accept followed by a slow fetch (D4), or a reject that arrives while the sender holds its lock (D5).
- Fix: bind to port 0, inject a scheduler, and share one wait helper through conftest. Then add two-node protocol tests over an in-memory transport.

## K. template_graph

The template_graph suite passes when run directly (139 passed, 5 skipped). The default `pytest` configuration never runs it (T1).

### T1. `template_graph` is an unpackaged runtime dependency of compiler_suit_runner, so it should move under `python/`
- Severity: high. Category: packaging, coupling.
- Evidence:
  - About 10 production imports use it:
    - `cps/workers/dependency_graph_worker/archive.py:38` imports it at module level.
    - Lazy imports with `noqa: PLC0415` sit in `cps/preflight.py:1180`, `cps/workers/eval_worker.py:1000`, `cps/workers/dependency_graph_worker/plan.py:65, 182`, `sum_drv.py:64, 106`, `cps/dependency_graph_planner/plan_cell.py:63, 95` and `plan_meta.py:45`. The last one imports the private `_ARCH_FAMILIES`.
  - `python/pyproject.toml:52-60` packages only `compiler_suit_runner*` and `ssh_debug_runner*`, and line 35 declares `dependencies = []`.
  - The import resolves only through workarounds:
    - The image puts the whole flake source on `PYTHONPATH` (`nix/docker-image.nix:449-453`).
    - The dev shell exports only `python/` (`develop.nix:61-65`), and the READMEs tell users to export the repo root by hand.
    - `template_graph/conftest.py:6-11` patches `sys.path`.
  - Its tests are not in `testpaths` (`python/pyproject.toml:63-66`), but the two test suites import each other's fixtures (`template_graph/tests/test_tree_walker_bytes.py:182-190`, `cps/tests/test_dependency_graph_worker.py:2178, 2282`).
  - The dependency runs one way: template_graph's runtime code never imports compiler_suit_runner.
- Why it matters: `pip install` of `compiler-suit-runner` produces a package that fails in preflight and the dependency-graph worker. A default `pytest` run never runs the planner's own tests. The repo-root placement suggests the package is standalone, but in practice it is a library of the runner.
- Fix:
  - Move it to `python/template_graph/` and add it to `include`.
  - Ship the `*.nix` files as package data.
  - Add `template_graph/tests` to `testpaths` and delete the conftest path hack.
  - Expose `ARCH_FAMILIES` publicly, or move it per T4.

### T2. Template-to-binary attribution parses the binary name from the first dash
- Severity: high. Category: correctness.
- Evidence:
  - `template_graph/cowalk/template_histogram.py:125-141` (`_binary_of`) takes the text before the first `-`.
  - `cps/workers/dependency_graph_worker/slice.py:22-58` copies that logic and uses it to split per-binary results (`plan.py:200-205`).
  - `template_graph/cowalk/_role_merge.py:90-96` matches with `startswith(f"{binary}-")` and overwrites `by_arch[arch]`.
  - `lib/packages.nix` has dashed labels, among them `c-ares` (208), `zlib-ng` (62), `cmark-gfm` (121) and `quickjs-ng` (329).
- Why it matters: `c-ares` is attributed to `c`. `zlib-ng` templates are merged into `zlib`, and `zlib-ng` gets none. The phase-4 descriptors and `meta_templates` for those binaries come out wrong or empty.
- Fix: the planner already knows `planner.mx.matrix_binary` (`template_graph/streaming/dispatch.py:106`) when it creates each array (`finalize.py:116-122, 158-164`). Store `binary` on `VariantArray` and delete both name parsers.

### T3. Dead or broken code paths
- Severity: medium. Category: dead code.
- Evidence:
  - The `python -m template_graph run|debug` CLI can never work. `cli_sumdrv.py:28-32` passes raw toolchain lists and leaf variant drvs to `make_sum_drv_from_paths`, which requires exactly one aggregate of each kind (`make_sum_drv.py:437-460`). No test imports `cli`.
  - Cross-binary template dedup never fires. Root roles carry the full variant name, so `_shape_equal` fails on the root (`graph/template.py:117-141`). Both `cowalk/cross_arch.py:14-23` and a test admit this, yet `cps/workers/dependency_graph_worker/plan.py:96-99` says dedup works.
  - If dedup ever did match, `finalize.py:122, 164` would overwrite arrays keyed by `(tmpl_id, arch)`.
  - `find_or_register_template` scans linearly (`graph/template.py:160`), which makes registration O(T²).
  - `core.py`, used only by the dead CLI, the re-export shims in `streaming/__init__.py:44-89` and `streaming/_helpers.py`, and the `__all__` lists that export underscore names all exist only for in-repo back-compat.
  - In `tests/fixtures/`, only `hello_sum_drv.txt` is read (`tests/conftest.py:33`). The `generate_*` and `probe_*` scripts and their committed summaries are one-off exploration tooling. Pytest never runs them, and they are the only callers of the deprecated `make_sum_drv` / `sum_drv.nix`.
- Fix:
  - Delete the CLI or rebuild it on `make_wrapper_drv_from_paths`.
  - Remove the dedup, or key templates by shape hash and arrays by `(binary, arch)`.
  - Drop the shims.
  - Move `generate.py` to `tools/` and delete the probes, the summaries and `sum_drv.nix`.

### T4. Naming, arch and drv-parsing logic is duplicated across the two packages
- Severity: medium. Category: duplication.
- Evidence:
  - Variant label slicing and composition appears three times: `template_graph/streaming/dispatch.py:166-175`, `cps/workers/dependency_graph_worker/archive.py:683-686` and `cps/workers/eval_worker.py:969`.
  - The tree query appears twice: `cli_sumdrv.py:35-47` and `cps/workers/dependency_graph_worker/sum_drv.py:75-92`.
  - `nix derivation show` parsing appears three times: `drv_io.py:43-95` (used only by tests), `cps/drv_graph.py` and `nix_drv_show.py`.
  - There are four arch tables: `tree_walker.py:127`, `cowalk/_helpers.py:25`, `cowalk/cross_arch.py:62` and `tests/dataset_naming.py:23`.
  - An arch missing from `DEFAULT_ARCHS` raises `TreeWalkError` (`tree_walker.py:190`) even under `lax=True`, which aborts phase 3.
- Fix: one variant-naming module (parse and compose) and one arch table generated from `lib/architectures.nix`, both owned by template_graph and imported by the runner.

### T5. `make_sum_drv.py` mixes runner debugging into library code and builds Nix in Python strings
- Severity: medium. Category: layering.
- Evidence:
  - `_live_peer_conf_opts` reads `/etc/nix/peer.conf` (`make_sum_drv.py:130-164`).
  - A `CSR_NIX_DEBUG_DIR` dump probes harmonia at `localhost:5005` and a hard-coded store path from one debug session (216-291, with the path at 280).
  - `_run_nix_instantiate` has 69 statements (ruff PLR0915).
  - Nix code is spliced from Python f-strings (99-116, 336-348, 476-484, 525-531).
  - The same `bash -c true` aggregate derivation exists three times (`sum_drv.nix:44-53`, `sum_drv_from_aggregates.nix:37-48`, `wrapper_drv.nix:13-18`).
  - `wrapper_drv.nix:7` uses an impure `import <nixpkgs> {}`, so the aggregate's drv hash depends on `NIX_PATH`. The image sets `NIX_PATH` only for this (`nix/docker-image.nix:455-460`).
- Fix:
  - Take `extra_nix_args` and an `on_failure` callback from the caller, and move the dump into compiler_suit_runner.
  - Replace the three files with one `aggregate.nix {name, system, bash, drvs}` and pass the data through `--argstr` or JSON.

### T6. The cowalk/streaming boundary is leaky, and planner state is stringly-typed
- Severity: medium. Category: separation of concerns, types, performance.
- Evidence:
  - cowalk takes `StreamPlanner`, mutates `planner.out/mx/vb`, and calls its private methods (`cowalk/build_template.py:73, 169`, `cowalk_variant.py:123, 141`). Seven function-level imports exist only to break the resulting cycle (`streaming/state.py:194, 204, 209, 229`, `dispatch.py:104, 247`, `finalize.py:34`).
  - The import at `state.py:204` runs on every tree line.
  - `finalize()` returns an 11-key dict with renamed fields, so consumers accept both shapes (`finalize.py:41-57`, `template_histogram.py:166-171`).
  - Classes are plain strings: `"common_dep"`, `"variant_specific"`, `"toolchain"` and `"?"`, used in 8 files.
  - `placement` is annotated `dict[str, ...]` but keyed by a tuple (`state.py:70`).
  - The per-walk `visit_flag` lives on the shared `TemplateNode` (`graph/template.py:35`).
  - `drv_role` (6+ regexes) is not memoized and runs per child per variant (`cowalk_variant.py:101-104`).
- Fix:
  - Pass a `CowalkCtx` with an explicit interface instead of the planner.
  - Return a frozen `PlanResult`.
  - Add `NodeClass` and `Section` enums.
  - Keep the visited set in the context.
  - Wrap `drv_role` in `lru_cache` and make `RawTreeNode` a `slots=True` class with a stored `ident`.

---

## What is good

- `streamed_spawn.py` is a clean, dependency-free codec. It does exact byte accounting against the size cap, decodes strictly with errors an operator can read, carries a wire version, and leaves no partial state when `add` fails. Its tests (29 cases, including an end-to-end consumer pipeline) are thorough.
- The NFS race fixes capture real failures and explain them in the code:
  - per-writer tmp file names (`cps/peer_cache.py:120-148`);
  - the `O_EXCL` election for the signing key (286-315);
  - harmonia TOML kept container-local (736-742);
  - `NixStoreRegistrationError` failing loudly (916-996).
- Dependency-injection seams exist across the board: `RunSubprocess` in fetch and cachix, the callables in `ReplicationContext`, and the broadcast fan-out. Most protocol logic can be tested without nix.
- `record_self_has` treats the file as the source of truth and the push as best-effort, which is the right split. An append-only JSONL file written with a single `O_APPEND` write is a sound choice.
- template_graph's streaming design is sound. `drv_tree_stream` and `plan_from_drv_tree` consume `nix-store --query --tree` output without holding the full tree in memory. `make_sum_drv_from_paths` fails early with clear errors and writes the expression to a temp file, which avoids ARG_MAX. Lax mode records structured violations. The synthetic tree fixtures let the planner be tested without nix, and that suite runs in about 2 s.
- `ssh_debug_runner/worker.py` follows the framework's worker contract. It reaps the sshd child in `finally`, and `make_handle` exposes injection points for tests.

## Suggested refactor order

1. **Delete dead code (D17) and its tests.** This has no dependencies and shrinks every later step.
2. **Add typed messages and constants (D10, D11).** That means `PlacementClass`, `SUBMITTER_PEER_ID`, `PeerInfo.push_url()`, dataclass messages and records, and a route-table server. Steps 3 to 6 build on these.
3. **Split modules by concept (D1, D2, D12).** This is mostly mechanical. Merge the duplicate harmonia, key and sshd launchers during the move. Keep re-export shims for a release if anything outside the repo imports the old names.
4. **Rebuild K=3 replication as a single-threaded actor (D4, D5, D9).** Write two-node in-memory protocol tests first, so they fail on today's behaviour. Then model the OFFERED/ACCEPTED states with a fetch deadline, and do I/O only after a decision is taken.
5. **Make broadcast receive asynchronous with per-id state (D6), and parallelise fan-out with peer pruning and the withdraw-order fix (D7).** These reuse the worker pattern from step 4.
6. **Add a `PeerMesh` composition root (D3).** It replaces the 560-line block in `SuitTask.on_run_start`. It needs steps 2 to 5 so the wiring it holds is typed and stable.
7. **Make placement reads incremental and add `holders()` (D8).** This is independent once step 3 is done.
8. **Clear the remaining fixes in any order:** SubmitterPeer (D19, D20, D21), cachix (D15, D16), error reporting (D13, D14), and ssh_debug_runner argv handling (D24, D26).
9. **Fix template_graph.**
   - Fix T2 right away. It is a small, independent correctness fix: store `binary` on `VariantArray`.
   - Then do the packaging work together, because it all touches pyproject, PYTHONPATH and the image layout: move the package under `python/` and add it to `testpaths` (T1), extract `cluster_node` (D25), and deduplicate naming and arch tables (T4).
   - After that, address T3, T5 and T6 inside the package.
10. **Restructure `tests/slurm` (D29).** This is independent and can run in parallel with any step above.
