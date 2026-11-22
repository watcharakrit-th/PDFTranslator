# pdftr: English textbook PDF → bilingual EN/TH PDF + pgvector

See [SPEC.md](SPEC.md) for the design and milestones. Status: **M1 (ingest) done.**

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

## Commands (M1)

| Command | Does |
|---|---|
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
