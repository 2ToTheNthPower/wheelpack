"""Integration tests: generated pylocks match the requested sources and packages.

These drive the real ``uv`` and ``pip`` resolvers against local flat directories
and local HTTP indexes. No network access is required.
"""

from __future__ import annotations

import sys

import pytest
from helpers import SimpleIndex, build_wheel, digest, write_project
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from wheelport import pylock, sources


def resolve(target_input, *, manager="auto", indexes=(), find_links=(), no_index=False,
            extras=(), groups=()):
    source = pylock.detect(target_input, manager)
    return pylock.resolve(
        source,
        indexes=list(indexes),
        find_links=list(find_links),
        no_index=no_index,
        extras=list(extras),
        groups=list(groups),
    )


def names(plan):
    return {canonicalize_name(package["name"]) for package in plan["packages"]}


def urls(plan):
    return [
        wheel.get("url", wheel.get("path", ""))
        for package in plan["packages"]
        for wheel in package.get("wheels", [])
    ]


def host_version():
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def uv_lock(root):
    sources._run(["uv", "lock", "--python", sys.executable], root)


# --- local flat directories (find-links) ------------------------------------


def test_pip_project_local_flat_dir(project, tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo")
    resolution = resolve(project, manager="pip", find_links=[str(links)], no_index=True)
    assert resolution.manager == "pip"
    assert names(resolution.plan) == {"demo"}
    assert all(url.startswith(links.as_uri()) for url in urls(resolution.plan))
    assert SpecifierSet(resolution.plan["requires-python"]).contains(host_version())


def test_pip_project_local_flat_dir_omits_unrelated_wheels(project, tmp_path):
    links = tmp_path / "links"
    build_wheel(links, "demo")
    build_wheel(links, "unrelated")
    resolution = resolve(project, manager="pip", find_links=[str(links)], no_index=True)
    assert names(resolution.plan) == {"demo"}


def test_pip_requirements_local_flat_dir(tmp_path):
    links = tmp_path / "links"
    wheel = build_wheel(links, "demo")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        f"--no-index\n--find-links {links}\ndemo==1 --hash=sha256:{digest(wheel)}\n",
        encoding="utf-8",
    )
    resolution = resolve(requirements, manager="pip")
    assert names(resolution.plan) == {"demo"}
    assert urls(resolution.plan) == [wheel.as_uri()]


def test_pip_requirements_local_html_find_links(tmp_path):
    links = tmp_path / "links"
    wheel = build_wheel(links, "demo")
    page = tmp_path / "index.html"
    page.write_text(
        f'<a href="{wheel.as_uri()}#sha256={digest(wheel)}">{wheel.name}</a>', encoding="utf-8"
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        f"--no-index\n--find-links {page}\ndemo==1 --hash=sha256:{digest(wheel)}\n",
        encoding="utf-8",
    )
    resolution = resolve(requirements, manager="pip")
    assert names(resolution.plan) == {"demo"}
    assert urls(resolution.plan) == [wheel.as_uri()]


def test_no_index_without_links_fails_closed(project, tmp_path):
    with pytest.raises(ValueError):
        resolve(project, manager="pip", no_index=True)


# --- remote indexes ---------------------------------------------------------


def test_pip_project_remote_index(project, tmp_path, index):
    wheel = build_wheel(tmp_path / "built", "demo")
    published = index.publish(wheel)
    resolution = resolve(project, manager="pip", indexes=[index.url])
    assert names(resolution.plan) == {"demo"}
    assert urls(resolution.plan) == [published]
    assert index.downloads


def test_pip_project_extra_index_supplies_second_package(tmp_path, index):
    root = write_project(tmp_path, dependencies=("demo", "dep"))
    demo = build_wheel(tmp_path / "built", "demo")
    dep = build_wheel(tmp_path / "built", "dep")
    published_demo = index.publish(demo)
    extra = SimpleIndex(tmp_path / "extra-index")
    try:
        published_dep = extra.publish(dep)
        resolution = resolve(root, manager="pip", indexes=[index.url, extra.url])
    finally:
        extra.close()
    assert names(resolution.plan) == {"demo", "dep"}
    assert set(urls(resolution.plan)) == {published_demo, published_dep}


def test_pip_requirements_remote_index(tmp_path, index):
    wheel = build_wheel(tmp_path / "built", "demo")
    published = index.publish(wheel)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"demo==1 --hash=sha256:{digest(wheel)}\n", encoding="utf-8")
    resolution = resolve(requirements, manager="pip", indexes=[index.url])
    assert names(resolution.plan) == {"demo"}
    assert urls(resolution.plan) == [published]


# --- uv lock / project inputs -----------------------------------------------


