"""Bundle publication contracts and real, network-denied offline installation."""

import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wheelport import bundle, cli, sources


def write_wheel(path, members, info):
    """Write complete wheel RECORDs, including hashes for entry-point metadata."""
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            content = content.encode() if isinstance(content, str) else content
            archive.writestr(name, content)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            writer.writerow([name, "sha256=" + digest.decode(), len(content)])
        writer.writerow([f"{info}/RECORD", "", ""])
        archive.writestr(f"{info}/RECORD", record.getvalue())
    return path


def make_wheel(folder, name="demo", version="1", *, dependencies=(), console=False):
    folder.mkdir(parents=True, exist_ok=True)
    info = f"{name}-{version}.dist-info"
    members = {
        f"{name}/__init__.py": (
            "import transitive\nVALUE = 'demo:' + transitive.VALUE\ndef main():\n    print(VALUE)\n"
            if console
            else f"VALUE = {name!r}\n"
        ),
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
        ),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: wheelport-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    if console:
        members[f"{info}/entry_points.txt"] = "[console_scripts]\nwheelport-demo = demo:main\n"
    return write_wheel(folder / f"{name}-{version}-py3-none-any.whl", members, info)


@pytest.fixture(scope="session")
def installed_pip_wheel(tmp_path_factory):
    """Repackage the real installed pip, never downloading or stubbing its code."""
    distribution = importlib.metadata.distribution("pip")
    files = distribution.files
    assert files, "installed pip must provide its distribution file list"
    metadata = [path for path in files if str(path).endswith(".dist-info/METADATA")]
    assert len(metadata) == 1
    info = metadata[0].parent.as_posix()
    members = {}
    for path in files:
        name = path.as_posix()
        # Installed scripts have ../ paths; only package code and metadata belong here.
        if not name.startswith(("pip/", info + "/")):
            continue
        if path.suffix == ".pyc" or name == f"{info}/RECORD":
            continue
        assert not path.is_absolute() and ".." not in path.parts
        members[name] = distribution.locate_file(path).read_bytes()
    assert "pip/__main__.py" in members
    assert f"{info}/WHEEL" in members
    path = tmp_path_factory.mktemp("installed-pip") / (
        f"pip-{distribution.version}-py3-none-any.whl"
    )
    return write_wheel(path, members, info)


@pytest.fixture
def packing(tmp_path, monkeypatch):
    """Replace external collection and processes, not bundle validation or writing."""
    pip = make_wheel(tmp_path / "bootstrap-input", "pip", "25.0")
    state = SimpleNamespace(
        input=tmp_path / "requirements.txt",
        output=tmp_path / "bundle.zip",
        pip=pip,
        wheels=[make_wheel(tmp_path / "input-wheels")],
        calls=[],
    )
    state.input.write_text("demo==1\n", encoding="utf-8")

    def collect(input, manager, wheels, **kwargs):
        for wheel in state.wheels:
            shutil.copyfile(wheel, wheels / wheel.name)
        # Even unexpected staging files must not leak the native resolution lock.
        (wheels.parent.parent / "resolved.pylock.toml").write_text(
            'url = "https://user:secret@repo.invalid/signed?token=private"\n'
        )
        return "pip" if manager == "auto" else manager

    def run(args, cwd, **kwargs):
        state.calls.append((args, cwd, kwargs))
        if args[:4] == [sys.executable, "-m", "pip", "download"]:
            destination = Path(args[args.index("--dest") + 1])
            shutil.copyfile(state.pip, destination / state.pip.name)
        else:
            assert args[:2] == [sys.executable, "-I"]
            assert Path(args[2]).name == "install.py"
            assert args[3] == "--venv"
            assert (
                Path(args[2]).read_bytes()
                == Path(bundle.__file__).with_name("install.py").read_bytes()
            )
            assert (Path(args[2]).parent / "pylock.toml").is_file()
            assert not state.output.exists()
        return ""

    state.collect = Mock(side_effect=collect)
    state.run = Mock(side_effect=run)
    monkeypatch.setattr(sources, "collect", state.collect)
    monkeypatch.setattr(sources, "_run", state.run)
    return state


