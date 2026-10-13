"""Installer tests using only the standard library.

Set WHEELPORT_TEST_BUNDLE to an extracted, compatible bundle for a real pip install.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

INSTALLER = Path(__file__).resolve().parents[1] / "src" / "wheelport" / "install.py"
SPEC = importlib.util.spec_from_file_location("standalone_install", INSTALLER)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
NATIVE_LIBC = tuple(installer._libc())
if sys.platform == "linux" and NATIVE_LIBC[0] in ("", "unknown"):
    NATIVE_LIBC = ("glibc", "2.17")


def target():
    return {
        "implementation": sys.implementation.name,
        "python": list(sys.version_info[:2]),
        "platform": sysconfig.get_platform(),
        "soabi": sysconfig.get_config_var("SOABI") or "",
        "libc": installer._libc(),
    }


def explicit_target():
    major, minor = sys.version_info[:2]
    full = f"{major}.{minor}.0"
    return {
        "mode": "explicit", "implementation": "cpython", "python": [major, minor],
        "python_full_version": full, "python_patch": None, "abi": f"cp{major}{minor}",
        "python_platform": "x86_64-pc-windows-msvc", "platforms": ["win_amd64"],
        "marker_environment": {
            "implementation_name": "cpython", "implementation_version": full,
            "os_name": "nt", "platform_machine": "AMD64", "platform_release": "",
            "platform_system": "Windows", "platform_version": "",
            "platform_python_implementation": "CPython", "python_full_version": full,
            "python_version": f"{major}.{minor}", "sys_platform": "win32", "extra": "",
        },
    }


class InstallerTests(unittest.TestCase):
    def setUp(self):
        # Keep native probes independent of generic subprocess mocks; detector tests override this.
        libc = mock.patch.object(installer.platform, "libc_ver", return_value=NATIVE_LIBC)
        libc.start()
        self.addCleanup(libc.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.manifest = {
            "format": 1,
            "target": target(),
            "bootstrap": self.bootstrap(),
            "wheels": [dict(self.wheel("wheels", "demo-1-py3-none-any.whl", b"demo"),
                            name="demo", version="1")],
        }
        self.write_manifest()

    def bootstrap(self, tags=("py3-none-any",)):
        # A stdlib-only bootstrap test double, not an installable pip distribution.
        bootstrap = io.BytesIO()
        with zipfile.ZipFile(bootstrap, "w") as archive:
            for package in ("pip", "pip/_vendor", "pip/_vendor/packaging"):
                archive.writestr(f"{package}/__init__.py", "")
            archive.writestr("pip/_vendor/packaging/tags.py",
                              "def Tag(interpreter, abi, platform):\n"
                              " return '-'.join((interpreter, abi, platform))\n"
                              f"def sys_tags(): return iter({tags!r})\n")
            archive.writestr("pip/_vendor/packaging/utils.py",
                             "def parse_wheel_filename(name):\n"
                             " parts = name[:-4].split('-')\n"
                             " if len(parts) != 5: raise ValueError('invalid wheel filename')\n"
                             " return parts[0], parts[1], (), {'-'.join(parts[-3:])}\n")
        return self.wheel("bootstrap", "pip-25.0-py3-none-any.whl", bootstrap.getvalue())

    def wheel(self, directory, filename, content):
        folder = self.bundle / directory
        folder.mkdir(exist_ok=True)
        (folder / filename).write_bytes(content)
        return {"filename": filename, "sha256": hashlib.sha256(content).hexdigest()}

    def write_manifest(self):
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_standalone_stdlib_import(self):
        subprocess.run(
            [sys.executable, "-I", "-S", "-c",
             "import runpy,sys; runpy.run_path(sys.argv[1],run_name='stdlib_import')",
             str(INSTALLER)],
            check=True, capture_output=True, text=True,
        )

    def test_valid_bundle_without_requirements(self):
        bootstrap, wheels = installer.verify_bundle(self.bundle)
        # verify_bundle resolves the bundle root; canonicalize the expected paths
        # so Windows short names and macOS /var symlinks compare equal.
        self.assertEqual(
            bootstrap,
            (self.bundle / "bootstrap" / self.manifest["bootstrap"]["filename"]).resolve(),
        )
        self.assertEqual(
            wheels, [(self.bundle / "wheels" / self.manifest["wheels"][0]["filename"]).resolve()]
        )

    def test_tamper_fails_before_creating_environment(self):
        for section in ("bootstrap", "wheels"):
            with self.subTest(section=section):
                entry = self.manifest[section] if section == "bootstrap" else self.manifest[section][0]
                path = self.bundle / section / entry["filename"]
                original = path.read_bytes()
                path.write_bytes(b"tampered")
                with mock.patch.object(installer.venv, "EnvBuilder") as builder:
                    with mock.patch.object(installer.subprocess, "run") as run:
                        with self.assertRaisesRegex(installer.InstallError, "SHA-256 mismatch.*fresh bundle"):
                            installer.install_bundle(self.bundle, self.root / "env")
                        run.assert_not_called()
                    builder.assert_not_called()
                path.write_bytes(original)

    def test_rejects_unsafe_filenames_in_both_sections(self):
        for section in ("bootstrap", "wheels"):
            entry = self.manifest[section] if section == "bootstrap" else self.manifest[section][0]
            original = entry["filename"]
            for filename in ("../evil.whl", "/tmp/evil.whl", "a/b.whl", "a\\b.whl",
                             "C:evil.whl", "https://host/evil.whl", "a\x00.whl", "not-a-wheel"):
                with self.subTest(section=section, filename=filename):
                    entry["filename"] = filename
                    self.write_manifest()
                    with self.assertRaisesRegex(installer.InstallError, "unsafe wheel filename"):
                        installer.verify_bundle(self.bundle)
            entry["filename"] = original

    def require_symlink(self, *, directory=False):
        probe = self.root / "symlink-probe"
        target = self.bundle if directory else self.bundle / "manifest.json"
        try:
            probe.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                self.skipTest("symlink creation requires Windows privileges")
            raise
        probe.unlink()

    def test_symlink_files_and_directories_rejected(self):
        for section in ("bootstrap", "wheels"):
            entry = self.manifest[section] if section == "bootstrap" else self.manifest[section][0]
            folder = self.bundle / section
            wheel = folder / entry["filename"]
            outside = self.root / entry["filename"]
            with self.subTest(section=section, kind="file"):
                self.require_symlink()
                wheel.rename(outside)
                wheel.symlink_to(outside)
                with self.assertRaisesRegex(installer.InstallError, "not a symlink"):
                    installer.verify_bundle(self.bundle)
                wheel.unlink()
                outside.rename(wheel)
            with self.subTest(section=section, kind="directory"):
                self.require_symlink(directory=True)
                moved = self.root / section
                folder.rename(moved)
                folder.symlink_to(moved, target_is_directory=True)
                with self.assertRaisesRegex(installer.InstallError, "not a symlink"):
                    installer.verify_bundle(self.bundle)
                folder.unlink()
                moved.rename(folder)

    def test_manifest_symlink_rejected(self):
        self.require_symlink()
        manifest = self.bundle / "manifest.json"
        outside = self.root / "manifest.json"
        manifest.rename(outside)
        manifest.symlink_to(outside)
        with self.assertRaisesRegex(installer.InstallError, "not a symlink"):
            installer.verify_bundle(self.bundle)

    def test_incompatible_target_fails_before_hashing_or_creation(self):
        for key, value in (("python", [0, 0]), ("platform", "wrong-platform"),
                           ("implementation", "wrong-implementation"),
                           ("soabi", "wrong-soabi"), ("libc", ["wrong-libc", "1"])):
            with self.subTest(key=key):
                original = self.manifest["target"][key]
                self.manifest["target"][key] = value
                self.write_manifest()
                with (mock.patch.object(installer, "_wheel_path") as wheel_path,
                      mock.patch.object(installer.venv, "EnvBuilder") as builder):
                    with self.assertRaisesRegex(installer.InstallError, f"incompatible target {key}"):
                        installer.install_bundle(self.bundle, self.root / "env")
                    wheel_path.assert_not_called()
                    builder.assert_not_called()
                self.manifest["target"][key] = original

    def test_platform_mismatch_reported_first(self):
        incompatible = dict(target(), platform="wrong", soabi="wrong", libc=["wrong", "1"])
        with self.assertRaisesRegex(installer.InstallError, "incompatible target platform"):
            installer._check_target(incompatible)

    def test_libc_compatibility(self):
        cases = [
            (["glibc", "2.39"], ["glibc", "2.28"], True),
            (["glibc", "2.9"], ["glibc", "2.10"], False),
            (["glibc", "2.28"], ["glibc", "2.39"], False),
            (["musl", "1.2.5"], ["musl", "1.2.3"], True),
            (["musl", "1.2.3"], ["musl", "1.2.5"], False),
            (["glibc", "2.39"], ["musl", "1.2.3"], False),
            (["glibc", "2.39"], ["", ""], False),
            (["", ""], ["glibc", "2.28"], False),
            (["", ""], ["", ""], False),
            (["musl", ""], ["musl", ""], True),
            (["musl", "1.2.5"], ["musl", ""], False),
            (["glibc", "2.39-custom"], ["glibc", "2.28"], False),
            (["glibc", "2.39-custom"], ["glibc", "2.39-custom"], True),
            (["other", "2"], ["other", "1"], False),
            (["other", "2"], ["other", "2"], True),
            (["glibc", "2.28"], ["glibc", "2.28.0"], True),
            (["glibc", "2.39"], ["glibc", "9" * 5000], False),
        ]
        for actual, required, compatible in cases:
            with (self.subTest(actual=actual, required=required),
                  mock.patch.object(installer.sys, "platform", "linux"),
                  mock.patch.object(installer.platform, "libc_ver", return_value=tuple(actual))):
                bundle_target = dict(target(), libc=required)
                if compatible:
                    installer._check_target(bundle_target)
                else:
                    with self.assertRaisesRegex(installer.InstallError, "incompatible target libc"):
                        installer._check_target(bundle_target)

    def test_libc_musl_elf_and_bounded_invalid_inputs(self):
        loader = b"/lib/ld-musl-test.so.1\x00"
        for elf_class in (1, 2):
            for encoding, endian in ((1, "<"), (2, ">")):
                header_format = "HHIIIIIHHHHHH" if elf_class == 1 else "HHIQQQIHHHHHH"
                entry_format = "IIIIIIII" if elf_class == 1 else "IIQQQQQQ"
                header_size = 16 + struct.calcsize(endian + header_format)
                entry_size = struct.calcsize(endian + entry_format)
                offset = header_size + entry_size
                ident = b"\x7fELF" + bytes((elf_class, encoding, 1)) + b"\x00" * 9
                header = ident + struct.pack(endian + header_format, 2, 0, 1, 0,
                                             header_size, 0, 0, header_size, entry_size, 1, 0, 0, 0)
                entry = ([3, offset, 0, 0, len(loader), 0, 0, 0] if elf_class == 1
                         else [3, 0, offset, 0, 0, len(loader), 0, 0])
                binary = header + struct.pack(endian + entry_format, *entry) + loader
                with (self.subTest(elf_class=elf_class, endian=endian),
                      mock.patch.object(installer.platform, "libc_ver", return_value=("", "")),
                      mock.patch.object(installer.sys, "platform", "linux"),
                      mock.patch.object(installer.sys, "executable", "/synthetic/python"),
                      mock.patch.object(installer.Path, "open", autospec=True,
                                        return_value=io.BytesIO(binary)) as opened,
                      mock.patch.object(installer.subprocess, "run") as run):
                    run.return_value.stderr = "musl libc (test)\nVersion 1.2.5\nDynamic Program Loader\n"
                    self.assertEqual(installer._libc(), ["musl", "1.2.5"])
                    opened.assert_called_once_with(Path("/synthetic/python"), "rb")
                    run.assert_called_once_with([loader[:-1].decode()], check=False,
                                                capture_output=True, text=True, timeout=5)
                bad_offset = entry.copy()
                bad_offset[1 if elf_class == 1 else 2] = len(binary) + 1
                bad_size = entry.copy()
                bad_size[4 if elf_class == 1 else 5] = 4097
                invalid = [binary[:20], binary[:-1], binary[:-1] + b"x",
                           header + struct.pack(endian + entry_format, *bad_offset) + loader,
                           header + struct.pack(endian + entry_format, *bad_size) + loader,
                           binary.replace(b"/lib/", b"./ib/"),
                           binary.replace(b"ld-musl", b"ld-glib"),
                           binary.replace(b"test", b"te\x00t")]
                for field, value in ((4, len(binary) + 1), (4, 0), (8, entry_size - 1),
                                     (8, 4097), (9, 0), (9, 4097)):
                    fields = list(struct.unpack(endian + header_format, header[16:]))
                    fields[field] = value
                    invalid.append(ident + struct.pack(endian + header_format, *fields)
                                   + binary[header_size:])
                for data in invalid:
                    with (self.subTest(elf_class=elf_class, endian=endian, data=data),
                          mock.patch.object(installer.platform, "libc_ver", return_value=("", "")),
                          mock.patch.object(installer.sys, "platform", "linux"),
                          mock.patch.object(installer.Path, "open", return_value=io.BytesIO(data)),
                          mock.patch.object(installer.subprocess, "run") as run):
                        self.assertEqual(installer._libc(), ["unknown", ""])
                        run.assert_not_called()
                for failure in (None, OSError("loader unavailable"),
                                subprocess.TimeoutExpired([loader[:-1].decode()], 5)):
                    with (self.subTest(elf_class=elf_class, endian=endian, failure=failure),
                          mock.patch.object(installer.platform, "libc_ver", return_value=("", "")),
                          mock.patch.object(installer.sys, "platform", "linux"),
                          mock.patch.object(installer.Path, "open", return_value=io.BytesIO(binary)),
                          mock.patch.object(installer.subprocess, "run", side_effect=failure) as run):
                        run.return_value.stderr = "no version available"
                        self.assertEqual(installer._libc(), ["unknown", ""])
                        run.assert_called_once()

    def test_libc_musl_placeholder_family_falls_through_to_elf_probe(self):
        loader = b"/lib/ld-musl-test.so.1\x00"
        ident = b"\x7fELF" + bytes((2, 1, 1)) + b"\x00" * 9
        header_format, entry_format = "HHIQQQIHHHHHH", "IIQQQQQQ"
        header_size = 16 + struct.calcsize("<" + header_format)
        entry_size = struct.calcsize("<" + entry_format)
        offset = header_size + entry_size
        header = ident + struct.pack("<" + header_format, 2, 0, 1, 0,
                                     header_size, 0, 0, header_size, entry_size, 1, 0, 0, 0)
        entry = [3, 0, offset, 0, 0, len(loader), 0, 0]
        binary = header + struct.pack("<" + entry_format, *entry) + loader
        with (mock.patch.object(installer.platform, "libc_ver", return_value=("libc", "")),
              mock.patch.object(installer.sys, "platform", "linux"),
              mock.patch.object(installer.sys, "executable", "/synthetic/python"),
              mock.patch.object(installer.Path, "open", autospec=True,
                                return_value=io.BytesIO(binary)),
              mock.patch.object(installer.subprocess, "run") as run):
            run.return_value.stderr = "musl libc (test)\nVersion 1.2.5\nDynamic Program Loader\n"
            self.assertEqual(installer._libc(), ["musl", "1.2.5"])

    def test_unidentified_linux_libc_rejected_even_when_equal(self):
        for required in (["", ""], ["unknown", ""]):
            with (self.subTest(required=required),
                  mock.patch.object(installer.platform, "libc_ver", return_value=("", "")),
                  mock.patch.object(installer.sys, "platform", "linux"),
                  mock.patch.object(installer.Path, "open", side_effect=OSError("unreadable")),
                  mock.patch.object(installer.subprocess, "run") as run):
                self.assertEqual(installer._libc(), ["unknown", ""])
                with self.assertRaisesRegex(installer.InstallError, "could not identify Linux libc"):
                    installer._check_target(dict(target(), libc=required))
                run.assert_not_called()

    def test_empty_libc_allowed_on_macos_and_windows(self):
        for system in ("darwin", "win32"):
            with (self.subTest(system=system),
                  mock.patch.object(installer.platform, "libc_ver", return_value=("", "")),
                  mock.patch.object(installer.sys, "platform", system),
                  mock.patch.object(installer.Path, "open") as opened,
                  mock.patch.object(installer.subprocess, "run") as run):
                self.assertEqual(installer._libc(), ["", ""])
                installer._check_target(dict(target(), libc=["", ""]))
                opened.assert_not_called()
                run.assert_not_called()

    def test_malformed_target_fields(self):
        changes = [("python", value) for value in (None, [3], [True, 1], [-1, 1], [3, "1"])]
        changes += [("libc", value) for value in (None, "glibc", [], ["glibc"], ["glibc", 2])]
        changes += [("soabi", None), ("implementation", None), ("platform", None)]
        for key, value in changes:
            with (self.subTest(key=key, value=value),
                  self.assertRaisesRegex(installer.InstallError, "manifest target must specify")):
                installer._check_target(dict(target(), **{key: value}))
        for key in target():
            incomplete = target()
            del incomplete[key]
            with self.subTest(missing=key), self.assertRaises(installer.InstallError):
                installer._check_target(incomplete)

    def test_explicit_target_does_not_require_native_fields(self):
        required = explicit_target()
        required.pop("python_patch")  # Current Target.manifest() has no patch-intent field.
        with (mock.patch.object(installer.sysconfig, "get_platform") as get_platform,
              mock.patch.object(installer.sysconfig, "get_config_var") as get_config,
              mock.patch.object(installer, "_libc") as libc,
              contextlib.redirect_stderr(io.StringIO()) as warnings):
            installer._check_target(required)
        get_platform.assert_not_called()
        get_config.assert_not_called()
        libc.assert_not_called()
        if sys.version_info.micro:
            self.assertIn("patch-specific markers may diverge", warnings.getvalue())

    def test_explicit_target_version_and_implementation_checked_before_hashing(self):
        for key in ("python", "implementation"):
            required = explicit_target()
            if key == "python":
                minor = sys.version_info.minor + 1
                full = f"3.{minor}.0"
                required.update(python=[3, minor], python_full_version=full, abi=f"cp3{minor}")
                required["marker_environment"].update(python_version=f"3.{minor}",
                                                      python_full_version=full,
                                                      implementation_version=full)
            self.manifest["target"] = required
            self.write_manifest()
            with (self.subTest(key=key),
                  mock.patch.object(installer.sys, "implementation",
                                    mock.Mock()) if key == "implementation"
                  else contextlib.nullcontext(),
                  mock.patch.object(installer, "_wheel_path") as wheel_path,
                  mock.patch.object(installer.venv, "EnvBuilder") as builder):
                if key == "implementation":
                    installer.sys.implementation.name = "pypy"
                with self.assertRaisesRegex(installer.InstallError, f"incompatible target {key}"):
                    installer.install_bundle(self.bundle, self.root / "env")
                wheel_path.assert_not_called()
                builder.assert_not_called()

    def test_explicit_target_patch_floor_and_exact_intent(self):
        for patch, exact, compatible in ((0, None, True),
                                         (sys.version_info.micro, sys.version_info.micro, True),
                                         (sys.version_info.micro + 1, None, False),
                                         (sys.version_info.micro + 1,
                                          sys.version_info.micro + 1, False)):
            required = explicit_target()
            full = f"{sys.version_info.major}.{sys.version_info.minor}.{patch}"
            required.update(python_full_version=full, python_patch=exact)
            required["marker_environment"].update(python_full_version=full,
                                                  implementation_version=full)
            with self.subTest(patch=patch, exact=exact), contextlib.redirect_stderr(io.StringIO()):
                if compatible:
                    installer._check_target(required)
                else:
                    with self.assertRaisesRegex(installer.InstallError, "python_full_version"):
                        installer._check_target(required)
        if sys.version_info.micro:
            required = explicit_target()
            required["python_patch"] = 0
            with self.assertRaisesRegex(installer.InstallError, "requires .* exactly"):
                installer._check_target(required)

    def test_malformed_explicit_target_fields(self):
        changes = [
            ("mode", "unknown"), ("implementation", "pypy"), ("python", [True, 12]),
            ("python", [3]), ("python", [3, "12"]), ("python", None), ("python", [3, 10]),
            ("python_full_version", None), ("python_full_version", "3.12"),
            ("python_full_version", "3.999.0"), ("python_patch", True),
            ("python_full_version", f"3.{sys.version_info.minor}." + "9" * 5000),
            ("python_patch", -1), ("python_patch", "0"), ("python_patch", 1),
            ("abi", "abi3"), ("abi", None), ("python_platform", "windows"),
            ("python_platform", None), ("python_platform", "x86_64-manylinux_2_999"),
            ("platforms", []), ("platforms", "win_amd64"), ("platforms", [None]),
            ("platforms", ["any"]), ("platforms", ["win-amd64"]),
            ("marker_environment", {}), ("marker_environment", None),
            ("marker_environment", {**explicit_target()["marker_environment"], "extra": None}),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value), self.assertRaises(installer.InstallError):
                installer._check_target(dict(explicit_target(), **{key: value}))
        for key in explicit_target():
            if key == "python_patch":
                continue
            required = explicit_target()
            del required[key]
            with self.subTest(missing=key), self.assertRaises(installer.InstallError):
                installer._check_target(required)

    def test_explicit_child_checks_target_even_for_pure_wheels(self):
        required = explicit_target()
        self.manifest["target"] = required
        interpreter = required["abi"]
        for tag, compatible in ((f"{interpreter}-{interpreter}-win_amd64", True),
                                (f"{interpreter}-{interpreter}-win32", False),
                                (f"{interpreter}-{interpreter}-win_arm64", False),
                                (f"{interpreter}-{interpreter}-manylinux_2_17_x86_64", False),
                                (f"{interpreter}-{interpreter}t-win_amd64", False),
                                (f"{interpreter}-abi3-win_amd64", False),
                                ("py3-none-any", False)):
            self.manifest["bootstrap"] = self.bootstrap(("py3-none-any", tag))
            self.write_manifest()
            with (self.subTest(tag=tag),
                  mock.patch.object(installer.venv, "EnvBuilder") as builder,
                  contextlib.redirect_stderr(io.StringIO())):
                if compatible:
                    installer.verify_bundle(self.bundle)
                else:
                    with self.assertRaisesRegex(installer.InstallError,
                                                "incompatible target ABI/architecture/platform"):
                        installer.install_bundle(self.bundle, self.root / "env")
                builder.assert_not_called()

    def test_explicit_child_still_checks_wheel_tags(self):
        required = explicit_target()
        self.manifest["target"] = required
        abi = required["abi"]
        self.manifest["bootstrap"] = self.bootstrap(("py3-none-any", f"{abi}-{abi}-win_amd64"))
        self.manifest["wheels"] = [dict(self.wheel("wheels", "demo-1-cp00-cp00-win32.whl", b"demo"),
                                        name="demo", version="1")]
        self.write_manifest()
        with self.assertRaisesRegex(installer.InstallError, "incompatible wheel tags"):
            installer.verify_bundle(self.bundle)

    def test_explicit_macos_universal2_requires_matching_process_architecture(self):
        for arch, suffix in (("aarch64", "arm64"), ("x86_64", "x86_64")):
            required = explicit_target()
            required.update(python_platform=f"{arch}-apple-darwin",
                            platforms=[f"macosx_11_0_{suffix}", "macosx_11_0_universal2"])
            required["marker_environment"].update(os_name="posix", platform_machine=suffix,
                                                  platform_system="Darwin", sys_platform="darwin")
            self.manifest["target"] = required
            abi = required["abi"]
            self.manifest["bootstrap"] = self.bootstrap(
                ("py3-none-any", f"{abi}-{abi}-macosx_11_0_universal2")
            )
            self.write_manifest()
            cases = (("arm64", "x86_64", arch == "aarch64"),
                     ("x86_64", "arm64", arch == "x86_64"),
                     ("universal2", "ARM64", arch == "aarch64"),
                     ("universal2", "aarch64", arch == "aarch64"),
                     ("universal2", "AMD64", arch == "x86_64"),
                     ("universal2", "x86_64", arch == "x86_64"),
                     ("universal2", "unknown", False))
            for actual_suffix, machine, compatible in cases:
                with (self.subTest(arch=arch, suffix=actual_suffix, machine=machine),
                      mock.patch.object(installer.sys, "platform", "darwin"),
                      mock.patch.object(installer.sysconfig, "get_platform",
                                        return_value=f"macosx-11.0-{actual_suffix}"),
                      mock.patch.object(installer.platform, "machine", return_value=machine),
                      mock.patch.object(installer.venv, "EnvBuilder") as builder,
                      contextlib.redirect_stderr(io.StringIO())):
                    if compatible:
                        installer.verify_bundle(self.bundle)
                    else:
                        with (mock.patch.object(installer, "_wheel_path") as wheel_path,
                              mock.patch.object(installer.subprocess, "run") as run,
                              self.assertRaisesRegex(installer.InstallError,
                                                     "incompatible target architecture")):
                            installer.install_bundle(self.bundle, self.root / "env")
                        wheel_path.assert_not_called()
                        run.assert_not_called()
                    builder.assert_not_called()
            # Matching architecture does not replace the independent pip tag check.
            self.manifest["bootstrap"] = self.bootstrap()
            self.write_manifest()
            with (mock.patch.object(installer.sys, "platform", "darwin"),
                  mock.patch.object(installer.sysconfig, "get_platform",
                                    return_value=f"macosx-11.0-{suffix}"),
                  contextlib.redirect_stderr(io.StringIO()),
                  self.assertRaisesRegex(installer.InstallError,
                                         "incompatible target ABI/architecture/platform")):
                installer.verify_bundle(self.bundle)

    def test_explicit_child_free_threaded_abi_uses_cpython_interpreter_tag(self):
        self.manifest["target"] = explicit_target()
        self.write_manifest()
        with (mock.patch.object(installer.subprocess, "run") as run,
              contextlib.redirect_stderr(io.StringIO())):
            installer.verify_bundle(self.bundle)
            command = run.call_args.args[0]
        required = explicit_target()
        required.update(python=[3, 13], abi="cp313t", python_full_version="3.13.0")
        required["marker_environment"].update(python_version="3.13", python_full_version="3.13.0",
                                              implementation_version="3.13.0")
        command[5] = json.dumps(required)
        for abi, compatible in (("cp313t", True), ("cp313", False)):
            self.bootstrap(("py3-none-any", f"cp313-{abi}-win_amd64"))
            with self.subTest(abi=abi):
                result = subprocess.run(command, check=False, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if compatible else 1)
                if not compatible:
                    self.assertIn("incompatible target ABI/architecture/platform", result.stderr)

    def test_reuse_probe_verifies_original_explicit_target_in_destination(self):
        required = explicit_target()
        abi = required["abi"]
        self.manifest["target"] = required
        self.manifest["bootstrap"] = self.bootstrap(("py3-none-any", f"{abi}-{abi}-win_amd64"))
        self.write_manifest()
        destination = self.root / "env"
        destination.mkdir()
        (destination / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        probe_target = dict(target(), prefix=str(destination), base_prefix=str(self.root / "base"))
        with (mock.patch.object(installer.subprocess, "run") as run,
              mock.patch.object(installer.venv, "EnvBuilder") as builder,
              contextlib.redirect_stderr(io.StringIO())):
            run.return_value.stdout = json.dumps(probe_target)
            installer.install_bundle(self.bundle, destination, reuse=True)
            probe_args = run.call_args_list[1].args[0]
            builder.assert_not_called()
            probe_target["soabi"] = "wrong"
            run.return_value.stdout = json.dumps(probe_target)
            with self.assertRaisesRegex(installer.InstallError, "incompatible target soabi"):
                installer.install_bundle(self.bundle, destination, reuse=True)
        # Execute the captured probe, rather than mocking its target checks.
        command = [sys.executable, *probe_args[1:]]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["python"], required["python"])
        required["python_patch"] = 0
        if sys.version_info.micro:
            self.write_manifest()
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("incompatible target python_full_version", result.stderr)
        required["python_patch"] = None
        self.manifest["bootstrap"] = self.bootstrap(("py3-none-any", f"{abi}-{abi}-win32"))
        self.write_manifest()
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible target ABI/architecture/platform", result.stderr)

    def test_invalid_manifest_fields(self):
        changes = (("format", True), ("format", 2), ("target", None), ("wheels", {}),
                   ("bootstrap", {"filename": "pip.whl", "sha256": "invalid"}),
                   ("wheels", [{"filename": "demo.whl", "sha256": "0" * 64}]))
        for field, value in changes:
            with self.subTest(field=field, value=value):
                original = self.manifest[field]
                self.manifest[field] = value
                self.write_manifest()
                with self.assertRaises(installer.InstallError):
                    installer.verify_bundle(self.bundle)
                self.manifest[field] = original

    def test_duplicate_wheels_rejected(self):
        self.manifest["wheels"] *= 2
        self.write_manifest()
        with self.assertRaisesRegex(installer.InstallError, "duplicate wheel"):
            installer.verify_bundle(self.bundle)

    def test_verify_only_does_not_touch_environment(self):
        destination = self.root / "env"
        with (mock.patch.object(installer, "__file__", str(self.bundle / "install.py")),
              mock.patch.object(installer.venv, "EnvBuilder") as builder,
              mock.patch.object(installer.subprocess, "run") as run):
            with contextlib.redirect_stdout(io.StringIO()):
                result = installer.main(["--verify-only", "--venv", str(destination)])
            self.assertEqual(result, 0)
            builder.assert_not_called()
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][:3], [sys.executable, "-I", "-c"])
        self.assertFalse(destination.exists())

    def test_verify_only_rejects_incompatible_or_invalid_wheel_tags(self):
        for section in ("bootstrap", "wheels"):
            for filename in ("demo-1-cp00-cp00-wrong.whl", "invalid.whl"):
                with self.subTest(section=section, filename=filename):
                    entry = (self.manifest[section] if section == "bootstrap"
                             else self.manifest[section][0])
                    source = self.bundle / section / entry["filename"]
                    replacement = self.wheel(section, filename, source.read_bytes())
                    entry.update(replacement)
                    self.write_manifest()
                    with (mock.patch.object(installer, "__file__", str(self.bundle / "install.py")),
                          mock.patch.object(installer.venv, "EnvBuilder") as builder):
                        with contextlib.redirect_stderr(io.StringIO()) as errors:
                            self.assertEqual(installer.main(["--verify-only"]), 1)
                        builder.assert_not_called()
                    self.assertIn("wheel compatibility check failed", errors.getvalue())
                    self.assertIn("incompatible wheel tags" if filename.startswith("demo-")
                                  else "invalid wheel filename", errors.getvalue())
                    self.assertNotIn("Traceback", errors.getvalue())
                    self.assertFalse((self.root / "env").exists())
                    entry["filename"] = source.name

    def test_install_uses_verified_local_paths_and_no_ensurepip(self):
        (self.bundle / "requirements.txt").write_text("https://evil.invalid/demo.whl\n")
        destination = self.root / "env"
        with (mock.patch.dict(os.environ, {"PIP_INDEX_URL": "https://evil.invalid",
                                          "PIP_TARGET": "/elsewhere", "PIP_CONFIG_FILE": "/evil"}),
              mock.patch.object(installer.venv, "EnvBuilder") as builder,
              mock.patch.object(installer.subprocess, "run") as run):
            installer.install_bundle(self.bundle, destination)
        builder.assert_called_once_with(with_pip=False, symlinks=os.name != "nt")
        builder.return_value.create.assert_called_once_with(destination)
        self.assertEqual(run.call_count, 3)
        args = run.call_args_list[1].args[0]
        self.assertEqual(args[1:3], ["-I", "-c"])
        self.assertIn("runpy.run_module('pip',run_name='__main__')", args[3])
        self.assertEqual(args[5:14], ["--isolated", "--disable-pip-version-check", "install",
                                    "--no-index", "--no-deps", "--no-cache-dir",
                                    "--only-binary", ":all:", args[4]])
        bootstrap, wheels = installer.verify_bundle(self.bundle)
        self.assertEqual(args[4], str(bootstrap))
        self.assertEqual(args[14:], list(map(str, wheels)))
        self.assertNotIn("-r", args)
        env = run.call_args.kwargs["env"]
        self.assertEqual({key: value for key, value in env.items() if key.startswith("PIP_")},
                         {"PIP_CONFIG_FILE": os.devnull})
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.args[0],
                         [str(destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
                          "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "check"])
        self.assertEqual(run.call_args_list[1].kwargs["env"], env)

    def test_dependency_check_failure_is_reported(self):
        failure = subprocess.CalledProcessError(1, ["pip", "check"])
        with (mock.patch.object(installer.venv, "EnvBuilder"),
              mock.patch.object(installer.subprocess, "run", side_effect=[None, None, failure]),
              mock.patch.object(installer, "__file__", str(self.bundle / "install.py")),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(installer.main(["--venv", str(self.root / "env")]), 1)
        self.assertIn("Installation failed", errors.getvalue())

    def test_existing_destination_refused_and_preserved(self):
        destination = self.root / "env"
        destination.mkdir()
        sentinel = destination / "keep"
        sentinel.write_text("unchanged")
        with (mock.patch.object(installer.venv, "EnvBuilder") as builder,
              mock.patch.object(installer.subprocess, "run") as run):
            with self.assertRaisesRegex(installer.InstallError, "already exists.*--reuse"):
                installer.install_bundle(self.bundle, destination)
            builder.assert_not_called()
            run.assert_called_once()  # Wheel tags are checked before destination validation.
        self.assertEqual(sentinel.read_text(), "unchanged")
        with self.assertRaisesRegex(installer.InstallError, "existing virtualenv"):
            installer.install_bundle(self.bundle, destination, reuse=True)

    def test_reuse_checks_interpreter_and_skips_creation(self):
        destination = self.root / "env"
        destination.mkdir()
        (destination / "pyvenv.cfg").write_text("home = test\ninclude-system-site-packages = false\n")
        probe_target = dict(target(), prefix=str(destination), base_prefix=str(self.root / "base"))
        with (mock.patch.object(installer.venv, "EnvBuilder") as builder,
              mock.patch.object(installer.subprocess, "run") as run):
            run.return_value.stdout = json.dumps(probe_target)
            installer.install_bundle(self.bundle, destination, reuse=True)
            builder.assert_not_called()
            self.assertEqual(run.call_count, 4)
            probe_args = run.call_args_list[1].args[0]
            self.assertEqual(probe_args[4], str(INSTALLER))
            self.assertIn("runpy.run_path(sys.argv[1],run_name='libc_probe')", probe_args[3])
            self.assertIn("libc=installer['_libc']()", probe_args[3])
            self.assertNotIn("platform.libc_ver", probe_args[3])
            probe_target["python"] = [0, 0]
            run.return_value.stdout = json.dumps(probe_target)
            run.reset_mock()
            with self.assertRaisesRegex(installer.InstallError, "incompatible target python"):
                installer.install_bundle(self.bundle, destination, reuse=True)
            self.assertEqual(run.call_count, 2)
        result = subprocess.run([sys.executable, *probe_args[1:]],
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["libc"], installer._libc())

    def test_reuse_rejects_system_sites_and_invalid_config(self):
        destination = self.root / "env"
        destination.mkdir()
        for setting in ("", "include-system-site-packages = true\n",
                        "include-system-site-packages = false\ninclude-system-site-packages = true\n"):
            with self.subTest(setting=setting):
                (destination / "pyvenv.cfg").write_text(setting)
                with mock.patch.object(installer.subprocess, "run") as run:
                    with self.assertRaisesRegex(installer.InstallError, "system-site-packages = false"):
                        installer.install_bundle(self.bundle, destination, reuse=True)
                    self.assertEqual(run.call_count, 1)

    def test_reuse_rejects_non_venv_wrong_prefix_and_target(self):
        destination = self.root / "env"
        destination.mkdir()
        (destination / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        valid = dict(target(), prefix=str(destination), base_prefix=str(self.root / "base"))
        cases = [(dict(valid, base_prefix=str(destination)), "not a virtualenv"),
                 (dict(valid, prefix=str(self.root / "other")), "does not belong"),
                 (dict(valid, soabi="free-threaded-mismatch"), "incompatible target soabi"),
                 (dict(valid, prefix=None), "could not validate"),
                 (dict(valid, base_prefix=None), "could not validate"),
                 (dict(valid, libc=["wrong", "1"]), "incompatible target libc")]
        for probe_target, message in cases:
            with (self.subTest(probe_target=probe_target),
                  mock.patch.object(installer.subprocess, "run") as run):
                run.return_value.stdout = json.dumps(probe_target)
                with self.assertRaisesRegex(installer.InstallError, message):
                    installer.install_bundle(self.bundle, destination, reuse=True)
                self.assertEqual(run.call_count, 2)

    def test_reuse_requires_exact_libc_even_when_older_libc_is_compatible(self):
        destination = self.root / "env"
        destination.mkdir()
        (destination / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        with mock.patch.object(installer.platform, "libc_ver", return_value=("glibc", "2.39")):
            self.manifest["target"] = target()
            self.write_manifest()
            probe_target = dict(target(), libc=["glibc", "2.28"], prefix=str(destination),
                                base_prefix=str(self.root / "base"))
            with mock.patch.object(installer.subprocess, "run") as run:
                run.return_value.stdout = json.dumps(probe_target)
                with self.assertRaisesRegex(installer.InstallError, "same libc"):
                    installer.install_bundle(self.bundle, destination, reuse=True)
                self.assertEqual(run.call_count, 2)

    def test_cli_reports_errors_without_traceback(self):
        (self.bundle / "manifest.json").write_text("{")
        with (mock.patch.object(installer, "__file__", str(self.bundle / "install.py")),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(installer.main(["--verify-only"]), 1)
        self.assertIn("invalid manifest.json", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


@unittest.skipUnless(os.environ.get("WHEELPORT_TEST_BUNDLE"), "no real bundle fixture supplied")
class RealInstallerTests(unittest.TestCase):
    def test_install_real_bundle_and_leave_working_pip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            shutil.copytree(os.environ["WHEELPORT_TEST_BUNDLE"], bundle)
            shutil.copyfile(INSTALLER, bundle / "install.py")
            env = dict(os.environ, PIP_INDEX_URL="https://invalid.invalid", PIP_TARGET=str(root / "bad"))
            subprocess.run([sys.executable, "-I", str(bundle / "install.py")],
                           cwd=root, env=env, check=True, capture_output=True, text=True)
            python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            result = subprocess.run(
                [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                 "list", "--format=json"], check=True, capture_output=True, text=True,
            )
            installed = {replaced["name"].lower().replace("_", "-"): replaced["version"]
                         for replaced in json.loads(result.stdout)}
            self.assertIn("pip", installed)
            manifest = json.loads((bundle / "manifest.json").read_text())
            for wheel in manifest["wheels"]:
                self.assertEqual(installed[wheel["name"].lower().replace("_", "-")], wheel["version"])
            self.assertFalse((root / "bad").exists())


if __name__ == "__main__":
    unittest.main()
