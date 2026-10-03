"""Tests for readmeta. Builds fake wheel/sdist fixtures in-test; no network."""

import io
import json
import tarfile
import zipfile

import pytest

from readmeta import cli
from readmeta.check import (
    check_description,
    check_html,
    check_text,
    fetch_pypi,
    read_artifact,
)


def _metadata(readme_body: str, content_type: str = "text/markdown") -> str:
    return (
        "Metadata-Version: 2.1\n"
        "Name: fakepkg\n"
        "Version: 0.1.0\n"
        f"Description-Content-Type: {content_type}\n"
        "\n"
        f"{readme_body}"
    )


def make_wheel(tmp_path, readme_body, content_type="text/markdown") -> str:
    whl = tmp_path / "fakepkg-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("fakepkg-0.1.0.dist-info/METADATA", _metadata(readme_body, content_type))
        z.writestr("fakepkg/__init__.py", "")
    return str(whl)


def make_sdist(tmp_path, readme_body, content_type="text/markdown") -> str:
    tarball = tmp_path / "fakepkg-0.1.0.tar.gz"
    data = _metadata(readme_body, content_type).encode()
    with tarfile.open(tarball, "w:gz") as t:
        ti = tarfile.TarInfo("fakepkg-0.1.0/PKG-INFO")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    return str(tarball)


# --- artifact reading -------------------------------------------------------

def test_read_wheel(tmp_path):
    p = make_wheel(tmp_path, "# Hi\n")
    name, version, ctype, desc = read_artifact(p)
    assert (name, version, ctype) == ("fakepkg", "0.1.0", "text/markdown")
    assert "# Hi" in desc


def test_read_sdist(tmp_path):
    p = make_sdist(tmp_path, "# Hi\n")
    name, version, ctype, desc = read_artifact(p)
    assert (name, version, ctype) == ("fakepkg", "0.1.0", "text/markdown")
    assert "# Hi" in desc


def test_read_artifact_bad_suffix(tmp_path):
    with pytest.raises(ValueError):
        read_artifact(str(tmp_path / "nope.rar"))


def make_zipsdist(tmp_path, readme_body, content_type="text/markdown") -> str:
    zipp = tmp_path / "fakepkg-0.1.0.zip"
    with zipfile.ZipFile(zipp, "w") as z:
        z.writestr("fakepkg-0.1.0/PKG-INFO", _metadata(readme_body, content_type))
    return str(zipp)


def test_read_zipsdist(tmp_path):
    p = make_zipsdist(tmp_path, "# Hi\n")
    name, version, ctype, desc = read_artifact(p)
    assert (name, version, ctype) == ("fakepkg", "0.1.0", "text/markdown")
    assert "# Hi" in desc


def test_read_artifact_missing_file():
    with pytest.raises(OSError):
        read_artifact("/tmp/does-not-exist-xyz.whl")


# --- markdown text mode -------------------------------------------------------

def test_relative_image_flagged():
    fs = check_text("# T\n\n![shot](docs/shot.png)\n")
    assert [f.kind for f in fs] == ["relative-image"]
    assert fs[0].target == "docs/shot.png"
    assert fs[0].context.endswith(":3")


def test_absolute_image_ok():
    fs = check_text("![shot](https://raw.githubusercontent.com/a/b/main/shot.png)\n")
    assert fs == []


def test_relative_link_flagged():
    fs = check_text("see [docs](docs/usage.md) for more\n")
    assert [f.kind for f in fs] == ["relative-link"]


def test_mailto_and_anchors_not_links():
    fs = check_text("[mail](mailto:a@b.com)\n")
    assert fs == []


def test_relative_svg_flagged_as_svg():
    fs = check_text("![logo](assets/logo.svg)\n")
    assert [f.kind for f in fs] == ["relative-svg"]


def test_svg_link_flagged():
    fs = check_text("[diagram](docs/arch.svg)\n")
    assert [f.kind for f in fs] == ["relative-svg"]


def test_broken_anchor_flagged():
    fs = check_text("## Install\n\nsee [setup](#instalation)\n")
    kinds = [f.kind for f in fs]
    assert kinds == ["broken-anchor"]
    assert fs[0].target == "#instalation"


