"""Standalone, standard-library-only installer for an extracted wheelport bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import struct
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path, PurePosixPath


class InstallError(Exception):
    """An invalid bundle or an unsafe installation request."""


def _libc() -> list[str]:
    """Identify libc, including musl via this interpreter's ELF runtime loader."""
    reported = list(platform.libc_ver())
    # Some musl interpreters report the bare family "libc" with no version; fall
    # through to the ELF PT_INTERP probe instead of trusting that placeholder.
    if reported[0] not in ("", "unknown", "libc") or sys.platform != "linux":
        return reported
    try:
        with Path(sys.executable).open("rb") as stream:
            file_size = stream.seek(0, os.SEEK_END)
            stream.seek(0)
            header = stream.read(64)
            if len(header) < 16 or header[:4] != b"\x7fELF" or header[5] not in (1, 2):
                return ["unknown", ""]
            endian = "<" if header[5] == 1 else ">"
            if header[4] == 1:
                header_format, entry_format = "HHIIIIIHHHHHH", "IIIIIIII"
                offset_index, size_index = 1, 4
            elif header[4] == 2:
                header_format, entry_format = "HHIQQQIHHHHHH", "IIQQQQQQ"
                offset_index, size_index = 2, 5
            else:
                return ["unknown", ""]
            fields = struct.unpack_from(endian + header_format, header, 16)
            phoff, phentsize, phnum = fields[4], fields[8], fields[9]
            entry_size = struct.calcsize(endian + entry_format)
            if (not 0 < phnum <= 4096 or not entry_size <= phentsize <= 4096
                    or phoff < 16 + struct.calcsize(endian + header_format)
                    or phoff + phentsize * phnum > file_size):
                return ["unknown", ""]
            for index in range(phnum):
                stream.seek(phoff + index * phentsize)
                entry = struct.unpack(endian + entry_format, stream.read(entry_size))
                if entry[0] != 3:  # PT_INTERP names the loader already used by the OS.
                    continue
                offset, size = entry[offset_index], entry[size_index]
                if not 2 <= size <= 4096 or offset + size > file_size:
                    return ["unknown", ""]
                stream.seek(offset)
                data = stream.read(size)
                if len(data) != size or not data.endswith(b"\x00") or b"\x00" in data[:-1]:
                    return ["unknown", ""]
                loader = os.fsdecode(data[:-1])
                loader_path = PurePosixPath(loader)
                if (not loader_path.is_absolute() or not loader_path.name.startswith("ld-musl-")
                        or not loader_path.name.endswith(".so.1")):
                    return ["unknown", ""]
                # musl prints its version to stderr and exits nonzero without a program.
                result = subprocess.run([loader], check=False, capture_output=True,
                                        text=True, timeout=5)
                version = re.search(r"^Version ([0-9]+\.[0-9]+\.[0-9]+)\s*$",
                                    result.stderr, re.MULTILINE)
                if version:
                    return ["musl", version[1]]
                return ["unknown", ""]
    except (OSError, ValueError, struct.error, subprocess.SubprocessError):
        pass
    return ["unknown", ""]


