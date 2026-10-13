"""Transfer CLI contracts, archive integrity, and network-denied subprocesses."""

import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from wheelport.targets import Target

spec = importlib.util.spec_from_file_location(
    "ci_transfer", Path(__file__).resolve().parents[1] / "scripts" / "ci_transfer.py"
)
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


def actual():
    return {
        "os": "linux", "arch": "x86_64", "python": "3.11", "version": "3.11.9",
        "implementation": "cpython", "pointer_bits": 64, "platform": "linux-x86_64",
        "soabi": "cpython-311-x86_64-linux-gnu", "libc": "gnu",
        "libc_version": ["glibc", "2.36"],
    }


def native_target():
    host = actual()
    return {"implementation": host["implementation"], "python": [3, 11],
            "platform": host["platform"], "soabi": host["soabi"],
            "libc": host["libc_version"]}


def flags():
    return ["--expected-os", "linux", "--expected-arch", "x86_64",
            "--expected-python", "3.11", "--expected-libc", "gnu"]


def payload(root, producer="one", target_id="native", members=None):
    folder = root / f"{target_id}-{producer}"
    folder.mkdir(parents=True)
    target = native_target() if target_id == "native" else Target.create(
        python_version="3.11", python_platform=transfer.TARGETS["linux-x64"]
    ).manifest()
    manifest = {"format": 1, "target": target, "created_by": "wheelport 0.1.0"}
    archive = folder / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("manifest.json", json.dumps(manifest))
        zipped.writestr("install.py", "pass\n")
        for name, value in (members or {}).items():
            # ZipInfo normalizes os.sep to "/" on Windows; restore the raw name so
            # the archive keeps backslash and other unsafe spellings verbatim.
            info = zipfile.ZipInfo(name)
            info.filename = name
            zipped.writestr(info, value)
    metadata = {
        "format": 1, "producer": producer, "target_id": target_id,
        "target_key": None if target_id == "native" else "linux-x64",
        "target": target, "archive_sha256": transfer.digest(archive),
        "created_by": "wheelport 0.1.0", "version": "0.1.0", "runtime": actual(),
    }
    transfer.write_json(folder / "producer.json", metadata)
    return archive, metadata


def test_catalog_fixed_32_outputs(capsys):
    assert transfer.main(["targets"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert len(catalog) == 32
    assert len({item["id"] for item in catalog}) == 32
    assert {item["key"] for item in catalog} == {
        "linux-x64", "linux-arm64", "macos-x64", "macos-arm64",
        "windows-x64", "windows-arm64", "musl-x64", "musl-arm64",
    }
    for item in catalog:
        assert item["id"] == f"{item['key']}-py{item['python']}"
        target = Target.create(python_version=item["python"],
                               python_platform=item["python_platform"])
        assert target.python_patch is None
        assert target.python_version == item["python"] + ".0"
    assert transfer.TARGETS["linux-x64"] == "x86_64-manylinux_2_28"


def test_catalog_local_limits_and_deduplication(capsys):
    assert transfer.main(["targets", "--target", "musl-arm64", "--target", "musl-arm64",
                          "--target-python", "3.14"]) == 0
    assert json.loads(capsys.readouterr().out) == [{
        "id": "musl-arm64-py3.14", "key": "musl-arm64", "python": "3.14",
        "python_platform": "aarch64-unknown-linux-musl",
    }]


@pytest.mark.parametrize("key,value", [
    ("os", "windows"), ("arch", "aarch64"), ("python", "3.12"), ("libc", "musl"),
    ("implementation", "pypy"), ("pointer_bits", 32),
])
def test_runtime_preflight(key, value):
    host = actual()
    host[key] = value
    args = Namespace(expected_os="linux", expected_arch="x86_64",
                     expected_python="3.11", expected_libc="gnu")
    with pytest.raises(ValueError):
        transfer.assert_runtime(args, host)


@pytest.mark.parametrize("key,value,reason", [
    ("platform", "win-amd64", "platform"),
    ("implementation", "pypy", "implementation"),
    ("python", [3, 12], "python"),
    ("soabi", "other", "soabi"),
    ("libc", ["musl", "1.2.5"], "libc"),
    ("libc", ["glibc", "2.37"], "libc"),
])
def test_independent_native_rejection(key, value, reason):
    target = native_target()
    target[key] = value
    assert transfer.native_rejection(target, actual()) == f"incompatible target {reason}:"


@pytest.mark.parametrize("version", ["2.28", "2.36", "2.36.0"])
def test_native_libc_backwards_compatible(version):
    target = native_target()
    target["libc"] = ["glibc", version]
    assert transfer.native_rejection(target, actual()) is None


def test_rejection_order_platform_before_python():
    target = native_target()
    target.update(platform="win-amd64", python=[3, 14])
    assert transfer.native_rejection(target, actual()) == "incompatible target platform:"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "a\\escape",
                                  "./install.py", "a//install.py"])
