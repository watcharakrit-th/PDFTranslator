"""Translate a document: chunks, the blocks they cover, headings, headers and tables."""

from __future__ import annotations

import html
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from pdftr.gpu import GpuGuard, GpuTooHot
from pdftr.ingest.textutil import split_sentences, strip_markup, word_count
from pdftr.render.contents import entries, exclusions, is_contents
from pdftr.render.contents import key as title_key
from pdftr.translate import memory
from pdftr.translate.checks import check
from pdftr.translate.engine import Engine, drop_echo
from pdftr.translate.protect import PH_RE, TAGGED_SPAN_RE, protect, restore
from pdftr.translate.rules import (
    Term,
    load_glossary,
    matching_terms,
    present_terms,
    put_back_kept_names,
    rules_for,
)
from pdftr.translate.titles import heading_thai, norm_title, translate_outline

PROMPT_VERSION = "2"  # 2: names and acronyms stay English (style rule)
LONG_WORDS = 120  # longer text is translated a few sentences at a time
SHORT_WORDS = 40  # after the GPU guard stopped a request, the text is retried this short
TRANSLATED_KINDS = (
    "paragraph",
    "list_item",
    "question",
    "answer",
    "callout",
    "caption",
    "footnote",
)
LETTERS_RE = re.compile(r"[A-Za-z]{2,}")


@dataclass
class Unit:
    table: str  # "chunks" or "blocks"
    id: int
    markup: str  # source markup
    page: int
    key: str | None  # tr_key of the current translation
    has_th: bool


@dataclass
class Outcome:
    source: str  # human | tm | mt | kept (only Keep-English terms) | unchanged
    seconds: float = 0.0
    flags: tuple[str, ...] = ()


class Glossary:
    def __init__(self, terms: list[Term]) -> None:
        self.terms = terms
        self.thai = {t.norm: t.term_th for t in terms if t.decision == "translate" and t.term_th}


# ------------------------------------------------------------------------- planning
def plan_units(conn: psycopg.Connection, doc_id: int, first: int, last: int):
    """Translation units in reading order, plus code chunks (copied) and table blocks."""
    chunks = conn.execute(
        """SELECT id, kind, flags, markup_en, text_en, block_ids, page_start, tr_key,
                  markup_th IS NOT NULL AS has_th
           FROM chunks WHERE doc_id=%s AND page_start BETWEEN %s AND %s ORDER BY ord, part""",
        (doc_id, first, last),
    ).fetchall()
    units: list[Unit] = []
    copies: list[int] = []
    tables: list[int] = []
    mcq_blocks: set[int] = set()
    covered: set[int] = set()
    for c in chunks:
        covered.update(c["block_ids"])
        if c["kind"] == "code":
            copies.append(c["id"])
        elif c["kind"] == "table":
            tables.extend(c["block_ids"])
        elif "mcq" in c["flags"]:
            mcq_blocks.update(c["block_ids"])
        elif c["kind"] in TRANSLATED_KINDS:
            markup = c["markup_en"] or html.escape(c["text_en"], quote=False)
            units.append(Unit("chunks", c["id"], markup, c["page_start"], c["tr_key"], c["has_th"]))
    blocks = conn.execute(
        """SELECT b.id, b.role, b.page_no, b.markup_en, b.text_en, b.tr_key,
                  b.markup_th IS NOT NULL AS has_th, p.kind AS page_kind
           FROM blocks b JOIN pages p ON p.doc_id = b.doc_id AND p.page_no = b.page_no
           WHERE b.doc_id=%s AND b.page_no BETWEEN %s AND %s AND b.translate
           ORDER BY b.ord""",
        (doc_id, first, last),
    ).fetchall()
    for b in blocks:
        if is_contents(b):  # contents pages: titles are translated cell by cell
            tables.append(b["id"])
            continue
        own = b["id"] in mcq_blocks
        extra = b["role"] in ("heading", "header") and b["id"] not in covered
        if (own or extra) and LETTERS_RE.search(b["text_en"] or ""):
            markup = b["markup_en"] or html.escape(b["text_en"], quote=False)
            units.append(Unit("blocks", b["id"], markup, b["page_no"], b["tr_key"], b["has_th"]))
    return units, copies, tables


