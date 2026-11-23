# pdftr: English textbook PDF → bilingual EN/TH PDF + pgvector

See [SPEC.md](SPEC.md) for the design and milestones.
Status: **M1–M6 done**: ingest, review steps R1–R5, translation with Typhoon or Claude,
the bilingual PDF, embeddings and search. Linux server: [docs/linux.md](docs/linux.md).

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

Every limit applies to the GPU **hot spot**, its hottest sensor. That's the reading
HWiNFO and GPU-Z show, and pdftr reads it through NVIDIA's NVAPI. On the RTX 2070 SUPER
the hot spot runs about 11 °C above the core temperature at idle and about 25 °C above it
under load. The core temperature is what `nvidia-smi` and most tools report.

- **Waits:** GPU work (layout batches, translation requests) starts only below **75 °C**.
  Once the hot spot reaches that, work waits until it is back at **65 °C**.
- **Stops:** translation requests are streamed, and a request is stopped the moment the
  hot spot reaches **90 °C**. After the GPU cools down, the text is retried in pieces of
  at most 40 words. If even a short piece reaches 90 °C, the run stops, and the work done
  so far is saved.
- **Rests:** after each request the GPU rests as long as the request took, so it is busy
  at most half the time.
- **Settings:** change the limits in `.env` with `PDFTR_GPU_PAUSE_AT_C`, `_RESUME_AT_C`,
  `_ABORT_AT_C` and `PDFTR_GPU_REST_RATIO`. `pdftr gpu` shows the hot spot, core, power
  and fan, and `PDFTR_GPU_DEVICE=cpu` keeps the GPU idle during ingest.
- **Power limit (recommended):** a lower power limit cuts heat at the source, and costs
  translation little speed. In an **Administrator** PowerShell:

  ```powershell
  nvidia-smi -pl 150     # watts; this card allows 125-250, default 235; resets at reboot
  # optional: re-apply it at every sign-in
  schtasks /Create /TN "GPU power limit 150W" /TR "nvidia-smi -pl 150" /SC ONLOGON /RL HIGHEST /F
  ```

  If `nvidia-smi` refuses, the Power Limit slider in MSI Afterburner does the same.
  With 150 W, a test run peaked at 76 °C hot spot. At 235 W the hot spot reached 105 °C.

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

## Translation (M3) and R4

- **Engine:** Typhoon Translate 1.5 (4B), running on your GPU through **Ollama**. Ollama is the portable build in `.cache\tools\ollama`, and the model (2.5 GB) is in `.cache\ollama\models`, both on drive D. `pdftr` starts Ollama when it needs it and stops it afterwards, so nothing auto-starts with Windows.
- **Run:** after R1–R3 are complete, run `pdftr translate <doc>`.
  - **Glossary rules:** each request gets the glossary rules for the terms that appear in its text.
  - **Placeholders:** code, paths and URLs are swapped for placeholders, so the model can't change them.
  - **Checks:** each result is checked for dropped code, English left untranslated, odd length, polite particles and missed glossary terms, including Keep English terms that got translated. Problems become flags in R4.
  - **Clean-up:** common Thai spellings of Keep English names (ลินุกซ์ → Linux) are switched back automatically, and text made only of Keep English terms is not sent to the model.
  - **Resuming:** every finished piece is saved at once. Ctrl+C (or a stop to protect the GPU) ends a run, and running the same command again continues it.
