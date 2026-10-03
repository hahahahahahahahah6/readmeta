"""Command-line interface for readmeta."""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error

from . import __version__
from .check import _PYPI_NAME_RE, check_description, fetch_pypi, read_artifact


def _report(name: str, version: str, content_type: str, source: str, description: str) -> int:
    findings = check_description(content_type, description, source)
    print(f"{name} {version}  [{content_type}]  <- {source}")
    if not findings:
        print("OK: no PyPI rendering issues found.")
        return 0
    print(f"Found {len(findings)} issue(s):\n")
    for f in findings:
        print(f"  [{f.kind}] {f.context}\n      {f.target}\n      -> {f.hint}\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="readmeta",
        description=(
            "Check whether a package's README will render correctly on PyPI. "
            "Inspects the built artifact's long_description (what PyPI actually "
            "renders), not the repo's README.md."
        ),
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check", help="check artifacts or a PyPI project for rendering issues")
    chk.add_argument("paths", nargs="*", help="wheel (.whl) and/or sdist (.tar.gz, .zip) files")
    chk.add_argument("--pypi", metavar="NAME", help="check the description hosted on PyPI instead of local files")

    args = ap.parse_args(argv)

    if args.command == "check":
        if args.pypi and args.paths:
            ap.error("--pypi cannot be combined with artifact paths")
        if args.pypi:
            if not _PYPI_NAME_RE.fullmatch(args.pypi):
                print(f"error: invalid PyPI project name: {args.pypi!r}", file=sys.stderr)
                return 2
            try:
                name, version, ctype, desc = fetch_pypi(args.pypi)
            except urllib.error.HTTPError as e:
                print(f"error: PyPI request failed ({e.code} {e.reason})", file=sys.stderr)
                return 2
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            except OSError as e:
                print(f"error: could not reach PyPI: {e}", file=sys.stderr)
                return 2
            return _report(name, version, ctype, f"pypi:{args.pypi}", desc)

        if not args.paths:
            ap.error("check needs at least one artifact path or --pypi NAME")
        worst = 0
        for path in args.paths:
            try:
                name, version, ctype, desc = read_artifact(path)
            except (OSError, ValueError) as e:
                print(f"error: {e}", file=sys.stderr)
                worst = max(worst, 2)
                continue
            rc = _report(name, version, ctype, path, desc)
            worst = max(worst, rc)
        return worst

    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
