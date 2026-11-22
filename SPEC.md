# PDF Translator — Spec v1

Status: **draft for your approval** · 2026-11-21
Answers this spec is based on: [requirements-questions.md](requirements-questions.md) + 4 follow-up choices.

## 1. Goal

Input: an English IT/coding textbook PDF (born-digital, any size).

Output:
1. A **bilingual PDF** with one wide sheet per page: the original English page on the left and its Thai translation on the right. With `--layout alternate`, the pages alternate instead (EN, TH, EN, TH…).
2. Every meaningful piece of the book (**chunk**) stored in **pgvector** in English and Thai, for later search, notes and summaries.

## 2. Pipeline and your 5 review steps

```
ingest ─► R1 review English chunks
       ─► collect terms ─► R2 review English term list
       ─► translate terms ─► R3 review Thai term list        (glossary is final here)
       ─► translate the book ─► R4 review Thai chunks + Thai page preview
       ─► render bilingual PDF (can be re-run any time)
       ─► R5 approve ─► embed (BGE-M3) ─► pgvector ─► search
```

- Each step unlocks only after the previous one is approved.
- You can approve one item, a page, a chapter, or everything at once.
- Filters show only flagged items: suspicious cuts, very long or short chunks, text that didn't fit, failed translation checks.

## 3. Extraction and the cutter

- **Docling** finds the structure: reading order, headings, paragraphs, bullets, tables, figures, captions, code, TOC, page headers and footers.
- **PyMuPDF** provides exact positions, fonts and colors. The two are matched by position on the page.
- A **block** is a piece of text on one page (used for rendering). A **chunk** is a unit for embedding, and can span two pages.

Cutting rules (your answers):

| Content | Rule |
|---|---|
| Paragraph | One chunk. Never cut in the middle. Parts split by a page or column break are joined. Over ~400 words: split at sentence ends (embedding only). |
| One-line paragraph | Stays its own chunk. |
| Bullet / numbered step | Each item is its own chunk, at every nesting level. |
| Heading | Not embedded alone. Becomes the context of the chunks under it. |
| Note / tip box, caption, footnote | Own chunk, embedded. |
| Code block | Own chunk, embedded once. Never translated or changed. |
| Table | Stored as Markdown, not embedded. Cells are translated in the PDF. |
| Image, TOC, page header/footer, page number, bibliography, index, copyright page, formula | Not embedded. |

Embedded text = **context + chunk**. The context is the section path (`Chapter 7 > Escape Sequences`) plus the sentence that introduces a list, or the question for a Q&A answer. The plain chunk text is stored separately.

## 4. Terminology (policy b)

- Words with an established Thai term are translated: database → ฐานข้อมูล.
- Specialized terms stay English: shell script, subshell, escape sequence.
- These always stay English: code, commands, options, file paths, URLs, product names and acronyms.
- Candidate terms are collected automatically from:
  - highlighted key terms (the CompTIA book marks them in blue bold)
  - acronyms
  - frequent noun phrases
  - the book's index
- A built-in seed list of common IT terms with established Thai equivalents decides the first draft. You correct it in R2 and R3.
- One glossary in Postgres is shared by all books. An optional CSV import overrides it.

## 5. Translation

- **Pluggable engine.** `typhoon` is the default: Typhoon Translate 1.5 (4B) running on this PC through Ollama. `claude` (Anthropic API) can be added later without changing the rest.
- Each request includes only the glossary rules for terms that appear in that text.
- Inline code, URLs and paths are replaced with placeholders before translation and restored after.
- Each output is checked: placeholders intact, no untranslated English sentences, reasonable length. Failures are flagged for R4.
- **Translation memory:** identical text is translated only once, and your R4 edits are reused automatically.
- Style: formal written Thai, Arabic digits, CE years.
- **Speed estimate:** Typhoon on the RTX 2070 SUPER needs roughly **3–7 hours** for the full CompTIA book (~290k words to translate, excluding code). The 147-page interview book takes under an hour. Big books run overnight, and an interrupted run resumes where it stopped. I'll measure the real speed in milestone M3.

## 6. Bilingual PDF (mirror layout)

