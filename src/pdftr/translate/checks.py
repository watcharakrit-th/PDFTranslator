"""Automatic checks on each translation; findings become flags shown in R4."""

from __future__ import annotations

import re

from pdftr.translate.protect import PH_RE
from pdftr.translate.rules import Term

THAI_RE = re.compile("[" + chr(0x0E00) + "-" + chr(0x0E7F) + "]")
LATIN_RE = re.compile(r"[A-Za-z]")
PARENS_RE = re.compile(r"\([^()]*\)")
# whole Thai words only: "จ้า" is also inside "เจ้าของ" (owner)
INFORMAL = frozenset({"ครับ", "ค่ะ", "คะ", "นะคะ", "นะครับ", "จ้ะ", "จ้า", "ฮะ"})
CHATTY_RE = re.compile(
    r"^(?:here is|here's|translation|the translation|คำแปล|แปลว่า)", re.IGNORECASE
)

FLAG_HELP = {
    "placeholder_missing": "The translation dropped some code/URL; it was added back at the end",
    "placeholder_extra": "The translation repeated or invented a placeholder",
    "untranslated": "Most of the text is still English",
    "length": "Much shorter or longer than the English",
    "informal": "Contains polite particles (ครับ/ค่ะ) in a textbook",
    "chatty": "Starts with an explanation instead of the translation",
    "glossary": "A glossary term is not as decided in R3 (its Thai is missing, or a term "
    "to keep in English was translated)",
    "split_th": "Thai split across a page break: check where the second part starts",
}


def _thai_words(text: str) -> set[str]:
    from pythainlp.tokenize import word_tokenize

    return set(word_tokenize(text, engine="newmm", keep_whitespace=False))


def check(source: str, thai: str, terms: list[Term]) -> list[str]:
    """``source`` and ``thai`` are the protected plain texts (with placeholders);
    ``terms`` are all glossary terms found in the source."""
    flags: list[str] = []
    # text in parentheses is mostly names and codes that stay English: "(S/390, z/Architecture)"
    rest = PARENS_RE.sub(" ", PH_RE.sub(" ", thai))
    for t in terms:
        if t.decision == "keep_en":
            rest = t.pattern.sub(" ", rest)
    thai_chars = len(THAI_RE.findall(rest))
    latin_chars = len(LATIN_RE.findall(rest))
    src_letters = len(LATIN_RE.findall(PARENS_RE.sub(" ", PH_RE.sub(" ", source))))
    if src_letters >= 15 and thai_chars < 0.4 * (thai_chars + latin_chars):
        flags.append("untranslated")
    if len(source) >= 20:
        ratio = len(thai) / len(source)
        if ratio < 0.35 or ratio > 3.0:
            flags.append("length")
    if any(p in thai for p in INFORMAL) and INFORMAL & _thai_words(thai):
        flags.append("informal")
    if CHATTY_RE.match(thai.strip()):
        flags.append("chatty")
    if glossary_issues(thai, terms):
        flags.append("glossary")
    return flags


def glossary_issues(thai: str, terms: list[Term]) -> list[str]:
    """What the Thai does differently from the glossary, for R4 ("Linux: keep in English")."""
    issues = []
    for t in terms:
        if t.decision == "translate" and t.term_th and t.term_th not in thai:
            issues.append(f"{t.term_en} → {t.term_th}")
        elif t.decision == "keep_en" and not t.pattern.search(thai):
            issues.append(f"{t.term_en}: keep in English")
    return issues
