"""Retrieval from a PEP 751 pylock.toml: download, build, hashes, targets, indexes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from helpers import build_wheel, digest

from wheelpack import retrieve, sources
from wheelpack.targets import Target

DIGEST = "a" * 64


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(value)


def write_lock(path, packages, **fields):
    fields.setdefault("lock-version", "1.0")
    text = "\n".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in fields.items())
    for package in packages:
        text += "\n\n[[packages]]\n" + "\n".join(
            f"{json.dumps(k)} = {toml_value(v)}" for k, v in package.items()
        )
    path.write_text(text + "\n", encoding="utf-8")
    return path


def wheel_artifact(url, sha256=DIGEST):
    return {"url": url, "hashes": {"sha256": sha256}}


def make_local_project(root, name):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "builder.py").write_text(
        "import pathlib, zipfile\n"
        "def build_wheel(directory, config_settings=None, metadata_directory=None):\n"
        f"    wheel = pathlib.Path(directory) / {name + '-1-py3-none-any.whl'!r}\n"
        "    with zipfile.ZipFile(wheel, 'w') as archive:\n"
        f"        archive.writestr({name + '/__init__.py'!r}, '')\n"
        f"        archive.writestr({name + '-1.dist-info/METADATA'!r}, "
        f"'Metadata-Version: 2.1\\nName: {name}\\nVersion: 1\\n')\n"
        f"        archive.writestr({name + '-1.dist-info/WHEEL'!r}, "
        "'Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
        f"        archive.writestr({name + '-1.dist-info/RECORD'!r}, '')\n"
        "    return wheel.name\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "builder"\nbackend-path = ["."]\n',
        encoding="utf-8",
    )
    return root


# --- pylock loading ---------------------------------------------------------


def test_load_plan_accepts_path_and_mapping(tmp_path):
    lock = write_lock(tmp_path / "pylock.toml", [{"name": "demo", "version": "1"}])
    assert retrieve.load_plan(lock)["packages"][0]["name"] == "demo"
    assert retrieve.load_plan({"lock-version": "1.0", "packages": []})["packages"] == []


@pytest.mark.parametrize(
    "text", ['lock-version = "2.0"\npackages = []\n', "not = valid toml\n[", 'lock-version = "1.0"\n']
)
def test_load_plan_rejects_invalid(tmp_path, text):
    lock = tmp_path / "pylock.toml"
    lock.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        retrieve.load_plan(lock)


def test_load_plan_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="Cannot read pylock"):
        retrieve.load_plan(tmp_path / "absent.pylock.toml")


# --- local artifacts --------------------------------------------------------


def test_retrieve_local_wheel(tmp_path):
    wheel = build_wheel(tmp_path / "links", "demo")
    lock = write_lock(
        tmp_path / "pylock.toml",
        [{"name": "demo", "version": "1", "wheels": [wheel_artifact(wheel.as_uri(), digest(wheel))]}],
    )
    result = retrieve.retrieve(lock, tmp_path / "wheels")
    assert result == [tmp_path / "wheels" / wheel.name]
    assert result[0].read_bytes() == wheel.read_bytes()


def test_retrieve_accepts_plan_mapping(tmp_path):
    wheel = build_wheel(tmp_path / "links", "demo")
    plan = {
        "lock-version": "1.0",
        "packages": [
            {"name": "demo", "version": "1", "wheels": [wheel_artifact(wheel.as_uri(), digest(wheel))]}
        ],
    }
    result = retrieve.retrieve(plan, tmp_path / "wheels")
    assert [path.name for path in result] == [wheel.name]


def test_retrieve_hash_mismatch_fails(tmp_path):
    wheel = build_wheel(tmp_path / "links", "demo")
    lock = write_lock(
        tmp_path / "pylock.toml",
        [{"name": "demo", "version": "1", "wheels": [wheel_artifact(wheel.as_uri(), DIGEST)]}],
    )
    with pytest.raises(ValueError):
        retrieve.retrieve(lock, tmp_path / "wheels")


def test_retrieve_rejects_package_without_artifact(tmp_path):
    lock = write_lock(tmp_path / "pylock.toml", [{"name": "demo", "version": "1"}])
    with pytest.raises(ValueError, match="No compatible"):
        retrieve.retrieve(lock, tmp_path / "wheels")


def test_retrieve_rejects_package_without_name(tmp_path):
    lock = write_lock(tmp_path / "pylock.toml", [{"version": "1"}])
    with pytest.raises(retrieve.RetrieveError, match="missing a name"):
        retrieve.retrieve(lock, tmp_path / "wheels")


# --- target architecture ----------------------------------------------------


def test_retrieve_selects_wheel_for_target_architecture(tmp_path):
    package = {
        "name": "demo",
        "version": "1",
        "wheels": [
            wheel_artifact("https://repo/demo-1-cp312-cp312-win_amd64.whl"),
            wheel_artifact("https://repo/demo-1-cp312-cp312-manylinux_2_17_x86_64.whl"),
        ],
    }
    lock = write_lock(tmp_path / "pylock.toml", [package])
    references = []

    def run(args, cwd, **kwargs):
        references.append(Path(args[args.index("-r") + 1]).read_text())
        return ""

    windows = Target.create(python_version="3.12", python_platform="x86_64-pc-windows-msvc", abi="cp312")
    with mock.patch.object(sources, "_run", side_effect=run):
        retrieve.retrieve(lock, tmp_path / "wheels", target=windows)
    assert "win_amd64.whl --hash=sha256:" + DIGEST in references[0]
    assert "manylinux" not in references[0]


# --- remote indexes ---------------------------------------------------------


def test_retrieve_from_remote_index(tmp_path, index):
    wheel = build_wheel(tmp_path / "built", "demo")
    published = index.publish(wheel)
    lock = write_lock(
        tmp_path / "pylock.toml",
        [{"name": "demo", "version": "1", "wheels": [wheel_artifact(published, digest(wheel))]}],
    )
    result = retrieve.retrieve(lock, tmp_path / "wheels", indexes=[index.url])
    assert [path.name for path in result] == [wheel.name]
    assert result[0].read_bytes() == wheel.read_bytes()
    assert index.downloads


def test_retrieve_injects_same_origin_credentials(tmp_path):
    lock = write_lock(
        tmp_path / "pylock.toml",
        [
            {
                "name": "demo",
                "version": "1",
                "wheels": [wheel_artifact("https://repo.invalid/demo-1-py3-none-any.whl")],
            }
        ],
    )
    references = []

    def run(args, cwd, **kwargs):
        references.append(Path(args[args.index("-r") + 1]).read_text())
        return ""

    with mock.patch.object(sources, "_run", side_effect=run):
        retrieve.retrieve(
            lock, tmp_path / "wheels", indexes=["https://user:secret@repo.invalid/simple"]
        )
    assert "https://user:secret@repo.invalid/demo-1-py3-none-any.whl" in references[0]


def test_retrieve_does_not_leak_credentials_across_origins(tmp_path):
    lock = write_lock(
        tmp_path / "pylock.toml",
        [
            {
                "name": "demo",
                "version": "1",
                "wheels": [wheel_artifact("https://cdn.invalid/demo-1-py3-none-any.whl")],
            }
        ],
    )
    references = []

    def run(args, cwd, **kwargs):
        references.append(Path(args[args.index("-r") + 1]).read_text())
        return ""

    with mock.patch.object(sources, "_run", side_effect=run):
        retrieve.retrieve(
            lock, tmp_path / "wheels", indexes=["https://user:secret@repo.invalid/simple"]
        )
    assert "user:secret@" not in references[0]


# --- direct sources ---------------------------------------------------------


def test_retrieve_builds_directory_source(tmp_path):
    project = make_local_project(tmp_path / "local", "local")
    lock = write_lock(
        tmp_path / "pylock.toml",
        [{"name": "local", "version": "1", "directory": {"path": str(project), "editable": False}}],
    )
    result = retrieve.retrieve(lock, tmp_path / "wheels")
    assert [path.name for path in result] == ["local-1-py3-none-any.whl"]


def test_retrieve_vcs_reference_includes_subdirectory(tmp_path):
    lock = write_lock(
        tmp_path / "pylock.toml",
        [
            {
                "name": "gitdemo",
                "version": "1",
                "vcs": {
                    "type": "git",
                    "url": "https://repo/gitdemo",
                    "commit-id": "f" * 40,
                    "subdirectory": "pkg",
                },
            }
        ],
    )
    references = []

    def run(args, cwd, **kwargs):
        references.append(Path(args[args.index("-r") + 1]).read_text())
        return ""

    with mock.patch.object(sources, "_run", side_effect=run):
        retrieve.retrieve(lock, tmp_path / "wheels")
    assert references[0].strip() == (
        "gitdemo @ git+https://repo/gitdemo@" + "f" * 40 + "#subdirectory=pkg"
    )


def test_retrieve_rejects_vcs_without_commit(tmp_path):
    lock = write_lock(
        tmp_path / "pylock.toml",
        [{"name": "gitdemo", "version": "1", "vcs": {"type": "git", "url": "https://repo/gitdemo"}}],
    )
    with pytest.raises(retrieve.RetrieveError, match="type/commit"):
        retrieve.retrieve(lock, tmp_path / "wheels")


def test_retrieve_rejects_directory_without_path(tmp_path):
    lock = write_lock(
        tmp_path / "pylock.toml",
        [{"name": "local", "version": "1", "directory": {"editable": False}}],
    )
    with pytest.raises(retrieve.RetrieveError, match="missing a path"):
        retrieve.retrieve(lock, tmp_path / "wheels")


def test_retrieve_resolves_relative_directory_path(tmp_path):
    make_local_project(tmp_path / "local", "local")
    lock = write_lock(
        tmp_path / "pylock.toml",
        [{"name": "local", "version": "1", "directory": {"path": "local", "editable": False}}],
    )
    result = retrieve.retrieve(lock, tmp_path / "wheels")
    assert [path.name for path in result] == ["local-1-py3-none-any.whl"]


def test_retrieve_creates_destination(tmp_path):
    wheel = build_wheel(tmp_path / "links", "demo")
    lock = write_lock(
        tmp_path / "pylock.toml",
        [{"name": "demo", "version": "1", "wheels": [wheel_artifact(wheel.as_uri(), digest(wheel))]}],
    )
    destination = tmp_path / "nested" / "wheels"
    assert not destination.exists()
    retrieve.retrieve(lock, destination)
    assert destination.is_dir()
