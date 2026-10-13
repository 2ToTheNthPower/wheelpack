"""Assert the CI runtime and run the complete suite without platform filtering."""

import argparse
import ctypes
import importlib.metadata
import json
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path


def _libc():
    if sys.platform != "linux":
        return "none"
    try:
        version = ctypes.CDLL(None).gnu_get_libc_version
        version.restype = ctypes.c_char_p
        return "gnu" if version() else "unknown"
    except AttributeError:
        for loader in Path("/lib").glob("ld-musl-*.so.1"):
            result = subprocess.run([str(loader)], capture_output=True, text=True, check=False)
            if "musl" in result.stdout + result.stderr:
                return "musl"
    return "unknown"


def _architecture():
    if sys.platform == "win32":
        # platform.machine() can describe the ARM host while Python runs under x64 emulation.
        return {"win-amd64": "x86_64", "win-arm64": "aarch64", "win32": "x86"}.get(
            sysconfig.get_platform(), "unknown"
        )
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-os", required=True, choices=("linux", "macos", "windows"))
    parser.add_argument("--expected-arch", required=True, choices=("x86_64", "aarch64"))
    parser.add_argument("--expected-python", required=True)
    parser.add_argument("--expected-libc", required=True, choices=("gnu", "musl", "none"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    results = root / "ci-results"
    results.mkdir(exist_ok=True)
    os_name = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(
        sys.platform, sys.platform
    )
    report = {
        "os": os_name,
        "arch": _architecture(),
        "machine": platform.machine(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "libc": _libc(),
        "implementation": platform.python_implementation(),
        "pointer_bits": struct.calcsize("P") * 8,
        "executable": sys.executable,
        "version": sys.version,
        "platform": platform.platform(),
        "sysconfig_platform": sysconfig.get_platform(),
        "expected": vars(args),
        "tools": {},
    }
    errors = []
    for key in ("os", "arch", "python", "libc"):
        expected = getattr(args, f"expected_{key}")
        if report[key] != expected:
            errors.append(f"Expected {key}={expected}, got {report[key]}")
    if report["implementation"] != "CPython" or report["pointer_bits"] != 64:
        errors.append("CI requires native 64-bit CPython")
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        required_uv = project["tool"]["uv"]["required-version"]
        if not required_uv.startswith("=="):
            raise ValueError("CI requires an exact uv version pin")
        expected_uv = required_uv.removeprefix("==")
        for tool, command in (("uv", ["uv", "--version"]), ("poetry", ["poetry", "--version"])):
            if shutil.which(tool) is None:
                raise RuntimeError(f"Required tool is missing: {tool}")
            output = subprocess.run(command, capture_output=True, text=True, check=True)
            report["tools"][tool] = output.stdout.strip()
        if report["tools"]["uv"].split()[1] != expected_uv:
            errors.append(f"uv must match pyproject.toml: {expected_uv}")
        for package in ("pip", "pytest", "poetry-plugin-export"):
            report["tools"][package] = importlib.metadata.version(package)
        subprocess.run(["poetry", "export", "--help"], capture_output=True, text=True, check=True)
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        importlib.metadata.PackageNotFoundError,
        subprocess.CalledProcessError,
    ) as error:
        errors.append(f"CI tooling check failed: {error}")
    report["errors"] = errors
    diagnostics = json.dumps(report, indent=2) + "\n"
    (results / "diagnostics.json").write_text(diagnostics, encoding="utf-8")
    print(diagnostics, flush=True)
    if errors:
        print("CI preflight failed: " + "; ".join(errors), file=sys.stderr)
        return 1
    # Use this exact interpreter; never let a PATH Python or .python-version select it.
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--junitxml=" + str(results / "junit.xml"), "-ra"],
        cwd=root,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