# ---------------------------------------------------------------------- one unit
def sentence_groups(markup: str, max_words: int = LONG_WORDS) -> list[str]:
    """Split long markup into groups of whole sentences (tags never cut in half)."""
    groups: list[list[str]] = [[]]
    count = 0
    for sentence in split_sentences(markup):
        n = word_count(sentence)
        if groups[-1] and count + n > max_words:
            groups.append([])
            count = 0
        groups[-1].append(sentence)
        count += n
    joined = [" ".join(g) for g in groups if g]
    merged: list[str] = []
    for g in joined:  # a sentence end inside inline code must not split its tags
        if merged and any(merged[-1].count(f"<{t}>") != merged[-1].count(f"</{t}>") for t in "ckb"):
            merged[-1] = f"{merged[-1]} {g}"
        else:
            merged.append(g)
    return merged


def translate_text(
    conn: psycopg.Connection,
    engine: Engine,
    glossary: Glossary,
    markup: str,
    *,
    force: bool = False,
    retry: bool = False,
    current_key: str | None = None,
    has_th: bool = False,
    split: bool = True,
    max_words: int = LONG_WORDS,
):
    """Translate one piece of markup. Returns (thai_markup | None if unchanged, key, Outcome).

    Text over LONG_WORDS words is translated in groups of sentences: each request
    stays short (seconds of GPU load, not half a minute) and the model does better.
    When the GPU guard stops a request (``GpuTooHot``, after the GPU has cooled),
    the text is retried in groups of SHORT_WORDS words; a piece that is already that
    short gets one more try, and a second ``GpuTooHot`` stops the run.
    """
    human = memory.human_get(conn, markup)
    if human is not None and not retry:
        return human, memory.human_key(markup), Outcome("human")
    prot = protect(markup)
    terms = matching_terms(prot.text, glossary.terms)
    rules = rules_for(prot.text, terms, has_placeholders=bool(prot.slots), label=prot.label)
    key = memory.key_for(engine.name, PROMPT_VERSION, prot.text, *rules)
    if not (force or retry) and current_key == key and has_th:
        return None, key, Outcome("unchanged")
    if _only_kept_terms(prot.text, glossary.terms):  # "Linux", "MIPS/MIPS64": no model needed
        return markup, key, Outcome("kept")
    if split and word_count(prot.text) > max_words:
        groups = sentence_groups(markup, max_words)
        if len(groups) > 1:
            return _translate_groups(conn, engine, glossary, groups, key, force, retry, max_words)
    present = present_terms(prot.text, glossary.terms)
    thai = None if (force or retry) else memory.mt_get(conn, key)
    outcome = Outcome("tm")
    if thai is None:
        try:
            result = engine.translate(prot.text, rules, retry=retry)
        except GpuTooHot:
            shorter = sentence_groups(markup, SHORT_WORDS) if max_words > SHORT_WORDS else []
            if len(shorter) > 1:
                return _translate_groups(
                    conn, engine, glossary, shorter, key, force, retry, SHORT_WORDS
                )
            result = engine.translate(prot.text, rules, retry=retry)  # cooled: last try
        thai = put_back_kept_names(drop_echo(prot.text, result.text), present)
        outcome = Outcome("mt", result.seconds)
    markup_th, flags = restore(thai, prot, glossary.thai)
    flags += check(prot.text, thai, present)
    if outcome.source == "mt" and "placeholder_missing" in flags and not retry:
        # one retry at a higher temperature; keep whichever result has fewer problems
        try:
            result = engine.translate(prot.text, rules, retry=True)
        except GpuTooHot:  # keep the first answer; it is flagged for R4
            result = None
        if result is not None:
            thai_2 = put_back_kept_names(drop_echo(prot.text, result.text), present)
            markup_2, flags_2 = restore(thai_2, prot, glossary.thai)
            flags_2 += check(prot.text, thai_2, present)
            outcome = Outcome("mt", outcome.seconds + result.seconds)
            if len(flags_2) < len(flags):
                thai, markup_th, flags = thai_2, markup_2, flags_2
    if outcome.source == "mt":
        memory.mt_put(conn, key, prot.text, thai, engine.name)
    return markup_th, key, Outcome(outcome.source, outcome.seconds, tuple(sorted(set(flags))))


