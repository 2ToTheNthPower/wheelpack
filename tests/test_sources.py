"""Host-only source normalization, lock fidelity, and credential handling."""

import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import tomllib
import zipfile
from pathlib import Path
from unittest import mock

import pytest
from packaging.tags import Tag

from wheelport import sources

DIGEST = "a" * 64


@pytest.mark.parametrize(
    "target,allowed",
    [
        ("https://repo.invalid/pkg", True),
        ("https://REPO.invalid:443/pkg", True),
        ("http://repo.invalid/pkg", False),
        ("http://repo.invalid:443/pkg", False),
        ("https://repo.invalid:8443/pkg", False),
        ("https://cdn.invalid/pkg", False),
        ("file:///pkg", False),
        ("https://repo.invalid:invalid/pkg", False),
    ],
)
def test_credential_transfer_requires_same_origin(target, allowed):
    source = "https://user:secret@repo.invalid/simple"
    assert sources._same_origin(source, target) is allowed
    result = sources._pip_auth(target, [source])
    assert ("user:secret@" in result) is allowed
    if not allowed:
        assert result == target
    assert sources._same_origin("http://repo.invalid:80", "http://repo.invalid")


def artifact(url="https://repo.invalid/demo-1-py3-none-any.whl", digest=DIGEST):
    return {"url": url, "hashes": {"sha256": digest}}


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
    return "\n".join(f'"{key}" = {sources._toml_value(value)}' for key, value in data.items())


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


def report_runner(data, calls, config=""):
    def run(args, cwd, **kwargs):
        calls.append(args)
        if "--report" in args:
            report = Path(args[args.index("--report") + 1])
            if os.name != "nt":
                assert report.parent.stat().st_mode & 0o777 == 0o700
            report.write_text(json.dumps(data))
            return "pip output is not the report"
        if args[3:] == ["config", "list"]:
            return config
        raise AssertionError(args)

    return run


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "1"\nrequires-python = ">=3.11"\n'
        'dependencies = ["demo"]\n'
    )
    return root


def collect(input, tmp_path, manager="pip", **kwargs):
    return sources.collect(
        input,
        manager,
        tmp_path / "work" / "wheels",
        **{
            "indexes": [],
            "find_links": [],
            "no_index": False,
            "extras": [],
            "groups": [],
            **kwargs,
        },
    )


def test_pip_normalizes_before_exact_retrieval(project, tmp_path):
    with (project / "pyproject.toml").open("a") as stream:
        stream.write('[build-system]\nrequires=[]\nbuild-backend="builder"\n')
    calls = []

    def run(args, cwd, **kwargs):
        calls.append(args)
        if args[:3] == ["uv", "pip", "compile"]:
            assert "--index-strategy" in args
            assert "unsafe-best-match" in args
            assert args[args.index("--python") + 1] == sys.executable
            return exported(plan())
        resolution = tmp_path / "work" / "resolved.pylock.toml"
        assert tomllib.loads(resolution.read_text())["lock-version"] == "1.0"
        if os.name != "nt":
            assert resolution.stat().st_mode & 0o777 == 0o600
        assert "--no-deps" in args
        assert "--require-hashes" in args if len(calls) == 2 else True
        return ""

    with mock.patch.object(sources, "_run", side_effect=run):
        assert collect(project, tmp_path, extras=["feature"], groups=["test"]) == "pip"
    assert calls[0][-4:] == ["--extra", "feature", "--group", "test"]
    assert len(calls) == 3  # Dependencies, then the root project.
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


