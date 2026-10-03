"""Core checks for readmeta.

Key insight: PyPI renders the *built artifact's* long_description (the body of
``METADATA`` in a wheel / ``PKG-INFO`` in an sdist), not the repository's
``README.md``. Relative images, relative links, in-page anchors and raw SVG
references that work on GitHub silently 404 on PyPI, and ``twine check``
does not catch them.
"""

from __future__ import annotations

import json
import re
import tarfile
import zipfile
from dataclasses import dataclass
from email import message_from_string
from html.parser import HTMLParser

from . import __version__


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    kind: str      # relative-image | relative-link | broken-anchor | relative-svg
    target: str    # the offending URL / anchor
    context: str = ""  # e.g. "dist/foo-1.0.whl:12"
    hint: str = ""


HINTS = {
    "relative-image": (
        "PyPI renders the description standalone; relative image paths 404. "
        "Use an absolute https:// URL (e.g. raw.githubusercontent.com)."
    ),
    "relative-link": (
        "Relative links resolve against pypi.org and break. "
        "Use an absolute https:// URL."
    ),
    "broken-anchor": (
        "No heading with a matching id was found; the link goes nowhere on PyPI."
    ),
    "relative-svg": (
        "Relative .svg references 404 on PyPI (PyPI does not serve repo files); "
        "twine check does not flag these. Use an absolute https:// URL."
    ),
}


def _is_external(url: str) -> bool:
    u = url.strip().lower()
    return u.startswith(
        ("http://", "https://", "//", "data:", "mailto:", "tel:", "ftp:")
    )


