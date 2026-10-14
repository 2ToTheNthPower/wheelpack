"""Shared plumbing for resolving native locks and explicit targets.

Resolution -- turning requirements files, projects, and native locks into a
pylock plan -- lives in :mod:`wheelport.pylock`; explicit-target resolution
lives in :mod:`wheelport.targeted`. This module holds the low-level primitives
they share: subprocess execution, URL/path handling, option assembly, credential
discovery, and locked-artifact selection. It does not download or build wheels.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import url2pathname

from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version


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
        command = [sys.executable, "-m", *args] if args[0] in {"uv", "poetry"} else args
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, env=env, check=False
        )
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

