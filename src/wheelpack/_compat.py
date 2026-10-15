"""Compatibility shims for the oldest supported Python (3.10).

``tomllib`` and ``hashlib.file_digest`` are standard library in 3.11+, so on 3.10
we fall back to ``tomli`` and a small chunked hasher. Everything else is written
to run unchanged on 3.10.
"""

from __future__ import annotations

import hashlib
from typing import BinaryIO

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


def file_digest(stream: BinaryIO, algorithm: str = "sha256"):
    """Return a hash object over ``stream`` without reading it all at once."""
    digest = hashlib.new(algorithm)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest


__all__ = ["file_digest", "tomllib"]