def _github_slug(text: str) -> str:
    """Approximate GitHub/PyPI heading-id slugification."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


def _sort_key(f: Finding) -> tuple[str, int]:
    """Sort by (source, numeric line): plain string sort puts 'src:10' before 'src:9'."""
    m = re.match(r"^(.*):(\d+)$", f.context)
    if m:
        return (m.group(1), int(m.group(2)))
    return (f.context, 0)


def _srcset_urls(srcset: str) -> list[str]:
    """Split a srcset attribute into its candidate URLs."""
    urls = []
    for part in srcset.split(","):
        part = part.strip()
        if part:
            urls.append(part.split()[0])
    return urls


# ---------------------------------------------------------------------------
# HTML mode (for text/html long descriptions)
# ---------------------------------------------------------------------------

class _ReadmeHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.anchors: list[tuple[str, int]] = []   # (target, line)
        self.images: list[tuple[str, int]] = []    # (src, line)
        self.links: list[tuple[str, int]] = []     # (href, line)
        self._heading_text: str | None = None
        self._counts: dict[str, int] = {}

    def _add_id(self, ident: str) -> None:
        # GitHub/PyPI dedupe repeated heading ids as name, name-1, name-2...
        n = self._counts.get(ident, 0)
        self.ids.add(ident if n == 0 else f"{ident}-{n}")
        self._counts[ident] = n + 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        line = self.getpos()[0]
        ident = a.get("id")
        if ident:
            self._add_id(ident)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_text = ""
        elif tag in ("img", "source"):
            self.images.append((a.get("src") or "", line))
            self.images.extend((u, line) for u in _srcset_urls(a.get("srcset") or ""))
        elif tag == "a":
            href = a.get("href") or ""
            self.links.append((href, line))
            if href.startswith("#") and len(href) > 1:
                self.anchors.append((href[1:], line))

    def handle_data(self, data: str) -> None:
        if self._heading_text is not None:
            self._heading_text += data

    def handle_endtag(self, tag: str) -> None:
        if self._heading_text is not None and tag in (
            "h1", "h2", "h3", "h4", "h5", "h6",
        ):
            slug = _github_slug(self._heading_text)
            if slug:
                self._add_id(slug)
            self._heading_text = None


def check_html(html: str, source: str = "<html>") -> list[Finding]:
    """Check an HTML long description. Returns findings sorted by line."""
    # Mask <pre>/<code> blocks: documentation *about* bad patterns must not flag.
    html = _mask_spans(r"<pre\b.*?</pre>", html, re.S | re.I)
    html = _mask_spans(r"<code\b.*?</code>", html, re.S | re.I)
    parser = _ReadmeHTMLParser()
    parser.feed(html)
    out: list[Finding] = []

    def ctx(line: int) -> str:
        return f"{source}:{line}"

    for src, line in parser.images:
        s = src.strip()
        if not s or _is_external(s):
            continue
        kind = "relative-svg" if s.lower().split("?")[0].split("#")[0].endswith(".svg") else "relative-image"
        out.append(Finding(kind, s, ctx(line), HINTS[kind]))

    for href, line in parser.links:
        h = href.strip()
        if not h or h.startswith("#") or _is_external(h):
            continue
        kind = "relative-svg" if h.lower().split("?")[0].split("#")[0].endswith(".svg") else "relative-link"
        out.append(Finding(kind, h, ctx(line), HINTS[kind]))

    for target, line in parser.anchors:
        if target not in parser.ids:
            out.append(Finding("broken-anchor", "#" + target, ctx(line), HINTS["broken-anchor"]))

    out.sort(key=_sort_key)
    return out


# ---------------------------------------------------------------------------
# Text mode (Markdown / RST raw source, as stored in METADATA / PKG-INFO)
# ---------------------------------------------------------------------------

# Inline Markdown: ![alt](url), [text](url "title"), [text](url 'title').
# The URL group tolerates balanced parens, e.g. docs/a_(b).png.
_MD_INLINE = re.compile(
    r"(!?)\[[^\]]*\]\(\s*((?:[^()\s]|\([^()]*\))+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)"
)
# Reference-style definitions: [ref]: url   (footnote defs [^1]: are excluded)
_MD_REFDEF = re.compile(r"^\s*\[(?!\^)([^\]]+)\]:\s*(\S+)", re.M)
# Reference-style usages: [text][ref] and [text][]
_MD_REFUSE = re.compile(r"(!?)\[([^\]]+)\]\[([^\]]*)\]")
# Shortcut references: [ref]  (scanned on masked text; [^1] footnotes excluded)
_MD_SHORTCUT = re.compile(r"(?<!!)\[(?!\^)([^\]]+)\]")
# ATX headings, with or without the space: ## Install / ##Install
_MD_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*#*\s*$", re.M)
# Setext headings: Title\n=====  and  Title\n-----
_MD_SETEXT = re.compile(r"^([^\n]+)\n(?:=+|-+)\s*$", re.M)
# <img> with quoted or unquoted src
_HTML_IMG_SRC = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\"'\s>]+))", re.I
)
# RST image/figure directives, incl. substitution form: .. |logo| image:: docs/x.png
_RST_IMAGE = re.compile(r"^\s*\.\.\s+(?:\|[^|]+\|\s+)?(?:image|figure)::\s*(\S+)", re.M)
# RST hyperlink targets: .. _name: docs/page
_RST_LINK = re.compile(r"^\s*\.\.\s+_[^:\s][^:]*:\s*(\S+)", re.M)


def _mask_spans(pattern: str, text: str, flags: int = 0) -> str:
    """Replace matches with spaces (newlines preserved) so line numbers survive."""

    def _mask(m: re.Match) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in m.group(0))

    return re.sub(pattern, _mask, text, flags=flags)


def _strip_code(text: str) -> str:
    """Mask fenced code blocks (``` or ~~~) and inline code spans: documentation
    *about* bad patterns (like this docstring) must not be flagged."""
    text = _mask_spans(r"(```|~~~).*?\1", text, re.S)
    return _mask_spans(r"`[^`\n]+`", text)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check_text(text: str, source: str = "<text>") -> list[Finding]:
    """Regex-based check for raw Markdown / RST long descriptions."""
    text = _strip_code(text)
    out: list[Finding] = []

    def ctx(pos: int) -> str:
        return f"{source}:{_line_of(text, pos)}"

    def add_url(url: str, pos: int, is_image: bool) -> None:
        u = url.strip()
        if not u or _is_external(u):
            return
        if u.startswith("#"):
            return  # handled by the anchor pass below
        stem = u.lower().split("?")[0].split("#")[0]
        kind = "relative-svg" if stem.endswith(".svg") else ("relative-image" if is_image else "relative-link")
        out.append(Finding(kind, u, ctx(pos), HINTS[kind]))

    # Inline Markdown images / links: ![alt](url), [text](url)
    for m in _MD_INLINE.finditer(text):
        add_url(m.group(2), m.start(), is_image=bool(m.group(1)))

    # Reference-style definitions: [ref]: url
    refdefs: dict[str, str] = {}
    anchor_uses: list[tuple[str, int]] = []  # (target, pos)
    for m in _MD_REFDEF.finditer(text):
        name = m.group(1).strip().lower()
        url = m.group(2).strip()
        refdefs.setdefault(name, url)
        # NOTE: a definition whose target is "#anchor" is only validated when
        # actually referenced ([text][ref] / [ref]); an unreferenced definition
        # renders no link, so there is nothing broken to report.
        if not url.startswith("#"):
            add_url(url, m.start(), is_image=False)

    # Raw <img> tags embedded in Markdown (quoted or unquoted src)
    for m in _HTML_IMG_SRC.finditer(text):
        src = m.group(1) or m.group(2) or m.group(3) or ""
        add_url(src, m.start(), is_image=True)

    # RST image / figure directives (incl. substitution form)
    for m in _RST_IMAGE.finditer(text):
        add_url(m.group(1), m.start(), is_image=True)

    # RST hyperlink targets: .. _name: url
    for m in _RST_LINK.finditer(text):
        add_url(m.group(1), m.start(), is_image=False)

    # --- anchor targets --------------------------------------------------
    counts: dict[str, int] = {}
    known: set[str] = set()

    def _register(raw: str) -> None:
        # Repeated headings get name, name-1, name-2... like GitHub/PyPI.
        slug = _github_slug(raw)
        if not slug:
            return
        n = counts.get(slug, 0)
        known.add(slug if n == 0 else f"{slug}-{n}")
        counts[slug] = n + 1

    for m in _MD_HEADING.finditer(text):
        _register(m.group(1))
    for m in _MD_SETEXT.finditer(text):
        _register(m.group(1))

    # Inline anchors: [text](#anchor)
    for m in _MD_INLINE.finditer(text):
        url = m.group(2).strip()
        if url.startswith("#") and len(url) > 1:
            anchor_uses.append((url[1:], m.start()))

    # Full reference anchors: [text][ref] / [text][]
    for m in _MD_REFUSE.finditer(text):
        ref = (m.group(3) or m.group(2)).strip().lower()
        url = refdefs.get(ref, "").strip()
        if url.startswith("#") and len(url) > 1:
            anchor_uses.append((url[1:], m.start()))

    # Shortcut reference anchors: [ref]
    # Scan masked text so inline/full-ref/definition constructs aren't re-matched.
    masked = _mask_spans(_MD_INLINE.pattern, text)
    masked = _mask_spans(_MD_REFUSE.pattern, masked)
    masked = _mask_spans(_MD_REFDEF.pattern, masked, re.M)
    for m in _MD_SHORTCUT.finditer(masked):
        url = refdefs.get(m.group(1).strip().lower(), "").strip()
        if url.startswith("#") and len(url) > 1:
            anchor_uses.append((url[1:], m.start()))

    for target, pos in anchor_uses:
        if target not in known:
            out.append(Finding("broken-anchor", "#" + target, ctx(pos), HINTS["broken-anchor"]))

    out.sort(key=_sort_key)
    return out


