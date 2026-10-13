"""Default solver dependencies work without their console scripts on PATH."""

import os
import shutil
import sys
import tomllib
from importlib.metadata import requires, version
from pathlib import Path
from unittest import mock

from packaging.requirements import Requirement

from wheelport.sources import _run


def test_default_tool_metadata():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    expected = {"uv": "==0.12.5", "poetry": ">=2", "poetry-plugin-export": ">=1.9"}
    for dependencies in (project["project"]["dependencies"], requires("wheelport")):
        runtime = {req.name: str(req.specifier) for req in map(Requirement, dependencies)}
        assert runtime.items() >= expected.items()
    assert {Requirement(req).name for req in project["dependency-groups"]["dev"]} == {
        "pytest",
        "ruff",
    }
    assert project["tool"]["uv"]["required-version"] == "==0.12.5"


def test_genuine_uv_module(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="default-tools"\nversion="1"\n'
        'requires-python=">=3.11"\ndependencies=[]\n'
        "[tool.uv]\npackage=false\n"
    )
    with mock.patch.dict(os.environ, {"PATH": str(tmp_path)}):
        assert shutil.which("uv") is None
        assert shutil.which("poetry") is None
        assert _run(["uv", "--version"], tmp_path).startswith("uv 0.12.5")
        _run(["uv", "lock", "--offline", "--python", sys.executable], tmp_path)
        before = (tmp_path / "uv.lock").read_bytes()
        text = _run(
            [
                "uv",
                "export",
                "--locked",
                "--offline",
                "--format",
                "pylock.toml",
                "--no-dev",
                "--no-emit-project",
                "--python",
                sys.executable,
            ],
            tmp_path,
        )
    assert tomllib.loads(text)["packages"] == []
    assert (tmp_path / "uv.lock").read_bytes() == before


def test_genuine_poetry_module(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="default-tools"\nversion="1"\n'
        'requires-python=">=3.11"\ndependencies=[]\n'
        "[tool.poetry]\npackage-mode=false\n"
    )
    with mock.patch.dict(os.environ, {"PATH": str(tmp_path), "POETRY_KEYRING_ENABLED": "false"}):
        assert shutil.which("uv") is None
        assert shutil.which("poetry") is None
        assert version("poetry") in _run(["poetry", "--version"], tmp_path)
        _run(["poetry", "lock"], tmp_path)
        before = (tmp_path / "poetry.lock").read_bytes()
        _run(["poetry", "check", "--lock"], tmp_path)
        assert not _run(["poetry", "export", "--format", "requirements.txt"], tmp_path).strip()
    assert (tmp_path / "poetry.lock").read_bytes() == before
