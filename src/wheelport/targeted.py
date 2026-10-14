"""Explicit-target, wheel-only adapters. Native collection stays in sources."""

from __future__ import annotations

import ast
import copy
import hashlib
import os
import re
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from . import pylock
from . import sources as s
from .targets import Target


def _marker(marker: str | Marker, target: Target, extras: list[str], groups: list[str]) -> bool:
    marker = Marker(marker) if isinstance(marker, str) else marker
    # Target's visitor rejects unknown OS variables, including short-circuited branches.
    # Its evaluator predates set-valued lock markers, so provide those in lock context.
    try:
        target.evaluate_marker(marker, extras=extras)
    except KeyError:
        pass
    environment = {
        **target.marker_environment,
        "extras": set(extras),
        "dependency_groups": set(groups),
    }
    return any(
        marker.evaluate({**environment, "extra": extra}, context="lock_file")
        for extra in ["", *extras]
    )


def _selected(plan: dict, target: Target, extras: list[str], groups: list[str]) -> list[dict]:
    extras = [canonicalize_name(extra) for extra in extras]
    groups = [canonicalize_name(group) for group in [*groups, *plan.get("default-groups", [])]]
    if not SpecifierSet(plan.get("requires-python", "")).contains(target.python_version):
        raise ValueError("Resolution pylock requires a different Python version")
    environments = [_marker(m, target, extras, groups) for m in plan.get("environments", [])]
    if environments and not any(environments):
        raise ValueError("Resolution pylock does not support the explicit target")
    selected = {}
    for package in plan["packages"]:
        if package.get("marker") and not _marker(package["marker"], target, extras, groups):
            continue
        name = canonicalize_name(package["name"])
        if name in selected:
            raise ValueError(f"Ambiguous pylock entries for {name}")
        if not SpecifierSet(package.get("requires-python", "")).contains(target.python_version):
            raise ValueError(f"Locked package {name} requires a different Python version")
        selected[name] = package
    return list(selected.values())


def _requirement(body: str, cwd: Path, target: Target, extras: list[str]) -> Requirement | None:
    try:
        requirement = Requirement(body)
        if not requirement.url and not requirement.specifier and body.lower().endswith(".whl"):
            raise InvalidRequirement("Bare wheel filename")
    except InvalidRequirement:
        # Bare wheel paths/URLs are legal requirements, but never directories or archives.
        location = body
        local = s._local_path(location, cwd)
        filename = str(local) if local is not None else unquote(urlsplit(location).path)
        if not filename.lower().endswith(".whl"):
            raise ValueError(
                "Explicit target requires prebuilt wheels; local/VCS/sdist inputs unsupported"
            )
        if local is not None and urlsplit(location).scheme != "file":
            location = local.as_uri()
        name = s.parse_wheel_filename(unquote(urlsplit(location).path.split("/")[-1]))[0]
        requirement = Requirement(f"{name} @ {location}")
    if requirement.marker and not target.evaluate_marker(requirement.marker, extras=extras):
        return None
    if requirement.url:
        parts = urlsplit(requirement.url)
        if parts.scheme not in {"http", "https", "file"} or not unquote(
            parts.path
        ).lower().endswith(".whl"):
            raise ValueError(
                "Explicit target requires prebuilt wheels; local/VCS/sdist inputs unsupported"
            )
        if parts.scheme == "file" and parts.netloc not in {"", "localhost"}:
            raise ValueError("Explicit target only supports local file wheel URLs")
    return requirement


def _hashes(line: str, requirement: Requirement) -> set[str]:
    hashes = set(re.findall(r"--hash(?:=|\s+)(\S+)", line))
    if requirement.url:
        for algorithm, digest in parse_qsl(urlsplit(requirement.url).fragment):
            if algorithm in {"sha256", "sha384", "sha512", "md5", "sha1"}:
                hashes.add(f"{algorithm}:{digest}")
            else:
                raise ValueError("Unrepresentable direct wheel URL fragment")
    if any(not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) for digest in hashes):
        raise ValueError("Explicit target input hashes must be SHA256")
    return {digest.lower() for digest in hashes}


def _restriction(requirement: Requirement, hashes: set[str], restrictions: list) -> None:
    if not hashes:
        return
    pins = [
        spec for spec in requirement.specifier if spec.operator == "==" and "*" not in spec.version
    ]
    if not requirement.url and (len(pins) != 1 or len(requirement.specifier) != 1):
        raise ValueError(
            "Hashed target requirements must pin an exact package/version or wheel URL"
        )
    restrictions.append((requirement, hashes))