def test_requirements_native_options_not_rewritten(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r nested.txt\n--extra-index-url https://other.invalid/simple\n")
    calls = []
    with mock.patch.object(sources, "_run", side_effect=report_runner(pip_report([]), calls)):
        collect(requirements, tmp_path, indexes=["https://cli.invalid/simple"], no_index=True)
    assert calls[0][:4] == [sys.executable, "-m", "pip", "install"]
    assert all(flag in calls[0] for flag in ("--dry-run", "--ignore-installed", "--no-index"))
    assert calls[0][-2:] == ["-r", str(requirements)]
    assert "--index-url" not in calls[0]
    assert len(calls) == 2
    assert not Path(calls[0][calls[0].index("--report") + 1]).parent.exists()


def test_requirements_report_normalized_before_retrieval(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo==1\n")
    calls = []

    def wheel(*args, **kwargs):
        resolution = tmp_path / "work" / "resolved.pylock.toml"
        data = tomllib.loads(resolution.read_text())
        assert data["packages"][0]["requires-python"] == ">=3.11"
        assert data["packages"][0]["wheels"] == [artifact()]
        if os.name != "nt":
            assert resolution.stat().st_mode & 0o777 == 0o600
        assert kwargs == {"locked": True, "hashed": True}

    with (
        mock.patch.object(sources, "_run", side_effect=report_runner(pip_report(), calls)),
        mock.patch.object(sources, "_wheel", side_effect=wheel) as retrieve,
    ):
        collect(requirements, tmp_path)
    retrieve.assert_called_once()
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


@pytest.mark.parametrize("local", [False, True])
def test_pip_report_missing_sha256_rejected_before_retrieval(tmp_path, local):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n")
    data = pip_report()
    info = data["install"][0]["download_info"]
    info["archive_info"]["hashes"] = {"sha512": "b" * 128}
    if local:
        info["url"] = make_wheel(tmp_path / "links").as_uri()
    calls = []
    with (
        mock.patch.object(sources, "_run", side_effect=report_runner(data, calls)),
        mock.patch.object(sources, "_wheel") as retrieve,
        pytest.raises(ValueError, match="pip report.*SHA256"),
    ):
        collect(requirements, tmp_path)
    retrieve.assert_not_called()
    assert not Path(calls[0][calls[0].index("--report") + 1]).parent.exists()


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
    package = sources._pip_report(data)["packages"][0]
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


def test_pip_report_legacy_sha256_and_missing_vcs_commit():
    data = pip_report()
    info = data["install"][0]["download_info"]
    info["archive_info"] = {"hash": "sha256=" + DIGEST}
    assert sources._pip_report(data)["packages"][0]["wheels"][0] == artifact()
    info["vcs_info"] = {"vcs": "git", "requested_revision": "main"}
    with pytest.raises(ValueError, match="locked VCS commit"):
        sources._pip_report(data)


@pytest.mark.parametrize("invalid", ["not JSON", '{"version": "2", "install": []}'])
def test_pip_invalid_report_cleanup(tmp_path, invalid):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n")
    reports = []

    def run(args, cwd):
        report = Path(args[args.index("--report") + 1])
        reports.append(report)
        report.write_text(invalid)
        return ""

    with (
        mock.patch.object(sources, "_run", side_effect=run),
        mock.patch.object(sources, "_wheel") as retrieve,
        pytest.raises(ValueError, match="report"),
    ):
        collect(requirements, tmp_path)
    retrieve.assert_not_called()
    assert not reports[0].parent.exists()


@pytest.mark.parametrize("origin", ["nested", "config", "environment", "cli"])
@pytest.mark.parametrize("host", ["repo.invalid", "cdn.invalid"])
def test_pip_requirements_credentials_transfer_same_host(tmp_path, origin, host):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n")
    url = "https://user:secret@repo.invalid/simple"
    config = ""
    kwargs = {}
    if origin == "nested":
        requirements.write_text("-r nested.txt\n")
        (tmp_path / "nested.txt").write_text(f"--index-url {url}\n-r requirements.txt\n")
    elif origin == "config":
        config = f"global.index-url={url!r}\n"
    elif origin == "environment":
        requirements.write_text("--index-url ${PRIVATE_INDEX}\ndemo\n")
    else:
        kwargs["indexes"] = [url]
    data = pip_report()
    data["install"][0]["download_info"]["url"] = f"https://{host}/demo-1-py3-none-any.whl"
    with (
        mock.patch.dict(os.environ, {"PRIVATE_INDEX": url}),
        mock.patch.object(sources, "_run", side_effect=report_runner(data, [], config)),
        mock.patch.object(sources, "_wheel") as wheel,
    ):
        collect(requirements, tmp_path, **kwargs)
    assert ("user:secret@" in wheel.call_args.args[0][0]) is (host == "repo.invalid")


def test_uv_native_export_and_marker_filtering(project, tmp_path):
    (project / "uv.lock").write_text("version = 1\n")
    packages = plan()["packages"] + [
        {
            "name": "foreign",
            "version": "1",
            "marker": "sys_platform == 'not-this-host'",
            "wheels": [{"path": "missing/foreign-1-py3-none-any.whl", "hashes": {}}],
        }
    ]
    with (
        mock.patch.object(sources, "_run", return_value=exported(plan(packages))) as run,
        mock.patch.object(sources, "_wheel") as wheel,
    ):
        assert (
            collect(project, tmp_path, "auto", extras=["x"], groups=["test"], include_project=False)
            == "uv"
        )
    args = run.call_args.args[0]
    assert args[:5] == ["uv", "export", "--locked", "--format", "pylock.toml"]
    for flag in ("--no-dev", "--no-default-groups", "--no-emit-project", "--no-editable"):
        assert flag in args
    wheel.assert_called_once()
    assert wheel.call_args.kwargs == {"locked": True, "hashed": True, "scoped": True}


def test_artifact_uses_host_tag_rank_and_path(tmp_path):
    data = {
        "name": "demo",
        "version": "1",
        "wheels": [
            artifact("https://repo/demo-1-py3-none-any.whl"),
            artifact("https://repo/demo-1-cp312-cp312-linux_x86_64.whl"),
            artifact("https://repo/demo-1-cp312-cp312-win_amd64.whl"),
        ],
    }
    data["wheels"][1]["path"] = "demo-1-cp312-cp312-linux_x86_64.whl"
    with mock.patch.object(
        sources,
        "sys_tags",
        return_value=iter([Tag("cp312", "cp312", "linux_x86_64"), Tag("py3", "none", "any")]),
    ):
        url, digest = sources._artifact(data, tmp_path)
    assert url == (tmp_path / data["wheels"][1]["path"]).as_uri()
    assert digest == "sha256:" + DIGEST


def test_sdist_fallback_and_missing_hash_fail_closed(tmp_path):
    data = {"name": "demo", "version": "1", "sdist": artifact("https://repo/demo-1.tar.gz")}
    assert sources._artifact(data, tmp_path)[0].endswith("demo-1.tar.gz")
    data["sdist"]["hashes"] = {}
    with pytest.raises(ValueError, match="SHA256"):
        sources._artifact(data, tmp_path)


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"requires-python": "<1"}, "different Python"),
        ({"environments": ["sys_platform == 'wrong'"]}, "current host"),
    ],
)
def test_pylock_host_compatibility(fields, message):
    with pytest.raises(ValueError, match=message):
        sources._selected(plan(**fields), [], [])


