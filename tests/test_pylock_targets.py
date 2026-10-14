"""Explicit-target resolution: generated pylocks match machine, arch, and Python.

Unit tests pin the selection rules; integration tests drive the real uv/pip
resolvers against local flat directories and a local HTTP index for foreign
targets.
"""

from __future__ import annotations

import copy
from pathlib import Path
from unittest import mock

import pytest
from helpers import build_wheel, write_project
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from wheelpack import pylock, sources, targeted
from wheelpack.targets import Target

DIGEST = "a" * 64


def target(platform="x86_64-manylinux_2_17", version="3.12"):
    parts = version.split(".")
    return Target.create(
        python_version=version, python_platform=platform, abi=f"cp{parts[0]}{parts[1]}"
    )


def plan(packages=None, **fields):
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
                        "hashes": {"sha256": DIGEST},
                    }
                ],
            }
        ],
        **fields,
    }


def collect_target(
    target_input,
    explicit_target,
    wheel_dir,
    *,
    indexes=(),
    find_links=(),
    no_index=False,
    extras=(),
    groups=(),
    include_project=False,
    project=None,
    uv_root=None,
):
    """Run targeted.collect and capture the pylock plan it generates."""
    captured = {}
    original = pylock._write_plan

    def capture(plan_dict, path, cwd):
        captured["plan"] = copy.deepcopy(plan_dict)
        return original(plan_dict, path, cwd)

    with mock.patch.object(pylock, "_write_plan", side_effect=capture):
        manager = targeted.collect(
            target_input,
            "pip",
            wheel_dir,
            target=explicit_target,
            indexes=list(indexes),
            find_links=list(find_links),
            no_index=no_index,
            extras=list(extras),
            groups=list(groups),
            include_project=include_project,
            project=project or {},
            uv_root=uv_root or Path(target_input).parent,
        )
    return manager, captured.get("plan")


def names(plan_dict):
    return {canonicalize_name(package["name"]) for package in plan_dict["packages"]}


# --- selection rules --------------------------------------------------------


def test_selected_explicit_markers_and_sets():
    packages = [
        {"name": "windows", "marker": "sys_platform == 'win32'"},
        {"name": "linux", "marker": "sys_platform == 'linux'"},
        {"name": "extra", "marker": "'feature' in extras"},
        {"name": "group", "marker": "'test' in dependency_groups"},
    ]
    windows = Target.create(python_version="3.12", python_platform="windows", abi="cp312")
    selected = targeted._selected(plan(packages), windows, ["feature"], ["test"])
    assert {p["name"] for p in selected} == {"windows", "extra", "group"}


def test_selected_rejects_unknown_os_markers():
    with pytest.raises(ValueError, match="platform_release"):
        targeted._selected(
            plan([{"name": "bad", "marker": "sys_platform == 'linux' and platform_release == '6'"}]),
            target(),
            [],
            [],
        )


def test_selected_enforces_package_and_plan_python():
    with pytest.raises(ValueError, match="different Python"):
        targeted._selected(plan([{"name": "new", "requires-python": ">=3.13"}]), target(), [], [])
    with pytest.raises(ValueError, match="different Python"):
        targeted._selected(plan(**{"requires-python": ">=3.13"}), target(), [], [])


@pytest.mark.parametrize(
    "platform,environments,supported",
    [
        ("x86_64-pc-windows-msvc", ["sys_platform == 'win32'"], True),
        ("x86_64-manylinux_2_17", ["sys_platform == 'win32'"], False),
    ],
)
def test_selected_enforces_target_environments(platform, environments, supported):
    explicit = Target.create(python_version="3.12", python_platform=platform, abi="cp312")
    if supported:
        assert targeted._selected(plan(environments=environments), explicit, [], [])
    else:
        with pytest.raises(ValueError, match="explicit target"):
            targeted._selected(plan(environments=environments), explicit, [], [])


def test_marker_evaluation_for_target():
    linux = target()
    assert targeted._marker('sys_platform == "linux"', linux, [], [])
    assert not targeted._marker('sys_platform == "win32"', linux, [], [])
    assert targeted._marker('extra == "feature"', linux, ["feature"], [])
    assert targeted._marker('"test" in dependency_groups', linux, [], ["test"])


