"""Foreign wheel resolution must not consult host markers or execute builds."""

import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from wheelport import sources, targeted
from wheelport.targets import Target


def target(platform="windows", python="3.12"):
    return Target.create(python_version=python, python_platform=platform)


def collect(input, tmp_path, manager="pip", **kwargs):
    return sources.collect(
        input,
        manager,
        tmp_path / "work" / "wheels",
        **{
            "target": target(),
            "indexes": [],
            "find_links": [],
            "no_index": False,
            "extras": [],
            "groups": [],
            "include_project": False,
            **kwargs,
        },
    )


def wheel(
    folder, name="demo", tag="py3-none-any", dependencies=(), requires_python=">=3.11", version="1"
):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{version}-{tag}.whl"
    info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}/__init__.py", "")
        archive.writestr(
            f"{info}/METADATA",
            (
                f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\nRequires-Python: {requires_python}\n"
                + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
            ),
        )
        archive.writestr(
            f"{info}/WHEEL", f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n"
        )
        archive.writestr(f"{info}/RECORD", "")
    return path


def plan(packages=None, **kwargs):
    return {
        "lock-version": "1.0",
        "packages": packages
        if packages is not None
        else [
            {
                "name": "demo",
                "version": "1",
                "wheels": [
                    {
                        "url": "https://repo.invalid/demo-1-cp312-cp312-win_amd64.whl",
                        "hashes": {"sha256": "a" * 64},
                    }
                ],
            }
        ],
        **kwargs,
    }


def exported(data):
    return "\n".join(f'"{key}" = {sources._toml_value(value)}' for key, value in data.items())


def project(tmp_path, extra=""):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="root"\nversion="1"\nrequires-python=">=3.11"\n'
        'dependencies=["demo"]\n' + extra
    )
    return root


@pytest.mark.parametrize(
    "body",
    [
        "./source",
        "demo @ file:///source",
        "demo @ git+https://repo/pkg",
        "demo @ https://repo/demo.tar.gz",
        "-e ./source",
        "-r https://repo/requirements.txt",
        "-c https://repo/constraints.txt",
    ],
)
def test_source_inputs_rejected_before_resolution(tmp_path, body):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(body + "\n")
    with (
        mock.patch.object(sources, "_run", return_value="") as run,
        pytest.raises(ValueError, match="prebuilt|[Uu]nsupported|Remote"),
    ):
        collect(requirements, tmp_path)
    run.assert_not_called()


def test_free_threaded_rejected_before_subprocess(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n")
    with (
        mock.patch.object(sources, "_run") as run,
        pytest.raises(ValueError, match="free-threaded"),
    ):
        collect(
            requirements,
            tmp_path,
            target=Target.create(python_version="3.13", python_platform="windows", abi="cp313t"),
        )
    run.assert_not_called()


@pytest.mark.parametrize("manager", ["pip", "uv", "poetry"])
def test_root_build_rejected_without_native_environment_check(tmp_path, manager):
    root = project(tmp_path, '[build-system]\nrequires=[]\nbuild-backend="evil"\n')
    with (
        mock.patch.object(sources, "_run") as run,
        pytest.raises(ValueError, match="--dependencies-only or --project-wheel"),
    ):
        collect(root, tmp_path, manager, include_project=True)
    run.assert_not_called()


def test_dynamic_dependencies_rejected(tmp_path):
    root = project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname="root"\nversion="1"\ndynamic=["dependencies"]\n'
    )
    with (
        mock.patch.object(sources, "_run", return_value="") as run,
        pytest.raises(ValueError, match="dynamic"),
    ):
        collect(root, tmp_path)
    assert not any(call.args[0][0] == "uv" for call in run.call_args_list)


def test_selected_explicit_markers_and_sets():
    packages = [
        {"name": "windows", "marker": "sys_platform == 'win32'"},
        {"name": "linux", "marker": "sys_platform == 'linux'"},
        {"name": "extra", "marker": "'feature' in extras"},
        {"name": "group", "marker": "'test' in dependency_groups"},
    ]
    assert {
        p["name"] for p in targeted._selected(plan(packages), target(), ["feature"], ["test"])
    } == {"windows", "extra", "group"}
    with pytest.raises(ValueError, match="platform_release"):
        targeted._selected(
            plan(
                [{"name": "bad", "marker": "sys_platform == 'linux' and platform_release == '6'"}]
            ),
            target(),
            [],
            [],
        )