def test_pylock_set_markers_and_duplicate_detection():
    packages = [
        {"name": "demo", "marker": "'feature' in extras"},
        {"name": "group", "marker": "'test' in dependency_groups"},
    ]
    assert len(sources._selected(plan(packages), ["feature"], ["test"])) == 2
    assert sources._selected(plan(packages), [], []) == []
    with pytest.raises(ValueError, match="Ambiguous"):
        sources._selected(plan([{"name": "Demo_Name"}, {"name": "demo-name"}]), [], [])


def test_local_and_vcs_build_noneditable_after_plan(project, tmp_path):
    data = plan(
        [
            {"name": "local", "directory": {"path": "../local", "editable": True}},
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
    )
    with (
        mock.patch.object(sources, "_run", return_value=exported(data)),
        mock.patch.object(sources, "_wheel") as wheel,
    ):
        collect(project, tmp_path, include_project=False)
    assert wheel.call_args_list[0].args[0] == [str(tmp_path / "local")]
    assert wheel.call_args_list[1].args[0] == [
        "gitdemo @ git+https://repo/gitdemo@" + "f" * 40 + "#subdirectory=pkg"
    ]
    assert all(call.kwargs == {"locked": True, "hashed": False} for call in wheel.call_args_list)


def test_cleanup_on_retrieval_failure(project, tmp_path):
    with (
        mock.patch.object(sources, "_run", return_value=exported(plan())),
        mock.patch.object(sources, "_wheel", side_effect=ValueError("download failed")),
        pytest.raises(ValueError, match="download failed"),
    ):
        collect(project, tmp_path)
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


def test_no_retrieval_until_all_artifacts_valid(project, tmp_path):
    data = plan(plan()["packages"] + [{"name": "broken"}])
    with (
        mock.patch.object(sources, "_run", return_value=exported(data)),
        mock.patch.object(sources, "_wheel") as wheel,
        pytest.raises(ValueError, match="No compatible"),
    ):
        collect(project, tmp_path)
    wheel.assert_not_called()


def poetry_project(project):
    with (project / "pyproject.toml").open("a") as stream:
        stream.write(
            '\n[[tool.poetry.source]]\nname="private"\nurl="https://repo.invalid/simple"\npriority="explicit"\n'
        )
    (project / "poetry.lock").write_text(
        '[[package]]\nname="demo"\nversion="1"\n'
        f'files=[{{file="demo-1-py3-none-any.whl", hash="sha256:{DIGEST}"}}]\n'
        '[package.source]\ntype="legacy"\nreference="private"\nurl="https://repo.invalid/simple"\n'
    )


def test_poetry_scoped_discovery_and_lock_hash_filter(project, tmp_path):
    poetry_project(project)
    discovered = plan()
    discovered["packages"][0]["wheels"].append(
        artifact("https://evil/demo-1-py3-none-any.whl", "b" * 64)
    )

    def run(args, cwd, **kwargs):
        if args[:2] == ["poetry", "check"]:
            return ""
        if args[:2] == ["poetry", "export"]:
            return f"demo==1 --hash=sha256:{DIGEST}\n"
        assert kwargs == {"scoped": True}
        assert "--no-deps" in args and "--no-config" in args
        assert args[args.index("--index-url") + 1] == "https://user:secret@repo.invalid/simple"
        assert "--extra-index-url" not in args and "--find-links" not in args
        return exported(discovered)

    with (
        mock.patch.dict(
            os.environ,
            {
                "POETRY_HTTP_BASIC_PRIVATE_USERNAME": "user",
                "POETRY_HTTP_BASIC_PRIVATE_PASSWORD": "secret",
            },
        ),
        mock.patch.object(sources, "_run", side_effect=run),
        mock.patch.object(sources, "_wheel") as wheel,
    ):
        collect(
            project,
            tmp_path,
            "poetry",
            indexes=["https://fallback.invalid"],
            find_links=["/unrelated"],
            include_project=False,
        )
    target = wheel.call_args.args[0][0]
    assert "https://user:secret@repo.invalid/demo-1-py3-none-any.whl" in target
    assert "--hash=sha256:" + DIGEST in target
    assert wheel.call_args.args[3] == ["--index-url", "https://user:secret@repo.invalid/simple"]
    assert wheel.call_args.kwargs["scoped"] is True


def test_poetry_mismatched_hash_aborts_before_retrieval(project, tmp_path):
    poetry_project(project)
    data = plan()
    data["packages"][0]["wheels"][0]["hashes"]["sha256"] = "b" * 64
    with (
        mock.patch.object(
            sources, "_run", side_effect=["", f"demo==1 --hash=sha256:{DIGEST}", exported(data)]
        ),
        mock.patch.object(sources, "_wheel") as wheel,
        pytest.raises(ValueError, match="No compatible"),
    ):
        collect(project, tmp_path, "poetry")
    wheel.assert_not_called()


@pytest.mark.parametrize(
    "artifact_url,authenticated",
    [
        ("https://repo.invalid", True),
        ("https://repo.invalid:443", True),
        ("http://repo.invalid", False),
        ("https://repo.invalid:8443", False),
        ("https://cdn.invalid", False),
    ],
)
def test_uv_credentials_do_not_cross_origins(project, tmp_path, artifact_url, authenticated):
    (project / "uv.lock").write_text(
        '[[package]]\nname="demo"\nversion="1"\nsource={registry="https://repo.invalid/simple"}\n'
    )
    with (project / "pyproject.toml").open("a") as stream:
        stream.write('\n[[tool.uv.index]]\nname="private"\nurl="https://repo.invalid/simple"\n')
    data = plan(
        [
            {
                "name": "demo",
                "version": "1",
                "wheels": [artifact(f"{artifact_url}/demo-1-py3-none-any.whl")],
            }
        ]
    )
    with (
        mock.patch.dict(
            os.environ, {"UV_INDEX_PRIVATE_USERNAME": "user", "UV_INDEX_PRIVATE_PASSWORD": "secret"}
        ),
        mock.patch.object(sources, "_run", return_value=exported(data)),
        mock.patch.object(sources, "_wheel") as wheel,
    ):
        collect(project, tmp_path, "uv", include_project=False)
    assert ("user:secret@" in wheel.call_args.args[0][0]) is authenticated


def test_useful_errors_redact_urls_and_credentials(tmp_path):
    result = subprocess.CompletedProcess(
        [], 2, "", "Resolution failed: https://user:secret@host/simple?token=abc\npassword secret"
    )
    with (
        mock.patch.dict(os.environ, {"UV_INDEX_PRIVATE_PASSWORD": "secret"}),
        mock.patch.object(sources.subprocess, "run", return_value=result),
        pytest.raises(ValueError) as caught,
    ):
        sources._run(["uv", "https://user:secret@host"], tmp_path)
    text = str(caught.value)
    assert "Resolution failed" in text and "exit 2" in text
    assert "secret" not in text and "abc" not in text and "user:" not in text


def test_scoped_subprocess_environment_keeps_credentials_and_netrc(tmp_path):
    with (
        mock.patch.dict(
            os.environ,
            {
                "UV_EXTRA_INDEX_URL": "https://fallback",
                "UV_FIND_LINKS": "/bad",
                "UV_INDEX_PRIVATE_PASSWORD": "secret",
                "NETRC": "/netrc",
                "PIP_INDEX_URL": "https://unrelated",
                "PIP_EXTRA_INDEX_URL": "https://unrelated-extra",
                "PIP_FIND_LINKS": "/unrelated",
                "PIP_CONFIG_FILE": "/unrelated-pip.conf",
            },
        ),
        mock.patch.object(
            sources.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as run,
    ):
        sources._run(["uv"], tmp_path, scoped=True)
    env = run.call_args.kwargs["env"]
    assert "UV_EXTRA_INDEX_URL" not in env and "UV_FIND_LINKS" not in env
    assert env["UV_INDEX_PRIVATE_PASSWORD"] == "secret" and env["NETRC"] == "/netrc"
    assert not any(key.startswith("PIP_") for key in env if key != "PIP_CONFIG_FILE")
    assert env["PIP_CONFIG_FILE"] == os.devnull


def test_scoped_pip_wheel_is_isolated(tmp_path):
    with mock.patch.object(sources, "_run") as run:
        sources._wheel(["demo"], tmp_path, tmp_path / "wheels", ["--no-index"], scoped=True)
    assert "--isolated" in run.call_args.args[0]
    assert run.call_args.kwargs == {"scoped": True}


def test_native_uv_build_keeps_uv_settings_but_disables_pip_config(tmp_path):
    with (
        mock.patch.dict(
            os.environ,
            {"UV_INDEX": "https://native.invalid", "PIP_INDEX_URL": "https://unrelated.invalid"},
        ),
        mock.patch.object(
            sources.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as run,
    ):
        sources._uv_build(str(tmp_path), tmp_path, tmp_path / "wheels", [])
    env = run.call_args.kwargs["env"]
    assert env["UV_INDEX"] == "https://native.invalid"
    assert "PIP_INDEX_URL" not in env
    assert env["PIP_CONFIG_FILE"] == os.devnull


def test_uv_archive_ambient_source_override_fails_closed(project, tmp_path):
    (project / "uv.lock").write_text("version=1\n")
    data = plan(
        [{"name": "demo", "version": "1", "sdist": artifact("https://repo.invalid/demo.tar.gz")}]
    )
    with (
        mock.patch.dict(os.environ, {"UV_DEFAULT_INDEX": "https://private.invalid"}),
        mock.patch.object(sources, "_run", return_value=exported(data)),
        mock.patch.object(sources, "_wheel") as wheel,
        pytest.raises(ValueError, match="ambient UV source overrides"),
    ):
        collect(project, tmp_path, "uv")
    wheel.assert_not_called()


@pytest.mark.parametrize("no_index", [False, True])
def test_uv_global_build_options_exclude_explicit_indexes(tmp_path, no_index):
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


@pytest.mark.parametrize("mapping", [False, True])
def test_uv_unrepresentable_archive_build_rules_fail_closed(project, tmp_path, mapping):
    (project / "uv.lock").write_text("version=1\n")
    with (project / "pyproject.toml").open("a") as stream:
        if mapping:
            stream.write('[tool.uv.sources]\nbackend={index="private"}\n')
        else:
            stream.write('[[tool.uv.index]]\nurl="https://extra.invalid/simple"\n')
    data = plan(
        [{"name": "demo", "version": "1", "sdist": artifact("https://repo.invalid/demo.tar.gz")}]
    )
    with (
        mock.patch.object(sources, "_run", return_value=exported(data)),
        mock.patch.object(sources, "_wheel") as wheel,
        pytest.raises(ValueError, match="build.*index|build.*first-index"),
    ):
        collect(project, tmp_path, "uv")
    wheel.assert_not_called()
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


@pytest.mark.parametrize("manager", ["pip", "uv", "poetry"])
def test_dependency_only_root_not_built(project, tmp_path, manager):
    if manager == "uv":
        (project / "uv.lock").write_text("version=1\n")
    elif manager == "poetry":
        poetry_project(project)
    responses = (
        [exported(plan())]
        if manager != "poetry"
        else ["", f"demo==1 --hash=sha256:{DIGEST}", exported(plan())]
    )
    with (
        mock.patch.object(sources, "_run", side_effect=responses),
        mock.patch.object(sources, "_wheel") as wheel,
        mock.patch.object(sources, "_uv_build") as build,
    ):
        collect(project, tmp_path, manager)
    assert wheel.call_count == 1
    build.assert_not_called()


@pytest.mark.parametrize("manager", ["uv", "poetry"])
def test_explicit_virtual_root_not_built_even_with_build_system(project, tmp_path, manager):
    with (project / "pyproject.toml").open("a") as stream:
        stream.write('[build-system]\nrequires=[]\nbuild-backend="builder"\n')
        if manager == "uv":
            stream.write("[tool.uv]\npackage=false\n")
        else:
            stream.write("[tool.poetry]\npackage-mode=false\n")
    if manager == "uv":
        (project / "uv.lock").write_text("version=1\n")
    else:
        poetry_project(project)
    responses = (
        [exported(plan())]
        if manager == "uv"
        else ["", f"demo==1 --hash=sha256:{DIGEST}", exported(plan())]
    )
    with (
        mock.patch.object(sources, "_run", side_effect=responses),
        mock.patch.object(sources, "_wheel") as wheel,
        mock.patch.object(sources, "_uv_build") as build,
    ):
        collect(project, tmp_path, manager)
    assert wheel.call_count == 1
    build.assert_not_called()


def make_wheel(folder, name="demo", version="1"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}/__init__.py", "")
        archive.writestr(
            f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        )
        archive.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{info}/RECORD", "")
    return path


def test_real_nested_requirements_offline_exact_wheel(tmp_path):
    wheel = make_wheel(tmp_path / "links space + %20")
    nested = tmp_path / "nested.txt"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    nested.write_text(f"demo==1 --hash=sha256:{digest}\n")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"--no-index\n--find-links {wheel.parent.as_uri()}\n-r nested.txt\n")
    collect(requirements, tmp_path)
    assert (tmp_path / "work" / "wheels" / wheel.name).read_bytes() == wheel.read_bytes()
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


def test_real_requirements_hash_mismatch_aborts_before_retrieval(tmp_path):
    wheel = make_wheel(tmp_path / "links")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        f"--no-index\n--find-links {wheel.parent.as_uri()}\ndemo==1 --hash=sha256:{DIGEST}\n"
    )
    with (
        mock.patch.object(sources, "_wheel") as retrieve,
        pytest.raises(ValueError, match="DO NOT MATCH THE HASHES"),
    ):
        collect(requirements, tmp_path)
    retrieve.assert_not_called()
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()
    assert list((tmp_path / "work" / "wheels").iterdir()) == []


def test_real_pip_requirements_local_directory(tmp_path):
    dependency = tmp_path / "local space + %20"
    local_project(dependency, "local")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"--no-index\nlocal @ {dependency.as_uri()}\n")
    collect(requirements, tmp_path)
    assert (tmp_path / "work" / "wheels" / "local-1-py3-none-any.whl").is_file()


