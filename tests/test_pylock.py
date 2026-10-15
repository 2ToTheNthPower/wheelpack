"""Unit tests for PEP 751 plan generation, selection, and source handling."""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest
from helpers import build_wheel, digest, write_project
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from wheelpack import pylock, sources
from wheelpack._compat import tomllib

DIGEST = "a" * 64


def artifact(url="https://repo.invalid/demo-1-py3-none-any.whl", sha256=DIGEST):
    return {"url": url, "hashes": {"sha256": sha256}}


def plan(packages=None, **fields):
    return {
        "lock-version": "1.0",
        "created-by": "uv",
        "packages": [{"name": "demo", "version": "1", "wheels": [artifact()]}]
        if packages is None
        else packages,
        **fields,
    }


def exported(data):
    return "\n".join(f'"{key}" = {pylock._toml_value(value)}' for key, value in data.items())


def pip_report(entries=None):
    return {
        "version": "1",
        "install": [
            {
                "metadata": {"name": "demo", "version": "1", "requires_python": ">=3.11"},
                "download_info": {
                    "url": artifact()["url"],
                    "archive_info": {"hashes": {"sha256": DIGEST}},
                },
                "is_direct": False,
            }
        ]
        if entries is None
        else entries,
    }


# --- plan selection: host machine, architecture, and Python version ----------


@pytest.mark.parametrize(
    "sys_platform,machine,expected",
    [
        ("win32", "AMD64", {"win"}),
        ("linux", "x86_64", {"nix"}),
        ("darwin", "arm64", {"mac"}),
    ],
)
def test_selected_matches_host_machine(monkeypatch, sys_platform, machine, expected):
    environment = {
        **default_environment(),
        "sys_platform": sys_platform,
        "platform_machine": machine,
    }
    packages = [
        {"name": "win", "marker": "sys_platform == 'win32'"},
        {"name": "nix", "marker": "sys_platform == 'linux'"},
        {"name": "mac", "marker": "sys_platform == 'darwin'"},
        {"name": "always"},
    ]
    monkeypatch.setattr(pylock, "default_environment", lambda: environment)
    selected = {package["name"] for package in pylock._selected(plan(packages), [], [])}
    assert selected == expected | {"always"}


def test_selected_matches_host_python_version(monkeypatch):
    environment = {
        **default_environment(),
        "python_full_version": "3.11.9",
        "python_version": "3.11",
    }
    monkeypatch.setattr(pylock, "default_environment", lambda: environment)
    compatible = [
        {"name": "old", "requires-python": ">=3.10"},
        {"name": "exact", "requires-python": "==3.11.9"},
    ]
    selected = {package["name"] for package in pylock._selected(plan(compatible), [], [])}
    assert selected == {"old", "exact"}
    with pytest.raises(ValueError, match="different Python"):
        pylock._selected(plan([{"name": "new", "requires-python": ">=3.12"}]), [], [])


def test_selected_enforces_plan_requires_python(monkeypatch):
    monkeypatch.setattr(
        pylock, "default_environment", lambda: {**default_environment(), "python_full_version": "3.11.0"}
    )
    with pytest.raises(ValueError, match="different Python"):
        pylock._selected(plan(**{"requires-python": ">=3.12"}), [], [])


@pytest.mark.parametrize(
    "environments,match",
    [
        (["sys_platform == 'wrong'"], "current host"),
        (["sys_platform == 'linux'"], None),
    ],
)
def test_selected_enforces_plan_environments(monkeypatch, environments, match):
    environment = {**default_environment(), "sys_platform": "linux", "python_full_version": "3.12.0"}
    monkeypatch.setattr(pylock, "default_environment", lambda: environment)
    if match:
        with pytest.raises(ValueError, match=match):
            pylock._selected(plan(environments=environments), [], [])
    else:
        assert len(pylock._selected(plan(environments=environments), [], [])) == 1


def test_selected_extras_and_groups():
    packages = [
        {"name": "demo", "marker": "'feature' in extras"},
        {"name": "group", "marker": "'test' in dependency_groups"},
    ]
    assert {p["name"] for p in pylock._selected(plan(packages), ["feature"], ["test"])} == {
        "demo",
        "group",
    }
    assert pylock._selected(plan(packages), [], []) == []


def test_selected_default_groups():
    packages = [{"name": "demo", "marker": "'dev' in dependency_groups"}]
    assert pylock._selected(plan(packages, **{"default-groups": ["dev"]}), [], []) != []


