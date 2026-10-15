"""Immutable CPython targets for wheel-only foreign resolution.

Two-component Python versions mean ``major.minor.0``, not the host patch version.
Aliases use fixed uv architectures: windows/linux are x86_64, macos is arm64.
Generic glibc Linux is pinned to manylinux 2.17, independently of uv's defaults.
macOS targets mean deployment version 11.0; source adapters must pass
MACOSX_DEPLOYMENT_TARGET=11.0 in the child environment (never mutate os.environ).
Adapters must use evaluate_marker before resolution, and reject free-threaded
ABIs if their resolver cannot select them: uv's version/platform flags alone do
not communicate the ABI. Installation must check actual pip sys_tags, not native
SOABI/libc manifest fields.
"""

from __future__ import annotations

import platform
import re
import struct
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from packaging.markers import Marker, Variable
from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms

from .libc import _libc

_GLIBC_MINORS = (17, 28, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40)
_ALIASES = {
    "windows": "x86_64-pc-windows-msvc",
    "linux": "x86_64-unknown-linux-gnu",
    "macos": "aarch64-apple-darwin",
    "win_amd64": "x86_64-pc-windows-msvc",
    "win_arm64": "aarch64-pc-windows-msvc",
    "win32": "i686-pc-windows-msvc",
    "macosx_11_0_arm64": "aarch64-apple-darwin",
    "macosx_11_0_x86_64": "x86_64-apple-darwin",
}
_WINDOWS = {
    "x86_64": ("win_amd64", "AMD64"),
    "aarch64": ("win_arm64", "ARM64"),
    "i686": ("win32", "x86"),
}


def _native_platform() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64", "x86": "i686", "i386": "i686"}.get(
        machine, machine
    )
    if sys.platform == "win32":
        if struct.calcsize("P") == 4:
            arch = "i686"
        if arch in _WINDOWS:
            return f"{arch}-pc-windows-msvc"
    elif sys.platform == "darwin" and arch in ("x86_64", "aarch64"):
        return f"{arch}-apple-darwin"
    elif sys.platform == "linux" and arch in ("x86_64", "aarch64"):
        family, version = _libc()
        match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)*", version)
        if match:
            major, minor = map(int, match.groups())
            if family == "glibc" and major == 2 and minor in _GLIBC_MINORS:
                return f"{arch}-manylinux_2_{minor}"
            if family == "musl" and (major, minor) == (1, 2):
                return f"{arch}-unknown-linux-musl"
    raise ValueError(
        "Cannot represent the native platform/libc exactly; specify "
        "--python-platform with a supported full uv triple"
    )


