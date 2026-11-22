"""pdftr command line."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="English PDF textbook -> bilingual EN/TH PDF + pgvector chunks.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database (PostgreSQL + pgvector in Docker).")
app.add_typer(db_app, name="db")
console = Console()


@app.callback()
def _setup() -> None:
    # Thai and box-drawing characters on the Windows console
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def parse_pages(spec: str | None) -> tuple[int, int]:
    """'1-100' -> (1, 100); '15' -> (15, 15); None -> whole document."""
    if not spec:
        return 1, 10**9
    if "-" in spec:
        a, b = spec.split("-", 1)
        return int(a), int(b) if b else 10**9
    return int(spec), int(spec)


# ------------------------------------------------------------------------------- db
@db_app.command("up")
def db_up() -> None:
    """Start the database container and create the tables."""
    from pdftr.db import compose_up, connect, init_schema

    compose_up()
    with connect() as conn:
        init_schema(conn)
    console.print("[green]Database ready[/] at 127.0.0.1:5433")


@db_app.command("status")
def db_status() -> None:
    """Show database version and row counts."""
    from pdftr.db import connect

    with connect() as conn:
        version = conn.execute("SHOW server_version").fetchone()["server_version"]
        ext = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        table = Table("table", "rows")
        for name in ("documents", "pages", "blocks", "chunks", "terms", "doc_terms", "jobs"):
            n = conn.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"]
            table.add_row(name, str(n))
    console.print(f"PostgreSQL {version}, pgvector {ext['extversion'] if ext else 'missing'}")
    console.print(table)


# ------------------------------------------------------------------------------ gpu
@app.command()
def gpu() -> None:
    """Show GPU temperature and load."""
    from pdftr.gpu import GpuGuard
    from pdftr.settings import get_settings

    guard = GpuGuard.from_settings(get_settings(), log=console.print)
    status = guard.status()
    if status is None:
        console.print("No NVIDIA GPU visible")
        return
    hot = f"{status.hotspot_c}°C" if status.hotspot_c is not None else "not readable"
    console.print(
        f"hot spot {hot}, core {status.temperature_c}°C, load {status.utilization_pct}%, "
        f"{status.power_w:.0f} W (limit {status.power_limit_w:.0f} W), fan {status.fan_pct}%, "
        f"VRAM {status.memory_used_mb}/{status.memory_total_mb} MB"
    )
    console.print(guard.describe())


# --------------------------------------------------------------------------- ingest
@app.command()
def ingest(
    pdf: Path = typer.Argument(..., exists=True, dir_okay=False, help="PDF file"),
    pages: str = typer.Option(None, "--pages", "-p", help="Page range, e.g. 1-100"),
    force: bool = typer.Option(False, help="Delete this document's data and start over"),
    no_db: bool = typer.Option(False, "--no-db", help="Only write the JSON dump"),
) -> None:
    """Extract, cut and collect terms (ready for review step R1)."""
    from pdftr.ingest.pipeline import AlreadyIngested
    from pdftr.ingest.pipeline import ingest as run

    first, last = parse_pages(pages)
    if force and not typer.confirm("--force deletes this document's chunks and reviews. Continue?"):
        raise typer.Abort()
    try:
        summary = run(pdf, first, last, force=force, use_db=not no_db, log=console.print)
    except AlreadyIngested as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from exc
    table = Table("item", "value", show_header=False)
    for key, value in summary.items():
        table.add_row(key, str(value))
    console.print(table)


# --------------------------------------------------------------------------- inspect
@app.command()
def docs() -> None:
    """List documents."""
    from pdftr.db import connect

    with connect() as conn:
        rows = conn.execute(
            """SELECT d.id, d.file_name, d.page_count, d.stage,
                      count(p.*) AS ingested, min(p.page_no) AS first, max(p.page_no) AS last
               FROM documents d LEFT JOIN pages p ON p.doc_id = d.id
               GROUP BY d.id ORDER BY d.id"""
        ).fetchall()
    table = Table("id", "file", "pages", "ingested", "stage")
    for r in rows:
        table.add_row(
            str(r["id"]),
            r["file_name"][:60],
            str(r["page_count"]),
            f"{r['ingested']} ({r['first']}-{r['last']})",
            r["stage"],
        )
    console.print(table)


@app.command()
def stats(doc_id: int) -> None:
    """Counts by page kind, block role, chunk kind and flag."""
    from pdftr.db import connect

    queries = {
        "page kinds": "SELECT kind AS k, count(*) AS n FROM pages WHERE doc_id=%s GROUP BY 1 ORDER BY 2 DESC",
        "block roles": "SELECT role AS k, count(*) AS n FROM blocks WHERE doc_id=%s GROUP BY 1 ORDER BY 2 DESC",
        "chunk kinds": "SELECT kind || CASE WHEN embed THEN '' ELSE ' (not embedded)' END AS k, "
        "count(*) AS n FROM chunks WHERE doc_id=%s GROUP BY 1 ORDER BY 2 DESC",
        "chunk flags": "SELECT unnest(flags) AS k, count(*) AS n FROM chunks WHERE doc_id=%s "
        "GROUP BY 1 ORDER BY 2 DESC",
        "term kinds": "SELECT t.kind AS k, count(*) AS n FROM doc_terms dt JOIN terms t ON t.id=dt.term_id "
        "WHERE dt.doc_id=%s GROUP BY 1 ORDER BY 2 DESC",
    }
    with connect() as conn:
        for title, sql in queries.items():
            table = Table(title, "count")
            for r in conn.execute(sql, (doc_id,)).fetchall():
                table.add_row(str(r["k"]), str(r["n"]))
            console.print(table)


@app.command()
def show(doc_id: int, page: int) -> None:
    """Print the blocks and chunks of one page."""
    from pdftr.db import connect

    with connect() as conn:
        blocks = conn.execute(
            "SELECT id, ord, role, level, translate, flags, text_en FROM blocks "
            "WHERE doc_id=%s AND page_no=%s ORDER BY ord",
            (doc_id, page),
        ).fetchall()
        chunks = conn.execute(
            "SELECT ord, part, kind, embed, flags, context_en, text_en, page_start, page_end "
            "FROM chunks WHERE doc_id=%s AND %s BETWEEN page_start AND page_end ORDER BY ord, part",
            (doc_id, page),
        ).fetchall()
    console.rule(f"page {page}: {len(blocks)} blocks")
    for b in blocks:
        lvl = f" L{b['level']}" if b["level"] is not None else ""
        tr = "" if b["translate"] else " [dim](kept English)[/]"
        console.print(
            f"[bold]{b['ord']}[/] {b['role']}{lvl}{tr} {b['flags'] or ''}: {b['text_en'][:110]!r}"
        )
    console.rule(f"{len(chunks)} chunks")
    for c in chunks:
        span = f"p{c['page_start']}" + (
            f"-{c['page_end']}" if c["page_end"] != c["page_start"] else ""
        )
        emb = "" if c["embed"] else " [dim](not embedded)[/]"
        console.print(
            f"[bold cyan]{c['ord']}.{c['part']}[/] {c['kind']} {span}{emb} {c['flags'] or ''}"
        )
        if c["context_en"]:
            console.print(f"   [dim]{c['context_en']}[/]".replace("\n", " | "))
        console.print(f"   {c['text_en'][:300]}")


@app.command()
def terms(
    doc_id: int, top: int = 40, kind: str = typer.Option(None, help="term|acronym|code")
) -> None:
    """Show collected term candidates (most frequent first)."""
    from pdftr.db import connect

    with connect() as conn:
        rows = conn.execute(
            """SELECT t.term_en, t.kind, t.decision, dt.freq, dt.first_page, dt.sources, dt.example
               FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
               WHERE dt.doc_id = %s AND (%s::text IS NULL OR t.kind = %s)
               ORDER BY dt.freq DESC, t.term_en LIMIT %s""",
            (doc_id, kind, kind, top),
        ).fetchall()
    table = Table("term", "kind", "freq", "page", "sources", "example")
    for r in rows:
        table.add_row(
            r["term_en"],
            r["kind"],
            str(r["freq"]),
            str(r["first_page"]),
            ",".join(r["sources"]),
            (r["example"] or "")[:60],
        )
    console.print(table)


ROLE_COLORS = {
    "heading": (0.85, 0.1, 0.1),
    "paragraph": (0.1, 0.35, 0.9),
    "list_item": (0.0, 0.6, 0.2),
    "callout": (0.6, 0.2, 0.8),
    "caption": (0.95, 0.5, 0.0),
    "footnote": (0.95, 0.5, 0.0),
    "code": (0.35, 0.35, 0.35),
    "table": (0.0, 0.6, 0.6),
    "toc": (0.5, 0.3, 0.1),
    "picture": (0.75, 0.75, 0.0),
    "formula": (0.75, 0.75, 0.0),
    "header": (0.65, 0.65, 0.65),
    "footer": (0.65, 0.65, 0.65),
    "other": (1.0, 0.0, 1.0),
}


@app.command("debug-pages")
def debug_pages(doc_id: int, pages: str = typer.Option(..., "--pages", "-p")) -> None:
    """Draw block boxes and chunk numbers on page images (data/doc_<id>/debug/)."""
    import pymupdf

    from pdftr.db import connect
    from pdftr.settings import get_settings

    first, last = parse_pages(pages)
    out_dir = get_settings().data_dir / f"doc_{doc_id}" / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        path = conn.execute("SELECT file_path FROM documents WHERE id=%s", (doc_id,)).fetchone()[
            "file_path"
        ]
        chunk_rows = conn.execute(
            "SELECT ord, part, kind, embed, block_ids FROM chunks WHERE doc_id=%s ORDER BY ord, part",
            (doc_id,),
        ).fetchall()
        label_of: dict[int, str] = {}
        for n, c in enumerate(chunk_rows, 1):
            for bid in c["block_ids"]:
                label_of.setdefault(bid, f"#{n}{'' if c['embed'] else '*'}")
        pdf = pymupdf.open(path)
        written = []
        for p in range(first, min(last, pdf.page_count) + 1):
            rows = conn.execute(
                "SELECT id, role, x0, y0, x1, y1 FROM blocks WHERE doc_id=%s AND "
                "page_no=%s ORDER BY ord",
                (doc_id, p),
            ).fetchall()
            page = pdf[p - 1]
            for r in rows:
                color = ROLE_COLORS.get(r["role"], (1, 0, 1))
                rect = pymupdf.Rect(r["x0"], r["y0"], r["x1"], r["y1"])
                page.draw_rect(rect, color=color, width=0.9)
                tag = f"{r['role']} {label_of.get(r['id'], '')}".strip()
                page.insert_text(
                    (rect.x0 + 1, max(6, rect.y0 - 1.5)), tag, fontsize=5.5, color=color
                )
            out = out_dir / f"p{p:04d}.png"
            page.get_pixmap(dpi=90).save(out)
            written.append(out)
    console.print(
        f"Wrote {len(written)} images to {out_dir}  (#n = chunk number, * = not embedded)"
    )


# --------------------------------------------------------------------------- review
LOCAL_HOSTS = ("127.0.0.1", "localhost")


@app.command()
def review(
    host: str = typer.Option("127.0.0.1", help="Address to listen on (default: this PC only)"),
    port: int = typer.Option(8765, help="Port"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser"),
) -> None:
    """Open the review web page (R1 English chunks, R2 English terms, R3 Thai terms)."""
    import threading
    import webbrowser

    import uvicorn

    from pdftr.web.app import create_app

    local = host in LOCAL_HOSTS
    if not local:
        console.print(
            "[yellow]Warning:[/] the review page has no login. Expose it only on a "
            "trusted network, or keep it on 127.0.0.1 and use an SSH tunnel."
        )
    url = f"http://{'127.0.0.1' if local or host == '0.0.0.0' else host}:{port}/"
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    console.print(f"Review page: {url}   (Ctrl+C to stop)")
    web = create_app(allowed_hosts=list(LOCAL_HOSTS) if local else ["*"])
    uvicorn.run(web, host=host, port=port, log_level="warning")


# ------------------------------------------------------------------------- glossary
glossary_app = typer.Typer(
    no_args_is_help=True, help="The shared glossary (English terms and their Thai)."
)
app.add_typer(glossary_app, name="glossary")


@glossary_app.command("export")
def glossary_export(path: Path = typer.Argument(..., help="CSV file to write")) -> None:
    """Write the glossary to a CSV file (opens in Excel; UTF-8 for Thai)."""
    from pdftr.db import connect
    from pdftr.glossary import export_csv

    with connect() as conn:
        text = export_csv(conn)
    path.write_text(text, encoding="utf-8-sig")
    console.print(f"Wrote {text.count(chr(10)) - 1} terms to {path}")


@glossary_app.command("import")
def glossary_import(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="CSV file to read"),
) -> None:
    """Import a CSV (term_en, decision, term_th); its rows override and count as reviewed."""
    from pdftr.db import connect
    from pdftr.glossary import import_csv

    with connect() as conn:
        result = import_csv(conn, path.read_text(encoding="utf-8-sig"))
    console.print(
        f"{result['added']} added, {result['updated']} updated, {result['skipped']} skipped"
    )


@glossary_app.command("seed")
def glossary_seed() -> None:
    """Re-apply the built-in seed list to collected terms nobody has reviewed yet."""
    from pdftr.db import connect
    from pdftr.glossary import apply_seed

    with connect() as conn:
        n = apply_seed(conn)
    console.print(f"Re-applied defaults to {n} unreviewed terms")


# ---------------------------------------------------------------------- translation
ollama_app = typer.Typer(no_args_is_help=True, help="The local translation server (Ollama).")
app.add_typer(ollama_app, name="ollama")


@ollama_app.command("status")
def ollama_status() -> None:
    """Is Ollama running, and is the translation model installed?"""
    from pdftr.settings import get_settings
    from pdftr.translate.ollama import OllamaClient

    s = get_settings()
    client = OllamaClient(s.ollama_url)
    if not client.alive():
        console.print(f"Ollama is not running at {s.ollama_url} (pdftr starts it when needed)")
        return
    installed = client.has_model(s.translate_model)
    console.print(
        f"Ollama {client.version()} at {s.ollama_url}; model {s.translate_model}: "
        + ("installed" if installed else "missing (run: pdftr ollama pull)")
    )


@ollama_app.command("pull")
def ollama_pull() -> None:
    """Download the translation model (about 2.5 GB, into .cache/ollama)."""
    from pdftr.settings import configure_environment, get_settings
    from pdftr.translate.ollama import OllamaServer

    s = get_settings()
    configure_environment(s)
    with OllamaServer(s, log=console.print) as client:
        client.pull(s.translate_model, progress=lambda msg: console.print(f"  {msg}"))


@app.command()
def translate(
    doc_id: int,
    pages: str = typer.Option(
        None, "--pages", "-p", help="Only these pages, e.g. 15-24 (also works before R3)"
    ),
    force: bool = typer.Option(False, help="Translate again even where nothing changed"),
) -> None:
    """Translate a document after R3. Resumable (Ctrl+C keeps what is done); keeps the GPU cool."""
    from pdftr.db import connect, init_schema
    from pdftr.gpu import GpuGuard, GpuTooHot
    from pdftr.settings import configure_environment, get_settings
    from pdftr.translate.engine import make_engine
    from pdftr.translate.ollama import OllamaServer
    from pdftr.translate.runner import translate_document

    s = get_settings()
    configure_environment(s)
    first, last = parse_pages(pages)
    with connect() as conn:
        init_schema(conn)
        doc = conn.execute("SELECT stage FROM documents WHERE id=%s", (doc_id,)).fetchone()
        if doc is None:
            console.print(f"[red]No document {doc_id}[/]")
            raise typer.Exit(1)
        before_r3 = doc["stage"] in ("r1_chunks_en", "r2_terms_en", "r3_terms_th")
        if before_r3 and not pages:
            console.print(
                "[yellow]Finish R1–R3 first[/] so the glossary is final, or try a "
                "few pages now with --pages 15-24."
            )
            raise typer.Exit(1)
        if before_r3:
            console.print(
                "[yellow]Preview:[/] the glossary is not final yet. Pages translated "
                "now are re-translated automatically where the glossary changes."
            )
        guard = GpuGuard.from_settings(s, log=console.print)
        if not guard.available:
            console.print("[red]Cannot read the GPU temperature[/], so translation will not run.")
            raise typer.Exit(1)
        console.print(guard.describe())
        try:
            with OllamaServer(s, log=console.print) as client:
                if not client.has_model(s.translate_model):
                    console.print("Downloading the translation model (about 2.5 GB)...")
                    client.pull(s.translate_model, progress=lambda _msg: None)
                engine = make_engine(s, client, guard)
                try:
                    summary = translate_document(
                        conn,
                        doc_id,
                        engine,
                        first=first,
                        last=last,
                        force=force,
                        guard=guard,
                        log=console.print,
                    )
                finally:
                    client.unload(s.translate_model)  # free the GPU now
        except KeyboardInterrupt:
            console.print(
                "[yellow]Stopped.[/] Finished pieces are saved; run the same "
                "command again to continue."
            )
            raise typer.Exit(130) from None
        except GpuTooHot as exc:
            console.print(
                f"[red]Stopped to protect the GPU:[/] {exc}, even on a short piece after "
                "cooling down. Finished pieces are saved. Lower the GPU power limit "
                "(README: GPU safety) before running again."
            )
            raise typer.Exit(3) from None
    table = Table("item", "value", show_header=False)
    for key, value in summary.items():
        table.add_row(key, str(value))
    console.print(table)


@app.command("render-page")
def render_page(doc_id: int, page: int) -> None:
    """Draw the Thai version of one page (PNG + PDF in data/doc_<id>/thai/)."""
    from pdftr.db import connect
    from pdftr.render.preview import page_blocks
    from pdftr.render.thai_page import render_thai_page
    from pdftr.settings import configure_environment, get_settings

    s = get_settings()
    configure_environment(s)
    with connect() as conn:
        doc = conn.execute("SELECT file_path FROM documents WHERE id=%s", (doc_id,)).fetchone()
        blocks = page_blocks(conn, doc_id, page)
    rendered, fits = render_thai_page(doc["file_path"], page, blocks)
    out = s.data_dir / f"doc_{doc_id}" / "thai"
    out.mkdir(parents=True, exist_ok=True)
    pdf_path, png_path = out / f"page{page:04d}.pdf", out / f"page{page:04d}.png"
    rendered.save(pdf_path, garbage=3, deflate=True)
    rendered[0].get_pixmap(dpi=130).save(png_path)
    shrunk = sum(1 for f in fits if f.scale < 1 and not f.overflow)
    overflow = sum(1 for f in fits if f.overflow)
    console.print(
        f"Wrote {png_path} and {pdf_path}: {len(fits)} text areas, "
        f"{shrunk} shrunk to fit, {overflow} do not fit"
    )
