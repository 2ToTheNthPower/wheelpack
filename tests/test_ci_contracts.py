"""Real CLI contracts: local artifacts, native locks, and standalone installation.

The CI environment must provision uv, Poetry and its export plugin. No resolver,
builder, installer, or CLI implementation is replaced by a test double.
"""

import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.markers import Marker, default_environment
from packaging.tags import sys_tags
from test_bundle import (  # noqa: F401
    AUDIT_HOOK,
    installed_pip_wheel,
    run_installer,
    write_wheel,
)

from wheelport.install import _libc
from wheelport.targets import Target

FORMATS = ("requirements", "pep621", "uv", "poetry")
FULL_PYTHON = ".".join(map(str, sys.version_info[:3]))
HOST_MINOR = f"3.{sys.version_info.minor}"
NATIVE_PROGRESS = (
    "Resolving and building wheels for this Python/platform; checking offline installation...\n"
)
TARGET_PROGRESS = (
    "Resolving target wheels; checking artifacts (target runtime installation not run)...\n"
)


def host_target():
    supported = {
        "win_amd64",
        "win_arm64",
        "win32",
        "macosx_11_0_arm64",
        "macosx_11_0_x86_64",
        "manylinux_2_17_x86_64",
        "manylinux_2_17_aarch64",
        "musllinux_1_2_x86_64",
        "musllinux_1_2_aarch64",
    }
    platform = next((tag.platform for tag in sys_tags() if tag.platform in supported), None)
    assert platform is not None, "CI host must expose a supported explicit deployment tag"
    return Target.create(python_version=FULL_PYTHON, python_platform=platform)


def wheel(folder, name, *, tag="py3-none-any", dependencies=(), python=">=3.11,<4", code=None):
    info = f"{name}-1.dist-info"
    path = write_wheel(
        folder / f"{name}-1-{tag}.whl",
        {
            f"{name}/__init__.py": code or f"VALUE = {name!r}\n",
            f"{info}/METADATA": (
                f"Metadata-Version: 2.3\nName: {name}\nVersion: 1\nRequires-Python: {python}\n"
                + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
            ),
            f"{info}/WHEEL": (
                "Wheel-Version: 1.0\nGenerator: wheelport-ci-contract\n"
                f"Root-Is-Purelib: true\nTag: {tag}\n"
            ),
        },
        info,
    )
    # RECORD hashes cover member bytes, not ZIP timestamps. Fix timestamps so
    # equivalent synthetic wheels have identical hashes across machines/runs.
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    with zipfile.ZipFile(path, "w") as archive:
        for filename, content in members.items():
            entry = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, content)
    return path