def _check_target(target: object) -> None:
    if not isinstance(target, dict):
        raise InstallError("manifest target must be an object")
    if target.get("mode") == "explicit":
        python = target.get("python")
        full = target.get("python_full_version")
        patch = target.get("python_patch")
        markers = target.get("marker_environment")
        platforms = target.get("platforms")
        if (
            target.get("implementation") != "cpython"
            or not isinstance(python, list) or len(python) != 2
            or any(type(part) is not int or part < 0 for part in python)
            or python[0] != 3 or python[1] < 11
            or not isinstance(full, str)
            or re.fullmatch(r"3\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", full) is None
            or not full.startswith(f"{python[0]}.{python[1]}.")
            or (patch is not None and (type(patch) is not int or patch < 0
                                      or str(patch) != full.rsplit(".", 1)[1]))
            or target.get("abi") not in (
                f"cp{python[0]}{python[1]}",
                *([f"cp{python[0]}{python[1]}t"] if python >= [3, 13] else []),
            )
            or not isinstance(target.get("python_platform"), str)
            or re.fullmatch(
                r"(?:x86_64|aarch64|i686)-pc-windows-msvc|"
                r"(?:x86_64|aarch64)-(?:apple-darwin|unknown-linux-musl|"
                r"manylinux_2_(?:17|28|31|32|33|34|35|36|37|38|39|40))",
                target["python_platform"],
            ) is None
            or not isinstance(platforms, list) or not platforms
            or any(not isinstance(tag, str) or tag == "any"
                   or re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", tag) is None
                   for tag in platforms)
            or not isinstance(markers, dict)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in markers.items())
            or any(key not in markers for key in (
                "implementation_name", "implementation_version", "os_name", "platform_machine",
                "platform_release", "platform_system", "platform_version",
                "platform_python_implementation", "python_full_version", "python_version",
                "sys_platform", "extra",
            ))
            or markers.get("implementation_name") != "cpython"
            or markers.get("platform_python_implementation") != "CPython"
            or markers.get("python_version") != f"{python[0]}.{python[1]}"
            or markers.get("python_full_version") != full
            or markers.get("implementation_version") != full
        ):
            raise InstallError("invalid explicit target manifest schema")
        for key, actual in (("implementation", sys.implementation.name),
                            ("python", list(sys.version_info[:2]))):
            if target[key] != actual:
                raise InstallError(f"incompatible target {key}: bundle requires {target[key]!r}, "
                                   f"this interpreter provides {actual!r}")
        if sys.platform == "darwin" and target["python_platform"].endswith("-apple-darwin"):
            aliases = {"amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
            arch = sysconfig.get_platform().rsplit("-", 1)[-1].lower()
            arch = aliases.get(arch, arch)
            if arch not in ("x86_64", "aarch64"):
                # Universal builds need the active process architecture, including Rosetta.
                machine = platform.machine().lower()
                arch = aliases.get(machine, machine)
            required_arch = target["python_platform"].split("-", 1)[0]
            if arch != required_arch:
                raise InstallError(f"incompatible target architecture: bundle requires "
                                   f"{required_arch!r}, this interpreter provides {arch!r}")
        try:
            required_patch = int(full.rsplit(".", 1)[1])
        except ValueError as exc:
            raise InstallError("invalid explicit target Python patch version") from exc
        if sys.version_info.micro < required_patch or (
            patch is not None and sys.version_info.micro != patch
        ):
            raise InstallError(f"incompatible target python_full_version: bundle requires {full}"
                               f"{' exactly' if patch is not None else ' or later'}, "
                               f"this interpreter provides {platform.python_version()}")
        if sys.version_info.micro != required_patch:
            print(f"Warning: target markers were resolved for Python {full}; "
                  "patch-specific markers may diverge on this interpreter.", file=sys.stderr)
        return
    if target.get("mode", "native") != "native":
        raise InstallError("manifest target mode must be native or explicit")
    python = target.get("python")
    libc = target.get("libc")
    if (
        not isinstance(python, list)
        or len(python) != 2
        or any(type(part) is not int or part < 0 for part in python)
        or not isinstance(target.get("implementation"), str)
        or not isinstance(target.get("platform"), str)
        or not isinstance(target.get("soabi"), str)
        or not isinstance(libc, list)
        or len(libc) != 2
        or any(not isinstance(part, str) for part in libc)
    ):
        raise InstallError("manifest target must specify implementation, python [major, minor], "
                           "platform, soabi, and libc [family, version]")
    actual = {
        "platform": sysconfig.get_platform(),
        "implementation": sys.implementation.name,
        "python": list(sys.version_info[:2]),
        "soabi": sysconfig.get_config_var("SOABI") or "",
    }
    for key, value in actual.items():
        if target[key] != value:
            raise InstallError(
                f"incompatible target {key}: bundle requires {target[key]!r}, "
                f"this interpreter provides {value!r}; use a matching interpreter/bundle"
            )
    actual_libc = _libc()
    if sys.platform == "linux" and actual_libc[0] in ("", "unknown"):
        raise InstallError("incompatible target libc: could not identify Linux libc; "
                           "use an interpreter with an identifiable glibc or musl runtime")
    compatible = libc == actual_libc
    if libc[0] == actual_libc[0] and libc[0] in ("glibc", "musl"):
        versions = []
        for version in (libc[1], actual_libc[1]):
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version) is None:
                break
            try:
                parts = [int(part) for part in version.split(".")]
            except ValueError:
                break
            while parts and parts[-1] == 0:
                parts.pop()
            versions.append(tuple(parts))
        if len(versions) == 2:
            compatible = versions[0] <= versions[1]
    if not compatible:
        raise InstallError(
            f"incompatible target libc: bundle requires {libc!r}, "
            f"this interpreter provides {actual_libc!r}; use a matching interpreter/bundle"
        )