@pytest.mark.parametrize("origin", ["nested", "environment", "config"])
def test_real_pip_authenticated_requirements(tmp_path, monkeypatch, origin):
    repository = tmp_path / "repository"
    wheel = make_wheel(repository / "simple" / "demo")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (wheel.parent / "index.html").write_text(
        f'<a href="{wheel.name}#sha256={digest}">{wheel.name}</a>'
    )
    authorization = "Basic " + base64.b64encode(b"user:secret").decode()
    downloads = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != authorization:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="private"')
                self.end_headers()
                return
            downloads.append(self.path)
            super().do_GET()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(repository))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://user:secret@127.0.0.1:{server.server_port}/simple"
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"demo==1 --hash=sha256:{digest}\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.setenv("PIP_NO_INPUT", "1")
    monkeypatch.setenv("PIP_NO_CACHE_DIR", "1")
    if origin == "nested":
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "requirements.txt").write_text(
            f"--index-url {url}\ndemo==1 --hash=sha256:{digest}\n"
        )
        requirements.write_text("-r nested/requirements.txt\n")
    elif origin == "environment":
        monkeypatch.setenv("PIP_INDEX_URL", url)
    else:
        config = tmp_path / "pip.conf"
        config.write_text(f"[global]\nindex-url = {url}\n")
        monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
    try:
        collect(requirements, tmp_path)
        assert (tmp_path / "work" / "wheels" / wheel.name).read_bytes() == wheel.read_bytes()
        assert sum(path.endswith(".whl") for path in downloads) >= 2
        assert not (tmp_path / "work" / "resolved.pylock.toml").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.skipif(not shutil.which("uv"), reason="uv executable required")
