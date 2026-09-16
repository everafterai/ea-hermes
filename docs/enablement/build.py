#!/usr/bin/env python3
"""Build the self-contained enablement deck and playbook.

Each ``*.template.html`` carries a ``/*FONTS*/`` marker inside its ``<style>``
block. This splices ``assets/fonts.css`` (base64 ``@font-face`` rules for
Poppins and Lato) in at that marker and writes the result to ``dist/``.

The fonts live outside the templates on purpose: the base64 payload is ~59 KB,
which would dominate every diff and every read of the files you actually edit.

Usage:
    python docs/enablement/build.py            # build both
    python docs/enablement/build.py deck       # build one
"""

from __future__ import annotations

import base64
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONTS = HERE / "assets" / "fonts.css"
DIST = HERE / "dist"
MARKER = "/*FONTS*/"

VOID_ELEMENTS = {"br", "hr", "img", "meta", "link", "input", "source", "wbr"}


class TagBalanceChecker(HTMLParser):
    """Catches the unclosed-tag class of bug that renders as silent layout drift."""

    def __init__(self) -> None:
        super().__init__()
        self.open_tags: list[tuple[str, tuple[int, int]]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag not in VOID_ELEMENTS:
            self.open_tags.append((tag, self.getpos()))

    def handle_endtag(self, tag: str) -> None:
        if not self.open_tags:
            self.errors.append(f"stray </{tag}> at line {self.getpos()[0]}")
            return
        expected, pos = self.open_tags[-1]
        if expected != tag:
            self.errors.append(
                f"</{tag}> at line {self.getpos()[0]} closes <{expected}> opened at line {pos[0]}"
            )
        else:
            self.open_tags.pop()


_IMG_TAG = re.compile(r'<img\s+(?:class="[^"]*"\s+)?(?:data-optional\s+)?src="assets/img/([^"]+)"([^>]*)>')
_OPT_WRAP = re.compile(r'<div class="hero-img" data-optional-wrap>\s*</div>')
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}


# Generated art arrives as ~2 MB PNGs; base64 inflates that by a third and the
# artifact page is capped at 16 MB. Cap the long edge and re-encode as WebP for
# the inlined copy only — the originals in assets/img/ are never touched.
_MAX_EDGE = 1400
_WEBP_QUALITY = 82


def _optimized(path: Path) -> tuple[str, bytes]:
    """Return (mime, bytes) for inlining — WebP-recompressed when Pillow is available."""
    if path.suffix.lower() in (".svg", ".gif"):
        return _MIME[path.suffix.lower()], path.read_bytes()
    try:
        from PIL import Image
        import io
        im = Image.open(path)
        im.thumbnail((_MAX_EDGE, _MAX_EDGE))
        buf = io.BytesIO()
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "WEBP", quality=_WEBP_QUALITY, method=6)
        return "image/webp", buf.getvalue()
    except Exception as exc:  # Pillow missing or an odd file: inline as-is
        print(f"  ~ {path.name}: inlined unoptimized ({exc})", file=sys.stderr)
        return _MIME.get(path.suffix.lower(), "application/octet-stream"), path.read_bytes()


def inline_images(html: str) -> str:
    """Embed ``assets/img/*`` as data URIs; render a dashed placeholder when a file is missing.

    Slides reference images as ``<img src="assets/img/name.png">``. The dist file
    must stay self-contained, so present files are base64-inlined; absent ones
    become a labelled placeholder so the layout survives until the image lands.
    """
    def repl(m: re.Match) -> str:
        whole, name, attrs = m.group(0), m.group(1), m.group(2)
        optional = "data-optional" in whole
        klass = re.search(r'class="([^"]*)"', whole)
        cls_attr = f' class="{klass.group(1)}"' if klass else ""
        path = HERE / "assets" / "img" / name
        if path.exists():
            mime, payload = _optimized(path)
            data = base64.b64encode(payload).decode("ascii")
            return f'<img{cls_attr} src="data:{mime};base64,{data}"{attrs}>'
        if optional:
            # Decorative: vanish without a trace so the slide reads as designed.
            print(f"  ~ optional image absent, omitted: assets/img/{name}", file=sys.stderr)
            return ""
        alt = re.search(r'alt="([^"]*)"', attrs)
        label = alt.group(1) if alt else name
        print(f"  ~ placeholder: assets/img/{name} not found", file=sys.stderr)
        return f'<div class="imgph">{name}<br><span style="text-transform:none;letter-spacing:0">{label}</span></div>'
    html = _IMG_TAG.sub(repl, html)
    # An optional hero whose image vanished leaves an empty frame; drop the frame too.
    return _OPT_WRAP.sub("", html)


