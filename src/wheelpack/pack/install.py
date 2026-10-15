"""Standalone, standard-library-only installer for a wheelpack bundle.

Extract the bundle, then run:

    python install.py --venv /path/to/new/venv

The destination needs Python 3.10+ with the venv module (ensurepip). No network
access, wheelpack, uv, or Poetry is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import venv
from pathlib import Path


def _load_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _verify(root: Path, manifest: dict) -> list[Path]:
    wheels = sorted((root / "wheels").glob("*.whl"))
    if not wheels:
        raise SystemExit("wheelpack: bundle contains no wheels")
    expected = {entry["filename"]: entry["sha256"] for entry in manifest.get("wheels", [])}
    for wheel in wheels:
        recorded = expected.get(wheel.name)
        if recorded is None:
            continue
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if recorded != digest:
            raise SystemExit(f"wheelpack: hash mismatch for {wheel.name}")
    return wheels


def _python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install a wheelpack offline bundle.")
    parser.add_argument("--venv", type=Path, default=Path(".venv"), help="destination venv")
    parser.add_argument("--verify-only", action="store_true", help="check wheels and exit")
    parser.add_argument("--install-source", action="store_true", help="install source/ too")
    parser.add_argument("--reuse", action="store_true", help="allow an existing destination")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    manifest = _load_manifest(root)
    wheels = _verify(root, manifest)
    if args.verify_only:
        print(f"verified {len(wheels)} wheels")
        return 0
    venv_dir = args.venv.resolve()
    if venv_dir.exists() and not args.reuse:
        raise SystemExit(f"wheelpack: destination exists: {venv_dir} (use --reuse)")
    venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(venv_dir)
    python = _python(venv_dir)
    try:
        subprocess.run(
            [
                str(python), "-m", "pip", "install",
                "--no-index", "--require-hashes", "--no-deps",
                "-r", str(root / "requirements.txt"),
            ],
            cwd=root,
            check=True,
        )
        if args.install_source and (root / "source" / "pyproject.toml").is_file():
            subprocess.run(
                [
                    str(python), "-m", "pip", "install",
                    "--no-index", "--find-links", str(root / "wheels"), "--no-deps",
                    str(root / "source"),
                ],
                cwd=root,
                check=True,
            )
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"wheelpack: installation failed (exit {error.returncode})") from None
    print(f"installed {len(wheels)} wheels into {venv_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