@pytest.mark.parametrize("kind", ["sdist", "directory", "vcs"])
def test_lock_source_only_rejected(tmp_path, kind):
    root = project(tmp_path)
    (root / "uv.lock").write_text("version=1\n")
    data = plan([{"name": "demo", "version": "1", kind: {"path": "source"}}])
    with (
        mock.patch.object(sources, "_run", return_value=exported(data)) as run,
        pytest.raises(ValueError, match="prebuilt|compatible locked wheel"),
    ):
        collect(root, tmp_path, "uv")
    assert run.call_count == 1
    args = run.call_args.args[0]
    assert args[:5] == ["uv", "export", "--locked", "--format", "pylock.toml"]
    assert "--no-build" in args
    assert "--python-platform" not in args and "--python-version" not in args
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


def test_target_download_and_credentials_and_plan_cleanup(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo==1\n")
    calls = []

    def run(args, cwd, **kwargs):
        calls.append(args)
        if "config" in args:
            return "global.index-url='https://user:secret@repo.invalid/simple'\n"
        if args[:3] == ["uv", "pip", "compile"]:
            assert "--no-config" in args and "--no-sources" in args
            assert args[args.index("--index-strategy") + 1] == "unsafe-best-match"
            assert "--no-build" in args
            return exported(plan())
        assert args[:4] == [sys.executable, "-m", "pip", "download"]
        assert "--no-deps" in args and "--require-hashes" in args
        assert args[args.index("--platform") + 1] == "win_amd64"
        assert args[args.index("--python-version") + 1] == "3.12.0"
        resolution = tmp_path / "work" / "resolved.pylock.toml"
        if not resolution.exists():
            resolution = Path(args[-1]).parent / "resolved.pylock.toml"
        if os.name != "nt":
            assert resolution.stat().st_mode & 0o777 == 0o600
        text = Path(args[-1]).read_text()
        assert "user:secret@repo.invalid" in text
        return ""

    with (
        mock.patch.object(sources, "_run", side_effect=run),
        mock.patch.object(targeted, "_metadata", return_value=[]),
    ):
        collect(requirements, tmp_path)
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


def test_mac_environment_override_is_child_only(tmp_path):
    with (
        mock.patch.dict(os.environ, {"MACOSX_DEPLOYMENT_TARGET": "99"}),
        mock.patch.object(
            sources.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as run,
    ):
        sources._run(
            ["uv"], tmp_path, scoped=True, env_overrides={"MACOSX_DEPLOYMENT_TARGET": "11.0"}
        )
        assert run.call_args.kwargs["env"]["MACOSX_DEPLOYMENT_TARGET"] == "11.0"
        assert os.environ["MACOSX_DEPLOYMENT_TARGET"] == "99"


@pytest.mark.parametrize(
    "platform,tag,machine,sys_platform",
    [
        ("windows", "cp313-cp313-win_amd64", "AMD64", "win32"),
        ("aarch64-manylinux_2_17", "cp313-cp313-manylinux_2_17_aarch64", "aarch64", "linux"),
    ],
)
def test_real_foreign_wheels_transitive_markers(tmp_path, platform, tag, machine, sys_platform):
    links = tmp_path / "links"
    root = wheel(
        links,
        tag=tag,
        dependencies=[
            f"osdep==1; sys_platform == '{sys_platform}'",
            f"archdep==1; platform_machine == '{machine}'",
            "pydep==1; python_version == '3.13'",
            "absent==1; python_version < '3.13'",
            "absent==1; sys_platform == 'darwin'",
        ],
    )
    for name in ("osdep", "archdep", "pydep"):
        wheel(links, name=name, tag=tag)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo==1\n")
    collect(
        requirements,
        tmp_path,
        target=target(platform, "3.13"),
        no_index=True,
        find_links=[str(links)],
    )
    assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {
        root.name,
        *(f"{name}-1-{tag}.whl" for name in ("osdep", "archdep", "pydep")),
    }


@pytest.mark.parametrize("mismatch", [False, True])
def test_real_nested_hash_constraints_target_markers(tmp_path, mismatch):
    path = wheel(tmp_path / "links", tag="cp312-cp312-win_amd64")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "constraints.txt").write_text(
        f"demo==1; sys_platform == 'win32' \\\n+    --hash=sha256:{'a' * 64 if mismatch else digest}\n".replace(
            "\n+", "\n"
        )
    )
    (tmp_path / "nested.txt").write_text(
        "demo==1; sys_platform == 'win32'\nabsent; sys_platform == 'linux'\n"
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        f"--no-index\n--find-links {path.parent.as_uri()}\n-r nested.txt\n-c constraints.txt\n"
    )
    if mismatch:
        with pytest.raises(ValueError, match="input allowed hashes"):
            collect(requirements, tmp_path)
        assert list((tmp_path / "work" / "wheels").iterdir()) == []
    else:
        collect(requirements, tmp_path)
        assert (tmp_path / "work" / "wheels" / path.name).read_bytes() == path.read_bytes()
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


@pytest.mark.parametrize("field", ["platform_release", "platform_version"])
def test_real_unknown_transitive_marker_rejected(tmp_path, field):
    path = wheel(
        tmp_path / "links", dependencies=[f"absent; sys_platform == 'darwin' and {field} == '1'"]
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n")
    with pytest.raises(ValueError, match=field):
        collect(requirements, tmp_path, no_index=True, find_links=[str(path.parent)])


def test_real_static_project_extras_groups(tmp_path):
    root = project(
        tmp_path,
        '[project.optional-dependencies]\nfeature=["optional"]\n[dependency-groups]\ntest=["groupdep"]\n',
    )
    links = tmp_path / "links"
    for name in ("demo", "optional", "groupdep"):
        wheel(links, name=name)
    collect(
        root, tmp_path, no_index=True, find_links=[str(links)], extras=["feature"], groups=["test"]
    )
    assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {
        f"{name}-1-py3-none-any.whl" for name in ("demo", "optional", "groupdep")
    }


@pytest.mark.parametrize("origin", ["base", "extra", "group", "included-group"])
@pytest.mark.parametrize("mismatch", [False, True])
def test_real_pep621_direct_wheel_hash(tmp_path, origin, mismatch):
    links = tmp_path / "links space + %20"
    path = wheel(links, dependencies=["child"])
    wheel(links, "child")
    wheel(links, "unhashed")
    digest = "0" * 64 if mismatch else hashlib.sha256(path.read_bytes()).hexdigest().upper()
    dependency = f"demo @ {path.as_uri()}#sha256={digest}"
    root = project(tmp_path)
    metadata = {
        "project": {
            "name": "root",
            "version": "1",
            "dependencies": ["unhashed", *([dependency] if origin == "base" else [])],
        }
    }
    kwargs = {}
    if origin == "extra":
        metadata["project"]["optional-dependencies"] = {"My_Feature": [dependency]}
        kwargs["extras"] = ["my_feature"]
    elif origin in {"group", "included-group"}:
        metadata["dependency-groups"] = {"My_Group": [dependency]}
        if origin == "included-group":
            metadata["dependency-groups"] = {
                "My_Group": [{"include-group": "inner_group"}],
                "Inner_Group": [dependency],
            }
        kwargs["groups"] = ["my_group"]
    (root / "pyproject.toml").write_text(exported(metadata))
    if mismatch:
        with pytest.raises(ValueError, match="input allowed hashes for demo"):
            collect(root, tmp_path, no_index=True, find_links=[str(links)], **kwargs)
        assert list((tmp_path / "work" / "wheels").iterdir()) == []
    else:
        collect(root, tmp_path, no_index=True, find_links=[str(links)], **kwargs)
        assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {
            f"{name}-1-py3-none-any.whl" for name in ("demo", "child", "unhashed")
        }
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


@pytest.mark.parametrize("inactive", ["marker", "extra", "group"])
def test_real_pep621_unselected_url_hash_not_enforced(tmp_path, inactive):
    path = wheel(tmp_path / "links")
    # The same package is selected without a hash; inactive declarations must not restrict it.
    dependency = f"demo @ {path.as_uri()}#sha256={'0' * 64}"
    metadata = {"project": {"name": "root", "version": "1", "dependencies": ["demo==1"]}}
    if inactive == "marker":
        metadata["project"]["dependencies"].append(dependency + " ; sys_platform == 'linux'")
    elif inactive == "extra":
        metadata["project"]["optional-dependencies"] = {"My_Feature": [dependency]}
    else:
        metadata["dependency-groups"] = {"My_Group": [dependency]}
    root = project(tmp_path)
    (root / "pyproject.toml").write_text(exported(metadata))
    collect(root, tmp_path, no_index=True, find_links=[str(path.parent)])
    assert (tmp_path / "work" / "wheels" / path.name).read_bytes() == path.read_bytes()


def test_real_uv_universal_lock_target_filter(tmp_path):
    root = project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname="root"\nversion="1"\nrequires-python=">=3.11"\ndependencies=["demo", "winonly; sys_platform == \'win32\'", "linuxonly; sys_platform == \'linux\'"]\n'
    )
    links = tmp_path / "links"
    for name in ("demo", "winonly", "linuxonly"):
        wheel(links, name=name)
    sources._run(
        ["uv", "lock", "--no-index", "--find-links", str(links), "--python", sys.executable], root
    )
    before = (root / "uv.lock").read_bytes()
    collect(root, tmp_path, "uv", no_index=True, find_links=[str(links)])
    assert (root / "uv.lock").read_bytes() == before
    assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {
        f"{name}-1-py3-none-any.whl" for name in ("demo", "winonly")
    }


def test_poetry_explicit_target_and_exact_source(tmp_path):
    root = project(tmp_path)
    (root / "poetry.lock").write_text(
        '[[package]]\nname="demo"\nversion="1"\nfiles=[{file="demo-1-cp312-cp312-win_amd64.whl", hash="sha256:'
        + "a" * 64
        + '"}]\n[package.source]\ntype="legacy"\nurl="https://repo.invalid/simple"\nreference="private"\n'
    )
    calls = []

    def run(args, cwd, **kwargs):
        calls.append(args)
        if args[:2] == ["poetry", "check"]:
            return ""
        if args[:2] == ["poetry", "export"]:
            return f"demo==1; sys_platform == 'win32' --hash=sha256:{'a' * 64}\nabsent==1; sys_platform == 'linux'\n"
        if args[:3] == ["uv", "pip", "compile"]:
            assert "--no-deps" in args
            assert args[args.index("--index-url") + 1] == "https://repo.invalid/simple"
            assert "--extra-index-url" not in args and "--find-links" not in args
            assert "--no-build" in args and "--python-platform" in args
            assert kwargs["scoped"] is True
            return exported(plan())
        return ""

    with mock.patch.object(sources, "_run", side_effect=run):
        collect(
            root,
            tmp_path,
            "poetry",
            indexes=["https://unrelated.invalid"],
            find_links=["/unrelated"],
        )
    assert len(calls) == 4


@pytest.mark.parametrize("mismatch", [False, True])
def test_real_direct_wheel_url_hash(tmp_path, mismatch):
    path = wheel(tmp_path / "links space + %20", tag="cp312-cp312-win_amd64")
    digest = "a" * 64 if mismatch else hashlib.sha256(path.read_bytes()).hexdigest()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"demo @ {path.as_uri()}#sha256={digest}\n")
    if mismatch:
        with pytest.raises(ValueError, match="input allowed hashes"):
            collect(requirements, tmp_path, no_index=True)
        assert list((tmp_path / "work" / "wheels").iterdir()) == []
    else:
        collect(requirements, tmp_path, no_index=True)
        assert (tmp_path / "work" / "wheels" / path.name).read_bytes() == path.read_bytes()


