"""Core checks for readmeta.

Key insight: PyPI renders the *built artifact's* long_description (the body of
``METADATA`` in a wheel / ``PKG-INFO`` in an sdist), not the repository's
``README.md``. Relative images, relative links, in-page anchors and raw SVG
references that work on GitHub silently 404 on PyPI, and ``twine check``
does not catch them.
"""

from __future__ import annotations

import re
import tarfile
import zipfile
from dataclasses import dataclass
from email import message_from_string
from html.parser import HTMLParser


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
    return u.startswith(("http://", "https://", "//", "data:", "mailto:"))


def _github_slug(text: str) -> str:
    """Approximate GitHub/PyPI heading-id slugification."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        line = self.getpos()[0]
        ident = a.get("id")
        if ident:
            self.ids.add(ident)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_text = ""
        elif tag == "img":
            self.images.append((a.get("src") or "", line))
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
                self.ids.add(slug)
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

    out.sort(key=lambda f: f.context)
    return out


# ---------------------------------------------------------------------------
# Text mode (Markdown / RST raw source, as stored in METADATA / PKG-INFO)
# ---------------------------------------------------------------------------

_MD_INLINE = re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_REFDEF = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.M)
_MD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_HTML_IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"']", re.I)
_RST_IMAGE = re.compile(r"^\s*\.\.\s+(?:image|figure)::\s*(\S+)", re.M)


def _mask_spans(pattern: str, text: str, flags: int = 0) -> str:
    """Replace matches with spaces (newlines preserved) so line numbers survive."""

    def _mask(m: re.Match) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in m.group(0))

    return re.sub(pattern, _mask, text, flags=flags)


def _strip_code(text: str) -> str:
    """Mask fenced code blocks and inline code spans: documentation *about*
    bad patterns (like this docstring) must not be flagged."""
    text = _mask_spans(r"```.*?```", text, re.S)
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
    for m in _MD_REFDEF.finditer(text):
        add_url(m.group(1), m.start(), is_image=False)

    # Raw <img> tags embedded in Markdown
    for m in _HTML_IMG_SRC.finditer(text):
        add_url(m.group(1), m.start(), is_image=True)

    # RST image / figure directives
    for m in _RST_IMAGE.finditer(text):
        add_url(m.group(1), m.start(), is_image=True)

    # In-page anchors (Markdown): [text](#anchor) vs ## Headings
    known = {_github_slug(m.group(1)) for m in _MD_HEADING.finditer(text)}
    for m in _MD_INLINE.finditer(text):
        url = m.group(2).strip()
        if url.startswith("#") and len(url) > 1:
            target = url[1:]
            if target not in known:
                out.append(Finding("broken-anchor", url, ctx(m.start()), HINTS["broken-anchor"]))

    out.sort(key=lambda f: f.context)
    return out


# ---------------------------------------------------------------------------
# Artifact / PyPI input
# ---------------------------------------------------------------------------

def read_artifact(path: str) -> tuple[str, str, str, str]:
    """Return (name, version, content_type, long_description) for a wheel/sdist."""
    if path.endswith(".whl"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
            if not names:
                raise ValueError(f"no .dist-info/METADATA found in {path}")
            raw = z.read(names[0]).decode("utf-8", "replace")
    elif path.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as t:
            members = [m for m in t.getmembers() if m.name.endswith("PKG-INFO")]
            if not members:
                raise ValueError(f"no PKG-INFO found in {path}")
            f = t.extractfile(members[0])
            assert f is not None
            raw = f.read().decode("utf-8", "replace")
    else:
        raise ValueError(f"unsupported artifact (want .whl or .tar.gz): {path}")

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


def fetch_pypi(name: str, timeout: float = 20.0) -> tuple[str, str, str, str]:
    """Return (name, version, content_type, description) from the PyPI JSON API.

    Note: the public JSON API exposes the raw ``description``, not rendered
    HTML, so text-mode scanning applies unless a ``description_html`` field
    is ever present.
    """
    import json
    import urllib.request

    url = f"https://pypi.org/pypi/{name}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "readmeta/0.1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
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
    if "html" in content_type:
        return check_html(description, source)
    return check_text(description, source)