def command(args, cwd, env):
    return subprocess.run(
        list(map(str, args)),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.fixture
def inputs(tmp_path, installed_pip_wheel):  # noqa: F811
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PIP_", "UV_", "POETRY_"))
    }
    env.update(
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_CACHE_DIR="1",
        UV_OFFLINE="1",
        UV_NO_CACHE="1",
        UV_PYTHON_DOWNLOADS="never",
        POETRY_VIRTUALENVS_CREATE="false",
        POETRY_KEYRING_ENABLED="false",
        POETRY_CACHE_DIR=str(tmp_path / "poetry-cache"),
    )

    def prepare(
        kind,
        *,
        tag="py3-none-any",
        python=">=3.11,<4",
        code=None,
        project_python=">=3.11,<4",
        release_marker=False,
    ):
        root = tmp_path / kind
        root.mkdir()
        links = root / "local wheels"
        links.mkdir()
        markers = {
            "winonly": "sys_platform == 'win32'",
            "linuxonly": "sys_platform == 'linux'",
            "maconly": "sys_platform == 'darwin'",
            "armonly": "platform_machine == 'ARM64' or platform_machine == 'aarch64' "
            "or platform_machine == 'arm64'",
            "minoronly": f"python_version == '{HOST_MINOR}'",
            "patchonly": f"python_full_version >= '{FULL_PYTHON}'",
        }
        if release_marker:
            markers["releaseonly"] = "platform_release == '6'"
        paths = {name: wheel(links, name) for name in ("leaf", "optional", "groupdep", *markers)}
        dependencies = ["leaf==1", *(f"{name}==1; {marker}" for name, marker in markers.items())]
        paths["demo"] = wheel(
            links,
            "demo",
            tag=tag,
            python=python,
            dependencies=dependencies,
            code=code,
        )
        declarations = ["demo==1", *dependencies]
        if kind == "requirements":
            path = root / "requirements.txt"
            path.write_text("-r nested.txt\n-c constraints.txt\n", encoding="utf-8")
            (root / "constraints.txt").write_text(
                "demo==1 --hash=sha256:"
                + hashlib.sha256(paths["demo"].read_bytes()).hexdigest()
                + "\nleaf==1; python_version >= '3.11' --hash=sha256:"
                + hashlib.sha256(paths["leaf"].read_bytes()).hexdigest()
                + "\n",
                encoding="utf-8",
            )
            nested = []
            for value in [*declarations, "optional==1", "groupdep==1"]:
                name = value.split("==", 1)[0]
                digest = hashlib.sha256(paths[name].read_bytes()).hexdigest()
                nested.append(f"{value} --hash=sha256:{digest}")
            (root / "nested.txt").write_text("\n".join(nested) + "\n", encoding="utf-8")
        else:
            path = root / "pyproject.toml"

            def reference(value):
                name, _, tail = value.partition("==1")
                return f"{name} @ {paths[name].as_uri()}" + (" ;" + tail[1:] if tail else "")

            # Poetry must resolve file wheel references, never local source folders.
            # uv also uses wheel references so its native lock retains artifact hashes.
            values = (
                [reference(value) for value in declarations]
                if kind in {"uv", "poetry"}
                else declarations
            )
            optional = reference("optional==1") if kind in {"uv", "poetry"} else "optional==1"
            group = reference("groupdep==1") if kind in {"uv", "poetry"} else "groupdep==1"
            body = (
                '[project]\nname="ci-root"\nversion="1"\n'
                f"requires-python={json.dumps(project_python)}\n"
                f"dependencies={json.dumps(values)}\n"
                f"[project.optional-dependencies]\nfeature={json.dumps([optional])}\n"
                f"[dependency-groups]\ntest={json.dumps([group])}\n"
            )
            if kind == "uv":
                body += "[tool.uv]\npackage=false\n"
            elif kind == "poetry":
                body += "[tool.poetry]\npackage-mode=false\n"
            path.write_text(body, encoding="utf-8")
        lock = root / f"{kind}.lock" if kind in {"uv", "poetry"} else None
        if lock:
            args = (
                ["uv", "lock", "--python", sys.executable, "--no-index", "--find-links", links]
                if kind == "uv"
                else ["poetry", "lock"]
            )
            result = command(args, root, env)
            assert result.returncode == 0, result.stdout + result.stderr
            assert lock.is_file()
        return SimpleNamespace(
            kind=kind,
            root=root,
            links=links,
            path=path,
            paths=paths,
            markers=markers,
            env=env,
            pip=installed_pip_wheel,
            lock=lock,
            lock_bytes=lock.read_bytes() if lock else None,
            output=root / "bundle.zip",
        )

    return prepare


def pack(case, target=None, *, dependencies_only=True):
    args = [
        sys.executable,
        "-m",
        "wheelport",
        "pack",
        case.path,
        "-o",
        case.output,
        "--no-index",
        "--find-links",
        case.links,
        "--pip-wheel",
        case.pip,
    ]
    if case.kind == "pep621":
        args += ["--manager", "pip"]
    if case.kind != "requirements":
        args += ["--extra", "feature", "--group", "test"]
    if dependencies_only:
        args += ["--dependencies-only"]
    if target:
        args += [
            "--python-version",
            target.python_version,
            "--python-platform",
            target.python_platform,
            "--abi",
            target.abi,
        ]
    result = command(args, case.root, case.env)
    assert "Traceback" not in result.stdout + result.stderr, result.stdout + result.stderr
    if case.lock:
        assert case.lock.read_bytes() == case.lock_bytes, "collection must not update native locks"
    return result


