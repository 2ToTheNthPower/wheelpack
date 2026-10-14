"""Identify the running interpreter's libc family and version.

Kept separate from any installer: explicit-target resolution needs the native
libc to build a uv platform triple, without pulling in bundle or install code.
"""

from __future__ import annotations

import os
import platform
import re
import struct
import subprocess
import sys
from pathlib import Path, PurePosixPath


def _libc() -> list[str]:
    """Identify libc, including musl via this interpreter's ELF runtime loader."""
    reported = list(platform.libc_ver())
    # Some musl interpreters report the bare family "libc" with no version; fall
    # through to the ELF PT_INTERP probe instead of trusting that placeholder.
    if reported[0] not in ("", "unknown", "libc") or sys.platform != "linux":
        return reported
    try:
        with Path(sys.executable).open("rb") as stream:
            file_size = stream.seek(0, os.SEEK_END)
            stream.seek(0)
            header = stream.read(64)
            if len(header) < 16 or header[:4] != b"\x7fELF" or header[5] not in (1, 2):
                return ["unknown", ""]
            endian = "<" if header[5] == 1 else ">"
            if header[4] == 1:
                header_format, entry_format = "HHIIIIIHHHHHH", "IIIIIIII"
                offset_index, size_index = 1, 4
            elif header[4] == 2:
                header_format, entry_format = "HHIQQQIHHHHHH", "IIQQQQQQ"
                offset_index, size_index = 2, 5
            else:
                return ["unknown", ""]
            fields = struct.unpack_from(endian + header_format, header, 16)
            phoff, phentsize, phnum = fields[4], fields[8], fields[9]
            entry_size = struct.calcsize(endian + entry_format)
            if (
                not 0 < phnum <= 4096
                or not entry_size <= phentsize <= 4096
                or phoff < 16 + struct.calcsize(endian + header_format)
                or phoff + phentsize * phnum > file_size
            ):
                return ["unknown", ""]
            for index in range(phnum):
                stream.seek(phoff + index * phentsize)
                entry = struct.unpack(endian + entry_format, stream.read(entry_size))
                if entry[0] != 3:  # PT_INTERP names the loader already used by the OS.
                    continue
                offset, size = entry[offset_index], entry[size_index]
                if not 2 <= size <= 4096 or offset + size > file_size:
                    return ["unknown", ""]
                stream.seek(offset)
                data = stream.read(size)
                if len(data) != size or not data.endswith(b"\x00") or b"\x00" in data[:-1]:
                    return ["unknown", ""]
                loader = os.fsdecode(data[:-1])
                loader_path = PurePosixPath(loader)
                if (
                    not loader_path.is_absolute()
                    or not loader_path.name.startswith("ld-musl-")
                    or not loader_path.name.endswith(".so.1")
                ):
                    return ["unknown", ""]
                # musl prints its version to stderr and exits nonzero without a program.
                result = subprocess.run(
                    [loader], check=False, capture_output=True, text=True, timeout=5
                )
                version = re.search(
                    r"^Version ([0-9]+\.[0-9]+\.[0-9]+)\s*$", result.stderr, re.MULTILINE
                )
                if version:
                    return ["musl", version[1]]
                return ["unknown", ""]
    except (OSError, ValueError, struct.error, subprocess.SubprocessError):
        pass
    return ["unknown", ""]