@pytest.mark.parametrize("project_pip", [False, True])
def test_archive_layout_and_final_artifact_hashes(packing, project_pip):
    if project_pip:
        packing.wheels.append(packing.pip)
    result = bundle.pack(packing.input, packing.output, pip_wheel=packing.pip)
    assert result == packing.output.absolute()
    packing.run.assert_called_once()
    with zipfile.ZipFile(result) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert set(names) == {
            "manifest.json",
            "pylock.toml",
            "requirements.txt",
            "install.py",
            "README.txt",
            "wheels/demo-1-py3-none-any.whl",
            f"wheels/{packing.pip.name}",
            f"bootstrap/{packing.pip.name}",
        }
        assert not any("resolved" in name for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        lock_text = archive.read("pylock.toml").decode()
        lock = tomllib.loads(lock_text)
        requirements = archive.read("requirements.txt").decode()
        assert lock["lock-version"] == "1.0"
        assert lock["requires-python"] == f"=={sys.version_info.major}.{sys.version_info.minor}.*"
        assert manifest["format"] == 1 and manifest["manager"] == "pip"
        assert manifest["target"]["python"] == list(sys.version_info[:2])
        assert [entry["name"] for entry in lock["packages"]] == ["demo", "pip"]
        assert [entry["name"] for entry in manifest["wheels"]] == ["demo", "pip"]
        for package, entry in zip(lock["packages"], manifest["wheels"], strict=True):
            path = "wheels/" + entry["filename"]
            digest = hashlib.sha256(archive.read(path)).hexdigest()
            assert entry["sha256"] == digest
            assert package == {
                "name": entry["name"],
                "version": entry["version"],
                "wheels": [{"path": path, "hashes": {"sha256": digest}}],
            }
            assert f"./{path} --hash=sha256:{digest}\n" in requirements
        assert requirements.startswith("--no-index\n--only-binary :all:\n")
        assert requirements.count(packing.pip.name) == 1
        assert manifest["bootstrap"] == manifest["wheels"][1]
        assert archive.read(f"bootstrap/{packing.pip.name}") == archive.read(
            f"wheels/{packing.pip.name}"
        )
        for name in ("manifest.json", "pylock.toml", "requirements.txt", "README.txt"):
            text = archive.read(name).decode()
            assert "http://" not in text and "https://" not in text
            assert "secret" not in text and "resolved.pylock" not in text


def test_options_forwarded_and_bootstrap_download_pinned(packing):
    options = {
        "manager": "uv",
        "indexes": ["https://first.invalid", "https://second.invalid"],
        "find_links": ["/local/wheels"],
        "no_index": True,
        "extras": ["feature"],
        "groups": ["test"],
        "include_project": False,
    }
    bundle.pack(packing.input, packing.output, **options)
    packing.collect.assert_called_once()
    args, kwargs = packing.collect.call_args
    assert args[:2] == (packing.input, "uv")
    assert Path(args[2]).name == "wheels"
    assert kwargs == {key: value for key, value in options.items() if key != "manager"}
    download, cwd, kwargs = packing.calls[0]
    assert download[:7] == [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--no-deps",
        "--only-binary",
        ":all:",
    ]
    assert download[-1] == f"pip=={importlib.metadata.version('pip')}"
    assert "--no-index" in download and "--index-url" not in download
    assert download[download.index("--find-links") + 1] == "/local/wheels"
    assert len(packing.calls) == 2
    assert not cwd.exists()  # Temporary staging is removed after publication.


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling-symlink"])
def test_existing_output_preserved_before_collection(packing, kind):
    original = b"user's existing archive"
    if kind == "file":
        packing.output.write_bytes(original)
    elif kind == "directory":
        packing.output.mkdir()
        (packing.output / "keep").write_bytes(original)
    else:
        target = packing.output.with_name("existing")
        try:
            packing.output.symlink_to(target)
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                pytest.skip("symlink creation requires Windows privileges")
            raise
        if kind == "symlink":
            target.write_bytes(original)
    with pytest.raises(ValueError, match="Output already exists"):
        bundle.pack(packing.input, packing.output)
    packing.collect.assert_not_called()
    packing.run.assert_not_called()
    if kind == "directory":
        assert (packing.output / "keep").read_bytes() == original
    elif kind == "dangling-symlink":
        assert packing.output.is_symlink() and not packing.output.exists()
    else:
        assert packing.output.read_bytes() == original


def test_missing_output_parent_rejected_before_collection(packing):
    with pytest.raises(ValueError, match="parent directory does not exist"):
        bundle.pack(packing.input, packing.output.parent / "missing" / "bundle.zip")
    packing.collect.assert_not_called()


@pytest.mark.parametrize("stage", ["collect", "bootstrap", "install"])
def test_failure_leaves_no_zip(packing, stage):
    if stage == "collect":
        packing.collect.side_effect = ValueError("collection failed")
    elif stage == "bootstrap":
        packing.run.side_effect = ValueError("bootstrap failed")
    else:
        packing.run.side_effect = ValueError("offline install failed")
    with pytest.raises(ValueError, match="failed"):
        bundle.pack(
            packing.input,
            packing.output,
            pip_wheel=packing.pip if stage == "install" else None,
        )
    assert not packing.output.exists()
    if stage == "collect":
        packing.run.assert_not_called()
    work = Path(packing.collect.call_args.args[2]).parent.parent
    assert not work.exists()


def test_concurrent_output_creation_preserved(packing):
    packing.run.side_effect = lambda *args, **kwargs: packing.output.write_bytes(b"other writer")
    with pytest.raises(ValueError, match="Output already exists"):
        bundle.pack(packing.input, packing.output, pip_wheel=packing.pip)
    assert packing.output.read_bytes() == b"other writer"


def test_archive_write_failure_removes_partial_output(packing, monkeypatch):
    monkeypatch.setattr(zipfile.ZipFile, "write", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        bundle.pack(packing.input, packing.output, pip_wheel=packing.pip)
    packing.run.assert_called_once()
    assert not packing.output.exists()


def test_empty_collection_still_locks_bootstrap_pip(packing):
    packing.wheels = []
    bundle.pack(packing.input, packing.output, pip_wheel=packing.pip)
    with zipfile.ZipFile(packing.output) as archive:
        lock = tomllib.loads(archive.read("pylock.toml").decode())
        assert [entry["name"] for entry in lock["packages"]] == ["pip"]
        assert lock["packages"][0]["wheels"][0]["path"] == f"wheels/{packing.pip.name}"


@pytest.mark.parametrize("fault", ["missing", "multiple", "wrong-name", "wrong-version"])
def test_malformed_wheel_metadata_fails_before_installer(packing, fault):
    wheel = packing.wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    metadata = "demo-1.dist-info/METADATA"
    if fault == "missing":
        del members[metadata]
    elif fault == "multiple":
        members["other-1.dist-info/METADATA"] = members[metadata]
    else:
        members[metadata] = (
            f"Metadata-Version: 2.1\nName: {'other' if fault == 'wrong-name' else 'demo'}\n"
            f"Version: {'2' if fault == 'wrong-version' else '1'}\n"
        )
    members.pop("demo-1.dist-info/RECORD")
    write_wheel(wheel, members, "demo-1.dist-info")
    with pytest.raises(ValueError, match="METADATA|filename and metadata disagree"):
        bundle.pack(packing.input, packing.output, pip_wheel=packing.pip)
    packing.run.assert_not_called()
    assert not packing.output.exists()


def test_multiple_versions_of_same_distribution_rejected(packing):
    packing.wheels.append(make_wheel(packing.wheels[0].parent, version="2"))
    with pytest.raises(ValueError, match="multiple wheels for the same package"):
        bundle.pack(packing.input, packing.output, pip_wheel=packing.pip)
    packing.run.assert_not_called()
    assert not packing.output.exists()


@pytest.mark.parametrize("bootstrap", [False, True])
def test_incompatible_wheel_rejected(packing, bootstrap):
    original = packing.pip if bootstrap else packing.wheels[0]
    incompatible = original.with_name(original.name.replace("py3-none-any", "cp00-cp00-wrong"))
    original.rename(incompatible)
    if bootstrap:
        packing.pip = incompatible
    else:
        packing.wheels = [incompatible]
    with pytest.raises(ValueError, match="incompatible with the packing interpreter"):
        bundle.pack(packing.input, packing.output, pip_wheel=packing.pip)
    packing.run.assert_not_called()
    assert not packing.output.exists()


@pytest.mark.parametrize("fault", ["not-pip", "different-pip", "no-download", "two-downloads"])
def test_invalid_bootstrap_leaves_no_archive(packing, fault):
    pip_wheel = packing.pip
    message = "pip distribution"
    if fault == "not-pip":
        pip_wheel = packing.wheels[0]
    elif fault == "different-pip":
        packing.wheels.append(make_wheel(packing.pip.parent, "pip", "24.0"))
        message = "Project pins a different pip"
    else:
        pip_wheel = None
        message = "exactly one pip wheel"

        def download(args, cwd):
            if fault == "two-downloads":
                folder = Path(args[args.index("--dest") + 1])
                make_wheel(folder, "pip", "24.0")
                make_wheel(folder, "pip", "25.0")

        packing.run.side_effect = download
    with pytest.raises(ValueError, match=message):
        bundle.pack(packing.input, packing.output, pip_wheel=pip_wheel)
    assert not packing.output.exists()
    assert packing.run.call_count == (0 if pip_wheel else 1)


def test_api_command_failure_redacts_secrets(tmp_path, monkeypatch):
    # Exercise the real _run error boundary instead of injecting a pre-redacted error.
    monkeypatch.setenv("UV_INDEX_PRIVATE_PASSWORD", "private-password")
    output = tmp_path / "bundle.zip"
    input = tmp_path / "requirements.txt"
    command = [sys.executable, "https://user:password@repo.invalid/simple?token=secret"]
    monkeypatch.setattr(
        sources,
        "collect",
        lambda *args, **kwargs: sources._run(command, tmp_path),
    )
    monkeypatch.setattr(
        sources.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                command,
                2,
                "",
                "Resolution failed at https://host.invalid/?token=secret private-password",
            )
        ),
    )
    with pytest.raises(ValueError) as caught:
        bundle.pack(input, output)
    text = str(caught.value)
    assert "exit 2" in text and "Resolution failed" in text and "redacted" in text
    assert all(secret not in text for secret in ("user:", "password", "token=", "secret"))
    assert not output.exists()