def pack_failure(case, result, *fragments):
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == ""
    assert "wheelport: " in result.stderr
    for fragment in fragments:
        assert fragment in result.stderr, result.stderr
    assert not case.output.exists(), "failed collection must not publish a partial ZIP"
    assert list(case.root.glob("*.zip")) == []


def unpack_success(case, result, target):
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == str(case.output.absolute()) + "\n"
    assert result.stderr == (TARGET_PROGRESS if target else NATIVE_PROGRESS)
    extracted = case.root / "extracted"
    with zipfile.ZipFile(case.output) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        lock = tomllib.loads(archive.read("pylock.toml").decode())
        environment = target.marker_environment if target else default_environment()
        expected = {"demo", "leaf", "optional", "groupdep", "pip"}
        expected.update(
            name for name, marker in case.markers.items() if Marker(marker).evaluate(environment)
        )
        assert {entry["name"] for entry in manifest["wheels"]} == expected
        assert {entry["name"] for entry in lock["packages"]} == expected
        assert manifest["manager"] == (case.kind if case.kind in {"uv", "poetry"} else "pip")
        assert manifest["verification"] == {
            "artifact_check": "passed",
            "runtime_install": "not-run" if target else "passed",
        }
        if target:
            assert manifest["target"] == target.manifest()
            assert lock["requires-python"] == f"=={target.python_version}"
        for entry in manifest["wheels"]:
            digest = hashlib.sha256(archive.read("wheels/" + entry["filename"])).hexdigest()
            assert entry["sha256"] == digest
        assert archive.read("bootstrap/" + case.pip.name) == case.pip.read_bytes()
        assert not any("resolved" in name for name in archive.namelist())
        archive.extractall(extracted)
    return extracted, expected


@pytest.mark.parametrize("kind", FORMATS)
@pytest.mark.parametrize("mode", ("native", "matching", "cross-os", "cross-arch", "minor"))
def test_expected_success_cli_and_standalone_install(inputs, kind, mode):
    host = host_target()
    target = None if mode == "native" else host
    if mode in {"cross-os", "cross-arch", "minor"}:
        arch, _, suffix = host.python_platform.partition("-")
        platform = host.python_platform
        if mode == "cross-os":
            platform = (
                ("aarch64" if arch == "aarch64" else "x86_64") + "-manylinux_2_17"
                if sys.platform == "win32"
                else f"{arch}-pc-windows-msvc"
            )
        elif mode == "cross-arch":
            platform = ("aarch64" if arch != "aarch64" else "x86_64") + "-" + suffix
        target = Target.create(
            python_version=f"3.{sys.version_info.minor + 1}.0" if mode == "minor" else FULL_PYTHON,
            python_platform=platform,
        )
    # Locked graphs remain universal; other inputs exercise an actual host-tagged wheel.
    tag = (
        str(next(host.tags()))
        if mode in {"native", "matching"} and kind in {"requirements", "pep621"}
        else "py3-none-any"
    )
    case = inputs(kind, tag=tag)
    extracted, expected = unpack_success(case, pack(case, target), target)
    destination = case.root / "offline environment"
    result = run_installer(extracted, "--venv", str(destination), env=case.env)
    assert "NETWORK FORBIDDEN" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    if mode in {"cross-os", "cross-arch", "minor"}:
        assert result.returncode == 1, result.stdout + result.stderr
        if mode == "minor":
            assert "Installation failed: incompatible target python:" in result.stderr
            assert "GUARDED CHILD" not in result.stdout
        elif mode == "cross-arch" and sys.platform == "darwin":
            assert "Installation failed: incompatible target architecture:" in result.stderr
            assert "GUARDED CHILD" not in result.stdout
        else:
            assert result.stderr == (
                "Installation failed: wheel compatibility check failed: "
                "incompatible target ABI/architecture/platform: "
                f"{target.abi} {target.python_platform}\n"
            )
            assert result.stdout == "GUARDED CHILD\n"
        assert not destination.exists()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.endswith(f"Installed bundle into {destination.absolute()}\n")
    assert result.stdout.count("GUARDED CHILD") == 3
    assert "No broken requirements found" in result.stdout
    untouched = case.root / "verify only must not create"
    verified = run_installer(extracted, "--verify-only", "--venv", str(untouched), env=case.env)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    lines = verified.stdout.splitlines(keepends=True)
    if "MUSL LOADER PROBE\n" in lines:
        assert mode == "native" and sys.platform == "linux" and _libc()[0] == "musl"
        lines = [line for line in lines if line != "MUSL LOADER PROBE\n"]
    assert "".join(lines) == "GUARDED CHILD\nBundle verified; no environment changes made.\n"
    assert verified.stdout.count("GUARDED CHILD") == 1
    assert "NETWORK FORBIDDEN" not in verified.stdout + verified.stderr
    assert not untouched.exists()
    python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = command(
        [
            python,
            "-I",
            "-c",
            AUDIT_HOOK + "\nimport importlib.metadata as m, json, demo, leaf; "
            "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))",
        ],
        case.root,
        case.env,
    )
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == {
        name: importlib.metadata.version("pip") if name == "pip" else "1" for name in expected
    }


