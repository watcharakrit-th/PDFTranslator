"""Chunk markup helpers: safe HTML rendering and re-tagging edited text.

Markup is the extracted text, HTML-escaped, with three tags of our own:
<c>code</c>, <k>key term</k>, <b>bold</b>.
"""

from __future__ import annotations

import html
import re

TAG_RE = re.compile(r"(</?[ckb]>)")
TAG_HTML = {
    "<c>": "<code>",
    "</c>": "</code>",
    "<k>": '<strong class="key">',
    "</k>": "</strong>",
    "<b>": "<b>",
    "</b>": "</b>",
}


def markup_html(markup: str) -> str:
    """HTML for display. Everything except our six tags is (re-)escaped, so text
    from a PDF can never inject markup into the review page."""
    out = []
    for part in TAG_RE.split(markup or ""):
        if part in TAG_HTML:
            out.append(TAG_HTML[part])
        elif part:
            out.append(html.escape(html.unescape(part), quote=False))
    return "".join(out)


def runs(markup: str) -> list[tuple[str, tuple[str, ...]]]:
    """Split markup into (escaped text, open tags) runs."""
    open_tags: list[str] = []
    out: list[tuple[str, tuple[str, ...]]] = []
    for part in TAG_RE.split(markup or ""):
        if not part:
            continue
        m = re.fullmatch(r"<(/?)([ckb])>", part)
        if m:
            if m.group(1):
                if m.group(2) in open_tags:
                    open_tags.remove(m.group(2))
            else:
                open_tags.append(m.group(2))
        else:
            out.append((part, tuple(open_tags)))
    return out


def retag(old_markup: str, new_text: str) -> str:
    """Markup for edited plain text: escape it, then put the old code / key-term /
    bold tags back around phrases that still appear (in their original order)."""
    escaped = html.escape(new_text, quote=False)
    result, cursor = [], 0
    for text, tags in runs(old_markup):
        core = text.strip()
        if not tags or not core:
            continue
        i = escaped.find(core, cursor)
        if i < 0:
            continue
        opening = "".join(f"<{t}>" for t in tags)
        closing = "".join(f"</{t}>" for t in reversed(tags))
        result.append(escaped[cursor:i] + opening + core + closing)
        cursor = i + len(core)
    result.append(escaped[cursor:])
    return "".join(result)