def test_selected_rejects_duplicate_normalized_names():
    with pytest.raises(ValueError, match="Ambiguous"):
        pylock._selected(plan([{"name": "Demo_Name"}, {"name": "demo-name"}]), [], [])


# --- pylock.toml writing ----------------------------------------------------


def test_write_plan_round_trips_and_is_private(tmp_path):
    out = tmp_path / "resolved.pylock.toml"
    pylock._write_plan(plan(), out, tmp_path)
    data = tomllib.loads(out.read_text(encoding="utf-8"))
    assert data["lock-version"] == "1.0"
    assert data["created-by"] == "uv"
    assert data["packages"][0]["name"] == "demo"
    assert data["packages"][0]["wheels"][0]["url"] == artifact()["url"]
    if os.name != "nt":
        assert out.stat().st_mode & 0o777 == 0o600


def test_write_plan_serializes_target_constraints(tmp_path):
    target_plan = plan(
        **{
            "requires-python": ">=3.12",
            "environments": ["sys_platform == 'win32' and platform_machine == 'AMD64'"],
        }
    )
    out = tmp_path / "resolved.pylock.toml"
    pylock._write_plan(target_plan, out, tmp_path)
    data = tomllib.loads(out.read_text(encoding="utf-8"))
    assert data["requires-python"] == ">=3.12"
    assert data["environments"] == ["sys_platform == 'win32' and platform_machine == 'AMD64'"]


def test_write_plan_empty_packages(tmp_path):
    out = tmp_path / "resolved.pylock.toml"
    pylock._write_plan({"lock-version": "1.0", "packages": []}, out, tmp_path)
    assert tomllib.loads(out.read_text(encoding="utf-8"))["packages"] == []


def test_write_plan_fills_local_hashes_and_absolute_paths(tmp_path):
    wheel = build_wheel(tmp_path / "links", "demo")
    package = {
        "name": "demo",
        "version": "1",
        "wheels": [{"path": "links/demo-1-py3-none-any.whl", "hashes": {}}],
    }
    out = tmp_path / "resolved.pylock.toml"
    pylock._write_plan({"lock-version": "1.0", "packages": [package]}, out, tmp_path)
    written = tomllib.loads(out.read_text(encoding="utf-8"))["packages"][0]["wheels"][0]
    assert written["path"] == str(wheel)
    assert written["hashes"]["sha256"] == digest(wheel)


def test_write_plan_disables_editable_and_keeps_subdirectory(tmp_path):
    packages = [
        {"name": "local", "directory": {"path": "local", "editable": True, "subdirectory": "src"}},
        {
            "name": "gitdemo",
            "vcs": {
                "type": "git",
                "url": "https://repo/gitdemo",
                "commit-id": "f" * 40,
                "subdirectory": "pkg",
            },
        },
    ]
    out = tmp_path / "resolved.pylock.toml"
    pylock._write_plan({"lock-version": "1.0", "packages": packages}, out, tmp_path)
    data = tomllib.loads(out.read_text(encoding="utf-8"))
    assert data["packages"][0]["directory"]["editable"] is False
    assert data["packages"][0]["directory"]["subdirectory"] == "src"
    assert data["packages"][1]["vcs"]["subdirectory"] == "pkg"


def test_write_plan_never_overwrites_existing_file(tmp_path):
    out = tmp_path / "resolved.pylock.toml"
    out.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        pylock._write_plan(plan(), out, tmp_path)
    assert out.read_text(encoding="utf-8") == "existing"


# --- native lock parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "text", ['lock-version = "2.0"\npackages = []', "invalid TOML", 'lock-version = "1.0"\n']
)
def test_pylock_validation(text):
    with pytest.raises(ValueError, match="pylock"):
        pylock._pylock(text)


def test_pylock_accepts_valid_plan():
    assert pylock._pylock(exported(plan()))["packages"][0]["name"] == "demo"


@pytest.mark.parametrize("kind", ["sdist", "archive", "directory", "vcs"])
def test_pip_report_source_types(tmp_path, kind):
    data = pip_report()
    entry = data["install"][0]
    entry["is_direct"] = kind != "sdist"
    info = entry["download_info"]
    info["url"] = "https://repo.invalid/demo.tar.gz"
    info["subdirectory"] = "pkg"
    if kind == "directory":
        info["url"] = (tmp_path / "local space").as_uri()
        info["dir_info"] = {"editable": True}
    elif kind == "vcs":
        info["vcs_info"] = {"vcs": "git", "commit_id": "f" * 40, "requested_revision": "main"}
    package = pylock._pip_report(data)["packages"][0]
    source = package[kind]
    assert source["subdirectory"] == "pkg"
    if kind == "directory":
        assert source["path"] == str(tmp_path / "local space")
        assert source["editable"] is True
    elif kind == "vcs":
        assert source["commit-id"] == "f" * 40
        assert source["requested-revision"] == "main"
    else:
        assert source["hashes"] == {"sha256": DIGEST}


