"""Foreign targets do not inherit host wheel tags or marker values."""

import json
import os
import sys
from dataclasses import FrozenInstanceError, is_dataclass
from unittest import mock

import pytest
from packaging.markers import Marker, default_environment
from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms

from wheelport import targets
from wheelport.targets import Target


def target(platform="x86_64-manylinux_2_28", version="3.12.7", **kwargs):
    return Target.create(python_version=version, python_platform=platform, **kwargs)


@pytest.mark.parametrize(
    "version,full,python,abi",
    [
        ("3.11", "3.11.0", (3, 11), "cp311"),
        ("3.12.7", "3.12.7", (3, 12), "cp312"),
        ("3.13", "3.13.0", (3, 13), "cp313"),
    ],
)
def test_versions(version, full, python, abi):
    result = target(version=version)
    assert result.python_version == full
    assert result.python == python
    assert result.abi == abi
    assert result.marker_environment["python_full_version"] == full
    assert result.marker_environment["implementation_version"] == full


def test_native_python_version():
    result = Target.create(python_platform="win_amd64")
    assert result.python_version == ".".join(map(str, sys.version_info[:3]))


@pytest.mark.parametrize(
    "version",
    [
        "",
        "3",
        "3.10",
        "2.7.18",
        "4.0",
        "3.12.1.0",
        "3.12t",
        "3.13rc1",
        "3.12.0rc1",
        "03.12",
        "3.012",
        "3.12.00",
        " 3.12",
        "3.12\n",
        "3.12 --no-index",
        "\u0663.\u0661\u0662",
        312,
    ],
)
def test_invalid_versions(version):
    with pytest.raises(ValueError, match="CPython"):
        target(version=version)


@pytest.mark.parametrize(
    "abi",
    ["cp311", "cp312t", "abi3", "none", "pp312", "cp312d", "cp312 --no-index", "cp312\n", "", 312],
)
def test_invalid_abis(abi):
    with pytest.raises(ValueError, match="ABI"):
        target(abi=abi)


def test_non_cpython_native_version(monkeypatch):
    monkeypatch.setattr(targets.sys, "implementation", mock.Mock(name="implementation"))
    targets.sys.implementation.name = "pypy"
    with pytest.raises(ValueError, match="Only CPython"):
        Target.create(python_platform="win_amd64")
    assert target().marker_environment["implementation_name"] == "cpython"