def _only_kept_terms(text: str, terms: list[Term]) -> bool:
    """True when nothing but Keep-English terms, placeholders, digits and punctuation is left."""
    rest = PH_RE.sub(" ", text)
    found = False
    for t in present_terms(rest, terms):
        if t.decision == "keep_en":
            rest, n = t.pattern.subn(" ", rest)
            found = found or n > 0
    return found and not LETTERS_RE.search(rest)


def _translate_groups(
    conn: psycopg.Connection,
    engine: Engine,
    glossary: Glossary,
    groups: list[str],
    key: str,
    force: bool,
    retry: bool,
    max_words: int,
):
    """Translate sentence groups one by one and join them (the whole text keeps ``key``)."""
    pieces, flags, seconds, sources = [], [], 0.0, set()
    for group in groups:
        piece, _k, o = translate_text(
            conn, engine, glossary, group, force=force, retry=retry, split=False,
            max_words=max_words,
        )  # fmt: skip
        pieces.append(piece or "")
        flags += o.flags
        seconds += o.seconds
        sources.add(o.source)
    source = "mt" if "mt" in sources else "human" if sources == {"human"} else "tm"
    return " ".join(pieces), key, Outcome(source, seconds, tuple(sorted(set(flags))))


def save_unit(
    conn: psycopg.Connection, unit: Unit, markup_th: str, key: str, outcome: Outcome
) -> None:
    if unit.table == "chunks":
        conn.execute(
            """UPDATE chunks SET markup_th=%s, text_th=%s, tr_source=%s, tr_flags=%s, tr_key=%s,
                      translated_at=now(),
                      review_th = CASE WHEN markup_th IS DISTINCT FROM %s THEN 'pending'
                                       ELSE review_th END
               WHERE id=%s""",
            (
                markup_th,
                strip_markup(markup_th),
                outcome.source,
                list(outcome.flags),
                key,
                markup_th,
                unit.id,
            ),
        )
    else:
        conn.execute(
            "UPDATE blocks SET markup_th=%s, tr_source=%s, tr_flags=%s, tr_key=%s WHERE id=%s",
            (markup_th, outcome.source, list(outcome.flags), key, unit.id),
        )


