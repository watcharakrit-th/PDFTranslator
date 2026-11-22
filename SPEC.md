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
| Multiple-choice question | The question and its a–d options form one chunk. Options alone mean nothing in search. In quiz sections (Review Questions, Self-Check), a numbered item followed by a./b. options counts as a question even without "?", such as "(Choose three answers.)" items or true/false statements. Lettered steps in a project stay separate. |
| "Key Terms" list | Kept but not embedded. Each printed line becomes a glossary candidate for R2. |
| "Label: value" line (e.g. "Estimated Time: 50 minutes") | A paragraph, not a heading, so it never enters the section path. |
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
- **Speed (measured in M3):** about **670 words a minute** with the GPU limits below, rests included. That's about 55 minutes per 100 pages of CompTIA, and **6–8 hours for the whole book**, so big books run overnight. An interrupted run resumes where it stopped.

## 6. Bilingual PDF (mirror layout)

- The Thai page is a copy of the English page. Only the English text being translated is removed; boxes, table shading, code blocks and images stay exactly as they are. Thai is drawn in the same areas.
- **Fitting:** Thai uses the original font size and may extend into free space below its block. Short lines (list items, headings) may also widen to the right, up to their column, shaded box or the next block. If still needed, the text shrinks to as little as 60% of the original size. Text that still doesn't fit is drawn smaller anyway (never dropped) and flagged in R4.
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
- **GPU safety:** all limits apply to the GPU **hot spot**, read through NVAPI, which runs up to 25 °C above the core temperature. New work waits at 75 °C until 65 °C, and a translation request stops at 90 °C. The card runs with a **150 W power limit** (set with `nvidia-smi -pl 150`; the default is 235 W). See README › GPU safety.
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
| M1 ✅ 2026-11-21 | Project skeleton, Docker Postgres, schema, `ingest` (Docling + PyMuPDF), cutter, term candidates | Ingest a book and inspect chunks in the DB |
| M2 ✅ 2026-11-22 | Review web page: R1, R2, R3 | Review chunks and terms |
| M3 ✅ 2026-11-22 | Typhoon engine, translation memory, R4 with Thai page preview, hot-spot GPU guard | Get a fully translated, reviewed book |
| M4 | Full-book renderer, side-by-side + alternate layouts, bookmarks, text-layer fix | Read the bilingual PDF |
| M5 | BGE-M3 embedding, R5, `search` | Search your books in Thai or English |
| M6 | Claude engine, Linux deployment guide | Switch engines, move to the server |

## 11. Prototype result

[prototype/page301_side_by_side.pdf](prototype/page301_side_by_side.pdf) shows CompTIA page 301, hand-translated by me to test rendering only. All 18 Thai blocks fit at full size with correct vowel and tone-mark stacking.

## 12. M1 results (CompTIA Linux+, pages 1–100)

| Measure | Result |
|---|---|
| Time | 151 s for 100 pages (≈ 20 min for the whole book) |
| GPU | Peak 67 °C in 5-page batches; no cool-down pauses needed |
| Pages | 1 cover, 1 title, 2 copyright, 6 table of contents, 90 body |
| Output | 1,598 blocks → 987 chunks (962 embedded) and 596 term candidates |

- **Layout cache:** layout results are cached per 5-page batch. Re-running an ingest after a rule change takes about 10 s and does no GPU work.
- **Missed lines:** Docling sometimes misses the first lines at the top of a page. These are kept as paragraphs flagged `orphan`, put in reading order by position, and joined to the previous page's unfinished sentence (9 such joins). R1 shows these flags.
- **Noisy phrases:** phrase-frequency terms ("press enter") are noisy. The R2 page will show them apart from styled terms, Key Terms entries and acronyms.

## 13. M2 results

- **Review page:** `pdftr review`, built with FastAPI and htmx, with no build step. It listens on localhost only, and actions must come from the page itself.
  - **R1 (English chunks):** the page image with chunk boxes. You can approve, edit, skip, change the kind, merge or split a chunk, approve a page, chapter or the whole book, and filter to pending or flagged chunks.
  - **R2 (English terms):** tabs by source and a Keep English / Translate / Not a term decision for each term.
  - **R3 (Thai terms):** a Thai field for each term marked Translate.
  - **Order:** each step unlocks when the previous one is complete, and every action is written to a review log.
