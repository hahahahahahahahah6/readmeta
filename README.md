# readmeta

Check that your README will actually render on PyPI — by inspecting the **built artifact**, not the repo. And with `--fix`, rewrite the broken references at build time.

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

### `--fix`: rewrite at build time (v0.2)

Checking tells you what's broken; `--fix` rewrites it. This is a **build-pipeline
step**, not a source edit — your source README keeps its GitHub-friendly relative
paths, and the *built artifacts* get absolute URLs:

```bash
python -m build
readmeta check dist/* --fix --repo octo/demo
twine upload dist/*.fixed.*
```

Why build-time and not source-time? Two constraints:

1. The artifact is derived output — hand-editing `dist/*.whl` is not a repeatable step.
2. Baking absolute URLs into the *source* README is wrong for GitHub: a
   tag-pinned `raw.githubusercontent.com` URL 404s until the tag exists
   (chicken-and-egg), and a branch-pinned URL drifts. Keep relative paths in
   the source; let the pipeline rewrite them for PyPI.

What `--fix` does:

- Rewrites every `relative-image` / `relative-link` / `relative-svg` finding to an
  absolute URL. Images (incl. `.svg`) resolve to
  `https://raw.githubusercontent.com/<repo>/<ref>/<path>`; other relative links
  to `https://github.com/<repo>/blob/<ref>/<path>`. Use `--ref` to pin something
  other than `main`.
- Or resolve everything against your own base with `--base-url
  https://cdn.example.com/static/` (mutually exclusive with `--repo`).
- Writes `*.fixed.whl` / `*.fixed.tar.gz` next to the inputs (or into `--out-dir`).
  Inputs are never modified.
- Never touches fenced code blocks, inline code spans, or `<pre>`/`<code>` —
  documentation *about* a bad pattern is not rewritten.
- Cannot rewrite broken in-page anchors (`#anchor` has no sensible absolute form);
  those are reported as remaining issues for you to fix by hand.

Example:

```
$ readmeta check dist/* --fix --repo octo/demo
...
--fix: rewrote 3 reference(s) -> dist/fakepkg-0.1.0-py3-none-any.fixed.whl

  [relative-image] dist/fakepkg-0.1.0-py3-none-any.whl:12
      docs/shot.png
      -> https://raw.githubusercontent.com/octo/demo/main/docs/shot.png

--fix: 1 issue(s) could not be rewritten (manual fix needed):

  [broken-anchor] dist/fakepkg-0.1.0-py3-none-any.fixed.whl:20
      #instalation
      -> No heading with a matching id was found; the link goes nowhere on PyPI.
```

With `--fix`, the exit code is `0` when everything was clean or fully rewritten,
`1` when unrewritable issues (anchors) remain, `2` on error.

## CI example

```yaml
- name: Build
  run: python -m build

- name: Check PyPI rendering
  run: |
    pip install readmeta
    readmeta check dist/*

- name: Rewrite for PyPI and upload the fixed artifacts
  run: |
    readmeta check dist/* --fix --repo ${{ github.repository }}
    twine upload dist/*.fixed.*
```

## Differentiation

- **Inspects the built artifact, not the repo.** Other README linters scan your
  `README.md`; readmeta reads the `long_description` out of the wheel/sdist —
  the exact bytes PyPI will render. A separate `README-pypi.md` workflow can't
  drift from what you ship, because the check runs on the artifact itself.
- **`twine check` doesn't check links.** `twine check` validates that the
  description renders as valid RST/Markdown. It never follows a URL, so a
  relative image that 404s on every PyPI page passes `twine check` cleanly.
- **`--fix` rewrites at build time.** No other zero-dependency tool both detects
  PyPI-broken references in the artifact *and* emits fixed artifacts in the same
  pipeline step — without touching your source files.
- **Zero dependencies.** Stdlib only (`zipfile`, `tarfile`, `html.parser`,
  `urllib`) — safe to `pip install` in any CI job.

## How it works

1. Reads `long_description` from `.whl` (`zipfile` → `.dist-info/METADATA`) or `.tar.gz` (`tarfile` → `PKG-INFO`), plus the `Description-Content-Type` header.
2. For `text/html`, parses with `html.parser` and validates every `img[src]`, `a[href]`, and `#anchor` against collected element ids (explicit ids plus GitHub-style heading slugs).
3. For Markdown/RST (what artifacts actually carry — the raw source, not rendered HTML), scans with regexes: inline and reference-style images/links, embedded `<img>` tags, RST `image::`/`figure::` directives, and `#anchor` links against `# Heading` slugs.
4. `--fix` locates the same matches on code-masked text (positions preserved) and splices absolute URLs into the original, then rewrites the `METADATA`/`PKG-INFO` body inside a copy of the artifact.
5. `--pypi` mode fetches `https://pypi.org/pypi/<name>/json` and checks the hosted description.

## Limitations

- **Anchors can't be auto-fixed** — `--fix` reports them; fix the heading or the link by hand.
- Anchor validation for RST is limited (Markdown headings and HTML ids are covered; RST `.. _target:` definitions are not yet resolved).
- Code spans and fenced code blocks are ignored (documenting a bad pattern doesn't flag it); indented code blocks are still scanned.
- Heading-slug generation approximates GitHub/PyPI's algorithm; exotic headings could produce false positives — explicit `id` attributes always win.
- `--pypi` uses the raw `description` from the JSON API (the API doesn't expose rendered HTML); findings are identical to checking a fresh local build.

## License

MIT — see [LICENSE](https://github.com/hahahahahahahahah6/readmeta/blob/main/LICENSE).