@pytest.mark.parametrize("kind", FORMATS)
@pytest.mark.parametrize("platform", ("win_amd64", "win_arm64", "manylinux_2_17_aarch64"))
def test_expected_success_foreign_native_tag_never_executes_wheel(inputs, kind, platform):
    target = Target.create(python_version=FULL_PYTHON, python_platform=platform)
    tag = f"{target.abi}-{target.abi}-{platform}"
    case = inputs(
        kind,
        tag=tag,
        code="raise RuntimeError('foreign wheel executed')\n",
        project_python=f"=={FULL_PYTHON}",
    )
    unpack_success(case, pack(case, target), target)


@pytest.mark.parametrize("kind", FORMATS)
def test_expected_success_native_default_does_not_build_nonpackage_root(inputs, kind):
    case = inputs(kind)
    unpack_success(case, pack(case, dependencies_only=False), None)


@pytest.mark.parametrize("kind", FORMATS)
def test_expected_failure_no_compatible_target_artifact(inputs, kind):
    host = host_target()
    case = inputs(kind, tag=str(next(host.tags())), project_python=f"=={FULL_PYTHON}")
    arch, _, suffix = host.python_platform.partition("-")
    foreign = Target.create(
        python_version=FULL_PYTHON,
        python_platform=("aarch64" if arch != "aarch64" else "x86_64") + "-" + suffix,
    )
    fragments = (
        ("No compatible locked wheel for demo",)
        if kind in {"uv", "poetry"}
        else (
            "Command failed (exit",
            "demo",
            "wheel",
        )
    )
    pack_failure(case, pack(case, foreign), *fragments)


@pytest.mark.parametrize("kind", ("uv", "poetry"))
def test_expected_failure_locked_unsupported_os_release_marker(inputs, kind):
    case = inputs(kind, release_marker=True)
    pack_failure(
        case, pack(case, host_target()), "Explicit target cannot evaluate platform_release"
    )


@pytest.mark.parametrize("kind", ("uv", "poetry"))
@pytest.mark.parametrize("source_kind", ("directory", "archive"))
def test_expected_failure_locked_source_only_dependency(inputs, kind, source_kind):
    case = inputs(kind)
    source = case.root / "source-only"
    source.mkdir()
    metadata = "Metadata-Version: 2.3\nName: sourcedep\nVersion: 1\nRequires-Python: >=3.11,<4\n"
    (source / "pyproject.toml").write_text(
        '[project]\nname="sourcedep"\nversion="1"\nrequires-python=">=3.11,<4"\n'
        'dependencies=[]\n[build-system]\nrequires=[]\nbuild-backend="backend"\n'
        'backend-path=["."]\n',
        encoding="utf-8",
    )
    backend = source / "backend.py"
    backend.write_text(
        "from pathlib import Path\n"
        "def prepare_metadata_for_build_wheel(directory, config_settings=None):\n"
        "    info = Path(directory) / 'sourcedep-1.dist-info'\n"
        "    info.mkdir()\n"
        f"    (info / 'METADATA').write_text({metadata!r})\n"
        "    return info.name\n"
        "def build_wheel(*args, **kwargs):\n"
        "    raise RuntimeError('source build forbidden')\n",
        encoding="utf-8",
    )
    (source / "PKG-INFO").write_text(metadata, encoding="utf-8")
    location = source
    if source_kind == "archive":
        location = case.root / "sourcedep-1.tar.gz"
        with tarfile.open(location, "w:gz") as archive:
            archive.add(source, arcname="sourcedep-1")
    case.path.write_text(
        case.path.read_text().replace(
            "dependencies=[",
            f"dependencies=[{json.dumps('sourcedep @ ' + location.as_uri())}, ",
            1,
        ),
        encoding="utf-8",
    )
    locked = command(
        ["uv", "lock", "--python", sys.executable, "--no-index", "--find-links", case.links]
        if kind == "uv"
        else ["poetry", "lock"],
        case.root,
        case.env,
    )
    assert locked.returncode == 0, locked.stdout + locked.stderr
    case.lock_bytes = case.lock.read_bytes()
    backend.write_text("raise RuntimeError('collection executed source code')\n", encoding="utf-8")
    fragments = (
        ("Explicit target local/VCS source requires a prebuilt wheel",)
        if kind == "uv" and source_kind == "directory"
        else (
            "Command failed (exit",
            "sourcedep",
            "--no-build",
            "no binary distribution",
        )
        if kind == "uv"
        else ("Explicit target requires prebuilt wheels",)
    )
    pack_failure(case, pack(case, host_target()), *fragments)


