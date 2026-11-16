"""Compact released batch reports; never remove active roots or candidates."""
import json
from pathlib import Path
import shutil
import tarfile

import batch


def main():
    parent=batch.REPO/".capture-experiments"
    released=[]
    for directory in sorted(parent.glob("batch-*")):
        ledger=directory/"ledger.json"
        if not ledger.is_file() or any(directory.glob("*.nar")):
            continue
        metadata=json.loads(ledger.read_text())
        if "released" not in metadata.get("cleanup","") or (directory/"inputs").exists():
            continue
        if any((directory/name).is_mount() for name in ["bounded"]):
            continue
        released.append(directory)
    if not released:
        return
    archive=parent/f"released-reports-{len(list(parent.glob('released-reports-*.tar.xz'))):04d}.tar.xz"
    with tarfile.open(archive,"x:xz") as tar:
        for directory in released:
            for path in sorted(directory.glob("*.json")):
                if path.is_symlink() or path.stat().st_size>batch.LIMITS["metadata_file_bytes"]:
                    raise RuntimeError("invalid retained report")
                tar.add(path,arcname=directory.name+"/"+path.name,recursive=False)
    if archive.stat().st_size>16*1024**2:
        archive.unlink()
        raise RuntimeError("compressed reports exceed the 16 MiB archive budget")
    # Verify every archived file before releasing its original directory.
    with tarfile.open(archive,"r:xz") as tar:
        for entry in tar:
            source=parent/entry.name
            if tar.extractfile(entry).read()!=source.read_bytes():
                raise RuntimeError("report archive verification failed")
    for directory in released:
        shutil.rmtree(directory)
    print(json.dumps(dict(archive=str(archive),batches=len(released),bytes=archive.stat().st_size)))


if __name__=="__main__":
    main()