@pytest.mark.parametrize(
    "name,canonical,first,machine,sys_platform,system,os_name",
    [
        ("windows", "x86_64-pc-windows-msvc", "win_amd64", "AMD64", "win32", "Windows", "nt"),
        ("win_amd64", "x86_64-pc-windows-msvc", "win_amd64", "AMD64", "win32", "Windows", "nt"),
        ("win_arm64", "aarch64-pc-windows-msvc", "win_arm64", "ARM64", "win32", "Windows", "nt"),
        ("win32", "i686-pc-windows-msvc", "win32", "x86", "win32", "Windows", "nt"),
        (
            "linux",
            "x86_64-manylinux_2_17",
            "manylinux_2_17_x86_64",
            "x86_64",
            "linux",
            "Linux",
            "posix",
        ),
        (
            "aarch64-unknown-linux-gnu",
            "aarch64-manylinux_2_17",
            "manylinux_2_17_aarch64",
            "aarch64",
            "linux",
            "Linux",
            "posix",
        ),
        (
            "manylinux_2_28_aarch64",
            "aarch64-manylinux_2_28",
            "manylinux_2_28_aarch64",
            "aarch64",
            "linux",
            "Linux",
            "posix",
        ),
        (
            "manylinux2014_x86_64",
            "x86_64-manylinux_2_17",
            "manylinux_2_17_x86_64",
            "x86_64",
            "linux",
            "Linux",
            "posix",
        ),
        (
            "x86_64-manylinux2014",
            "x86_64-manylinux_2_17",
            "manylinux_2_17_x86_64",
            "x86_64",
            "linux",
            "Linux",
            "posix",
        ),
        (
            "musllinux_1_2_x86_64",
            "x86_64-unknown-linux-musl",
            "musllinux_1_2_x86_64",
            "x86_64",
            "linux",
            "Linux",
            "posix",
        ),
        (
            "aarch64-unknown-linux-musl",
            "aarch64-unknown-linux-musl",
            "musllinux_1_2_aarch64",
            "aarch64",
            "linux",
            "Linux",
            "posix",
        ),
        (
            "macos",
            "aarch64-apple-darwin",
            "macosx_11_0_arm64",
            "arm64",
            "darwin",
            "Darwin",
            "posix",
        ),
        (
            "macosx_11_0_arm64",
            "aarch64-apple-darwin",
            "macosx_11_0_arm64",
            "arm64",
            "darwin",
            "Darwin",
            "posix",
        ),
        (
            "macosx_11_0_x86_64",
            "x86_64-apple-darwin",
            "macosx_11_0_x86_64",
            "x86_64",
            "darwin",
            "Darwin",
            "posix",
        ),
    ],
)
def test_platform_normalization(name, canonical, first, machine, sys_platform, system, os_name):
    expected_keys = set(default_environment()) | {"extra"}
    with (
        mock.patch.object(targets.platform, "machine", side_effect=AssertionError("host probe")),
        mock.patch.object(targets, "_libc", side_effect=AssertionError("host probe")),
    ):
        result = target(name)
        assert result.python_platform == canonical
        assert result.platforms[0] == first
        env = result.marker_environment
        assert env["platform_machine"] == machine
        assert env["sys_platform"] == sys_platform
        assert env["platform_system"] == system
        assert env["os_name"] == os_name
        assert env["platform_python_implementation"] == "CPython"
        assert env["platform_release"] == env["platform_version"] == env["extra"] == ""
        assert set(env) == expected_keys
        assert all(isinstance(value, str) for value in env.values())


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
@pytest.mark.parametrize("minor", [17, 28, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40])
def test_all_pinned_glibc_platforms(arch, minor):
    result = target(f"{arch}-manylinux_2_{minor}")
    assert result.python_platform == f"{arch}-manylinux_2_{minor}"
    assert target(f"manylinux_2_{minor}_{arch}") == result
    assert result.platforms[0] == f"manylinux_2_{minor}_{arch}"
    assert not any(p.startswith("linux_") for p in result.platforms)
    assert not any(p.startswith("musllinux_") for p in result.platforms)
    assert len(result.platforms) == len(set(result.platforms))


def test_manylinux_backwards_expansion():
    platforms = target("x86_64-manylinux_2_28").platforms
    expected = []
    for minor in range(28, 4, -1):
        expected.append(f"manylinux_2_{minor}_x86_64")
        if minor in {17, 12, 5}:
            expected.append(
                {17: "manylinux2014_x86_64", 12: "manylinux2010_x86_64", 5: "manylinux1_x86_64"}[
                    minor
                ]
            )
    assert platforms == expected
    assert target("aarch64-manylinux_2_17").platforms == [
        "manylinux_2_17_aarch64",
        "manylinux2014_aarch64",
    ]


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_musl_expansion(arch):
    assert target(f"{arch}-unknown-linux-musl").platforms == [
        f"musllinux_1_2_{arch}",
        f"musllinux_1_1_{arch}",
    ]


@pytest.mark.parametrize("arch,wheel_arch", [("aarch64", "arm64"), ("x86_64", "x86_64")])
def test_macos_expansion(arch, wheel_arch):
    platforms = target(f"{arch}-apple-darwin").platforms
    assert platforms == list(mac_platforms(version=(11, 0), arch=wheel_arch))
    assert "macosx_11_0_universal2" in platforms
    assert not any(p.startswith("macosx_12_") for p in platforms)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "--no-index",
        "linux; touch /tmp/unsafe",
        "win_amd64\n",
        " windows",
        "WINDOWS",
        "x86_64-manylinux_2_18",
        "aarch64-manylinux_2_29",
        "manylinux_2_41_x86_64",
        "manylinux_2_028_x86_64",
        "manylinux1_x86_64",
        "manylinux2014_i686",
        "linux_x86_64",
        "musllinux_1_1_x86_64",
        "musllinux_1_3_aarch64",
        "musllinux_2_0_x86_64",
        "macosx_10_15_x86_64",
        "macosx_12_0_arm64",
        "macosx_11_0_universal2",
        "riscv64-unknown-linux-gnu",
        "x86_64-unknown-freebsd",
        "cp312-cp312-win_amd64",
        123,
    ],
)
def test_invalid_platforms(name):
    with pytest.raises(ValueError, match="platform|macOS"):
        target(name)


