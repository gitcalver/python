# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import argparse
import importlib.metadata
import sys
from dataclasses import dataclass
from typing import NoReturn

from gitcalver._errors import ExitError
from gitcalver._format import Format
from gitcalver._version import forward, is_version_string, reverse

USAGE = """\
Usage: gitcalver [options] [REVISION | VERSION]

Compute a gitcalver version for a git commit, or find the commit for a version.

Options:
  --prefix PREFIX     Prepend PREFIX to the version (default: none)
  --dirty STRING      Allow dirty workspace; append STRING.HASH as suffix
  --no-dirty          Refuse dirty workspace (overrides --dirty)
  --no-dirty-hash     Suppress .HASH in dirty suffix (requires --dirty)
  --branch BRANCH     Override default branch detection
  --short             Output short commit hash (reverse mode only)
  --version           Show version and exit
  --help              Show this help
"""


def _package_version() -> str:
    try:
        return importlib.metadata.version("gitcalver")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@dataclass
class Args:
    help: bool = False
    version: bool = False
    prefix: str = ""
    dirty: str = ""
    no_dirty: bool = False
    no_dirty_hash: bool = False
    branch: str | None = None
    short: bool = False
    positional: str | None = None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ExitError(1, message)


class _NonEmptyStrAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        if not values:
            parser.error(f"{option_string} requires a non-empty string")
        setattr(namespace, self.dest, values)


# Options that take a string value; used by `_normalize_argv` to rewrite the
# space-separated form to `--opt=value` so argparse accepts values starting
# with `-`. Keep in sync with the non-boolean options declared in `_parse_args`.
_OPTS_TAKING_VALUE = frozenset({"--prefix", "--dirty", "--branch"})


def _normalize_argv(argv: list[str]) -> list[str]:
    """Rewrite `--opt value` to `--opt=value` for options in `_OPTS_TAKING_VALUE`.

    argparse otherwise refuses values that begin with `-` in the space-separated
    form (so `--dirty -dirty` fails, though `--dirty=-dirty` works). Rewriting
    makes both forms equivalent. Stops at `--` so values after the terminator
    are passed through unchanged.
    """
    result: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            result.extend(argv[i:])
            return result
        if arg in _OPTS_TAKING_VALUE and i + 1 < len(argv):
            result.append(f"{arg}={argv[i + 1]}")
            i += 2
        else:
            result.append(arg)
            i += 1
    return result


def _parse_args(argv: list[str]) -> Args:
    parser = _Parser(prog="gitcalver", add_help=False, allow_abbrev=False)
    parser.add_argument("--help", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--dirty", action=_NonEmptyStrAction, default="")
    parser.add_argument("--no-dirty", action="store_true")
    parser.add_argument("--no-dirty-hash", action="store_true")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--short", action="store_true")
    parser.add_argument("positional", nargs="?", default=None)

    args = parser.parse_args(_normalize_argv(argv), namespace=Args())

    if args.no_dirty_hash and not args.dirty:
        parser.error("--no-dirty-hash requires --dirty")

    return args


def _build_format(args: Args) -> Format:
    dirty_suffix: str | None = None if args.no_dirty else (args.dirty or None)
    return Format(
        prefix=args.prefix,
        dirty_suffix=dirty_suffix,
        dirty_hash=not args.no_dirty_hash,
    )


def run(argv: list[str], *, dir: str | None = None) -> tuple[str, int]:
    try:
        args = _parse_args(argv)
    except ExitError as e:
        return f"gitcalver: {e.message}", e.code

    if args.help:
        return USAGE.rstrip("\n"), 0

    if args.version:
        return f"gitcalver {_package_version()}", 0

    lookup: str | None = None
    # Require the prefix to match before reverse-lookup; avoids treating a
    # bare `20260410.1` as a version when --prefix is set.
    if args.positional is not None and args.positional.startswith(args.prefix):
        candidate = args.positional.removeprefix(args.prefix)
        if is_version_string(candidate):
            lookup = candidate

    if args.short and lookup is None:
        return "gitcalver: --short is only valid in reverse lookup mode", 1

    try:
        if lookup is not None:
            result = reverse(
                dir=dir,
                version_str=lookup,
                branch_override=args.branch,
                short=args.short,
            )
        else:
            fmt = _build_format(args)
            result = forward(
                dir=dir,
                revision=args.positional,
                fmt=fmt,
                branch_override=args.branch,
            )
    except ExitError as e:
        return f"gitcalver: {e.message}", e.code

    return result, 0


def main(argv: list[str] | None = None) -> NoReturn:
    if argv is None:
        argv = sys.argv[1:]
    output, code = run(argv)
    print(output, file=sys.stderr if code != 0 else sys.stdout)
    sys.exit(code)
