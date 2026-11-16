"""Bounded ELF/DWARF inspection for independent debug-file associations."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import zlib

MAX_SECTION = 64 * 1024**2
MAX_DEBUG = 128 * 1024**2
MAX_UNITS = 4096


class DebugError(ValueError):
    pass


class Sections:
    def __init__(self, path):
        self.file = open(path, "rb")
        self.size = os.fstat(self.file.fileno()).st_size
        self.cache = {}
        self.total = 0
        try:
            header = self.read(0, min(64, self.size))
            if len(header) < 52 or header[:4] != b"\x7fELF" or header[4] not in (1, 2) or header[5] not in (1, 2):
                raise DebugError("invalid debug ELF")
            self.wide = header[4] == 2
            self.order = "<" if header[5] == 1 else ">"
            self.abi = [header[4], header[5], struct.unpack_from(self.order + "H", header, 18)[0]]
            self.flags = struct.unpack_from(self.order + "I", header, 48 if self.wide else 36)[0]
            offset = struct.unpack_from(self.order + ("Q" if self.wide else "I"), header, 40 if self.wide else 32)[0]
            stride, count, strings = struct.unpack_from(self.order + "HHH", header, 58 if self.wide else 46)
            fmt = self.order + ("IIQQQQIIQQ" if self.wide else "IIIIIIIIII")
            if not 0 < count <= 4096 or stride < struct.calcsize(fmt) or strings >= count:
                raise DebugError("invalid debug ELF section table")
            rows = [struct.unpack(fmt, self.read(offset + i * stride, struct.calcsize(fmt))) for i in range(count)]
            self.file_backed_code = any(row[2] & 4 and row[1] != 8 and row[5] for row in rows)
            names = self.read(rows[strings][4], rows[strings][5], maximum=1024**2)
            self.headers = {}
            self.other = {}
            for row in rows:
                if row[0] >= len(names):
                    if row[0] == 0:
                        continue
                    raise DebugError("invalid debug section name")
                end = names.find(b"\0", row[0])
                if end < 0:
                    raise DebugError("unterminated debug section name")
                name = names[row[0]:end].decode("ascii", "surrogateescape")
                if name.startswith(".zdebug_"):
                    name = ".debug_" + name[8:]
                if name.startswith(".debug_"):
                    if name in self.headers:
                        raise DebugError("duplicate debug section")
                    self.headers[name] = row
                elif name == ".gnu_debuglink":
                    self.other[name] = row
        except BaseException:
            self.file.close()
            raise

    def close(self):
        self.file.close()

    def read(self, offset, length, *, maximum=MAX_SECTION):
        if offset < 0 or length < 0 or length > maximum or offset + length > self.size:
            raise DebugError("debug section exceeds its bounds")
        self.file.seek(offset)
        result = self.file.read(length)
        if len(result) != length:
            raise DebugError("truncated debug section")
        return result

    def get(self, name):
        if name in self.cache:
            return self.cache[name]
        row = self.headers.get(name) or self.other.get(name)
        if row is None or not row[5]:
            return b""
        if row[1] == 8:
            raise DebugError("debug data is not file backed")
        data = self.read(row[4], row[5])
        expanded = None
        if row[2] & 0x800:
            fmt = self.order + ("IIQQ" if self.wide else "III")
            length = struct.calcsize(fmt)
            if len(data) < length:
                raise DebugError("truncated compressed debug header")
            fields = struct.unpack_from(fmt, data)
            if fields[0] != 1:
                raise DebugError("unsupported debug compression")
            expanded = fields[2] if self.wide else fields[1]
            data = data[length:]
        elif data.startswith(b"ZLIB"):
            if len(data) < 12:
                raise DebugError("truncated GNU compressed debug header")
            expanded = int.from_bytes(data[4:12], "big")
            data = data[12:]
        if expanded is not None:
            if expanded > MAX_SECTION:
                raise DebugError("expanded debug section exceeds its limit")
            decoder = zlib.decompressobj()
            try:
                data = decoder.decompress(data, expanded + 1)
            except zlib.error as error:
                raise DebugError("invalid compressed debug section") from error
            if len(data) != expanded or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise DebugError("compressed debug length mismatch")
        self.total += len(data)
        if self.total > MAX_DEBUG:
            raise DebugError("debug data exceeds its analysis limit")
        self.cache[name] = data
        return data


def _number(data, cursor, width, order):
    if width not in (1, 2, 3, 4, 8, 16) or cursor + width > len(data):
        raise DebugError("truncated DWARF value")
    return int.from_bytes(data[cursor:cursor + width], "little" if order == "<" else "big"), cursor + width


def _leb(data, cursor, *, signed=False):
    result = shift = 0
    for _ in range(10):
        if cursor >= len(data):
            raise DebugError("truncated DWARF integer")
        byte = data[cursor]
        cursor += 1
        result |= (byte & 127) << shift
        shift += 7
        if not byte & 128:
            if signed and byte & 64:
                result -= 1 << shift
            return result, cursor
    raise DebugError("oversized DWARF integer")


def _string(data, cursor):
    end = data.find(b"\0", cursor)
    if cursor < 0 or end < 0 or end - cursor > 1024**2:
        raise DebugError("invalid DWARF string")
    return data[cursor:end], end + 1


def _length(data, cursor, order):
    length, cursor = _number(data, cursor, 4, order)
    width = 4
    if length == 0xffffffff:
        length, cursor = _number(data, cursor, 8, order)
        width = 8
    elif length >= 0xfffffff0:
        raise DebugError("reserved DWARF unit length")
    if not length or cursor + length > len(data):
        raise DebugError("invalid DWARF unit length")
    return cursor + length, cursor, width


def _abbreviation(data, offset, wanted):
    cursor = offset
    for _ in range(65536):
        code, cursor = _leb(data, cursor)
        if not code:
            break
        tag, cursor = _leb(data, cursor)
        _, cursor = _number(data, cursor, 1, "<")
        fields = []
        for _ in range(1024):
            attr, cursor = _leb(data, cursor)
            form, cursor = _leb(data, cursor)
            if not attr and not form:
                break
            implicit = None
            if form == 0x21:
                implicit, cursor = _leb(data, cursor, signed=True)
            fields.append((attr, form, implicit))
        else:
            raise DebugError("oversized DWARF abbreviation")
        if code == wanted:
            return tag, fields
    raise DebugError("missing DWARF abbreviation")


def _form(data, cursor, form, width, address_size, order, implicit=None, depth=0, version=4):
    if depth > 4:
        raise DebugError("nested DWARF indirect forms")
    fixed = {0x05: 2, 0x06: 4, 0x07: 8, 0x0b: 1, 0x0c: 1, 0x11: 1, 0x12: 2,
             0x13: 4, 0x14: 8, 0x1c: 4, 0x1e: 16, 0x20: 8, 0x24: 8,
             0x25: 1, 0x26: 2, 0x27: 3, 0x28: 4, 0x29: 1, 0x2a: 2, 0x2b: 3, 0x2c: 4}
    if form == 0x01:
        return _number(data, cursor, address_size, order)
    if form in fixed:
        return _number(data, cursor, fixed[form], order)
    if form == 0x10:
        return _number(data, cursor, address_size if version == 2 else width, order)
    if form in (0x0e, 0x17, 0x1d, 0x1f):
        return _number(data, cursor, width, order)
    if form in (0x0f, 0x15, 0x1a, 0x1b, 0x22, 0x23, 0x1f01, 0x1f02):
        return _leb(data, cursor)
    if form == 0x0d:
        return _leb(data, cursor, signed=True)
    if form == 0x08:
        return _string(data, cursor)
    if form == 0x19:
        return 1, cursor
    if form == 0x21:
        return implicit, cursor
    if form == 0x16:
        actual, cursor = _leb(data, cursor)
        return _form(data, cursor, actual, width, address_size, order, depth=depth + 1, version=version)
    if form in (0x03, 0x04, 0x09, 0x0a, 0x18):
        length, cursor = (_leb(data, cursor) if form in (0x09, 0x18) else
                          _number(data, cursor, {0x03: 2, 0x04: 4, 0x0a: 1}[form], order))
        if cursor + length > len(data):
            raise DebugError("truncated DWARF block")
        return data[cursor:cursor + length], cursor + length
    raise DebugError(f"unsupported DWARF form {form:#x}")


def _units(sections, suffix):
    data = sections.get(".debug_info" + suffix)
    abbreviations = sections.get(".debug_abbrev" + suffix)
    result = []
    cursor = 0
    count = 0
    string_bytes = 0
    while cursor < len(data):
        count += 1
        if count > MAX_UNITS:
            raise DebugError("too many DWARF units")
        end, cursor, width = _length(data, cursor, sections.order)
        unit_data = data
        version, cursor = _number(unit_data, cursor, 2, sections.order)
        if version not in (2, 3, 4, 5):
            raise DebugError("unsupported DWARF unit version")
        dwo_id = None
        if version == 5:
            kind, cursor = _number(unit_data, cursor, 1, sections.order)
            address_size, cursor = _number(unit_data, cursor, 1, sections.order)
            abbrev, cursor = _number(unit_data, cursor, width, sections.order)
            if kind in (4, 5):
                dwo_id, cursor = _number(unit_data, cursor, 8, sections.order)
            elif kind in (2, 6):
                cursor = end
                continue
            elif kind != 1:
                raise DebugError("unsupported DWARF unit kind")
        else:
            abbrev, cursor = _number(unit_data, cursor, width, sections.order)
            address_size, cursor = _number(unit_data, cursor, 1, sections.order)
        if address_size not in (4, 8):
            raise DebugError("invalid DWARF address size")
        code, cursor = _leb(unit_data, cursor)
        tag, fields = _abbreviation(abbreviations, abbrev, code)
        if tag not in (0x11, 0x3c, 0x4a):
            raise DebugError("invalid DWARF compilation-unit tag")
        values = {}
        for attr, form, implicit in fields:
            value, cursor = _form(unit_data, cursor, form, width, address_size, sections.order, implicit, version=version)
            values[attr] = (form, value)
        if cursor > end:
            raise DebugError("DWARF attributes extend beyond their unit")
        if 0x2131 in values:
            dwo_id = values[0x2131][1]
        if dwo_id is not None and (type(dwo_id) is not int or not 0 <= dwo_id <= 2**64 - 1):
            raise DebugError("invalid split-DWARF compilation-unit identity")
        item = dict(version=version, dwo_id=None if dwo_id is None else f"{dwo_id:016x}")
        for attr, name in [(0x03, "name_hex"), (0x1b, "directory_hex"), (0x76, "dwo_name_hex"), (0x2130, "dwo_name_hex")]:
            if attr not in values:
                continue
            form, value = values[attr]
            if form == 0x0e:
                value, _ = _string(sections.get(".debug_str" + suffix), value)
            elif form == 0x1f:
                value, _ = _string(sections.get(".debug_line_str" + suffix), value)
            elif form in (0x1a, 0x25, 0x26, 0x27, 0x28, 0x1f02):
                base = values.get(0x72, (None, 8 if version == 5 and width == 4 else 16 if version == 5 else 0))[1]
                offset, _ = _number(sections.get(".debug_str_offsets" + suffix), base + value * width, width, sections.order)
                value, _ = _string(sections.get(".debug_str" + suffix), offset)
            if not isinstance(value, bytes):
                raise DebugError("unsupported DWARF compilation-unit string")
            string_bytes += len(value)
            if len(value) > 65536 or string_bytes > 1024**2:
                raise DebugError("DWARF compilation-unit strings exceed their limit")
            item[name] = value.hex()
        result.append(item)
        cursor = end
    return result


def inspect(path):
    sections = Sections(path)
    try:
        hashes = {name: hashlib.sha256(sections.get(name)).hexdigest() for name in sorted(sections.headers)
                  if sections.headers[name][5]}
        units = _units(sections, "")
        split_units = _units(sections, ".dwo")
        line_units = 0
        line_programs = 0
        for suffix in ("", ".dwo"):
            data = sections.get(".debug_line" + suffix)
            cursor = 0
            while cursor < len(data):
                end, start, width = _length(data, cursor, sections.order)
                version, position = _number(data, start, 2, sections.order)
                if version not in (2, 3, 4, 5):
                    raise DebugError("unsupported DWARF line version")
                if version == 5:
                    address_size, position = _number(data, position, 1, sections.order)
                    segment_size, position = _number(data, position, 1, sections.order)
                    if address_size not in (4, 8) or segment_size:
                        raise DebugError("unsupported DWARF line address size")
                length, position = _number(data, position, width, sections.order)
                minimum = 6 if version >= 4 else 5
                if length < minimum or position + length > end:
                    raise DebugError("invalid DWARF line header length")
                opcode_base = data[position + minimum - 1]
                if not data[position] or not opcode_base or minimum + opcode_base - 1 > length:
                    raise DebugError("invalid DWARF line opcode table")
                if position + length < end:
                    line_programs += 1
                cursor = end
                line_units += 1
                if line_units > MAX_UNITS:
                    raise DebugError("too many DWARF line units")
        debuglink = None
        raw_link = sections.get(".gnu_debuglink")
        if raw_link:
            name, end = _string(raw_link, 0)
            checksum = (end + 3) & ~3
            if not name or b"/" in name or len(raw_link) != checksum + 4:
                raise DebugError("invalid GNU debuglink")
            debuglink = dict(name_hex=name.hex(), crc32=int.from_bytes(raw_link[checksum:],
                "little" if sections.order == "<" else "big"))
        return dict(abi=sections.abi, sections=hashes, units=units, split_units=split_units,
            file_backed_code=sections.file_backed_code, elf_flags=sections.flags,
            line_units=line_units, line_programs=line_programs, debuglink=debuglink)
    finally:
        sections.close()


def debug_files(root):
    """Select real debug ELFs, checking aliases without copying source closures."""
    root = Path(root).resolve(strict=True)
    result = []
    seen = set()
    pending = [root]
    visited = 0
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            visited += 1
            if visited > 16384 or len(path.relative_to(root).parts) > 32:
                raise DebugError("debug output tree exceeds its limit")
            if path.name.endswith((".executable", ".source", ".sourceoverlay")):
                continue
            if path.is_symlink():
                try:
                    target = path.resolve(strict=True)
                    relative = target.relative_to(root)
                except (OSError, ValueError, RuntimeError) as error:
                    raise DebugError("broken or escaping separate-debug alias") from error
                if not target.is_file():
                    raise DebugError("directory aliases are not debug files")
                if target.suffix != ".debug":
                    raise DebugError("separate-debug alias has an unexpected target")
                result.append((path.relative_to(root).as_posix(), target))
                continue
            if path.is_dir():
                # Nix can retain patched source overlays here. Preprocessed
                # sources have their own captured role; do not republish them.
                if path.relative_to(root).parts[:1] != ("src",):
                    pending.append(path)
                continue
            if path.is_file():
                with path.open("rb") as file:
                    is_elf = file.read(4) == b"\x7fELF"
                if not is_elf and path.suffix != ".debug":
                    continue
                relative = path.relative_to(root).as_posix()
                if relative not in seen:
                    result.append((relative, path))
                    seen.add(relative)
    return sorted(result)