@pytest.mark.parametrize(
    "sys_platform,machine,libc,canonical",
    [
        ("linux", "x86_64", ["glibc", "2.36"], "x86_64-manylinux_2_36"),
        ("linux", "aarch64", ["glibc", "2.17.0"], "aarch64-manylinux_2_17"),
        ("linux", "aarch64", ["musl", "1.2.5"], "aarch64-unknown-linux-musl"),
        ("win32", "AMD64", None, "x86_64-pc-windows-msvc"),
        ("win32", "ARM64", None, "aarch64-pc-windows-msvc"),
        ("win32", "x86", None, "i686-pc-windows-msvc"),
        ("darwin", "arm64", None, "aarch64-apple-darwin"),
        ("darwin", "x86_64", None, "x86_64-apple-darwin"),
    ],
)
def test_native_platform(monkeypatch, sys_platform, machine, libc, canonical):
    monkeypatch.setattr(targets.sys, "platform", sys_platform)
    monkeypatch.setattr(targets.platform, "machine", lambda: machine)
    with mock.patch.object(targets, "_libc", return_value=libc) as probe:
        assert Target.create(python_version="3.12").python_platform == canonical
        assert probe.call_count == (1 if sys_platform == "linux" else 0)


def test_native_windows_32_bit(monkeypatch):
    monkeypatch.setattr(targets.sys, "platform", "win32")
    monkeypatch.setattr(targets.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(targets.struct, "calcsize", lambda _: 4)
    assert Target.create(python_version="3.12").python_platform == "i686-pc-windows-msvc"


@pytest.mark.parametrize(
    "libc",
    [
        ["unknown", ""],
        ["glibc", "2.27"],
        ["glibc", "2.41"],
        ["glibc", "3.17"],
        ["glibc", "bad"],
        ["musl", "1.1.24"],
        ["musl", "1.3.0"],
        ["other", "2.17"],
    ],
)
def test_native_libc_requires_exact_supported_baseline(monkeypatch, libc):
    monkeypatch.setattr(targets.sys, "platform", "linux")
    monkeypatch.setattr(targets.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(targets, "_libc", lambda: libc)
    with pytest.raises(ValueError, match="--python-platform"):
        Target.create(python_version="3.12")


@pytest.mark.parametrize("sys_platform,machine", [("linux", "ppc64le"), ("freebsd", "x86_64")])
def test_unsupported_native_platform(monkeypatch, sys_platform, machine):
    monkeypatch.setattr(targets.sys, "platform", sys_platform)
    monkeypatch.setattr(targets.platform, "machine", lambda: machine)
    with pytest.raises(ValueError, match="--python-platform"):
        Target.create(python_version="3.12")


def test_marker_evaluation_and_extras():
    result = target("win_arm64")
    assert result.evaluate_marker('sys_platform == "win32" and platform_machine == "ARM64"')
    assert not result.evaluate_marker('sys_platform == "linux"')
    assert result.evaluate_marker('python_full_version > "3.12.6" and python_version == "3.12"')
    assert result.evaluate_marker(Marker('implementation_name == "cpython"'))
    assert not result.evaluate_marker('extra == "feature"')
    assert result.evaluate_marker('extra == "feature"', extras={"other", "feature"})
    assert result.evaluate_marker('extra == ""', extras={"feature"})
    assert result.evaluate_marker('extra == "my-feature"', extras={"my_feature"})
    assert not result.evaluate_marker('extra == "absent"', extras={"other", "feature"})
    assert result.marker_environment["extra"] == ""


@pytest.mark.parametrize(
    "expression",
    [
        'platform_release == "6.1"',
        '"10" <= platform_version',
        'sys_platform == "linux" or (extra == "x" and platform_version == "10")',
        'platform_release != ""',
    ],
)
def test_unknown_os_markers_rejected(expression):
    with pytest.raises(ValueError, match="OS release/version"):
        target().evaluate_marker(expression, extras={"x"})


def test_os_field_name_in_literal_is_not_rejected():
    assert target().evaluate_marker('extra == "platform_release"', extras={"platform_release"})


def test_tag_generation():
    result = target()
    tags = list(result.tags())
    assert tags == [
        *cpython_tags(python_version=(3, 12), abis=["cp312"], platforms=result.platforms),
        *compatible_tags(python_version=(3, 12), interpreter="cp312", platforms=result.platforms),
    ]
    assert len(tags) == len(set(tags))
    assert Tag("cp312", "cp312", "manylinux_2_28_x86_64") in tags
    assert Tag("cp311", "abi3", "manylinux2014_x86_64") in tags
    assert Tag("py3", "none", "any") in tags
    assert Tag("cp313", "cp313", "manylinux_2_28_x86_64") not in tags
    assert Tag("cp312", "cp312", "manylinux_2_29_x86_64") not in tags
    assert Tag("cp312", "cp312", "linux_x86_64") not in tags
    assert Tag("cp312", "cp312", "win_amd64") not in tags


def test_free_threaded_tags_and_resolver_limitation():
    result = target("win_amd64", "3.13.2", abi="cp313t")
    tags = set(result.tags())
    assert Tag("cp313", "cp313t", "win_amd64") in tags
    assert Tag("cp313", "cp313", "win_amd64") not in tags
    assert all(tag.abi != "abi3" for tag in tags)
    assert Tag("py3", "none", "any") in tags
    assert result.download_args()[4:6] == ["--abi", "cp313t"]
    # These mandated uv flags cannot signal free-threading. The source adapter
    # must reject this ABI until it can enforce the resolver's ABI selection.
    assert result.resolver_args() == target("win_amd64", "3.13.2").resolver_args()


def test_args_manifest_and_immutability():
    result = target()
    assert is_dataclass(result)
    with pytest.raises(FrozenInstanceError):
        result.abi = "cp313"
    assert result.resolver_args() == [
        "--python-version",
        "3.12.7",
        "--python-platform",
        "x86_64-manylinux_2_28",
        "--only-binary",
        ":all:",
        "--no-build",
    ]
    assert result.download_args() == [
        "--python-version",
        "3.12.7",
        "--implementation",
        "cp",
        "--abi",
        "cp312",
        *(arg for platform in result.platforms for arg in ("--platform", platform)),
    ]
    expected = {
        "mode": "explicit",
        "implementation": "cpython",
        "python": [3, 12],
        "python_full_version": "3.12.7",
        "python_patch": 7,
        "abi": "cp312",
        "python_platform": "x86_64-manylinux_2_28",
        "platforms": result.platforms,
        "marker_environment": result.marker_environment,
    }
    assert json.loads(json.dumps(result.manifest())) == expected
    result.platforms.clear()
    result.marker_environment["sys_platform"] = "host"
    manifest = result.manifest()
    manifest["python"][0] = 9
    manifest["platforms"].clear()
    manifest["marker_environment"].clear()
    result.resolver_args().clear()
    result.download_args().clear()
    assert result.manifest() == expected
    assert hash(result) == hash(target())


def test_no_system_mutation():
    before = dict(os.environ)
    with (
        mock.patch.object(targets.platform, "machine", side_effect=AssertionError("host probe")),
        mock.patch.object(targets, "_libc", side_effect=AssertionError("host probe")),
    ):
        result = target("macos")
        result.resolver_args()
        result.download_args()
        result.manifest()
        list(result.tags())
    assert result.evaluate_marker('platform_system == "Darwin"')
    assert dict(os.environ) == before