def test_artifact_selects_wheel_for_target_architecture(tmp_path):
    package = {
        "name": "demo",
        "version": "1",
        "wheels": [
            {"url": "https://repo/demo-1-cp312-cp312-win_amd64.whl", "hashes": {"sha256": DIGEST}},
            {
                "url": "https://repo/demo-1-cp312-cp312-manylinux_2_17_x86_64.whl",
                "hashes": {"sha256": DIGEST},
            },
            {"url": "https://repo/demo-1-py3-none-any.whl", "hashes": {"sha256": DIGEST}},
        ],
    }
    windows = Target.create(python_version="3.12", python_platform="x86_64-pc-windows-msvc", abi="cp312")
    url, _ = sources._artifact(package, tmp_path, tags=windows.tags(), wheel_only=True)
    assert url.endswith("win_amd64.whl")
    linux = Target.create(python_version="3.12", python_platform="x86_64-manylinux_2_17", abi="cp312")
    url, _ = sources._artifact(package, tmp_path, tags=linux.tags(), wheel_only=True)
    assert url.endswith("manylinux_2_17_x86_64.whl")


# --- requirement parsing ----------------------------------------------------


def test_requirement_accepts_only_prebuilt_wheels(tmp_path):
    requirement = targeted._requirement(
        "demo @ https://repo/demo-1-py3-none-any.whl", tmp_path, target(), []
    )
    assert requirement.name == "demo"
    for body in ("demo @ https://repo/demo-1.tar.gz", "./source", "demo @ git+https://repo/pkg"):
        with pytest.raises(ValueError, match="prebuilt wheels"):
            targeted._requirement(body, tmp_path, target(), [])


def test_hashes_require_sha256():
    requirement = Requirement("demo==1")
    assert targeted._hashes(f"demo==1 --hash=sha256:{DIGEST}", requirement) == {f"sha256:{DIGEST}"}
    with pytest.raises(ValueError, match="SHA256"):
        targeted._hashes("demo==1 --hash=md5:abcd", requirement)


def test_requirements_parses_includes_and_options(tmp_path):
    folder = tmp_path / "nested"
    folder.mkdir()
    nested = folder / "requirements.txt"
    nested.write_text("--index-url https://user:secret@repo.invalid/simple\ndemo==1\n", encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f'-r "{nested}"\n', encoding="utf-8")
    selected, constraints, options, _, _ = targeted._requirements(requirements, target())
    assert selected == ["demo==1"]
    assert constraints == []
    assert options == ["--index-url", "https://user:secret@repo.invalid/simple"]


