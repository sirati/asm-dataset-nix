"""asm-dataset-nix ssh-debug runner.

Alternative dynamic_runner TaskDefinition that, instead of dispatching
compiler-suit work, spawns N podman containers (typically via SLURM)
each running OpenSSH on a high port. A developer with the matching
ephemeral private key can ssh in through the SLURM gateway for live
debugging.

Subpackages:
- :mod:`ssh_debug_runner.cli` — `submit` (primary host) and
  `secondary` (in-container) subcommands.
- :mod:`ssh_debug_runner.task` — :class:`SshDebugTask` (the
  TaskDefinition the framework consumes).
- :mod:`ssh_debug_runner.worker` — per-item worker module that
  exec's sshd in foreground.
"""

__all__: list[str] = []
