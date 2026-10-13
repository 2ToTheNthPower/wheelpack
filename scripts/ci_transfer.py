"""Produce transferable ZIPs and test downloaded ZIPs without network access.

Artifacts contain bundle.zip and producer.json. Upload native/ as
native-zip-${producer}, and each ${key}/py${minor}/ grouped under one
target-zip-${key}-${producer} artifact. Download without merging artifacts.
`targets` prints a JSON array with id, key, python, and python_platform fields.
Consumers write results.json and junit.xml, including failures and rejections.
"""

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import struct
import subprocess
import sys
import sysconfig
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

TARGETS = {
    "linux-x64": "x86_64-manylinux_2_28",
    "linux-arm64": "aarch64-manylinux_2_28",
    "macos-x64": "x86_64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
    "windows-x64": "x86_64-pc-windows-msvc",
    "windows-arm64": "aarch64-pc-windows-msvc",
    "musl-x64": "x86_64-unknown-linux-musl",
    "musl-arm64": "aarch64-unknown-linux-musl",
}
PYTHONS = ("3.11", "3.12", "3.13", "3.14")
ROOT = Path(__file__).resolve().parents[1]

AUDIT_HOOK = """
import sys
def deny_network(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                 'socket.sendto', 'socket.sendmsg'):
        print('NETWORK FORBIDDEN: ' + event, file=sys.stderr, flush=True)
        raise RuntimeError('network forbidden by transfer test')
sys.addaudithook(deny_network)
"""
INSTALL_HARNESS = AUDIT_HOOK + f"""
import runpy, subprocess
from pathlib import PurePosixPath
real_run = subprocess.run
hook = {AUDIT_HOOK!r}
def guarded_run(args, *positional, **kwargs):
    args = list(args)
    loader = PurePosixPath(args[0])
    if (len(args) == 1 and loader.is_absolute()
            and loader.name.startswith('ld-musl-') and loader.name.endswith('.so.1')):
        return real_run(args, *positional, **kwargs)
    assert args[1:3] in (['-I', '-c'], ['-I', '-m']), args
    if args[2] == '-m':
        module = args[3]
        args[2:4] = ['-c', 'import runpy; runpy.run_module(' + repr(module) +
                     ', run_name="__main__")']
    args[3] = hook + '\\n' + args[3]
    return real_run(args, *positional, **kwargs)
subprocess.run = guarded_run
installer = sys.argv.pop(1)
sys.argv[0] = installer
runpy.run_path(installer, run_name='__main__')
"""
SMOKE = AUDIT_HOOK + """
import importlib.machinery, importlib.metadata
import transfer_sample, msgpack, msgpack._cmsgpack
assert transfer_sample.__version__ == '0.1.0'
assert importlib.metadata.version('transfer-sample') == '0.1.0'
assert any(msgpack._cmsgpack.__file__.endswith(suffix)
           for suffix in importlib.machinery.EXTENSION_SUFFIXES)
assert importlib.metadata.version('msgpack') == '1.2.3'
value = {'transfer': [1, 'native', True]}
assert msgpack.unpackb(msgpack.packb(value), raw=False) == value
"""


def catalog(keys=None, pythons=None):
    return [
        {"id": f"{key}-py{minor}", "key": key, "python": minor,
         "python_platform": TARGETS[key]}
        for key in dict.fromkeys(keys or TARGETS)
        for minor in dict.fromkeys(pythons or PYTHONS)
    ]


def runtime():
    os_name = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(
        sys.platform, sys.platform
    )
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if sys.platform == "win32":
        arch = {"win-amd64": "x86_64", "win-arm64": "aarch64"}.get(
            sysconfig.get_platform(), "unknown"
        )
    libc = ["", ""]
    if sys.platform == "linux":
        libc = ["unknown", ""]
        try:
            probe = ctypes.CDLL(None).gnu_get_libc_version
            probe.restype = ctypes.c_char_p
            libc = ["glibc", probe().decode("ascii")]
        except AttributeError:
            for loader in Path("/lib").glob("ld-musl-*.so.1"):
                result = subprocess.run(
                    [str(loader)], capture_output=True, text=True, timeout=5, check=False
                )
                match = re.search(r"^Version (\d+\.\d+\.\d+)\s*$", result.stderr, re.MULTILINE)
                if match:
                    libc = ["musl", match[1]]
                    break
    return {
        "os": os_name, "arch": arch,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "version": platform.python_version(), "implementation": sys.implementation.name,
        "pointer_bits": struct.calcsize("P") * 8,
        "platform": sysconfig.get_platform(), "soabi": sysconfig.get_config_var("SOABI") or "",
        "libc": {"glibc": "gnu", "musl": "musl", "": "none"}.get(libc[0], "unknown"),
        "libc_version": libc,
    }


