"""Produce and consume wheelpack ZIP bundles across machines in CI.

A producer packs a synthetic project into native and pure-Python bundles; a
consumer on another machine extracts a bundle and runs the bundled installer.
Native bundles are consumed on the same platform/Python; pure bundles cross
platforms, architectures, and Python versions.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path

import ci

from wheelpack.pack import pack


def _project(root: Path, dependencies: tuple[str, ...]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rendered = ", ".join(f'"{dependency}"' for dependency in dependencies)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "transfer-sample"\nversion = "1"\n'
        f'requires-python = ">=3.11"\ndependencies = [{rendered}]\n',
        encoding="utf-8",
    )
    return root


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def produce(args) -> int:
    report = ci.validate(args.expected_os, args.expected_arch, args.expected_python, args.expected_libc)
    output = Path(args.output)
    native_dir = output / "native"
    pure_dir = output / "pure"
    native_dir.mkdir(parents=True, exist_ok=True)
    pure_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wheelpack-transfer-") as temporary:
        work = Path(temporary)
        packaging = metadata.version("packaging")
        native = _project(work / "native", (f"msgpack=={args.msgpack}", f"packaging=={packaging}"))
        pure = _project(work / "pure", (f"packaging=={packaging}",))
        pack(native, native_dir / f"{args.producer}.zip", manager="pip")
        pack(pure, pure_dir / f"{args.producer}.zip", manager="pip")
    (output / f"{args.producer}.json").write_text(
        json.dumps(
            {
                "producer": args.producer,
                "os": report["os"],
                "arch": report["arch"],
                "python": report["python"],
                "libc": report["libc"],
                "msgpack": args.msgpack,
                "packaging": metadata.version("packaging"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"produced native and pure bundles for {args.producer}")
    return 0


def _install(archive: Path, label: str, work: Path, imports: str) -> dict:
    if not archive.is_file():
        raise RuntimeError(f"missing bundle: {archive}")
    bundle = work / f"{label}-bundle"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(bundle)
    venv = work / f"{label}-venv"
    subprocess.run(
        [sys.executable, str(bundle / "install.py"), "--venv", str(venv)],
        cwd=bundle,
        check=True,
    )
    result = subprocess.run(
        [str(_venv_python(venv)), "-c", imports],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{label} import failed: {result.stdout}{result.stderr}")
    return {"archive": archive.name, "imports": result.stdout.strip()}


def consume(args) -> int:
    report = ci.validate(args.expected_os, args.expected_arch, args.expected_python, args.expected_libc)
    input_dir = Path(args.input)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix="wheelpack-transfer-") as temporary:
        work = Path(temporary)
        results["native"] = _install(
            input_dir / "native" / f"{args.consumer}.zip",
            "native",
            work,
            "import msgpack, packaging, importlib.metadata as m; "
            "print(m.version('msgpack'), m.version('packaging'))",
        )
        results["pure"] = _install(
            input_dir / "pure" / f"{args.pure_producer}.zip",
            "pure",
            work,
            "import packaging, importlib.metadata as m; print(m.version('packaging'))",
        )
    (output / f"{args.consumer}.json").write_text(
        json.dumps(
            {
                "consumer": report["os"],
                "os": report["os"],
                "arch": report["arch"],
                "python": report["python"],
                "libc": report["libc"],
                "results": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"consumed native {args.consumer} and pure {args.pure_producer} bundles")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("produce", "consume"):
        command = subparsers.add_parser(name)
        command.add_argument("--expected-os", required=True)
        command.add_argument("--expected-arch", required=True)
        command.add_argument("--expected-python", required=True)
        command.add_argument("--expected-libc", required=True)
        if name == "produce":
            command.add_argument("--producer", required=True)
            command.add_argument("--output", required=True)
            command.add_argument("--msgpack", default="1.2.3")
        else:
            command.add_argument("--consumer", required=True)
            command.add_argument("--pure-producer", required=True)
            command.add_argument("--input", required=True)
            command.add_argument("--output", required=True)
    args = parser.parse_args()
    return produce(args) if args.command == "produce" else consume(args)


if __name__ == "__main__":
    raise SystemExit(main())
