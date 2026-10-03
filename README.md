# readmeta

Check that your README will actually render on PyPI — by inspecting the **built artifact**, not the repo.

## The problem

PyPI renders the `long_description` from your built artifact (the body of `METADATA` in a wheel / `PKG-INFO` in an sdist). It does **not** render your repo's `README.md`. Things that look perfect on GitHub silently break on PyPI:

- **Relative images** (`![shot](docs/shot.png)`) → 404, PyPI has no `docs/` folder
- **Relative links** (`[usage](docs/usage.md)`) → resolve against `pypi.org`, broken
- **Raw SVG references** (`assets/logo.svg`) → 404; PyPI doesn't serve repo files
- **In-page anchors** (`[setup](#instalation)`) → go nowhere when the heading id doesn't exist

`twine check` validates that the description *renders* — it does not check that any link or image actually *resolves*.

Real cases this catches:

- `armsmith` v1.2.1 shipped a relative SVG badge that 404'd on its PyPI page while rendering fine on GitHub.
- `aicertify` maintains a separate `README-pypi.md` with rewritten absolute URLs, precisely because the GitHub README breaks on PyPI.
- An audit of the `modern-python` GitHub org found 23 of 25 packages shipping relative asset references that can't resolve on PyPI.

## Install

```bash
pip install readmeta
```

Requires Python 3.9+. No dependencies — stdlib only.

## Usage

Check your built artifacts (build first, then check what PyPI will actually see):

```bash
python -m build
readmeta check dist/*
```

Or check what's currently hosted on PyPI:

```bash
readmeta check --pypi requests
```

Example output:

```
fakepkg 0.1.0  [text/markdown]  <- dist/fakepkg-0.1.0-py3-none-any.whl
Found 2 issue(s):

  [relative-image] dist/fakepkg-0.1.0-py3-none-any.whl:12
      docs/shot.png
      -> PyPI renders the description standalone; relative image paths 404. Use an absolute https:// URL (e.g. raw.githubusercontent.com).

  [broken-anchor] dist/fakepkg-0.1.0-py3-none-any.whl:20
      #instalation
      -> No heading with a matching id was found; the link goes nowhere on PyPI.
```

Exit codes are CI-friendly: `0` = clean, `1` = issues found, `2` = error (unreadable artifact, PyPI unreachable, bad usage).

## CI example

```yaml
- name: Build
  run: python -m build

- name: Check PyPI rendering
  run: |
    pip install readmeta
    readmeta check dist/*
```

## How it works

1. Reads `long_description` from `.whl` (`zipfile` → `.dist-info/METADATA`) or `.tar.gz` (`tarfile` → `PKG-INFO`), plus the `Description-Content-Type` header.
2. For `text/html`, parses with `html.parser` and validates every `img[src]`, `a[href]`, and `#anchor` against collected element ids (explicit ids plus GitHub-style heading slugs).
3. For Markdown/RST (what artifacts actually carry — the raw source, not rendered HTML), scans with regexes: inline and reference-style images/links, embedded `<img>` tags, RST `image::`/`figure::` directives, and `#anchor` links against `# Heading` slugs.
4. `--pypi` mode fetches `https://pypi.org/pypi/<name>/json` and checks the hosted description.

## Limitations (v0.1)

- **Check only** — no `--fix`. A build-time rewrite mode (convert relative refs to absolute URLs at build time) is planned.
- Anchor validation for RST is limited (Markdown headings and HTML ids are covered; RST `.. _target:` definitions are not yet resolved).
- Code spans and fenced code blocks are ignored (documenting a bad pattern doesn't flag it); indented code blocks are still scanned.
- Heading-slug generation approximates GitHub/PyPI's algorithm; exotic headings could produce false positives — explicit `id` attributes always win.
- `--pypi` uses the raw `description` from the JSON API (the API doesn't expose rendered HTML); findings are identical to checking a fresh local build.