def test_pip_report_wheel_and_legacy_hash():
    package = pylock._pip_report(pip_report())["packages"][0]
    assert package["wheels"][0] == artifact()
    assert package["requires-python"] == ">=3.11"
    data = pip_report()
    data["install"][0]["download_info"]["archive_info"] = {"hash": "sha256=" + DIGEST}
    assert pylock._pip_report(data)["packages"][0]["wheels"][0] == artifact()


def test_pip_report_missing_sha256_and_vcs_commit():
    data = pip_report()
    data["install"][0]["download_info"]["archive_info"]["hashes"] = {"sha512": "b" * 128}
    with pytest.raises(ValueError, match="SHA256"):
        pylock._pip_report(data)
    data = pip_report()
    data["install"][0]["download_info"]["vcs_info"] = {"vcs": "git", "requested_revision": "main"}
    with pytest.raises(ValueError, match="locked VCS commit"):
        pylock._pip_report(data)


@pytest.mark.parametrize("invalid", [{}, {"version": "2", "install": []}, {"version": "1"}, {"version": "1", "install": "x"}])
def test_pip_report_invalid(invalid):
    with pytest.raises(ValueError, match="installation report"):
        pylock._pip_report(invalid)


# --- poetry export parsing --------------------------------------------------


def test_exports_skips_source_flags_and_hashes():
    text = (
        "--index-url https://repo.invalid/simple\n"
        "# a comment\n"
        f"demo==1 --hash=sha256:{DIGEST}\n"
        "other==2 \\\n    --hash=sha256:" + "b" * 64 + "\n"
    )
    parsed = pylock._exports(text, [])
    assert [str(requirement) for _, requirement in parsed] == ["demo==1", "other==2"]


def test_exports_filters_host_markers():
    text = 'win==1; sys_platform == "win32"\nnix==1; sys_platform == "linux"\n'
    parsed = pylock._exports(text, [])
    names = {canonicalize_name(requirement.name) for _, requirement in parsed}
    expected = {"win"} if sys.platform == "win32" else {"nix"} if sys.platform.startswith("linux") else set()
    assert names == expected


def test_exports_local_lines_and_invalid():
    parsed = pylock._exports("-e ./local\n./direct\n", [])
    assert parsed == [("./local", None), ("./direct", None)]
    with pytest.raises(ValueError, match="Invalid requirement"):
        pylock._exports("this is not a requirement", [])


def test_package_matches_lock_entry():
    lock = {"package": [{"name": "Demo", "version": "1"}]}
    assert pylock._package(lock, Requirement("demo==1"))["name"] == "Demo"
    with pytest.raises(ValueError, match="pin"):
        pylock._package(lock, Requirement("demo>=1"))
    with pytest.raises(ValueError, match="unambiguously"):
        pylock._package({"package": []}, Requirement("demo==1"))


# --- manager detection ------------------------------------------------------


def test_detect_auto_selects_uv(tmp_path):
    root = write_project(tmp_path)
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    source = pylock.detect(root, "auto")
    assert (source.manager, source.is_project) == ("uv", True)


def test_detect_auto_selects_poetry(tmp_path):
    root = write_project(tmp_path)
    (root / "poetry.lock").write_text("", encoding="utf-8")
    assert pylock.detect(root, "auto").manager == "poetry"


def test_detect_auto_selects_pip_for_plain_project(tmp_path):
    assert pylock.detect(write_project(tmp_path), "auto").manager == "pip"


def test_detect_directory_prefers_pyproject_then_requirements(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "requirements.txt").write_text("demo\n", encoding="utf-8")
    assert pylock.detect(root, "auto").input.name == "requirements.txt"
    (root / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="1"\n', encoding="utf-8"
    )
    assert pylock.detect(root, "auto").input.name == "pyproject.toml"


@pytest.mark.parametrize("manager", ["uv", "poetry"])
def test_detect_requires_project_for_lock_managers(tmp_path, manager):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pyproject.toml"):
        pylock.detect(requirements, manager)


