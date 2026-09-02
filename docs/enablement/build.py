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


def build(name: str, fonts_css: str) -> Path:
    template = HERE / f"{name}.template.html"
    if not template.exists():
        raise SystemExit(f"no such template: {template}")

    source = template.read_text(encoding="utf-8")
    if MARKER not in source:
        raise SystemExit(f"{template.name} is missing the {MARKER} marker")

    output = source.replace(MARKER, fonts_css)

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


def main() -> None:
    if not FONTS.exists():
        raise SystemExit(f"missing {FONTS} — see README for how to regenerate it")
    fonts_css = FONTS.read_text(encoding="utf-8")

    names = sys.argv[1:] or ["deck", "playbook"]
    print("building:")
    for name in names:
        build(name.removesuffix(".html"), fonts_css)


if __name__ == "__main__":
    main()
