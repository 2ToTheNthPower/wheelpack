"""Read and validate a PEP 751 pylock.toml for retrieval."""

from __future__ import annotations

from pathlib import Path

from .._compat import tomllib

LOCK_VERSION = "1.0"


def load_plan(lock) -> dict:
    """Return a validated pylock plan from a path or an already-parsed mapping."""
    if isinstance(lock, dict):
        plan = lock
    else:
        path = Path(lock)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise ValueError(f"Cannot read pylock: {path.name}") from None
        try:
            plan = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            raise ValueError(f"Invalid pylock TOML: {path.name}") from None
    if plan.get("lock-version") != LOCK_VERSION or not isinstance(plan.get("packages"), list):
        raise ValueError("Not a PEP 751 pylock lock-version 1.0 plan")
    return plan