def test_real_hash_mode_requires_hashes_for_transitives(tmp_path):
    path = wheel(tmp_path / "links", dependencies=["child==1"])
    wheel(path.parent, "child")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"--require-hashes\ndemo==1 --hash=sha256:{digest}\n")
    with pytest.raises(ValueError, match="incoming pinned hashes for child"):
        collect(requirements, tmp_path, no_index=True, find_links=[str(path.parent)])
    assert list((tmp_path / "work" / "wheels").iterdir()) == []


def test_real_target_package_extra_transitive(tmp_path):
    path = wheel(tmp_path / "links", dependencies=["child[feature]==1"])
    wheel(
        path.parent,
        "child",
        dependencies=["optional==1; extra == 'feature' and sys_platform == 'win32'"],
    )
    wheel(path.parent, "optional")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo==1\n")
    collect(requirements, tmp_path, no_index=True, find_links=[str(path.parent)])
    assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {
        f"{name}-1-py3-none-any.whl" for name in ("demo", "child", "optional")
    }


def test_real_poetry_native_graph_foreign_direct_wheels(tmp_path):
    links = tmp_path / "links"
    path = wheel(links, tag="cp312-cp312-win_amd64")
    windows = wheel(links, "winonly")
    linux = wheel(links, "linuxonly")
    root = project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname="root"\nversion="1"\nrequires-python=">=3.11,<4"\n'
        f'dependencies=["demo @ {path.as_uri()}", '
        f"\"winonly @ {windows.as_uri()} ; sys_platform == 'win32'\", "
        f"\"linuxonly @ {linux.as_uri()} ; sys_platform == 'linux'\"]\n"
    )
    sources._run(["poetry", "lock"], root)
    before = (root / "poetry.lock").read_bytes()
    collect(root, tmp_path, "poetry", no_index=True)
    assert (root / "poetry.lock").read_bytes() == before
    assert {p.name for p in (tmp_path / "work" / "wheels").iterdir()} == {path.name, windows.name}