@pytest.mark.parametrize("kind", FORMATS)
def test_expected_failure_missing_artifact(inputs, kind):
    case = inputs(kind)
    case.paths["demo"].unlink()
    fragments = (
        ("Command failed (exit", "Distribution not found at: <redacted URL>")
        if kind == "poetry"
        else ("demo",)
    )
    pack_failure(case, pack(case, host_target()), *fragments)


@pytest.mark.parametrize("kind", FORMATS)
def test_expected_failure_hash_contract(inputs, kind):
    case = inputs(kind)
    if kind == "requirements":
        nested = case.root / "nested.txt"
        digest = hashlib.sha256(case.paths["demo"].read_bytes()).hexdigest()
        nested.write_text(nested.read_text().replace(digest, "0" * 64), encoding="utf-8")
        fragments = ("Discovered artifact hash is not in input allowed hashes for demo",)
    elif kind == "pep621":
        body = case.path.read_text().replace(
            '"demo==1"',
            json.dumps(f"demo @ {case.paths['demo'].as_uri()}#sha256={'0' * 64}"),
        )
        case.path.write_text(body, encoding="utf-8")
        fragments = ("Discovered artifact hash is not in input allowed hashes for demo",)
    else:
        path = case.paths["demo"]
        with zipfile.ZipFile(path) as archive:
            members = {
                name: archive.read(name)
                for name in archive.namelist()
                if not name.endswith("/RECORD")
            }
        members["demo/__init__.py"] = "VALUE = 'changed artifact, retained native lock'\n"
        write_wheel(path, members, "demo-1.dist-info")
        fragments = (
            ("Local wheel SHA256 hash mismatch: demo-1-",)
            if kind == "uv"
            else ("Discovered artifact hash is not in input allowed hashes for demo",)
        )
    pack_failure(case, pack(case, host_target()), *fragments)


@pytest.mark.parametrize("kind", ("uv", "poetry"))
@pytest.mark.parametrize("explicit", (False, True))
def test_expected_failure_stale_lock(inputs, kind, explicit):
    case = inputs(kind)
    newpin = wheel(case.links, "newpin")
    case.path.write_text(
        case.path.read_text().replace(
            "dependencies=[",
            f"dependencies=[{json.dumps('newpin @ ' + newpin.as_uri())}, ",
            1,
        ),
        encoding="utf-8",
    )
    result = pack(case, host_target() if explicit else None)
    fragments = (
        ("lockfile", "needs to be updated", "--locked")
        if kind == "uv"
        else (
            "pyproject.toml changed significantly",
            "poetry.lock",
        )
    )
    pack_failure(case, result, "Command failed (exit", *fragments)


@pytest.mark.parametrize("kind", FORMATS)
def test_expected_failure_existing_output_preserves_bytes(inputs, kind):
    case = inputs(kind)
    original = b"existing user archive\x00do not overwrite"
    case.output.write_bytes(original)
    result = pack(case, host_target())
    assert result.returncode == 1
    assert result.stdout == ""
    assert f"wheelport: Output already exists: {case.output}" in result.stderr
    assert case.output.read_bytes() == original