def test_valid_anchor_ok():
    fs = check_text("## Install\n\nsee [setup](#install)\n")
    assert fs == []


def test_refstyle_definition_checked():
    fs = check_text("[logo]: docs/logo.png\n")
    assert [f.kind for f in fs] == ["relative-link"]


def test_embedded_html_img_in_markdown():
    fs = check_text('<img src="docs/x.png" alt="x">\n')
    assert [f.kind for f in fs] == ["relative-image"]


def test_rst_image_directive():
    fs = check_text("Title\n=====\n\n.. image:: docs/x.png\n", source="x")
    assert [f.kind for f in fs] == ["relative-image"]


def test_findings_sorted_by_line():
    fs = check_text("[b](b.md)\n\n![a](a.png)\n")
    lines = [int(f.context.rsplit(":", 1)[1]) for f in fs]
    assert lines == sorted(lines) == [1, 3]


def test_code_spans_ignored():
    # documenting a bad pattern must not flag it
    assert check_text("avoid `![x](docs/x.png)` here\n") == []
    assert check_text("```\n![x](docs/x.png)\n```\n") == []
    assert check_text("    ![x](docs/x.png)\n") != []  # indented, not fenced: still flagged


def test_html_pre_code_ignored():
    html = "<pre><img src=\"docs/x.png\"></pre><code>[x](docs/y.md)</code>"
    assert check_html(html) == []


# --- HTML mode ----------------------------------------------------------------

def test_html_relative_image_and_link():
    html = (
        "<h1 id='intro'>Intro</h1>"
        '<img src="docs/x.png">'
        '<a href="docs/y.md">y</a>'
        '<a href="#intro">ok</a>'
        '<a href="#nope">bad</a>'
    )
    kinds = sorted(f.kind for f in check_html(html))
    assert kinds == ["broken-anchor", "relative-image", "relative-link"]


def test_html_heading_slug_anchors():
    html = "<h2>Getting Started</h2><a href='#getting-started'>x</a>"
    assert check_html(html) == []


def test_html_external_ok():
    html = '<img src="https://example.com/x.png"><a href="https://example.com/">e</a>'
    assert check_html(html) == []


def test_html_svg():
    html = '<img src="assets/logo.svg">'
    fs = check_html(html)
    assert [f.kind for f in fs] == ["relative-svg"]


def test_check_description_dispatches_on_content_type():
    md = "![x](docs/x.png)\n"
    assert check_description("text/markdown", md, "s")[0].kind == "relative-image"
    assert check_description("text/x-rst", md, "s")[0].kind == "relative-image"
    html = '<img src="docs/x.png">'
    assert check_description("text/html", html, "s")[0].kind == "relative-image"


# --- --pypi mode (mocked) -------------------------------------------------------

class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def test_fetch_pypi(monkeypatch):
    payload = json.dumps({
        "info": {
            "name": "fakepkg",
            "version": "0.2.0",
            "description_content_type": "text/markdown",
            "description": "![x](docs/x.png)\n",
        }
    }).encode()

    def fake_urlopen(req, timeout=20.0):
        assert "pypi.org/pypi/fakepkg/json" in req.full_url
        return _FakeResp(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    name, version, ctype, desc = fetch_pypi("fakepkg")
    assert (name, version, ctype) == ("fakepkg", "0.2.0", "text/markdown")
    fs = check_description(ctype, desc, "pypi:fakepkg")
    assert [f.kind for f in fs] == ["relative-image"]


# --- CLI ------------------------------------------------------------------------

def test_cli_clean_exits_zero(tmp_path, capsys):
    p = make_wheel(tmp_path, "# Hi\n\n![x](https://example.com/x.png)\n")
    assert cli.main(["check", p]) == 0
    assert "OK" in capsys.readouterr().out


def test_cli_issues_exit_one(tmp_path, capsys):
    p = make_wheel(tmp_path, "![x](docs/x.png)\n")
    assert cli.main(["check", p]) == 1
    out = capsys.readouterr().out
    assert "relative-image" in out and "docs/x.png" in out


def test_cli_multiple_artifacts_worst_wins(tmp_path):
    good = make_wheel(tmp_path, "# Hi\n")
    bad = make_sdist(tmp_path, "[x](docs/x.md)\n")
    assert cli.main(["check", good, bad]) == 1


def test_cli_missing_file_exits_two(capsys):
    assert cli.main(["check", "/tmp/nope-not-here.whl"]) == 2


def test_cli_no_paths_exits_two():
    with pytest.raises(SystemExit) as e:
        cli.main(["check"])
    assert e.value.code == 2


def test_cli_pypi_mode(monkeypatch, capsys):
    payload = json.dumps({
        "info": {
            "name": "fakepkg", "version": "1.0",
            "description_content_type": "text/markdown",
            "description": "[x](docs/x.md)\n",
        }
    }).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=20.0: _FakeResp(payload)
    )
    assert cli.main(["check", "--pypi", "fakepkg"]) == 1
    assert "relative-link" in capsys.readouterr().out


