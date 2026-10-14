"""Assert the CI runtime and run the complete suite with the exact interpreter.

The environment checks guard against an emulated, wrong-architecture, or
wrong-Python interpreter masquerading as the requested native runtime.
"""

import argparse
import ctypes
import importlib.metadata
import json
import platform
import struct
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _libc() -> str:
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


def _architecture() -> str:
    if sys.platform == "win32":
        # platform.machine() can describe the ARM host while Python runs under x64 emulation.
        return {"win-amd64": "x86_64", "win-arm64": "aarch64", "win32": "x86"}.get(
            sysconfig.get_platform(), "unknown"
        )
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def diagnose() -> dict:
    return {
        "os": {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(
            sys.platform, sys.platform
        ),
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
        "tools": {},
    }


def validate(expected_os: str, expected_arch: str, expected_python: str, expected_libc: str) -> dict:
    """Return diagnostics, raising RuntimeError when the runtime is not the request."""
    report = diagnose()
    errors = []
    for key, expected in (
        ("os", expected_os),
        ("arch", expected_arch),
        ("python", expected_python),
        ("libc", expected_libc),
    ):
        if report[key] != expected:
            errors.append(f"Expected {key}={expected}, got {report[key]}")
    if report["implementation"] != "CPython" or report["pointer_bits"] != 64:
        errors.append("CI requires native 64-bit CPython")
    try:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        required_uv = project["tool"]["uv"]["required-version"]
        if not required_uv.startswith("=="):
            raise ValueError("CI requires an exact uv version pin")
        expected_uv = required_uv.removeprefix("==")
        output = subprocess.run(
            ["uv", "--version"], capture_output=True, text=True, check=True
        )
        report["tools"]["uv"] = output.stdout.strip()
        if report["tools"]["uv"].split()[1] != expected_uv:
            errors.append(f"uv must match pyproject.toml: {expected_uv}")
        report["tools"]["pip"] = importlib.metadata.version("pip")
    except (
        OSError,
        ValueError,
        KeyError,
        importlib.metadata.PackageNotFoundError,
        subprocess.CalledProcessError,
    ) as error:
        errors.append(f"CI tooling check failed: {error}")
    report["expected"] = {
        "os": expected_os,
        "arch": expected_arch,
        "python": expected_python,
        "libc": expected_libc,
    }
    report["errors"] = errors
    if errors:
        raise RuntimeError("; ".join(errors))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-os", required=True, choices=("linux", "macos", "windows"))
    parser.add_argument("--expected-arch", required=True, choices=("x86_64", "aarch64"))
    parser.add_argument("--expected-python", required=True)
    parser.add_argument("--expected-libc", required=True, choices=("gnu", "musl", "none"))
    args = parser.parse_args()
    results = ROOT / "ci-results"
    results.mkdir(exist_ok=True)
    try:
        report = validate(
            args.expected_os, args.expected_arch, args.expected_python, args.expected_libc
        )
    except RuntimeError as error:
        (results / "diagnostics.json").write_text(
            json.dumps({"errors": [str(error)]}, indent=2) + "\n", encoding="utf-8"
        )
        print(f"CI preflight failed: {error}", file=sys.stderr)
        return 1
    (results / "diagnostics.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2), flush=True)
    # Use this exact interpreter; never let a PATH Python or .python-version select it.
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--junitxml=" + str(results / "junit.xml"), "-ra"],
        cwd=ROOT,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