def test_pip_requirements_index_replaces_configured_index(tmp_path):
    path = wheel(tmp_path / "links")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("--index-url https://requirements.invalid/simple\ndemo==1\n")
    data = plan([{"name": "demo", "version": "1", "wheels": [{"path": str(path), "hashes": {}}]}])

    def run(args, cwd, **kwargs):
        if "config" in args:
            return "global.index-url='https://configured.invalid/simple'\n"
        if args[:3] == ["uv", "pip", "compile"]:
            assert args.count("--index-url") == 1
            assert args[args.index("--index-url") + 1] == "https://requirements.invalid/simple"
            return exported(data)
        return ""

    with mock.patch.object(sources, "_run", side_effect=run):
        collect(requirements, tmp_path)


def test_resolution_collision_prevents_all_subprocesses(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n")
    work = tmp_path / "work"
    work.mkdir()
    resolution = work / "resolved.pylock.toml"
    resolution.write_text("user data")
    with (
        mock.patch.object(sources, "_run") as run,
        pytest.raises(ValueError, match="already exists"),
    ):
        collect(requirements, tmp_path)
    run.assert_not_called()
    assert resolution.read_text() == "user data"


@pytest.mark.parametrize(
    "location",
    [
        "https://repo/demo.tar.gz",
        "git+https://repo/demo-1-py3-none-any.whl",
        "file://remote/demo-1-py3-none-any.whl",
    ],
)
def test_wheel_only_artifact_cannot_disguise_build_source(tmp_path, location):
    package = {
        "name": "demo",
        "version": "1",
        "archive": {
            "url": location,
            "name": "demo-1-py3-none-any.whl",
            "hashes": {"sha256": "a" * 64},
        },
    }
    with pytest.raises(ValueError, match="compatible locked wheel"):
        sources._artifact(package, tmp_path, tags=target().tags(), wheel_only=True)


def test_target_python_metadata_validation(tmp_path):
    path = wheel(tmp_path, requires_python=">=3.13")
    with pytest.raises(ValueError, match="different Python"):
        targeted._metadata(path, target(), [])


def test_mac_resolver_child_environment(tmp_path):
    root = project(tmp_path)
    (root / "uv.lock").write_text("version=1\n")
    with mock.patch.object(sources, "_run", return_value=exported(plan([]))) as run:
        collect(root, tmp_path, "uv", target=target("macos"))
    assert run.call_args.kwargs["env_overrides"] == {"MACOSX_DEPLOYMENT_TARGET": "11.0"}
    assert run.call_args.kwargs["pip_isolated"] is True


def test_real_local_file_include_preserves_hashes(tmp_path):
    path = wheel(tmp_path / "links space + %20")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    nested = tmp_path / "nested space + %20.txt"
    nested.write_text(f"demo==1 --hash=sha256:{digest}\n")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"-r {nested.as_uri()}\n")
    collect(requirements, tmp_path, no_index=True, find_links=[str(path.parent)])
    assert (tmp_path / "work" / "wheels" / path.name).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("absolute", [False, True])
