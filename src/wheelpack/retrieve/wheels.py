"""Download or build every package named by a pylock plan."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from .. import sources as s
from ..progress import Progress
from .plan import load_plan


class RetrieveError(ValueError):
    """A pylock package could not be retrieved."""


def retrieve(
    lock,
    destination,
    *,
    indexes=(),
    find_links=(),
    no_index: bool = False,
    python: str | None = None,
    target=None,
    progress=None,
) -> list[Path]:
    """Download or build every package in ``lock`` into ``destination``.

    ``lock`` is a pylock.toml path or an already-parsed plan. ``target`` is an
    optional :class:`wheelpack.targets.Target` used to pick the matching wheel
    for a foreign machine/architecture. ``progress`` is an optional
    :class:`wheelpack.progress.Progress` used to report per-package progress.
    Returns the wheel paths in ``destination``.
    """
    plan = load_plan(lock)
    cwd = Path(lock).resolve().parent if isinstance(lock, (str, Path)) else Path.cwd()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    interpreter = python or sys.executable
    options = s._options(list(indexes), list(find_links), no_index)
    credential_urls = [*indexes, *find_links]
    progress = progress or Progress(enabled=False)
    packages = plan["packages"]
    with progress.bar("retrieving", len(packages)) as bar:
        for package in packages:
            _retrieve_package(
                package, destination, options, credential_urls, interpreter, cwd, target
            )
            bar.advance(package.get("name", ""))
    return sorted(destination.glob("*.whl"))


def _retrieve_package(package, destination, options, credential_urls, interpreter, cwd, target):
    name = package.get("name")
    if not name:
        raise RetrieveError("pylock package is missing a name")
    if "directory" in package:
        source = package["directory"]
        path = source.get("path")
        if not path:
            raise RetrieveError(f"Locked directory source is missing a path for {name}")
        local = Path(path)
        if not local.is_absolute():
            local = cwd / local
        reference = str(local / source.get("subdirectory", ""))
        hashed = False
    elif "vcs" in package:
        source = package["vcs"]
        if not source.get("type") or not source.get("commit-id"):
            raise RetrieveError(f"Locked VCS source is missing a type/commit for {name}")
        url = source.get("url") or (
            Path(source["path"]).as_uri() if source.get("path") else ""
        )
        if not url:
            raise RetrieveError(f"Locked VCS source is missing a URL for {name}")
        reference = f"{name} @ {source['type']}+{url}@{source['commit-id']}"
        if source.get("subdirectory"):
            reference += "#subdirectory=" + quote(source["subdirectory"])
        hashed = False
    else:
        tags = target.tags() if target else None
        url, digest = s._artifact(package, cwd, tags=tags, wheel_only=target is not None)
        url = s._pip_auth(url, credential_urls)
        reference = f"{name} @ {url} --hash={digest}"
        hashed = True
    _wheel(interpreter, reference, destination, options, hashed=hashed)


def _wheel(interpreter, reference, destination, options, *, hashed: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="wheelpack-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        requirements.write_text(reference + "\n", encoding="utf-8")
        args = [interpreter, "-m", "pip", "wheel", "--wheel-dir", str(destination), "--no-deps"]
        if hashed:
            args.append("--require-hashes")
        s._run(args + list(options) + ["-r", str(requirements)], Path.cwd())