def test_requirements_rejects_remote_includes(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r https://repo/requirements.txt\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Remote"):
        targeted._requirements(requirements, target())


def test_requirements_resolves_source_options_from_included_file(tmp_path):
    folder = tmp_path / "nested space"
    folder.mkdir()
    nested = folder / "requirements.txt"
    nested.write_text('--find-links "wheel space"\n--cert "cert space/ca.pem"\ndemo==1\n', encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"-r {nested.as_uri()}\n", encoding="utf-8")
    assert targeted._requirements(requirements, target())[2] == [
        "--find-links",
        str(folder / "wheel space"),
        "--cert",
        str(folder / "cert space" / "ca.pem"),
    ]


# --- preflight rejection before any subprocess ------------------------------


def test_free_threaded_rejected_before_subprocess(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n", encoding="utf-8")
    explicit = Target.create(python_version="3.13", python_platform="windows", abi="cp313t")
    with mock.patch.object(sources, "_run") as run, pytest.raises(ValueError, match="free-threaded"):
        targeted.collect(
            requirements,
            "pip",
            tmp_path / "wheels",
            target=explicit,
            indexes=[],
            find_links=[],
            no_index=False,
            extras=[],
            groups=[],
            include_project=False,
            project={},
            uv_root=tmp_path,
        )
    run.assert_not_called()


def test_root_build_rejected_for_explicit_target(tmp_path):
    root = write_project(tmp_path, extra='[build-system]\nrequires=[]\nbuild-backend="evil"\n')
    project = sources._toml(root / "pyproject.toml")
    with (
        mock.patch.object(sources, "_run") as run,
        pytest.raises(ValueError, match="dependencies-only|project-wheel"),
    ):
        targeted.collect(
            root / "pyproject.toml",
            "pip",
            tmp_path / "wheels",
            target=target(),
            indexes=[],
            find_links=[],
            no_index=False,
            extras=[],
            groups=[],
            include_project=True,
            project=project,
            uv_root=root,
        )
    run.assert_not_called()


def test_dynamic_dependencies_rejected(tmp_path):
    root = write_project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "1"\ndynamic = ["dependencies"]\n', encoding="utf-8"
    )
    project = sources._toml(root / "pyproject.toml")
    with mock.patch.object(sources, "_run") as run, pytest.raises(ValueError, match="dynamic"):
        targeted.collect(
            root / "pyproject.toml",
            "pip",
            tmp_path / "wheels",
            target=target(),
            indexes=[],
            find_links=[],
            no_index=False,
            extras=[],
            groups=[],
            include_project=False,
            project=project,
            uv_root=root,
        )
    run.assert_not_called()


# --- end-to-end target resolution -------------------------------------------


def requirements(tmp_path, body):
    path = tmp_path / "requirements.txt"
    path.write_text(body, encoding="utf-8")
    return path


def test_target_selects_windows_wheel(tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo", tag="cp312-cp312-win_amd64", purelib=False)
    build_wheel(links, "demo", tag="cp312-cp312-manylinux_2_17_x86_64", purelib=False)
    req = requirements(tmp_path, f"--no-index\n--find-links {links}\ndemo==1\n")
    explicit = Target.create(python_version="3.12", python_platform="x86_64-pc-windows-msvc", abi="cp312")
    manager, generated = collect_target(
        req, explicit, tmp_path / "wheels", find_links=[str(links)], no_index=True
    )
    assert manager == "pip"
    assert [p.name for p in (tmp_path / "wheels").iterdir()] == ["demo-1-cp312-cp312-win_amd64.whl"]
    assert generated["requires-python"] == ">=3.12"
    assert names(generated) == {"demo"}


def test_target_selects_linux_wheel(tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo", tag="cp312-cp312-win_amd64", purelib=False)
    build_wheel(links, "demo", tag="cp312-cp312-manylinux_2_17_x86_64", purelib=False)
    req = requirements(tmp_path, f"--no-index\n--find-links {links}\ndemo==1\n")
    explicit = Target.create(python_version="3.12", python_platform="x86_64-manylinux_2_17", abi="cp312")
    _, generated = collect_target(
        req, explicit, tmp_path / "wheels", find_links=[str(links)], no_index=True
    )
    assert [p.name for p in (tmp_path / "wheels").iterdir()] == [
        "demo-1-cp312-cp312-manylinux_2_17_x86_64.whl"
    ]
    assert generated["requires-python"] == ">=3.12"


def test_target_filters_platform_conditional_dependencies(tmp_path):
    links = tmp_path / "links"
    build_wheel(
        links,
        "demo",
        requires=("winonly; sys_platform == 'win32'", "nixonly; sys_platform == 'linux'"),
    )
    build_wheel(links, "winonly")
    build_wheel(links, "nixonly")
    req = requirements(tmp_path, f"--no-index\n--find-links {links}\ndemo==1\n")
    windows = Target.create(python_version="3.12", python_platform="x86_64-pc-windows-msvc", abi="cp312")
    _, win_plan = collect_target(
        req, windows, tmp_path / "wheels-win", find_links=[str(links)], no_index=True
    )
    assert names(win_plan) == {"demo", "winonly"}
    linux = Target.create(python_version="3.12", python_platform="x86_64-manylinux_2_17", abi="cp312")
    _, nix_plan = collect_target(
        req, linux, tmp_path / "wheels-nix", find_links=[str(links)], no_index=True
    )
    assert names(nix_plan) == {"demo", "nixonly"}


def test_target_preserves_patch_python_version(tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo")
    req = requirements(tmp_path, f"--no-index\n--find-links {links}\ndemo==1\n")
    explicit = Target.create(
        python_version="3.12.7", python_platform="x86_64-manylinux_2_17", abi="cp312"
    )
    _, generated = collect_target(
        req, explicit, tmp_path / "wheels", find_links=[str(links)], no_index=True
    )
    assert generated["requires-python"] == ">=3.12.7"


def test_target_rejects_incompatible_requires_python(tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo", requires_python=">=3.13")
    req = requirements(tmp_path, f"--no-index\n--find-links {links}\ndemo==1\n")
    with pytest.raises(ValueError):
        collect_target(
            req, target(version="3.12"), tmp_path / "wheels", find_links=[str(links)], no_index=True
        )


def test_target_remote_index(tmp_path, index):
    wheel = build_wheel(tmp_path / "built", "demo")
    index.publish(wheel)
    req = requirements(tmp_path, "demo==1\n")
    explicit = Target.create(python_version="3.12", python_platform="x86_64-manylinux_2_17", abi="cp312")
    _, generated = collect_target(req, explicit, tmp_path / "wheels", indexes=[index.url])
    assert names(generated) == {"demo"}
    urls = [
        artifact.get("url", "")
        for package in generated["packages"]
        for artifact in package.get("wheels", [])
    ]
    assert any(url.startswith(index.url) for url in urls)
    assert index.downloads
