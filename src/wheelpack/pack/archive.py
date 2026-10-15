"""Assemble source, dependencies, and a standalone installer into one ZIP."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from packaging.utils import parse_wheel_filename

from .. import __version__, pylock
from .._compat import file_digest
from ..retrieve import retrieve

_SOURCE_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    ".tox",
    ".nox",
    ".eggs",
}


class PackError(ValueError):
    """A bundle could not be assembled."""


def pack(
    input,
    output,
    *,
    manager: str = "auto",
    indexes=(),
    find_links=(),
    no_index: bool = False,
    extras=(),
    groups=(),
    target=None,
    python: str | None = None,
    include_source: bool = True,
) -> Path:
    """Resolve ``input``, retrieve its wheels, and write a self-contained ZIP.

    The archive contains the project source, every dependency wheel, the pylock
    it was retrieved from, a hashed requirements file, a standalone installer,
    a manifest, and usage instructions. Pass ``target`` (a
    :class:`wheelpack.targets.Target`) to pack for a foreign machine,
    architecture, and Python version instead of the packing host. Returns the
    output path.
    """
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise PackError(f"Output already exists: {output}")
    if not output.parent.is_dir():
        raise PackError(f"Output parent directory does not exist: {output.parent}")
    source = pylock.detect(input, manager)
    with tempfile.TemporaryDirectory(prefix="wheelpack-") as temporary:
        root = Path(temporary) / "bundle"
        wheels_dir = root / "wheels"
        wheels_dir.mkdir(parents=True)
        pylock_path = root / "pylock.toml"
        if target is not None:
            from .. import targeted

            effective = targeted.collect(
                source.input,
                source.manager,
                wheels_dir,
                target=target,
                indexes=list(indexes),
                find_links=list(find_links),
                no_index=no_index,
                extras=list(extras),
                groups=list(groups),
                include_project=False,
                project=source.project,
                uv_root=source.uv_root,
                lock_out=pylock_path,
            )
            wheel_paths = sorted(wheels_dir.glob("*.whl"))
        else:
            resolution = pylock.resolve(
                source,
                indexes=list(indexes),
                find_links=list(find_links),
                no_index=no_index,
                extras=list(extras),
                groups=list(groups),
            )
            effective = resolution.manager
            resolution.plan["packages"] = pylock.select(resolution.plan, extras, groups)
            pylock.write_plan(resolution.plan, pylock_path, resolution.plan_cwd)
            wheel_paths = retrieve(
                pylock_path,
                wheels_dir,
                indexes=indexes,
                find_links=find_links,
                no_index=no_index,
                python=python,
            )
        source_manifest = _copy_source(source, root / "source", output, include_source)
        entries = [_wheel_entry(path) for path in wheel_paths]
        (root / "requirements.txt").write_text(
            _requirements(entries), encoding="utf-8", newline="\n"
        )
        manifest = {
            "format": 1,
            "created_by": f"wheelpack {__version__}",
            "manager": effective,
            "target": target.manifest() if target else None,
            "source": source_manifest,
            "pylock": "pylock.toml",
            "wheels": entries,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        shutil.copyfile(Path(__file__).with_name("install.py"), root / "install.py")
        (root / "README.txt").write_text(
            _instructions(source_manifest), encoding="utf-8", newline="\n"
        )
        _archive(root, output)
    return output


def _copy_source(source, destination: Path, output: Path, include: bool):
    if not include:
        return None
    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_project:
        target = destination / source.input.name
        shutil.copyfile(source.input, target)
        return {"kind": "requirements", "path": target.name}
    root = source.cwd
    for directory, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in _SOURCE_EXCLUDES]
        for filename in files:
            path = Path(directory) / filename
            if path.resolve() == output.resolve() or path.suffix == ".zip":
                continue
            target = destination / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    name = source.project.get("project", {}).get("name")
    return {"kind": "project", "path": "source", "name": name}


def _wheel_entry(path: Path) -> dict:
    name, version, _, _ = parse_wheel_filename(path.name)
    with path.open("rb") as stream:
        sha256 = file_digest(stream, "sha256").hexdigest()
    return {"filename": path.name, "name": name, "version": str(version), "sha256": sha256}


def _requirements(entries: list[dict]) -> str:
    lines = ["--no-index", "--only-binary :all:"]
    for entry in entries:
        lines.append(f"./wheels/{entry['filename']} --hash=sha256:{entry['sha256']}")
    return "\n".join(lines) + "\n"


def _instructions(source_manifest) -> str:
    lines = [
        "wheelpack offline bundle",
        "",
        "Contents:",
        "  source/          the project source code",
        "  wheels/          every dependency as a wheel",
        "  pylock.toml      the PEP 751 lock the wheels were retrieved from",
        "  requirements.txt local wheel references with SHA-256 hashes",
        "  install.py       standalone installer (standard library only)",
        "  manifest.json    bundle metadata",
        "",
        "Extract with unzip, or: python -m zipfile -e BUNDLE.zip DIRECTORY",
        "",
        "Install the dependencies into a new virtual environment:",
        "  python install.py --venv /path/to/new/venv",
        "",
        "Verify the wheels without installing:",
        "  python install.py --verify-only",
        "",
        "Also install the bundled source project into the environment:",
        "  python install.py --venv /path/to/new/venv --install-source",
        "",
        "Run the bundled source with the environment, for example:",
        "  /path/to/new/venv/bin/python source/your_app.py",
        "",
        "Requires Python 3.10+ with the venv module (ensurepip). No network",
        "access, wheelpack, uv, or Poetry is required.",
        "",
        "Only install bundles from trusted sources: the hashes detect corruption,",
        "not malicious replacement of the entire archive.",
    ]
    if source_manifest is None:
        lines.insert(4, "  (source code omitted)")
    return "\n".join(lines) + "\n"


def _archive(root: Path, output: Path) -> None:
    descriptor, staged = tempfile.mkstemp(prefix=".wheelpack-", suffix=".zip", dir=output.parent)
    try:
        with (
            os.fdopen(descriptor, "wb") as stream,
            zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive,
        ):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        try:
            os.link(staged, output)
        except FileExistsError:
            raise PackError(f"Output already exists: {output}") from None
    finally:
        Path(staged).unlink(missing_ok=True)