def uv_project(tmp_path, *, dependencies=("demo",), extra="", wheels=("demo",)):
    root = write_project(tmp_path, dependencies=dependencies)
    links = root / "links"
    for name in wheels:
        build_wheel(links, name)
    with (root / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write('[tool.uv]\nno-index = true\nfind-links = ["links"]\n')
        stream.write(extra)
    uv_lock(root)
    return root


def test_uv_project_flat_dir_packages_and_python(tmp_path):
    root = uv_project(tmp_path)
    resolution = resolve(root, manager="uv", no_index=True, find_links=[str(root / "links")])
    assert resolution.manager == "uv"
    assert names(resolution.plan) == {"demo"}
    assert resolution.plan["requires-python"] == ">=3.11"


def test_uv_project_preserves_requires_python_range(tmp_path):
    root = write_project(tmp_path, requires_python=">=3.11,<3.13")
    links = root / "links"
    build_wheel(links, "demo")
    with (root / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write('[tool.uv]\nno-index = true\nfind-links = ["links"]\n')
    uv_lock(root)
    resolution = resolve(root, manager="uv", no_index=True, find_links=[str(links)])
    assert SpecifierSet(resolution.plan["requires-python"]) == SpecifierSet(">=3.11,<3.13")


def test_uv_project_extras_and_groups(tmp_path):
    root = write_project(tmp_path, dependencies=("demo",))
    links = root / "links"
    for name in ("demo", "optional", "groupdep"):
        build_wheel(links, name)
    with (root / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '[project.optional-dependencies]\nfeature = ["optional"]\n'
            '[dependency-groups]\ntest = ["groupdep"]\n'
            '[tool.uv]\nno-index = true\nfind-links = ["links"]\n'
        )
    uv_lock(root)
    resolution = resolve(
        root, manager="uv", no_index=True, find_links=[str(links)],
        extras=["feature"], groups=["test"],
    )
    assert names(resolution.plan) == {"demo", "optional", "groupdep"}


def test_uv_project_omits_unselected_extra_and_group(tmp_path):
    root = write_project(tmp_path, dependencies=("demo",))
    links = root / "links"
    for name in ("demo", "optional", "groupdep"):
        build_wheel(links, name)
    with (root / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '[project.optional-dependencies]\nfeature = ["optional"]\n'
            '[dependency-groups]\ntest = ["groupdep"]\n'
            '[tool.uv]\nno-index = true\nfind-links = ["links"]\n'
        )
    uv_lock(root)
    resolution = resolve(root, manager="uv", no_index=True, find_links=[str(links)])
    assert names(resolution.plan) == {"demo"}


def test_uv_universal_lock_filters_host_platform_markers(tmp_path):
    root = write_project(tmp_path, dependencies=("demo",))
    links = root / "links"
    build_wheel(
        links,
        "demo",
        requires=("winonly; sys_platform == 'win32'", "nixonly; sys_platform == 'linux'"),
    )
    build_wheel(links, "winonly")
    build_wheel(links, "nixonly")
    with (root / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write('[tool.uv]\nno-index = true\nfind-links = ["links"]\n')
    uv_lock(root)
    resolution = resolve(root, manager="uv", no_index=True, find_links=[str(links)])
    selected = {
        canonicalize_name(package["name"])
        for package in pylock._selected(resolution.plan, [], [])
    }
    if sys.platform == "win32":
        assert selected == {"demo", "winonly"}
    elif sys.platform.startswith("linux"):
        assert selected == {"demo", "nixonly"}
    else:
        assert selected == {"demo"}


def test_uv_lock_python_marker_filtered_for_host(tmp_path):
    root = write_project(
        tmp_path, dependencies=("demo", "newdep; python_full_version >= '3.13'")
    )
    links = root / "links"
    build_wheel(links, "demo")
    build_wheel(links, "newdep", requires_python=">=3.13")
    with (root / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write('[tool.uv]\nno-index = true\nfind-links = ["links"]\n')
    uv_lock(root)
    resolution = resolve(root, manager="uv", no_index=True, find_links=[str(links)])
    selected = {
        canonicalize_name(package["name"])
        for package in pylock._selected(resolution.plan, [], [])
    }
    expected = {"demo", "newdep"} if sys.version_info >= (3, 13) else {"demo"}
    assert selected == expected


def test_uv_workspace_member_uses_shared_lock(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    links = root / "links"
    for name in ("demo", "rootdep", "unrelateddep"):
        build_wheel(links, name)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "workspace-root"\nversion = "1"\n'
        'requires-python = ">=3.11"\ndependencies = ["rootdep"]\n'
        '[tool.uv]\nno-index = true\nfind-links = ["links"]\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
        "[tool.uv.sources]\nsibling = { workspace = true }\n",
        encoding="utf-8",
    )
    member = root / "packages" / "member"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text(
        '[project]\nname = "member"\nversion = "1"\n'
        'requires-python = ">=3.11"\ndependencies = ["demo", "sibling"]\n',
        encoding="utf-8",
    )
    sibling = root / "packages" / "sibling"
    sibling.mkdir()
    (sibling / "pyproject.toml").write_text(
        '[project]\nname = "sibling"\nversion = "1"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    unrelated = root / "packages" / "unrelated"
    unrelated.mkdir()
    (unrelated / "pyproject.toml").write_text(
        '[project]\nname = "unrelated"\nversion = "1"\n'
        'requires-python = ">=3.11"\ndependencies = ["unrelateddep"]\n',
        encoding="utf-8",
    )
    uv_lock(root)
    resolution = resolve(member, manager="auto", no_index=True, find_links=[str(links)])
    assert resolution.manager == "uv"
    assert names(resolution.plan) == {"demo", "sibling"}
    assert "member" not in names(resolution.plan)
    assert "rootdep" not in names(resolution.plan)
    assert "unrelateddep" not in names(resolution.plan)


# --- error handling ---------------------------------------------------------


def test_extras_require_project_input(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Extras and groups require a project input"):
        resolve(requirements, manager="pip", extras=["feature"])


def test_uv_manager_requires_project(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pyproject.toml"):
        pylock.detect(requirements, "uv")