def test_real_project_extras_groups_dependency_only(project, tmp_path):
    links = tmp_path / "links"
    for name in ("demo", "optional", "groupdep"):
        make_wheel(links, name)
    with (project / "pyproject.toml").open("a") as stream:
        stream.write(
            '[project.optional-dependencies]\nfeature=["optional"]\n'
            '[dependency-groups]\ntest=["groupdep"]\n'
        )
    collect(
        project,
        tmp_path,
        extras=["feature"],
        groups=["test"],
        no_index=True,
        find_links=[str(links)],
        include_project=False,
    )
    assert {path.name for path in (tmp_path / "work" / "wheels").iterdir()} == {
        f"{name}-1-py3-none-any.whl" for name in ("demo", "optional", "groupdep")
    }


def test_real_pip_multiple_indexes_best_match(tmp_path):
    indexes = []
    for label, version in (("first", "1"), ("second", "2")):
        root = tmp_path / label
        wheel = make_wheel(root / "demo", version=version)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (wheel.parent / "index.html").write_text(
            f'<a href="{wheel.name}#sha256={digest}">{wheel.name}</a>'
        )
        indexes.append(root.as_uri())
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n")
    collect(requirements, tmp_path, indexes=indexes)
    assert (tmp_path / "work" / "wheels" / "demo-2-py3-none-any.whl").is_file()