def _requirements(
    input: Path, target: Target
) -> tuple[list[str], list[str], list[str], list, bool]:
    requirements, constraints, options, restrictions = [], [], [], []
    visited = set()
    require_hashes = False

    def read(path: Path, constraint: bool = False) -> None:
        nonlocal require_hashes
        path = path.resolve()
        if (path, constraint) in visited:
            return
        visited.add((path, constraint))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise ValueError(f"Cannot read local target requirements: {path.name}") from None
        text = re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m[1], m[0]), text)
        text = re.sub(r"\\\s*\n", " ", text)
        for line in text.splitlines():
            line = re.split(r"\s+#", line.strip(), maxsplit=1)[0]
            if not line or line.startswith("#"):
                continue
            include = re.fullmatch(r"(-r|-c|--requirement|--constraint)(?:\s+|=)?(.+)", line)
            if include:
                location = s._tokens(include[2])
                if len(location) != 1:
                    raise ValueError("Invalid local requirements/constraint include")
                try:
                    nested = s._local_path(location[0], path.parent)
                except ValueError:
                    nested = None
                if nested is None:
                    raise ValueError(
                        "Remote requirements/constraint includes cannot preserve target hashes"
                    )
                read(nested, include[1] in {"-c", "--constraint"})
                continue
            if line.startswith(
                (
                    "--index-url",
                    "--extra-index-url",
                    "--find-links",
                    "--no-index",
                    "--trusted-host",
                    "--cert",
                    "-i ",
                )
            ):
                tokens = s._tokens(line)
                flag, separator, value = tokens[0].partition("=")
                flag = "--index-url" if flag == "-i" else flag
                if flag == "--no-index" and len(tokens) == 1:
                    options.append(flag)
                    continue
                values = [value] if separator else tokens[1:]
                if (
                    flag
                    not in {
                        "--index-url",
                        "--extra-index-url",
                        "--find-links",
                        "--trusted-host",
                        "--cert",
                    }
                    or len(values) != 1
                ):
                    raise ValueError("Unsupported target requirements source option")
                value = values[0]
                if flag in {"--find-links", "--cert"}:
                    local = s._local_path(value, path.parent)
                    if local is not None:
                        value = str(local)
                options.extend([flag, value])
                continue
            if line == "--require-hashes":
                require_hashes = True
                continue
            if line.startswith("-"):
                raise ValueError("Unsupported target requirements option or editable source")
            body = re.sub(r"\s+--hash(?:=|\s+)\S+", "", line).strip()
            requirement = _requirement(body, path.parent, target, [])
            if requirement is None:
                continue
            hashes = _hashes(line, requirement)
            _restriction(requirement, hashes, restrictions)
            requirement.marker = None
            (constraints if constraint else requirements).append(str(requirement))

    read(input)
    return requirements, constraints, options, restrictions, require_hashes or bool(restrictions)


def _pip_options(input: Path, cwd: Path, indexes: list[str], links: list[str], no_index: bool):
    configured = {}
    text = s._run([sys.executable, "-m", "pip", "config", "list"], cwd)
    # Apply the same precedence as native pip: env, install, global.
    for line in sorted(
        text.splitlines(),
        key=lambda line: (
            2 if line.startswith(":env:.") else 1 if line.startswith("install.") else 0
        ),
    ):
        key, separator, value = line.partition("=")
        if separator and key.startswith(("global.", "install.", ":env:.")):
            try:
                configured[key.split(".")[-1]] = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                continue
    primary = indexes[:1] or str(configured.get("index-url", "https://pypi.org/simple")).split()
    secondary = (
        indexes[1:] if len(indexes) > 1 else str(configured.get("extra-index-url", "")).split()
    )
    registries = [*primary, *secondary]
    links = [*str(configured.get("find-links", "")).split(), *links]
    disabled = str(configured.get("no-index", "false")).lower() in {"true", "1", "yes"}
    credentials = s._pip_credentials(input, cwd, [*registries, *links])
    options = s._options(registries, links, no_index or disabled)
    for host in str(configured.get("trusted-host", "")).split():
        options += ["--trusted-host", host]
    if configured.get("cert"):
        options += ["--cert", str(configured["cert"])]
    if configured.get("client-cert"):
        raise ValueError("Target uv resolution cannot transfer pip client-cert configuration")
    return options, credentials


