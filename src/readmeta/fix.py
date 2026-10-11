"""Build-time rewriting of broken README references (``--fix``).

v0.1 only *checked* the built artifact's ``long_description`` for references
that break on PyPI (relative images/links, raw SVGs, dead anchors). v0.2 adds
``--fix``: rewrite the fixable references to absolute URLs **at build time**
and emit fixed artifacts.

Why build-time, not source-time? Two constraints:

* The build artifact is derived output — editing ``dist/*.whl`` by hand is
  not a repeatable pipeline step.
* Baking absolute URLs into the *source* README is wrong for GitHub: a
  tag-pinned ``raw.githubusercontent.com`` URL 404s until the tag exists
  (chicken-and-egg), and a branch-pinned URL drifts. The source README should
  keep working relative paths for GitHub; the *build pipeline* rewrites them
  for PyPI.

So the flow is: ``python -m build`` → ``readmeta check dist/* --fix ...`` →
upload the ``*.fixed.*`` artifacts. Source files are never touched.

What ``--fix`` rewrites (relative-image / relative-link / relative-svg
findings): every relative URL becomes absolute, resolved against
``--base-url`` or derived from ``--repo OWNER/NAME [--ref REF]``:

* images (incl. ``.svg``) → ``https://raw.githubusercontent.com/<repo>/<ref>/<path>``
* other relative links → ``https://github.com/<repo>/blob/<ref>/<path>``
* with ``--base-url`` → ``urljoin(base_url, path)`` for everything

What it cannot rewrite: broken in-page anchors (``#anchor``) — there is no
sensible absolute form, so they are reported as remaining issues.
"""

from __future__ import annotations

import copy
import io
import re
import tarfile
import zipfile
from dataclasses import dataclass
from urllib.parse import urljoin

from .check import (
    _HTML_IMG_SRC,
    _MD_INLINE,
    _MD_REFDEF,
    _RST_IMAGE,
    _RST_LINK,
    _is_external,
    _line_of,
    _mask_spans,
    _pick_shallowest,
    _strip_code,
    check_description,
    read_artifact,
)

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# Image extensions that always resolve to the raw-content host in --repo mode.
_IMAGE_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".bmp", ".ico", ".avif", ".tif", ".tiff",
)


@dataclass
class Rewrite:
    kind: str      # relative-image | relative-link | relative-svg
    old: str       # original relative URL
    new: str       # rewritten absolute URL
    context: str = ""  # e.g. "dist/foo-1.0.whl:12"


def _join(base: str, url: str) -> str:
    """Join a relative README path onto a base URL.

    A leading ``/`` or ``./`` in a README means "repo root", not "URL host
    root", so strip it before joining; ``..`` segments are normalized by
    ``urljoin``.
    """
    u = url.strip()
    while u.startswith("./"):
        u = u[2:]
    u = u.lstrip("/")
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, u)


def _resolve(
    url: str,
    *,
    base_url: str | None,
    repo: str | None,
    ref: str,
    is_image: bool,
) -> str:
    u = url.strip()
    if base_url:
        return _join(base_url, u)
    assert repo is not None  # validated by the CLI
    stem = u.lower().split("?")[0].split("#")[0]
    if is_image or stem.endswith(_IMAGE_EXTS):
        root = f"https://raw.githubusercontent.com/{repo}/{ref}/"
    else:
        root = f"https://github.com/{repo}/blob/{ref}/"
    return _join(root, u)


def _fixable(url: str) -> bool:
    u = url.strip()
    return bool(u) and not u.startswith("#") and not _is_external(u)


def _classify(url: str, is_image: bool) -> str:
    stem = url.strip().lower().split("?")[0].split("#")[0]
    if stem.endswith(".svg"):
        return "relative-svg"
    return "relative-image" if is_image else "relative-link"


# ---------------------------------------------------------------------------
# Text mode (Markdown / RST raw source)
# ---------------------------------------------------------------------------