def test_real_direct_wheel_path(tmp_path, absolute):
    path = wheel(tmp_path / "links space + %20", tag="cp312-cp312-win_amd64")
    requirements = tmp_path / "requirements.txt"
    location = str(path) if absolute else str(path.relative_to(tmp_path))
    requirements.write_text(location + "\n")
    collect(requirements, tmp_path, no_index=True)
    assert (tmp_path / "work" / "wheels" / path.name).read_bytes() == path.read_bytes()


def test_transport_source_options_transfer_without_ambient_pip_settings(tmp_path):
    path = wheel(tmp_path / "links")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("--trusted-host private.invalid\n--cert ca.pem\ndemo==1\n")
    data = plan([{"name": "demo", "version": "1", "wheels": [{"path": str(path), "hashes": {}}]}])

    def run(args, cwd, **kwargs):
        if "config" in args:
            return ""
        assert args[args.index("--cert") + 1] == str(tmp_path / "ca.pem")
        assert kwargs["scoped"] is True
        if args[:3] == ["uv", "pip", "compile"]:
            assert "--trusted-host" not in args
            assert args[args.index("--allow-insecure-host") + 1] == "private.invalid"
            return exported(data)
        assert args[args.index("--trusted-host") + 1] == "private.invalid"
        return ""

    with mock.patch.object(sources, "_run", side_effect=run):
        collect(requirements, tmp_path)