- The Thai page is a copy of the English page. Only the English text being translated is removed; boxes, table shading, code blocks and images stay exactly as they are. Thai is drawn in the same areas.
- **Fitting:** Thai uses the original font size and may extend into free space below its block. If still needed, it shrinks to as little as 70% of the original size. Text that still doesn't fit is flagged in R4.
- **Fonts:** IBM Plex Sans Thai (Thai + Latin) and IBM Plex Mono for inline code. Lines break between Thai words, using word boundaries from PyThaiNLP.
- **Page furniture:**
  - The chapter title in the running header is translated, and the page number is kept.
  - The copyright footer is kept as is.
  - TOC titles are translated in place. Their page numbers stay valid because pages stay paired.
  - Index and bibliography pages are copied unchanged.
- **Bookmarks:** the book's outline, with titles translated.
- **Known issue (found in the prototype):** copying Thai text out of the PDF gives wrong tone marks in some words (คำสั่ง → คําสัǪง) and adds spaces between words. This happens with every Thai font tested; it's a PyMuPDF text-layer issue, and the page looks correct. Planned fix: rewrite the font's character map after rendering (milestone M4). The database always stores the correct Thai.

## 7. Storage and search

- **Database:** PostgreSQL + pgvector in Docker (official `pgvector/pgvector` image), with data kept in a Docker volume.
- **Tables:** `documents`, `pages`, `blocks`, `chunks`, `terms`, `doc_terms`, `translation_memory`, `jobs`.
- **Embeddings:** BGE-M3 (1024 dimensions) on the GPU. Both the English and the Thai text of each chunk are embedded, with an HNSW cosine index on each.
- **Re-processing:** the same file (same hash) reuses the existing document and all your reviews. `--force` starts over, after asking you to confirm.
- **Search:** `pdftr search "..."` accepts Thai or English. It shows the book, page, section, and the English and Thai text.

## 8. How you run it

| Command | Does |
|---|---|
| `pdftr db up` | Start Postgres (Docker) and create the tables |
| `pdftr ingest book.pdf` | Extract, cut, collect terms → ready for R1 |
| `pdftr review` | Open the review web page at http://127.0.0.1:8765 |
| `pdftr translate <book>` | Translate terms or the book (also startable from the web page) |
| `pdftr render <book>` | Build the bilingual PDF |
| `pdftr embed <book>` | Embed approved chunks into pgvector |
| `pdftr search "query"` | Search the database |

- Review web page: FastAPI with server-rendered HTML. No build step, so it runs on the Linux server unchanged.
- Settings go in a `.env` file: database URL, Ollama URL, model names, folders.

## 9. Platform

- **OS:** Windows 10 now, Linux server later. The code is cross-platform Python 3.12, managed with `uv`. Postgres runs in Docker and Ollama on both.
- **GPU:** RTX 2070 SUPER, 8 GB, shared by Docling, Typhoon and BGE-M3, one at a time. Each stage releases the GPU when it finishes.
- **One-time downloads:** about 10 GB in total:

| Download | Size |
|---|---|
| PyTorch + CUDA | ~2.5 GB |
| Docling models | ~0.5 GB |
| BGE-M3 | ~2.3 GB |
| Ollama | ~1 GB |
| Typhoon Translate 1.5 | ~2.6 GB |

- **Licenses:** PyMuPDF is AGPL, which is fine for personal use. Docling is MIT. The fonts are OFL.

## 10. Milestones

| # | Delivers | You can then |
|---|---|---|
| M1 | Project skeleton, Docker Postgres, schema, `ingest` (Docling + PyMuPDF), cutter, term candidates | Ingest a book and inspect chunks in the DB |
| M2 | Review web page: R1, R2, R3 | Review chunks and terms |
| M3 | Typhoon engine, translation memory, R4 with Thai page preview | Get a fully translated, reviewed book |
| M4 | Full-book renderer, side-by-side + alternate layouts, bookmarks, text-layer fix | Read the bilingual PDF |
| M5 | BGE-M3 embedding, R5, `search` | Search your books in Thai or English |
| M6 | Claude engine, Linux deployment guide | Switch engines, move to the server |

## 11. Prototype result

[prototype/page301_side_by_side.pdf](prototype/page301_side_by_side.pdf) shows CompTIA page 301, hand-translated by me to test rendering only. All 18 Thai blocks fit at full size with correct vowel and tone-mark stacking.

## 12. Open items

- The third sample (*The Comprehensive DevOps Interview Guide*) is **damaged**. It was opened and saved as text, which replaced 5.3 million bytes with "�". Please copy it again from the original source using File Explorer, and don't open it in VS Code.