def _merge_options(options: list[str]) -> list[str]:
    # pip requirements can replace the configured index; uv rejects duplicate flags.
    indexes, extra_indexes, links, other, no_index = [], [], [], [], False
    position = 0
    while position < len(options):
        flag = options[position]
        position += 1
        if flag == "--no-index":
            no_index = True
            continue
        value = options[position]
        position += 1
        if flag == "--index-url":
            indexes = [value]
        elif flag == "--extra-index-url":
            extra_indexes.append(value)
        elif flag == "--find-links":
            links.append(value)
        else:
            other.extend([flag, value])
    return [*s._options([*indexes, *extra_indexes], links, no_index), *other]


def _metadata(path: Path, target: Target, extras: list[str]) -> list[Requirement]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError(f"Wheel must contain exactly one METADATA file: {path.name}")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if not SpecifierSet(metadata.get("Requires-Python", "")).contains(target.python_version):
        raise ValueError(f"Wheel {path.name} requires a different Python version")
    dependencies = []
    for dependency in metadata.get_all("Requires-Dist", []):
        requirement = _requirement(dependency, path.parent, target, extras)
        if requirement is not None:
            requirement.marker = None
            dependencies.append(requirement)
    return dependencies


def _check_hash(package: dict, url: str, digest: str, restrictions: list) -> None:
    name = canonicalize_name(package["name"])
    for requirement, allowed in restrictions:
        if canonicalize_name(requirement.name) != name:
            continue
        if requirement.url:
            if (s._identity(requirement.url), urlsplit(requirement.url).query) != (
                s._identity(url),
                urlsplit(url).query,
            ):
                raise ValueError(f"Discovered wheel URL changed hashed input for {name}")
        elif not requirement.specifier.contains(package.get("version", "0")):
            raise ValueError(f"Discovered version changed hashed input for {name}")
        if digest.lower() not in allowed:
            raise ValueError(f"Discovered artifact hash is not in input allowed hashes for {name}")