def test_archive_rejects_unsafe_paths(tmp_path, name):
    if os.name == "nt" and "\\" in name:
        # zipfile normalizes os.sep to "/" while reading on Windows, so the raw
        # backslash never reaches validation and the normalized name is safe.
        pytest.skip("Windows zipfile rewrites backslashes before validation")
    archive, metadata = payload(tmp_path, members={name: "bad"})
    with pytest.raises(ValueError, match="Unsafe"):
        transfer.read_archive(archive, metadata, tmp_path / "extracted")
    assert not (tmp_path / "extracted").exists()


def test_archive_rejects_symlink(tmp_path):
    archive, metadata = payload(tmp_path)
    with zipfile.ZipFile(archive, "a") as zipped:
        member = zipfile.ZipInfo("link")
        member.external_attr = (0o120777 << 16)
        zipped.writestr(member, "outside")
    metadata["archive_sha256"] = transfer.digest(archive)
    with pytest.raises(ValueError, match="Unsafe"):
        transfer.read_archive(archive, metadata, tmp_path / "extracted")


def test_archive_rejects_duplicates(tmp_path):
    archive, metadata = payload(tmp_path)
    with zipfile.ZipFile(archive, "a") as zipped, pytest.warns(UserWarning):
        zipped.writestr("install.py", "pass")
    metadata["archive_sha256"] = transfer.digest(archive)
    with pytest.raises(ValueError, match="duplicate"):
        transfer.read_archive(archive, metadata, tmp_path / "extracted")


@pytest.mark.parametrize("field,value,match", [
    ("archive_sha256", "0" * 64, "SHA-256"),
    ("target", {}, "target/format"),
    ("created_by", "wheelport other", "origin"),
    ("version", "other", "version"),
])
def test_archive_metadata_integrity(tmp_path, field, value, match):
    archive, metadata = payload(tmp_path)
    metadata[field] = value
    with pytest.raises(ValueError, match=match):
        transfer.read_archive(archive, metadata, tmp_path / "extracted")


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("operation,event", [
    ("socket.getaddrinfo('localhost', 9)", "socket.getaddrinfo"),
    ("socket.socket().connect(('127.0.0.1', 9))", "socket.connect"),
    ("socket.socket(type=socket.SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 9))", "socket.sendto"),
])
def test_network_guard_parent_and_child(tmp_path, child, operation, event):
    code = "import socket; " + operation
    if child:
        installer = tmp_path / "install.py"
        installer.write_text("import subprocess, sys\n"
                             f"subprocess.run([sys.executable, '-I', '-c', {code!r}], check=True)\n")
        command = [sys.executable, "-I", "-S", "-c", transfer.INSTALL_HARNESS, str(installer)]
    else:
        command = [sys.executable, "-I", "-S", "-c", transfer.AUDIT_HOOK + code]
    result = transfer.run_offline(command, tmp_path)
    assert result.returncode != 0
    assert "NETWORK FORBIDDEN: " + event in result.stderr


