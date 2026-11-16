"""Harmless ENOSPC tests. The batch selects a 64 MiB, 8192-inode filesystem."""
import errno
import json
import os
from pathlib import Path
import shutil
import sys

root = Path(os.environ["TMPDIR"])/"owned-quota-probe"
root.mkdir()
report = dict(schema=1,builder_pid=os.getpid(),cgroup=Path("/proc/self/cgroup").read_text().strip(),
              tmpdir=os.environ["TMPDIR"])
try:
    allocation = root/"allocation"
    total = 0
    try:
        with allocation.open("wb",buffering=0) as dest:
            block = b"\0"*65536
            for _ in range(2048):
                total += dest.write(block)
        raise RuntimeError("filesystem did not stop a 128 MiB allocation")
    except OSError as error:
        if error.errno != errno.ENOSPC:
            raise
        report["disk_exhaustion_errno"] = error.errno
        report["allocated_file_bytes"] = allocation.stat().st_blocks*512
        report["written_bytes"] = total
    allocation.unlink()
    files = 0
    try:
        for index in range(16384):
            (root/str(index)).touch()
            files += 1
        raise RuntimeError("filesystem did not enforce its inode limit")
    except OSError as error:
        if error.errno != errno.ENOSPC:
            raise
        report["inode_exhaustion_errno"] = error.errno
        report["created_files"] = files
finally:
    shutil.rmtree(root)
stats = os.statvfs(os.environ["TMPDIR"])
report["after_cleanup"] = dict(free_bytes=stats.f_bavail*stats.f_frsize,free_inodes=stats.f_favail)
Path(sys.argv[1]).write_text(json.dumps(report,sort_keys=True)+"\n")
