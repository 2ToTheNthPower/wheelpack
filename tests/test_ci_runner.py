"""CI must assert interpreter architecture, not merely the runner's hardware label."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ci_runner", Path(__file__).resolve().parents[1] / "scripts" / "ci.py"
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.mark.parametrize(
    "platform,machine,expected",
    [("win-amd64", "ARM64", "x86_64"), ("win-arm64", "ARM64", "aarch64"),
     ("win32", "AMD64", "x86"), ("unexpected", "ARM64", "unknown")],
)
def test_windows_ci_detects_process_architecture(monkeypatch, platform, machine, expected):
    monkeypatch.setattr(ci.sys, "platform", "win32")
    monkeypatch.setattr(ci.platform, "machine", lambda: machine)
    monkeypatch.setattr(ci.sysconfig, "get_platform", lambda: platform)
    assert ci._architecture() == expected


@pytest.mark.parametrize("machine,expected", [("x86_64", "x86_64"), ("arm64", "aarch64")])
def test_posix_ci_architecture(monkeypatch, machine, expected):
    monkeypatch.setattr(ci.sys, "platform", "darwin")
    monkeypatch.setattr(ci.platform, "machine", lambda: machine)
    assert ci._architecture() == expected
