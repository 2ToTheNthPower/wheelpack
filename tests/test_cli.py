"""The single-command CLI: resolve, retrieve, and pack in one step."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile

import pytest
from helpers import build_wheel, write_project

from wheelpack import cli


def test_version_via_module():
    result = subprocess.run(
        [sys.executable, "-m", "wheelpack", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("wheelpack ")


def test_single_command_packs_a_bundle(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    code = cli.main(
        [str(root), "-o", str(archive), "--manager", "pip",
         "--find-links", str(links), "--no-index"]
    )
    assert code == 0
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
        assert "source/pyproject.toml" in zipped.namelist()
    assert manifest["manager"] == "pip"
    assert [entry["name"] for entry in manifest["wheels"]] == ["demo"]


def test_default_output_is_derived_from_input(tmp_path, monkeypatch, capsys):
    write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    monkeypatch.chdir(tmp_path)
    code = cli.main(
        ["project", "--manager", "pip", "--find-links", str(links), "--no-index"]
    )
    assert code == 0
    assert (tmp_path / "project.zip").is_file()
    assert "project.zip" in capsys.readouterr().out


def test_target_flags_cross_pack(tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo", tag="cp312-cp312-win_amd64", purelib=False)
    build_wheel(links, "demo", tag="cp312-cp312-manylinux_2_17_x86_64", purelib=False)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        f"--no-index\n--find-links {links.as_uri()}\ndemo==1\n", encoding="utf-8"
    )
    archive = tmp_path / "bundle.zip"
    code = cli.main(
        [str(requirements), "-o", str(archive), "--manager", "pip",
         "--find-links", str(links), "--no-index",
         "--python-version", "3.12", "--python-platform", "x86_64-pc-windows-msvc",
         "--abi", "cp312"]
    )
    assert code == 0
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
    assert manifest["target"]["python_platform"] == "x86_64-pc-windows-msvc"
    assert manifest["wheels"][0]["filename"] == "demo-1-cp312-cp312-win_amd64.whl"


def test_force_replaces_an_existing_output(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    arguments = [
        str(root), "-o", str(archive), "--manager", "pip",
        "--find-links", str(links), "--no-index",
    ]
    assert cli.main(arguments) == 0
    assert cli.main(arguments) == 1  # refuses to overwrite
    assert cli.main([*arguments, "--force"]) == 0


def test_errors_return_one(tmp_path, capsys):
    code = cli.main([str(tmp_path / "missing"), "-o", str(tmp_path / "bundle.zip")])
    assert code == 1
    assert "wheelpack:" in capsys.readouterr().err


def test_unknown_manager_exits():
    with pytest.raises(SystemExit):
        cli.main(["project", "--manager", "conda"])
