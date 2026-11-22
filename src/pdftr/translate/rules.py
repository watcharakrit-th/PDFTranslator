"""Glossary rules: each request gets only the rules for terms that appear in its text."""

from __future__ import annotations

import re
from dataclasses import dataclass

import psycopg

from pdftr.translate.protect import placeholder

STYLE_RULE = (
    "Use formal written Thai suitable for a textbook; do not add polite particles or explanations. "
    "Keep acronyms and the names of products, companies and programs (such as UNIX, Linux "
    "and Windows) in English."
)


@dataclass
class Term:
    term_en: str
    norm: str
    decision: str  # keep_en | translate
    term_th: str | None
    kind: str
    pattern: re.Pattern


# Latin-letter boundaries: Thai letters count as \w, and Thai text often has no space
# before an English word ("ระบบLinux"), so \b would miss terms in translations.
_START, _END = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"


def term_pattern(norm: str) -> re.Pattern:
    """Matches the term with common plural endings and hyphen/space variants."""
    if norm.isupper():
        return re.compile(rf"{_START}{re.escape(norm)}s?{_END}")
    words = [re.escape(w) for w in norm.split(" ")]
    if norm.endswith("y"):
        words[-1] = words[-1][:-1] + r"(?:y|ies)"
    else:
        words[-1] += r"(?:s|es)?"
    return re.compile(_START + r"[\s-]+".join(words) + _END, re.IGNORECASE)


# Names the model likes to write in Thai letters even when told to keep them English.
KNOWN_TRANSLITERATIONS: dict[str, tuple[str, ...]] = {
    "linux": ("ลินุกซ์", "ลีนุกซ์", "ลินุกส์", "ลีนุกส์", "ไลนุกซ์"),
    "unix": ("ยูนิกซ์", "ยูนิกส์"),
    "windows": ("วินโดวส์", "วินโดว์ส"),
    "cpu": ("ซีพียู",),
    "gpu": ("จีพียู",),
    "intel": ("อินเทล",),
    "ubuntu": ("อูบุนตู",),
    "android": ("แอนดรอยด์",),
    "macos": ("แมคโอเอส",),
}
_NO_SPACE_BEFORE = set(" ([{\"'“‘")
_NO_SPACE_AFTER = set(" )]}\"'”’,.:;!?")


def put_back_kept_names(thai: str, terms: list[Term]) -> str:
    """Keep-English terms of the source that the model wrote in Thai letters
    ("ลินุกซ์") become English again ("Linux"), with spaces around them."""
    for t in terms:
        forms = KNOWN_TRANSLITERATIONS.get(t.term_en.lower())
        if t.decision != "keep_en" or not forms or t.pattern.search(thai):
            continue
        for form in forms:

            def spaced(m: re.Match, text: str = thai, word: str = t.term_en) -> str:
                before = text[m.start() - 1] if m.start() > 0 else " "
                after = text[m.end()] if m.end() < len(text) else " "
                return (
                    ("" if before in _NO_SPACE_BEFORE else " ")
                    + word
                    + ("" if after in _NO_SPACE_AFTER else " ")
                )

            thai = re.sub(re.escape(form), spaced, thai)
    return thai


def present_terms(text: str, glossary: list[Term]) -> list[Term]:
    """Every glossary term in ``text`` (the checks use all of them, the prompt a subset)."""
    return [t for t in glossary if t.pattern.search(text)]


def load_glossary(conn: psycopg.Connection, doc_id: int) -> list[Term]:
    rows = conn.execute(
        """SELECT t.term_en, t.norm, t.decision, t.term_th, t.kind
           FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
           WHERE dt.doc_id = %s AND t.decision IN ('keep_en', 'translate') AND t.kind <> 'code'""",
        (doc_id,),
    ).fetchall()
    return [
        Term(
            r["term_en"], r["norm"], r["decision"], r["term_th"], r["kind"], term_pattern(r["norm"])
        )
        for r in rows
    ]


def matching_terms(text: str, glossary: list[Term], limit: int = 24) -> list[Term]:
    hits = present_terms(text, glossary)
    # the style rule keeps acronyms in English; leave them out to keep prompts short
    hits = [t for t in hits if not (t.kind == "acronym" and t.decision == "keep_en")]
    hits.sort(key=lambda t: (-len(t.term_en), t.term_en.lower()))
    return hits[:limit]


def rules_for(
    text: str, terms: list[Term], *, has_placeholders: bool, label: tuple[str, str] | None = None
) -> list[str]:
    rules = [STYLE_RULE]
    if has_placeholders:
        rules.append(f"Copy every placeholder such as {placeholder(1)} exactly as it is.")
    if label:
        rules.append(f'Translate "{label[0]}" as "{label[1]}".')
    for t in terms:
        if t.decision == "translate" and t.term_th:
            rules.append(f'Translate "{t.term_en}" as "{t.term_th}".')
        elif t.decision == "keep_en":
            rules.append(f'Keep "{t.term_en}" in English.')
    return rules
