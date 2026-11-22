"""The cutter: turn blocks (in reading order) into chunks for embedding.

Rules (from the requirements):
- never cut inside a paragraph; join a paragraph split by a page/column break
- one chunk per bullet / numbered step, at every nesting level
- headings are context, not chunks
- long paragraphs (> max_words) are split at sentence ends, for embedding only
- tables are stored as Markdown but not embedded; images/TOC/furniture are skipped
- multiple-choice options (a., b., ...) stay with their question
"""

from __future__ import annotations

import re

from pdftr.ingest.models import Block, Chunk
from pdftr.ingest.textutil import (
    ends_sentence,
    join_blocks,
    split_sentences,
    starts_lowercase,
    strip_markup,
    word_count,
)

SKIP_PAGE_KINDS = {"cover", "title", "copyright", "toc", "index", "bibliography", "blank"}
SKIP_ROLES = {"header", "footer", "picture", "formula", "toc"}
NO_EMBED_SECTIONS = {"key terms"}  # lists of bare terms: kept, used for the glossary, not embedded
ANSWER_RE = re.compile(r"^(answer|explanation|solution)\s*:", re.IGNORECASE)
OPTION_MARKER_RE = re.compile(r"^\(?[a-hA-H][.)]$")


class Cutter:
    def __init__(self, vocab: set[str], max_words: int = 400, target_words: int = 300) -> None:
        self.vocab = vocab
        self.max_words = max_words
        self.target_words = target_words
        self.chunks: list[Chunk] = []
        self.path: list[tuple[int, str]] = []
        self.cont: Chunk | None = None  # chunk a following block may continue
        self.list_intro: str | None = None
        self.list_parents: dict[int, str] = {}
        self.last_question: str | None = None
        self.mcq: Chunk | None = None  # open multiple-choice question

    # ------------------------------------------------------------------ helpers
    def _section_path(self) -> list[str]:
        return [title for _, title in self.path]

    def _context(
        self, *, intro: str | None = None, parent: str | None = None, question: str | None = None
    ) -> str:
        lines = []
        if self.path:
            lines.append("Section: " + " > ".join(self._section_path()))
        if intro:
            lines.append("Intro: " + intro)
        if parent:
            lines.append("Parent item: " + parent)
        if question:
            lines.append("Question: " + question)
        return "\n".join(lines)

    def _new(self, kind: str, block: Block, text: str, **ctx) -> Chunk:
        chunk = Chunk(
            kind=kind,
            blocks=[block],
            text=text,
            section_path=self._section_path(),
            context=self._context(**ctx),
        )
        # A bare list of terms (no sentences) is glossary input, not embedding content.
        # Sentences that land in such a section (e.g. a summary column) stay embedded.
        if (
            self.path
            and self.path[-1][1].lower() in NO_EMBED_SECTIONS
            and kind in ("paragraph", "list_item", "table")
            and not ends_sentence(text)
        ):
            chunk.embed = False
            chunk.flags.append("key_terms_list")
        self.chunks.append(chunk)
        return chunk

    @staticmethod
    def _last_sentence(text: str) -> str:
        sentences = split_sentences(text)
        return sentences[-1] if sentences else text

    # ------------------------------------------------------------------ main
    def feed(self, block: Block, page_kind: str) -> None:
        role = block.role
        if page_kind in SKIP_PAGE_KINDS or role in SKIP_ROLES:
            return
        plain = strip_markup(block.markup) if block.markup else block.text

        if role == "heading":
            level = block.level or 1
            title = block.style.get("outline_title") or block.text
            self.path = [(lv, t) for lv, t in self.path if lv < level]
            if not (self.path and self.path[-1][1] == title):
                self.path.append((level, title))
            self.cont = self.mcq = None
            self.list_intro = self.last_question = None
            self.list_parents = {}
            return

        if role == "table":
            chunk = self._new("table", block, block.text)
            chunk.embed = False
            self.cont = self.mcq = None
            return

        if role == "code":
            intro = None
            if self.chunks and self.chunks[-1].text.rstrip().endswith(":"):
                intro = self._last_sentence(self.chunks[-1].text)
            self._new("code", block, block.text, intro=intro)
            self.mcq = None
            return  # keep self.cont: prose may continue after a code sample

        if role == "list_item":
            self._feed_list_item(block, plain)
            return

        # paragraph, callout, caption, footnote, other
        if (
            role in ("paragraph", "other")
            and self.cont is not None
            and not ends_sentence(self.cont.text)
            and starts_lowercase(plain)
        ):
            prev_page = self.cont.blocks[-1].page_no
            self.cont.text = join_blocks(self.cont.text, plain, self.vocab)
            self.cont.blocks.append(block)
            self.cont.flags.append("joined_pages" if block.page_no != prev_page else "joined")
            return

        kind = {"paragraph": "paragraph", "other": "paragraph"}.get(role, role)
        question = None
        if role in ("paragraph", "callout", "other"):
            if ANSWER_RE.match(plain):
                kind, question = "answer", self.last_question
            elif plain.endswith("?") and word_count(plain) <= 60:
                kind = "question"
        chunk = self._new(kind, block, plain, question=question)
        if role == "other":
            chunk.flags.append("unknown_role")
        if kind == "question":
            self.last_question = plain
        self.mcq = None
        if role == "paragraph" and plain.endswith(":"):
            self.list_intro = self._last_sentence(plain)
        elif role in ("paragraph", "other"):
            self.list_intro = None
            self.list_parents = {}
        if role in ("paragraph", "callout", "other"):
            self.cont = chunk
        # captions/footnotes keep self.cont: a paragraph may continue around a figure

    def _feed_list_item(self, block: Block, plain: str) -> None:
        level = block.level or 0
        marker = (block.marker or "").strip()
        if self.mcq is not None and OPTION_MARKER_RE.match(marker):
            self.mcq.text += f"\n{marker} {plain}"
            self.mcq.blocks.append(block)
            if "mcq" not in self.mcq.flags:
                self.mcq.flags.append("mcq")
            return
        parent = self.list_parents.get(level - 1) if level > 0 else None
        is_question = plain.endswith("?")
        chunk = self._new(
            "question" if is_question else "list_item",
            block,
            plain,
            intro=self.list_intro,
            parent=parent,
            question=None if is_question else self.last_question,
        )
        chunk.list_level = level
        self.list_parents = {lv: t for lv, t in self.list_parents.items() if lv < level}
        self.list_parents[level] = plain
        if is_question:
            self.last_question = plain
            self.mcq = chunk
        else:
            self.mcq = None
        self.cont = chunk

    def finish(self) -> list[Chunk]:
        out: list[Chunk] = []
        for chunk in self.chunks:
            if (
                chunk.kind in ("paragraph", "callout", "answer")
                and word_count(chunk.text) > self.max_words
            ):
                out.extend(self._split(chunk))
                continue
            if chunk.kind == "paragraph" and word_count(chunk.text) < 4:
                chunk.flags.append("short")
            out.append(chunk)
        return out

    def _split(self, chunk: Chunk) -> list[Chunk]:
        parts: list[list[str]] = [[]]
        count = 0
        for sentence in split_sentences(chunk.text):
            n = word_count(sentence)
            if parts[-1] and count + n > self.target_words:
                parts.append([])
                count = 0
            parts[-1].append(sentence)
            count += n
        result = []
        for i, sentences in enumerate(parts):
            result.append(
                Chunk(
                    kind=chunk.kind,
                    blocks=chunk.blocks,
                    text=" ".join(sentences),
                    section_path=chunk.section_path,
                    context=chunk.context,
                    list_level=chunk.list_level,
                    part=i,
                    embed=chunk.embed,
                    flags=chunk.flags + ["split"],
                )
            )
        return result


def cut(blocks: list[Block], page_kinds: dict[int, str], vocab: set[str]) -> list[Chunk]:
    cutter = Cutter(vocab)
    for block in sorted(blocks, key=lambda b: b.ord):
        cutter.feed(block, page_kinds.get(block.page_no, "body"))
    return cutter.finish()