# --- v0.1.1 review fixes ------------------------------------------------------

def test_findings_sorted_numerically():
    # lines 1, 3, 10: string sort would put 10 before 3
    text = "[a](a.md)\n\n[b](b.md)\n" + "\n" * 6 + "[c](c.md)\n"
    fs = check_text(text)
    lines = [int(f.context.rsplit(":", 1)[1]) for f in fs]
    assert lines == [1, 3, 10]


def test_footnote_definition_ignored():
    assert check_text("[^1]: some note\n\ntext with footnote[^1]\n") == []


def test_corrupt_wheel_clean_error(tmp_path):
    p = tmp_path / "bad-0.1.whl"
    p.write_bytes(b"this is not a zip file at all")
    with pytest.raises(ValueError, match="not a valid wheel"):
        read_artifact(str(p))


def test_corrupt_sdist_clean_error(tmp_path):
    p = tmp_path / "bad-0.1.tar.gz"
    p.write_bytes(b"\x00\x01\x02garbage")
    with pytest.raises(ValueError, match="not a valid sdist"):
        read_artifact(str(p))


def test_cli_corrupt_artifact_exits_two(tmp_path, capsys):
    p = tmp_path / "bad-0.1.whl"
    p.write_bytes(b"garbage")
    assert cli.main(["check", str(p)]) == 2
    assert "error" in capsys.readouterr().err


def test_read_artifact_multiple_metadata(tmp_path):
    whl = tmp_path / "dup-0.1-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("a-0.1.dist-info/METADATA", _metadata("# A\n"))
        z.writestr("b-0.1.dist-info/METADATA", _metadata("# B\n"))
    with pytest.raises(ValueError, match="multiple"):
        read_artifact(str(whl))


def test_sdist_prefers_toplevel_pkginfo_over_egginfo(tmp_path):
    # real sdists bundle src/*.egg-info/PKG-INFO; the top-level one wins
    tarball = tmp_path / "fakepkg-0.1.0.tar.gz"
    with tarfile.open(tarball, "w:gz") as t:
        for arcname, body in [
            ("fakepkg-0.1.0/PKG-INFO", _metadata("# Top\n")),
            ("fakepkg-0.1.0/src/fakepkg.egg-info/PKG-INFO", _metadata("# Egg\n")),
        ]:
            data = body.encode()
            ti = tarfile.TarInfo(arcname)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    name, version, ctype, desc = read_artifact(str(tarball))
    assert "# Top" in desc


def test_refstyle_anchor_broken():
    fs = check_text("[go][sec]\n\n[sec]: #missing\n")
    assert [f.kind for f in fs] == ["broken-anchor"]
    assert fs[0].target == "#missing"


def test_refstyle_anchor_valid():
    assert check_text("## Install\n\n[go][sec]\n\n[sec]: #install\n") == []


def test_refstyle_anchor_implicit_ref():
    # [text][] uses the link text as the ref name
    assert check_text("## Install\n\n[Install][]\n\n[install]: #install\n") == []


def test_shortcut_ref_anchor_broken():
    fs = check_text("[sec]\n\n[sec]: #nope\n")
    assert [f.kind for f in fs] == ["broken-anchor"]


