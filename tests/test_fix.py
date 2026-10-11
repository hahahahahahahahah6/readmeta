"""Tests for readmeta --fix (v0.2): build-time rewriting of broken references."""

import io
import tarfile
import zipfile

import pytest

from readmeta.check import check_description, read_artifact
from readmeta.cli import main
from readmeta.fix import (
    fix_description,
    fixed_artifact_name,
    rewrite_artifact,
)

REPO = "octo/demo"
REF = "main"

BAD_MD = """# demo

![shot](docs/shot.png)

![logo](assets/logo.svg)

See [usage](docs/usage.md) and the [site](https://example.com/).

![abs](https://cdn.example.com/a.png)

[broken](#no-such-heading)

[refimg][shot]

[shot]: ./docs/shot2.png
"""

WHEEL_META = """Metadata-Version: 2.1
Name: fakepkg
Version: 0.1.0
Description-Content-Type: text/markdown

""" + BAD_MD

SDIST_PKGINFO = """Metadata-Version: 2.1
Name: fakepkg
Version: 0.1.0
Description-Content-Type: text/markdown

""" + BAD_MD


def _make_wheel(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fakepkg-0.1.0.dist-info/METADATA", WHEEL_META)
        z.writestr("fakepkg-0.1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        z.writestr("fakepkg/__init__.py", "__version__ = '0.1.0'\n")


def _make_sdist(path):
    with tarfile.open(path, "w:gz") as t:
        for arcname, data in (
            ("fakepkg-0.1.0/PKG-INFO", SDIST_PKGINFO.encode()),
            ("fakepkg-0.1.0/setup.py", b"# setup\n"),
        ):
            ti = tarfile.TarInfo(arcname)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))


# --- URL rewriting --------------------------------------------------------


def test_fix_relative_image_repo_mode():
    new, rewrites = fix_description("text/markdown", "![a](docs/a.png)", repo=REPO)
    assert new == "![a](https://raw.githubusercontent.com/octo/demo/main/docs/a.png)"
    assert len(rewrites) == 1
    assert rewrites[0].kind == "relative-image"
    assert rewrites[0].old == "docs/a.png"


def test_fix_relative_link_md_goes_to_blob():
    new, rewrites = fix_description("text/markdown", "[u](docs/usage.md)", repo=REPO)
    assert new == "[u](https://github.com/octo/demo/blob/main/docs/usage.md)"
    assert rewrites[0].kind == "relative-link"


def test_fix_svg_uses_raw_host():
    new, _ = fix_description("text/markdown", "![s](assets/logo.svg)", repo=REPO)
    assert new == "![s](https://raw.githubusercontent.com/octo/demo/main/assets/logo.svg)"


def test_fix_reference_style_definition_fixes_usages():
    new, rewrites = fix_description("text/markdown", "[t][r]\n\n[r]: docs/x.png", repo=REPO)
    assert "[r]: https://raw.githubusercontent.com/octo/demo/main/docs/x.png" in new
    # the usage [t][r] is untouched; it resolves through the fixed definition
    assert new.startswith("[t][r]")
    assert len(rewrites) == 1


def test_fix_skips_anchors_external_and_keeps_anchor_reported():
    src = "[a](#missing)\n[e](https://example.com/)\n![x](docs/x.png)"
    new, rewrites = fix_description("text/markdown", src, repo=REPO)
    assert "[a](#missing)" in new
    assert "[e](https://example.com/)" in new
    assert len(rewrites) == 1  # only the image was rewritten
    # the anchor is still a finding after fixing (it cannot be rewritten)
    remaining = check_description("text/markdown", new, "x")
    assert [f.kind for f in remaining] == ["broken-anchor"]


def test_fix_skips_code_blocks():
    src = "```\n![a](docs/a.png)\n```\n\n![b](docs/b.png)\n\n`[c](docs/c.png)`\n"
    new, rewrites = fix_description("text/markdown", src, repo=REPO)
    assert "```\n![a](docs/a.png)\n```" in new  # fenced block untouched
    assert "`[c](docs/c.png)`" in new  # inline code untouched
    assert "https://raw.githubusercontent.com/octo/demo/main/docs/b.png" in new
    assert len(rewrites) == 1


def test_fix_base_url_join_semantics():
    # no trailing slash + ./ prefix + query string preserved
    new, _ = fix_description(
        "text/markdown",
        "![a](./docs/a.png?raw=true)",
        base_url="https://cdn.example.com/static",
    )
    assert new == "![a](https://cdn.example.com/static/docs/a.png?raw=true)"


def test_fix_dotdot_and_root_relative_resolve_under_repo_root():
    new, _ = fix_description("text/markdown", "![a](../shared/a.png)", repo=REPO)
    assert new == "![a](https://raw.githubusercontent.com/octo/demo/shared/a.png)"
    new, _ = fix_description("text/markdown", "![b](/docs/b.png)", repo=REPO)
    assert new == "![b](https://raw.githubusercontent.com/octo/demo/main/docs/b.png)"


