"""Explicit bundles validate foreign artifacts without executing their code."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from packaging.markers import Marker
from packaging.tags import sys_tags
from test_bundle import installed_pip_wheel, write_wheel  # noqa: F401

from wheelport import bundle, cli, sources
from wheelport.targets import Target


def wheel(folder, name="demo", version="1", tag="py3-none-any", dependencies=(), python=">=3.11"):
    folder.mkdir(parents=True, exist_ok=True)
    info = f"{name}-{version}.dist-info"
    return write_wheel(
        folder / f"{name}-{version}-{tag}.whl",
        {
            f"{name}/__init__.py": "raise RuntimeError('wheel code must not execute')\n",
            f"{info}/METADATA": (
                f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\nRequires-Python: {python}\n"
                + "".join(f"Requires-Dist: {value}\n" for value in dependencies)
            ),
            f"{info}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n",
        },
        info,
    )


@pytest.fixture
def packing(tmp_path, monkeypatch):
    input = tmp_path / "requirements.txt"
    input.write_text("demo[cli]==1\n")
    state = {
        "input": input,
        "output": tmp_path / "bundle.zip",
        "target": Target.create(python_version="3.11", python_platform="windows"),
        "pip": wheel(tmp_path / "pip", "pip", "25"),
        "wheels": [wheel(tmp_path / "links")],
    }

    def collect(input, manager, destination, **kwargs):
        for path in state["wheels"]:
            shutil.copyfile(path, destination / path.name)
        return "pip" if manager == "auto" else manager

    state["collect"] = Mock(side_effect=collect)
    state["run"] = Mock(side_effect=AssertionError("must not execute host installer"))
    monkeypatch.setattr(sources, "collect", state["collect"])
    monkeypatch.setattr(sources, "_run", state["run"])
    return state


def pack(state, **options):
    return bundle.pack(
        state["input"], state["output"], target=state["target"],
        pip_wheel=state["pip"], **options,
    )


@pytest.mark.parametrize("version,requires", [("3.11", "==3.11.*"), ("3.11.9", "==3.11.9")])
def test_target_manifest_lock_and_no_runtime_install(packing, version, requires):
    packing["target"] = Target.create(python_version=version, python_platform="win_arm64")
    with zipfile.ZipFile(pack(packing)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        lock = tomllib.loads(archive.read("pylock.toml").decode())
        assert manifest["target"] == packing["target"].manifest()
        assert not {"soabi", "libc", "platform"} & manifest["target"].keys()
        assert manifest["verification"] == {
            "artifact_check": "passed", "runtime_install": "not-run",
        }
        assert lock["requires-python"] == requires
        marker = Marker(lock["environments"][0])
        assert marker.evaluate(packing["target"].marker_environment)
        assert not marker.evaluate({**packing["target"].marker_environment, "sys_platform": "linux"})
        assert not marker.evaluate({**packing["target"].marker_environment, "platform_machine": "AMD64"})
        assert not marker.evaluate({**packing["target"].marker_environment,
                                    "implementation_name": "pypy"})
        for entry in manifest["wheels"]:
            digest = hashlib.sha256(archive.read("wheels/" + entry["filename"])).hexdigest()
            assert entry["sha256"] == digest
        assert manifest["bootstrap"]["name"] == "pip"
    packing["run"].assert_not_called()
    assert packing["collect"].call_args.kwargs["target"] == packing["target"]


@pytest.mark.parametrize("bootstrap", [False, True])
def test_requires_python_rejected_before_publication(packing, bootstrap):
    path = wheel(packing["input"].parent / "bad", "pip" if bootstrap else "demo", python=">=3.12")
    if bootstrap:
        packing["pip"] = path
    else:
        packing["wheels"] = [path]
    with pytest.raises(ValueError, match="different target Python"):
        pack(packing)
    assert not packing["output"].exists()
    packing["run"].assert_not_called()


@pytest.mark.parametrize("fault", ["tag", "name", "version"])
def test_target_wheel_validation(packing, fault):
    path = packing["wheels"][0]
    if fault == "tag":
        renamed = path.with_name(path.name.replace("py3-none-any", "cp311-cp311-manylinux2014_x86_64"))
    else:
        renamed = path.with_name(path.name.replace("demo-1-", "other-1-" if fault == "name" else "demo-2-"))
    path.rename(renamed)
    packing["wheels"] = [renamed]
    with pytest.raises(ValueError, match="incompatible|filename and metadata"):
        pack(packing)
    assert not packing["output"].exists()


@pytest.mark.parametrize("fault", ["missing", "version", "extra", "nested-extra", "bootstrap"])
def test_dependency_closure_failures(packing, fault):
    folder = packing["input"].parent / "closure"
    dependency = 'absent==1; extra == "cli"' if fault == "extra" else "absent==1"
    packing["wheels"] = [wheel(folder, dependencies=[dependency])]
    if fault == "version":
        packing["wheels"].append(wheel(folder, "absent", "2"))
    elif fault == "nested-extra":
        packing["wheels"] = [
            wheel(folder, dependencies=["middle[feature]==1"]),
            wheel(folder, "middle", dependencies=['absent; extra == "feature"']),
        ]
    elif fault == "bootstrap":
        packing["wheels"] = [wheel(folder)]
        packing["pip"] = wheel(folder, "pip", "25", dependencies=["absent"])
    with pytest.raises(ValueError, match="missing absent|version mismatch"):
        pack(packing)
    assert not packing["output"].exists()
    packing["run"].assert_not_called()


def test_extras_propagate_without_activating_unselected_extras(packing):
    folder = packing["input"].parent / "closure"
    packing["wheels"] = [
        wheel(folder, dependencies=['middle[feature]; extra == "cli"',
                                    'absent; extra == "unselected"',
                                    'linux_only; sys_platform == "linux"']),
        wheel(folder, "middle", dependencies=['demo[cycle]; extra == "feature"',
                                              'leaf; extra == "feature"']),
        wheel(folder, "leaf"),
    ]
    pack(packing)
    packing["run"].assert_not_called()


def test_direct_metadata_references_are_only_checked_locally(packing):
    folder = packing["input"].parent / "closure"
    packing["wheels"] = [
        wheel(folder, dependencies=["leaf @ git+https://invalid.invalid/source"]),
        wheel(folder, "leaf"),
    ]
    pack(packing)
    packing["run"].assert_not_called()


def project_input(state, body='version="1"\n'):
    path = state["input"].with_name("pyproject.toml")
    path.write_text('[project]\nname="root"\n' + body
                    + '[build-system]\nrequires=[]\nbuild-backend="never.execute"\n')
    state["input"] = path
    return path


@pytest.mark.parametrize("body", ['version="1"\n', 'dynamic=["version"]\n'])
def test_prebuilt_project_and_root_extras(packing, body):
    project_input(packing, body)
    folder = packing["input"].parent / "project-wheels"
    root = wheel(folder, "root", dependencies=['demo[cli]; extra == "feature"'])
    packing["wheels"] = [wheel(folder, dependencies=['leaf; extra == "cli"']), wheel(folder, "leaf")]
    with zipfile.ZipFile(pack(packing, project_wheel=root, extras=["feature"])) as archive:
        assert "wheels/" + root.name in archive.namelist()
    assert packing["collect"].call_args.kwargs["include_project"] is False
    packing["run"].assert_not_called()


@pytest.mark.parametrize("name,version", [("other", "1"), ("root", "2")])
def test_prebuilt_project_mismatch(packing, name, version):
    project_input(packing)
    root = wheel(packing["input"].parent / "project-wheels", name, version)
    with pytest.raises(ValueError, match="does not match"):
        pack(packing, project_wheel=root)
    packing["collect"].assert_not_called()
    assert not packing["output"].exists()


def test_prebuilt_project_closure_and_dynamic_dependencies(packing):
    root = wheel(packing["input"].parent / "project-wheels", "root", dependencies=["missing"])
    project_input(packing)
    with pytest.raises(ValueError, match="missing missing"):
        pack(packing, project_wheel=root)
    assert not packing["output"].exists()
    project_input(packing, 'dynamic=["version", "dependencies"]\n')
    packing["collect"].reset_mock()
    with pytest.raises(ValueError, match="dynamic dependencies"):
        pack(packing, project_wheel=root)
    packing["collect"].assert_not_called()


@pytest.mark.parametrize("kind", ["optional", "group", "poetry"])
def test_project_declarations_seed_dependency_extras(packing, kind):
    body = 'version="1"\ndependencies=[]\n'
    suffix = {
        "optional": '[project.optional-dependencies]\nfeature=["demo[cli]"]\n',
        "group": '[dependency-groups]\ntest=[{include-group="nested"}]\nnested=["demo[cli]"]\n',
        "poetry": '[tool.poetry.dependencies]\ndemo={version="*", extras=["cli"]}\n',
    }[kind]
    path = project_input(packing, body)
    path.write_text(path.read_text() + suffix)
    packing["wheels"] = [wheel(path.parent / "deps", dependencies=['missing; extra == "cli"'])]
    with pytest.raises(ValueError, match="missing missing"):
        pack(packing, include_project=False, extras=["feature"] if kind == "optional" else [],
             groups=["test"] if kind == "group" else [])


@pytest.mark.parametrize("kind", ["optional", "optional-url", "group", "poetry-extra", "poetry-group"])
def test_normalized_project_selections_activate_dependency_extras(packing, kind):
    suffix = {
        "optional": '[project.optional-dependencies]\n"My.Feature"=["addon[cli]"]\n',
        "optional-url": (
            '[project.optional-dependencies]\n"My.Feature"=['
            '"addon[cli] @ https://invalid.invalid/addon-1-py3-none-any.whl"]\n'
        ),
        "group": (
            '[dependency-groups]\n"My.Group"=[{include-group="Nested_Group"}]\n'
            'nested-group=[{include-group="MY_group"}, "addon[cli]"]\n'
        ),
        "poetry-extra": (
            '[tool.poetry.extras]\n"My.Feature"=["ADDON (>=1)"]\n'
            '[tool.poetry.dependencies]\nAddon={version="*", optional=true, extras=["cli"]}\n'
        ),
        "poetry-group": (
            '[tool.poetry.group."My.Group".dependencies]\nAddon={version="*", extras=["cli"]}\n'
        ),
    }[kind]
    path = project_input(packing, 'version="1"\n'
                         'dependencies=["unselected; sys_platform == \'linux\'"]\n')
    path.write_text(path.read_text() + suffix)
    folder = path.parent / "deps"
    packing["wheels"] = [wheel(folder, "addon", dependencies=['child; extra == "cli"'])]
    options = {
        "include_project": False,
        "extras": ["my_feature"] if kind in {"optional", "optional-url", "poetry-extra"} else [],
        "groups": ["my_group"] if kind in {"group", "poetry-group"} else [],
    }
    with pytest.raises(ValueError, match="missing child"):
        pack(packing, **options)
    assert not packing["output"].exists()
    packing["run"].assert_not_called()
    packing["wheels"].append(wheel(folder, "child"))
    pack(packing, **options)
    packing["run"].assert_not_called()


@pytest.mark.parametrize("compatible", [False, True])
def test_target_bootstrap_download_flags_and_pin(packing, monkeypatch, compatible):
    monkeypatch.setattr(bundle.importlib.metadata, "metadata", lambda name: {
        "Requires-Python": ">=3.11" if compatible else ">=3.12",
    })
    monkeypatch.setattr(bundle.importlib.metadata, "version", lambda name: "99")

    def download(args, cwd):
        assert args[:4] == [sys.executable, "-m", "pip", "download"]
        assert args[-1] == ("pip==99" if compatible else "pip>=25")
        flags = packing["target"].download_args()
        start = args.index("--python-version")
        assert args[start:start + len(flags)] == flags
        destination = Path(args[args.index("--dest") + 1])
        shutil.copyfile(packing["pip"], destination / packing["pip"].name)

    packing["run"].side_effect = download
    bundle.pack(packing["input"], packing["output"], target=packing["target"])
    packing["run"].assert_called_once()


def test_project_pip_automatically_bootstraps(packing):
    packing["wheels"].append(packing["pip"])
    bundle.pack(packing["input"], packing["output"], target=packing["target"])
    packing["run"].assert_not_called()


@pytest.mark.parametrize("flags", [
    ["--python-version", "3.11", "--python-platform", "win_amd64", "--abi", "cp311"],
    ["--python-platform", "windows"],
    ["--python-version", "3.11"],
    ["--abi", f"cp{sys.version_info.major}{sys.version_info.minor}"],
])
def test_cli_target_flags_infer_defaults(tmp_path, monkeypatch, capsys, flags):
    mocked = Mock(return_value=tmp_path / "bundle.zip")
    monkeypatch.setattr(cli, "pack", mocked)
    monkeypatch.setattr("wheelport.targets._native_platform", lambda: "x86_64-manylinux_2_17")
    assert cli.main(["pack", "requirements.txt", "-o", str(tmp_path / "bundle.zip"), *flags]) == 0
    assert isinstance(mocked.call_args.kwargs["target"], Target)
    assert "runtime installation not run" in capsys.readouterr().err


def test_cli_project_wheel_without_target_preserves_native_call(tmp_path, monkeypatch):
    mocked = Mock(return_value=tmp_path / "bundle.zip")
    monkeypatch.setattr(cli, "pack", mocked)
    assert cli.main(["pack", "pyproject.toml", "-o", str(tmp_path / "bundle.zip"),
                     "--project-wheel", "root.whl"]) == 0
    assert mocked.call_args.kwargs["project_wheel"] == Path("root.whl")
    assert "target" not in mocked.call_args.kwargs


@pytest.mark.parametrize("flags", [
    ["--python-version", "3.10"], ["--python-version", "3.11.1.2"],
    ["--python-platform", "invalid"],
    ["--python-version", "3.11", "--abi", "cp312"],
])
def test_cli_invalid_target_returns_clean_error(tmp_path, monkeypatch, capsys, flags):
    mocked = Mock()
    monkeypatch.setattr(cli, "pack", mocked)
    assert cli.main(["pack", "requirements.txt", "-o", str(tmp_path / "bundle.zip"), *flags]) == 1
    assert "wheelport:" in capsys.readouterr().err
    mocked.assert_not_called()


@pytest.mark.parametrize("platform,tag", [
    ("windows", "cp311-cp311-win_amd64"),
    ("aarch64-manylinux_2_17", "cp311-cp311-manylinux2014_aarch64"),
])
def test_real_local_foreign_bundle(
    tmp_path, monkeypatch, installed_pip_wheel, platform, tag,  # noqa: F811
):
    links = tmp_path / "local wheels"
    selected = wheel(links, tag=tag, dependencies=["leaf[cli]"], python=">=3.11,<3.12")
    wheel(links, version="2", python=">=3.12",
          tag="cp312-cp312-win_amd64" if platform == "windows" else "py3-none-any")
    wheel(links, "leaf", dependencies=['end; extra == "cli"'])
    wheel(links, "end")
    input = tmp_path / "requirements.txt"
    input.write_text("demo\n")
    output = tmp_path / "bundle.zip"
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_NO_CACHE_DIR", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    real_run = sources._run
    calls = []

    def run(args, cwd, **kwargs):
        assert "install.py" not in " ".join(args)
        assert "build" not in args and "wheel" not in args
        calls.append(args)
        return real_run(args, cwd, **kwargs)

    monkeypatch.setattr(sources, "_run", run)
    target = Target.create(python_version="3.11", python_platform=platform)
    bundle.pack(input, output, target=target, no_index=True, find_links=[str(links)],
                pip_wheel=installed_pip_wheel)
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["target"] == target.manifest()
        assert {entry["name"] for entry in manifest["wheels"]} == {"demo", "leaf", "end", "pip"}
        assert archive.read("wheels/" + selected.name) == selected.read_bytes()
        extracted = tmp_path / "extracted"
        archive.extractall(extracted)
    assert any(args[:3] == ["uv", "pip", "compile"] for args in calls)
    destination = tmp_path / "target-environment"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(extracted / "install.py"), "--venv", str(destination)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    compatible = tag in {str(value) for value in sys_tags()}
    if compatible:
        assert result.returncode == 0, result.stdout + result.stderr
        python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        installed = subprocess.run(
            [str(python), "-I", "-c",
             "import importlib.metadata as m; print(m.version('demo'))"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        assert installed.stdout.strip() == "1"
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        reason = ("implementation" if sys.implementation.name != "cpython" else
                  "python" if sys.version_info[:2] != (3, 11) else
                  "ABI/architecture/platform")
        assert f"incompatible target {reason}:" in result.stderr
        assert not destination.exists()

    # Test rejection independently of whether this matrix host matches the collected artifacts.
    required = manifest["target"]
    full = f"3.11.{sys.version_info.micro + 1}"
    required.update(python_full_version=full, python_patch=sys.version_info.micro + 1)
    required["marker_environment"].update(python_full_version=full, implementation_version=full)
    (extracted / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rejected = tmp_path / "must-not-exist"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(extracted / "install.py"), "--venv", str(rejected)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    reason = ("implementation" if sys.implementation.name != "cpython" else
              "python" if sys.version_info[:2] != (3, 11) else "python_full_version")
    assert f"incompatible target {reason}:" in result.stderr
    assert not rejected.exists()


def test_buildable_project_requires_opt_in(packing, monkeypatch):
    project_input(packing)
    monkeypatch.undo()
    run = Mock(side_effect=AssertionError("must not build root project"))
    monkeypatch.setattr(sources, "_run", run)
    with pytest.raises(ValueError, match="--dependencies-only or --project-wheel"):
        pack(packing)
    run.assert_not_called()
    assert not packing["output"].exists()


def test_real_prebuilt_root_with_dynamic_version(tmp_path, monkeypatch, request):
    pip = request.getfixturevalue("installed_pip_wheel")
    links = tmp_path / "links"
    wheel(links, dependencies=['leaf; extra == "cli"'])
    wheel(links, "leaf")
    root = wheel(tmp_path / "root-wheel", "root", "7", dependencies=["demo[cli]"])
    input = tmp_path / "pyproject.toml"
    input.write_text(
        '[project]\nname="root"\ndynamic=["version"]\ndependencies=["demo[cli]"]\n'
        '[build-system]\nrequires=["backend-that-does-not-exist"]\nbuild-backend="never.execute"\n'
    )
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    output = bundle.pack(
        input, tmp_path / "bundle.zip", manager="pip", no_index=True,
        find_links=[str(links)], pip_wheel=pip, project_wheel=root,
        target=Target.create(python_version="3.11", python_platform="windows"),
    )
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert {entry["name"] for entry in manifest["wheels"]} == {"root", "demo", "leaf", "pip"}
        assert archive.read("wheels/" + root.name) == root.read_bytes()


def test_native_call_shapes_and_verification_marker(packing):
    packing["run"].side_effect = None
    bundle.pack(packing["input"], packing["output"], pip_wheel=packing["pip"])
    assert "target" not in packing["collect"].call_args.kwargs
    packing["run"].assert_called_once()
    args, _ = packing["run"].call_args.args
    assert args[:2] == [sys.executable, "-I"]
    assert Path(args[2]).name == "install.py" and args[3] == "--venv"
    with zipfile.ZipFile(packing["output"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["verification"] == {
            "artifact_check": "passed", "runtime_install": "passed",
        }


def test_real_matching_explicit_bundle_installs_offline(
    tmp_path, monkeypatch, installed_pip_wheel,  # noqa: F811
):
    # A supported deployment baseline also runs on newer host libc versions.
    supported = {
        "win_amd64", "win_arm64", "win32", "macosx_11_0_arm64", "macosx_11_0_x86_64",
        "manylinux_2_17_x86_64", "manylinux_2_17_aarch64",
        "musllinux_1_2_x86_64", "musllinux_1_2_aarch64",
    }
    platform = next((tag.platform for tag in sys_tags() if tag.platform in supported), None)
    if platform is None:
        pytest.skip("Host has no supported explicit deployment baseline")
    target = Target.create(python_platform=platform)
    links = tmp_path / "links"
    wheel(links)
    input = tmp_path / "requirements.txt"
    input.write_text("demo==1\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    output = bundle.pack(input, tmp_path / "bundle.zip", target=target, no_index=True,
                         find_links=[str(links)], pip_wheel=installed_pip_wheel)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(extracted)
    # Use the existing audit harness to deny network in both installer and children.
    from test_bundle import run_installer

    destination = tmp_path / "offline-venv"
    result = run_installer(extracted, "--venv", str(destination))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NETWORK FORBIDDEN" not in result.stdout + result.stderr
    assert "Installed bundle into" in result.stdout