# ------------------------------------------------------------------- whole document
def translate_document(
    conn: psycopg.Connection,
    doc_id: int,
    engine: Engine,
    *,
    first: int = 1,
    last: int = 10**9,
    force: bool = False,
    guard: GpuGuard | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Translate pages ``first``..``last``. The engine guards the GPU itself (see
    ``GuardedEngine``); ``guard`` is only read for the log and the summary.
    Every finished piece is committed at once, so a stop loses nothing."""
    glossary = Glossary(load_glossary(conn, doc_id))
    units, copies, tables = plan_units(conn, doc_id, first, last)
    conn.execute(
        """UPDATE chunks SET markup_th='', text_th=text_en, tr_source='copy', tr_flags='{}',
                  review_th='approved', translated_at=now()
           WHERE id = ANY(%s) AND markup_th IS NULL""",
        (copies,),
    )
    stats: Counter = Counter()
    flagged = 0
    started = time.monotonic()
    log(
        f"{len(units)} pieces to check, {len(copies)} code blocks copied, "
        f"{len(tables)} tables; glossary: {len(glossary.terms)} terms"
    )
    for i, unit in enumerate(units, 1):
        markup_th, key, outcome = translate_text(
            conn,
            engine,
            glossary,
            unit.markup,
            force=force,
            current_key=unit.key,
            has_th=unit.has_th,
        )
        stats[outcome.source] += 1
        if markup_th is not None:
            save_unit(conn, unit, markup_th, key, outcome)
            flagged += bool(outcome.flags)
        conn.commit()
        if i % 10 == 0 or i == len(units):
            elapsed = time.monotonic() - started
            eta = elapsed / i * (len(units) - i)
            temp = guard.temperature() if guard is not None else None
            log(
                f"  {i}/{len(units)} · {stats['mt']} translated, {stats['tm'] + stats['human']} "
                f"from memory, {stats['unchanged']} unchanged · {flagged} flagged · "
                f"ETA {eta / 60:.0f} min" + (f" · GPU hot spot {temp}°C" if temp else "")
            )
    titles = heading_thai(conn, doc_id)  # contents and bookmarks reuse heading Thai
    stats["table_cells"] = translate_tables(
        conn, tables, engine, glossary, force=force, titles=titles, log=log
    )

    def title(markup: str):
        markup_th, key, outcome = translate_text(conn, engine, glossary, markup, force=force)
        return markup_th, key, outcome.flags

    stats["outline"] = translate_outline(
        conn, doc_id, title, first=first, last=last, force=force, titles=titles
    )
    apply_to_blocks(conn, doc_id, first, last)
    conn.commit()
    advanced = _advance_stage(conn, doc_id)
    return {
        "units": len(units),
        **dict(stats),
        "flagged": flagged,
        "seconds": round(time.monotonic() - started),
        "stage_advanced": advanced,
        "gpu_peak_hotspot_c": guard.peak_c if guard else None,
        "gpu_paused_s": round(guard.paused_s) if guard else None,
        "gpu_rested_s": round(guard.rested_s) if guard else None,
        "gpu_requests_stopped": guard.stopped if guard else None,
    }


def translate_tables(
    conn: psycopg.Connection,
    block_ids: list[int],
    engine: Engine,
    glossary: Glossary,
    *,
    force: bool = False,
    titles: dict[str, str] | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> int:
    """Translate the text cells of tables and contents pages (code-like cells and page
    numbers stay as they are). A contents title reuses its heading's Thai if known."""
    count = 0
    pdf = None
    for b in conn.execute(
        """SELECT b.id, b.doc_id, b.page_no, b.role, b.x0, b.y0, b.x1, b.y1, b.style,
                  b.cells_th, p.kind AS page_kind, d.file_path
           FROM blocks b JOIN pages p ON p.doc_id = b.doc_id AND p.page_no = b.page_no
                JOIN documents d ON d.id = b.doc_id
           WHERE b.id = ANY(%s) ORDER BY b.ord""",
        (block_ids,),
    ).fetchall():
        if is_contents(b):
            if pdf is None:
                import pymupdf

                pdf = pymupdf.open(b["file_path"])
            count = _translate_contents(conn, b, pdf, engine, glossary, force, titles, log, count)
            continue
        cells = (b["style"] or {}).get("cells") or []
        done = dict(b["cells_th"] or {})
        for cell in cells:
            text = (cell.get("text") or "").strip()
            pos = f"{cell['row']},{cell['col']}"
            if not _translatable_cell(text) or (pos in done and not force):
                continue
            markup_th, _key, _outcome = translate_text(
                conn, engine, glossary, html.escape(text, quote=False), force=force
            )
            done[pos] = strip_markup(markup_th or "")
            count += 1
            if count % 25 == 0:
                log(f"  tables and contents: {count} cells")
            conn.execute("UPDATE blocks SET cells_th=%s WHERE id=%s", (Jsonb(done), b["id"]))
            conn.commit()
        conn.execute("UPDATE blocks SET cells_th=%s WHERE id=%s", (Jsonb(done), b["id"]))
    for c in conn.execute(
        "SELECT id, block_ids FROM chunks WHERE kind='table' AND block_ids && %s::bigint[]",
        (block_ids,),
    ).fetchall():
        conn.execute(
            "UPDATE chunks SET text_th=%s, markup_th='', tr_source='mt', "
            "translated_at=now() WHERE id=%s",
            (_table_markdown(conn, c["block_ids"]), c["id"]),
        )
    return count


def _translate_contents(conn, b, pdf, engine, glossary, force, titles, log, count) -> int:
    """A contents block, entry by entry (see pdftr.render.contents); Thai under title keys."""
    done = {k: v for k, v in (b["cells_th"] or {}).items() if k.startswith("t:")}
    others = conn.execute(
        "SELECT role, x0, y0, x1, y1, translate FROM blocks WHERE doc_id=%s AND page_no=%s",
        (b["doc_id"], b["page_no"]),
    ).fetchall()
    page = pdf[b["page_no"] - 1]
    for e in entries(page, (b["x0"], b["y0"], b["x1"], b["y1"]), exclusions(others)):
        k = title_key(e.title)
        if (k in done and not force) or not LETTERS_RE.search(e.title):
            continue
        same = (titles or {}).get(norm_title(e.title))
        if same:
            done[k] = same
        else:
            markup_th, _key, _outcome = translate_text(
                conn, engine, glossary, html.escape(e.title, quote=False), force=force
            )
            done[k] = strip_markup(markup_th or "")
        count += 1
        if count % 25 == 0:
            log(f"  tables and contents: {count} cells")
        conn.execute("UPDATE blocks SET cells_th=%s WHERE id=%s", (Jsonb(done), b["id"]))
        conn.commit()
    conn.execute("UPDATE blocks SET cells_th=%s WHERE id=%s", (Jsonb(done), b["id"]))
    return count


def _translatable_cell(text: str) -> bool:
    words = re.findall(r"[A-Za-z]{3,}", text)
    return bool(words) and not re.match(r"^[\\/$~-]", text)


def _table_markdown(conn: psycopg.Connection, block_ids: list[int]) -> str:
    lines = []
    for b in conn.execute(
        "SELECT style, cells_th FROM blocks WHERE id = ANY(%s) ORDER BY ord", (block_ids,)
    ).fetchall():
        cells = (b["style"] or {}).get("cells") or []
        th = b["cells_th"] or {}
        rows: dict[int, dict[int, str]] = defaultdict(dict)
        for cell in cells:
            pos = f"{cell['row']},{cell['col']}"
            rows[cell["row"]][cell["col"]] = th.get(pos) or cell.get("text") or ""
        for r in sorted(rows):
            lines.append("| " + " | ".join(rows[r][c] for c in sorted(rows[r])) + " |")
            if r == min(rows):
                lines.append("|" + "---|" * len(rows[r]))
    return "\n".join(lines)


# ---------------------------------------------------- chunk Thai -> page blocks
def apply_to_blocks(
    conn: psycopg.Connection, doc_id: int, first: int = 1, last: int = 10**9
) -> None:
    """Give every block the Thai it shows on the Thai page.

    - a block with one chunk (or several parts of one long paragraph): their Thai;
    - a paragraph split by a page break: its Thai is split at a space, in proportion
      to the English lengths of the two blocks (flagged ``split_th`` for review);
    - a multiple-choice question: its blocks were translated one by one; the chunk
      gets question + options back as one text.
    """
    chunks = conn.execute(
        """SELECT id, kind, flags, block_ids, part, markup_th FROM chunks
           WHERE doc_id=%s AND page_start BETWEEN %s AND %s AND kind = ANY(%s)
           ORDER BY ord, part""",
        (doc_id, first, last, list(TRANSLATED_KINDS)),
    ).fetchall()
    groups: dict[tuple[int, ...], list[str | None]] = defaultdict(list)
    for c in chunks:
        if "mcq" in c["flags"]:
            _assemble_mcq(conn, c)
        else:
            groups[tuple(c["block_ids"])].append(c["markup_th"])
    for ids, parts in groups.items():
        if any(p is None for p in parts):
            continue
        thai = " ".join(p for p in parts if p)
        if len(ids) == 1:
            conn.execute("UPDATE blocks SET markup_th=%s WHERE id=%s", (thai, ids[0]))
            continue
        lengths = {
            r["id"]: len(r["text_en"] or "")
            for r in conn.execute(
                "SELECT id, text_en FROM blocks WHERE id = ANY(%s)", (list(ids),)
            ).fetchall()
        }
        pieces = split_markup(thai, [lengths.get(i, 1) for i in ids])
        for n, (block_id, piece) in enumerate(zip(ids, pieces, strict=True)):
            conn.execute(
                "UPDATE blocks SET markup_th=%s, "
                "tr_flags = CASE WHEN %s THEN array_append(array_remove(tr_flags, 'split_th'), "
                "'split_th') ELSE tr_flags END WHERE id=%s",
                (piece, n > 0, block_id),
            )


def _assemble_mcq(conn: psycopg.Connection, chunk: dict) -> None:
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, markup_th, style, tr_flags, tr_source FROM blocks WHERE id = ANY(%s)",
            (chunk["block_ids"],),
        ).fetchall()
    }
    blocks = [rows[i] for i in chunk["block_ids"] if i in rows]
    if not blocks or any(b["markup_th"] is None for b in blocks):
        return
    lines = [blocks[0]["markup_th"]]
    for b in blocks[1:]:
        marker = html.escape((b["style"] or {}).get("marker") or "", quote=False)
        lines.append(f"{marker} {b['markup_th']}".strip())
    markup_th = "\n".join(lines)
    flags = sorted({f for b in blocks for f in (b["tr_flags"] or [])})
    sources = {b["tr_source"] for b in blocks}
    source = "human" if sources == {"human"} else "mt" if "mt" in sources else "tm"
    conn.execute(
        """UPDATE chunks SET markup_th=%s, text_th=%s, tr_flags=%s, tr_source=%s,
                  translated_at=now(),
                  review_th = CASE WHEN markup_th IS DISTINCT FROM %s THEN 'pending'
                                   ELSE review_th END
           WHERE id=%s""",
        (markup_th, strip_markup(markup_th), flags, source, markup_th, chunk["id"]),
    )