def test_detect_unknown_manager(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown manager"):
        pylock.detect(requirements, "conda")


def test_detect_missing_input(tmp_path):
    with pytest.raises(ValueError, match="Input must be"):
        pylock.detect(tmp_path / "missing.txt", "auto")


# --- index and source option assembly ---------------------------------------


def test_options_local_flat_dirs_and_remote_indexes(tmp_path):
    options = sources._options(
        ["https://primary.invalid/simple", "https://extra.invalid/simple"],
        [str(tmp_path)],
        False,
    )
    assert options == [
        "--index-url",
        "https://primary.invalid/simple",
        "--extra-index-url",
        "https://extra.invalid/simple",
        "--find-links",
        str(tmp_path),
    ]


def test_options_no_index_disables_registries_but_keeps_links(tmp_path):
    assert sources._options(["https://primary.invalid/simple"], [str(tmp_path)], True) == [
        "--no-index",
        "--find-links",
        str(tmp_path),
    ]


@pytest.mark.parametrize("no_index", [False, True])
def test_uv_options_exclude_explicit_indexes(tmp_path, no_index):
    config = {
        "index": [
            {"name": "private", "url": "https://private.invalid", "explicit": True},
            {"name": "mirror", "url": "https://mirror.invalid", "default": True},
            {"url": "https://secondary.invalid"},
        ],
        "no-index": no_index,
        "find-links": ["links"],
    }
    with mock.patch.dict(
        os.environ, {"UV_INDEX_MIRROR_USERNAME": "user", "UV_INDEX_MIRROR_PASSWORD": "secret"}
    ):
        options = sources._uv_options(config, [], [], False, tmp_path)
    assert "https://private.invalid" not in options
    assert options[-2:] == ["--find-links", str(tmp_path / "links")]
    if no_index:
        assert options == ["--no-index", "--find-links", str(tmp_path / "links")]
    else:
        assert options[:4] == [
            "--index-url",
            "https://user:secret@mirror.invalid",
            "--extra-index-url",
            "https://secondary.invalid",
        ]
        assert "https://pypi.org/simple" not in options


def test_uv_options_rejects_multiple_default_indexes(tmp_path):
    config = {
        "index": [
            {"url": "https://a.invalid", "default": True},
            {"url": "https://b.invalid", "default": True},
        ]
    }
    with pytest.raises(ValueError, match="multiple default"):
        sources._uv_options(config, [], [], False, tmp_path)


def test_uv_root_detects_workspace_member(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="root"\nversion="1"\n[tool.uv.workspace]\nmembers=["packages/*"]\n',
        encoding="utf-8",
    )
    member = root / "packages" / "member"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname="member"\nversion="1"\n')
    assert sources._uv_root(member) == root


def test_uv_options_resolve_relative_find_links(tmp_path):
    assert sources._uv_options({"find-links": ["wheel space"]}, [], [], True, tmp_path) == [
        "--no-index",
        "--find-links",
        str(tmp_path / "wheel space"),
    ]


def test_pip_credentials_reads_nested_includes(tmp_path):
    folder = tmp_path / "nested space"
    folder.mkdir()
    nested = folder / "requirements.txt"
    nested.write_text(
        "--index-url https://user:secret@repo.invalid/simple\ndemo==1\n", encoding="utf-8"
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f'-r "{nested}"\n', encoding="utf-8")
    with mock.patch.object(sources, "_run", return_value=""):
        credentials = sources._pip_credentials(requirements, tmp_path, [])
    assert credentials == ["https://user:secret@repo.invalid/simple"]


# --- credentials and origin safety ------------------------------------------


@pytest.mark.parametrize(
    "target,allowed",
    [
        ("https://repo.invalid/pkg", True),
        ("https://REPO.invalid:443/pkg", True),
        ("http://repo.invalid/pkg", False),
        ("https://repo.invalid:8443/pkg", False),
        ("https://cdn.invalid/pkg", False),
        ("file:///pkg", False),
    ],
)
def test_credential_transfer_requires_same_origin(target, allowed):
    source = "https://user:secret@repo.invalid/simple"
    assert sources._same_origin(source, target) is allowed
    result = sources._pip_auth(target, [source])
    assert ("user:secret@" in result) is allowed
    if not allowed:
        assert result == target


def test_credentials_injected_for_named_index():
    with mock.patch.dict(
        os.environ,
        {"UV_INDEX_PRIVATE_USERNAME": "user", "UV_INDEX_PRIVATE_PASSWORD": "secret"},
    ):
        result = sources._credentials("https://repo.invalid/simple", "private", "uv")
    assert result == "https://user:secret@repo.invalid/simple"
    with mock.patch.dict(
        os.environ,
        {"POETRY_HTTP_BASIC_PRIVATE_USERNAME": "user", "POETRY_HTTP_BASIC_PRIVATE_PASSWORD": "secret"},
    ):
        result = sources._credentials("https://repo.invalid/simple", "private", "poetry")
    assert result == "https://user:secret@repo.invalid/simple"


def test_credentials_ignore_non_http_and_missing_names():
    assert sources._credentials("file:///pkg", "private", "uv") == "file:///pkg"
    assert sources._credentials("https://repo.invalid", None, "uv") == "https://repo.invalid"


def test_artifact_appends_subdirectory_fragment(tmp_path):
    package = {
        "name": "demo",
        "version": "1",
        "wheels": [
            {
                "url": artifact()["url"],
                "hashes": {"sha256": DIGEST},
                "subdirectory": "pkg",
            }
        ],
    }
    url, _ = sources._artifact(package, tmp_path)
    assert url.endswith("#subdirectory=pkg")


def test_artifact_rejects_missing_hash(tmp_path):
    package = {"name": "demo", "version": "1", "wheels": [{"url": artifact()["url"], "hashes": {}}]}
    with pytest.raises(ValueError, match="SHA256"):
        sources._artifact(package, tmp_path)


def test_artifact_local_path_becomes_file_url(tmp_path):
    wheel = build_wheel(tmp_path / "links", "demo")
    package = {
        "name": "demo",
        "version": "1",
        "wheels": [{"path": str(wheel), "hashes": {"sha256": digest(wheel)}}],
    }
    url, sha = sources._artifact(package, tmp_path)
    assert url == wheel.as_uri()
    assert sha == f"sha256:{digest(wheel)}"


def test_artifact_preserves_sdist_fallback(tmp_path):
    package = {
        "name": "demo",
        "version": "1",
        "sdist": artifact("https://repo.invalid/demo-1.tar.gz"),
    }
    url, _ = sources._artifact(package, tmp_path)
    assert url.endswith("demo-1.tar.gz")


def test_uv_build_no_longer_present():
    assert not hasattr(sources, "_wheel")
    assert not hasattr(sources, "_uv_build")
    assert not hasattr(sources, "collect")


# --- poetry resolution (native tools mocked) --------------------------------


def test_resolve_poetry_uses_export_and_lock(tmp_path):
    root = tmp_path / "poetry"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "1"\n'
        'requires-python = ">=3.11"\ndependencies = ["demo"]\n'
        "[tool.poetry]\npackage-mode = false\n",
        encoding="utf-8",
    )
    (root / "poetry.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "1"\n'
        f'files = [{{ file = "demo-1-py3-none-any.whl", hash = "sha256:{DIGEST}" }}]\n',
        encoding="utf-8",
    )
    responses = ["", f"demo==1 --hash=sha256:{DIGEST}\n", exported(plan())]
    with mock.patch.object(sources, "_run", side_effect=responses) as run:
        resolution = pylock.resolve(
            pylock.detect(root, "poetry"),
            indexes=[],
            find_links=[],
            no_index=False,
            extras=[],
            groups=[],
        )
    assert resolution.manager == "poetry"
    assert [package["name"] for package in resolution.plan["packages"]] == ["demo"]
    assert resolution.locked_hashes["demo"] == {f"sha256:{DIGEST}"}
    assert run.call_args_list[0].args[0] == ["poetry", "check", "--lock"]
    assert run.call_args_list[1].args[0][:2] == ["poetry", "export"]
    assert run.call_args_list[2].args[0][:3] == ["uv", "pip", "compile"]


def test_resolve_poetry_requires_lock_hashes(tmp_path):
    root = tmp_path / "poetry"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "1"\n'
        'requires-python = ">=3.11"\ndependencies = ["demo"]\n'
        "[tool.poetry]\npackage-mode = false\n",
        encoding="utf-8",
    )
    (root / "poetry.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "1"\n'
        f'files = [{{ file = "demo-1-py3-none-any.whl", hash = "sha256:{DIGEST}" }}]\n',
        encoding="utf-8",
    )
    with (
        mock.patch.object(sources, "_run", side_effect=["", "demo==1\n", exported(plan())]),
        pytest.raises(ValueError, match="Missing exported lock hashes"),
    ):
        pylock.resolve(
            pylock.detect(root, "poetry"),
            indexes=[],
            find_links=[],
            no_index=False,
            extras=[],
            groups=[],
        )