@pytest.mark.skipif(not shutil.which("uv"), reason="uv executable required")
def test_real_uv_export_uses_existing_lock_and_omits_dev(project, tmp_path):
    links = tmp_path / "links"
    for name in ("demo", "devdep", "optional", "groupdep"):
        make_wheel(links, name)
    with (project / "pyproject.toml").open("a") as stream:
        stream.write(
            '[project.optional-dependencies]\nfeature=["optional"]\n'
            '[dependency-groups]\ndev=["devdep"]\ntest=["groupdep"]\n'
        )
    sources._run(
        ["uv", "lock", "--no-index", "--find-links", str(links), "--python", sys.executable],
        project,
    )
    before = (project / "uv.lock").read_bytes()
    collect(
        project,
        tmp_path,
        "uv",
        no_index=True,
        find_links=[str(links)],
        extras=["feature"],
        groups=["test"],
        include_project=False,
    )
    assert (project / "uv.lock").read_bytes() == before
    assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {
        f"{name}-1-py3-none-any.whl" for name in ("demo", "optional", "groupdep")
    }


def local_project(root, name, dependencies=(), dynamic=False):
    root.mkdir(parents=True, exist_ok=True)
    wheel = make_wheel(root / "artifact", name)
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1\n" + "".join(
        f"Requires-Dist: {dependency}\n" for dependency in dependencies
    )
    with zipfile.ZipFile(wheel) as archive:
        members = {item: archive.read(item) for item in archive.namelist()}
    members[f"{name}-1.dist-info/METADATA"] = metadata.encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for filename, content in members.items():
            archive.writestr(filename, content)
    (root / "builder.py").write_text(
        "import pathlib, shutil\n"
        f"METADATA = {metadata!r}\n"
        "def prepare_metadata_for_build_wheel(directory, config_settings=None):\n"
        f"    info = pathlib.Path(directory) / {name + '-1.dist-info'!r}\n"
        "    info.mkdir()\n"
        "    (info / 'METADATA').write_text(METADATA)\n"
        "    return info.name\n"
        "def build_wheel(directory, config_settings=None, metadata_directory=None):\n"
        f"    source = pathlib.Path(__file__).parent / 'artifact' / {wheel.name!r}\n"
        "    shutil.copyfile(source, pathlib.Path(directory) / source.name)\n"
        "    return source.name\n"
    )
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires=[]\nbuild-backend="builder"\nbackend-path=["."]\n'
        f'[project]\nname="{name}"\nversion="1"\nrequires-python=">=3.11"\n'
        + ('dynamic=["dependencies"]\n' if dynamic else f"dependencies={list(dependencies)!r}\n")
    )


@pytest.mark.skipif(not shutil.which("uv"), reason="uv executable required")
@pytest.mark.parametrize("include_project", [False, True])
def test_real_uv_workspace_member_shared_lock(tmp_path, include_project):
    root = tmp_path / "workspace"
    root.mkdir()
    links = root / "links"
    for name in ("demo", "optional", "groupdep", "rootdep", "unrelateddep"):
        make_wheel(links, name)
    (root / "pyproject.toml").write_text(
        '[project]\nname="workspace-root"\nversion="1"\ndependencies=["rootdep"]\n'
        '[tool.uv]\nno-index=true\nfind-links=["links"]\n'
        '[tool.uv.workspace]\nmembers=["packages/*"]\n'
        "[tool.uv.sources]\nsibling={workspace=true}\n"
    )
    member = root / "packages" / "member"
    local_project(member, "member", ["demo", "sibling"])
    with (member / "pyproject.toml").open("a") as stream:
        stream.write(
            '[project.optional-dependencies]\nfeature=["optional"]\n'
            '[dependency-groups]\ntest=["groupdep"]\n'
        )
    local_project(root / "packages" / "sibling", "sibling")
    local_project(root / "packages" / "unrelated", "unrelated", ["unrelateddep"])
    sources._run(["uv", "lock", "--python", sys.executable], root)
    before = (root / "uv.lock").read_bytes()
    assert not (member / "uv.lock").exists()
    assert (
        collect(
            member,
            tmp_path,
            "auto",
            extras=["feature"],
            groups=["test"],
            include_project=include_project,
        )
        == "uv"
    )
    names = {p.name for p in (tmp_path / "work" / "wheels").iterdir()}
    expected = {
        f"{name}-1-py3-none-any.whl" for name in ("demo", "optional", "groupdep", "sibling")
    }
    if include_project:
        expected.add("member-1-py3-none-any.whl")
    assert names == expected
    assert (root / "uv.lock").read_bytes() == before
    assert not (member / "uv.lock").exists()


