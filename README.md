# pdftr: English textbook PDF → bilingual EN/TH PDF + pgvector

See [SPEC.md](SPEC.md) for the design and milestones.
Status: **M1 (ingest) and M2 (review page for R1–R3) done.**

## Setup (Windows, PowerShell)

```powershell
cd D:\Python\PDFTranslator
. .\scripts\env.ps1                     # keeps downloads/models/temp files on drive D
python -m uv sync --group dev           # Python 3.12 venv in .venv (PyTorch CUDA 12.8)
.venv\Scripts\pdftr db up               # PostgreSQL 17 + pgvector in Docker (127.0.0.1:5433)
```

Everything large stays under `D:\Python\PDFTranslator`:

| Path | Contents |
|---|---|
| `.venv\` | Python environment (~5 GB, mostly PyTorch) |
| `.cache\uv`, `.cache\python` | uv download cache, Python 3.12 |
| `.cache\huggingface` | Docling layout/table models (~0.5 GB), later BGE-M3 |
| `.cache\tmp` | temp files of pdftr runs |
| `data\doc_<id>\` | JSON dumps and debug images per document |
| Docker volume `pdftranslator_pdftr-pgdata` | the database (Docker Desktop keeps it under `D:\Docker\wsl`) |

## GPU safety

GPU work runs in small batches (5 pages; GPU batch size 2). Before every batch the GPU
temperature is checked: at **75 °C or more** the run pauses until it is back at **65 °C**.
Change the limits in `.env` (`PDFTR_GPU_PAUSE_AT_C`, `PDFTR_GPU_RESUME_AT_C`), or set
`PDFTR_GPU_DEVICE=cpu` to keep the GPU idle (slower). `pdftr gpu` shows the current state.

## Reviewing (R1–R3)

```powershell
.venv\Scripts\pdftr review          # opens http://127.0.0.1:8765 in your browser
```

- **R1 · English chunks.** Your page image is on the left, with every chunk drawn as a numbered box. Hover a card to light up its box; click a box to jump to its card. For each chunk you can:
  - **Approve** it
  - **Edit** its text (code and key-term styling are kept where the words are unchanged)
  - **Skip** it (kept, but not embedded)
  - change its **kind**
  - **Merge ↑** it into the chunk before it
  - **Split ✂** it between sentences or blocks

  You can also approve a whole page, a chapter or the whole book.
  - **Filters:** *pending* and *flagged* show only what still needs a look, and **Next flagged** jumps to the next page with a flagged chunk.
  - **Shortcuts:** `n` / `p` next or previous page · `f` next flagged page · `Shift+A` approve page.
- **R2 · English terms.** The tabs split terms by where they came from: the book's key terms, acronyms, "maybe" phrases and commands. For each term choose **Keep English**, **Translate** or **Not a term**; changing the decision approves the term. A first draft comes from the built-in seed list (policy b).
- **R3 · Thai terms.** Only the terms to translate. Thai from the seed list is filled in; type the missing ones. Typing approves the term. After R3 the glossary is final, ready for translation in M3.
- **Order:** each step unlocks when the previous one is complete. The glossary is shared by all your books.

**Safety.** The page listens on 127.0.0.1 only and answers only to that host name. Actions must come from the page itself (htmx header check), so other websites cannot trigger them. It has no login: on a server, keep it on 127.0.0.1 and use an SSH tunnel.

**Glossary CSV.** The columns are `term_en, decision, term_th, kind, note`, where `decision` is `keep_en`, `translate` or `ignore`. Imported rows override the glossary and count as reviewed. Export, edit in Excel, import back:

```powershell
.venv\Scripts\pdftr glossary export glossary.csv
.venv\Scripts\pdftr glossary import glossary.csv
```

## Commands

| Command | Does |
|---|---|
| `pdftr review [--port 8765] [--no-open]` | Open the review web page (R1–R3) |
| `pdftr glossary export\|import <csv>` | Glossary to / from CSV (UTF-8, opens in Excel) |
| `pdftr glossary seed` | Re-apply the seed list to terms nobody has reviewed yet |
| `pdftr ingest <pdf> --pages 1-100` | Layout analysis, cutting, term candidates → database |
| `pdftr docs` | List documents |
| `pdftr stats <doc>` | Counts by page kind, block role, chunk kind, flag |
| `pdftr show <doc> <page>` | Blocks and chunks of one page, with context |
| `pdftr terms <doc> --top 50 [--kind term\|acronym\|code]` | Term candidates for review R2 |
| `pdftr debug-pages <doc> --pages 15-20` | Page images with block boxes and chunk numbers |
| `pdftr db status` | Database version and row counts |

Ingesting more pages of the same PDF later (`--pages 101-200`) adds them to the same
document. Re-ingesting pages that already exist is refused unless you pass `--force`,
which deletes that document's chunks and reviews and starts over.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

Database tests create a throwaway database inside the running container and drop it afterwards; they are skipped when the database is not running.