def _wheel_path(root: Path, directory: str, entry: object) -> Path:
    if not isinstance(entry, dict):
        raise InstallError(f"manifest {directory} entry must be an object")
    filename = entry.get("filename")
    if (
        not isinstance(filename, str)
        or not filename.endswith(".whl")
        or any(char in filename for char in ("/", "\\", ":", "\x00"))
        or filename in (".", "..")
    ):
        raise InstallError(f"unsafe wheel filename {filename!r}: expected a direct .whl basename")
    digest = entry.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise InstallError(f"invalid SHA-256 for {directory}/{filename}")
    folder = root / directory
    path = folder / filename
    if folder.is_symlink() or not folder.is_dir():
        raise InstallError(f"{folder} must be a real directory, not a symlink")
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"{path} must be a regular file, not a symlink")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual != digest.lower():
        raise InstallError(
            f"SHA-256 mismatch for {directory}/{filename}: expected {digest.lower()}, "
            f"got {actual}; the bundle is corrupt or has been modified. "
            "Obtain a fresh bundle from a trusted source."
        )
    return path


def verify_bundle(bundle_dir: Path) -> tuple[Path, list[Path]]:
    """Validate target, paths, hashes and wheel tags; return verified local wheels."""
    root = Path(bundle_dir).resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InstallError(f"{manifest_path} must be a regular file, not a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise InstallError(f"invalid manifest.json: {exc}") from exc
    if not isinstance(manifest, dict) or type(manifest.get("format")) is not int:
        raise InstallError("manifest format must be the integer 1")
    if manifest["format"] != 1:
        raise InstallError(f"unsupported manifest format {manifest['format']!r}; expected 1")
    _check_target(manifest.get("target"))
    entries = manifest.get("wheels")
    if not isinstance(entries, list):
        raise InstallError("manifest wheels must be a list")
    bootstrap = _wheel_path(root, "bootstrap", manifest.get("bootstrap"))
    wheels = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(key), str) or not entry[key].strip()
            for key in ("name", "version")
        ):
            raise InstallError("each manifest wheel must have nonempty name and version strings")
        path = _wheel_path(root, "wheels", entry)
        if path.name == bootstrap.name and entry["sha256"].lower() != manifest["bootstrap"]["sha256"].lower():
            raise InstallError("bootstrap pip and wheelhouse copy must have identical hashes")
        if path.name in seen:
            raise InstallError(f"duplicate wheel filename in manifest: {path.name}")
        seen.add(path.name)
        wheels.append(path)
    # Import packaging only in a child process, after every wheel hash is verified.
    try:
        subprocess.run(
            [sys.executable, "-I", "-c",
             ("import json,sys; sys.path.insert(0,sys.argv[1]); "
              "from pip._vendor.packaging.tags import Tag,sys_tags; "
              "from pip._vendor.packaging.utils import parse_wheel_filename; "
              "supported=set(sys_tags()); target=json.loads(sys.argv[2])\n"
              "if target is not None:\n"
              " interpreter='cp'+''.join(map(str,target['python']))\n"
              " required={Tag(interpreter,target['abi'],p) for p in target['platforms']}\n"
              " if supported.isdisjoint(required):\n"
              "  sys.exit('incompatible target ABI/architecture/platform: '+"
              "target['abi']+' '+target['python_platform'])\n"
              "for name in sys.argv[3:]:\n"
             " try: wheel_tags=parse_wheel_filename(name)[3]\n"
             " except ValueError as exc: sys.exit(str(exc))\n"
             " if supported.isdisjoint(wheel_tags):\n"
             "  sys.exit('incompatible wheel tags: '+name)\n"),
              str(bootstrap), json.dumps(manifest["target"]
                                         if manifest["target"].get("mode") == "explicit" else None),
              bootstrap.name, *(wheel.name for wheel in wheels)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or str(exc)
        raise InstallError(f"wheel compatibility check failed: {detail}") from exc
    return bootstrap, wheels


def install_bundle(bundle_dir: Path, venv_dir: Path, *, reuse: bool = False) -> None:
    """Install only verified local wheels, including pip itself, into a virtualenv."""
    bootstrap, wheels = verify_bundle(bundle_dir)
    destination = Path(venv_dir).absolute()
    if destination.is_symlink():
        raise InstallError(f"refusing symlink virtualenv destination: {destination}")
    python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
    # Also disable machine-wide pip configuration, which --isolated does not ignore.
    env["PIP_CONFIG_FILE"] = os.devnull
    if destination.exists():
        if not reuse:
            raise InstallError(f"{destination} already exists; choose another path or use --reuse")
        config = destination / "pyvenv.cfg"
        if not destination.is_dir() or config.is_symlink() or not config.is_file():
            raise InstallError(f"--reuse requires an existing virtualenv: {destination}")
        try:
            system_sites = [value.strip().lower()
                            for line in config.read_text(encoding="utf-8").splitlines()
                            for key, sep, value in [line.partition("=")]
                            if sep and key.strip().lower() == "include-system-site-packages"]
        except UnicodeError as exc:
            raise InstallError(f"invalid virtualenv configuration: {config}") from exc
        if system_sites != ["false"]:
            raise InstallError("--reuse requires include-system-site-packages = false")
        probe = subprocess.run(
            [str(python), "-I", "-c",
              ("import json,runpy,sys,sysconfig; from pathlib import Path; "
               "installer=runpy.run_path(sys.argv[1],run_name='libc_probe'); "
               "libc=installer['_libc'](); "
               "target=json.loads((Path(sys.argv[2])/'manifest.json').read_text())['target']; "
               "target.get('mode') == 'explicit' and "
               "installer['verify_bundle'](Path(sys.argv[2])); "
              "print(json.dumps({"
             "'implementation':sys.implementation.name,'python':list(sys.version_info[:2]),"
             "'platform':sysconfig.get_platform(),'soabi':sysconfig.get_config_var('SOABI') or '',"
              "'libc':libc,'prefix':sys.prefix,'base_prefix':sys.base_prefix}))"),
              str(Path(__file__).resolve()), str(Path(bundle_dir).resolve())],
            check=True, capture_output=True, text=True, env=env,
        )
        try:
            target = json.loads(probe.stdout)
            _check_target(target)
            if target["libc"] != _libc():
                raise InstallError("virtualenv interpreter must have the same libc as this interpreter")
            if not all(isinstance(target.get(key), str) and target[key]
                       for key in ("prefix", "base_prefix")):
                raise InstallError(f"could not validate virtualenv interpreter: {python}")
            if Path(target["prefix"]).resolve() == Path(target["base_prefix"]).resolve():
                raise InstallError(f"{python} is not a virtualenv interpreter")
            if Path(target["prefix"]).resolve() != destination.resolve():
                raise InstallError(f"{python} does not belong to the requested virtualenv")
        except (ValueError, KeyError, TypeError) as exc:
            raise InstallError(f"could not validate virtualenv interpreter: {python}") from exc
    else:
        # POSIX interpreters can locate shared libraries relative to their real executable.
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(destination)
    subprocess.run(
        [
            str(python), "-I", "-c",
            ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
             "runpy.run_module('pip',run_name='__main__')"),
            str(bootstrap),
            "--isolated", "--disable-pip-version-check", "install",
            "--no-index", "--no-deps", "--no-cache-dir", "--only-binary", ":all:",
            *(["--force-reinstall"] if reuse else []),
            str(bootstrap), *(str(wheel) for wheel in wheels if wheel.name != bootstrap.name),
        ],
        check=True,
        env=env,
    )
    subprocess.run(
        [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "check"],
        check=True, env=env,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=Path(".venv"),
                        help="virtualenv destination (default: .venv relative to current directory)")
    parser.add_argument("--verify-only", action="store_true", help="verify without creating a venv")
    parser.add_argument("--reuse", action="store_true", help="allow modifying an existing virtualenv")
    args = parser.parse_args(argv)
    bundle_dir = Path(__file__).resolve().parent
    try:
        if args.verify_only:
            verify_bundle(bundle_dir)
            print("Bundle verified; no environment changes made.")
        else:
            install_bundle(bundle_dir, args.venv, reuse=args.reuse)
            print(f"Installed bundle into {args.venv.absolute()}")
    except (InstallError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