@pytest.mark.parametrize("local_dependency", [False, True])
def test_real_uv_locked_freshness_never_executes_dynamic_backend(tmp_path, local_dependency):
    root = project(tmp_path)
    metadata = root / "pyproject.toml"
    backend_root = root
    if local_dependency:
        backend_root = tmp_path / "local"
        backend_root.mkdir()
        metadata.write_text(
            '[project]\nname="root"\nversion="1"\nrequires-python=">=3.11"\n'
            'dependencies=["local"]\n[tool.uv.sources]\nlocal={path="../local"}\n'
        )
    backend_metadata = backend_root / "pyproject.toml"
    backend_metadata.write_text(
        "[project]\n"
        + ('name="local"\n' if local_dependency else 'name="root"\n')
        + 'version="1"\nrequires-python=">=3.11"\ndependencies=["demo"]\n'
        '[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n'
    )
    spy = backend_root / "backend-ran"
    (backend_root / "backend.py").write_text(
        'from pathlib import Path\nPath(__file__).with_name("backend-ran").write_text("executed")\n'
        'raise RuntimeError("backend must not execute")\n'
    )
    path = wheel(tmp_path / "links")
    sources._run(
        ["uv", "lock", "--no-index", "--find-links", str(path.parent), "--python", sys.executable],
        root,
    )
    before = (root / "uv.lock").read_bytes()
    assert not spy.exists()
    backend_metadata.write_text(
        backend_metadata.read_text().replace('dependencies=["demo"]', 'dynamic=["dependencies"]')
    )
    with pytest.raises(ValueError, match="build|locked|lockfile"):
        collect(
            root,
            tmp_path,
            "uv",
            target=target(python="3.13"),
            no_index=True,
            find_links=[str(path.parent)],
        )
    assert not spy.exists()
    assert (root / "uv.lock").read_bytes() == before
    assert list((tmp_path / "work" / "wheels").iterdir()) == []
    # Positive control: the same freshness check without the guard imports the spy.
    with pytest.raises(ValueError, match="backend must not execute"):
        sources._run(
            [
                "uv",
                "export",
                "--locked",
                "--format",
                "pylock.toml",
                "--no-emit-project",
                "--python",
                sys.executable,
                "--no-index",
                "--find-links",
                str(path.parent),
            ],
            root,
        )
    assert spy.read_text() == "executed"


