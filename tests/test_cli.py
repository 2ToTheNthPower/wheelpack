"""The command-line interface: resolve, retrieve, and pack."""

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


def test_resolve_writes_pylock(tmp_path, capsys):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    output = tmp_path / "pylock.toml"
    code = cli.main(
        ["resolve", str(root), "-o", str(output), "--manager", "pip",
         "--find-links", str(links), "--no-index"]
    )
    assert code == 0
    assert output.is_file()
    assert "1 packages" in capsys.readouterr().out


def test_resolve_reports_errors(tmp_path, capsys):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n", encoding="utf-8")
    code = cli.main(["resolve", str(requirements), "--manager", "pip", "--extra", "x"])
    assert code == 1
    assert "wheelpack:" in capsys.readouterr().err


def test_retrieve_downloads_wheels(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    wheel = build_wheel(links, "demo")
    pylock = tmp_path / "pylock.toml"
    assert cli.main(
        ["resolve", str(root), "-o", str(pylock), "--manager", "pip",
         "--find-links", str(links), "--no-index"]
    ) == 0
    wheelhouse = tmp_path / "wheelhouse"
    assert cli.main(["retrieve", str(pylock), "-o", str(wheelhouse)]) == 0
    assert (wheelhouse / wheel.name).is_file()


def test_pack_writes_bundle(tmp_path):
    root = write_project(tmp_path)
    links = tmp_path / "links"
    build_wheel(links, "demo")
    archive = tmp_path / "bundle.zip"
    code = cli.main(
        ["pack", str(root), "-o", str(archive), "--manager", "pip",
         "--find-links", str(links), "--no-index"]
    )
    assert code == 0
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
    assert manifest["target"] is None
    assert manifest["wheels"][0]["name"] == "demo"


def test_pack_target_command(tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo", tag="cp312-cp312-win_amd64", purelib=False)
    build_wheel(links, "demo", tag="cp312-cp312-manylinux_2_17_x86_64", purelib=False)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        f"--no-index\n--find-links {links.as_uri()}\ndemo==1\n", encoding="utf-8"
    )
    archive = tmp_path / "bundle.zip"
    code = cli.main(
        ["pack", str(requirements), "-o", str(archive), "--manager", "pip",
         "--find-links", str(links), "--no-index",
         "--python-version", "3.12", "--python-platform", "x86_64-pc-windows-msvc",
         "--abi", "cp312"]
    )
    assert code == 0
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
    assert manifest["target"]["python_platform"] == "x86_64-pc-windows-msvc"
    assert manifest["wheels"][0]["filename"] == "demo-1-cp312-cp312-win_amd64.whl"


def test_retrieve_missing_pylock(tmp_path, capsys):
    code = cli.main(["retrieve", str(tmp_path / "absent.toml"), "-o", str(tmp_path / "wh")])
    assert code == 1
    assert "wheelpack:" in capsys.readouterr().err


def test_unknown_manager_exits():
    with pytest.raises(SystemExit):
        cli.main(["resolve", "x", "--manager", "conda"])