def assert_runtime(args, actual):
    for key in ("os", "arch", "python", "libc"):
        expected = getattr(args, "expected_" + key)
        if actual[key] != expected:
            raise ValueError(f"Expected {key}={expected}, got {actual[key]}")
    if actual["implementation"] != "cpython" or actual["pointer_bits"] != 64:
        raise ValueError("Transfer CI requires native 64-bit CPython")
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise ValueError("Transfer CI requires GIL-enabled CPython")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def produce(args, actual):
    from wheelport import __version__, bundle, sources
    from wheelport.targets import Target

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.producer):
        raise ValueError("Producer ID must be an artifact-safe identifier")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    dist = output / "dist"
    dist.mkdir()
    sample = ROOT / "tests" / "transfer_sample"
    sources._run(
        ["uv", "build", "--wheel", "--python", sys.executable, "--out-dir", str(dist)], sample
    )
    wheels = list(dist.glob("transfer_sample-*.whl"))
    if len(wheels) != 1:
        raise ValueError("Build must produce exactly one transfer sample wheel")
    bootstrap = output / "bootstrap"
    bootstrap.mkdir()
    pip_version = importlib.metadata.version("pip")
    packaging_version = importlib.metadata.version("packaging")
    sources._run(
        [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary", ":all:",
         "--dest", str(bootstrap), f"pip=={pip_version}"], output
    )
    pip_wheels = list(bootstrap.glob("pip-*.whl"))
    if len(pip_wheels) != 1:
        raise ValueError("Bootstrap must contain exactly one pip wheel")
    requirements = output / "requirements.txt"
    requirements.write_text(
        f"transfer_sample @ {wheels[0].as_uri()}\nmsgpack==1.2.3\n"
        f"pip=={pip_version}\npackaging=={packaging_version}\n", encoding="utf-8"
    )
    native_requirements = output / "native-requirements.txt"
    native_requirements.write_text(
        "--only-binary :all:\n" + requirements.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for descriptor in [None, *catalog(args.target, args.target_python)]:
        target = Target.create(
            python_version=descriptor["python"], python_platform=descriptor["python_platform"]
        ) if descriptor else None
        # Group every Python variant of a target key under one directory so a
        # single artifact carries them, keeping the artifact count under the
        # 1000-entry limit of the artifact listing API.
        directory = output / descriptor["key"] / f"py{descriptor['python']}" if descriptor else (
            output / "native"
        )
        directory.mkdir(parents=True)
        archive = bundle.pack(
            requirements if target else native_requirements, directory / "bundle.zip",
            manager="pip", pip_wheel=pip_wheels[0],
            target=target,
        )
        with zipfile.ZipFile(archive) as zipped:
            manifest = json.loads(zipped.read("manifest.json"))
        metadata = {
            "format": 1, "producer": args.producer, "runtime": actual,
            "target_id": descriptor["id"] if descriptor else "native",
            "target_key": descriptor["key"] if descriptor else None,
            "target": manifest["target"], "version": __version__,
            "created_by": manifest["created_by"], "archive_sha256": digest(archive),
            "origin": {key: os.environ.get(key, "") for key in
                       ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID")},
        }
        write_json(directory / "producer.json", metadata)
        print(json.dumps({"producer": args.producer, "target_id": metadata["target_id"],
                          "directory": str(directory)}), flush=True)
    return 0


def native_rejection(target, actual):
    # This oracle compares metadata independently, without calling the installer.
    for key in ("platform", "implementation", "python", "soabi"):
        value = list(map(int, actual["python"].split("."))) if key == "python" else actual[key]
        if target[key] != value:
            return "incompatible target " + key + ":"
    required, provided = target["libc"], actual["libc_version"]
    compatible = required == provided
    if (required[0] == provided[0] and required[0] in ("glibc", "musl")
            and all(re.fullmatch(r"\d+(?:\.\d+)*", item[1]) for item in (required, provided))):
        versions = []
        for item in (required, provided):
            parts = list(map(int, item[1].split(".")))
            while parts and parts[-1] == 0:
                parts.pop()
            versions.append(tuple(parts))
        compatible = versions[0] <= versions[1]
    return None if compatible else "incompatible target libc:"


def read_archive(path, metadata, destination):
    if digest(path) != metadata["archive_sha256"]:
        raise ValueError("Archive SHA-256 mismatch")
    with zipfile.ZipFile(path) as archive:
        names = set()
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if (name.is_absolute() or ".." in name.parts or "\\" in member.filename
                    or ":" in member.filename or member.filename in names
                    or member.filename.rstrip("/") != name.as_posix()
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Unsafe or duplicate ZIP member: " + member.filename)
            names.add(member.filename)
        corrupt = archive.testzip()
        if corrupt:
            raise ValueError("ZIP CRC failure: " + corrupt)
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["format"] != 1 or manifest["target"] != metadata["target"]:
            raise ValueError("Manifest target/format disagrees with producer metadata")
        if manifest["created_by"] != metadata["created_by"]:
            raise ValueError("Manifest origin disagrees with producer metadata")
        if metadata["created_by"] != "wheelport " + metadata["version"]:
            raise ValueError("Producer version disagrees with manifest origin")
        archive.extractall(destination)
    return manifest


def run_offline(command, cwd):
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("PIP_", "UV_", "PYTHON"))}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1")
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=180, check=False)


