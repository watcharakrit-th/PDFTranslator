# PDF Translator — open questions

Write your answer after **Answer:**. Leave it blank to accept my default (shown in *italics*).
The most useful thing you can send is #1, the sample PDFs.

## A. Your PDFs

1. ★ **Sample PDFs.** Put 2–3 real PDFs in `samples/` (next to this file), including your hardest one. A real file answers half of this list. If you have a good human Thai translation of any page, add it too. I'll use it to measure quality.
   **Answer:**
   My 3 hardest sample pdfs. I dont have good full translation yet.

2. **Text or scans?** Can you select the text, or are the pages scanned images? Scans need OCR, which adds errors. *Default: text PDFs only for v1.*
   **Answer:**
   Text only.

3. **Document type?** Textbooks, research papers (2 columns, equations), manuals, slide decks, standards? Any math or code?
   **Answer:**
   Textbook, for now mainly about coding and IT related.

4. **Size and volume?** Pages per file, files per month. *Default: up to ~500 pages, a few files a week.*
   **Answer:**
   unlimit

## B. Output PDF (the biggest decision)

5. ★ **Which layout?**
   - **(a) Mirror the original:** same pages. Images, tables, and drawings stay in place, and Thai replaces English in the same boxes. Thai needs more room, so tight boxes get smaller text and can look cramped.
   - **(b) Re-typeset:** a new, clean Thai PDF built from the structure: headings, paragraphs, bullets with correct indentation, tables, and images put back. Easiest to read, but page breaks won't match the original.
   - **(c) Bilingual:** (a) or (b) with the English shown next to the Thai.

   *Default: (b), with (c) as an option for studying. Choose (a) if your PDFs depend on visual design (slides, brochures, forms).*
   **Answer:**
   (c) Bilingual: join pdf together e.g. page1(eng) next to page1(thai), page2(eng) next to page2(thai)...

6. **In the output PDF** (not the embeddings): translate table cells? Rebuild the TOC in Thai with new page numbers and clickable bookmarks? Leave text inside images as it is? Keep or drop the original running headers/footers? *Default: translate tables and captions, rebuild the TOC, leave images, drop old headers/footers and add new page numbers.*
   **Answer:**
   Default.

7. **Look:** which font? Sarabun (formal, official-document look), IBM Plex Sans Thai (modern, designed to sit next to English words), or Noto Sans Thai. Which page size? Arabic digits and CE years (2024), or Thai digits and BE years (๒๕๖๗)? *Default: IBM Plex Sans Thai, A4, Arabic digits, CE years.*
   **Answer:**
   Default.

## C. Translation

8. ★ **Which translation engine is acceptable?** Is the content confidential, so it must stay on this PC?
   - **Claude API:** best quality, and best at keeping terms in English. Rough estimate for a 300-page book: ~$10–20 on Opus 5.5, about half on Sonnet 5.5, under $1 on Haiku 5.5. Batch mode halves these. I'll measure the real cost on your sample.
   - **Google / DeepL / Azure:** cheap and fast, but weak at keeping terms in English.
   - **Local Typhoon Translate:** free and private. Its quality on dense technical text still needs testing.

   *Default: Claude Opus 5.5, with a switch to Typhoon for offline use. Do you already have an API key?*
   **Answer:**
   use Typhoon on my PC for now but make it pluggable so I can swap to Claude API in the future.

9. ★ **Which words stay in English?** How should these come out?
   - "database" → `database` or `ฐานข้อมูล`?
   - "neural network" → `neural network` or `โครงข่ายประสาทเทียม`?
   - "algorithm" → `algorithm` or `อัลกอริทึม`?

   Policy options:
   - **(a)** all technical terms in English
   - **(b)** established Thai terms in Thai, specialized terms in English
   - **(c)** Thai with English in parentheses at first use, e.g. `ฐานข้อมูล (database)`

   Code, commands, URLs, file paths, product names, acronyms, and formulas always stay English.
   *Default: (a), as you asked. I'm checking because pure (a) can read like "ใช้ gradient descent เพื่อลดค่า loss function ของ neural network", which some readers find hard.*
   **Answer:**
   (b).

