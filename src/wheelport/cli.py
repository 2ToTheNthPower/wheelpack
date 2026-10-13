"""Command-line entry point."""

import argparse
import sys
import zipfile
from pathlib import Path

from . import __version__
from .bundle import pack
from .sources import _redact
from .targets import Target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wheelport", description="Pack Python dependencies for fully air-gapped installation."
    )
    parser.add_argument("--version", action="version", version=f"wheelport {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    packing = commands.add_parser("pack", help="build and verify an offline wheel ZIP")
    packing.add_argument("input", type=Path, help="requirements file, pyproject.toml, or directory")
    packing.add_argument("-o", "--output", type=Path, required=True, help="new ZIP archive path")
    packing.add_argument("--manager", choices=["auto", "pip", "uv", "poetry"], default="auto")
    packing.add_argument("--index-url", action="append", default=[], dest="indexes",
                         help="repeat for multiple indexes; first is default")
    packing.add_argument("--find-links", action="append", default=[],
                         help="additional flat wheel directory or HTML page")
    packing.add_argument("--no-index", action="store_true", help="disable registry discovery")
    packing.add_argument("--extra", action="append", default=[], dest="extras")
    packing.add_argument("--group", action="append", default=[], dest="groups")
    packing.add_argument("--dependencies-only", action="store_true", help="do not build root project")
    packing.add_argument("--pip-wheel", type=Path, help="local pip wheel for installer bootstrap")
    packing.add_argument("--project-wheel", type=Path, help="prebuilt root project wheel")
    packing.add_argument("--python-version", help="target CPython major.minor[.patch]")
    packing.add_argument("--python-platform", help="target uv triple or supported wheel platform tag")
    packing.add_argument("--abi", help="target CPython ABI (e.g. cp311)")
    args = parser.parse_args(argv)
    try:
        options = {}
        if any(value is not None for value in (args.python_version, args.python_platform, args.abi)):
            options["target"] = Target.create(
                python_version=args.python_version, python_platform=args.python_platform, abi=args.abi,
            )
        if args.project_wheel is not None:
            options["project_wheel"] = args.project_wheel
        print(
            "Resolving target wheels; checking artifacts (target runtime installation not run)..."
            if "target" in options else
            "Resolving and building wheels for this Python/platform; checking offline installation...",
            file=sys.stderr,
        )
        result = pack(
            args.input, args.output, manager=args.manager, indexes=args.indexes,
            find_links=args.find_links, no_index=args.no_index, extras=args.extras,
            groups=args.groups, include_project=not args.dependencies_only, pip_wheel=args.pip_wheel,
            **options,
        )
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"wheelport: {_redact(str(exc))}", file=sys.stderr)
        return 1
    print(result)
    return 0
