"""Reusable builders for wheels and local package indexes used across tests."""

from __future__ import annotations

import hashlib
import http.server
import threading
import zipfile
from pathlib import Path


def build_wheel(
    folder: str | Path,
    name: str,
    version: str = "1",
    *,
    tag: str = "py3-none-any",
    requires: tuple[str, ...] = (),
    requires_python: str | None = ">=3.10",
    purelib: bool = True,
) -> Path:
    """Create a minimal but structurally valid wheel that resolution can read."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{version}-{tag}.whl"
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if requires_python is not None:
        metadata += f"Requires-Python: {requires_python}\n"
    for requirement in requires:
        metadata += f"Requires-Dist: {requirement}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}/__init__.py", "")
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: wheelpack-tests\n"
            f"Root-Is-Purelib: {'true' if purelib else 'false'}\nTag: {tag}\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return path


def digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_project(
    tmp_path: str | Path,
    *,
    name: str = "root",
    dependencies: tuple[str, ...] = ("demo",),
    requires_python: str = ">=3.10",
    extra: str = "",
) -> Path:
    """Write a PEP 621 project and return its directory."""
    root = Path(tmp_path) / "project"
    root.mkdir(parents=True, exist_ok=True)
    rendered = ", ".join(f'"{dependency}"' for dependency in dependencies)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "1"\n'
        f'requires-python = "{requires_python}"\ndependencies = [{rendered}]\n{extra}',
        encoding="utf-8",
    )
    return root


class SimpleIndex:
    """A local PEP 503 index served over HTTP that records every download."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.downloads: list[str] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/simple"

    def publish(self, wheel: str | Path) -> str:
        wheel = Path(wheel)
        name = wheel.name.split("-", 1)[0]
        folder = self.root / "simple" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / wheel.name).write_bytes(wheel.read_bytes())
        (folder / "index.html").write_text(
            f'<a href="{wheel.name}#sha256={digest(wheel)}">{wheel.name}</a>',
            encoding="utf-8",
        )
        return f"{self.url}/{name}/{wheel.name}"

    def _handler(self):
        index = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(index.root), **kwargs)

            def do_GET(self):
                index.downloads.append(self.path)
                super().do_GET()

            def log_message(self, *args):
                pass

        return Handler

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