10. **Glossary:** will you give me a term list (CSV: English term → keep / Thai term)? Or should the tool collect terms from each document and let you approve them? Either way it's stored in Postgres, so every document uses the same terms. *Default: automatic, plus an optional CSV that overrides it.*
    **Answer:**
    Default.

11. **Style:** formal written Thai (textbook tone, no ครับ/ค่ะ), or a simpler explaining style? *Default: formal.*
    **Answer:**
    Default.

12. **Review step:** do you want to check and fix translations in an EN | TH file before the final PDF is made? Your fixes would be saved and reused as translation memory. *Default: a `--review` flag, off unless you use it.*
    **Answer:**
    manual review step
    1st - review cutted pieces before embeding and before translating. (still in english)
    2nd - review collected term list. (still in english)
    3rd - review cutted pieces, still not doing the embedding process. (Translated in thai)
    4th - review collected term list. (Translated in thai)
    5th - manual approve to start embedding process.

## D. Cutter

Chunks are for embedding only. The translator sees the whole section, so meaning stays consistent, and each Thai piece is mapped back to its chunk.

13. **Paragraph size:** a paragraph is never cut in the middle. Very long ones (over ~400 words): split at sentence ends, or keep whole? One-line paragraphs: keep alone, or merge? Paragraphs broken by a page or column end are joined back together. *Default: split very long ones at sentence ends only; keep short ones alone.*
    **Answer:**
    Default.

14. **Lists:** every bullet is its own chunk. Should sub-bullets be their own chunk, or part of the parent bullet? Do numbered steps (1. 2. 3.) follow the same rule? *Default: every level is its own chunk; numbered steps follow the bullet rule.*
    **Answer:**
    Default.

15. ★ **Context for small chunks:** "Supports up to 10 users" on its own means nothing to a search. Can I embed it as `Section path > list intro sentence > bullet`, and store the plain bullet text separately? Headings then become this context instead of being embedded alone. *Default: yes. Optional upgrade: an LLM writes a one-line context for each chunk. Search gets better, but it costs more.*
    **Answer:**
    Default.

16. **What else is skipped from embedding?** Besides images, tables, and the TOC: headers/footers, page numbers, bibliography, index, copyright page, code, formulas, captions, footnotes? Should skipped tables be stored as Markdown without a vector, for later notes and summaries? *Default: skip headers/footers, page numbers, bibliography, index, copyright page, and formulas; embed captions, footnotes, and code; store tables as Markdown.*
    **Answer:**
    Default.

## E. Embeddings and pgvector

17. ★ **Search language:** will you search in Thai, English, or both? *Default: store and embed both texts, so either language works.*
    **Answer:**
    Default.

18. **Embedding model:** local BGE-M3 (free, private) or an API (OpenAI, Voyage, Cohere, Gemini)? *Default: BGE-M3 on your GPU.*
    **Answer:**
    Default.

19. **Postgres:** do you already have PostgreSQL with pgvector somewhere (Supabase, Neon, a server)? *Default: Docker on this PC with the official `pgvector/pgvector` image. This is the only part that needs virtualization.*
    **Answer:**
    Default.

20. **Metadata:** each chunk stores the document, page, position on the page, section path, type (paragraph, bullet, caption…), order, English text, and Thai text. Do you need more, like tags, subject/course, or author? When a PDF is processed again: replace its chunks, or keep versions? *Default: replace, matched by file hash.*
    **Answer:**
    Default.

21. **"Future learning, notes, summary":** is that part of this build (search, Q&A, chapter summaries, flashcards), or just the database for now? Does "learning" mean you studying, or the system learning from your corrections? *Default: database plus a simple `search` command now.*
    **Answer:**
    Default.

## F. Running it

22. **How will you run it?** A CLI (`pdftr translate book.pdf`), a small web UI, or a watched folder? Only on this Windows PC, or later on a Linux server too? What run time is acceptable for a 300-page book? *Default: CLI on this PC, under ~1 hour per book.*
    **Answer:**
    Default. plus later on a Linux server too.

23. **Personal or commercial use?** Some library licenses matter if you sell or distribute the tool (PyMuPDF is AGPL; Docling is MIT). *Default: personal use.*
    **Answer:**
    Personal.