@pytest.mark.parametrize(
    "flags,message",
    [
        (["--python-version", "3.10"], "Only CPython >=3.11 is supported"),
        (["--python-version", "3.11", "--abi", "cp312"], "ABI must be cp311"),
    ],
)
@pytest.mark.parametrize("kind", FORMATS)
def test_expected_failure_invalid_target_flags(inputs, flags, message, kind):
    case = inputs(kind)
    result = command(
        [sys.executable, "-m", "wheelport", "pack", case.path, "-o", case.output, *flags],
        case.root,
        case.env,
    )
    pack_failure(case, result, message)
    assert result.stderr == f"wheelport: {message}\n"


@pytest.mark.parametrize("kind", ("requirements", "pep621"))
@pytest.mark.parametrize(
    "dependency,message",
    [
        ("demo==1; platform_release == '6'", "Explicit target cannot evaluate platform_release"),
        ("demo @ file:///local/source", "Explicit target requires prebuilt wheels"),
        ("demo @ git+https://invalid.invalid/source", "Explicit target requires prebuilt wheels"),
        ("demo @ https://invalid.invalid/demo.tar.gz", "Explicit target requires prebuilt wheels"),
    ],
)
def test_expected_failure_unsupported_marker_and_build_sources(inputs, kind, dependency, message):
    case = inputs(kind)
    if kind == "requirements":
        case.path.write_text(dependency + "\n", encoding="utf-8")
    else:
        case.path.write_text(
            case.path.read_text().replace('"demo==1"', json.dumps(dependency)), encoding="utf-8"
        )
    pack_failure(case, pack(case, host_target()), message)


@pytest.mark.parametrize("kind", ("pep621", "uv", "poetry"))
def test_expected_failure_explicit_root_build_requires_opt_in(inputs, kind):
    case = inputs(kind)
    body = (
        case.path.read_text()
        .replace("package=false", "package=true")
        .replace(
            "package-mode=false",
            "package-mode=true",
        )
    )
    case.path.write_text(
        body + '[build-system]\nrequires=[]\nbuild-backend="never.execute"\n', encoding="utf-8"
    )
    pack_failure(
        case,
        pack(case, host_target(), dependencies_only=False),
        "Explicit target cannot build the root project; use --dependencies-only or --project-wheel",
    )


@pytest.mark.parametrize("kind", FORMATS)
def test_expected_failure_requires_python_patch(inputs, kind):
    case = inputs(kind, python=f"=={FULL_PYTHON}", project_python=f"=={FULL_PYTHON}")
    target = Target.create(
        python_version=f"{HOST_MINOR}.{sys.version_info.micro + 1}", python_platform="win_amd64"
    )
    fragments = (
        ("demo", "Python")
        if kind == "requirements"
        else ("Root project requires a different target Python version",)
    )
    pack_failure(case, pack(case, target), *fragments)


@pytest.mark.parametrize("kind", FORMATS)
def test_expected_success_requires_python_exact_host_patch(inputs, kind):
    case = inputs(kind, python=f"=={FULL_PYTHON}", project_python=f"=={FULL_PYTHON}")
    target = host_target()
    unpack_success(case, pack(case, target), target)