@pytest.mark.skipif(not shutil.which("uv"), reason="uv executable required")
@pytest.mark.parametrize("explicit", [False, True])
def test_real_uv_root_build_native_indexes(tmp_path, monkeypatch, explicit):
    root = tmp_path / "root"
    local_project(root, "root")
    repository = tmp_path / "repository"
    wheel = make_wheel(repository / "builddep", "builddep")
    (wheel.parent / "index.html").write_text(f'<a href="{wheel.name}">{wheel.name}</a>')
    project = root / "pyproject.toml"
    text = project.read_text().replace("requires=[]", 'requires=["builddep==1"]')
    if explicit:
        text += (
            '[tool.uv.sources]\nbuilddep={index="private"}\n'
            f'[[tool.uv.index]]\nname="private"\nurl="{repository.as_uri()}"\nexplicit=true\n'
        )
        empty = tmp_path / "empty"
        empty.mkdir()
        text += f'[[tool.uv.index]]\nurl="{empty.as_uri()}"\ndefault=true\n'
    else:
        text += f'[[tool.uv.index]]\nurl="{repository.as_uri()}"\ndefault=true\n'
    project.write_text(text)
    builder = root / "builder.py"
    builder.write_text("import builddep\n" + builder.read_text())
    sources._run(["uv", "lock", "--python", sys.executable], root)
    monkeypatch.setenv("PIP_INDEX_URL", "http://127.0.0.1:1/bad")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "http://127.0.0.1:1/extra")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    collect(root, tmp_path, "uv")
    assert (tmp_path / "work" / "wheels" / "root-1-py3-none-any.whl").is_file()


@pytest.mark.skipif(not shutil.which("uv"), reason="uv executable required")
def test_real_uv_sdist_build_configured_offline_links(tmp_path, monkeypatch):
    dependency = tmp_path / "dependency"
    local_project(dependency, "demo")
    metadata = dependency / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace("requires=[]", 'requires=["builddep==1"]'))
    builder = dependency / "builder.py"
    builder.write_text("import builddep\n" + builder.read_text())
    archive = tmp_path / "demo-1.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(dependency, arcname="demo-1")
    root = tmp_path / "root"
    root.mkdir()
    make_wheel(root / "links", "builddep")
    (root / "pyproject.toml").write_text(
        '[project]\nname="root"\nversion="1"\n'
        f'dependencies=["demo @ {archive.as_uri()}"]\n'
        '[tool.uv]\nno-index=true\nfind-links=["links"]\n'
    )
    sources._run(["uv", "lock", "--python", sys.executable], root)
    bad_config = tmp_path / "bad-pip.conf"
    bad_config.write_text(
        "[global]\nno-index=false\nfind-links=/does-not-exist\nindex-url=http://127.0.0.1:1/bad\n"
    )
    monkeypatch.setenv("PIP_CONFIG_FILE", str(bad_config))
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "http://127.0.0.1:1/extra")
    monkeypatch.setenv("PIP_FIND_LINKS", "/does-not-exist")
    collect(root, tmp_path, "uv")
    assert (tmp_path / "work" / "wheels" / "demo-1-py3-none-any.whl").is_file()


@pytest.mark.skipif(
    not shutil.which("uv") or not shutil.which("poetry"), reason="native uv/Poetry required"
)
@pytest.mark.parametrize("manager", ["uv", "poetry"])
def test_real_virtual_root_with_build_system(tmp_path, manager):
    wheel = make_wheel(tmp_path / "links")
    root = tmp_path / "root"
    local_project(root, "root", [f"demo @ {wheel.as_uri()}"])
    with (root / "pyproject.toml").open("a") as stream:
        stream.write(
            "[tool.uv]\npackage=false\n"
            if manager == "uv"
            else "[tool.poetry]\npackage-mode=false\n"
        )
    (root / "builder.py").write_text('raise RuntimeError("virtual root must not be built")\n')
    sources._run([manager, "lock"], root)
    collect(root, tmp_path, manager, no_index=True)
    assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {wheel.name}


@pytest.mark.skipif(not shutil.which("uv"), reason="uv executable required")
def test_real_dynamic_dependencies_then_root_build(tmp_path):
    root = tmp_path / "root"
    local_project(root, "root", ["demo==1"], dynamic=True)
    links = tmp_path / "links"
    make_wheel(links)
    collect(root, tmp_path, no_index=True, find_links=[str(links)])
    assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {
        "demo-1-py3-none-any.whl",
        "root-1-py3-none-any.whl",
    }


