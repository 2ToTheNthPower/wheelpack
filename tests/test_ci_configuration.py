"""Regression contracts for CI bootstrap and its POSIX container entry point."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow", ["ci.yml", "musl.yml", "zip-transfer.yml"])
def test_ci_bootstrap_is_independent_of_local_devbox(workflow):
    text = (ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8")
    assert "devbox" not in text.lower()
    assert "version-file: pyproject.toml" in text
    assert "python-arch:" not in text
    assert "UV_PYTHON_PREFERENCE" in text
    assert "UV_MANAGED_PYTHON:" not in text


def test_musl_workflow_passes_a_script_not_multiline_shell_code():
    text = (ROOT / ".github/workflows/musl.yml").read_text(encoding="utf-8")
    assert "sh scripts/ci-musl.sh" in text
    assert "sh -ec" not in text
    assert "steps.uv.outputs.uv-version" in text


def test_transfer_uploads_cover_every_catalog_target():
    text = (ROOT / ".github/workflows/zip-transfer.yml").read_text(encoding="utf-8")
    keys = ("linux-x64", "linux-arm64", "macos-x64", "macos-arm64",
            "windows-x64", "windows-arm64", "musl-x64", "musl-arm64")
    expected = {f"{key}-py{minor}" for key in keys for minor in ("3.11", "3.12", "3.13", "3.14")}
    uploads = re.findall(r"name: target-zip-([a-z0-9.-]+)-\$\{\{ env.TRANSFER_PRODUCER \}\}", text)
    assert len(uploads) == 32
    assert set(uploads) == expected
    for target in expected:
        assert f"path: transfer-out/{target}/" in text
    assert "sh scripts/ci-transfer-musl.sh" in text
    assert "--producer-count 40" in text
    assert "merge-multiple: false" in text
    assert "--user" in text


@pytest.mark.skipif(os.name == "nt", reason="musl entry point runs in a POSIX container")
@pytest.mark.parametrize("failure", [None, "python", "sync"])
@pytest.mark.parametrize("entrypoint,mode", [
    ("ci-musl.sh", None), ("ci-transfer-musl.sh", "produce"),
    ("ci-transfer-musl.sh", "consume"),
])
def test_musl_entrypoint_preserves_arguments_and_stops_on_failure(
    tmp_path, failure, entrypoint, mode,
):
    folder = tmp_path / "fake tools"
    folder.mkdir()
    log = tmp_path / "commands.jsonl"
    stub = folder / "record.py"
    stub.write_text(
        "import json, os, sys\n"
        "with open(os.environ['COMMAND_LOG'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(23 if sys.argv[1] == os.environ.get('FAIL_COMMAND') else 0)\n",
        encoding="utf-8",
    )
    shim = folder / "uv"
    shim.write_text('#!/bin/sh\nexec "$TEST_PYTHON" "$TEST_STUB" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    request = "cpython-3.11-linux-aarch64-musl"
    env = {
        **os.environ,
        "PATH": str(folder) + os.pathsep + os.environ.get("PATH", ""),
        "TEST_PYTHON": sys.executable,
        "TEST_STUB": str(stub),
        "COMMAND_LOG": str(log),
        "FAIL_COMMAND": failure or "",
        "UV_PYTHON": request,
        "EXPECTED_ARCH": "aarch64",
        "EXPECTED_PYTHON": "3.11",
        "EXPECTED_OS": "linux",
        "EXPECTED_LIBC": "musl",
        "TRANSFER_MODE": mode or "",
        "TRANSFER_PRODUCER": "test-host-py3.11",
        "TRANSFER_TARGET": "musl-arm64",
    }
    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / entrypoint)], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False, timeout=30,
    )
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    dependency_flags = ["--no-dev"] if mode else []
    command = ["scripts/ci.py"] if mode is None else ["scripts/ci_transfer.py", mode]
    if mode == "produce":
        command += ["--producer", "test-host-py3.11", "--output", "transfer-out"]
    elif mode == "consume":
        command += ["--target", "musl-arm64", "--producer-count", "40",
                    "--input", "transfer-in", "--output", "transfer-results"]
    expected = [
        ["python", "install", request],
        ["sync", "--locked", *dependency_flags, "--python", request],
        ["run", "--locked", *dependency_flags, "--python", request, "python", *command,
         "--expected-os", "linux", "--expected-arch", "aarch64",
         "--expected-python", "3.11", "--expected-libc", "musl"],
    ]
    count = 1 if failure == "python" else 2 if failure == "sync" else 3
    assert commands == expected[:count]
    assert result.returncode == (23 if failure else 0), result.stdout + result.stderr
