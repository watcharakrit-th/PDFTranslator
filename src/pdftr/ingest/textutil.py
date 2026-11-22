"""Text helpers: markup, line joining, de-hyphenation, sentences."""

from __future__ import annotations

import html
import re
import unicodedata

TAG_RE = re.compile(r"</?[ckb]>")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]*")
SOFT_HYPHEN = "\u00ad"
HYPHENS = ("-", "‐", "‑", SOFT_HYPHEN)

# Characters that should not get a space when a code span wraps onto the next line.
CODE_WRAP_CHARS = "/\\-=._:,"


def strip_markup(markup: str) -> str:
    """Plain text from markup (tags removed, HTML entities decoded)."""
    return html.unescape(TAG_RE.sub("", markup))


def clean_text(text: str) -> str:
    """Normalize what PDF extraction gives us (ligatures, odd spaces, soft hyphens)."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(SOFT_HYPHEN, "").replace("\u200b", "")
    text = text.replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def build_vocabulary(texts: list[str]) -> set[str]:
    """Lower-case words that appear somewhere in the document (for de-hyphenation)."""
    vocab: set[str] = set()
    for t in texts:
        vocab.update(w.lower() for w in WORD_RE.findall(t))
    return vocab


def join_lines(lines: list[tuple[str, bool, bool]], vocab: set[str]) -> str:
    """Join visual lines into one paragraph.

    ``lines`` holds (markup, starts_with_code, ends_with_code) per line.
    A line-end hyphen is removed when the joined word is a known word
    ("com-" + "puter" -> "computer"); kept when the hyphenated compound is
    known ("three-" + "digit" -> "three-digit").
    """
    out = ""
    prev_ends_code = False
    for markup, starts_code, ends_code in lines:
        piece = markup.strip()
        if not piece:
            continue
        if not out:
            out = piece
        elif prev_ends_code and starts_code and strip_markup(out).rstrip()[-1:] in CODE_WRAP_CHARS:
            out = re.sub(r"</c>$", "", out) + re.sub(r"^<c>", "", piece)
        elif strip_markup(out).endswith(HYPHENS) and piece[:1].islower():
            out = _join_hyphenated(out, piece, vocab)
        else:
            out = f"{out} {piece}"
        prev_ends_code = ends_code
    return out


def _join_hyphenated(left: str, right: str, vocab: set[str]) -> str:
    m_left = re.search(r"([A-Za-z]+)[-‐‑\u00ad]((?:</[ckb]>)*)$", left)
    m_right = re.match(r"((?:<[ckb]>)*)([A-Za-z]+)", right)
    if not m_left or not m_right:
        return f"{left}{right}"
    joined = (m_left.group(1) + m_right.group(2)).lower()
    hyphenated = f"{m_left.group(1)}-{m_right.group(2)}".lower()
    # Drop the hyphen if the joined word occurs elsewhere ("computer");
    # keep it if both halves are words ("three-digit"); otherwise drop it.
    if joined not in vocab and _known_compound(hyphenated, vocab):
        return f"{left}{right}"
    return left[: m_left.start(0) + len(m_left.group(1))] + m_left.group(2) + right


def _known_compound(hyphenated: str, vocab: set[str]) -> bool:
    a, b = hyphenated.split("-", 1)
    # both halves are words in their own right, e.g. "three-digit", "built-in"
    return a in vocab and b in vocab and len(a) > 2 and len(b) > 2


def join_blocks(left: str, right: str, vocab: set[str]) -> str:
    """Join two blocks of one paragraph that a page or column break split."""
    if strip_markup(left).endswith(HYPHENS) and right[:1].islower():
        return _join_hyphenated(left, right, vocab)
    return f"{left} {right}"


_ABBREV = {
    "e.g",
    "i.e",
    "etc",
    "vs",
    "fig",
    "figs",
    "no",
    "nos",
    "dr",
    "mr",
    "mrs",
    "ms",
    "inc",
    "ltd",
    "co",
    "corp",
    "approx",
    "ch",
    "sec",
    "vol",
    "pp",
    "p",
    "jr",
    "sr",
    "st",
    "u.s",
}
_SENT_END = re.compile(r"(?<=[.!?])[\"”’)]*\s+(?=[\"“(]?[A-Z0-9<])")


def split_sentences(text: str) -> list[str]:
    """Split on sentence ends, ignoring abbreviations and numbers like 6.13.8."""
    parts: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        candidate = text[start : m.start()].rstrip()
        last_word = re.search(r"([A-Za-z][A-Za-z.]*)\.[\"”’)]*$", candidate)
        if last_word and last_word.group(1).lower().rstrip(".") in _ABBREV:
            continue
        if re.search(r"\b[A-Z]\.$", candidate):  # initials like "J."
            continue
        parts.append(candidate)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def word_count(text: str) -> int:
    return len(strip_markup(text).split())


def ends_sentence(text: str) -> bool:
    t = strip_markup(text).rstrip()
    t = t.rstrip('"”’)]')
    return t.endswith((".", "!", "?", ":", ";"))


def starts_lowercase(text: str) -> bool:
    t = strip_markup(text).lstrip('"“‘(')
    return bool(t) and t[0].islower()