@pytest.mark.parametrize(
    "extra_key,group_key,include_key",
    [
        ("my-feature", "my-group", "inner-group"),
        ("My_Feature", "My_Group", "Inner_Group"),
    ],
)
def test_real_normalized_extra_group_and_include_names(tmp_path, extra_key, group_key, include_key):
    root = project(
        tmp_path,
        f'[project.optional-dependencies]\n{extra_key}=["optional"]\n'
        f'[dependency-groups]\n{group_key}=["groupdep", {{include-group="inner_group"}}]\n'
        f'{include_key}=["included"]\n',
    )
    links = tmp_path / "links"
    for name in ("demo", "optional", "groupdep", "included"):
        wheel(links, name)
    before = (root / "pyproject.toml").read_bytes()
    collect(
        root,
        tmp_path,
        no_index=True,
        find_links=[str(links)],
        extras=["my_feature"],
        groups=["my_group"],
    )
    assert (root / "pyproject.toml").read_bytes() == before
    assert {path.name for path in (tmp_path / "work" / "wheels").iterdir()} == {
        f"{name}-1-py3-none-any.whl" for name in ("demo", "optional", "groupdep", "included")
    }


def test_selected_normalizes_default_groups_and_user_names():
    data = plan(
        [
            {"name": "optional", "marker": "'my-feature' in extras"},
            {"name": "group", "marker": "'my-group' in dependency_groups"},
            {"name": "default", "marker": "'default-group' in dependency_groups"},
        ],
        **{"default-groups": ["Default_Group"]},
    )
    assert {
        package["name"]
        for package in targeted._selected(data, target(), ["My_Feature"], ["My_Group"])
    } == {"optional", "group", "default"}


@pytest.mark.parametrize("manager", ["pip", "uv"])
@pytest.mark.parametrize("declared", ["a" * 64, "invalid"])
def test_declared_local_hash_checked_before_metadata_or_unrelated_download(
    tmp_path, manager, declared
):
    path = wheel(tmp_path / "links")
    packages = [
        {
            "name": "unrelated",
            "version": "1",
            "wheels": [
                {
                    "url": "https://repo.invalid/unrelated-1-py3-none-any.whl",
                    "hashes": {"sha256": "b" * 64},
                }
            ],
        },
        {
            "name": "demo",
            "version": "1",
            "wheels": [{"path": str(path), "hashes": {"sha256": declared}}],
        },
    ]
    if manager == "uv":
        input = project(tmp_path)
        (input / "uv.lock").write_text("version=1\n")
    else:
        input = tmp_path / "requirements.txt"
        input.write_text("unrelated\ndemo\n")

    def run(args, cwd, **kwargs):
        if "config" in args:
            return ""
        assert args[0] == "uv"
        return exported(plan(packages))

    with (
        mock.patch.object(sources, "_run", side_effect=run) as commands,
        mock.patch.object(targeted, "_metadata") as metadata,
        pytest.raises(ValueError, match="[Ii]nvalid.*SHA256|SHA256 hash mismatch"),
    ):
        collect(input, tmp_path, manager)
    metadata.assert_not_called()
    assert not any(
        call.args[0][:4] == [sys.executable, "-m", "pip", "download"]
        for call in commands.call_args_list
    )
    assert list((tmp_path / "work" / "wheels").iterdir()) == []
    assert not (tmp_path / "work" / "resolved.pylock.toml").exists()


def test_local_hash_accepts_uppercase_and_fills_missing(tmp_path):
    path = wheel(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for hashes in ({}, {"sha256": digest.upper()}):
        artifact = {"url": path.as_uri(), "hashes": hashes}
        targeted._local_hash(artifact, tmp_path)
        assert artifact["hashes"]["sha256"] == digest


def test_real_windows_requires_python_candidate_is_fail_closed(tmp_path):
    links = tmp_path / "links"
    wheel(links, requires_python=">=3.11", version="1")
    wheel(links, requires_python=">=3.13", version="2")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n")
    with pytest.raises(ValueError, match="different Python"):
        collect(requirements, tmp_path, no_index=True, find_links=[str(links)])
    assert list((tmp_path / "work" / "wheels").iterdir()) == []