- **Chunk markup:** chunks keep their tagged text (`markup_en`). That way an edit keeps code and key-term styling, and M3 can translate chunks with their styling.
- **Seed glossary:** about 190 common IT terms with established Thai words. They give the first-draft decisions:

| Kind of term | First draft |
|---|---|
| Command | Keep English, approved automatically |
| Term in the seed list | Translate, with its Thai filled in |
| Acronym or other term | Keep English |
| Phrase found only by frequency | Not a term |

  For CompTIA pages 1–100 that gives 68 Translate, 346 Keep English, 150 Not a term and 32 commands. Everything is reviewable in R2 and R3, and the glossary moves in and out as CSV.
- **Cutting fixes:** structural question detection merged 56 stray answer options into their questions, leaving 931 chunks and 58 multiple-choice questions. Two-column question lists no longer nest under each other.

## 14. M3 results

- **Translator:** Typhoon Translate 1.5 (4B, 2.5 GB) runs in a portable Ollama kept under `.cache`. `pdftr translate` starts Ollama when it's needed, then unloads the model and stops the server afterwards.
- **What is translated:**
  - Paragraphs, list items, questions, notes, captions, headings and running headers.
  - Multiple-choice questions are translated part by part, so each option keeps its own line.
  - Table cells are translated one by one, except cells that look like code.
  - Code blocks are copied unchanged.
  - Text made only of Keep English terms ("Linux", "MIPS/MIPS64") skips the model.
- **Prompt:** the model card's format, with numbered rules:
  - formal written Thai, with acronyms and product names kept in English;
  - copy each placeholder unchanged;
  - one rule for each glossary term in the text.

  Inline code, URLs, paths and e-mail addresses become placeholders. Key-term and bold styling is put back afterwards, and "Note"/"Tip" labels become หมายเหตุ/เคล็ดลับ.
- **Long text:** anything over 120 words is translated a few sentences at a time, which keeps each request to a few seconds of GPU work.
- **Clean-up:**
  - Lines where the model repeats its rules are dropped.
  - Common Thai spellings of Keep English names (ลินุกซ์ → Linux, ซีพียู → CPU) are switched back to English.
- **Checks (flags in R4):** dropped code, mostly English left, odd length, polite particles, an opening explanation, and glossary misses (a Thai term not used, or a Keep English term translated). Each R4 card names the term it means.
- **Translation memory:** a re-run translates only what changed, and a glossary change re-translates only the text that uses that term. Your R4 edits are reused wherever the same English appears, and always win.
- **R4 page:** the Thai page preview next to the chunk cards, where you can:
  - approve, edit or re-translate a chunk;
  - approve a whole page, chapter or the book;
  - jump to the next pending or flagged page.

  R3 has a **Suggest** button that asks Typhoon for Thai terms.

**GPU heat.** In the first test runs the guard read the core temperature, which stayed at 80 °C or below, while the hot spot reached **105 °C**. The guard now reads the hot spot and stops a request mid-way at 90 °C. You set a 150 W power limit. The test below ran on pages 25–27:

| Measure | Result |
|---|---|
| Work | 44 text pieces + 14 table cells (1,475 words) in 132 s, including 65 s of rest |
| Hot spot | peak **76 °C** (it was 100–105 °C at 235 W with the old guard); no waits, no stopped requests |
| Power, fan | at most 163 W in brief spikes (150 W limit), fan up to 51% |
| Quality | glossary rules followed, code and URLs intact, tables complete; most flags come from the test copy's unreviewed draft glossary |

**Rendering fixes:**
- **Lost text:** text that did not fit used to disappear from the Thai page. It is now always drawn.
- **Shrinking:** short lines may widen. On four test pages, text areas that had to shrink went from 26 to 6.

## 15. Open items

- **Power limit:** the 150 W limit resets at every reboot. The README has a one-line scheduled task that re-applies it at sign-in. The fans stay off below about 60 °C (core) and reached 51% in the test. A custom fan curve, or fresh thermal paste, would cool the GPU further, because a 25 °C gap between core and hot spot is large.
- **Headings and running headers:** they are translated and shown on the Thai page, but R4 can't edit them yet. Planned for M4, together with the TOC and bookmark titles.
- **Thai text layer:** copying text from the Thai page still gives wrong marks in some words. The page looks right. Fixed in M4.

- The third sample (*The Comprehensive DevOps Interview Guide*) is **damaged**. It was opened and saved as text, which replaced 5.3 million bytes with "�". Please copy it again from the original source using File Explorer, and don't open it in VS Code.
