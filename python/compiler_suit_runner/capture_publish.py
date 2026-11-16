"""Publish complete capture generations through the framework's file transport."""
from __future__ import annotations

import fcntl
import glob
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid


class PublicationError(ValueError):
    pass


def _document(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PublicationError("duplicate publication metadata key")
            result[key] = value
        return result
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        limit = 16 * 1024**2
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PublicationError("publication metadata is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(limit + 1)
        if len(data) > limit:
            raise PublicationError("publication metadata grew beyond its limit")
        result = json.loads(data, object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise PublicationError("invalid publication metadata document")
        return result
    finally:
        os.close(fd)


def _directory_sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_outputs(outputs, src_root, dst_root, publish_all, validate, *, before_commit=None):
    """Return False for ordinary/legacy outputs, otherwise publish one generation.

    The transport stages files inside a hidden destination directory. Only a
    validated directory becomes visible through a relative variant symlink.
    Existing readers can resolve that symlink once to pin a generation. Retain
    the previous generation; cleanup never traverses unrelated output trees.
    """
    outputs = [Path(path) for path in outputs]
    completions = [path for path in outputs if path.name == "capture-complete.json"]
    if not completions:
        return False
    if len(completions) != 1:
        raise PublicationError("expected exactly one captured variant publication")
    completion = _document(completions[0])
    revision = completion.get("publication_schema", 1)
    if type(revision) is not int or revision not in [1, 2]:
        raise PublicationError("unsupported capture publication revision")
    if revision < 2:
        return False
    source = completions[0].parent
    src_root = Path(src_root).resolve(strict=True)
    Path(dst_root).mkdir(parents=True, exist_ok=True)
    dst_root = Path(dst_root).resolve(strict=True)
    source = source.resolve(strict=True)
    try:
        relative = source.relative_to(src_root)
    except ValueError as error:
        raise PublicationError("capture generation is outside its staging root") from error
    if not relative.parts or source == dst_root/relative:
        raise PublicationError("capture staging and destination must be distinct")
    destination = dst_root/relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.parent.resolve().is_relative_to(dst_root):
        raise PublicationError("capture destination escapes its publication root")
    expected = dict(label=completion["label"], drv=completion["drv"], contract=completion["contract"])
    if not validate(source, **expected):
        raise PublicationError("staged capture generation failed validation")
    members = [source/path for path in completion["files"]] + [source/"capture-complete.json"]
    if set(path.resolve() for path in outputs if path.is_relative_to(source)) != set(members):
        raise PublicationError("staged outputs do not cover exactly the captured generation")
    sidecars = [path for path in outputs if not path.is_relative_to(source)]
    if len(sidecars) > 1:
        raise PublicationError("unexpected files outside the capture generation")
    if sidecars:
        sidecar = sidecars[0]
        if sidecar.parent.resolve() != source.parent or sidecar.suffix != ".json":
            raise PublicationError("capture sidecar is outside its package staging directory")
        if _document(sidecar) != _document(source/"auxiliary/provenance/variant.json"):
            raise PublicationError("capture sidecar differs from its indexed generation metadata")
    generations = destination.parent/("." + destination.name + ".capture-generations")
    if generations.is_symlink():
        raise PublicationError("capture generation directory is a symlink")
    generations.mkdir(exist_ok=True)
    marker = generations/"owner.json"
    owner = dict(schema=1, variant=destination.name)
    lock_path = generations/"publish.lock"
    if lock_path.is_symlink():
        raise PublicationError("capture publication lock is a symlink")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.exists():
            recorded_owner = _document(marker)
            if type(recorded_owner.get("schema")) is not int or recorded_owner != owner:
                raise PublicationError("capture generation ownership mismatch")
        else:
            if set(path.name for path in generations.iterdir()) != {"publish.lock"}:
                raise PublicationError("refusing to claim a nonempty generation directory")
            with marker.open("x") as file:
                json.dump(owner, file, sort_keys=True)
        # A writer can die after writing history or creating a pointer, before
        # its rename. The verified owner and lock let the next writer remove
        # only these reserved temporary names without following their targets.
        for path in generations.glob(".history-*"):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"\.history-[0-9a-f]{32}", path.name):
                raise PublicationError("invalid abandoned capture history file")
            path.unlink()
        pointer_prefix = "." + destination.name + ".pointer-"
        for path in destination.parent.glob(glob.escape(pointer_prefix) + "*"):
            if not path.is_symlink() or not re.fullmatch(re.escape(pointer_prefix) + r"[0-9a-f]{32}", path.name):
                raise PublicationError("invalid abandoned capture pointer")
            target = Path(os.readlink(path))
            if target.parent != Path(generations.name) or not re.fullmatch(r"generation-[0-9a-f]{32}", target.name):
                raise PublicationError("abandoned capture pointer escapes managed generations")
            path.unlink()
        previous = None
        if os.path.lexists(destination):
            if not destination.is_symlink():
                raise PublicationError("refusing to replace an unmanaged capture destination")
            target = Path(os.readlink(destination))
            if target.is_absolute() or target.parent != Path(generations.name) or not re.fullmatch(r"generation-[0-9a-f]{32}", target.name):
                raise PublicationError("capture destination points outside its managed generations")
            previous = target.name
        history_path = generations/"history.json"
        history = _document(history_path) if history_path.exists() else {}
        if not isinstance(history, dict) or any(key not in ["current", "previous"] for key in history):
            raise PublicationError("invalid capture generation history")
        retained = {previous}
        for key in ["current", "previous"]:
            value = history.get(key)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"generation-[0-9a-f]{32}", value)):
                raise PublicationError("invalid capture generation history entry")
            if value and value != previous:
                retained.add(value)
                break
        for path in generations.glob("generation-*"):
            if path.is_symlink() or not re.fullmatch(r"generation-[0-9a-f]{32}", path.name):
                raise PublicationError("invalid owned capture generation")
            if path.name not in retained:
                shutil.rmtree(path)
        # No other writer can own an incoming directory while this lock is held.
        for path in generations.glob(".incoming-*"):
            if path.is_symlink() or not re.fullmatch(r"\.incoming-[0-9a-f]{32}", path.name):
                raise PublicationError("invalid abandoned capture staging directory")
            shutil.rmtree(path)
        identity = uuid.uuid4().hex
        incoming = generations/(".incoming-" + identity)
        generation = generations/("generation-" + identity)
        incoming.mkdir(mode=0o700)
        pointer = destination.parent/("." + destination.name + ".pointer-" + identity)
        committed = False
        try:
            publish_all([(path, incoming/path.relative_to(source)) for path in members])
            if _document(incoming/"capture-complete.json") != completion:
                raise PublicationError("transport changed capture completion metadata")
            # The pinned cross-filesystem transport copies bytes into new files
            # without preserving execute bits. Restore those from the already
            # validated sender index while the generation is still hidden.
            for relative, entry in completion["files"].items():
                fd = os.open(incoming/relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    mode = os.fstat(fd).st_mode
                    if not stat.S_ISREG(mode):
                        raise PublicationError("transported capture member is not a regular file")
                    os.fchmod(fd, (mode & 0o666) | (0o111 if entry["executable"] else 0))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if not validate(incoming, **expected):
                raise PublicationError("transported capture generation failed validation")
            if sidecars:
                alias = dst_root/sidecars[0].resolve().relative_to(src_root)
                target = Path(destination.name)/"auxiliary/provenance/variant.json"
                if os.path.lexists(alias):
                    if not alias.is_symlink() or os.readlink(alias) != str(target):
                        raise PublicationError("refusing to replace an unmanaged capture sidecar")
                else:
                    alias.symlink_to(target)
            incoming.chmod(0o755)
            os.replace(incoming, generation)
            _directory_sync(generations)
            if before_commit is not None:
                before_commit()
            pointer.symlink_to(Path(generations.name)/generation.name)
            os.replace(pointer, destination)
            committed = True
            _directory_sync(destination.parent)
            history_temporary = generations/(".history-" + identity)
            try:
                history_temporary.write_text(json.dumps(dict(current=generation.name, previous=previous), sort_keys=True))
                os.replace(history_temporary, history_path)
            finally:
                history_temporary.unlink(missing_ok=True)
            for path in generations.iterdir():
                if path.name in [generation.name, previous, "owner.json", "publish.lock", "history.json"]:
                    continue
                if path.is_symlink() or not re.fullmatch(r"generation-[0-9a-f]{32}", path.name):
                    raise PublicationError("unexpected path in owned capture generations")
                shutil.rmtree(path)
            return True
        finally:
            pointer.unlink(missing_ok=True)
            if incoming.exists():
                shutil.rmtree(incoming)
            if not committed and generation.exists():
                shutil.rmtree(generation)
