"""Single command-line entry point: resolve, retrieve, and pack in one step.

The three stages are an internal structure; users run one command that produces
a self-contained offline ZIP.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .pack import pack
from .targets import Target

MANAGERS = ["auto", "pip", "uv", "poetry"]


def _default_output(input: Path) -> Path:
    resolved = Path(input).resolve()
    name = resolved.name if resolved.is_dir() else resolved.stem
    return Path(f"{name or 'bundle'}.zip")


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
        description="Resolve, retrieve, and pack Python dependencies into one offline ZIP.",
    )
    parser.add_argument("--version", action="version", version=f"wheelpack {__version__}")
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=Path("."),
        help="requirements file, pyproject.toml, or project directory (default: .)",
    )
    parser.add_argument("-o", "--output", type=Path, help="output ZIP (default: <name>.zip)")
    parser.add_argument("--manager", choices=MANAGERS, default="auto")
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
    parser.add_argument("--extra", action="append", default=[], dest="extras", metavar="NAME")
    parser.add_argument("--group", action="append", default=[], dest="groups", metavar="NAME")
    parser.add_argument("--no-source", action="store_true", help="do not copy the project source")
    parser.add_argument("--force", action="store_true", help="replace an existing output")
    parser.add_argument("--python", help="interpreter used for retrieval")
    parser.add_argument("--python-version", help="target CPython major.minor[.patch]")
    parser.add_argument("--python-platform", help="target uv triple or wheel platform tag")
    parser.add_argument("--abi", help="target CPython ABI (for example cp312)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output or _default_output(args.input)
    try:
        if args.force:
            Path(output).unlink(missing_ok=True)
        result = pack(
            args.input,
            output,
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
    except (ValueError, OSError) as error:
        print(f"wheelpack: {error}", file=sys.stderr)
        return 1
    print(f"wrote {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
