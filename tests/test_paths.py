"""Local sources use the host filesystem flavor, not URL or shell path rules."""

import hashlib
import os
import tomllib
from pathlib import Path, PureWindowsPath
from urllib.request import url2pathname

import pytest
from packaging.tags import Tag

from wheelport import sources, targeted
from wheelport.targets import Target


def target():
    return Target.create(python_version="3.12", python_platform="windows")


@pytest.mark.parametrize("authority", ["", "localhost"])
def test_file_url_decoded_once(tmp_path, authority):
    path = tmp_path / "space + literal%20 and%25" / "demo-1-py3-none-any.whl"
    url = path.as_uri().replace("file://", f"file://{authority}", 1)
    assert sources._local_path(url, tmp_path) == path
    assert sources._local_path(Path(path), tmp_path) == path


def test_file_url_uses_native_converter(tmp_path, monkeypatch):
    calls = []

    def convert(path):
        calls.append(path)
        return str(tmp_path / "native drive" / "literal%20.whl")

    monkeypatch.setattr(sources, "url2pathname", convert)
    assert sources._local_path("file:///C:/native%20drive/literal%2520.whl", tmp_path) == (
        tmp_path / "native drive" / "literal%20.whl"
    )
    assert calls == ["/C:/native%20drive/literal%2520.whl"]


@pytest.mark.parametrize("url", ["file://remote/share/demo.whl", "file://user@localhost/demo.whl"])
def test_remote_file_authority_rejected(tmp_path, url):
    with pytest.raises(ValueError, match="local file"):
        sources._local_path(url, tmp_path)


def test_windows_artifact_basename(monkeypatch):
    monkeypatch.setattr(sources, "Path", PureWindowsPath)
    assert sources._artifact_name({"path": r"C:\wheel folder\demo-1-py3-none-any.whl"}) == (
        "demo-1-py3-none-any.whl"
    )
    assert sources._artifact_name({"path": r"wheels\demo-1-py3-none-any.whl"}) == (
        "demo-1-py3-none-any.whl"
    )


@pytest.mark.parametrize(
    "line,expected",
    [
        (r'-r "C:\requirements folder\nested.txt"', ["-r", r"C:\requirements folder\nested.txt"]),
        (r"--find-links=C:\wheels", [r"--find-links=C:\wheels"]),
        (r'--cert "cert folder\ca.pem"', ["--cert", r"cert folder\ca.pem"]),
        ("-r 'nested space.txt'", ["-r", "nested space.txt"]),
    ],
)
def test_requirements_tokens_preserve_backslashes(line, expected):
    assert sources._tokens(line) == expected


@pytest.mark.parametrize("field", ["path", "url"])
def test_local_artifact_hash_and_plan_roundtrip(tmp_path, field):
    path = tmp_path / "wheel space + %20"
    path.mkdir()
    wheel = path / "demo-1-py3-none-any.whl"
    wheel.write_bytes(b"wheel content")
    artifact = {field: str(wheel.relative_to(tmp_path)) if field == "path" else wheel.as_uri()}
    package = {"name": "demo", "version": "1", "wheels": [artifact]}
    targeted._local_hash(artifact, tmp_path)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert artifact["hashes"] == {"sha256": digest}
    # Native normalization also fills hashes emitted empty by uv.
    artifact.pop("hashes")
    resolution = tmp_path / "resolved.pylock.toml"
    sources._write_plan({"lock-version": "1.0", "packages": [package]}, resolution, tmp_path)
    package = tomllib.loads(resolution.read_text())["packages"][0]
    assert sources._artifact(
        package, tmp_path, tags=[Tag("py3", "none", "any")], wheel_only=True
    ) == (wheel.as_uri(), f"sha256:{digest}")


@pytest.mark.parametrize("file_url", [False, True])
def test_nested_requirements_paths_and_credentials(tmp_path, monkeypatch, file_url):
    folder = tmp_path / "nested space + %20"
    folder.mkdir()
    nested = folder / "requirements.txt"
    nested.write_text("--index-url https://user:secret@repo.invalid/simple\ndemo==1\n")
    requirements = tmp_path / "requirements.txt"
    location = nested.as_uri() if file_url else str(nested)
    requirements.write_text(f'-r "{location}"\n')
    monkeypatch.setattr(sources, "_run", lambda *args, **kwargs: "")
    assert sources._pip_credentials(requirements, tmp_path, []) == [
        "https://user:secret@repo.invalid/simple"
    ]
    selected, constraints, options, _, _ = targeted._requirements(requirements, target())
    assert selected == ["demo==1"]
    assert constraints == []
    assert options == ["--index-url", "https://user:secret@repo.invalid/simple"]


def test_source_options_resolve_from_included_file(tmp_path):
    folder = tmp_path / "nested space"
    folder.mkdir()
    nested = folder / "requirements.txt"
    nested.write_text('--find-links "wheel space"\n--cert "cert space/ca.pem"\ndemo==1\n')
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"-r {nested.as_uri()}\n")
    assert targeted._requirements(requirements, target())[2] == [
        "--find-links",
        str(folder / "wheel space"),
        "--cert",
        str(folder / "cert space" / "ca.pem"),
    ]
    assert sources._uv_options({"find-links": ["wheel space"]}, [], [], True, folder) == [
        "--no-index",
        "--find-links",
        str(folder / "wheel space"),
    ]


@pytest.mark.parametrize("file_url", [False, True])
def test_bare_direct_wheel_path(tmp_path, file_url):
    path = tmp_path / "wheel space + %20" / "demo-1-py3-none-any.whl"
    location = path.as_uri() if file_url else str(path)
    requirement = targeted._requirement(location, tmp_path, target(), [])
    assert requirement.name == "demo"
    assert requirement.url == path.as_uri()


@pytest.mark.parametrize("location", ["demo-1-py3-none-any.whl", "./demo-1-py3-none-any.whl"])
def test_bare_relative_wheel_filename(tmp_path, location):
    requirement = targeted._requirement(location, tmp_path, target(), [])
    assert requirement.name == "demo"
    assert requirement.url == (tmp_path / "demo-1-py3-none-any.whl").as_uri()


def test_bare_file_url_preserves_hash_fragment(tmp_path):
    url = (tmp_path / "demo-1-py3-none-any.whl").as_uri() + "#sha256=" + "a" * 64
    requirement = targeted._requirement(url, tmp_path, target(), [])
    assert requirement.url == url
    assert targeted._hashes(url, requirement) == {"sha256:" + "a" * 64}


@pytest.mark.skipif(os.name != "nt", reason="native Windows drive paths")
def test_native_windows_drive_and_relative_paths(tmp_path):
    path = tmp_path / "wheel space" / "demo-1-py3-none-any.whl"
    assert path.drive
    assert "\\" in str(path)
    assert sources._local_path(str(path), tmp_path) == path
    assert sources._local_path(path.as_uri(), tmp_path) == path
    assert Path(url2pathname("/C:/wheel%20space/demo.whl")) == Path(r"C:\wheel space\demo.whl")
    assert sources._local_path(r"wheel space\demo-1-py3-none-any.whl", tmp_path) == path