def split_markup(markup: str, weights: list[int]) -> list[str]:
    """Split Thai markup into len(weights) pieces at spaces outside tags, in proportion."""
    if len(weights) == 1:
        return [markup]
    inside = [m.span() for m in TAGGED_SPAN_RE.finditer(markup)]
    spaces = [
        m.start()
        for m in re.finditer(r" ", markup)
        if not any(a < m.start() < b for a, b in inside)
    ]
    total = sum(weights) or 1
    cuts, acc = [], 0
    for w in weights[:-1]:
        acc += w
        target = len(markup) * acc / total
        options = [p for p in spaces if p not in cuts and (not cuts or p > cuts[-1])]
        cuts.append(min(options, key=lambda p: abs(p - target)) if options else len(markup))
    pieces, start = [], 0
    for cut in cuts:
        pieces.append(markup[start:cut].strip())
        start = cut
    pieces.append(markup[start:].strip())
    return pieces


def _advance_stage(conn: psycopg.Connection, doc_id: int) -> bool:
    """translate -> r4_chunks_th once every chunk of the document has Thai."""
    row = conn.execute(
        """SELECT d.stage, count(c.*) FILTER (WHERE c.text_th IS NULL) AS missing
           FROM documents d LEFT JOIN chunks c ON c.doc_id = d.id
           WHERE d.id=%s GROUP BY d.stage""",
        (doc_id,),
    ).fetchone()
    if row and row["stage"] == "translate" and row["missing"] == 0:
        conn.execute(
            "UPDATE documents SET stage='r4_chunks_th', updated_at=now() WHERE id=%s", (doc_id,)
        )
        conn.commit()
        return True
    return False