def test_shortcut_ref_anchor_valid():
    assert check_text("## Install\n\n[sec]\n\n[sec]: #install\n") == []


def test_bare_refdef_anchor_not_flagged():
    # an unreferenced definition renders no link, so nothing is broken
    assert check_text("[sec]: #missing\n") == []


def test_rst_substitution_image():
    fs = check_text("Title\n=====\n\n.. |logo| image:: docs/logo.png\n")
    assert [f.kind for f in fs] == ["relative-image"]
    assert fs[0].target == "docs/logo.png"


def test_rst_hyperlink_target():
    fs = check_text("Title\n=====\n\n.. _docs: docs/page\n")
    assert [f.kind for f in fs] == ["relative-link"]
    assert fs[0].target == "docs/page"


def test_rst_hyperlink_target_absolute_ok():
    assert check_text(".. _docs: https://example.com/page\n") == []


def test_tilde_fence_ignored():
    assert check_text("~~~\n![x](docs/x.png)\n~~~\n") == []


def test_atx_heading_no_space():
    assert check_text("##Install\n\n[x](#install)\n") == []


def test_setext_headings():
    assert check_text("Install\n=======\n\n[x](#install)\n") == []
    assert check_text("Install\n-------\n\n[x](#install)\n") == []


def test_unquoted_img_src():
    fs = check_text("<img src=docs/x.png>\n")
    assert [f.kind for f in fs] == ["relative-image"]
    assert fs[0].target == "docs/x.png"


def test_html_srcset_checked():
    html = '<img srcset="docs/a.png 1x, https://example.com/b.png 2x">'
    fs = check_html(html)
    assert [f.target for f in fs] == ["docs/a.png"]


def test_html_source_tag_checked():
    html = '<picture><source src="docs/v.mp4"><img src="https://example.com/x.png"></picture>'
    fs = check_html(html)
    assert [f.kind for f in fs] == ["relative-image"]
    assert fs[0].target == "docs/v.mp4"


def test_duplicate_heading_anchors():
    text = "## Intro\n\n## Intro\n\n[a](#intro)\n[b](#intro-1)\n"
    assert check_text(text) == []


def test_duplicate_heading_anchors_html():
    html = "<h2>Intro</h2><h2>Intro</h2><a href='#intro-1'>x</a>"
    assert check_html(html) == []


def test_tel_and_ftp_schemes_external():
    assert check_text("[t](tel:+123456)\n") == []
    assert check_text("[t](ftp://example.com/f)\n") == []


def test_url_with_parens_kept_whole():
    fs = check_text("![t](docs/a_(b).png)\n")
    assert [f.kind for f in fs] == ["relative-image"]
    assert fs[0].target == "docs/a_(b).png"


def test_single_quote_title():
    fs = check_text("![t](docs/u.png 'the title')\n")
    assert [f.kind for f in fs] == ["relative-image"]
    assert fs[0].target == "docs/u.png"


def test_check_description_case_insensitive():
    html = '<img src="docs/x.png">'
    fs = check_description("TEXT/HTML", html, "s")
    assert [f.kind for f in fs] == ["relative-image"]


def test_cli_pypi_invalid_name(capsys):
    assert cli.main(["check", "--pypi", "../../etc"]) == 2
    assert "invalid PyPI project name" in capsys.readouterr().err


def test_cli_pypi_with_paths_errors():
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "some.whl", "--pypi", "fakepkg"])
    assert e.value.code == 2


def test_fetch_pypi_bad_json(monkeypatch):
    def fake_urlopen(req, timeout=20.0):
        return _FakeResp(b"<html>not json</html>")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ValueError, match="non-JSON"):
        fetch_pypi("fakepkg")


def test_fetch_pypi_invalid_name():
    with pytest.raises(ValueError, match="invalid PyPI project name"):
        fetch_pypi("../../x")


def test_python_m_module(monkeypatch, capsys):
    import runpy

    monkeypatch.setattr("sys.argv", ["readmeta", "--version"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("readmeta", run_name="__main__")
    assert e.value.code == 0
    assert "0.1.1" in capsys.readouterr().out