def fix_text(
    text: str,
    source: str = "<text>",
    *,
    base_url: str | None = None,
    repo: str | None = None,
    ref: str = "main",
) -> tuple[str, list[Rewrite]]:
    """Rewrite relative URLs in a Markdown/RST description.

    Matches are located on the code-masked text (so fenced code blocks and
    inline code spans are never rewritten) and applied at the same offsets in
    the original text — masking preserves positions.
    """
    masked = _strip_code(text)
    edits: list[tuple[int, int, str, str, str]] = []  # (start, end, old, new, kind)

    def consider(url: str, start: int, end: int, is_image: bool) -> None:
        if not _fixable(url):
            return
        new = _resolve(url, base_url=base_url, repo=repo, ref=ref, is_image=is_image)
        if new != url.strip():
            edits.append((start, end, url.strip(), new, _classify(url, is_image)))

    # Inline Markdown: ![alt](url), [text](url "title")
    for m in _MD_INLINE.finditer(masked):
        consider(m.group(2), m.start(2), m.end(2), bool(m.group(1)))

    # Reference-style definitions: [ref]: url  (usages [text][ref] resolve
    # through the definition, so fixing the definition fixes every usage)
    for m in _MD_REFDEF.finditer(masked):
        consider(m.group(2), m.start(2), m.end(2), False)

    # Raw <img> tags embedded in Markdown (quoted or unquoted src)
    for m in _HTML_IMG_SRC.finditer(masked):
        for g in (1, 2, 3):
            if m.group(g) is not None:
                consider(m.group(g), m.start(g), m.end(g), True)
                break

    # RST image/figure directives and hyperlink targets
    for m in _RST_IMAGE.finditer(masked):
        consider(m.group(1), m.start(1), m.end(1), True)
    for m in _RST_LINK.finditer(masked):
        consider(m.group(1), m.start(1), m.end(1), False)

    return _apply_edits(text, source, edits)


def _apply_edits(
    text: str, source: str, edits: list[tuple[int, int, str, str, str]]
) -> tuple[str, list[Rewrite]]:
    edits = sorted(edits, key=lambda e: e[0])
    parts: list[str] = []
    rewrites: list[Rewrite] = []
    pos = 0
    for start, end, old, new, kind in edits:
        if start < pos:
            continue  # overlapping match; keep the first
        parts.append(text[pos:start])
        parts.append(new)
        rewrites.append(
            Rewrite(kind, old, new, f"{source}:{_line_of(text, start)}")
        )
        pos = end
    parts.append(text[pos:])
    return "".join(parts), rewrites


# ---------------------------------------------------------------------------
# HTML mode (for text/html long descriptions)
# ---------------------------------------------------------------------------

_HTML_TAG = re.compile(r"<(?P<tag>img|a|source)\b(?P<attrs>[^>]*?)>", re.I)
_HTML_ATTR = re.compile(
    r"(?P<name>src|href|srcset)\s*=\s*"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\"'\s>]+)",
    re.I,
)


def _unquote(val: str) -> tuple[str, str, str]:
    """Split an attribute value into (quote, inner, quote)."""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[0], val[1:-1], val[-1]
    return "", val, ""