@dataclass(frozen=True, slots=True)
class Target:
    """A validated target; construct with create(), not the dataclass initializer.

    Mutable public containers are fresh snapshots, so changing them cannot change
    the target. OS release/version are deliberately unknown, even for native
    inference; requirements using them must go through evaluate_marker().
    """

    python_version: str
    python: tuple[int, int]
    python_platform: str
    abi: str
    python_patch: int | None = None

    @classmethod
    def create(
        cls,
        *,
        python_version: str | None = None,
        python_platform: str | None = None,
        abi: str | None = None,
    ) -> Target:
        if python_version is None:
            if sys.implementation.name != "cpython":
                raise ValueError("Only CPython is supported; specify a CPython --python-version")
            python_version = ".".join(map(str, sys.version_info[:3]))
        if (
            not isinstance(python_version, str)
            or re.fullmatch(r"3\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?", python_version) is None
        ):
            raise ValueError("--python-version must be CPython major.minor[.patch] (>=3.10)")
        parts = tuple(map(int, python_version.split(".")))
        python = (parts[0], parts[1])
        if python < (3, 10):
            raise ValueError("Only CPython >=3.10 is supported")
        full_version = python_version if len(parts) == 3 else python_version + ".0"
        default_abi = f"cp{python[0]}{python[1]}"
        if abi is None:
            abi = default_abi
        if abi != default_abi and not (python >= (3, 13) and abi == default_abi + "t"):
            raise ValueError(
                f"ABI must be {default_abi}" + (f" or {default_abi}t" if python >= (3, 13) else "")
            )

        if python_platform is None:
            python_platform = _native_platform()
        if not isinstance(python_platform, str):
            raise ValueError(  # noqa: TRY004 - invalid CLI values consistently raise ValueError.
                "--python-platform must be a supported uv triple or wheel platform tag"
            )
        name = _ALIASES.get(python_platform, python_platform)
        if name.startswith("macosx_"):
            raise ValueError(
                "macOS targets support only deployment version 11.0; use "
                "macosx_11_0_arm64 or macosx_11_0_x86_64"
            )
        wheel_linux = re.fullmatch(
            r"manylinux_(2_[0-9]+)_(x86_64|aarch64)|manylinux2014_(x86_64|aarch64)", name
        )
        if wheel_linux:
            version, arch, legacy_arch = wheel_linux.groups()
            name = f"{arch or legacy_arch}-manylinux_{version or '2_17'}"
        musl = re.fullmatch(r"musllinux_1_2_(x86_64|aarch64)", name)
        if musl:
            name = f"{musl[1]}-unknown-linux-musl"
        for arch in ("x86_64", "aarch64"):
            if name in (f"{arch}-unknown-linux-gnu", f"{arch}-manylinux2014"):
                name = f"{arch}-manylinux_2_17"
        supported = {
            *(f"{arch}-pc-windows-msvc" for arch in _WINDOWS),
            *(f"{arch}-apple-darwin" for arch in ("x86_64", "aarch64")),
            *(f"{arch}-unknown-linux-musl" for arch in ("x86_64", "aarch64")),
            *(
                f"{arch}-manylinux_2_{minor}"
                for arch in ("x86_64", "aarch64")
                for minor in _GLIBC_MINORS
            ),
        }
        if name not in supported:
            raise ValueError(
                "Unsupported --python-platform; use a supported full uv triple "
                "(e.g. x86_64-manylinux_2_17, aarch64-apple-darwin, or "
                "x86_64-pc-windows-msvc). musl targets require baseline 1.2"
            )
        return cls(full_version, python, name, abi, parts[2] if len(parts) == 3 else None)

    @property
    def marker_environment(self) -> dict[str, str]:
        arch = self.python_platform.split("-", 1)[0]
        if self.python_platform.endswith("-windows-msvc"):
            sys_platform, system, os_name, machine = "win32", "Windows", "nt", _WINDOWS[arch][1]
        elif self.python_platform.endswith("-apple-darwin"):
            sys_platform, system, os_name = "darwin", "Darwin", "posix"
            machine = "arm64" if arch == "aarch64" else arch
        else:
            sys_platform, system, os_name, machine = "linux", "Linux", "posix", arch
        return {
            "implementation_name": "cpython",
            "implementation_version": self.python_version,
            "os_name": os_name,
            "platform_machine": machine,
            "platform_release": "",
            "platform_system": system,
            "platform_version": "",
            "platform_python_implementation": "CPython",
            "python_full_version": self.python_version,
            "python_version": ".".join(map(str, self.python)),
            "sys_platform": sys_platform,
            "extra": "",
        }

    def evaluate_marker(self, marker: Marker | str, *, extras: Iterable[str] = ()) -> bool:
        """Evaluate for the base requirement plus each requested extra.

        Reject unknown OS fields anywhere in the expression, including branches
        that could otherwise short-circuit. Quoted literals are not variables.
        """
        if isinstance(marker, str):
            marker = Marker(marker)
        # packaging has no public variable visitor; walk its parsed expression.
        pending = list(marker._markers)
        while pending:
            node = pending.pop()
            if isinstance(node, (list, tuple)):
                pending.extend(node)
            elif isinstance(node, Variable) and node.value in {
                "platform_release",
                "platform_version",
            }:
                raise ValueError(
                    f"Explicit target cannot evaluate {node.value}; "
                    "target OS release/version overrides are not supported"
                )
        environment = self.marker_environment
        return any(marker.evaluate({**environment, "extra": extra}) for extra in ("", *extras))

    @property
    def platforms(self) -> list[str]:
        arch = self.python_platform.split("-", 1)[0]
        if self.python_platform.endswith("-windows-msvc"):
            return [_WINDOWS[arch][0]]
        if self.python_platform.endswith("-apple-darwin"):
            return list(mac_platforms(version=(11, 0), arch="arm64" if arch == "aarch64" else arch))
        if self.python_platform.endswith("-musl"):
            return [f"musllinux_1_{minor}_{arch}" for minor in (2, 1)]
        baseline = int(self.python_platform.rsplit("_", 1)[1])
        minimum = 5 if arch == "x86_64" else 17
        legacy = {17: "manylinux2014", 12: "manylinux2010", 5: "manylinux1"}
        platforms = []
        for minor in range(baseline, minimum - 1, -1):
            platforms.append(f"manylinux_2_{minor}_{arch}")
            if minor in legacy:
                platforms.append(f"{legacy[minor]}_{arch}")
        return platforms

    def tags(self) -> Iterator[Tag]:
        platforms = self.platforms
        yield from cpython_tags(python_version=self.python, abis=[self.abi], platforms=platforms)
        yield from compatible_tags(
            python_version=self.python,
            interpreter=f"cp{self.python[0]}{self.python[1]}",
            platforms=platforms,
        )

    def resolver_args(self) -> list[str]:
        """uv flags; see module docs for macOS environment and ABI constraints."""
        return [
            "--python-version",
            self.python_version,
            "--python-platform",
            self.python_platform,
            "--only-binary",
            ":all:",
            "--no-build",
        ]

    def download_args(self) -> list[str]:
        return [
            "--python-version",
            self.python_version,
            "--implementation",
            "cp",
            "--abi",
            self.abi,
            *(arg for platform in self.platforms for arg in ("--platform", platform)),
        ]

    def manifest(self) -> dict:
        return {
            "mode": "explicit",
            "implementation": "cpython",
            "python": list(self.python),
            "python_full_version": self.python_version,
            "python_patch": self.python_patch,
            "abi": self.abi,
            "python_platform": self.python_platform,
            "platforms": self.platforms,
            "marker_environment": self.marker_environment,
        }
