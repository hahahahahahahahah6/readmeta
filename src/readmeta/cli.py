"""Command-line interface for readmeta."""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error

from . import __version__
from .check import _PYPI_NAME_RE, check_description, fetch_pypi, read_artifact
from .fix import _REPO_RE, fixed_artifact_name, rewrite_artifact


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
    chk.add_argument(
        "--fix",
        action="store_true",
        help=(
            "rewrite fixable issues (relative images/links, raw SVGs) to absolute "
            "URLs at build time and write fixed artifacts (sources untouched)"
        ),
    )
    chk.add_argument(
        "--base-url",
        metavar="URL",
        help="base URL that relative references resolve against (e.g. "
        "https://raw.githubusercontent.com/OWNER/NAME/main/)",
    )
    chk.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="GitHub repo used to build absolute URLs: images -> "
        "raw.githubusercontent.com, other links -> github.com/.../blob/...",
    )
    chk.add_argument(
        "--ref",
        default="main",
        help="git ref used with --repo (default: main)",
    )
    chk.add_argument(
        "--out-dir",
        metavar="DIR",
        help="where to write fixed artifacts (default: same directory as each input)",
    )

    args = ap.parse_args(argv)

    if args.command == "check":
        if args.pypi and args.paths:
            ap.error("--pypi cannot be combined with artifact paths")
        if args.fix and args.pypi:
            ap.error("--fix cannot be combined with --pypi (nothing to rewrite there)")
        if (args.base_url or args.repo or args.ref != "main" or args.out_dir) and not args.fix:
            ap.error("--base-url/--repo/--ref/--out-dir need --fix")
        if args.fix:
            if not args.base_url and not args.repo:
                ap.error("--fix needs --base-url URL or --repo OWNER/NAME")
            if args.base_url and args.repo:
                ap.error("--base-url and --repo are mutually exclusive")
            if args.base_url and not args.base_url.lower().startswith(("http://", "https://")):
                ap.error("--base-url must be an http(s) URL")
            if args.repo and not _REPO_RE.fullmatch(args.repo):
                ap.error(f"--repo must look like OWNER/NAME, got {args.repo!r}")
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
            if args.fix:
                worst = max(worst, _fix_one(args, path, name, version, ctype))
        return worst

    return 2  # pragma: no cover


def _fix_one(args: argparse.Namespace, path: str, name: str, version: str, ctype: str) -> int:
    """Rewrite one artifact's long_description; return the exit-code contribution."""
    import os

    out_dir = args.out_dir or os.path.dirname(path) or "."
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create --out-dir {out_dir!r}: {e}", file=sys.stderr)
        return 2
    out_path = os.path.join(out_dir, fixed_artifact_name(os.path.basename(path)))
    try:
        _, _, _, new_desc, rewrites = rewrite_artifact(
            path,
            out_path,
            base_url=args.base_url,
            repo=args.repo,
            ref=args.ref,
        )
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not rewrites:
        print(f"--fix: {out_path}: nothing to rewrite (already clean).")
        return 0
    print(f"--fix: rewrote {len(rewrites)} reference(s) -> {out_path}\n")
    for r in rewrites:
        print(f"  [{r.kind}] {r.context}\n      {r.old}\n      -> {r.new}\n")

    # Re-check the fixed description: anchors can't be rewritten, so they may remain.
    remaining = check_description(ctype, new_desc, out_path)
    if remaining:
        print(f"--fix: {len(remaining)} issue(s) could not be rewritten (manual fix needed):\n")
        for f in remaining:
            print(f"  [{f.kind}] {f.context}\n      {f.target}\n      -> {f.hint}\n")
        return 1
    print("--fix: fixed artifact is clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