def _fix_srcset(
    value: str, *, base_url: str | None, repo: str | None, ref: str
) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite the URLs inside a srcset value, keeping descriptors (1x/2x)."""
    out: list[str] = []
    changed: list[tuple[str, str]] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        tokens = chunk.split()
        url = tokens[0]
        if _fixable(url):
            new = _resolve(url, base_url=base_url, repo=repo, ref=ref, is_image=True)
            changed.append((url.strip(), new))
            tokens[0] = new
        out.append(" ".join(tokens))
    return ", ".join(out), changed


def fix_html(
    html: str,
    source: str = "<html>",
    *,
    base_url: str | None = None,
    repo: str | None = None,
    ref: str = "main",
) -> tuple[str, list[Rewrite]]:
    """Rewrite relative URLs in an HTML description (<pre>/<code> masked)."""
    masked = _mask_spans(r"<pre\b.*?</pre>", html, re.S | re.I)
    masked = _mask_spans(r"<code\b.*?</code>", masked, re.S | re.I)
    edits: list[tuple[int, int, str, str, str]] = []

    for m in _HTML_TAG.finditer(masked):
        is_image = m.group("tag").lower() in ("img", "source")
        attrs = m.group("attrs")
        base = m.start("attrs")
        for am in _HTML_ATTR.finditer(attrs):
            name = am.group("name").lower()
            val = am.group("val")
            q1, inner, q2 = _unquote(val)
            vstart = base + am.start("val") + len(q1)
            vend = vstart + len(inner)
            if name == "srcset":
                new_inner, changed = _fix_srcset(
                    inner, base_url=base_url, repo=repo, ref=ref
                )
                if changed:
                    edits.append((vstart, vend, inner.strip(), new_inner, "relative-image"))
                # one edit per srcset attribute; break after handling
                break
            else:
                if not _fixable(inner):
                    continue
                new = _resolve(
                    inner, base_url=base_url, repo=repo, ref=ref,
                    is_image=is_image or name == "src",
                )
                if new != inner.strip():
                    edits.append(
                        (vstart, vend, inner.strip(), new,
                         _classify(inner, is_image or name == "src"))
                    )

    return _apply_edits(html, source, edits)


# ---------------------------------------------------------------------------
# Description-level entry point
# ---------------------------------------------------------------------------

def fix_description(
    content_type: str,
    description: str,
    source: str = "<description>",
    *,
    base_url: str | None = None,
    repo: str | None = None,
    ref: str = "main",
) -> tuple[str, list[Rewrite]]:
    """Rewrite fixable references; returns (new_description, rewrites)."""
    if "html" in content_type.lower():
        return fix_html(description, source, base_url=base_url, repo=repo, ref=ref)
    return fix_text(description, source, base_url=base_url, repo=repo, ref=ref)


# ---------------------------------------------------------------------------
# Artifact rewriting (build-time output: fixed artifacts, sources untouched)
# ---------------------------------------------------------------------------

def _replace_body(raw: str, new_desc: str) -> str:
    """Swap the long_description body of METADATA/PKG-INFO text, keeping headers."""
    m = re.search(r"\r?\n\r?\n", raw)
    if not m:
        return raw
    head = raw[: m.start()]
    body = new_desc if new_desc.endswith("\n") else new_desc + "\n"
    return head + "\n\n" + body


def _metadata_member(path: str, names: list[str]) -> str:
    if path.endswith(".whl"):
        return _pick_shallowest(
            [n for n in names if n.endswith(".dist-info/METADATA")],
            path, ".dist-info/METADATA",
        )
    return _pick_shallowest(
        [n for n in names if n.endswith("PKG-INFO")], path, "PKG-INFO"
    )


def _rewrite_zip_artifact(src: str, dst: str, new_desc: str) -> None:
    with zipfile.ZipFile(src) as zin:
        member = _metadata_member(src, zin.namelist())
        raw = zin.read(member).decode("utf-8", "replace")
        new_raw = _replace_body(raw, new_desc).encode("utf-8")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = new_raw if item.filename == member else zin.read(item.filename)
                zout.writestr(item, data)


def _rewrite_tar_artifact(src: str, dst: str, new_desc: str) -> None:
    with tarfile.open(src, "r:gz") as tin:
        members = tin.getmembers()
        blobs: dict[str, bytes | None] = {}
        for mi in members:
            f = tin.extractfile(mi)
            blobs[mi.name] = f.read() if f is not None else None
    member = _metadata_member(src, [mi.name for mi in members])
    raw = (blobs[member] or b"").decode("utf-8", "replace")
    new_raw = _replace_body(raw, new_desc).encode("utf-8")
    with tarfile.open(dst, "w:gz") as tout:
        for mi in members:
            ti = copy.copy(mi)
            data = new_raw if mi.name == member else blobs[mi.name]
            if data is None:  # pragma: no cover - exotic member types
                continue
            ti.size = len(data)
            tout.addfile(ti, io.BytesIO(data))


def rewrite_artifact(
    path: str,
    out_path: str,
    *,
    base_url: str | None = None,
    repo: str | None = None,
    ref: str = "main",
) -> tuple[str, str, str, str, list[Rewrite]]:
    """Rewrite an artifact's long_description and write the fixed artifact.

    Returns (name, version, content_type, new_description, rewrites).
    The input artifact is never modified.
    """
    name, version, ctype, desc = read_artifact(path)
    new_desc, rewrites = fix_description(
        ctype, desc, path, base_url=base_url, repo=repo, ref=ref
    )
    if path.endswith((".whl", ".zip")):
        _rewrite_zip_artifact(path, out_path, new_desc)
    elif path.endswith((".tar.gz", ".tgz")):
        _rewrite_tar_artifact(path, out_path, new_desc)
    else:  # pragma: no cover - read_artifact already rejects these
        raise ValueError(f"unsupported artifact: {path}")
    return name, version, ctype, new_desc, rewrites


def fixed_artifact_name(filename: str) -> str:
    """foo-1.0-py3-none-any.whl -> foo-1.0-py3-none-any.fixed.whl."""
    if filename.endswith(".tar.gz"):
        return filename[:-7] + ".fixed.tar.gz"
    if filename.endswith(".tgz"):
        return filename[:-4] + ".fixed.tgz"
    stem, _, ext = filename.rpartition(".")
    return f"{stem}.fixed.{ext}" if stem else filename
