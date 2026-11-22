"""Collect glossary candidates for review step R2."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from pdftr.ingest.models import Block, Chunk, TermCandidate
from pdftr.ingest.textutil import split_sentences, strip_markup

KEY_RE = re.compile(r"<k>(.*?)</k>")
CODE_RE = re.compile(r"<c>(.*?)</c>")
ACRONYM_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})(?:s)?\b")
COMMAND_RE = re.compile(r"[a-z][a-z0-9_.+-]{1,30}")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#-]*")
PAREN_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)$")  # "Dynamic Host Configuration Protocol (DHCP)"
BODY_ROLES = {"paragraph", "list_item", "callout", "caption", "footnote"}

ACRONYM_STOP = {
    "I", "A", "OK", "AM", "PM", "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII",
    "XIII", "XIV", "XV", "XX", "TM", "NOTE", "FAQ", "ETC", "US", "AN", "IT", "AND", "THE", "OR",
}  # fmt: skip
# Common words that never start or end a term (kept as text: easy to read and edit).
_STOPWORDS_TEXT = """
    a an the and or but if then else of to in on at by for with from into onto over under
    as is are was were be been being this that these those it its it's you your we our they
    their he she his her them can could may might must should would will shall do does did
    done not no yes so than such also only very more most less least other another each
    every any some many much few both either neither all one two three first second new use
    used using uses following example examples way ways type types number numbers within
    without about after before between during through which who whom whose what when where
    why how there here just like make makes made get gets got set sets see seen
