# SLURM Dispatch

Runs the full `hello` package across all compilers and architectures, sampling 2 random flag/hardening combinations per (compiler, arch, opt) group. The primary runs locally and orchestrates 10 SLURM secondaries on the Krater cluster via podman containers.

```bash
nix develop --no-write-lock-file --command bash -c '
  cd /home/sirati/devel/nix/asm-dataset-nix
  PYTHONPATH=python python -m compiler_suit_runner submit \
    --shared-fs /tmp/asm-suit-shared \
    --packages hello \
    --multi-computer slurm \
    --packaging podman \
    --jobs 8 \
    --gateway "ssh://kruppb@remote.cip.ifi.lmu.de" \
    --slurm-root-folder /home/k/kruppb/BIG/slurm \
    --slurm-partition Krater \
    --slurm-time-limit 6:00:00
'
```
