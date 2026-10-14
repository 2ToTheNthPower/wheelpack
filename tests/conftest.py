"""Shared pytest fixtures for wheelport resolution tests."""

from __future__ import annotations

import pytest
from helpers import SimpleIndex, write_project


@pytest.fixture
def index(tmp_path):
    """A local HTTP PEP 503 index; call ``.publish(wheel)`` to add packages."""
    server = SimpleIndex(tmp_path / "index-root")
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def project(tmp_path):
    return write_project(tmp_path)