def test_fix_html_img_src_and_srcset():
    html = (
        '<img src="docs/a.png" srcset="docs/a.png 1x, docs/a@2x.png 2x" alt="a">\n'
        '<pre><img src="docs/nope.png"></pre>\n'
        '<a href="docs/page.md">p</a>'
    )
    new, rewrites = fix_description("text/html", html, repo=REPO)
    assert 'src="https://raw.githubusercontent.com/octo/demo/main/docs/a.png"' in new
    assert "https://raw.githubusercontent.com/octo/demo/main/docs/a@2x.png 2x" in new
    assert 'src="docs/nope.png"' in new  # inside <pre>: untouched
    assert 'href="https://github.com/octo/demo/blob/main/docs/page.md"' in new
    assert len(rewrites) == 3  # img src, srcset, a href


def test_fix_rst_directives():
    src = ".. image:: docs/a.png\n\n.. _docs: docs/page\n"
    new, rewrites = fix_description("text/x-rst", src, repo=REPO)
    assert ".. image:: https://raw.githubusercontent.com/octo/demo/main/docs/a.png" in new
    assert ".. _docs: https://github.com/octo/demo/blob/main/docs/page" in new
    assert len(rewrites) == 2


# --- artifact round-trips ---------------------------------------------------


def test_rewrite_wheel_roundtrip(tmp_path):
    src = tmp_path / "fakepkg-0.1.0-py3-none-any.whl"
    _make_wheel(src)
    before = src.read_bytes()
    dst = tmp_path / fixed_artifact_name(src.name)

    name, version, ctype, new_desc, rewrites = rewrite_artifact(
        str(src), str(dst), repo=REPO
    )

    # input artifact untouched
    assert src.read_bytes() == before
    # fixed artifact exists, parses, and carries the rewritten description
    assert dst.exists()
    rname, rver, rctype, rdesc = read_artifact(str(dst))
    assert (rname, rver, rctype) == ("fakepkg", "0.1.0", "text/markdown")
    assert rdesc == new_desc
    # non-metadata members survive
    with zipfile.ZipFile(dst) as z:
        assert z.read("fakepkg/__init__.py") == b"__version__ = '0.1.0'\n"
    # the fixed description is clean except the unrewritable anchor
    remaining = check_description(rctype, rdesc, str(dst))
    assert [f.kind for f in remaining] == ["broken-anchor"]
    assert name == "fakepkg" and version == "0.1.0"
    assert len(rewrites) >= 4  # image, svg, link, refdef


def test_rewrite_sdist_roundtrip(tmp_path):
    src = tmp_path / "fakepkg-0.1.0.tar.gz"
    _make_sdist(src)
    before = src.read_bytes()
    dst = tmp_path / fixed_artifact_name(src.name)

    _, _, _, new_desc, rewrites = rewrite_artifact(str(src), str(dst), repo=REPO)

    assert src.read_bytes() == before
    assert dst.exists()
    _, _, _, rdesc = read_artifact(str(dst))
    assert rdesc == new_desc
    assert "https://raw.githubusercontent.com/octo/demo/main/docs/shot.png" in rdesc
    with tarfile.open(dst, "r:gz") as t:
        assert t.extractfile("fakepkg-0.1.0/setup.py").read() == b"# setup\n"
    assert len(rewrites) >= 4


def test_fixed_artifact_name():
    assert fixed_artifact_name("foo-1.0-py3-none-any.whl") == "foo-1.0-py3-none-any.fixed.whl"
    assert fixed_artifact_name("foo-1.0.tar.gz") == "foo-1.0.fixed.tar.gz"
    assert fixed_artifact_name("foo-1.0.zip") == "foo-1.0.fixed.zip"


# --- CLI ---------------------------------------------------------------------


def test_cli_fix_end_to_end(tmp_path, capsys):
    src = tmp_path / "fakepkg-0.1.0-py3-none-any.whl"
    _make_wheel(src)
    rc = main(["check", str(src), "--fix", "--repo", REPO])
    out = capsys.readouterr().out
    # unfixable anchor remains -> exit 1
    assert rc == 1
    assert "--fix: rewrote" in out
    assert "could not be rewritten" in out
    fixed = tmp_path / "fakepkg-0.1.0-py3-none-any.fixed.whl"
    assert fixed.exists()


def test_cli_fix_requires_base_or_repo(capsys):
    with pytest.raises(SystemExit) as e:
        main(["check", "dist/x.whl", "--fix"])
    assert e.value.code == 2


def test_cli_fix_clean_artifact_exits_zero(tmp_path, capsys):
    src = tmp_path / "clean-1.0-py3-none-any.whl"
    meta = (
        "Metadata-Version: 2.1\nName: clean\nVersion: 1.0\n"
        "Description-Content-Type: text/markdown\n\n# clean\n\nAll good.\n"
    )
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("clean-1.0.dist-info/METADATA", meta)
    rc = main(["check", str(src), "--fix", "--repo", REPO])
    assert rc == 0
    assert "nothing to rewrite" in capsys.readouterr().out
