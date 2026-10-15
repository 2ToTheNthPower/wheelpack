"""Generate PEP 751 pylock plans from native locks and requirement inputs.

This module owns dependency *resolution*: turning a requirements file, a PEP 621
project, a ``uv.lock``, or a ``poetry.lock`` into a normalized, host-filtered
PEP 751 plan. It never downloads, builds, or installs a wheel. The shared
plumbing (subprocess execution, URL/path handling, option assembly) lives in
:mod:`wheelpack.sources`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from packaging.markers import Marker, default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from . import sources as s
from ._compat import file_digest, tomllib
from .targets import Target


def _toml_value(value) -> str:
    # Inline tables let us preserve native pylock fields without a TOML dependency.
    if isinstance(value, dict):
        return (
            "{ " + ", ".join(f"{json.dumps(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
        )
    if isinstance(value, list):
        return "[" + ", ".join(map(_toml_value, value)) + "]"
    if isinstance(value, datetime):
        return value.isoformat()
    return json.dumps(value, ensure_ascii=True)


def _write_plan(plan: dict, path: Path, cwd: Path) -> None:
    for package in plan["packages"]:
        for source in [
            package.get(key, {}) for key in ("directory", "vcs", "archive", "sdist")
        ] + package.get("wheels", []):
            if "path" in source:
                source["path"] = str((cwd / source["path"]).resolve())
            if source and source not in (package.get("directory"), package.get("vcs")):
                # uv 0.12 emits empty hashes for local find-links artifacts.
                local = s._local_path(
                    Path(source["path"]) if "path" in source else source.get("url", ""), cwd
                )
                if local and not source.get("hashes", {}).get("sha256"):
                    with local.open("rb") as stream:
                        source.setdefault("hashes", {})["sha256"] = file_digest(
                            stream, "sha256"
                        ).hexdigest()
        if "directory" in package:
            package["directory"]["editable"] = False
    text = "\n".join(
        f"{json.dumps(k)} = {_toml_value(v)}" for k, v in plan.items() if k != "packages"
    )
    if not plan["packages"]:
        text += "\npackages = []\n"
    for package in plan["packages"]:
        text += "\n\n[[packages]]\n" + "\n".join(
            f"{json.dumps(k)} = {_toml_value(v)}" for k, v in package.items()
        )
    # This file may contain signed URLs or credentials. It is never bundle data.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_plan(plan: dict, path: Path, cwd: Path) -> None:
    """Write ``plan`` as a PEP 751 ``pylock.toml`` (mode 0600, never overwritten)."""
    _write_plan(plan, path, cwd)


def _pylock(text: str) -> dict:
    try:
        plan = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError("Native tool did not produce valid pylock TOML") from None
    if plan.get("lock-version") != "1.0" or not isinstance(plan.get("packages"), list):
        raise ValueError("Native tool did not produce a PEP 751 pylock lock-version 1.0 plan")
    return plan


def _pip_report(report: dict) -> dict:
    if (
        not isinstance(report, dict)
        or report.get("version") != "1"
        or not isinstance(report.get("install"), list)
    ):
        raise ValueError("pip did not produce a valid installation report version 1")
    plan = {"lock-version": "1.0", "created-by": "wheelpack (pip)", "packages": []}
    for entry in report["install"]:
        metadata = entry.get("metadata", {})
        if not metadata.get("name") or not metadata.get("version"):
            raise ValueError("pip report is missing package name or version")
        package = {"name": metadata["name"], "version": metadata["version"]}
        if metadata.get("requires_python"):
            package["requires-python"] = metadata["requires_python"]
        info = entry.get("download_info", {})
        url = info.get("url", "")
        if not url:
            raise ValueError(f"pip report is missing a source URL for {package['name']}")
        if "dir_info" in info:
            parts = urlsplit(url)
            if parts.scheme != "file" or parts.netloc not in {"", "localhost"}:
                raise ValueError("pip report directory must be a local file URL")
            package["directory"] = {
                "path": str(s._local_path(url, Path.cwd())),
                "editable": info["dir_info"].get("editable", False),
            }
            source = package["directory"]
        elif "vcs_info" in info:
            vcs = info["vcs_info"]
            if not vcs.get("vcs") or not vcs.get("commit_id"):
                raise ValueError(f"pip report is missing a locked VCS commit for {package['name']}")
            source = {"type": vcs["vcs"], "url": url, "commit-id": vcs["commit_id"]}
            if vcs.get("requested_revision"):
                source["requested-revision"] = vcs["requested_revision"]
            package["vcs"] = source
        else:
            archive = info.get("archive_info", {})
            hashes = dict(archive.get("hashes", {}))
            # Older pip reports may use the singular algorithm=digest field.
            if not hashes.get("sha256") and archive.get("hash", "").startswith("sha256="):
                hashes["sha256"] = archive["hash"].split("=", 1)[1]
            if not re.fullmatch(r"[a-fA-F0-9]{64}", hashes.get("sha256", "")):
                raise ValueError(
                    f"pip report is missing a SHA256 artifact hash for {package['name']}"
                )
            source = {"url": url, "hashes": hashes}
            if unquote(urlsplit(url).path).lower().endswith(".whl"):
                package["wheels"] = [source]
            else:
                package["archive" if entry.get("is_direct") else "sdist"] = source
        if info.get("subdirectory"):
            source["subdirectory"] = info["subdirectory"]
        plan["packages"].append(package)
    # pip has already evaluated dependency markers for this interpreter/host.
    return plan


def _exports(
    text: str,
    extras: list[str],
    *,
    target: Target | None = None,
) -> list[tuple[str, Requirement | None]]:
    result = []
    text = re.sub(r"\\\s*\n", " ", text)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Exported index flags must not turn per-package sources into global fallbacks.
        if line.startswith(
            (
                "--index-url",
                "--extra-index-url",
                "--find-links",
                "--trusted-host",
                "--no-index",
                "-i ",
            )
        ):
            continue
        line = re.split(r"\s+#", line, maxsplit=1)[0]
        body = re.sub(r"\s+--hash(?:=|\s+)\S+", "", line).strip()
        try:
            requirement = Requirement(body)
        except InvalidRequirement:
            if body.startswith(("./", "../", "/", "file:", "-e ")):
                result.append((line.removeprefix("-e "), None))
                continue
            raise ValueError("Invalid requirement in native export") from None
        if requirement.marker and not (
            target.evaluate_marker(requirement.marker, extras=extras)
            if target
            else any(
                requirement.marker.evaluate({**default_environment(), "extra": extra})
                for extra in ["", *extras]
            )
        ):
            continue
        result.append((line, requirement))
    return result


def _package(lock: dict, requirement: Requirement) -> dict:
    versions = [
        spec.version
        for spec in requirement.specifier
        if spec.operator == "==" and "*" not in spec.version
    ]
    if len(versions) != 1:
        raise ValueError(f"Export did not pin {requirement.name} to an exact version")
    candidates = [
        package
        for package in lock.get("package", [])
        if canonicalize_name(package["name"]) == canonicalize_name(requirement.name)
        and Version(str(package["version"])) == Version(versions[0])
    ]
    if len(candidates) != 1:
        raise ValueError(f"Cannot unambiguously match {requirement.name} in lock file")
    return candidates[0]


def _selected(plan: dict, extras: list[str], groups: list[str]) -> list[dict]:
    environment = {
        **default_environment(),
        "extras": set(extras),
        "dependency_groups": set(groups + plan.get("default-groups", [])),
    }
    python = environment["python_full_version"]
    if not SpecifierSet(plan.get("requires-python", "")).contains(python):
        raise ValueError("Resolution pylock requires a different Python version")
    if plan.get("environments") and not any(
        Marker(marker).evaluate(environment) for marker in plan["environments"]
    ):
        raise ValueError("Resolution pylock does not support the current host")
    selected = {}
    for package in plan["packages"]:
        if package.get("marker") and not Marker(package["marker"]).evaluate(environment):
            continue
        name = canonicalize_name(package["name"])
        if name in selected:
            raise ValueError(f"Ambiguous pylock entries for {name}")
        if not SpecifierSet(package.get("requires-python", "")).contains(python):
            raise ValueError(f"Locked package {name} requires a different Python version")
        selected[name] = package
    return list(selected.values())


def select(plan: dict, extras=(), groups=()) -> list[dict]:
    """Return the packages in ``plan`` that apply to the running host."""
    return _selected(plan, list(extras), list(groups))


@dataclass(frozen=True, slots=True)
class Source:
    """A validated input plus its manager and uv workspace context."""

    input: Path
    manager: str
    cwd: Path
    is_project: bool
    project: dict
    uv_root: Path


def detect(input: Path, manager: str) -> Source:
    """Normalize an input path and select the effective manager (pip, uv, poetry)."""
    input = Path(input).resolve()
    if input.is_dir():
        input = input / (
            "pyproject.toml" if (input / "pyproject.toml").is_file() else "requirements.txt"
        )
    if not input.is_file():
        raise ValueError("Input must be a requirements file, pyproject.toml, or project directory")
    if manager not in {"auto", "pip", "uv", "poetry"}:
        raise ValueError(f"Unknown manager: {manager}")
    cwd = input.parent
    is_project = input.name == "pyproject.toml"
    project = s._toml(input) if is_project else {}
    tool = project.get("tool", {})
    uv_root = s._uv_root(cwd) if is_project and manager in {"uv", "auto"} else cwd
    if manager == "auto":
        if is_project and ((uv_root / "uv.lock").exists() or "uv" in tool or uv_root != cwd):
            manager = "uv"
        elif is_project and ((cwd / "poetry.lock").exists() or "poetry" in tool):
            manager = "poetry"
        else:
            manager = "pip"
    if manager != "pip" and not is_project:
        raise ValueError(f"{manager} requires a pyproject.toml input")
    return Source(input, manager, cwd, is_project, project, uv_root)


@dataclass(slots=True)
class Resolution:
    """A PEP 751 plan plus the context retrieval needs to act on it."""

    manager: str
    plan: dict
    cwd: Path
    plan_cwd: Path
    options: list[str]
    config: dict = field(default_factory=dict)
    lock: dict = field(default_factory=dict)
    root_tool: dict = field(default_factory=dict)
    scopes: dict = field(default_factory=dict)
    locked_hashes: dict = field(default_factory=dict)
    credential_urls: list[str] = field(default_factory=list)
    uv_root: Path | None = None
    uv_args: list[str] = field(default_factory=list)
    project: dict = field(default_factory=dict)
    is_project: bool = False


def resolve(
    source: Source,
    *,
    indexes: list[str],
    find_links: list[str],
    no_index: bool,
    extras: list[str],
    groups: list[str],
) -> Resolution:
    """Normalize ``source`` into a PEP 751 plan without downloading any wheel.

    The returned :class:`Resolution` carries the plan plus the source options,
    lock data, and per-package scopes that retrieval needs. No artifact is
    fetched or built here; that is exclusively :mod:`wheelpack.sources`'s job.
    """
    input = source.input
    manager = source.manager
    cwd = source.cwd
    is_project = source.is_project
    project = source.project
    tool = project.get("tool", {})
    uv_root = source.uv_root

    options = s._options(indexes, find_links, no_index)
    compile_args = ["uv", "pip", "compile", "--format", "pylock.toml", "--python", sys.executable]
    lock = {}
    config = {}
    root_tool = {}
    scopes = {}
    locked_hashes = {}
    credential_urls = [*indexes, *find_links]
    uv_args = []
    plan_cwd = cwd
    if manager == "pip":
        if not is_project and (extras or groups):
            raise ValueError("Extras and groups require a project input")
        if not is_project:
            with tempfile.TemporaryDirectory(prefix="wheelpack-pip-") as temporary:
                report = Path(temporary) / "report.json"
                s._run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--dry-run",
                        "--ignore-installed",
                        "--report",
                        str(report),
                        *options,
                        "-r",
                        str(input),
                    ],
                    cwd,
                )
                try:
                    data = json.loads(report.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise ValueError(
                        "pip did not produce a readable JSON installation report"
                    ) from None
                plan = _pip_report(data)
            credential_urls = s._pip_credentials(input, cwd, credential_urls)
        else:
            args = [
                *compile_args,
                "--no-config",
                "--no-sources",
                "--index-strategy",
                "unsafe-best-match",
                *options,
                str(input),
            ]
            for extra in extras:
                args += ["--extra", extra]
            for group in groups:
                args += ["--group", group]
            plan = _pylock(s._run(args, cwd))
    if manager == "uv":
        lock = s._toml(uv_root / "uv.lock")
        root_tool = s._toml(uv_root / "pyproject.toml").get("tool", {})
        config = (
            s._toml(uv_root / "uv.toml")
            if (uv_root / "uv.toml").exists()
            else root_tool.get("uv", {})
        )
        options = s._uv_options(config, indexes, find_links, no_index, uv_root)
        plan_cwd = uv_root
        args = [
            "uv",
            "export",
            "--locked",
            "--format",
            "pylock.toml",
            "--no-dev",
            "--no-default-groups",
            "--no-editable",
            "--python",
            sys.executable,
        ]
        if uv_root != cwd:
            member = project.get("project", {}).get("name")
            if not member:
                raise ValueError("uv workspace member must declare a project name")
            args += ["--project", str(uv_root), "--package", member, "--no-emit-package", member]
        else:
            args.append("--no-emit-project")
        for extra in extras:
            args += ["--extra", extra]
        for group in groups:
            args += ["--group", group]
        if no_index or config.get("no-index", False):
            uv_args.append("--no-index")
        for index in indexes:
            uv_args += ["--index", index]
        for link in find_links:
            uv_args += [
                "--find-links",
                str(local) if (local := s._local_path(link, cwd)) is not None else link,
            ]
        plan = _pylock(s._run(args + uv_args, uv_root))
    elif manager == "poetry":
        lock = s._toml(cwd / "poetry.lock")
        s._run(["poetry", "check", "--lock"], cwd)
        args = [
            "poetry",
            "export",
            "--format",
            "requirements.txt",
            "--only",
            ",".join(["main", *groups]),
        ]
        for extra in extras:
            args += ["--extras", extra]
        exported = s._run(args, cwd)
        repositories = tool.get("poetry", {}).get("source", [])
        plan = {"lock-version": "1.0", "created-by": "wheelpack", "packages": []}
        for line, requirement in _exports(exported, extras):
            package = _package(lock, requirement) if requirement and not requirement.url else None
            package_source = package.get("source", {}) if package else {}
            scoped = options
            registry = ""
            if package_source.get("url"):
                name = package_source.get("reference")
                if not name:
                    name = next(
                        (
                            item["name"]
                            for item in repositories
                            if s._identity(item["url"]) == s._identity(package_source["url"])
                        ),
                        None,
                    )
                registry = s._credentials(package_source["url"], name, "poetry")
                if no_index:
                    raise ValueError("Cannot discover a locked Poetry repository with no_index")
                scoped = s._options([registry], [], False)
            if package and "--hash" not in line:
                raise ValueError(f"Missing exported lock hashes for {requirement.name}")
            # Discover artifacts, never resolve the native dependency graph again.
            with tempfile.TemporaryDirectory(prefix="wheelpack-poetry-") as temporary:
                requirements = Path(temporary) / "requirements.txt"
                requirements.write_text(line + "\n", encoding="utf-8")
                discovered = _pylock(
                    s._run(
                        [
                            *compile_args,
                            "--no-config",
                            "--no-deps",
                            "--index-strategy",
                            "first-index",
                            *scoped,
                            str(requirements),
                        ],
                        cwd,
                        scoped=True,
                    )
                )
            entries = _selected(discovered, extras, groups)
            if len(entries) != 1:
                raise ValueError("Poetry artifact discovery must produce exactly one package")
            entry = entries[0]
            hashes = set(re.findall(r"--hash(?:=|\s+)(\S+)", line))
            if hashes:
                locked_hashes[canonicalize_name(entry["name"])] = hashes
            if package:
                if canonicalize_name(entry["name"]) != canonicalize_name(
                    package["name"]
                ) or Version(entry.get("version", "0")) != Version(str(package["version"])):
                    raise ValueError("Poetry artifact discovery changed the locked package")
                files = {item["file"]: item["hash"] for item in package.get("files", [])}
                for key in ("sdist", "wheels"):
                    artifacts = (
                        entry.get("wheels", []) if key == "wheels" else [entry.get("sdist", {})]
                    )
                    accepted = []
                    for artifact in artifacts:
                        filename = s._artifact_name(artifact)
                        digest = "sha256:" + artifact.get("hashes", {}).get("sha256", "")
                        if digest in hashes and files.get(filename) == digest:
                            accepted.append(artifact)
                    if key == "wheels":
                        entry[key] = accepted
                    elif accepted:
                        entry[key] = accepted[0]
                    else:
                        entry.pop(key, None)
                s._artifact(entry, cwd)  # Fail closed if discovery has no native-locked artifact.
            plan["packages"].append(entry)
            scopes[canonicalize_name(entry["name"])] = (registry, scoped)

    return Resolution(
        manager=manager,
        plan=plan,
        cwd=cwd,
        plan_cwd=plan_cwd,
        options=options,
        config=config,
        lock=lock,
        root_tool=root_tool,
        scopes=scopes,
        locked_hashes=locked_hashes,
        credential_urls=credential_urls,
        uv_root=uv_root,
        uv_args=uv_args,
        project=project,
        is_project=is_project,
    )