@pytest.mark.skipif(not shutil.which("uv"), reason="uv executable required")
@pytest.mark.parametrize("manager", ["pip", "uv", "poetry"])
def test_real_local_directory_dependency_build(tmp_path, manager):
    if manager == "poetry" and not shutil.which("poetry"):
        pytest.skip("Poetry executable required")
    dependency = tmp_path / "local"
    local_project(dependency, "local")
    root = tmp_path / "root"
    local_project(root, "root", [f"local @ {dependency.as_uri()}"])
    if manager != "pip":
        sources._run([manager, "lock"], root)
    collect(root, tmp_path, manager, no_index=True, include_project=False)
    assert (tmp_path / "work" / "wheels" / "local-1-py3-none-any.whl").is_file()


@pytest.mark.parametrize(
    "text", ['lock-version="2.0"\npackages=[]', "invalid TOML", 'lock-version="1.0"\n']
)
def test_invalid_pylock_is_rejected(text):
    with pytest.raises(ValueError, match="pylock"):
        sources._pylock(text)


def test_missing_uv_executable_has_clear_error(project, tmp_path):
    with (
        mock.patch.object(sources.subprocess, "run", side_effect=FileNotFoundError),
        pytest.raises(ValueError, match="Could not run uv"),
    ):
        collect(project, tmp_path)


@pytest.mark.parametrize("manager", ["pip", "poetry"])
@pytest.mark.parametrize(
    "artifact_origin,allowed",
    [
        ("https://repo.invalid", True),
        ("https://repo.invalid:443", True),
        ("http://repo.invalid", False),
        ("https://repo.invalid:8443", False),
        ("https://other.invalid", False),
    ],
)
def test_manager_index_credentials_transfer_same_origin(
    project, tmp_path, manager, artifact_origin, allowed
):
    data = plan(
        [
            {
                "name": "demo",
                "version": "1",
                "wheels": [artifact(f"{artifact_origin}/demo-1-py3-none-any.whl")],
            }
        ]
    )
    responses = [exported(data)]
    if manager == "poetry":
        poetry_project(project)
        responses = ["", f"demo==1 --hash=sha256:{DIGEST}", exported(data)]
    with (
        mock.patch.dict(
            os.environ,
            {
                "POETRY_HTTP_BASIC_PRIVATE_USERNAME": "user",
                "POETRY_HTTP_BASIC_PRIVATE_PASSWORD": "secret",
            },
        ),
        mock.patch.object(sources, "_run", side_effect=responses),
        mock.patch.object(sources, "_wheel") as wheel,
    ):
        collect(
            project,
            tmp_path,
            manager,
            indexes=["https://user:secret@repo.invalid/simple"],
            include_project=False,
        )
    assert ("user:secret@" in wheel.call_args.args[0][0]) is allowed


def test_resolution_collision_is_not_overwritten_or_deleted(project, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    resolution = work / "resolved.pylock.toml"
    resolution.write_text("existing user's file")
    with (
        mock.patch.object(sources, "_run", return_value=exported(plan())),
        mock.patch.object(sources, "_wheel") as wheel,
        pytest.raises(ValueError, match="already exists"),
    ):
        collect(project, tmp_path)
    assert resolution.read_text() == "existing user's file"
    wheel.assert_not_called()


@pytest.mark.skipif(
    not shutil.which("poetry") or not shutil.which("uv"),
    reason="native Poetry and uv executables required",
)
def test_real_poetry_explicit_authenticated_repository(project, tmp_path):
    repository = tmp_path / "repository"
    wheel = make_wheel(repository / "simple" / "demo")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (wheel.parent / "index.html").write_text(
        f'<a href="{wheel.name}#sha256={digest}">{wheel.name}</a>'
    )
    authorization = "Basic " + base64.b64encode(b"user:secret").decode()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != authorization:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="private"')
                self.end_headers()
                return
            super().do_GET()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(repository))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/simple"
    with (project / "pyproject.toml").open("a") as stream:
        stream.write(
            '[tool.poetry.dependencies]\ndemo={source="private"}\n'
            f'[[tool.poetry.source]]\nname="private"\nurl="{url}"\npriority="explicit"\n'
        )
    try:
        with mock.patch.dict(
            os.environ,
            {
                "POETRY_HTTP_BASIC_PRIVATE_USERNAME": "user",
                "POETRY_HTTP_BASIC_PRIVATE_PASSWORD": "secret",
                "POETRY_CACHE_DIR": str(tmp_path / "poetry-cache"),
                "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
            },
        ):
            sources._run(["poetry", "lock"], project)
            before = (project / "poetry.lock").read_bytes()
            with mock.patch.dict(
                os.environ,
                {
                    "UV_EXTRA_INDEX_URL": "http://127.0.0.1:1/bad",
                    "UV_FIND_LINKS": "/nonexistent-unrelated",
                },
            ):
                collect(
                    project,
                    tmp_path,
                    "poetry",
                    find_links=["/nonexistent-unrelated"],
                    include_project=False,
                )
        assert before == (project / "poetry.lock").read_bytes()
        assert (tmp_path / "work" / "wheels" / wheel.name).read_bytes() == wheel.read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