def check_payload(path, metadata, actual, descriptor):
    with tempfile.TemporaryDirectory(prefix="wheelport-transfer-") as temporary:
        root = Path(temporary)
        extracted, venv = root / "bundle", root / "venv"
        manifest = read_archive(path, metadata, extracted)
        if descriptor:
            from wheelport.targets import Target

            expected = Target.create(python_version=descriptor["python"],
                                     python_platform=descriptor["python_platform"]).manifest()
            if manifest["target"] != expected or metadata["target_key"] != descriptor["key"]:
                raise ValueError("Explicit payload does not match requested target")
            rejection = None
        else:
            if manifest["target"].get("mode", "native") != "native":
                raise ValueError("Native payload contains an explicit target")
            rejection = native_rejection(manifest["target"], actual)
        result = run_offline(
            [sys.executable, "-I", "-S", "-c", INSTALL_HARNESS,
             str(extracted / "install.py"), "--venv", str(venv)], root
        )
        message = result.stdout + result.stderr
        if "NETWORK FORBIDDEN" in message:
            raise ValueError(message)
        if rejection:
            if result.returncode != 1 or rejection not in message or venv.exists():
                raise ValueError(f"Expected clean rejection {rejection!r}:\n{message}")
            return "rejected", message
        if result.returncode:
            raise ValueError("Offline installation failed:\n" + message)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        smoke = run_offline([str(python), "-I", "-c", SMOKE, metadata["version"]], root)
        message += smoke.stdout + smoke.stderr
        if smoke.returncode or "NETWORK FORBIDDEN" in message:
            raise ValueError("Installed package smoke test failed:\n" + message)
        return "installed", message


def reports(output, report):
    output.mkdir(parents=True, exist_ok=True)
    report["counts"] = {status: sum(item["status"] == status for item in report["results"])
                        for status in ("installed", "rejected", "failed")}
    write_json(output / "results.json", report)
    suite = ET.Element("testsuite", name="zip-transfer",
                       tests=str(len(report["results"]) + len(report["errors"])),
                       failures=str(report["counts"]["failed"] + len(report["errors"])))
    for item in report["results"]:
        case = ET.SubElement(suite, "testcase", classname=item["producer"], name=item["target_id"])
        if item["status"] == "failed":
            ET.SubElement(case, "failure", message="Transfer validation failed").text = item["message"]
        ET.SubElement(case, "system-out").text = item["message"]
    for error in report["errors"]:
        case = ET.SubElement(suite, "testcase", classname="preflight", name="artifact-contract")
        ET.SubElement(case, "failure", message=error).text = error
    ET.ElementTree(suite).write(output / "junit.xml", encoding="utf-8", xml_declaration=True)