def test_installer_harness_guards_module_children(tmp_path):
    installer = tmp_path / "install.py"
    installer.write_text(
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-I', '-m', 'json.tool'], "
        "input='{}', text=True, check=True)\n"
    )
    result = transfer.run_offline(
        [sys.executable, "-I", "-S", "-c", transfer.INSTALL_HARNESS, str(installer)], tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"


def test_consumer_collects_all_failures_and_writes_reports(tmp_path, monkeypatch):
    inputs, output = tmp_path / "input", tmp_path / "output"
    for producer in ("one", "two"):
        payload(inputs, producer)
        payload(inputs, producer, "linux-x64-py3.11")
    monkeypatch.setattr(transfer, "runtime", actual)
    checker = Mock(side_effect=[("installed", "good"), ValueError("broken"),
                                ("installed", "explicit good"), ("installed", "also good")])
    monkeypatch.setattr(transfer, "check_payload", checker)
    assert transfer.main(["consume", "--producer-count", "2", "--input", str(inputs),
                          "--output", str(output), "--target", "linux-x64", *flags()]) == 1
    assert checker.call_count == 4
    report = json.loads((output / "results.json").read_text())
    assert report["counts"] == {"installed": 3, "rejected": 0, "failed": 1}
    suite = ET.parse(output / "junit.xml").getroot()
    assert suite.attrib["tests"] == "4"
    assert suite.attrib["failures"] == "1"
    assert len(suite.findall("testcase/failure")) == 1


@pytest.mark.parametrize("problem,match", [
    ("missing", "Expected 2 producers"), ("duplicate", "Duplicate producer"),
    ("different", "producer IDs differ"), ("runtime", "Expected os"),
    ("target", "does not match"), ("zero", "must be positive"),
])
def test_consumer_preflight_reports(tmp_path, monkeypatch, problem, match):
    inputs, output = tmp_path / "input", tmp_path / "output"
    for producer in ("one", "two"):
        payload(inputs, producer)
        payload(inputs, "three" if problem == "different" and producer == "two" else producer,
                "linux-x64-py3.11")
    if problem == "missing":
        (inputs / "native-two" / "producer.json").unlink()
    if problem == "duplicate":
        path = inputs / "native-two" / "producer.json"
        data = json.loads(path.read_text())
        data["producer"] = "one"
        transfer.write_json(path, data)
    host = actual()
    if problem == "runtime":
        host["os"] = "windows"
    monkeypatch.setattr(transfer, "runtime", lambda: host)
    checker = Mock()
    monkeypatch.setattr(transfer, "check_payload", checker)
    assert transfer.main([
        "consume", "--producer-count", "0" if problem == "zero" else "2",
        "--input", str(inputs), "--output", str(output), "--target",
        "windows-x64" if problem == "target" else "linux-x64", *flags(),
    ]) == 1
    checker.assert_not_called()
    report = json.loads((output / "results.json").read_text())
    assert match in report["errors"][0]
    assert ET.parse(output / "junit.xml").getroot().attrib["failures"] == "1"


@pytest.mark.parametrize("returncode,message,created,passes", [
    (1, "incompatible target python: mismatch", False, True),
    (2, "incompatible target python: mismatch", False, False),
    (1, "unrelated error", False, False),
    (1, "incompatible target python: mismatch", True, False),
    (1, "incompatible target python: NETWORK FORBIDDEN", False, False),
])
def test_expected_rejection_is_narrow(tmp_path, monkeypatch, returncode, message, created, passes):
    archive, metadata = payload(tmp_path)
    metadata["target"]["python"] = [3, 12]
    monkeypatch.setattr(transfer, "read_archive", lambda *args: {"target": metadata["target"]})

    def run(command, cwd):
        if created:
            Path(command[-1]).mkdir()
        return subprocess.CompletedProcess(command, returncode, "", message)

    monkeypatch.setattr(transfer, "run_offline", run)
    if passes:
        assert transfer.check_payload(archive, metadata, actual(), None) == ("rejected", message)
    else:
        with pytest.raises(ValueError):
            transfer.check_payload(archive, metadata, actual(), None)


def test_explicit_mismatch_fails_before_install(tmp_path, monkeypatch):
    archive, metadata = payload(tmp_path, target_id="linux-x64-py3.11")
    descriptor = transfer.catalog(["linux-x64"], ["3.12"])[0]
    runner = Mock()
    monkeypatch.setattr(transfer, "run_offline", runner)
    with pytest.raises(ValueError, match="does not match"):
        transfer.check_payload(archive, metadata, actual(), descriptor)
    runner.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
def test_success_runs_isolated_installed_package_smoke(tmp_path, monkeypatch, explicit):
    archive, metadata = payload(tmp_path, target_id="linux-x64-py3.11" if explicit else "native")
    descriptor = transfer.catalog(["linux-x64"], ["3.11"])[0] if explicit else None
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "passed\n", ""))
    monkeypatch.setattr(transfer, "run_offline", runner)
    assert transfer.check_payload(archive, metadata, actual(), descriptor) == (
        "installed", "passed\npassed\n"
    )
    assert runner.call_count == 2
    installer, smoke = [call.args[0] for call in runner.call_args_list]
    assert installer[:5] == [sys.executable, "-I", "-S", "-c", transfer.INSTALL_HARNESS]
    assert "venv" in Path(smoke[0]).parts
    assert smoke[1:] == ["-I", "-c", transfer.SMOKE, "0.1.0"]


