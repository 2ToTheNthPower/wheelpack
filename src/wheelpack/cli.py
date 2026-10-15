"""Command-line entry point: resolve, retrieve, and pack."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, pylock, retrieve
from .pack import pack
from .targets import Target

MANAGERS = ["auto", "pip", "uv", "poetry"]


def _source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--index-url",
        action="append",
        default=[],
        dest="indexes",
        metavar="URL",
        help="package index; repeatable, the first is the default",
    )
    parser.add_argument(
        "--find-links",
        action="append",
        default=[],
        metavar="PATH",
        help="flat wheel directory or local HTML page; repeatable",
    )
    parser.add_argument("--no-index", action="store_true", help="disable registry discovery")


def _selection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--extra", action="append", default=[], dest="extras", metavar="NAME")
    parser.add_argument("--group", action="append", default=[], dest="groups", metavar="NAME")


def _target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--python-version", help="target CPython major.minor[.patch]")
    parser.add_argument("--python-platform", help="target uv triple or wheel platform tag")
    parser.add_argument("--abi", help="target CPython ABI (for example cp312)")


def _target(args) -> Target | None:
    if not any((args.python_version, args.python_platform, args.abi)):
        return None
    return Target.create(
        python_version=args.python_version,
        python_platform=args.python_platform,
        abi=args.abi,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wheelpack",
        description="Resolve, retrieve, and pack Python wheels for offline installs.",
    )
    parser.add_argument("--version", action="version", version=f"wheelpack {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve", help="generate a PEP 751 pylock from an input")
    resolve.add_argument("input", type=Path, help="requirements file, pyproject.toml, or directory")
    resolve.add_argument("-o", "--output", type=Path, default=Path("pylock.toml"))
    resolve.add_argument("--manager", choices=MANAGERS, default="auto")
    _source_options(resolve)
    _selection_options(resolve)

    retrieve_command = commands.add_parser(
        "retrieve", help="download or build the wheels a pylock names"
    )
    retrieve_command.add_argument("pylock", type=Path, help="a pylock.toml file")
    retrieve_command.add_argument("-o", "--output", type=Path, required=True, help="wheelhouse dir")
    retrieve_command.add_argument("--python", help="interpreter used for pip")
    _source_options(retrieve_command)
    _target_options(retrieve_command)

    pack_command = commands.add_parser(
        "pack", help="assemble source, wheels, and an installer into a ZIP"
    )
    pack_command.add_argument("input", type=Path, help="requirements file, pyproject.toml, or directory")
    pack_command.add_argument("-o", "--output", type=Path, required=True, help="new ZIP path")
    pack_command.add_argument("--manager", choices=MANAGERS, default="auto")
    pack_command.add_argument(
        "--no-source", action="store_true", help="do not copy the project source"
    )
    pack_command.add_argument("--python", help="interpreter used for retrieval")
    _source_options(pack_command)
    _selection_options(pack_command)
    _target_options(pack_command)
    return parser


def _resolve(args) -> int:
    source = pylock.detect(args.input, args.manager)
    resolution = pylock.resolve(
        source,
        indexes=args.indexes,
        find_links=args.find_links,
        no_index=args.no_index,
        extras=args.extras,
        groups=args.groups,
    )
    resolution.plan["packages"] = pylock.select(resolution.plan, args.extras, args.groups)
    pylock.write_plan(resolution.plan, args.output, resolution.plan_cwd)
    count = len(resolution.plan["packages"])
    print(f"wrote {args.output} ({count} packages, manager={resolution.manager})")
    return 0


def _retrieve(args) -> int:
    wheels = retrieve.retrieve(
        args.pylock,
        args.output,
        indexes=args.indexes,
        find_links=args.find_links,
        no_index=args.no_index,
        python=args.python,
        target=_target(args),
    )
    print(f"retrieved {len(wheels)} wheels into {args.output}")
    return 0


def _pack(args) -> int:
    output = pack(
        args.input,
        args.output,
        manager=args.manager,
        indexes=args.indexes,
        find_links=args.find_links,
        no_index=args.no_index,
        extras=args.extras,
        groups=args.groups,
        target=_target(args),
        python=args.python,
        include_source=not args.no_source,
    )
    print(f"wrote {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resolve":
            return _resolve(args)
        if args.command == "retrieve":
            return _retrieve(args)
        return _pack(args)
    except (ValueError, OSError) as error:
        print(f"wheelpack: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