def consume(args, actual, report):
    if args.producer_count <= 0:
        raise ValueError("Producer count must be positive")
    prefix = "musl" if actual["libc"] == "musl" else actual["os"]
    key = args.target or prefix + ("-x64" if actual["arch"] == "x86_64" else "-arm64")
    descriptor = catalog([key], [actual["python"]])[0]
    inferred = prefix + ("-x64" if actual["arch"] == "x86_64" else "-arm64")
    if key != inferred:
        raise ValueError("Consumer --target does not match its asserted runtime")
    report["target_id"] = descriptor["id"]
    payloads = {"native": {}, descriptor["id"]: {}}
    if not args.input.is_dir():
        raise ValueError("Input directory does not exist")
    for metadata_path in sorted(args.input.rglob("producer.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("format") != 1:
            raise ValueError("Unsupported producer metadata format")
        target_id, producer = metadata["target_id"], metadata["producer"]
        if target_id not in payloads:
            continue
        if not isinstance(producer, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", producer):
            raise ValueError("Invalid producer ID in metadata")
        if producer in payloads[target_id]:
            raise ValueError(f"Duplicate producer {producer} for {target_id}")
        payloads[target_id][producer] = (metadata_path.with_name("bundle.zip"), metadata)
    for target_id, producers in payloads.items():
        if len(producers) != args.producer_count:
            raise ValueError(f"Expected {args.producer_count} producers for {target_id}, "
                             f"got {len(producers)}")
    if set(payloads["native"]) != set(payloads[descriptor["id"]]):
        raise ValueError("Native and explicit producer IDs differ")
    for target_id, producers in payloads.items():
        for producer, (path, metadata) in sorted(producers.items()):
            try:
                status, message = check_payload(path, metadata, actual,
                                                None if target_id == "native" else descriptor)
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
                    subprocess.SubprocessError) as error:
                status, message = "failed", str(error)
            report["results"].append({"producer": producer, "target_id": target_id,
                                      "status": status, "message": message})
            print(json.dumps({"producer": producer, "target_id": target_id, "status": status}),
                  flush=True)
    return int(any(item["status"] == "failed" for item in report["results"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    targets = commands.add_parser("targets", help="Print the fixed explicit-target JSON catalog")
    producer = commands.add_parser("produce")
    producer.add_argument("--producer", required=True)
    producer.add_argument("--target", action="append", choices=TARGETS)
    producer.add_argument("--target-python", action="append", choices=PYTHONS)
    consumer = commands.add_parser("consume")
    consumer.add_argument("--producer-count", required=True, type=int)
    consumer.add_argument("--input", required=True, type=Path)
    consumer.add_argument("--target", choices=TARGETS)
    targets.add_argument("--target", action="append", choices=TARGETS)
    targets.add_argument("--target-python", action="append", choices=PYTHONS)
    for command in (producer, consumer):
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--expected-os", required=True, choices=("linux", "macos", "windows"))
        command.add_argument("--expected-arch", required=True, choices=("x86_64", "aarch64"))
        command.add_argument("--expected-python", required=True, choices=PYTHONS)
        command.add_argument("--expected-libc", required=True, choices=("gnu", "musl", "none"))
    args = parser.parse_args(argv)
    if args.command == "targets":
        print(json.dumps(catalog(args.target, args.target_python), indent=2))
        return 0
    report = {"format": 1, "runtime": {}, "results": [], "errors": []}
    try:
        actual = runtime()
        report["runtime"] = actual
        assert_runtime(args, actual)
        return produce(args, actual) if args.command == "produce" else consume(args, actual, report)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
            subprocess.SubprocessError) as error:
        report["errors"].append(str(error))
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if args.command == "consume":
            reports(args.output, report)


if __name__ == "__main__":
    raise SystemExit(main())