@pytest.mark.parametrize("fault", ("bootstrap", "patch", "os", "arch", "destination"))
def test_expected_failure_standalone_preflight_preserves_destination(inputs, fault):
    case = inputs("requirements")
    target = host_target()
    extracted, _ = unpack_success(case, pack(case, target), target)
    manifest_path = extracted / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    message = "SHA-256 mismatch"
    if fault == "bootstrap":
        path = extracted / "bootstrap" / case.pip.name
        path.write_bytes(path.read_bytes() + b"corruption")
    elif fault == "patch":
        manifest["target"] = Target.create(
            python_version=f"{HOST_MINOR}.{sys.version_info.micro + 1}",
            python_platform=target.python_platform,
        ).manifest()
        message = "incompatible target python_full_version"
    elif fault in {"os", "arch"}:
        arch, _, suffix = target.python_platform.partition("-")
        if fault == "arch":
            platform = ("aarch64" if arch != "aarch64" else "x86_64") + "-" + suffix
        elif sys.platform == "win32":
            platform = ("aarch64" if arch == "aarch64" else "x86_64") + "-manylinux_2_17"
        else:
            platform = "win_arm64" if arch == "aarch64" else "win_amd64"
        manifest["target"] = Target.create(
            python_version=FULL_PYTHON,
            python_platform=platform,
        ).manifest()
        message = (
            "incompatible target architecture"
            if fault == "arch" and sys.platform == "darwin"
            else "incompatible target ABI/architecture/platform"
        )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = case.root / "must not change"
    if fault == "destination":
        destination.mkdir()
        (destination / "keep").write_bytes(b"unchanged")
        message = "already exists; choose another path or use --reuse"
    result = run_installer(extracted, "--venv", str(destination), env=case.env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Installation failed:" in result.stderr and message in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert "NETWORK FORBIDDEN" not in result.stdout + result.stderr
    if fault == "destination":
        assert {path.name: path.read_bytes() for path in destination.iterdir()} == {
            "keep": b"unchanged",
        }
    else:
        assert not destination.exists()


def test_expected_success_cli_wheel_built_from_sdist(tmp_path, installed_pip_wheel):  # noqa: F811
    """Exercise the shipped entry point, not the workspace's editable install.

    uv's managed build-backend cache is populated when CI installs this project.
    Build isolation stays enabled, but offline mode forbids backend downloads.
    """
    root = Path(__file__).resolve().parents[1]
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PIP_", "UV_"))
    }
    env.update(PIP_CONFIG_FILE=os.devnull, UV_PYTHON_DOWNLOADS="never")
    built = command(
        ["uv", "build", "--python", sys.executable, "--offline", "--sdist",
         "--out-dir", artifacts, root], tmp_path, env
    )
    assert built.returncode == 0, built.stdout + built.stderr
    sdists = list(artifacts.glob("*.tar.gz"))
    assert len(sdists) == 1
    built = command(
        ["uv", "build", "--python", sys.executable, "--offline", "--wheel",
         "--out-dir", artifacts, sdists[0]], tmp_path, env
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheels = list(artifacts.glob("wheelport-*.whl"))
    assert len(wheels) == 1
    # Repackage the only other runtime dependency without downloading it.
    distribution = importlib.metadata.distribution("packaging")
    members = {}
    info = None
    for entry in distribution.files:
        name = entry.as_posix()
        if not name.startswith(("packaging/", "packaging-")) or entry.suffix == ".pyc":
            continue
        if name.endswith("/RECORD"):
            continue
        if name.endswith(".dist-info/METADATA"):
            info = entry.parent.as_posix()
        members[name] = distribution.locate_file(entry).read_bytes()
    assert info is not None
    packaging = write_wheel(
        artifacts / f"packaging-{distribution.version}-py3-none-any.whl", members, info
    )
    destination = tmp_path / "shipped environment"
    created = command(
        [sys.executable, "-I", "-m", "venv", "--without-pip", destination], tmp_path, env
    )
    assert created.returncode == 0, created.stderr
    python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = command(
        [
            python,
            "-I",
            "-c",
            AUDIT_HOOK + "\nimport runpy, sys; "
            f"sys.path.insert(0, {str(installed_pip_wheel)!r}); "
            "runpy.run_module('pip', run_name='__main__')",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-index",
            "--no-deps",
            installed_pip_wheel,
            packaging,
            wheels[0],
        ],
        tmp_path,
        env,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert "NETWORK FORBIDDEN" not in installed.stdout + installed.stderr
    script = destination / ("Scripts/wheelport.exe" if os.name == "nt" else "bin/wheelport")
    for args in ([script, "--version"], [python, "-I", "-m", "wheelport", "--version"]):
        smoke = command(args, tmp_path, env)
        assert smoke.returncode == 0, smoke.stdout + smoke.stderr
        assert smoke.stdout == f"wheelport {importlib.metadata.version('wheelport')}\n"
        assert smoke.stderr == ""
