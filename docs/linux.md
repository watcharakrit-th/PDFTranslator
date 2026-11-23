# Running pdftr on a Linux server

The same code runs on Linux. What changes is where each part runs:

| Part | On a Linux server |
|---|---|
| Database | Docker, same `docker-compose.yml` (PostgreSQL 17 + pgvector on 127.0.0.1:5433) |
| Translation | `claude` engine (Anthropic API, no GPU), or Typhoon through Ollama on an NVIDIA GPU |
| Layout analysis (Docling) and embeddings (BGE-M3) | NVIDIA GPU if there is one, otherwise the CPU (`PDFTR_GPU_DEVICE=cpu`, slower) |
| Review page | Listens on the server's 127.0.0.1; you reach it through an SSH tunnel |

The test suite runs on Ubuntu in GitHub Actions for every pull request (`.github/workflows/tests.yml`), so the Linux path stays tested.

## 1. Requirements

- **System:** Ubuntu 22.04 or 24.04 (or similar), with git, curl and Docker Engine plus the compose plugin.
- **Disk:** about 10 GB for Python, PyTorch and the models, plus your books and the database.
- **Optional NVIDIA GPU:** a driver recent enough for CUDA 12.8 (`nvidia-smi` must work). Without a GPU everything still works: the PyTorch packages install the same way and run on the CPU.

## 2. Install

```bash
git clone https://github.com/watcharakrit-th/PDFTranslator.git
cd PDFTranslator
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, into ~/.local/bin
. scripts/env.sh                                   # caches stay in ./.cache
uv python install 3.12
uv sync
cp .env.example .env                               # then edit it (next section)
.venv/bin/pdftr db up                              # PostgreSQL + pgvector in Docker
.venv/bin/python -m pytest -q                      # optional check: everything passes
```

Source `scripts/env.sh` in every new shell before using `uv` or `pdftr`. It is the Linux counterpart of `scripts/env.ps1`.

## 3. Settings (`.env`)

- **Translation engine:**
  - With Claude: `PDFTR_TRANSLATE_ENGINE=claude` and `ANTHROPIC_API_KEY=...`. Instead of a key you can sign in once with `ant auth login`. The model is `PDFTR_CLAUDE_MODEL` (default `claude-opus-5-5`; `claude-sonnet-5-5` or `claude-haiku-5-5` cost less).
  - With Typhoon on a GPU: install Ollama (its install script also sets up a service). pdftr uses an Ollama that is already running, or starts the `ollama` it finds on the PATH or at `PDFTR_OLLAMA_EXE`.
- **Server without a GPU:** `PDFTR_GPU_DEVICE=cpu`.
- **GPU safety:**
  - **Hot spot:** NVIDIA's hot-spot reading (NVAPI) exists only on Windows. On Linux the guard estimates it as the core temperature plus 25 °C (`PDFTR_GPU_HOTSPOT_MARGIN_C`), and the same limits apply to the estimate.
  - **Power limit:** set it once per boot with `sudo nvidia-smi -pm 1 && sudo nvidia-smi -pl 150`. With `PDFTR_GPU_MAX_POWER_W=150`, GPU commands refuse to start until it is set.

## 4. Move your work from the Windows PC

Your reviews, glossary, translations, translation memory and embeddings all live in the database.

1. On the Windows PC, export it:

   ```powershell
   docker exec pdftr-db pg_dump -U pdftr -Fc pdftr > pdftr.dump
   ```

2. Copy `pdftr.dump` and the book PDFs to the server, for example the PDFs into `~/books`.
3. On the server, load it, then point each book at its new location:

   ```bash
   docker exec -i pdftr-db pg_restore -U pdftr -d pdftr --clean --if-exists < pdftr.dump
   .venv/bin/pdftr relocate ~/books
   ```

`relocate` accepts a PDF only if its content matches what was ingested, so a different edition with the same file name is refused. The `data/` folder holds caches: page images, Thai pages and the layout cache. Copying `data/layout_cache` saves the GPU time of a new layout analysis; everything else is rebuilt on demand.

## 5. Run

- **Long jobs over SSH:** run them inside `tmux`, so they survive a dropped connection. Every finished piece is saved, and running the same command again continues where it stopped.

  ```bash
  tmux new -s pdftr
  .venv/bin/pdftr translate 1          # Ctrl+B then D to detach; `tmux attach -t pdftr` to return
  ```

- **Review page:** start it on the server, then open an SSH tunnel from your PC:

  ```bash
  .venv/bin/pdftr review --no-open                 # on the server
  ssh -N -L 8765:127.0.0.1:8765 you@server         # on your PC; then open http://127.0.0.1:8765
  ```

  The page has no login, so keep it on 127.0.0.1. `--host 0.0.0.0` would let anyone on the network approve or edit your book.
- **Bilingual PDF:** `pdftr render 1` writes it to `data/output/`. Copy it back with `scp you@server:PDFTranslator/data/output/*.pdf .`.

## 6. Backups

The database is the only thing you can't rebuild. A daily dump is enough. For example, in `crontab -e`:

```cron
0 3 * * * docker exec pdftr-db pg_dump -U pdftr -Fc pdftr > $HOME/backups/pdftr-$(date +\%F).dump
```
