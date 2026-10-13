"""CI must assert interpreter architecture, not merely the runner's hardware label."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("actual_uv,expected_status", [("0.12.5", 0), ("0.12.4", 1)])
def test_ci_checks_uv_against_project_pin(monkeypatch, tmp_path, actual_uv, expected_status):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv]\nrequired-version = "==0.12.5"\n', encoding="utf-8"
    )
    monkeypatch.setattr(ci, "__file__", str(tmp_path / "scripts/ci.py"))
    monkeypatch.setattr(ci.sys, "argv", [
        "ci.py", "--expected-os", "macos", "--expected-arch", "x86_64",
        "--expected-python", f"{ci.sys.version_info.major}.{ci.sys.version_info.minor}",
        "--expected-libc", "none",
    ])
    monkeypatch.setattr(ci.sys, "platform", "darwin")
    monkeypatch.setattr(ci.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ci.shutil, "which", lambda tool: tool)
    monkeypatch.setattr(ci.importlib.metadata, "version", lambda package: "1.0")

    def run(command, **kwargs):
        output = f"uv {actual_uv}" if command[0] == "uv" else "Poetry (version 2.0)"
        return SimpleNamespace(stdout=output, returncode=0)

    monkeypatch.setattr(ci.subprocess, "run", run)
    assert ci.main() == expected_status
    report = json.loads((tmp_path / "ci-results/diagnostics.json").read_text(encoding="utf-8"))
    assert report["errors"] == (
        [] if expected_status == 0 else ["uv must match pyproject.toml: 0.12.5"]
    )