"""
STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


def normalize(term: str) -> str:
    t = re.sub(r"\s+", " ", term.strip().strip(".,;:[]\"'“”‘’")).strip()
    if not t:
        return ""
    if t.isupper() and len(t) <= 10:
        return t  # acronym
    t = t.lower()
    words = t.split(" ")
    last = words[-1]
    if len(last) > 4 and last.endswith("ies"):
        last = last[:-3] + "y"
    elif len(last) > 4 and last.endswith(("sses", "xes", "ches", "shes")):
        last = last[:-2]
    elif len(last) > 3 and last.endswith("s") and not last.endswith(("ss", "us", "is", "os")):
        last = last[:-1]
    words[-1] = last
    return " ".join(words)


def collect_terms(
    blocks: list[Block],
    chunks: list[Chunk],
    skip_pages: set[int],
    key_term_lines: list[tuple[int, str]] | None = None,
) -> list[TermCandidate]:
    """Candidates from styled key terms, Key Terms lists, acronyms, commands, n-grams.

    ``key_term_lines`` holds (page, line text) for every printed line of a
    "Key Terms" list; each line is one term.
    """
    body = [b for b in blocks if b.role in BODY_ROLES and b.page_no not in skip_pages]
    cands: dict[str, TermCandidate] = {}
    surface: dict[str, Counter] = defaultdict(Counter)

    def add(raw: str, kind: str, source: str, page: int) -> None:
        raw = re.sub(r"\s+", " ", raw).strip(" .,;:")
        if not raw or len(raw) < 2 or len(raw) > 60 or len(raw.split()) > 6:
            return
        norm = normalize(raw)
        if not norm or norm in STOPWORDS:
            return
        c = cands.get(norm)
        if c is None:
            c = cands[norm] = TermCandidate(norm=norm, term=raw, kind=kind, first_page=page)
        if kind == "term" and c.kind == "code":
            c.kind = "term"  # styled as a key term somewhere: a real concept
        c.sources.add(source)
        c.first_page = min(c.first_page or page, page)
        surface[norm][raw] += 1

    for b in body:
        for m in KEY_RE.finditer(b.markup):
            add(strip_markup(m.group(1)), "term", "styled", b.page_no)
        for m in CODE_RE.finditer(b.markup):
            token = strip_markup(m.group(1)).strip()
            if COMMAND_RE.fullmatch(token):
                add(token, "code", "code", b.page_no)
        for m in ACRONYM_RE.finditer(b.text):
            if m.group(1) not in ACRONYM_STOP and not m.group(1).isdigit():
                add(m.group(1), "acronym", "acronym", b.page_no)

    for page, line in key_term_lines or []:
        m = PAREN_RE.match(line.strip())
        if m:  # "Name (ACR)" -> the name, plus the acronym when it is one
            add(m.group(1), "term", "key_terms_list", page)
            if m.group(2).isupper():
                add(m.group(2), "acronym", "key_terms_list", page)
        else:
            add(line, "term", "key_terms_list", page)

    _add_ngrams(body, add)

    # frequency over one string per page (fewer regex scans); example from one block
    by_page: dict[int, list[str]] = defaultdict(list)
    for b in body:
        by_page[b.page_no].append(b.text)
    texts = [(page, "\n".join(parts)) for page, parts in sorted(by_page.items())]
    for norm, c in cands.items():
        forms = surface[norm]
        # "Software" at a sentence start is still the term "software"; "Linux" stays
        c.term = norm if any(f[:1].islower() for f in forms) else forms.most_common(1)[0][0]
        if c.kind == "acronym":
            pattern = re.compile(rf"\b{re.escape(norm)}s?\b")
        else:
            words = [re.escape(w) for w in norm.split(" ")]
            if norm.endswith("y"):
                words[-1] = words[-1][:-1] + r"(?:y|ies)"
            else:
                words[-1] = words[-1] + r"(?:s|es)?"
            pattern = re.compile(r"\b" + r"[\s-]+".join(words) + r"\b", re.IGNORECASE)
        for page, text in texts:
            hits = len(pattern.findall(text))
            if hits:
                c.freq += hits
                if c.example is None:
                    c.example = _example(by_page[page], pattern)
    return sorted(cands.values(), key=lambda c: (-c.freq, c.norm))


def _example(block_texts: list[str], pattern: re.Pattern) -> str | None:
    """First sentence (inside one block) that uses the term."""
    for text in block_texts:
        if pattern.search(text):
            return next((s for s in split_sentences(text) if pattern.search(s)), None)
    return None


def _add_ngrams(body: list[Block], add, min_freq: int = 5) -> None:
    counts: Counter = Counter()
    first_page: dict[tuple, int] = {}
    for b in body:
        for sentence in split_sentences(b.text):
            words = [w.lower() for w in WORD_RE.findall(sentence)]
            for n in (2, 3):
                for i in range(len(words) - n + 1):
                    gram = tuple(words[i : i + n])
                    if any(len(w) < 3 or w in STOPWORDS for w in gram):
                        continue
                    counts[gram] += 1
                    first_page.setdefault(gram, b.page_no)
    for gram, n in counts.items():
        if n >= min_freq:
            add(" ".join(gram), "term", "ngram", first_page[gram])


def key_term_lines(chunks: list[Chunk], lines_by_page: dict) -> list[tuple[int, str]]:
    """Printed lines inside the blocks of 'Key Terms' lists, one term per line.

    A wrapped term continues on an indented line (hanging indent), e.g.
    "GNU Network Object Model" + "   Environment (GNOME)"; a line starting
    with '(' also continues the previous term.
    """
    out: list[tuple[int, str]] = []
    for ch in chunks:
        if "key_terms_list" not in ch.flags:
            continue
        for b in ch.blocks:
            x0, y0, x1, y1 = b.bbox
            inside = [
                ln
                for ln in lines_by_page.get(b.page_no, [])
                if x0 - 2 <= (ln.bbox[0] + ln.bbox[2]) / 2 <= x1 + 2
                and y0 - 2 <= (ln.bbox[1] + ln.bbox[3]) / 2 <= y1 + 2
            ]
            if not inside:
                continue
            left = min(ln.bbox[0] for ln in inside)
            started = False
            for ln in sorted(inside, key=lambda ln: (ln.bbox[1], ln.bbox[0])):
                text = ln.text.strip()
                if not text:
                    continue
                continues = ln.bbox[0] > left + 4 or text.startswith("(")
                if started and continues:
                    out[-1] = (b.page_no, f"{out[-1][1]} {text}")
                else:
                    out.append((b.page_no, text))
                    started = True
    return out