@pytest.mark.parametrize("error", [ValueError, OSError])
def test_cli_error_redaction(tmp_path, monkeypatch, capsys, error):
    monkeypatch.setenv("POETRY_HTTP_BASIC_PRIVATE_PASSWORD", "private-password")
    pack = Mock(
        side_effect=error(
            "Resolution failed https://user:password@host.invalid/simple?token=secret private-password"
        )
    )
    monkeypatch.setattr(cli, "pack", pack)
    assert cli.main(["pack", "requirements.txt", "-o", str(tmp_path / "bundle.zip")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "wheelport: Resolution failed" in captured.err and "redacted" in captured.err
    assert all(
        secret not in captured.err
        for secret in (
            "user:",
            "password",
            "token=",
            "secret",
            "Traceback",
        )
    )


def test_cli_success_forwards_all_options(packing, capsys):
    assert (
        cli.main(
            [
                "pack",
                str(packing.input),
                "-o",
                str(packing.output),
                "--manager",
                "poetry",
                "--index-url",
                "https://first.invalid",
                "--index-url",
                "https://second.invalid",
                "--find-links",
                "/first",
                "--find-links",
                "/second",
                "--no-index",
                "--extra",
                "a",
                "--extra",
                "b",
                "--group",
                "test",
                "--dependencies-only",
                "--pip-wheel",
                str(packing.pip),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out.strip() == str(packing.output.absolute())
    assert "checking offline installation" in captured.err
    assert packing.collect.call_args.args[:2] == (packing.input, "poetry")
    assert packing.collect.call_args.kwargs == {
        "indexes": ["https://first.invalid", "https://second.invalid"],
        "find_links": ["/first", "/second"],
        "no_index": True,
        "extras": ["a", "b"],
        "groups": ["test"],
        "include_project": False,
    }
    packing.run.assert_called_once()  # --pip-wheel must bypass bootstrap download.


AUDIT_HOOK = """
import sys
def deny_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        print('NETWORK FORBIDDEN: ' + event, file=sys.stderr, flush=True)
        raise RuntimeError('network forbidden by offline test')
sys.addaudithook(deny_network)
"""

INSTALL_HARNESS = (
    AUDIT_HOOK
    + f"""
import runpy, subprocess
from pathlib import PurePosixPath
real_run = subprocess.run
hook = {AUDIT_HOOK!r}
def guarded_run(args, *positional, **kwargs):
    args = list(args)
    loader = PurePosixPath(args[0])
    if (len(args) == 1 and loader.is_absolute()
            and loader.name.startswith('ld-musl-') and loader.name.endswith('.so.1')):
        # The ELF runtime loader only reports its version, without Python or network access.
        print('MUSL LOADER PROBE', flush=True)
        return real_run(args, *positional, **kwargs)
    assert args[1:3] in (['-I', '-c'], ['-I', '-m']), args
    if args[2] == '-m':
        module = args[3]
        args[2:4] = ['-c', 'import runpy; runpy.run_module(' + repr(module) +
                     ', run_name="__main__")']
    args[3] = hook + '\\n' + args[3]
    print('GUARDED CHILD', flush=True)
    return real_run(args, *positional, **kwargs)
subprocess.run = guarded_run
installer = sys.argv.pop(1)
sys.argv[0] = installer
runpy.run_path(installer, run_name='__main__')
"""
)


@pytest.fixture(scope="session")
def offline_bundle(tmp_path_factory, installed_pip_wheel):
    root = tmp_path_factory.mktemp("offline-bundle")
    links = root / "local wheels"
    make_wheel(links, "transitive", "2")
    make_wheel(links, dependencies=["transitive==2"], console=True)
    requirements = root / "requirements.txt"
    requirements.write_text("demo==1\n", encoding="utf-8")
    output = root / "bundle.zip"
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "wheelport",
            "pack",
            str(requirements),
            "-o",
            str(output),
            "--no-index",
            "--find-links",
            str(links),
            "--pip-wheel",
            str(installed_pip_wheel),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == str(output)
    extracted = root / "extracted"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(extracted)
    return extracted


def run_installer(root, *args, env=None):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", INSTALL_HARNESS, str(root / "install.py"), *args],
        cwd=root.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize(
    "operation,event",
    [
        ("socket.getaddrinfo('localhost', 9)", "socket.getaddrinfo"),
        ("socket.socket().connect(('127.0.0.1', 9))", "socket.connect"),
    ],
)
def test_network_guard_blocks_sockets_in_parent_and_child(tmp_path, child, operation, event):
    code = "import socket; " + operation
    if child:
        (tmp_path / "install.py").write_text(
            "import subprocess, sys\n"
            f"subprocess.run([sys.executable, '-I', '-c', {code!r}], check=True)\n",
            encoding="utf-8",
        )
        result = run_installer(tmp_path)
        assert result.stdout.count("GUARDED CHILD") == 1
    else:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                AUDIT_HOOK + "\n" + code,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode != 0 and f"NETWORK FORBIDDEN: {event}" in result.stderr


def test_real_offline_install_transitive_entrypoint_and_pip(offline_bundle, tmp_path):
    destination = tmp_path / "new environment"
    hostile = tmp_path / "hostile-target"
    config = tmp_path / "pip.conf"
    config.write_text(f"[global]\nrequire-virtualenv = true\ntarget = {hostile}\n")
    env = dict(
        os.environ,
        PIP_INDEX_URL="https://invalid.invalid/simple",
        PIP_EXTRA_INDEX_URL="https://invalid.invalid/extra",
        PIP_FIND_LINKS="https://invalid.invalid/wheels",
        PIP_TARGET=str(hostile),
        PIP_PREFIX=str(hostile),
        PIP_USER="1",
        PIP_REQUIRE_HASHES="1",
        PIP_CONFIG_FILE=str(config),
    )
    result = run_installer(offline_bundle, "--venv", str(destination), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installed bundle into" in result.stdout
    assert result.stdout.count("GUARDED CHILD") == 3
    assert "NETWORK FORBIDDEN" not in result.stdout + result.stderr
    assert not hostile.exists()
    python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            AUDIT_HOOK
            + """
import importlib.metadata as metadata, json, demo, transitive, sys
print(json.dumps({'value': demo.VALUE, 'dependency': transitive.VALUE,
                 'versions': {name: metadata.version(name) for name in ('demo', 'transitive', 'pip')},
                 'prefix': sys.prefix, 'base_prefix': sys.base_prefix}))
""",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    data = json.loads(probe.stdout)
    assert data["value"] == "demo:transitive" and data["dependency"] == "transitive"
    assert data["versions"] == {
        "demo": "1",
        "transitive": "2",
        "pip": importlib.metadata.version("pip"),
    }
    assert Path(data["prefix"]).resolve() == destination.resolve()
    assert data["prefix"] != data["base_prefix"]
    check = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            AUDIT_HOOK + "\nimport runpy; runpy.run_module('pip', run_name='__main__')",
            "--isolated",
            "--disable-pip-version-check",
            "check",
        ],
        env={**os.environ, "PIP_CONFIG_FILE": os.devnull},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert "No broken requirements found" in check.stdout
    script = destination / (
        "Scripts/wheelport-demo.exe" if os.name == "nt" else "bin/wheelport-demo"
    )
    command = subprocess.run([str(script)], capture_output=True, text=True, check=True, timeout=30)
    assert command.stdout.strip() == "demo:transitive"


def test_real_verify_only_creates_no_environment(offline_bundle, tmp_path):
    destination = tmp_path / "must-not-exist"
    result = run_installer(offline_bundle, "--verify-only", "--venv", str(destination))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Bundle verified; no environment changes made" in result.stdout
    assert result.stdout.count("GUARDED CHILD") == 1
    assert "NETWORK FORBIDDEN" not in result.stdout + result.stderr
    assert not destination.exists()


def test_real_reuse_replaces_modified_same_version_package(offline_bundle, tmp_path):
    destination = tmp_path / "reused environment"
    result = run_installer(offline_bundle, "--venv", str(destination))
    assert result.returncode == 0, result.stdout + result.stderr
    python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = subprocess.run(
        [str(python), "-I", "-c", "import demo; print(demo.__file__)"],
        capture_output=True, text=True, check=True,
    )
    module = Path(probe.stdout.strip())
    module.write_text("VALUE = 'modified'\n", encoding="utf-8")
    result = run_installer(offline_bundle, "--venv", str(destination), "--reuse")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("GUARDED CHILD") == 4
    assert "NETWORK FORBIDDEN" not in result.stdout + result.stderr
    assert "VALUE = 'modified'" not in module.read_text(encoding="utf-8")
    probe = subprocess.run(
        [str(python), "-I", "-c", "import demo; print(demo.VALUE)"],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip() == "demo:transitive"


@pytest.mark.parametrize("section", ["wheels", "bootstrap"])
def test_real_corrupted_wheel_rejected_before_venv(offline_bundle, tmp_path, section):
    extracted = tmp_path / "corrupt bundle"
    shutil.copytree(offline_bundle, extracted)
    wheel = next((extracted / section).glob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"corruption")
    destination = tmp_path / "must-not-exist"
    result = run_installer(extracted, "--venv", str(destination))
    assert result.returncode == 1
    assert "Installation failed:" in result.stderr and "SHA-256 mismatch" in result.stderr
    assert "Traceback" not in result.stderr and "GUARDED CHILD" not in result.stdout
    assert not destination.exists()
