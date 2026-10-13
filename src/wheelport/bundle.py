"""Build portable ZIP bundles from resolved wheels."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import sysconfig
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from . import __version__, sources
from .install import _libc
from .targets import Target


def _wheel_entry(path: Path, target: Target | None = None) -> dict:
    name, version, _, tags = parse_wheel_filename(path.name)
    if set(target.tags() if target else sys_tags()).isdisjoint(tags):
        context = "explicit target" if target else "packing interpreter"
        raise ValueError(f"Wheel is incompatible with the {context}: {path.name}")
    with zipfile.ZipFile(path) as archive:
        metadata = [item for item in archive.namelist() if item.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError(f"Wheel must contain exactly one METADATA file: {path.name}")
        message = BytesParser().parsebytes(archive.read(metadata[0]))
        if canonicalize_name(message.get("Name", "")) != name or Version(
            message.get("Version", "0")
        ) != version:
            raise ValueError(f"Wheel filename and metadata disagree: {path.name}")
        if target and not SpecifierSet(message.get("Requires-Python", "")).contains(
            target.python_version
        ):
            raise ValueError(f"Wheel {path.name} requires a different target Python version")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"filename": path.name, "name": name, "version": str(version), "sha256": digest}


def _write_lock(root: Path, entries: list[dict], target: Target | None = None) -> None:
    # Only final, locally built wheel hashes belong in the transferable lock.
    python = (
        target.python_version if target.python_patch is not None
        else f"{target.python[0]}.{target.python[1]}.*"
    ) if target else f"{sys.version_info.major}.{sys.version_info.minor}.*"
    text = (
        'lock-version = "1.0"\n'
        f'created-by = "wheelport {__version__}"\n'
        f'requires-python = "=={python}"\n'
    )
    if target:
        environment = target.marker_environment
        marker = " and ".join(
            f"{key} == {environment[key]!r}"
            for key in ("sys_platform", "platform_machine", "implementation_name")
        )
        text += f"environments = [{json.dumps(marker)}]\n"
    if not entries:
        text += "packages = []\n"
    for entry in entries:
        text += (
            "\n[[packages]]\n"
            f"name = {json.dumps(entry['name'])}\n"
            f"version = {json.dumps(entry['version'])}\n"
            "[[packages.wheels]]\n"
            f"path = {json.dumps('wheels/' + entry['filename'])}\n"
            f"hashes = {{ sha256 = {json.dumps(entry['sha256'])} }}\n"
        )
    (root / "pylock.toml").write_text(text, encoding="utf-8")


def _check_closure(
    wheels: Path, entries: list[dict], input: Path, project: dict,
    target: Target, extras: list[str], groups: list[str], project_entry: dict | None,
) -> None:
    """Check local metadata only; direct references never trigger source retrieval."""
    extras = [canonicalize_name(extra) for extra in extras]
    groups = [canonicalize_name(group) for group in groups]
    by_name = {entry["name"]: entry for entry in entries}
    requested = {name: set() for name in by_name}
    roots = []
    metadata = project.get("project", {})
    if project:
        roots.extend(metadata.get("dependencies", []))
        for extra, values in metadata.get("optional-dependencies", {}).items():
            if canonicalize_name(extra) in extras:
                roots.extend(values)
        dependency_groups = {
            canonicalize_name(group): values
            for group, values in project.get("dependency-groups", {}).items()
        }
        selected_groups = set(groups)
        pending = list(groups)
        while pending:
            for value in dependency_groups.get(pending.pop(), []):
                if isinstance(value, str):
                    roots.append(value)
                else:
                    included = canonicalize_name(value["include-group"])
                    if included not in selected_groups:
                        selected_groups.add(included)
                        pending.append(included)
        # Legacy Poetry declarations can carry dependency extras absent from PEP 621.
        poetry = project.get("tool", {}).get("poetry", {})
        optional = {
            canonicalize_name(name.split("(", 1)[0].strip())
            for extra, names in poetry.get("extras", {}).items()
            if canonicalize_name(extra) in extras
            for name in names
        }
        declarations = [poetry.get("dependencies", {})]
        declarations.extend(values.get("dependencies", {})
                            for group, values in poetry.get("group", {}).items()
                            if canonicalize_name(group) in groups)
        if "dev" in groups:
            declarations.append(poetry.get("dev-dependencies", {}))
        for dependencies in declarations:
            for name, values in dependencies.items():
                if name == "python":
                    continue
                for value in values if isinstance(values, list) else [values]:
                    if not isinstance(value, dict):
                        continue
                    if value.get("optional") and canonicalize_name(name) not in optional:
                        continue
                    if value.get("markers") and not target.evaluate_marker(value["markers"]):
                        continue
                    if canonicalize_name(name) in requested:
                        requested[canonicalize_name(name)].update(
                            canonicalize_name(extra) for extra in value.get("extras", [])
                        )
    else:
        from .targeted import _requirements

        roots.extend(_requirements(input, target)[0])
    if project_entry:
        requested[project_entry["name"]].update(extras)

    def check(requirement: Requirement) -> None:
        name = canonicalize_name(requirement.name)
        if name not in by_name:
            raise ValueError(f"Target dependency closure is missing {name}")
        if not requirement.specifier.contains(by_name[name]["version"], prereleases=True):
            raise ValueError(f"Target dependency version mismatch: {requirement}, "
                             f"bundled {by_name[name]['version']}")
        requested[name].update(canonicalize_name(extra) for extra in requirement.extras)

    for body in roots:
        requirement = Requirement(body)
        if not requirement.marker or target.evaluate_marker(requirement.marker, extras=extras):
            check(requirement)
    messages = {}
    for name, entry in by_name.items():
        with zipfile.ZipFile(wheels / entry["filename"]) as archive:
            path = next(p for p in archive.namelist() if p.endswith(".dist-info/METADATA"))
            messages[name] = BytesParser().parsebytes(archive.read(path))
    evaluated = {}
    while True:
        pending = [name for name in by_name if evaluated.get(name) != requested[name]]
        if not pending:
            break
        for name in pending:
            evaluated[name] = requested[name].copy()
            for body in messages[name].get_all("Requires-Dist", []):
                requirement = Requirement(body)
                if not requirement.marker or target.evaluate_marker(
                    requirement.marker, extras=requested[name]
                ):
                    check(requirement)


def pack(
    input: Path,
    output: Path,
    *,
    manager: str = "auto",
    indexes: list[str] | None = None,
    find_links: list[str] | None = None,
    no_index: bool = False,
    extras: list[str] | None = None,
    groups: list[str] | None = None,
    include_project: bool = True,
    pip_wheel: Path | None = None,
    target: Target | None = None,
    project_wheel: Path | None = None,
) -> Path:
    """Resolve and verify wheels, then publish without replacing existing outputs."""
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"Output already exists: {output}")
    if not output.parent.is_dir():
        raise ValueError(f"Output parent directory does not exist: {output.parent}")
    indexes, find_links = indexes or [], find_links or []
    extras, groups = extras or [], groups or []
    project, project_entry = {}, None
    if target or project_wheel:
        input = Path(input).resolve()
        if input.is_dir():
            input /= "pyproject.toml" if (input / "pyproject.toml").is_file() else "requirements.txt"
        project = sources._toml(input) if input.name == "pyproject.toml" else {}
        if target and any(field in project.get("project", {}).get("dynamic", [])
                          for field in ("dependencies", "optional-dependencies")):
            raise ValueError("Explicit target dynamic dependencies unsupported")
    if project_wheel:
        if not project:
            raise ValueError("--project-wheel requires a pyproject.toml input")
        project_wheel = Path(project_wheel).resolve()
        project_entry = _wheel_entry(project_wheel, target=target) if target else _wheel_entry(
            project_wheel
        )
        metadata = project.get("project", {})
        legacy = project.get("tool", {}).get("poetry", {})
        name = metadata.get("name", legacy.get("name"))
        version = metadata.get("version", legacy.get("version"))
        if (name and canonicalize_name(name) != project_entry["name"]) or (
            version and "version" not in metadata.get("dynamic", [])
            and Version(str(version)) != Version(project_entry["version"])
        ):
            raise ValueError("--project-wheel name/version does not match input project metadata")
    with tempfile.TemporaryDirectory(prefix="wheelport-") as temporary:
        root = Path(temporary) / "bundle"
        wheels = root / "wheels"
        wheels.mkdir(parents=True)
        effective = sources.collect(
            input,
            manager,
            wheels,
            indexes=indexes,
            find_links=find_links,
            no_index=no_index,
            extras=extras,
            groups=groups,
            include_project=False if project_wheel else include_project,
            **({"target": target} if target else {}),
        )
        if project_wheel:
            destination = wheels / project_wheel.name
            if destination.exists() and destination.read_bytes() != project_wheel.read_bytes():
                raise ValueError("Resolution conflicts with --project-wheel")
            shutil.copyfile(project_wheel, destination)
        entries = [
            _wheel_entry(path, target=target) if target else _wheel_entry(path)
            for path in sorted(wheels.glob("*.whl"))
        ]
        names = [entry["name"] for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError("Resolution produced multiple wheels for the same package")
        bootstrap = root / "bootstrap"
        bootstrap.mkdir()
        bundled_pip = next((entry for entry in entries if entry["name"] == "pip"), None)
        if target and bundled_pip and not pip_wheel:
            pip_wheel = wheels / bundled_pip["filename"]
        if pip_wheel:
            pip_wheel = Path(pip_wheel).resolve()
            shutil.copyfile(pip_wheel, bootstrap / pip_wheel.name)
        else:
            pip_requirement = f"pip=={importlib.metadata.version('pip')}"
            if target and not SpecifierSet(
                importlib.metadata.metadata("pip").get("Requires-Python", "")
            ).contains(target.python_version):
                pip_requirement = "pip>=25"
            sources._run(
                [
                    sys.executable, "-m", "pip", "download", "--no-deps",
                    "--only-binary", ":all:", "--dest", str(bootstrap),
                    *sources._options(indexes, find_links, no_index),
                    *(target.download_args() if target else []),
                    pip_requirement,
                ],
                Path(temporary),
            )
        bootstrap_paths = list(bootstrap.glob("*.whl"))
        if len(bootstrap_paths) != 1:
            raise ValueError("Bootstrap must contain exactly one pip wheel")
        pip_entry = _wheel_entry(bootstrap_paths[0], target=target) if target else _wheel_entry(
            bootstrap_paths[0]
        )
        if pip_entry["name"] != "pip":
            raise ValueError("--pip-wheel must be a pip distribution wheel")
        if bundled_pip and bundled_pip["sha256"] != pip_entry["sha256"]:
            raise ValueError("Project pins a different pip; supply its wheel with --pip-wheel")
        # Bootstrap pip is also installed, and is part of the transferable lock.
        if not bundled_pip:
            shutil.copyfile(bootstrap_paths[0], wheels / pip_entry["filename"])
            entries.append(pip_entry)
            entries.sort(key=lambda entry: entry["name"])
        if target:
            _check_closure(wheels, entries, input, project, target, extras, groups, project_entry)
        manifest_target = target.manifest() if target else {
            "implementation": sys.implementation.name,
            "python": list(sys.version_info[:2]),
            "platform": sysconfig.get_platform(),
            "soabi": sysconfig.get_config_var("SOABI") or "",
            "libc": _libc(),
        }
        manifest = {
            "format": 1,
            "created_by": f"wheelport {__version__}",
            "manager": effective,
            "target": manifest_target,
            "wheels": entries,
            "bootstrap": pip_entry,
            "verification": {
                "artifact_check": "passed",
                "runtime_install": "not-run" if target else "passed",
            },
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if target:
            _write_lock(root, entries, target=target)
        else:
            _write_lock(root, entries)
        (root / "requirements.txt").write_text(
            "--no-index\n--only-binary :all:\n"
            + "".join(
                f"./wheels/{entry['filename']} --hash=sha256:{entry['sha256']}\n"
                for entry in entries
            ),
            encoding="utf-8",
        )
        shutil.copyfile(Path(__file__).with_name("install.py"), root / "install.py")
        (root / "README.txt").write_text(
            "wheelport offline bundle\n\n"
            "Extract with unzip, or python -m zipfile -e BUNDLE.zip DIRECTORY.\n"
            "Run: python /path/to/extracted/install.py --venv /path/to/new/venv\n"
            "Check without installation: python install.py --verify-only\n"
            "Requires matching Python (3.11+) and the venv standard-library module.\n"
            "No uv, Poetry, installed pip, ensurepip, or network is needed.\n"
            "Hashes detect corruption, not malicious replacement of the entire bundle.\n"
            "Only install bundles from trusted sources.\n",
            encoding="utf-8",
        )
        # Validate dependency closure with precisely the installer shipped to the target.
        if target is None:
            sources._run(
                [sys.executable, "-I", str(root / "install.py"),
                 "--venv", str(Path(temporary) / "check")],
                Path(temporary),
            )
        # Publish a complete archive without overwriting a concurrent writer's output.
        descriptor, staged = tempfile.mkstemp(prefix=".wheelport-", suffix=".zip", dir=output.parent)
        try:
            with (
                os.fdopen(descriptor, "wb") as stream,
                zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive,
            ):
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
            try:
                os.link(staged, output)
            except FileExistsError:
                raise ValueError(f"Output already exists: {output}") from None
        finally:
            Path(staged).unlink(missing_ok=True)
    return output