def _compile(
    args: list[str],
    cwd: Path,
    target: Target,
    extras: list[str],
    groups: list[str],
    roots: list[Requirement],
    restrictions: list,
    credentials: list[str],
    run,
    *,
    require_hashes: bool = False,
) -> dict:
    """Let uv choose versions, but evaluate dependency markers with the exact target.

    uv normalizes Windows machine names differently from Python/packaging. Resolving
    all explicitly evaluated requirements together also preserves version conflicts.
    Metadata comes exclusively from hashed prebuilt wheels, never a build backend.
    """
    with tempfile.TemporaryDirectory(prefix="wheelport-metadata-") as temporary:
        transport = [
            value
            for position, arg in enumerate(args)
            if arg in {"--trusted-host", "--cert"}
            for value in args[position : position + 2]
        ]
        folder = Path(temporary)
        dependencies = folder / "dependencies.txt"
        dependencies.write_text("", encoding="utf-8")
        metadata_cache = {}
        previous = None
        for _ in range(100):
            requested = {}
            for requirement in roots:
                requested.setdefault(canonicalize_name(requirement.name), set()).update(
                    canonicalize_name(extra) for extra in requirement.extras
                )
            # Other platforms use uv's full backtracking resolver. Windows needs
            # explicitly evaluated wheel dependencies because uv's machine names
            # differ from Python's AMD64/ARM64/x86 marker values.
            discovery = ["--no-deps"] if target.python_platform.endswith("-windows-msvc") else []
            try:
                plan = pylock._pylock(run([*args, *discovery, str(dependencies)]))
            except ValueError as exc:
                if discovery and previous is not None:
                    raise ValueError(
                        f"{exc}\nWindows target metadata resolution cannot backtrack across "
                        "parent versions; provide pinned requirements or a native lock."
                    ) from None
                raise
            packages = _selected(plan, target, extras, groups)
            for package in packages:
                _with_local_hashes(package, cwd)
            by_name = {canonicalize_name(package["name"]): package for package in packages}
            active = {canonicalize_name(requirement.name) for requirement in roots}
            pending = list(active)
            requirements = set()
            artifacts = []
            while pending:
                name = pending.pop()
                if name not in by_name:
                    continue
                package = by_name[name]
                if require_hashes and not any(
                    canonicalize_name(req.name) == name for req, _ in restrictions
                ):
                    raise ValueError(
                        f"Hash-checking mode requires incoming pinned hashes for {name}"
                    )
                url, digest = s._artifact(package, cwd, tags=target.tags(), wheel_only=True)
                _check_hash(package, url, digest, restrictions)
                artifacts.append((name, url, digest))
                key = (url, digest, tuple(sorted(requested.get(name, set()))))
                if key not in metadata_cache:
                    local = s._local_path(url, cwd)
                    if local is None:
                        # Even metadata retrieval has an ephemeral plan before downloading.
                        stage = folder / "stage"
                        stage.mkdir(exist_ok=True)
                        resolution = folder / "resolved.pylock.toml"
                        pylock._write_plan({**plan, "packages": [package]}, resolution, cwd)
                        path = folder / "wheel.txt"
                        authenticated = s._pip_auth(url, credentials)
                        path.write_text(
                            f"{name} @ {authenticated} --hash={digest}\n", encoding="utf-8"
                        )
                        try:
                            run(
                                [
                                    sys.executable,
                                    "-m",
                                    "pip",
                                    "download",
                                    "--isolated",
                                    "--no-deps",
                                    "--only-binary",
                                    ":all:",
                                    "--require-hashes",
                                    *transport,
                                    *target.download_args(),
                                    "--dest",
                                    str(stage),
                                    "-r",
                                    str(path),
                                ]
                            )
                        finally:
                            resolution.unlink(missing_ok=True)
                        local = stage / unquote(urlsplit(url).path.split("/")[-1])
                    metadata_cache[key] = _metadata(
                        local, target, sorted(requested.get(name, set()))
                    )
                for requirement in metadata_cache[key]:
                    requirements.add(str(requirement))
                    dependency = canonicalize_name(requirement.name)
                    old_extras = requested.setdefault(dependency, set()).copy()
                    requested[dependency].update(
                        canonicalize_name(extra) for extra in requirement.extras
                    )
                    if dependency not in active or old_extras != requested[dependency]:
                        active.add(dependency)
                        pending.append(dependency)
            signature = (tuple(sorted(artifacts)), tuple(sorted(requirements)))
            if signature == previous:
                plan["packages"] = [package for name, package in by_name.items() if name in active]
                return plan
            previous = signature
            dependencies.write_text("\n".join(sorted(requirements)) + "\n", encoding="utf-8")
        raise ValueError("Explicit target wheel metadata resolution did not converge")