def build(name: str, fonts_css: str) -> Path:
    template = HERE / f"{name}.template.html"
    if not template.exists():
        raise SystemExit(f"no such template: {template}")

    source = template.read_text(encoding="utf-8")
    if MARKER not in source:
        raise SystemExit(f"{template.name} is missing the {MARKER} marker")

    output = source.replace(MARKER, fonts_css)
    output = inline_images(output)
    if name == "deck":
        # Speaker notes live in the template only; the projected deck must not carry them.
        output = re.sub(r'\s+data-notes="[^"]*"', "", output)

    checker = TagBalanceChecker()
    checker.feed(output)
    problems = checker.errors + [f"unclosed <{t}> at line {p[0]}" for t, p in checker.open_tags]
    if problems:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
        raise SystemExit(f"{template.name}: {len(problems)} markup problem(s), not written")

    DIST.mkdir(exist_ok=True)
    target = DIST / f"{name}.html"
    target.write_text(output, encoding="utf-8")
    print(f"  {target.relative_to(HERE)}  ({len(output) // 1024} KB)")
    return target


_SLIDE_RE = re.compile(r'<section class="slide" data-title="([^"]*)" data-notes="([^"]*)"', re.S)


def build_notes(fonts_css: str) -> Path:
    """A printable / phone-friendly speaker-notes document, one entry per slide."""
    import html as _html
    src = (HERE / "deck.template.html").read_text(encoding="utf-8")
    items = [(_html.unescape(a), _html.unescape(b)) for a, b in _SLIDE_RE.findall(src)]
    entries = "\n".join(
        f'<article><div class="n">{k}</div><div><h2>{_html.escape(t)}</h2><p>{_html.escape(n)}</p></div></article>'
        for k, (t, n) in enumerate(items, 1)
    )
    page = f"""<meta charset="utf-8">
<title>Off Your Plate — Speaker notes</title>
<style>
{fonts_css}
:root{{--ink:#17131F;--muted:#6B6480;--line:#E8E3F2;--purple:#8F4AFB;--ground:#FFFFFF}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--ink:#F2EEFA;--muted:#948CAB;--line:#2E2740;--purple:#AC82FF;--ground:#131019}}}}
:root[data-theme="dark"]{{--ink:#F2EEFA;--muted:#948CAB;--line:#2E2740;--purple:#AC82FF;--ground:#131019}}
body{{margin:0;background:var(--ground);color:var(--ink);font-family:'Lato',-apple-system,sans-serif;font-size:17px;line-height:1.55}}
.wrap{{max-width:760px;margin:0 auto;padding:32px 22px 80px}}
h1{{font-family:'Poppins',sans-serif;font-weight:600;font-size:26px;margin:0 0 6px}}
.sub{{color:var(--muted);margin:0 0 28px}}
article{{display:grid;grid-template-columns:44px 1fr;gap:14px;padding:18px 0;border-top:1px solid var(--line);break-inside:avoid}}
.n{{font-family:'Poppins',sans-serif;font-weight:600;color:var(--purple);font-size:15px;padding-top:2px;font-variant-numeric:tabular-nums}}
h2{{font-family:'Poppins',sans-serif;font-weight:600;font-size:17px;margin:0 0 6px}}
p{{margin:0;color:var(--muted)}}
@media print{{body{{font-size:12.5px}} .wrap{{max-width:none;padding:0}} article{{padding:10px 0}}}}
</style>
<div class="wrap"><h1>Off Your Plate — speaker notes</h1><p class="sub">{len(items)} slides · press P in the deck for presenter mode; this is the paper fallback.</p>
{entries}
</div>
"""
    DIST.mkdir(exist_ok=True)
    target = DIST / "notes.html"
    target.write_text(page, encoding="utf-8")
    print(f"  {target.relative_to(HERE)}  ({len(page) // 1024} KB, {len(items)} slides)")
    return target


def main() -> None:
    if not FONTS.exists():
        raise SystemExit(f"missing {FONTS} — see README for how to regenerate it")
    fonts_css = FONTS.read_text(encoding="utf-8")

    names = sys.argv[1:] or ["deck", "playbook", "notes"]
    print("building:")
    for name in names:
        name = name.removesuffix(".html")
        if name == "notes":
            build_notes(fonts_css)
        else:
            build(name, fonts_css)


if __name__ == "__main__":
    main()
