"""Assemble a self-contained offline ZIP: source, wheels, installer, instructions."""

from .archive import PackError, pack

__all__ = ["PackError", "pack"]