def collect(
    input: Path,
    manager: str,
    wheel_dir: Path,
    *,
    target: Target,
    indexes: list[str],
    find_links: list[str],
    no_index: bool,
    extras: list[str],
    groups: list[str],
    include_project: bool,
    project: dict,
    uv_root: Path,
) -> str:
    extras = [canonicalize_name(extra) for extra in extras]
    groups = [canonicalize_name(group) for group in groups]
    if target.abi.endswith("t"):
        raise ValueError(
            "uv cannot specify a free-threaded target ABI; normal CPython ABI required"
        )
    cwd = input.parent
    is_project = input.name == "pyproject.toml"
    tool = project.get("tool", {})
    buildable = bool(project.get("build-system"))
    if manager == "uv":
        buildable = tool.get("uv", {}).get("package", buildable)
    elif manager == "poetry":
        buildable = buildable and tool.get("poetry", {}).get("package-mode", True)
    if include_project and buildable:
        raise ValueError(
            "Explicit target cannot build the root project; use --dependencies-only or --project-wheel"
        )
    if not SpecifierSet(project.get("project", {}).get("requires-python", "")).contains(
        target.python_version
    ):
        raise ValueError("Root project requires a different target Python version")
    wheel_dir.mkdir(parents=True, exist_ok=True)
    resolution = wheel_dir.parent / "resolved.pylock.toml"
    if resolution.exists() or resolution.is_symlink():
        raise ValueError("Ephemeral resolution path already exists: resolved.pylock.toml")
    overrides = (
        {"MACOSX_DEPLOYMENT_TARGET": "11.0"}
        if target.python_platform.endswith("-apple-darwin")
        else {}
    )

    def run(args, root=cwd, *, scoped=True):
        if args[:3] == ["uv", "pip", "compile"]:
            args = [
                "--allow-insecure-host" if value == "--trusted-host" else value for value in args
            ]
        return s._run(args, root, scoped=scoped, pip_isolated=not scoped, env_overrides=overrides)

    resolver_args = target.resolver_args()
    # uv treats --no-build as --only-binary :all: and rejects specifying both.
    binary = resolver_args.index("--only-binary")
    del resolver_args[binary : binary + 2]
    compile_args = ["uv", "pip", "compile", "--format", "pylock.toml", *resolver_args]
    options = s._options(indexes, find_links, no_index)
    restrictions, scopes, credentials = [], {}, [*indexes, *find_links]
    plan_cwd, config, lock = cwd, {}, {}
    if manager == "pip":
        if is_project:
            metadata = project.get("project", {})
            if any(
                field in metadata.get("dynamic", [])
                for field in ("dependencies", "optional-dependencies")
            ):
                raise ValueError(
                    "Explicit target requires static PEP 621 dependencies; dynamic dependencies unsupported"
                )
            dependencies = [*metadata.get("dependencies", [])]
            for values in metadata.get("optional-dependencies", {}).values():
                dependencies.extend(values)
            for values in project.get("dependency-groups", {}).values():
                dependencies.extend(value for value in values if isinstance(value, str))
            for dependency in dependencies:
                _requirement(dependency, cwd, target, extras)
            options, credentials = _pip_options(input, cwd, indexes, find_links, no_index)
            # Keep uv's extras/group selection, replacing only host-sensitive root markers.
            static = copy.deepcopy(project)
            optional = {
                canonicalize_name(extra): values
                for extra, values in metadata.get("optional-dependencies", {}).items()
            }
            dependency_groups = {
                canonicalize_name(group): [
                    {**value, "include-group": canonicalize_name(value["include-group"])}
                    if isinstance(value, dict) and "include-group" in value
                    else value
                    for value in values
                ]
                for group, values in project.get("dependency-groups", {}).items()
            }
            if len(optional) != len(metadata.get("optional-dependencies", {})) or len(
                dependency_groups
            ) != len(project.get("dependency-groups", {})):
                raise ValueError("Duplicate normalized extra or dependency group names")
            if "optional-dependencies" in metadata:
                static["project"]["optional-dependencies"] = optional
            if "dependency-groups" in project:
                static["dependency-groups"] = dependency_groups
            roots = []

            def evaluated(values, selected=True):
                result = []
                for value in values:
                    if not isinstance(value, str):
                        result.append(value)
                        continue
                    requirement = _requirement(value, cwd, target, extras)
                    if requirement is not None:
                        requirement.marker = None
                        result.append(str(requirement))
                        if selected:
                            roots.append(requirement)
                            _restriction(
                                requirement, _hashes(str(requirement), requirement), restrictions
                            )
                return result

            static["project"]["dependencies"] = evaluated(metadata.get("dependencies", []))
            for extra, values in optional.items():
                static["project"]["optional-dependencies"][extra] = evaluated(
                    values, extra in extras
                )
            selected_groups = set(groups)
            pending = list(groups)
            while pending:
                group = pending.pop()
                for value in dependency_groups.get(group, []):
                    included = value.get("include-group") if isinstance(value, dict) else None
                    if included and included not in selected_groups:
                        selected_groups.add(included)
                        pending.append(included)
            for group, values in dependency_groups.items():
                static["dependency-groups"][group] = evaluated(values, group in selected_groups)
            with tempfile.TemporaryDirectory(prefix="wheelport-project-") as temporary:
                path = Path(temporary) / "pyproject.toml"
                path.write_text(
                    "\n".join(f'"{key}" = {pylock._toml_value(value)}' for key, value in static.items()),
                    encoding="utf-8",
                )
                args = [
                    *compile_args,
                    "--no-config",
                    "--no-sources",
                    "--index-strategy",
                    "unsafe-best-match",
                    *options,
                    str(path),
                ]
                for extra in extras:
                    args += ["--extra", extra]
                for group in groups:
                    args += ["--group", f"{path}:{group}"]
                plan = _compile(
                    args, cwd, target, extras, groups, roots, restrictions, credentials, run
                )
        else:
            if extras or groups:
                raise ValueError("Extras and groups require a project input")
            requirements, constraints, source_options, restrictions, require_hashes = _requirements(
                input, target
            )
            options, credentials = _pip_options(input, cwd, indexes, find_links, no_index)
            options = _merge_options([*options, *source_options])
            candidates = [
                *source_options,
                *(Requirement(value).url or "" for value in [*requirements, *constraints]),
            ]
            credentials.extend(
                value for value in candidates if urlsplit(value).username is not None
            )
            with tempfile.TemporaryDirectory(prefix="wheelport-target-") as temporary:
                path = Path(temporary) / "requirements.txt"
                path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
                constraint = Path(temporary) / "constraints.txt"
                constraint.write_text("\n".join(constraints) + "\n", encoding="utf-8")
                plan = _compile(
                    [
                        *compile_args,
                        "--no-config",
                        "--no-sources",
                        "--index-strategy",
                        "unsafe-best-match",
                        *options,
                        "--constraint",
                        str(constraint),
                        str(path),
                    ],
                    cwd,
                    target,
                    extras,
                    groups,
                    [Requirement(value) for value in requirements],
                    restrictions,
                    credentials,
                    run,
                    require_hashes=require_hashes,
                )
    elif manager == "uv":
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
            "--no-build",
            "--python",
            sys.executable,
        ]
        if uv_root != cwd:
            member = project.get("project", {}).get("name")
            if not member:
                raise ValueError("uv workspace member must declare a project name")
            args += ["--project", str(uv_root), "--package", member, "--no-emit-package", member]
        else:
            args += ["--no-emit-project"]
        for extra in extras:
            args += ["--extra", extra]
        for group in groups:
            args += ["--group", group]
        if no_index or config.get("no-index", False):
            args += ["--no-index"]
        for index in indexes:
            args += ["--index", index]
        for link in find_links:
            args += [
                "--find-links",
                str(local) if (local := s._local_path(link, cwd)) is not None else link,
            ]
        plan = pylock._pylock(run(args, uv_root, scoped=False))
    else:
        lock = s._toml(cwd / "poetry.lock")
        run(["poetry", "check", "--lock"])
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
        exported = run(args)
        repositories = tool.get("poetry", {}).get("source", [])
        plan = {"lock-version": "1.0", "created-by": "wheelport", "packages": []}
        for line, requirement in pylock._exports(exported, extras, target=target):
            body = re.sub(r"\s+--hash(?:=|\s+)\S+", "", line).strip()
            requirement = _requirement(body, cwd, target, extras)
            if requirement is None:
                continue
            package = pylock._package(lock, requirement) if not requirement.url else None
            source = package.get("source", {}) if package else {}
            if source.get("type") not in {None, "legacy"}:
                raise ValueError("Explicit target Poetry source requires a prebuilt wheel URL")
            registry, scoped = "", options
            if source.get("url"):
                name = source.get("reference") or next(
                    (
                        item["name"]
                        for item in repositories
                        if s._identity(item["url"]) == s._identity(source["url"])
                    ),
                    None,
                )
                registry = s._credentials(source["url"], name, "poetry")
                if no_index:
                    raise ValueError("Cannot discover a locked Poetry repository with no_index")
                scoped = s._options([registry], [], False)
            hashes = _hashes(line, requirement)
            if package and not hashes:
                raise ValueError(f"Missing exported lock hashes for {requirement.name}")
            _restriction(requirement, hashes, restrictions)
            requirement.marker = None
            with tempfile.TemporaryDirectory(prefix="wheelport-poetry-") as temporary:
                path = Path(temporary) / "requirements.txt"
                path.write_text(str(requirement) + "\n", encoding="utf-8")
                discovered = pylock._pylock(
                    run(
                        [
                            *compile_args,
                            "--no-config",
                            "--no-sources",
                            "--no-deps",
                            "--index-strategy",
                            "first-index",
                            *scoped,
                            str(path),
                        ]
                    )
                )
            entries = _selected(discovered, target, extras, groups)
            if len(entries) != 1:
                raise ValueError("Poetry artifact discovery must produce exactly one package")
            entry = entries[0]
            if package:
                if canonicalize_name(entry["name"]) != canonicalize_name(
                    package["name"]
                ) or Version(entry.get("version", "0")) != Version(str(package["version"])):
                    raise ValueError("Poetry artifact discovery changed the locked package")
                # Local wheels need their actual hashes before matching the native file list.
                for artifact in entry.get("wheels", []):
                    _local_hash(artifact, cwd)
                files = {item["file"]: item["hash"] for item in package.get("files", [])}
                entry["wheels"] = [
                    artifact
                    for artifact in entry.get("wheels", [])
                    if (
                        (digest := "sha256:" + artifact.get("hashes", {}).get("sha256", ""))
                        in hashes
                        and files.get(s._artifact_name(artifact)) == digest
                    )
                ]
                entry.pop("archive", None)
            plan["packages"].append(entry)
            scopes[canonicalize_name(entry["name"])] = (registry, scoped)

    created = False
    try:
        plan["packages"] = _selected(plan, target, extras, groups)
        for package in plan["packages"]:
            _with_local_hashes(package, plan_cwd)
        # Reject source-only entries before touching their files or invoking retrieval.
        for package in plan["packages"]:
            if ("directory" in package or "vcs" in package) and not package.get("wheels"):
                raise ValueError("Explicit target local/VCS source requires a prebuilt wheel")
            for artifact in [
                *package.get("wheels", []),
                *([package["archive"]] if package.get("archive") else []),
            ]:
                filename = (
                    str(Path(artifact["path"]))
                    if "path" in artifact
                    else unquote(urlsplit(artifact.get("url", "")).path)
                )
                if not filename.lower().endswith(".whl"):
                    continue
                local = s._local_path(
                    Path(artifact["path"]) if "path" in artifact else artifact.get("url", ""),
                    plan_cwd,
                )
                if local:
                    _metadata(local, target, extras)
            s._artifact(package, plan_cwd, tags=target.tags(), wheel_only=True)
        pylock._write_plan(plan, resolution, plan_cwd)
        created = True
        downloads = []
        for package in _selected(s._toml(resolution), target, extras, groups):
            name = canonicalize_name(package["name"])
            url, digest = s._artifact(
                package, resolution.parent, tags=target.tags(), wheel_only=True
            )
            _check_hash(package, url, digest, restrictions)
            registry, scoped = scopes.get(name, (package.get("index", ""), options))
            if manager == "uv":
                candidates = [
                    item
                    for item in lock.get("package", [])
                    if canonicalize_name(item["name"]) == name
                    and Version(str(item.get("version", "0")))
                    == Version(package.get("version", "0"))
                ]
                if not registry and len(candidates) == 1:
                    registry = candidates[0].get("source", {}).get("registry", "")
                index = next(
                    (
                        item
                        for item in config.get("index", [])
                        if s._identity(item.get("url", "")) == s._identity(registry)
                    ),
                    {},
                )
                authenticated = s._credentials(index.get("url", registry), index.get("name"), "uv")
                url = s._userinfo(url, authenticated)
            else:
                url = s._pip_auth(url, [registry, *credentials])
            downloads.append((f"{name} @ {url} --hash={digest}", scoped))
        for requirement, scoped in downloads:
            with tempfile.TemporaryDirectory(prefix="wheelport-download-") as temporary:
                path = Path(temporary) / "requirements.txt"
                path.write_text(requirement + "\n", encoding="utf-8")
                run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "download",
                        "--isolated",
                        "--no-deps",
                        "--only-binary",
                        ":all:",
                        "--require-hashes",
                        *target.download_args(),
                        "--dest",
                        str(wheel_dir),
                        *scoped,
                        "-r",
                        str(path),
                    ]
                )
        for path in wheel_dir.glob("*.whl"):
            _metadata(path, target, extras)
    finally:
        if created:
            resolution.unlink(missing_ok=True)
    return manager


def _local_hash(artifact: dict, cwd: Path) -> None:
    local = s._local_path(
        Path(artifact["path"]) if "path" in artifact else artifact.get("url", ""), cwd
    )
    if local:
        declared = artifact.get("hashes", {}).get("sha256")
        if declared is not None and (
            not isinstance(declared, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", declared)
        ):
            raise ValueError(f"Invalid declared local wheel SHA256 hash: {local.name}")
        with local.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if declared is not None and actual != declared.lower():
            raise ValueError(f"Local wheel SHA256 hash mismatch: {local.name}")
        artifact.setdefault("hashes", {})["sha256"] = actual


def _with_local_hashes(package: dict, cwd: Path) -> dict:
    for artifact in [
        *package.get("wheels", []),
        *([package["archive"]] if package.get("archive") else []),
    ]:
        filename = (
            str(Path(artifact["path"]))
            if "path" in artifact
            else unquote(urlsplit(artifact.get("url", "")).path)
        )
        if filename.lower().endswith(".whl"):
            _local_hash(artifact, cwd)
    return package