# ---------------------------------------------------------------------------
# Artifact / PyPI input
# ---------------------------------------------------------------------------

def _pick_shallowest(candidates: list[str], path: str, what: str) -> str:
    """Pick the top-level metadata file.

    Real sdists often bundle a second copy under ``*.egg-info/``; the one PyPI
    reads is the shallowest (``name-version/PKG-INFO``). Only error when the
    choice is genuinely ambiguous.
    """
    if not candidates:
        raise ValueError(f"no {what} found in {path}")
    by_depth: dict[int, list[str]] = {}
    for c in candidates:
        by_depth.setdefault(c.count("/"), []).append(c)
    shallowest = sorted(by_depth[min(by_depth)])
    if len(shallowest) > 1:
        raise ValueError(f"multiple {what} files in {path}: {shallowest}")
    return shallowest[0]


def _read_wheel_metadata(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            name = _pick_shallowest(
                [n for n in z.namelist() if n.endswith(".dist-info/METADATA")],
                path, ".dist-info/METADATA",
            )
            return z.read(name).decode("utf-8", "replace")
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a valid wheel (corrupt zip): {path}") from e


def _read_tar_pkginfo(path: str) -> str:
    try:
        with tarfile.open(path, "r:gz") as t:
            member = _pick_shallowest(
                [m.name for m in t.getmembers() if m.name.endswith("PKG-INFO")],
                path, "PKG-INFO",
            )
            f = t.extractfile(member)
            if f is None:
                raise ValueError(f"could not read PKG-INFO in {path}")
            return f.read().decode("utf-8", "replace")
    except tarfile.TarError as e:
        raise ValueError(f"not a valid sdist (corrupt tar): {path}") from e


def _read_zip_pkginfo(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            name = _pick_shallowest(
                [n for n in z.namelist() if n.endswith("PKG-INFO")],
                path, "PKG-INFO",
            )
            return z.read(name).decode("utf-8", "replace")
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a valid zip sdist (corrupt zip): {path}") from e


def read_artifact(path: str) -> tuple[str, str, str, str]:
    """Return (name, version, content_type, long_description) for a wheel/sdist."""
    if path.endswith(".whl"):
        raw = _read_wheel_metadata(path)
    elif path.endswith((".tar.gz", ".tgz")):
        raw = _read_tar_pkginfo(path)
    elif path.endswith(".zip"):
        raw = _read_zip_pkginfo(path)
    else:
        raise ValueError(f"unsupported artifact (want .whl, .tar.gz or .zip): {path}")

    msg = message_from_string(raw)
    ctype = (msg.get("Description-Content-Type") or "text/plain").split(";")[0].strip().lower()
    desc = msg.get_payload()
    if not isinstance(desc, str):
        desc = ""
    return (
        msg.get("Name") or "?",
        msg.get("Version") or "?",
        ctype,
        desc,
    )


_PYPI_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def fetch_pypi(name: str, timeout: float = 20.0) -> tuple[str, str, str, str]:
    """Return (name, version, content_type, description) from the PyPI JSON API.

    Note: the public JSON API exposes the raw ``description``, not rendered
    HTML, so text-mode scanning applies unless a ``description_html`` field
    is ever present.
    """
    import urllib.request

    if not _PYPI_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid PyPI project name: {name!r}")
    url = f"https://pypi.org/pypi/{name}/json"
    req = urllib.request.Request(url, headers={"User-Agent": f"readmeta/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        try:
            data = json.load(resp)
        except json.JSONDecodeError as e:
            raise ValueError(f"PyPI returned non-JSON for {name!r}") from e
    info = data.get("info", {})
    if "description_html" in info and info["description_html"]:
        return (
            info.get("name") or name,
            info.get("version") or "?",
            "text/html",
            info["description_html"],
        )
    ctype = (info.get("description_content_type") or "text/plain").split(";")[0].strip().lower()
    return (
        info.get("name") or name,
        info.get("version") or "?",
        ctype,
        info.get("description") or "",
    )


def check_description(content_type: str, description: str, source: str) -> list[Finding]:
    if "html" in content_type.lower():
        return check_html(description, source)
    return check_text(description, source)