- **Translation memory:** identical text is translated once. After a glossary change only the affected text is translated again. Your R4 edits are reused automatically wherever the same English appears.
- **Preview:** `pdftr translate <doc> --pages 15-24` works before R3 too, to judge quality early.
- **GPU safety:** see [GPU safety](#gpu-safety). The R3 **Suggest** and R4 **Re-translate** buttons follow the same limits.
- **R4 · Thai chunks:** on the review page, the left side shows the **Thai page preview** (the right side of the bilingual PDF), or the English page. Each chunk card shows the English, the Thai, where the Thai came from (machine / memory / you / kept in English), any check flags (a glossary flag names its term), and whether the Thai had to shrink or did not fit. You can approve, **Edit Thai** (which also approves it) or **Re-translate** a chunk.
- **Other text on the page:** below the chunk cards, R4 lists the page's headings, running header, contents and table cells, and bookmark titles, each with its Thai. Fix one and press Enter.
  - Your Thai is reused wherever the same English appears.
  - Editing a heading also updates its contents title and bookmark, unless you edited those yourself.
- **`pdftr render-page <doc> <page>`** writes one Thai page as PNG and PDF.
- **Model license:** the Typhoon Translate model card lists Apache-2.0 in its metadata but the Gemma license in its text, plus the OpenTyphoon terms of use. That's fine for this personal tool; check both before any commercial use.

### Claude as the translator (M6)

Typhoon (on your GPU, free) is the default. Claude, through the Anthropic API, translates more accurately: it follows glossary rules and keeps names and code more reliably. It also runs several requests at once and needs no GPU. Set it up once in `.env`:

```ini
PDFTR_TRANSLATE_ENGINE=claude      # or keep typhoon and pass --engine claude per run
ANTHROPIC_API_KEY=sk-ant-...       # or sign in once with `ant auth login` instead
PDFTR_CLAUDE_MODEL=claude-opus-5-5 # default; claude-sonnet-5-5 or claude-haiku-5-5 cost less
PDFTR_CLAUDE_EFFORT=low            # thinking depth per request: low is plenty for translation
PDFTR_CLAUDE_WORKERS=4             # requests at a time
```

```powershell
.venv\Scripts\pdftr translate 1 --engine claude --pages 15-17   # try a few pages first
```

- **Usage report:** each run prints its token use and an estimated cost at list prices.
- **Cost:** a rough estimate for the whole 786-page CompTIA book (about 290,000 words) at list prices:
  - `claude-opus-5-5`: about $40–60;
  - `claude-sonnet-5-5`: about $20–30;
  - `claude-haiku-5-5`: about $2.
- **Refusals:** on Opus and Sonnet, a request the model refuses is retried on a fallback model, which the API does server-side. A text that is still refused stays English and gets the `refused` flag in R4.
- **Switching engines:** everything you approved in R4 is kept. Only unapproved Thai is translated again (`--force` re-translates all, but your own edits still win). Each engine has its own translation memory.
- **Key:** `.env` is never committed, and the key is never printed.
- **Review page:** with Claude configured, the R3 **Suggest** and R4 **Re-translate** buttons use Claude too.

## Bilingual PDF (M4)

```powershell
.venv\Scripts\pdftr render 1                     # English left, Thai right, one sheet per page
.venv\Scripts\pdftr render 1 --layout alternate  # English page, then its Thai page
.venv\Scripts\pdftr render 1 --pages 15-34       # just some pages
```

- **Output:** the PDF goes to `data\output\` unless you pass `--out`. You can re-run `render` any time. Pages whose Thai hasn't changed come from the cache, so a re-run takes seconds; it also tells you how many chunks are still untranslated or unapproved.
- **What stays English:** the cover, title, copyright, index, bibliography and blank pages are shown in English on both sides, and so is any page without Thai yet.
- **Contents pages** are translated title by title and keep their page numbers. Titles that match a heading reuse the heading's Thai, so the contents, the chapter and the bookmarks agree.
- **Navigation:** bookmarks follow the book's outline with Thai titles. Page labels keep the book's numbering (i, ii … 1, 2 …), so "go to page 54" works. In the alternate layout, Thai pages are labelled "54 TH".
- **Copy and search:** Thai text copies out correctly and Ctrl+F finds Thai words, because pdftr repairs the text layer that the drawing engine writes. The page looks exactly the same.

## Embedding and search (M5)

- **R5 · Embedding:** after R4, the review page's R5 step shows, chunk by chunk, exactly what goes into the search index, in English and in Thai. Each entry is the context (section path, list intro, parent item, question) followed by the chunk. Approve it there, then run:

  ```powershell
  .venv\Scripts\pdftr embed 1
  ```

- **What is embedded:**
  - every chunk approved in R1, except tables, Key Terms lists and chunks you kept out;
  - the Thai of every chunk approved in R4;
  - code once, in English only.
- **The model:** BGE-M3 runs on the GPU in small batches under the GPU guard. It downloads once, about 2.2 GB, into `.cache\huggingface`.
- **Re-runs:** a re-run embeds only new or changed text. After later edits, run `pdftr embed` again.
- **Search:** in Thai or English, matching either language. Each result shows its score, book, page and section, with the English and the Thai text.

  ```powershell
  .venv\Scripts\pdftr search "คำสั่งแสดงไฟล์ที่ซ่อน"
  .venv\Scripts\pdftr search "how do I list hidden files" --doc 1 --top 5
  ```

  The review page has a **Search** link (top right) that does the same in the browser. Results link to their page in R4.
- **Power check:** with `PDFTR_GPU_MAX_POWER_W=150` in `.env`, `translate`, `embed` and `ingest` refuse to start while the card's power limit is higher. The limit goes back to 235 W after every reboot.

## Commands

| Command | Does |
|---|---|
| `pdftr translate <doc> [--pages 15-24] [--engine claude] [--force]` | Translate (after R3; any time with `--pages`) |
| `pdftr relocate <folder>` | Point books at their PDFs in another folder (after moving machines) |
| `pdftr ollama status` / `pdftr ollama pull` | Check Ollama / download the translation model |
| `pdftr render <doc> [--layout alternate] [--pages 15-34] [--out file.pdf]` | Build the bilingual PDF |
| `pdftr embed <doc> [--force]` | Embed the reviewed chunks after R5 (only changes on a re-run) |
| `pdftr search "query" [--doc 1] [--top 8] [--lang both\|en\|th]` | Search in Thai or English |
| `pdftr render-page <doc> <page>` | One Thai page as PNG + PDF |
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
