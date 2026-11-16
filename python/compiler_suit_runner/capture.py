"""Validate schema-1 compiler capture before it can count as dataset output."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import tempfile
import struct
import importlib.util
import zlib

SCHEMA = 1
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_RECORD_BYTES = 4 * 1024**2
MAX_ARTIFACT_BYTES = 128 * 1024**2
MAX_OUTPUT_BYTES = 1024 * 1024**2
MAX_ARTIFACTS = 4096
MAX_RECORDS = 4096
CONFIG_FIELDS = [
    "schema",
    "wrapper_revision",
    "executable",
    "family",
    "version",
    "target",
    "driver",
    "backend",
    "supervisor",
    "debug_policy",
    "nix_response_default",
]
ROLES = {
    "preprocessing",
    "assembly",
    "ir",
    "object",
    "linked-output",
    "command",
    "auxiliary",
    "response",
    "link-map",
    "link-resolution",
    "lto-metadata",
    "lto-native-object",
    "pch",
    "module",
    "module-map",
    "debug-companion",
}
C_SOURCE_SUFFIXES = (
    b".c",
    b".cc",
    b".cp",
    b".cpp",
    b".CPP",
    b".cxx",
    b".c++",
    b".C",
    b".i",
    b".ii",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
NIX_REFERENCE = re.compile(
    rb"/nix/store/([0123456789abcdfghijklmnpqrsvwxyz]{32}|e{32})-"
)
PUBLICATION_SCHEMA = 2
PROVENANCE_SCHEMA = 1

_debug_spec = importlib.util.spec_from_file_location(
    __name__ + "_debug", Path(__file__).with_name("capture_debug.py")
)
_debug = importlib.util.module_from_spec(_debug_spec)
_debug_spec.loader.exec_module(_debug)


def _debug_inspect(path):
    try:
        return _debug.inspect(path)
    except (_debug.DebugError, UnicodeError, struct.error) as error:
        raise CaptureValidationError(
            f"invalid separate debug information: {error}"
        ) from error


def required_contract(family):
    if family not in ["gcc", "clang"]:
        raise CaptureValidationError("unknown captured compiler family")
    return dict(
        schema=SCHEMA,
        wrapper_revision=1,
        policy="required",
        debug_policy="required",
        backend=family + "-trace",
        required_elf_associations=True,
        required_wrapper_identity=True,
        publication_schema=PUBLICATION_SCHEMA,
        provenance_schema=PROVENANCE_SCHEMA,
        separate_debug_info=True,
    )


def captured_spec(spec):
    """Give captured variants a distinct publication and completion identity."""
    result = dict(spec)
    if result.get("compiler_capture") is None:
        result["compiler_capture"] = required_contract(result["compiler_family"])
    suffix = "_capture-v" + str(result["compiler_capture"].get("publication_schema", 1))
    if not result["variant_dir"].endswith(suffix):
        if result["variant_dir"].endswith("_capture-v1"):
            result["variant_dir"] = result["variant_dir"][: -len("_capture-v1")]
        result["variant_dir"] += suffix
    result["metadata_name"] = result["variant_dir"] + ".json"
    return result


class CaptureValidationError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class ValidatedCapture:
    root: Path
    manifest_sha256: str
    artifact_bytes: int
    artifact_count: int
    invocation_count: int
    complete: bool
    manifest: dict


def _integer(value, *, minimum=0, maximum=2**64 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CaptureValidationError("invalid integer in capture metadata")
    return value


def _byte_sequence(value):
    if not isinstance(value, list) or any(
        type(byte) is not int or not 0 <= byte <= 255 for byte in value
    ):
        raise CaptureValidationError("invalid byte sequence in compiler analysis")
    return bytes(value)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8", "backslashreplace")


def _write_provenance(path, value):
    data = _canonical(value) + b"\n"
    if len(data) > 1024**2:
        raise CaptureValidationError("provenance document exceeds its limit")
    Path(path).write_bytes(data)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CaptureValidationError("duplicate key in capture metadata")
        result[key] = value
    return result


def _read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise CaptureValidationError("capture file is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(limit + 1)
        if len(data) > limit:
            raise CaptureValidationError("capture file exceeds its limit")
        return data
    finally:
        os.close(fd)


def _object_hash(path, size):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    count = 0
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise CaptureValidationError("capture artifact has the wrong type or size")
        with os.fdopen(fd, "rb", closefd=False) as file:
            while block := file.read(65536):
                count += len(block)
                if count > size:
                    raise CaptureValidationError(
                        "capture artifact grew while validating"
                    )
                digest.update(block)
        if count != size:
            raise CaptureValidationError("capture artifact changed while validating")
    finally:
        os.close(fd)
    return digest.hexdigest()


def _cfi_transformation_hash(path, size, kind):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        if kind in [
            "clang-riscv-saved-text-alignment",
            "clang14-riscv-saved-text-no-relax",
        ]:
            digest = hashlib.sha256(
                b".text\n.option norelax\n.balign 4\n"
                if kind == "clang14-riscv-saved-text-no-relax"
                else b".text\n.option push\n.option norelax\n.balign 4\n.option pop\n"
            )
            scanned = 0
            while block := file.read(65536):
                scanned += len(block)
                if scanned > size:
                    raise CaptureValidationError(
                        "assembly grew during transformation validation"
                    )
                digest.update(block)
            if scanned != size:
                raise CaptureValidationError(
                    "assembly shrank during transformation validation"
                )
            return digest.hexdigest()
        prefix = None
        scanned = 0
        position = 0
        while line := file.readline(258):
            start = scanned
            scanned += len(line)
            if scanned > size:
                raise CaptureValidationError(
                    "assembly grew during transformation validation"
                )
            if len(line) > 257 or not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = file.readline(258)
                    scanned += len(line)
                    if scanned > size:
                        raise CaptureValidationError(
                            "assembly grew during transformation validation"
                        )
                continue
            if line.lstrip(b" \t").startswith((b".cfi_sections ", b".cfi_sections\t")):
                prefix = line
                position = start
                break
        if prefix is None:
            raise CaptureValidationError("missing compiler-emitted CFI directive")
        file.seek(0)
        if kind == "gcc-4.5-arm-debug-cfi-sections":
            if prefix.split()[1:] != [b".debug_frame"]:
                raise CaptureValidationError("unexpected GCC CFI sections")
            digest = hashlib.sha256()
            remaining = position
            while remaining:
                block = file.read(min(remaining, 65536))
                if not block:
                    raise CaptureValidationError("truncated assembly transformation")
                digest.update(block)
                remaining -= len(block)
            digest.update(b"\t.cfi_sections\t.eh_frame, .debug_frame\n")
            file.seek(position + len(prefix))
            scanned = position + len(prefix)
        else:
            digest = hashlib.sha256(prefix)
            scanned = 0
        while block := file.read(65536):
            scanned += len(block)
            if scanned > size:
                raise CaptureValidationError(
                    "assembly grew during transformation validation"
                )
            digest.update(block)
        if scanned != size:
            raise CaptureValidationError(
                "assembly shrank during transformation validation"
            )
        return digest.hexdigest()


def elf_information(path):
    """Read bounded ELF tables and executable segments for association evidence."""
    with open(path, "rb") as file:
        size = os.fstat(file.fileno()).st_size

        def read(offset, length):
            if offset < 0 or length < 0 or offset + length > size:
                raise CaptureValidationError("ELF table extends beyond the file")
            file.seek(offset)
            data = file.read(length)
            if len(data) != length:
                raise CaptureValidationError("truncated ELF table")
            return data

        header = read(0, min(64, size))
        if (
            len(header) < 64
            or header[:4] != b"\x7fELF"
            or header[4] not in [1, 2]
            or header[5] not in [1, 2]
        ):
            raise CaptureValidationError("invalid captured ELF")
        order = "<" if header[5] == 1 else ">"
        kind, machine = struct.unpack_from(order + "HH", header, 16)
        wide = header[4] == 2
        phoff, shoff = struct.unpack_from(
            order + ("QQ" if wide else "II"), header, 32 if wide else 28
        )
        phsize, phcount, shsize, shcount, names_index = struct.unpack_from(
            order + "HHHHH", header, 54 if wide else 42
        )
        if max(phcount, shcount) > 4096:
            raise CaptureValidationError("ELF tables exceed the analysis limit")
        shfmt = order + ("IIQQQQIIQQ" if wide else "IIIIIIIIII")
        phfmt = order + ("IIQQQQQQ" if wide else "IIIIIIII")
        if (
            shcount
            and shsize < struct.calcsize(shfmt)
            or phcount
            and phsize < struct.calcsize(phfmt)
        ):
            raise CaptureValidationError("invalid ELF table entry size")
        sections = [
            struct.unpack(shfmt, read(shoff + i * shsize, struct.calcsize(shfmt)))
            for i in range(shcount)
        ]
        names = b""
        if sections:
            if names_index >= len(sections) or sections[names_index][5] > 1024**2:
                raise CaptureValidationError("invalid ELF section name table")
            names = read(sections[names_index][4], sections[names_index][5])
        section_names = {
            names[s[0] :].split(b"\0", 1)[0].decode("ascii", "replace")
            for s in sections
            if s[5] and s[0] < len(names)
        }
        # strip changes section headers; patchelf can change interpreter and
        # dynamic-string storage. Retain all other allocated section bytes,
        # flags, addresses, sizes and alignment as independent evidence.
        allocated = []
        reference_erased_allocated = []
        store_references = []
        reference_ranges = []
        mutable_ranges = []
        dynamic_linkage = []
        dynamic_symbols = []

        def string_at(table, index):
            if index >= len(table):
                raise CaptureValidationError("ELF string index exceeds its table")
            end = table.find(b"\0", index)
            if end < 0:
                raise CaptureValidationError("unterminated ELF string")
            return table[index:end].hex()

        for section in sections:
            name = names[section[0] :].split(b"\0", 1)[0]
            if name in [b".interp", b".dynstr", b".dynamic", b".note.gnu.build-id"]:
                mutable_ranges.append((section[4], section[4] + section[5]))
            if section[1] in [6, 11]:  # SHT_DYNAMIC, SHT_DYNSYM
                if section[6] >= len(sections) or section[5] > 1024**2:
                    raise CaptureValidationError("invalid ELF dynamic table")
                strings = sections[section[6]]
                if strings[5] > 1024**2:
                    raise CaptureValidationError("ELF dynamic strings exceed the limit")
                table = read(strings[4], strings[5])
                entries = read(section[4], section[5])
                stride = section[9]
                minimum = (
                    (16 if wide else 8) if section[1] == 6 else (24 if wide else 16)
                )
                if stride < minimum or len(entries) % stride:
                    raise CaptureValidationError("invalid ELF dynamic entry size")
                for cursor in range(0, len(entries), stride):
                    if section[1] == 6:
                        tag, value = struct.unpack_from(
                            order + ("QQ" if wide else "II"), entries, cursor
                        )
                        if tag in [1, 14]:
                            dynamic_linkage.append([tag, string_at(table, value)])
                        elif tag in [30, 0x6FFFFFFB]:
                            dynamic_linkage.append([tag, value])
                    else:
                        dynamic_symbols.append(
                            string_at(
                                table,
                                struct.unpack_from(order + "I", entries, cursor)[0],
                            )
                        )
            if not section[2] & 2 or name in [
                b".interp",
                b".dynstr",
                b".dynamic",
                b".note.gnu.build-id",
            ]:
                continue
            digest = hashlib.sha256()
            erased = hashlib.sha256()
            if section[1] != 8:  # SHT_NOBITS has a memory size, no file bytes.
                for start in range(0, section[5], 65536):
                    length = min(65536, section[5] - start)
                    first = max(0, start - 44)
                    last = min(section[5], start + length + 44)
                    window = bytearray(read(section[4] + first, last - first))
                    shift = start - first
                    digest.update(window[shift : shift + length])
                    for match in NIX_REFERENCE.finditer(window):
                        position = first + match.start(1)
                        if start <= position < start + length:
                            if len(store_references) >= 4096:
                                raise CaptureValidationError(
                                    "ELF store-reference index exceeds its limit"
                                )
                            store_references.append(
                                [name.hex(), position, match[1].decode("ascii")]
                            )
                            reference_ranges.append(
                                (section[4] + position, section[4] + position + 32)
                            )
                        window[match.start(1) : match.end(1)] = b"e" * 32
                    erased.update(window[shift : shift + length])
            allocated.append(
                [
                    name.hex(),
                    section[1],
                    section[2],
                    section[3],
                    section[5],
                    section[8],
                    digest.hexdigest(),
                ]
            )
            reference_erased_allocated.append(
                [
                    name.hex(),
                    section[1],
                    section[2],
                    section[3],
                    section[5],
                    section[8],
                    erased.hexdigest(),
                ]
            )
        build_ids = set()
        code = hashlib.sha256()
        reference_erased_code = hashlib.sha256()
        reference_ranges.sort()
        executable_segments = 0
        for i in range(phcount):
            fields = struct.unpack(
                phfmt, read(phoff + i * phsize, struct.calcsize(phfmt))
            )
            if wide:
                ptype, flags, offset, _, _, length, _, _ = fields
            else:
                ptype, offset, _, _, length, _, flags, _ = fields
            if ptype == 4 and length <= 1024**2:
                notes = read(offset, length)
                cursor = 0
                while cursor + 12 <= len(notes):
                    name_size, desc_size, note_type = struct.unpack_from(
                        order + "III", notes, cursor
                    )
                    cursor += 12
                    name = notes[cursor : cursor + name_size]
                    cursor += (name_size + 3) // 4 * 4
                    desc = notes[cursor : cursor + desc_size]
                    cursor += (desc_size + 3) // 4 * 4
                    if cursor > len(notes):
                        raise CaptureValidationError("truncated ELF note")
                    if name == b"GNU\0" and note_type == 3:
                        build_ids.add(desc.hex())
            if ptype == 1 and flags & 1 and length:
                executable_segments += 1
                code.update(length.to_bytes(8, "little"))
                reference_erased_code.update(length.to_bytes(8, "little"))
                for start in range(0, length, 65536):
                    position = offset + start
                    block = bytearray(read(position, min(65536, length - start)))
                    # Older linkers include the ELF header in an executable
                    # PT_LOAD. strip changes only the section-table fields in
                    # that header. The loader does not use those fields; all
                    # program headers and every other byte still participate.
                    ranges = mutable_ranges + (
                        [(40, 48), (58, 64)] if wide else [(32, 36), (46, 52)]
                    )
                    for first, last in ranges:
                        left = max(position, first)
                        right = min(position + len(block), last)
                        if left < right:
                            block[left - position : right - position] = bytes(
                                right - left
                            )
                    code.update(block)
                    for first, last in reference_ranges:
                        if last <= position:
                            continue
                        if first >= position + len(block):
                            break
                        left = max(position, first)
                        right = min(position + len(block), last)
                        block[left - position : right - position] = b"e" * (
                            right - left
                        )
                    reference_erased_code.update(block)
        return dict(
            elf_class=header[4],
            machine=machine,
            byte_order=header[5],
            type=kind,
            flags=struct.unpack_from(order + "I", header, 48 if wide else 36)[0],
            entrypoint=struct.unpack_from(order + ("Q" if wide else "I"), header, 24)[
                0
            ],
            allocated_sections_sha256=hashlib.sha256(
                _canonical(sorted(allocated))
            ).hexdigest(),
            reference_erased_sections_sha256=hashlib.sha256(
                _canonical(sorted(reference_erased_allocated))
            ).hexdigest(),
            reference_erased_code_sha256=reference_erased_code.hexdigest(),
            store_references=sorted(store_references),
            build_ids=sorted(build_ids),
            executable_segments=executable_segments,
            code_sha256=code.hexdigest(),
            code_normalization="elf-fixup-fields-v1",
            dynamic_linkage=dynamic_linkage,
            dynamic_symbols_sha256=hashlib.sha256(
                _canonical(dynamic_symbols)
            ).hexdigest(),
            debug_info=bool(
                section_names & {".debug_info", ".zdebug_info", ".debug_info.dwo"}
            ),
            debug_line=bool(
                section_names & {".debug_line", ".zdebug_line", ".debug_line.dwo"}
            ),
            symbols=".symtab" in section_names,
        )


def elf_associations(
    root, verified, *, required=False, required_debug=False, expected_toolchain=None
):
    candidates = {}
    producers = {}
    artifact_producers = {}
    for entry in verified.manifest["invocations"]:
        for artifact in entry["record"]["artifacts"]:
            artifact_producers.setdefault(artifact["sha256"], []).append(
                entry["record"]["compiler"]
            )
    for entry in verified.manifest["invocations"]:
        record = entry["record"]
        for artifact in record["artifacts"]:
            if artifact["role"] == "linked-output":
                digest = artifact["sha256"]
                if digest not in candidates:
                    candidates[digest] = elf_information(
                        verified.root / "objects" / digest
                    )
                configs = [record["compiler"]]
                if record["compiler"]["family"] in ["assembler", "linker"]:
                    configs = [
                        config
                        for value in record["artifacts"]
                        if value["role"] in ["object", "ir", "lto-native-object"]
                        for config in artifact_producers.get(value["sha256"], [])
                        if config["family"] in ["gcc", "clang"]
                    ] or configs
                producers.setdefault(digest, []).extend(configs)
    result = {}
    association_count = 0
    for path in sorted(Path(root).iterdir()):
        if not path.is_file() or path.name == "capture-complete.json":
            continue
        info = elf_information(path)
        digest = _object_hash(path.resolve(), path.stat().st_size)
        matches = []
        for artifact, candidate in candidates.items():
            same_abi = all(
                info[key] == candidate[key]
                for key in [
                    "elf_class",
                    "machine",
                    "byte_order",
                    "type",
                    "flags",
                    "entrypoint",
                    "dynamic_linkage",
                    "dynamic_symbols_sha256",
                ]
            )
            same_code = (
                info["executable_segments"]
                and info["code_sha256"] == candidate["code_sha256"]
            )
            same_allocated = (
                info["executable_segments"]
                and info["allocated_sections_sha256"]
                == candidate["allocated_sections_sha256"]
            )
            shared_ids = sorted(set(info["build_ids"]) & set(candidate["build_ids"]))
            rewrites = []
            if (
                same_abi
                and not (same_allocated and same_code)
                and info["reference_erased_sections_sha256"]
                == candidate["reference_erased_sections_sha256"]
                and info["reference_erased_code_sha256"]
                == candidate["reference_erased_code_sha256"]
            ):
                before = candidate["store_references"]
                after = info["store_references"]
                if len(before) == len(after) and all(
                    a[:2] == b[:2] and (a[2] == b[2] or b[2] == "e" * 32)
                    for a, b in zip(before, after)
                ):
                    rewrites = [
                        dict(
                            section=a[0],
                            offset=a[1],
                            original_hash=a[2],
                            installed_hash=b[2],
                        )
                        for a, b in zip(before, after)
                        if a[2] != b[2]
                    ]
            if same_abi and (
                artifact == digest or same_allocated and same_code or rewrites
            ):
                if expected_toolchain is not None and not any(
                    _producer_matches(config, expected_toolchain)
                    for config in producers[artifact]
                ):
                    continue
                if required_debug and not (
                    candidate["debug_line"] and candidate["symbols"]
                ):
                    raise CaptureValidationError(
                        "pre-install ELF lacks required DWARF lines or symbols"
                    )
                match = dict(
                    artifact_sha256=artifact,
                    evidence="identical-file"
                    if artifact == digest
                    else "validated-store-reference-removal"
                    if rewrites
                    else "allocated-sections-and-executable-segments",
                    build_ids=shared_ids,
                    excluded_sections=[
                        ".interp",
                        ".dynstr",
                        ".dynamic",
                        ".note.gnu.build-id",
                    ],
                )
                if rewrites:
                    match["reference_rewrites"] = rewrites
                if expected_toolchain is not None:
                    match["producer"] = expected_toolchain
                association_count += 1
                if expected_toolchain is not None and association_count > MAX_RECORDS:
                    raise CaptureValidationError(
                        "ELF association index exceeds its limit"
                    )
                matches.append(match)
        if required and not matches:
            suffix = (
                " for the selected compiler/target/wrapper"
                if expected_toolchain is not None
                else ""
            )
            raise CaptureValidationError(
                f"installed ELF has no validated pre-install association{suffix}: {path.name}"
            )
        result[path.name] = dict(installed_sha256=digest, elf=info, candidates=matches)
    return result


def _producer_matches(config, expected):
    if config.get("family") in ["gcc", "clang"]:
        return all(
            config.get(key) == expected.get(key)
            for key in ["family", "version", "target", "wrapper"]
        )
    return (
        config.get("family") in ["assembler", "linker"]
        and config.get("target") == expected.get("target")
        and config.get("wrapper") == expected.get("bintools_wrapper")
        and config.get("version") == expected.get("bintools_version")
    )


def _text(value, *, nullable=False, maximum=4096):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise CaptureValidationError("invalid provenance text")
    return value


def validate_provenance(root, *, contract, metadata=None, expected=None, document=None):
    if document is None:
        document = json.loads(
            _read_regular(Path(root) / "provenance.json", 1024**2),
            object_pairs_hook=_unique_keys,
        )
    if (
        not isinstance(document, dict)
        or type(document.get("schema")) is not int
        or document["schema"] != PROVENANCE_SCHEMA
    ):
        raise CaptureValidationError("invalid provenance schema")
    if set(document) != {"schema", "label", "package", "build", "toolchain"}:
        raise CaptureValidationError("invalid provenance fields")
    _text(document["label"])
    package, build, toolchain = (
        document["package"],
        document["build"],
        document["toolchain"],
    )
    if not isinstance(package, dict) or set(package) != {"label", "pname", "version"}:
        raise CaptureValidationError("invalid package provenance")
    _text(package["label"])
    for key in ["pname", "version"]:
        _text(package[key], nullable=True)
    if not isinstance(build, dict) or set(build) != {
        "package_drv",
        "outputs",
        "source",
        "flake_lock_sha256",
        "debug_policy",
    }:
        raise CaptureValidationError("invalid build provenance")
    debug_policy = build["debug_policy"]
    if (
        not isinstance(debug_policy, dict)
        or set(debug_policy) != {"separate_debug_info", "hook_compatibility"}
        or debug_policy["separate_debug_info"] is not True
        or debug_policy["hook_compatibility"]
        not in [None, "clang-3.4-without-hook-assembler-compression"]
    ):
        raise CaptureValidationError("invalid separate-debug provenance policy")
    if not _text(build["package_drv"]).startswith("/nix/store/") or not build[
        "package_drv"
    ].endswith(".drv"):
        raise CaptureValidationError("invalid package derivation identity")
    if not SHA256.fullmatch(_text(build["flake_lock_sha256"])):
        raise CaptureValidationError("invalid provenance lock hash")
    if not isinstance(build["outputs"], dict) or not 1 <= len(build["outputs"]) <= 64:
        raise CaptureValidationError("invalid provenance output index")
    for name, output in build["outputs"].items():
        _text(name, maximum=128)
        if (
            not isinstance(output, dict)
            or set(output) != {"path", "role"}
            or output["role"] not in ["package", "debug", "compiler-capture"]
        ):
            raise CaptureValidationError("invalid provenance output role")
        if not _text(output["path"]).startswith("/nix/store/"):
            raise CaptureValidationError("invalid provenance output identity")
    source = build["source"]
    if not isinstance(source, dict) or set(source) != {"path", "revision"}:
        raise CaptureValidationError("invalid source provenance")
    for value in source.values():
        _text(value, nullable=True)
    if source["path"] is not None and not source["path"].startswith("/nix/store/"):
        raise CaptureValidationError("source provenance is not a store identity")
    if not isinstance(toolchain, dict) or set(toolchain) != {
        "family",
        "version",
        "target",
        "wrapper",
        "bintools_wrapper",
        "bintools_version",
    }:
        raise CaptureValidationError("invalid selected toolchain provenance")
    if toolchain["family"] not in ["gcc", "clang"]:
        raise CaptureValidationError("invalid selected compiler family")
    for key, value in toolchain.items():
        _text(value)
    if not toolchain["wrapper"].startswith("/nix/store/") or not toolchain[
        "bintools_wrapper"
    ].startswith("/nix/store/"):
        raise CaptureValidationError("invalid selected wrapper provenance")
    if debug_policy["hook_compatibility"] is not None and not (
        toolchain["family"] == "clang" and toolchain["version"].startswith("3.4.")
    ):
        raise CaptureValidationError(
            "debug-hook compatibility contradicts the selected compiler"
        )
    if contract.get("backend") != toolchain["family"] + "-trace":
        raise CaptureValidationError("provenance contradicts the compiler contract")
    if metadata is not None:
        if (
            not isinstance(metadata, dict)
            or metadata.get("compilerCapture") != contract
            or metadata.get("expectedToolchain") != toolchain
            or metadata.get("captureProvenance") != document
        ):
            raise CaptureValidationError(
                "provenance contradicts the Nix folder metadata"
            )
        if (
            metadata.get("package") != package["label"]
            or metadata.get("compilerFamily") != toolchain["family"]
            or metadata.get("compilerVersion") != toolchain["version"]
        ):
            raise CaptureValidationError(
                "provenance contradicts the selected package/compiler"
            )
        if expected is not None:
            mapping = {
                "pkg": "package",
                "arch": "arch",
                "compiler_id": "compiler",
                "compiler_family": "compilerFamily",
                "optimization": "optimization",
                "flag_set": "flags",
                "hardening": "hardening",
                "sanitizer": "sanitizer",
                "march": "march",
            }
            if any(
                expected.get(key) not in [None, ""]
                and expected[key] != metadata.get(field)
                for key, field in mapping.items()
            ):
                raise CaptureValidationError(
                    "provenance does not match the requested variant"
                )
            version = expected.get("compiler_version")
            if version and not (
                toolchain["version"] == version
                or toolchain["version"].startswith(version + ".")
            ):
                raise CaptureValidationError(
                    "provenance does not match the requested compiler version"
                )
    return document


def separate_debug_associations(
    debug_root, verified, associations, *, installed_root=None
):
    try:
        files = _debug.debug_files(debug_root) if Path(debug_root).is_dir() else []
    except _debug.DebugError as error:
        raise CaptureValidationError(str(error)) from error
    aliases_by_hash = {}
    debug_by_signature = {}
    inspected = {}
    unit_count = metadata_bytes = file_bytes = 0
    for relative, path in files:
        file_bytes += path.stat().st_size
        if file_bytes > MAX_OUTPUT_BYTES or len(files) > MAX_ARTIFACTS:
            raise CaptureValidationError(
                "separate debug output exceeds its publication limit"
            )
        if path not in inspected:
            details = _debug_inspect(path)
            unit_count += len(details["units"]) + len(details["split_units"])
            metadata_bytes += sum(
                len(value)
                for unit in details["units"] + details["split_units"]
                for key, value in unit.items()
                if key.endswith("_hex")
            )
            if unit_count > MAX_RECORDS or metadata_bytes > 8 * 1024**2:
                raise CaptureValidationError(
                    "separate debug unit index exceeds its limit"
                )
            inspected[path] = (
                elf_information(path),
                details,
                _object_hash(path, path.stat().st_size),
            )
        info, details, file_hash = inspected[path]
        if (
            details["file_backed_code"]
            or not details["units"]
            or not details["line_programs"]
        ):
            raise CaptureValidationError(
                "separate debug ELF lacks usable DWARF units or line tables"
            )
        item = (relative, path, info, details, file_hash)
        signature = (tuple(details["abi"]), tuple(sorted(details["sections"].items())))
        debug_by_signature.setdefault(signature, []).append(item)
        aliases_by_hash.setdefault(file_hash, []).append("auxiliary/debug/" + relative)
    companions = {}
    for entry in verified.manifest["invocations"]:
        for artifact in entry["record"]["artifacts"]:
            if artifact["role"] != "debug-companion":
                continue
            digest = artifact["sha256"]
            if digest in companions:
                continue
            details = _debug_inspect(verified.root / "objects" / digest)
            unit_count += len(details["split_units"])
            if unit_count > MAX_RECORDS:
                raise CaptureValidationError("split-DWARF unit index exceeds its limit")
            companions[digest] = dict(
                abi=details["abi"],
                split_units=[
                    dict(dwo_id=unit["dwo_id"]) for unit in details["split_units"]
                ],
            )
    result = {}
    originals = {}
    checksums = {}
    index_bytes = 0
    for name, association in associations.items():
        matches = []
        debuglink = (
            _debug_inspect(Path(installed_root) / name)["debuglink"]
            if installed_root is not None
            else None
        )
        for candidate in association["candidates"]:
            digest = candidate["artifact_sha256"]
            if digest not in originals:
                originals[digest] = _debug_inspect(verified.root / "objects" / digest)
            original = originals[digest]
            signature = (
                tuple(original["abi"]),
                tuple(sorted(original["sections"].items())),
            )
            for relative, path, info, details, file_hash in debug_by_signature.get(
                signature, []
            ):
                if (
                    info["flags"] != original["elf_flags"]
                    or info["type"] != association["elf"]["type"]
                ):
                    continue
                ids = sorted(
                    set(info["build_ids"]) & set(association["elf"]["build_ids"])
                )
                if not ids:
                    continue
                if debuglink is not None:
                    aliases = [
                        alias
                        for alias in aliases_by_hash[file_hash]
                        if os.fsencode(Path(alias).name).hex() == debuglink["name_hex"]
                    ]
                    if file_hash not in checksums:
                        checksum = 0
                        with path.open("rb") as file:
                            while block := file.read(65536):
                                checksum = zlib.crc32(block, checksum)
                        checksums[file_hash] = checksum
                    if not aliases or checksums[file_hash] != debuglink["crc32"]:
                        continue
                lookup = {}
                for unit in details["units"]:
                    identity = unit["dwo_id"]
                    if identity is None:
                        continue
                    found = [
                        digest
                        for digest, companion in companions.items()
                        if companion["abi"] == details["abi"]
                        and any(
                            value["dwo_id"] == identity
                            for value in companion["split_units"]
                        )
                    ]
                    if not found:
                        raise CaptureValidationError(
                            "separate debug skeleton has no matching split-DWARF companion"
                        )
                    # Different byte content under one unit identity is ambiguous.
                    if len(set(found)) != 1:
                        raise CaptureValidationError(
                            "ambiguous split-DWARF companion identity"
                        )
                    lookup[identity] = "auxiliary/datasetCapture/objects/" + found[0]
                if not any(match["sha256"] == file_hash for match in matches):
                    matches.append(
                        dict(
                            path="auxiliary/debug/" + relative,
                            sha256=file_hash,
                            aliases=sorted(aliases_by_hash[file_hash]),
                            build_ids=ids,
                            captured_elf_sha256=digest,
                            companions=lookup,
                        )
                    )
        if not matches:
            raise CaptureValidationError(
                "installed ELF has no validated separate debug file: " + name
            )
        index_bytes += len(_canonical(matches)) + len(os.fsencode(name))
        if index_bytes > MAX_MANIFEST_BYTES // 2:
            raise CaptureValidationError(
                "separate debug association index exceeds its limit"
            )
        result[name] = matches
    return result, files


def validate_capture(root, *, require_complete=True):
    root = Path(root)
    try:
        raw = _read_regular(root / "manifest.json", MAX_MANIFEST_BYTES)
        manifest = json.loads(raw, object_pairs_hook=_unique_keys)
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("schema")) is not int
            or manifest.get("schema") != SCHEMA
            or type(manifest.get("wrapper_revision")) is not int
            or manifest.get("wrapper_revision") != 1
        ):
            raise CaptureValidationError(
                "unsupported capture schema or wrapper revision"
            )
        complete = manifest.get("complete")
        if type(complete) is not bool or (require_complete and not complete):
            raise CaptureValidationError("required compiler capture is incomplete")
        if manifest.get("policy") not in ["observe", "required"] or not isinstance(
            manifest.get("errors"), list
        ):
            raise CaptureValidationError("invalid capture policy or errors")
        if complete and manifest["errors"]:
            raise CaptureValidationError("complete capture contains errors")
        objects = manifest.get("artifacts")
        records = manifest.get("invocations")
        if (
            not isinstance(objects, dict)
            or len(objects) > MAX_ARTIFACTS
            or not isinstance(records, list)
            or len(records) > MAX_RECORDS
        ):
            raise CaptureValidationError("invalid capture artifact or invocation index")
        object_directory = root / "objects"
        if (
            object_directory.is_symlink()
            or not object_directory.is_dir()
            or set(os.listdir(object_directory)) != set(objects)
        ):
            raise CaptureValidationError(
                "capture contains unindexed artifacts or an invalid objects directory"
            )
        total = 0
        for digest, size in objects.items():
            if not SHA256.fullmatch(digest):
                raise CaptureValidationError("invalid content-addressed artifact ID")
            total += _integer(size, maximum=MAX_ARTIFACT_BYTES)
            if total > MAX_OUTPUT_BYTES:
                raise CaptureValidationError("capture exceeds the output byte limit")
            if _object_hash(root / "objects" / digest, size) != digest:
                raise CaptureValidationError("capture artifact checksum mismatch")
        seen = set()
        referenced = set()
        invocations = 0
        transformation_hashes = {}
        companion_information = {}
        pch_producers = {}
        for entry in records:
            record = entry.get("record", {}) if isinstance(entry, dict) else {}
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("analysis"), dict)
                or not record["analysis"].get("pch")
            ):
                continue
            artifacts = record.get("artifacts")
            if not isinstance(artifacts, list) or not all(
                isinstance(artifact, dict) for artifact in artifacts
            ):
                continue
            sources = {
                _canonical(artifact)
                for artifact in artifacts
                if artifact.get("role") == "preprocessing"
                and artifact.get("path_base") == "invocation-stage"
            }
            for artifact in artifacts:
                if artifact.get("role") == "pch" and isinstance(
                    artifact.get("sha256"), str
                ):
                    pch_producers.setdefault(artifact["sha256"], set()).update(sources)
        for entry in records:
            if not isinstance(entry, dict) or set(entry) != {"id", "count", "record"}:
                raise CaptureValidationError("malformed invocation entry")
            record = entry["record"]
            if (
                not isinstance(record, dict)
                or len(_canonical(record)) > MAX_RECORD_BYTES
                or record.get("schema") != SCHEMA
            ):
                raise CaptureValidationError("invalid invocation record")
            identity = hashlib.sha256(_canonical(record)).hexdigest()
            if entry["id"] != identity or identity in seen:
                raise CaptureValidationError(
                    "invocation identity mismatch or duplicate"
                )
            seen.add(identity)
            invocations += _integer(entry["count"], minimum=1)
            _integer(invocations)
            if type(record.get("capture_complete")) is not bool or (
                complete and not record["capture_complete"]
            ):
                raise CaptureValidationError(
                    "manifest completeness contradicts an invocation"
                )
            config = record.get("compiler")
            config_fields = CONFIG_FIELDS + (
                ["wrapper"] if isinstance(config, dict) and "wrapper" in config else []
            )
            if not isinstance(config, dict) or set(config) != set(config_fields):
                raise CaptureValidationError("invalid compiler configuration")
            # Schema 1 hashes the typed Rust configuration in this declared
            # field order; records themselves use sorted JSON object keys.
            config_bytes = json.dumps(
                {key: config[key] for key in config_fields},
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            if record.get("config_sha256") != hashlib.sha256(config_bytes).hexdigest():
                raise CaptureValidationError("compiler configuration identity mismatch")
            if (
                config["schema"] != SCHEMA
                or config["wrapper_revision"] != 1
                or not all(
                    isinstance(config[key], str) and config[key]
                    for key in CONFIG_FIELDS[2:-1]
                )
            ):
                raise CaptureValidationError("invalid compiler identity fields")
            if type(config["nix_response_default"]) is not bool or config[
                "debug_policy"
            ] not in ["preserve", "required"]:
                raise CaptureValidationError("invalid compiler capture policy")
            if not os.path.isabs(config["executable"]) or not os.path.isabs(
                config["supervisor"]
            ):
                raise CaptureValidationError("compiler identity paths must be absolute")
            if "wrapper" in config and (
                not isinstance(config["wrapper"], str)
                or not os.path.isabs(config["wrapper"])
            ):
                raise CaptureValidationError(
                    "compiler wrapper identity must be an absolute path"
                )
            if config["family"] not in ["gcc", "clang", "assembler", "linker"]:
                raise CaptureValidationError("unknown compiler family")
            if config["backend"] not in [
                "forward",
                "observe",
                "gcc-dumpdir",
                "gcc-trace",
                "clang-trace",
                "tool-observe",
            ]:
                raise CaptureValidationError("unknown compiler capture backend")
            if (
                not isinstance(record.get("capture_errors"), list)
                or record["capture_complete"]
                and record["capture_errors"]
            ):
                raise CaptureValidationError("invalid invocation capture errors")
            status = record.get("compiler_status")
            if not isinstance(status, dict) or set(status) != {"exit_code", "signal"}:
                raise CaptureValidationError("missing compiler status")
            if status["exit_code"] is not None:
                _integer(status["exit_code"], maximum=255)
            if status["signal"] is not None:
                _integer(status["signal"], minimum=1, maximum=127)
            if (status["exit_code"] is None) == (status["signal"] is None):
                raise CaptureValidationError("ambiguous compiler status")
            artifacts = record.get("artifacts")
            if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
                raise CaptureValidationError("invalid invocation artifact list")
            roles = set()
            limits = record.get("limits")
            artifact_limit = 16 * 1024**2
            if limits is not None:
                if not isinstance(limits, dict) or set(limits) != {
                    "artifact_bytes",
                    "invocation_bytes",
                    "total_bytes",
                }:
                    raise CaptureValidationError("invalid invocation capture limits")
                artifact_limit = _integer(
                    limits["artifact_bytes"], minimum=1, maximum=MAX_ARTIFACT_BYTES
                )
                _integer(
                    limits["invocation_bytes"],
                    minimum=artifact_limit,
                    maximum=512 * 1024**2,
                )
                _integer(limits["total_bytes"], minimum=1, maximum=MAX_OUTPUT_BYTES)
            for artifact in artifacts:
                if (
                    not isinstance(artifact, dict)
                    or artifact.get("sha256") not in objects
                ):
                    raise CaptureValidationError(
                        "invocation refers to an absent artifact"
                    )
                if (
                    artifact.get("size") != objects[artifact["sha256"]]
                    or artifact.get("role") not in ROLES
                ):
                    raise CaptureValidationError("artifact role or size mismatch")
                if artifact["size"] > artifact_limit:
                    raise CaptureValidationError(
                        "artifact exceeds its invocation's declared limit"
                    )
                if artifact.get("path_base") not in [
                    "cwd",
                    "invocation-stage",
                    "stdout",
                    "wrapper-response",
                    "compiler-response",
                ]:
                    raise CaptureValidationError("invalid artifact path base")
                bytes.fromhex(artifact["original_path_hex"])
                roles.add(artifact["role"])
                referenced.add(artifact["sha256"])
                if artifact["role"] == "debug-companion":
                    digest = artifact["sha256"]
                    if digest not in companion_information:
                        companion_information[digest] = elf_information(
                            root / "objects" / digest
                        )
                    deferred = (
                        config["family"] == "gcc"
                        and isinstance(record.get("analysis"), dict)
                        and record["analysis"].get("lto") is True
                    )
                    if (
                        companion_information[digest]["type"] != 1
                        or not companion_information[digest]["debug_info"]
                        and not deferred
                    ):
                        raise CaptureValidationError(
                            "split DWARF companion has no retained ELF debug information"
                        )
            transformations = record.get("transformations", [])
            retained = record.get("retained_pch_sources", [])
            if not isinstance(retained, list) or len(retained) > MAX_ARTIFACTS:
                raise CaptureValidationError("invalid retained PCH source index")
            allowed = set().union(
                *(
                    pch_producers.get(artifact["sha256"], set())
                    for artifact in artifacts
                    if artifact["role"] == "pch"
                )
            )
            if any(_canonical(source) not in allowed for source in retained):
                raise CaptureValidationError(
                    "retained PCH source has no matching producer"
                )
            if (
                not isinstance(transformations, list)
                or len(transformations) > MAX_ARTIFACTS
            ):
                raise CaptureValidationError("invalid auxiliary transformation index")
            assembly = {
                artifact["sha256"]
                for artifact in artifacts
                if artifact["role"] == "assembly"
            }
            for transformation in transformations:
                if (
                    not isinstance(transformation, dict)
                    or set(transformation)
                    != {"kind", "emitted_assembly_sha256", "assembler_input_sha256"}
                    or transformation["kind"]
                    not in [
                        "clang-3.8-aarch64-cfi-directive-order",
                        "clang-3.8-cfi-directive-order",
                        "clang-cfi-directive-order",
                        "gcc-4.5-arm-debug-cfi-sections",
                        "clang-riscv-saved-text-alignment",
                        "clang14-riscv-saved-text-no-relax",
                    ]
                ):
                    raise CaptureValidationError("unknown auxiliary transformation")
                kind = transformation["kind"]
                compatible = (
                    (
                        config["family"] == "gcc"
                        and config["version"].startswith("4.5.")
                        and config["target"].startswith("arm")
                        and config["debug_policy"] == "required"
                    )
                    if kind == "gcc-4.5-arm-debug-cfi-sections"
                    else (
                        config["family"] == "clang"
                        and re.match(r"3\.[4-9]\.", config["version"]) is not None
                    )
                )
                if kind == "clang-riscv-saved-text-alignment":
                    compatible = (
                        config["family"] == "clang"
                        and config["target"].startswith("riscv64-")
                        and re.match(r"(?:14|15|16)\.", config["version"]) is not None
                    )
                if kind == "clang14-riscv-saved-text-no-relax":
                    compatible = (
                        config["family"] == "clang"
                        and config["target"].startswith("riscv64-")
                        and config["version"].startswith("14.")
                    )
                if (
                    not compatible
                    or kind == "clang-3.8-aarch64-cfi-directive-order"
                    and not config["target"].startswith("aarch64-")
                ):
                    raise CaptureValidationError(
                        "auxiliary transformation contradicts compiler identity"
                    )
                source = transformation["emitted_assembly_sha256"]
                derived = transformation["assembler_input_sha256"]
                if source not in assembly or derived not in assembly:
                    raise CaptureValidationError(
                        "auxiliary transformation has no retained assembly pair"
                    )
                key = (kind, source)
                if key not in transformation_hashes:
                    transformation_hashes[key] = _cfi_transformation_hash(
                        root / "objects" / source, objects[source], kind
                    )
                if transformation_hashes[key] != derived:
                    raise CaptureValidationError(
                        "assembler input does not match the declared CFI correction"
                    )
            analysis = record.get("analysis")
            if not isinstance(analysis, dict) or not isinstance(
                analysis.get("inputs", []), list
            ):
                raise CaptureValidationError("invalid compiler analysis")
            inputs = [_byte_sequence(item) for item in analysis.get("inputs", [])]
            language = analysis.get("language")
            language = _byte_sequence(language) if language is not None else None
            if analysis.get("output") is not None:
                _byte_sequence(analysis["output"])
            languages = analysis.get("input_languages")
            if languages is not None:
                if not isinstance(languages, list) or len(languages) != len(inputs):
                    raise CaptureValidationError("invalid per-input language index")
                languages = [
                    _byte_sequence(item) if item is not None else None
                    for item in languages
                ]
            if "split_dwarf" in analysis and type(analysis["split_dwarf"]) is not bool:
                raise CaptureValidationError("invalid split DWARF analysis")
            debug = analysis.get("debug", [])
            if not isinstance(debug, list):
                raise CaptureValidationError("invalid compiler debug options")
            inferred_split = False
            for option in debug:
                option = _byte_sequence(option)
                if option in [b"-gsplit-dwarf", b"-gsplit-dwarf=split"]:
                    inferred_split = True
                elif option in [b"-gno-split-dwarf", b"-gsplit-dwarf=single"]:
                    inferred_split = False
            if (
                "debug" in analysis
                and "split_dwarf" in analysis
                and analysis["split_dwarf"] != inferred_split
            ):
                raise CaptureValidationError(
                    "split DWARF analysis contradicts its debug options"
                )
            split_dwarf = analysis.get("split_dwarf", inferred_split)
            if "lto" in analysis and type(analysis["lto"]) is not bool:
                raise CaptureValidationError("invalid LTO analysis")
            c_source = any(
                input_language in [b"c", b"c++", b"cpp-output", b"c++-cpp-output"]
                if input_language is not None
                else item.endswith(C_SOURCE_SUFFIXES)
                for item, input_language in zip(
                    inputs,
                    languages if languages is not None else [language] * len(inputs),
                )
            )
            if status["exit_code"] == 0 and record["capture_complete"]:
                mode = analysis.get("mode")
                preprocessed = bool(inputs) and all(
                    input_language
                    in [
                        b"cpp-output",
                        b"c++-cpp-output",
                        b"c++-module-cpp-output",
                        b"assembler",
                        b"pcm",
                    ]
                    if input_language is not None
                    else item.endswith((b".i", b".ii", b".iim", b".s", b".pcm"))
                    for item, input_language in zip(
                        inputs,
                        languages
                        if languages is not None
                        else [language] * len(inputs),
                    )
                )
                native_from_ir = record.get("native_from_ir", False)
                if (
                    type(native_from_ir) is not bool
                    or native_from_ir
                    and (
                        mode not in ["compile", "link"]
                        or not c_source
                        or config["family"] != "clang"
                        or analysis.get("saved_temps")
                        or analysis.get("pch")
                    )
                ):
                    raise CaptureValidationError(
                        "native IR backend contradicts compiler analysis"
                    )
                expected = {
                    "compile": {"object", "assembly"},
                    "assembly": {"assembly"},
                    "preprocess": {"preprocessing"},
                    "link": {"linked-output"},
                    "query": set(),
                    "dependencies": set(),
                    "syntax": set(),
                }.get(mode)
                if (
                    expected is not None
                    and analysis.get("lto")
                    and config["family"] == "clang"
                ):
                    expected.discard("assembly")
                    expected.add("ir")
                if native_from_ir:
                    expected.discard("assembly")
                    expected.add("ir")
                    for artifact in artifacts:
                        if artifact["role"] != "ir":
                            continue
                        descriptor = os.open(
                            root / "objects" / artifact["sha256"],
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        )
                        try:
                            magic = os.read(descriptor, 4)
                        finally:
                            os.close(descriptor)
                        if magic not in [b"BC\xc0\xde", b"\xde\xc0\x17\x0b"]:
                            raise CaptureValidationError(
                                "native backend input is not LLVM bitcode"
                            )
                if mode == "compile" and analysis.get("pch"):
                    expected = {"pch"}
                if mode == "compile" and analysis.get("module_only"):
                    expected = {"module"}
                if mode == "link" and analysis.get("relocatable"):
                    expected = {"object"}
                if mode == "link" and analysis.get("link_output_role") is not None:
                    role = analysis["link_output_role"]
                    if role not in ["object", "ir", "linked-output"]:
                        raise CaptureValidationError("invalid linked output role")
                    expected = {role}
                if analysis.get("emit_llvm") and mode in ["compile", "assembly"]:
                    expected = {"ir"}
                deferred_split = False
                if (
                    split_dwarf
                    and mode == "compile"
                    and config["family"] == "clang"
                    and (
                        analysis.get("lto") is True or analysis.get("emit_llvm") is True
                    )
                ):
                    # A bitcode-only frontend has not generated a skeleton or
                    # native companion yet. Require the DWO at native emission,
                    # not an invented placeholder alongside the LLVM object.
                    code_outputs = [
                        artifact
                        for artifact in artifacts
                        if artifact["role"] == "object"
                    ] or [
                        artifact for artifact in artifacts if artifact["role"] == "ir"
                    ]
                    deferred_split = bool(code_outputs)
                    for artifact in code_outputs:
                        descriptor = os.open(
                            root / "objects" / artifact["sha256"],
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        )
                        try:
                            magic = os.read(descriptor, 4)
                        finally:
                            os.close(descriptor)
                        deferred_split &= magic in [b"BC\xc0\xde", b"\xde\xc0\x17\x0b"]
                if (
                    expected is not None
                    and split_dwarf
                    and mode in ["compile", "link"]
                    and c_source
                    and not deferred_split
                ):
                    expected.add("debug-companion")
                    if mode == "link" and not any(
                        companion_information[artifact["sha256"]]["debug_info"]
                        for artifact in artifacts
                        if artifact["role"] == "debug-companion"
                    ):
                        raise CaptureValidationError(
                            "split DWARF link has no retained compilation-unit information"
                        )
                if expected is not None and analysis.get("output") == list(
                    b"/dev/null"
                ):
                    discarded = {
                        "compile": "object",
                        "assembly": "assembly",
                        "preprocess": "preprocessing",
                        "link": "linked-output",
                    }.get(mode)
                    expected.discard(discarded)
                if mode in ["compile", "assembly"] and config["backend"] in [
                    "gcc-dumpdir",
                    "gcc-trace",
                    "clang-trace",
                ]:
                    if not preprocessed:
                        expected.add("preprocessing")
                    version = re.match(r"(\d+)\.(\d+)", config["version"])
                    # Clang 3.4/3.5 save preprocessing and assembly, without
                    # the separate bitcode stage emitted by 3.6 and later.
                    emits_ir = version is not None and tuple(
                        map(int, version.groups())
                    ) >= (3, 6)
                    if (
                        config["backend"] == "clang-trace"
                        and emits_ir
                        and c_source
                        and not preprocessed
                        and not analysis.get("pch")
                    ):
                        expected.add("ir")
                if (
                    mode == "link"
                    and analysis.get("lto")
                    and config["family"] in ["clang", "linker"]
                    and "ir" in roles
                    and analysis.get("link_output_role") != "ir"
                ):
                    # Linker plugins produce native objects in-process. A
                    # frontend bitcode .o cannot satisfy that capture role.
                    native = "lto-native-object" in roles
                    if not native:
                        produced = {
                            digest
                            for command in record.get("subprocesses", [])
                            for digest in command.get("outputs", [])
                        }
                        for artifact in artifacts:
                            if (
                                artifact["role"] != "object"
                                or artifact["sha256"] in produced
                            ):
                                continue
                            descriptor = os.open(
                                root / "objects" / artifact["sha256"],
                                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            )
                            with os.fdopen(descriptor, "rb") as file:
                                header = file.read(18)
                            if (
                                len(header) >= 18
                                and header[:4] == b"\x7fELF"
                                and header[5] in [1, 2]
                                and struct.unpack_from(
                                    ("<" if header[5] == 1 else ">") + "H", header, 16
                                )[0]
                                == 1
                            ):
                                native = True
                                break
                    if not native:
                        raise CaptureValidationError(
                            "LLVM LTO has no retained linker-generated native object"
                        )
                if expected is None or not expected <= roles:
                    raise CaptureValidationError(
                        f"required artifact roles are missing for {mode}: expected {sorted(expected or [])}, retained {sorted(roles)}"
                    )
        if referenced != set(objects):
            raise CaptureValidationError("capture contains unassociated artifacts")
        return ValidatedCapture(
            root,
            hashlib.sha256(raw).hexdigest(),
            total,
            len(objects),
            invocations,
            complete,
            manifest,
        )
    except CaptureValidationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        raise CaptureValidationError(f"invalid compiler capture: {error}") from error


def _validate_contract(verified, contract):
    if (
        not isinstance(contract, dict)
        or type(contract.get("schema")) is not int
        or contract["schema"] != SCHEMA
    ):
        raise CaptureValidationError("missing capture contract")
    if any(
        verified.manifest.get(key) != contract[key]
        for key in ["policy", "wrapper_revision"]
        if key in contract
    ):
        raise CaptureValidationError(
            "capture manifest does not match the requested contract"
        )
    for entry in verified.manifest["invocations"]:
        config = entry["record"]["compiler"]
        if contract.get("required_wrapper_identity") and not config.get("wrapper"):
            raise CaptureValidationError("capture has no selected-wrapper identity")
        policy_keys = ["wrapper_revision", "debug_policy", "backend"]
        if (
            config["family"] in ["assembler", "linker"]
            and config["backend"] == "tool-observe"
        ):
            policy_keys.remove("backend")
        if any(
            config.get(key) != contract[key] for key in policy_keys if key in contract
        ):
            raise CaptureValidationError(
                "capture configuration does not match the requested contract"
            )
    revision = contract.get("publication_schema", 1)
    if type(revision) is not int or revision not in [1, PUBLICATION_SCHEMA]:
        raise CaptureValidationError("unsupported capture publication schema")
    if revision == PUBLICATION_SCHEMA and (
        type(contract.get("provenance_schema")) is not int
        or contract.get("provenance_schema") != PROVENANCE_SCHEMA
        or contract.get("separate_debug_info") is not True
        or contract.get("required_elf_associations") is not True
    ):
        raise CaptureValidationError("incomplete separate-debug/provenance contract")


def _publication_role(relative):
    return (
        "compiler-capture"
        if relative.startswith("auxiliary/datasetCapture/")
        else "debug"
        if relative.startswith("auxiliary/debug/")
        else "provenance"
        if relative.startswith("auxiliary/provenance/")
        else "installed-elf"
    )


def stage_captured_variant(
    out_store,
    destination,
    variant_dir,
    *,
    label,
    drv,
    contract,
    expected=None,
    metadata=None,
):
    """Stage a validated generation. The framework publishes this whole file set.

    The completion record includes every staged byte. A legacy sidecar, a
    partial retry, or a directory copied without its metadata cannot satisfy it.
    """
    import fcntl

    out_store, destination = Path(out_store), Path(destination)
    if (
        not variant_dir
        or Path(variant_dir).name != variant_dir
        or variant_dir in [".", ".."]
    ):
        raise CaptureValidationError("invalid variant directory")
    source = out_store / "auxiliary" / "datasetCapture"
    verified = validate_capture(source, require_complete=True)
    _validate_contract(verified, contract)
    revision = contract.get("publication_schema", 1)
    provenance = folder_metadata = None
    if revision >= 2:
        _text(label)
        if not _text(drv).startswith("/nix/store/") or not drv.endswith(".drv"):
            raise CaptureValidationError(
                "missing requested ELF-folder derivation identity"
            )
        folder_metadata = json.loads(
            _read_regular(out_store / "meta.json", 1024**2),
            object_pairs_hook=_unique_keys,
        )
        provenance = validate_provenance(
            out_store, contract=contract, metadata=folder_metadata, expected=expected
        )
    configurations = sorted(
        {entry["record"]["config_sha256"] for entry in verified.manifest["invocations"]}
    )
    compiler_identities = {
        _canonical(
            {
                key: entry["record"]["compiler"].get(key)
                for key in ["family", "version", "target", "wrapper"]
            }
        )
        for entry in verified.manifest["invocations"]
    }
    destination.mkdir(parents=True, exist_ok=True)
    # This lock belongs to staging, never to the published corpus.
    with (destination / ("." + variant_dir + ".capture.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        temporary = Path(
            tempfile.mkdtemp(prefix="." + variant_dir + ".capture-", dir=destination)
        )
        final = destination / variant_dir
        backup = destination / ("." + variant_dir + ".previous")
        try:
            # These directories are owned exclusively by this variant's lock.
            for stale in destination.glob("." + variant_dir + ".capture-*"):
                if stale != temporary and stale.is_dir() and not stale.is_symlink():
                    shutil.rmtree(stale)
            if backup.exists():
                if not final.exists():
                    os.replace(backup, final)
                else:
                    shutil.rmtree(backup)
            capture_dir = temporary / "auxiliary" / "datasetCapture"
            capture_dir.mkdir(parents=True)
            (capture_dir / "objects").mkdir()
            shutil.copyfile(source / "manifest.json", capture_dir / "manifest.json")
            for digest in verified.manifest["artifacts"]:
                shutil.copyfile(
                    source / "objects" / digest, capture_dir / "objects" / digest
                )
            validate_capture(capture_dir)
            elf_dir = out_store / "elf"
            if not elf_dir.is_dir():
                raise CaptureValidationError("missing executable corpus")
            if any(elf_dir.iterdir()) and not verified.invocation_count:
                raise CaptureValidationError(
                    "executable corpus has no captured compiler invocations"
                )
            published_count = verified.artifact_count + 1
            elf_bytes = 0
            for elf in sorted(elf_dir.iterdir()):
                if elf.name in ["auxiliary", "capture-complete.json"]:
                    raise CaptureValidationError(
                        "executable name conflicts with publication metadata"
                    )
                if not elf.is_file():
                    raise CaptureValidationError("invalid executable corpus member")
                published_count += 1
                elf_bytes += elf.stat().st_size
                if (
                    published_count > MAX_ARTIFACTS + MAX_RECORDS
                    or elf_bytes > MAX_OUTPUT_BYTES
                ):
                    raise CaptureValidationError(
                        "executable corpus exceeds the publication limit"
                    )
                shutil.copy2(elf, temporary / elf.name)
            associations = elf_associations(
                temporary,
                verified,
                required=contract.get("required_elf_associations", False),
                required_debug=contract.get("required_elf_associations", False)
                and contract.get("debug_policy") == "required",
                expected_toolchain=provenance["toolchain"]
                if provenance is not None
                else None,
            )
            debug_associations = None
            selected_debug = None
            if revision >= 2:
                debug_associations, selected_debug = separate_debug_associations(
                    out_store / "auxiliary" / "debug",
                    verified,
                    associations,
                    installed_root=temporary,
                )
                directory = temporary / "auxiliary" / "provenance"
                directory.mkdir(parents=True)
                _write_provenance(directory / "build.json", provenance)
                _write_provenance(directory / "metadata.json", folder_metadata)
                if metadata is not None:
                    summary = dict(
                        schema=SCHEMA,
                        contract=contract,
                        manifest_sha256=verified.manifest_sha256,
                        config_sha256=configurations,
                        artifact_count=verified.artifact_count,
                        artifact_bytes=verified.artifact_bytes,
                        invocation_count=verified.invocation_count,
                        compilers=[
                            json.loads(value) for value in sorted(compiler_identities)
                        ],
                        provenance=provenance,
                        debug_associations=debug_associations,
                        complete=True,
                    )
                    sidecar = dict(metadata, compiler_capture=summary)
                    _write_provenance(directory / "variant.json", sidecar)
            # Debug output has its own role and never enters the flat ELF set.
            debug_source = out_store / "auxiliary" / "debug"
            if debug_source.is_dir():
                debug_total = 0
                debug_directories = set()
                paths = (
                    [(relative, path) for relative, path in selected_debug]
                    if selected_debug is not None
                    else [
                        (path.relative_to(debug_source), path)
                        for path in sorted(debug_source.rglob("*"))
                    ]
                )
                for relative, path in paths:
                    if path.is_dir():
                        continue
                    debug_total += path.stat().st_size
                    published_count += 1
                    relative = Path(relative)
                    debug_directories.update(relative.parents)
                    if (
                        debug_total > MAX_OUTPUT_BYTES
                        or published_count > MAX_ARTIFACTS + MAX_RECORDS
                        or len(debug_directories) > MAX_ARTIFACTS
                        or len(relative.parts) > 32
                        or not path.is_file()
                    ):
                        raise CaptureValidationError(
                            "debug output exceeds the publication limit"
                        )
                    target = temporary / "auxiliary" / "debug" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            files = {}
            total_bytes = 0
            for path in sorted(temporary.rglob("*")):
                if path.is_file():
                    info = path.stat()
                    size = info.st_size
                    total_bytes += size
                    if (revision >= 2 and total_bytes > MAX_OUTPUT_BYTES) or len(
                        files
                    ) >= MAX_ARTIFACTS + MAX_RECORDS:
                        raise CaptureValidationError(
                            "captured publication exceeds its total byte/file limit"
                        )
                    relative = path.relative_to(temporary).as_posix()
                    files[relative] = dict(
                        size=size,
                        sha256=_object_hash(path, size),
                        role=_publication_role(relative),
                        executable=bool(info.st_mode & 0o111),
                    )
            completion = dict(
                schema=SCHEMA,
                complete=True,
                label=label,
                drv=drv,
                contract=contract,
                config_sha256=configurations,
                manifest_sha256=verified.manifest_sha256,
                files=files,
                elf_associations=associations,
            )
            if revision >= 2:
                completion.update(
                    publication_schema=revision,
                    provenance=provenance,
                    debug_associations=debug_associations,
                )
            completion_bytes = _canonical(completion) + b"\n"
            if len(completion_bytes) > MAX_MANIFEST_BYTES:
                raise CaptureValidationError(
                    "publication completion record exceeds its limit"
                )
            (temporary / "capture-complete.json").write_bytes(completion_bytes)
            if final.exists():
                if final.is_symlink() or not final.is_dir():
                    raise CaptureValidationError("invalid existing staging directory")
                os.replace(final, backup)
            try:
                os.replace(temporary, final)
            except BaseException:
                if backup.exists():
                    os.replace(backup, final)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            staged = [final / relative for relative in files]
            staged.append(final / "capture-complete.json")
            return staged, dict(
                schema=SCHEMA,
                contract=contract,
                manifest_sha256=verified.manifest_sha256,
                config_sha256=configurations,
                artifact_count=verified.artifact_count,
                artifact_bytes=verified.artifact_bytes,
                invocation_count=verified.invocation_count,
                compilers=[json.loads(value) for value in sorted(compiler_identities)],
                complete=True,
                **(
                    dict(provenance=provenance, debug_associations=debug_associations)
                    if revision >= 2
                    else {}
                ),
            )
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def captured_variant_complete(root, *, label, drv, contract, expected=None):
    """Check publication bytes before reusing a captured dataset result."""
    root = Path(root)
    try:
        root = root.resolve(strict=True)
        completion = json.loads(
            _read_regular(root / "capture-complete.json", MAX_MANIFEST_BYTES),
            object_pairs_hook=_unique_keys,
        )
        if (
            not isinstance(completion, dict)
            or type(completion.get("schema")) is not int
            or completion.get("complete") is not True
        ):
            return False
        if any(
            completion.get(key) != expected
            for key, expected in dict(
                schema=SCHEMA, complete=True, label=label, drv=drv, contract=contract
            ).items()
        ):
            return False
        files = completion["files"]
        if not isinstance(files, dict) or len(files) > MAX_ARTIFACTS + MAX_RECORDS:
            return False
        total_bytes = 0
        for relative, item in files.items():
            path = Path(relative)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.as_posix() != relative
                or any(
                    (root / Path(*path.parts[:i])).is_symlink()
                    for i in range(1, len(path.parts) + 1)
                )
            ):
                return False
            if (
                not isinstance(item, dict)
                or item.get("role") != _publication_role(relative)
                or type(item.get("executable")) is not bool
            ):
                return False
            size = _integer(item["size"], maximum=MAX_OUTPUT_BYTES)
            total_bytes += size
            if (
                contract.get("publication_schema", 1) >= 2
                and total_bytes > MAX_OUTPUT_BYTES
            ):
                return False
            if (
                bool((root / path).stat().st_mode & 0o111) != item["executable"]
                or _object_hash(root / path, size) != item["sha256"]
            ):
                return False
        actual = {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_symlink() or not p.is_dir()
        }
        if actual != set(files) | {"capture-complete.json"}:
            return False
        verified = validate_capture(root / "auxiliary" / "datasetCapture")
        _validate_contract(verified, contract)
        provenance = None
        if contract.get("publication_schema", 1) >= 2:
            if completion.get("publication_schema") != PUBLICATION_SCHEMA:
                return False
            provenance_directory = root / "auxiliary" / "provenance"
            document = json.loads(
                _read_regular(provenance_directory / "build.json", 1024**2),
                object_pairs_hook=_unique_keys,
            )
            metadata = json.loads(
                _read_regular(provenance_directory / "metadata.json", 1024**2),
                object_pairs_hook=_unique_keys,
            )
            # The common validator reads the raw-folder filename. Use an
            # explicit document argument instead of trusting the completion copy.
            provenance = validate_provenance(
                provenance_directory,
                contract=contract,
                metadata=metadata,
                expected=expected,
                document=document,
            )
            if provenance != completion.get("provenance"):
                return False
        associations = elf_associations(
            root,
            verified,
            required=contract.get("required_elf_associations", False),
            required_debug=contract.get("required_elf_associations", False)
            and contract.get("debug_policy") == "required",
            expected_toolchain=provenance["toolchain"]
            if provenance is not None
            else None,
        )
        if provenance is not None:
            debug_associations, _ = separate_debug_associations(
                root / "auxiliary" / "debug",
                verified,
                associations,
                installed_root=root,
            )
            if debug_associations != completion.get("debug_associations"):
                return False
            sidecar_path = root / "auxiliary/provenance/variant.json"
            if sidecar_path.is_file():
                sidecar = json.loads(
                    _read_regular(sidecar_path, 1024**2), object_pairs_hook=_unique_keys
                )
                if (
                    not isinstance(sidecar, dict)
                    or sidecar.get("label") != label
                    or sidecar.get("drv") != drv
                ):
                    return False
                requested = dict(sidecar, compiler_id=sidecar.get("compiler"))
                validate_provenance(
                    provenance_directory,
                    contract=contract,
                    metadata=metadata,
                    expected=requested,
                    document=document,
                )
                summary = sidecar.get("compiler_capture")
                if not isinstance(summary, dict) or any(
                    summary.get(key) != value
                    for key, value in dict(
                        provenance=provenance,
                        contract=contract,
                        manifest_sha256=verified.manifest_sha256,
                        debug_associations=debug_associations,
                        complete=True,
                    ).items()
                ):
                    return False
        return (
            associations == completion.get("elf_associations")
            and verified.manifest_sha256 == completion["manifest_sha256"]
            and sorted(
                {
                    entry["record"]["config_sha256"]
                    for entry in verified.manifest["invocations"]
                }
            )
            == completion["config_sha256"]
        )
    except (
        CaptureValidationError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
    ):
        return False
