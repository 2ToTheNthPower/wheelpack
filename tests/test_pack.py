"""The pack package: source + dependencies + installer assembled into one ZIP."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from helpers import build_wheel, digest, write_project

from wheelpack import pack as pack_module

DIGEST = "a" * 64


def extract(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(destination)
    return destination


def run_installer(bundle: Path, *args, expect: int = 0):
    result = subprocess.run(
        [sys.executable, str(bundle / "install.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expect, result.stdout + result.stderr
    return result


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def make_source_project(root: Path, name="app", dependencies=()):
    root.mkdir(parents=True, exist_ok=True)
    (root / "builder.py").write_text(
        "import pathlib, zipfile\n"
        "def build_wheel(directory, config_settings=None, metadata_directory=None):\n"
        f"    wheel = pathlib.Path(directory) / {name + '-1-py3-none-any.whl'!r}\n"
        "    with zipfile.ZipFile(wheel, 'w') as archive:\n"
        f"        archive.writestr({name + '/__init__.py'!r}, 'VALUE = 1\\n')\n"
        f"        archive.writestr({name + '-1.dist-info/METADATA'!r}, "
        f"'Metadata-Version: 2.1\\nName: {name}\\nVersion: 1\\n')\n"
        f"        archive.writestr({name + '-1.dist-info/WHEEL'!r}, "
        "'Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
        f"        archive.writestr({name + '-1.dist-info/RECORD'!r}, '')\n"
        "    return wheel.name\n",
        encoding="utf-8",
    )
    rendered = ", ".join(f'"{dependency}"' for dependency in dependencies)
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "builder"\nbackend-path = ["."]\n'
        f'[project]\nname = "{name}"\nversion = "1"\nrequires-python = ">=3.11"\n'
        f"dependencies = [{rendered}]\n",
        encoding="utf-8",
    )
    return root


# --- archive assembly -------------------------------------------------------


def test_pack_creates_expected_members(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())
    assert {
        "README.txt",
        "install.py",
        "manifest.json",
        "pylock.toml",
        "requirements.txt",
        "source/pyproject.toml",
        "wheels/demo-1-py3-none-any.whl",
    } <= names
    assert not any("resolved.pylock" in name for name in names)


def test_pack_manifest_records_wheels_source_and_manager(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    wheel = build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
    assert manifest["format"] == 1
    assert manifest["manager"] == "pip"
    assert manifest["source"]["kind"] == "project"
    assert manifest["wheels"] == [
        {
            "filename": wheel.name,
            "name": "demo",
            "version": "1",
            "sha256": digest(wheel),
        }
    ]


def test_pack_requirements_file_lists_local_wheels_with_hashes(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    wheel = build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    with zipfile.ZipFile(archive) as zipped:
        text = zipped.read("requirements.txt").decode()
    assert "--no-index" in text
    assert f"./wheels/{wheel.name} --hash=sha256:{digest(wheel)}" in text


def test_pack_omits_source_when_requested(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(
        root, archive, manager="pip", find_links=[str(links)], no_index=True, include_source=False
    )
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
    assert not any(name.startswith("source/") for name in names)


def test_pack_requirements_input_copies_requirements(tmp_path):
    links = tmp_path / "links"
    wheel = build_wheel(links, "demo")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        f"--no-index\n--find-links {links.as_uri()}\ndemo==1 --hash=sha256:{digest(wheel)}\n",
        encoding="utf-8",
    )
    archive = tmp_path / "bundle.zip"
    pack_module.pack(requirements, archive, manager="pip")
    with zipfile.ZipFile(archive) as zipped:
        assert "source/requirements.txt" in zipped.namelist()


def test_pack_does_not_overwrite_existing_output(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    archive.write_text("existing", encoding="utf-8")
    with pytest.raises(pack_module.PackError, match="already exists"):
        pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    assert archive.read_text(encoding="utf-8") == "existing"


def test_pack_requires_existing_parent(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    with pytest.raises(pack_module.PackError, match="parent directory"):
        pack_module.pack(
            root, tmp_path / "missing" / "bundle.zip",
            manager="pip", find_links=[str(links)], no_index=True,
        )


# --- the standalone installer ----------------------------------------------


def test_installer_is_standard_library_only(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    bundle = extract(archive, tmp_path / "extracted")
    text = (bundle / "install.py").read_text(encoding="utf-8")
    assert "import wheelpack" not in text
    assert "from wheelpack" not in text


def test_installer_verify_only(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    bundle = extract(archive, tmp_path / "extracted")
    result = run_installer(bundle, "--verify-only")
    assert "verified 1 wheels" in result.stdout


def test_installer_detects_tampered_wheel(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    wheel = build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    bundle = extract(archive, tmp_path / "extracted")
    (bundle / "wheels" / wheel.name).write_bytes(b"tampered")
    run_installer(bundle, "--verify-only", expect=1)


def test_installer_installs_dependencies(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    bundle = extract(archive, tmp_path / "extracted")
    venv = tmp_path / "env"
    run_installer(bundle, "--venv", str(venv))
    python = venv_python(venv)
    result = subprocess.run(
        [str(python), "-c", "import demo, importlib.metadata as m; print(m.version('demo'))"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_installer_refuses_existing_destination_without_reuse(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    pack_module.pack(root, archive, manager="pip", find_links=[str(links)], no_index=True)
    bundle = extract(archive, tmp_path / "extracted")
    venv = tmp_path / "env"
    venv.mkdir()
    run_installer(bundle, "--venv", str(venv), expect=1)


def test_installer_can_install_the_source_project(tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo")
    source = make_source_project(tmp_path / "source-project", "app", dependencies=("demo",))
    archive = tmp_path / "bundle.zip"
    pack_module.pack(source, archive, manager="pip", find_links=[str(links)], no_index=True)
    bundle = extract(archive, tmp_path / "extracted")
    venv = tmp_path / "env"
    run_installer(bundle, "--venv", str(venv), "--install-source")
    python = venv_python(venv)
    result = subprocess.run(
        [str(python), "-c", "import app, demo; print(app.VALUE)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
