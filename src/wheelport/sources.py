"""Collect wheels for native or explicit targets, preserving lock sources."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import url2pathname

from packaging.markers import Marker, default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from .targets import Target


def _local_path(location: str | Path, cwd: Path) -> Path | None:
    if not location:
        return None
    if isinstance(location, Path) or Path(location).drive:
        return (cwd / location).resolve()
    parts = urlsplit(location)
    if not parts.scheme:
        return (cwd / location).resolve()
    if parts.scheme == "file":
        if parts.netloc not in {"", "localhost"}:
            raise ValueError("Only local file URLs are supported")
        # url2pathname performs percent decoding and native drive conversion once.
        return (cwd / url2pathname(parts.path)).resolve()
    return None


def _artifact_name(artifact: dict) -> str:
    if artifact.get("name"):
        return artifact["name"]
    if "path" in artifact:
        return Path(artifact["path"]).name
    return unquote(urlsplit(artifact.get("url", "")).path.rsplit("/", 1)[-1])


def _tokens(line: str) -> list[str]:
    parser = shlex.shlex(line, posix=True)
    parser.whitespace_split = True
    parser.commenters = ""
    # Requirements paths are not shell commands; backslashes are path separators.
    parser.escape = ""
    return list(parser)


def _redact(text: str) -> str:
    text = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+", "<redacted URL>", text)
    for key, value in os.environ.items():
        if value and (
            key.startswith(("UV_INDEX_", "POETRY_HTTP_BASIC_"))
            and key.endswith(("_USERNAME", "_PASSWORD"))
        ):
            text = text.replace(value, "<redacted credential>")
    return text


def _run(
    args: list[str],
    cwd: Path,
    *,
    scoped: bool = False,
    pip_isolated: bool = False,
    env_overrides: dict[str, str] | None = None,
) -> str:
    try:
        env = None
        if scoped or pip_isolated:
            env = {
                key: value
                for key, value in os.environ.items()
                if (
                    not scoped
                    or key
                    not in {
                        "UV_INDEX",
                        "UV_DEFAULT_INDEX",
                        "UV_INDEX_URL",
                        "UV_EXTRA_INDEX_URL",
                        "UV_FIND_LINKS",
                        "UV_NO_INDEX",
                        "UV_INDEX_STRATEGY",
                    }
                )
                and not key.startswith("PIP_")
            }
            env["PIP_CONFIG_FILE"] = os.devnull
        if env_overrides:
            env = {**(os.environ if env is None else env), **env_overrides}
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env, check=False)
    except OSError:
        raise ValueError(f"Could not run {Path(args[0]).name}") from None
    if result.returncode:
        raise ValueError(
            f"Command failed (exit {result.returncode}): {_redact(' '.join(args))}\n"
            f"{_redact(result.stderr or result.stdout)[-8000:]}"
        ) from None
    return result.stdout


def _toml(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        raise ValueError(f"Cannot read TOML file: {path.name}") from None


def _credentials(url: str, name: str | None, manager: str) -> str:
    if not name or urlsplit(url).scheme not in {"http", "https"}:
        return url
    key = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    prefix = f"UV_INDEX_{key}" if manager == "uv" else f"POETRY_HTTP_BASIC_{key}"
    username = os.environ.get(f"{prefix}_USERNAME")
    password = os.environ.get(f"{prefix}_PASSWORD")
    if username is None and password is None:
        return url
    parts = urlsplit(url)
    user = username if username is not None else unquote(parts.username or "")
    secret = password if password is not None else unquote(parts.password or "")
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(
        parts._replace(netloc=f"{quote(user, safe='')}:{quote(secret, safe='')}@{host}")
    )


def _identity(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1], query="", fragment="")
    ).rstrip("/")


def _same_origin(left: str, right: str) -> bool:
    def origin(url: str):
        try:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                return None
            port = (
                parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
            )
        except ValueError:
            return None
        return parts.scheme, parts.hostname.lower(), port

    source = origin(left)
    return source is not None and source == origin(right)


def _userinfo(url: str, authenticated: str) -> str:
    if not _same_origin(url, authenticated) or urlsplit(authenticated).username is None:
        return url
    # Preserve the target authority, including its explicit/default port spelling.
    userinfo = urlsplit(authenticated).netloc.rsplit("@", 1)[0]
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=userinfo + "@" + parts.netloc.rsplit("@", 1)[-1]))


def _options(indexes: list[str], links: list[str], no_index: bool) -> list[str]:
    args = ["--no-index"] if no_index else []
    if indexes and not no_index:
        args += ["--index-url", indexes[0]]
        for index in indexes[1:]:
            args += ["--extra-index-url", index]
    for link in links:
        args += ["--find-links", link]
    return args


def _uv_root(cwd: Path) -> Path:
    for root in cwd.parents:
        metadata = root / "pyproject.toml"
        if not metadata.is_file():
            continue
        workspace = _toml(metadata).get("tool", {}).get("uv", {}).get("workspace")
        if workspace is None:
            continue
        included = any(
            cwd == member.resolve() for p in workspace.get("members", []) for member in root.glob(p)
        )
        excluded = any(
            cwd == member.resolve() for p in workspace.get("exclude", []) for member in root.glob(p)
        )
        return root if included and not excluded else cwd
    return cwd


def _uv_options(
    config: dict, indexes: list[str], links: list[str], no_index: bool, root: Path
) -> list[str]:
    native = [item for item in config.get("index", []) if not item.get("explicit", False)]
    defaults = [item for item in native if item.get("default", False)]
    if len(defaults) > 1:
        raise ValueError("uv configuration has multiple default indexes")
    default = (
        defaults[0] if defaults else {"url": config.get("index-url", "https://pypi.org/simple")}
    )
    registries = [
        _credentials(default["url"], default.get("name"), "uv"),
        *indexes,
        *[
            _credentials(item["url"], item.get("name"), "uv")
            for item in native
            if not item.get("default", False)
        ],
        *config.get("extra-index-url", []),
    ]
    configured_links = [
        str(local) if (local := _local_path(link, root)) is not None else link
        for link in config.get("find-links", [])
    ]
    return _options(
        registries, [*links, *configured_links], no_index or config.get("no-index", False)
    )


def _uv_build(target: str, root: Path, wheel_dir: Path, args: list[str]) -> None:
    _run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-create-gitignore",
            "--out-dir",
            str(wheel_dir),
            "--python",
            sys.executable,
            "--project",
            str(root),
            *args,
            target,
        ],
        root,
        pip_isolated=True,
    )


def _wheel(
    requirements: list[str],
    cwd: Path,
    wheel_dir: Path,
    options: list[str],
    *,
    locked: bool = False,
    hashed: bool = False,
    scoped: bool = False,
) -> None:
    if not requirements:
        return
    with tempfile.TemporaryDirectory(prefix="wheelport-") as temporary:
        path = Path(temporary) / "requirements.txt"
        path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        args = [sys.executable, "-m", "pip", "wheel", "--wheel-dir", str(wheel_dir)]
        if scoped:
            args.append("--isolated")
        if locked:
            args.append("--no-deps")
        if hashed:
            args.append("--require-hashes")
        _run(args + options + ["-r", str(path)], cwd, **({"scoped": True} if scoped else {}))


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


def _artifact(package: dict, cwd: Path, *, tags=None, wheel_only: bool = False) -> tuple[str, str]:
    ranks = {tag: rank for rank, tag in enumerate(sys_tags() if tags is None else tags)}
    compatible = []
    artifacts = package.get("wheels", [])
    if wheel_only and package.get("archive"):
        artifacts = [*artifacts, package["archive"]]
    for artifact in artifacts:
        location = artifact.get("path", artifact.get("url", ""))
        if wheel_only:
            parts = urlsplit(artifact.get("url", "")) if "path" not in artifact else None
            if (
                (parts is not None and parts.scheme not in {"http", "https", "file"})
                or not (str(Path(location)) if parts is None else unquote(parts.path))
                .lower()
                .endswith(".whl")
                or (
                    parts is not None
                    and parts.scheme == "file"
                    and parts.netloc not in {"", "localhost"}
                )
                or artifact.get("subdirectory")
            ):
                continue
        try:
            name, version, _, tags = parse_wheel_filename(_artifact_name(artifact))
        except ValueError:
            continue
        if canonicalize_name(name) != canonicalize_name(package["name"]) or (
            package.get("version") and version != Version(str(package["version"]))
        ):
            continue
        priority = min((ranks[tag] for tag in tags if tag in ranks), default=None)
        if priority is not None:
            compatible.append((priority, artifact))
    artifact = (None if wheel_only else package.get("archive")) or (
        min(compatible, key=lambda item: item[0])[1]
        if compatible
        else (None if wheel_only else package.get("sdist"))
    )
    if not artifact:
        kind = "wheel" if wheel_only else "wheel or sdist"
        raise ValueError(f"No compatible locked {kind} for {package['name']}")
    location = artifact.get("path", artifact.get("url", ""))
    if not location:
        raise ValueError(f"Missing locked artifact for {package['name']}")
    if "path" in artifact:
        location = (cwd / Path(location)).resolve().as_uri()
    elif urlsplit(location).scheme == "file":
        _local_path(location, cwd)  # Reject remote file authorities before retrieval.
    digest = artifact.get("hashes", {}).get("sha256", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError(f"Missing locked artifact or SHA256 hash for {package['name']}")
    if artifact.get("subdirectory"):
        location += "#subdirectory=" + quote(artifact["subdirectory"])
    return location, f"sha256:{digest}"


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
    plan = {"lock-version": "1.0", "created-by": "wheelport (pip)", "packages": []}
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
                "path": str(_local_path(url, Path.cwd())),
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


def _pip_credentials(input: Path, cwd: Path, cli_urls: list[str]) -> list[str]:
    urls = []
    config = _run([sys.executable, "-m", "pip", "config", "list"], cwd)
    configured = []
    # Environment overrides install-specific configuration, which overrides global.
    for line in sorted(
        config.splitlines(),
        key=lambda line: (
            0 if line.startswith(":env:.") else 1 if line.startswith("install.") else 2
        ),
    ):
        key, separator, value = line.partition("=")
        if separator and key.endswith((".index-url", ".extra-index-url", ".find-links")):
            try:
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, str):
                configured.extend(value.split())
    visited = set()

    def read(path: Path) -> None:
        path = path.resolve()
        if path in visited:
            return
        visited.add(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return  # pip, not this credential scan, validates requirements inputs.
        text = re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m[1], m[0]), text)
        text = re.sub(r"\\\s*\n", " ", text)
        for line in text.splitlines():
            line = re.split(r"\s+#", line.strip(), maxsplit=1)[0]
            if line.startswith("#"):
                continue
            urls.extend(re.findall(r"https?://[^\s\"'<>]+", line))
            try:
                tokens = _tokens(line)
            except ValueError:
                continue
            for position, token in enumerate(tokens):
                include = ""
                if token in {"-r", "-c", "--requirement", "--constraint"}:
                    if position + 1 < len(tokens):
                        include = tokens[position + 1]
                elif token.startswith(("--requirement=", "--constraint=")):
                    include = token.split("=", 1)[1]
                elif token.startswith(("-r", "-c")):
                    include = token[2:]
                if include:
                    try:
                        local = _local_path(include, path.parent)
                    except ValueError:
                        continue
                    if local is not None:
                        read(local)

    read(input)
    return [url for url in [*urls, *cli_urls, *configured] if urlsplit(url).username is not None]


def _pip_auth(url: str, candidates: list[str]) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.username is not None:
        return url
    for candidate in candidates:
        authenticated = urlsplit(candidate)
        if _same_origin(url, candidate) and authenticated.username is not None:
            return _userinfo(url, candidate)
    return url


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
                local = _local_path(
                    Path(source["path"]) if "path" in source else source.get("url", ""), cwd
                )
                if local and not source.get("hashes", {}).get("sha256"):
                    with local.open("rb") as stream:
                        source.setdefault("hashes", {})["sha256"] = hashlib.file_digest(
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


def collect(
    input: Path,
    manager: str,
    wheel_dir: Path,
    *,
    indexes: list[str],
    find_links: list[str],
    no_index: bool,
    extras: list[str],
    groups: list[str],
    include_project: bool = True,
    target: Target | None = None,
) -> str:
    """Build/download wheels and return the effective manager (pip, uv, or poetry).

    With target=None, use the running Python/platform unchanged. Explicit targets
    dispatch to wheel-only collection and never build sources. All inputs normalize to an
    ephemeral PEP 751 plan before retrieval. ``pip`` requirements use native pip
    resolution, including input hashes, configuration, and nested includes. PEP 621
    project inputs still require uv for dependency-only resolution. Poetry export
    hashes are explicitly enforced.
    Poetry also requires its native export plugin. Environment credentials and
    .netrc are supported; native credential keyrings are not promised. URL credentials
    in pip configuration and local nested requirements transfer only to the same origin.
    For remote includes, supply credentials through pip configuration or .netrc.
    uv roots/local directories build natively. Archive/VCS builds use isolated pip
    with global non-explicit uv indexes; per-package build dependency index mappings
    and first-index selection across multiple registries cannot be represented and
    are rejected for those builds rather than silently broadening their sources.
    Ambient UV source overrides also require native builds for archives/VCS.
    """
    input = Path(input).resolve()
    wheel_dir = Path(wheel_dir).resolve()
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
    project = _toml(input) if is_project else {}
    tool = project.get("tool", {})
    uv_root = _uv_root(cwd) if is_project and manager in {"uv", "auto"} else cwd
    if manager == "auto":
        if is_project and ((uv_root / "uv.lock").exists() or "uv" in tool or uv_root != cwd):
            manager = "uv"
        elif is_project and ((cwd / "poetry.lock").exists() or "poetry" in tool):
            manager = "poetry"
        else:
            manager = "pip"
    if manager != "pip" and not is_project:
        raise ValueError(f"{manager} requires a pyproject.toml input")
    if target is not None:
        from . import targeted

        return targeted.collect(
            input,
            manager,
            wheel_dir,
            target=target,
            indexes=indexes,
            find_links=find_links,
            no_index=no_index,
            extras=extras,
            groups=groups,
            include_project=include_project,
            project=project,
            uv_root=uv_root,
        )
    wheel_dir.mkdir(parents=True, exist_ok=True)
    options = _options(indexes, find_links, no_index)
    compile_args = ["uv", "pip", "compile", "--format", "pylock.toml", "--python", sys.executable]
    lock = {}
    config = {}
    scopes = {}
    locked_hashes = {}
    credential_urls = [*indexes, *find_links]
    uv_args = []
    plan_cwd = cwd
    if manager == "pip":
        if not is_project and (extras or groups):
            raise ValueError("Extras and groups require a project input")
        if not is_project:
            with tempfile.TemporaryDirectory(prefix="wheelport-pip-") as temporary:
                report = Path(temporary) / "report.json"
                _run(
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
            credential_urls = _pip_credentials(input, cwd, credential_urls)
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
            plan = _pylock(_run(args, cwd))
    if manager == "uv":
        lock = _toml(uv_root / "uv.lock")
        root_tool = _toml(uv_root / "pyproject.toml").get("tool", {})
        config = (
            _toml(uv_root / "uv.toml")
            if (uv_root / "uv.toml").exists()
            else root_tool.get("uv", {})
        )
        options = _uv_options(config, indexes, find_links, no_index, uv_root)
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
                str(local) if (local := _local_path(link, cwd)) is not None else link,
            ]
        plan = _pylock(_run(args + uv_args, uv_root))
    elif manager == "poetry":
        lock = _toml(cwd / "poetry.lock")
        _run(["poetry", "check", "--lock"], cwd)
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
        exported = _run(args, cwd)
        sources = tool.get("poetry", {}).get("source", [])
        plan = {"lock-version": "1.0", "created-by": "wheelport", "packages": []}
        for line, requirement in _exports(exported, extras):
            package = _package(lock, requirement) if requirement and not requirement.url else None
            source = package.get("source", {}) if package else {}
            scoped = options
            registry = ""
            if source.get("url"):
                name = source.get("reference")
                if not name:
                    name = next(
                        (
                            item["name"]
                            for item in sources
                            if _identity(item["url"]) == _identity(source["url"])
                        ),
                        None,
                    )
                registry = _credentials(source["url"], name, "poetry")
                if no_index:
                    raise ValueError("Cannot discover a locked Poetry repository with no_index")
                scoped = _options([registry], [], False)
            if package and "--hash" not in line:
                raise ValueError(f"Missing exported lock hashes for {requirement.name}")
            # Discover artifacts, never resolve the native dependency graph again.
            with tempfile.TemporaryDirectory(prefix="wheelport-poetry-") as temporary:
                requirements = Path(temporary) / "requirements.txt"
                requirements.write_text(line + "\n", encoding="utf-8")
                discovered = _pylock(
                    _run(
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
                        filename = _artifact_name(artifact)
                        digest = "sha256:" + artifact.get("hashes", {}).get("sha256", "")
                        if digest in hashes and files.get(filename) == digest:
                            accepted.append(artifact)
                    if key == "wheels":
                        entry[key] = accepted
                    elif accepted:
                        entry[key] = accepted[0]
                    else:
                        entry.pop(key, None)
                _artifact(entry, cwd)  # Fail closed if discovery has no native-locked artifact.
            plan["packages"].append(entry)
            scopes[canonicalize_name(entry["name"])] = (registry, scoped)

    resolution = wheel_dir.parent / "resolved.pylock.toml"
    # The filename is intentionally outside wheel_dir and is removed even on failure.
    if resolution.exists() or resolution.is_symlink():
        raise ValueError("Ephemeral resolution path already exists: resolved.pylock.toml")
    created = False
    try:
        plan["packages"] = _selected(plan, extras, groups)
        _write_plan(plan, resolution, plan_cwd)
        created = True
        selected = _selected(_toml(resolution), extras, groups)
        targets = []
        for package in selected:
            name = canonicalize_name(package["name"])
            registry, scoped = scopes.get(name, (package.get("index", ""), options))
            if manager == "uv" and not registry and package.get("version"):
                candidates = [
                    item
                    for item in lock.get("package", [])
                    if canonicalize_name(item["name"]) == name
                    and Version(str(item["version"])) == Version(package["version"])
                ]
                if len(candidates) == 1:
                    registry = candidates[0].get("source", {}).get("registry", "")
            if "directory" in package:
                source = package["directory"]
                target = str(Path(source["path"]) / source.get("subdirectory", ""))
                hashed = False
            elif "vcs" in package:
                source = package["vcs"]
                if not source.get("commit-id"):
                    raise ValueError(f"Missing locked VCS commit for {name}")
                url = source.get("url") or Path(source["path"]).as_uri()
                if manager == "pip" and not is_project:
                    url = _pip_auth(url, credential_urls)
                target = f"{name} @ {source['type']}+{url}@{source['commit-id']}"
                if source.get("subdirectory"):
                    target += "#subdirectory=" + quote(source["subdirectory"])
                hashed = False
            else:
                url, digest = _artifact(package, resolution.parent)
                if manager == "pip" and not is_project:
                    url = _pip_auth(url, credential_urls)
                if name in locked_hashes and digest not in locked_hashes[name]:
                    raise ValueError(
                        f"Discovered artifact hash is not in the Poetry export for {name}"
                    )
                if not registry:
                    registry = next(
                        (
                            candidate
                            for candidate in credential_urls
                            if _same_origin(candidate, url)
                        ),
                        "",
                    )
                # Credentials must never cross an origin boundary or downgrade TLS.
                if registry and _same_origin(url, registry):
                    if manager == "uv":
                        index = next(
                            (
                                item
                                for item in config.get("index", [])
                                if _identity(item.get("url", "")) == _identity(registry)
                            ),
                            {},
                        )
                        authenticated = index.get("url", registry)
                        url = _userinfo(url, authenticated)
                        if _same_origin(url, authenticated):
                            url = _credentials(url, index.get("name"), "uv")
                    elif urlsplit(registry).username is not None and urlsplit(url).username is None:
                        url = _userinfo(url, registry)
                target = f"{name} @ {url} --hash={digest}"
                hashed = True
            native_build = manager == "uv" and "directory" in package
            needs_build = not hashed or not urlsplit(url).path.lower().endswith(".whl")
            if manager == "uv" and needs_build and not native_build:
                mappings = [
                    *config.get("sources", {}).values(),
                    *root_tool.get("uv", {}).get("sources", {}).values(),
                    *tool.get("uv", {}).get("sources", {}).values(),
                ]
                if any(
                    "index" in source
                    for mapping in mappings
                    for source in (mapping if isinstance(mapping, list) else [mapping])
                ):
                    raise ValueError(
                        "uv archive/VCS build dependency index mappings require native build support"
                    )
                if (
                    options.count("--extra-index-url")
                    and config.get("index-strategy", "first-index") != "unsafe-best-match"
                ):
                    raise ValueError(
                        "uv archive/VCS builds cannot preserve first-index rules across multiple indexes"
                    )
                if any(
                    os.environ.get(key)
                    for key in (
                        "UV_INDEX",
                        "UV_DEFAULT_INDEX",
                        "UV_INDEX_URL",
                        "UV_EXTRA_INDEX_URL",
                        "UV_FIND_LINKS",
                        "UV_NO_INDEX",
                        "UV_INDEX_STRATEGY",
                        "UV_CONFIG_FILE",
                    )
                ):
                    raise ValueError(
                        "uv archive/VCS builds with ambient UV source overrides require native build support"
                    )
            isolated = manager == "uv" or (manager == "poetry" and bool(registry))
            targets.append((target, scoped, hashed, native_build, isolated))
        for target, scoped, hashed, native_build, isolated in targets:
            if native_build:
                _uv_build(target, uv_root, wheel_dir, uv_args)
            else:
                _wheel(
                    [target],
                    cwd,
                    wheel_dir,
                    scoped,
                    locked=True,
                    hashed=hashed,
                    **({"scoped": True} if isolated else {}),
                )
        build_project = is_project and bool(project.get("build-system"))
        if manager == "uv":
            build_project = tool.get("uv", {}).get("package", build_project)
        elif manager == "poetry":
            build_project = build_project and tool.get("poetry", {}).get("package-mode", True)
        if include_project and build_project:
            if manager == "uv":
                _uv_build(str(cwd), uv_root, wheel_dir, uv_args)
            else:
                _wheel([str(cwd)], cwd, wheel_dir, options, locked=True)
    finally:
        if created:
            resolution.unlink(missing_ok=True)
    return manager