def test_producer_layout_and_build_interpreter(tmp_path, monkeypatch):
    from wheelport import bundle, sources

    calls = []

    def run(command, cwd):
        calls.append(command)
        if command[:2] == ["uv", "build"]:
            destination = Path(command[command.index("--out-dir") + 1])
            (destination / "transfer_sample-0.1.0-py3-none-any.whl").write_bytes(b"project")
        else:
            destination = Path(command[command.index("--dest") + 1])
            (destination / "pip-26.0-py3-none-any.whl").write_bytes(b"pip")

    packs = []

    def pack(requirements, output, **kwargs):
        packs.append((requirements.read_text(), output, kwargs))
        target = kwargs["target"].manifest() if kwargs["target"] else native_target()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"target": target,
                                                         "created_by": "wheelport 0.1.0"}))
        return output

    monkeypatch.setattr(sources, "_run", run)
    monkeypatch.setattr(bundle, "pack", pack)
    monkeypatch.setattr(transfer, "runtime", actual)
    assert transfer.main(["produce", "--producer", "linux-test-py3.11",
                          "--output", str(tmp_path), "--target", "windows-arm64",
                          "--target-python", "3.14", *flags()]) == 0
    assert calls[0][calls[0].index("--python") + 1] == sys.executable
    assert len(packs) == 2
    assert "--only-binary :all:" in packs[0][0]
    assert "--only-binary" not in packs[1][0]
    assert all("msgpack==1.2.3" in requirements
               and "transfer_sample @ file:" in requirements for requirements, _, _ in packs)
    assert packs[0][2]["target"] is None
    assert packs[1][2]["target"].python_platform == "aarch64-pc-windows-msvc"
    for path, target_id in ((tmp_path / "native", "native"),
                            (tmp_path / "windows-arm64" / "py3.14", "windows-arm64-py3.14")):
        metadata = json.loads((path / "producer.json").read_text())
        assert metadata["target_id"] == target_id
        assert metadata["producer"] == "linux-test-py3.11"
        assert metadata["archive_sha256"] == transfer.digest(path / "bundle.zip")
        assert metadata["runtime"] == actual()
