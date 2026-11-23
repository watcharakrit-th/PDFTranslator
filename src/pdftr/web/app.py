"""Local review web page: FastAPI, server-rendered HTML, htmx for in-place updates."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pdftr import glossary
from pdftr.db import connect, init_schema
from pdftr.gpu import GpuGuard, GpuTooHot
from pdftr.review import ReviewError, stages
from pdftr.review import chunks as rc
from pdftr.review import terms as rt
from pdftr.review import thai as rth
from pdftr.review.markup import markup_html
from pdftr.settings import get_settings
from pdftr.translate.checks import FLAG_HELP as TR_FLAG_HELP

HERE = Path(__file__).parent
PAGE_ZOOM = 1.6  # page images at ~115 dpi
STEP_URL = {
    "r1_chunks_en": "r1",
    "r2_terms_en": "r2",
    "r3_terms_th": "r3",
    "translate": "r4",
    "r4_chunks_th": "r4",
    "r5_embed": "r5",
    "done": "r5",
}

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["markup"] = lambda s: Markup(markup_html(s or ""))


def _highlight(text: str | None, term: str) -> Markup:
    if not text:
        return Markup("")
    out = str(escape(text))
    words = [re.escape(str(escape(w))) for w in term.split()]
    if words:
        pattern = r"\b" + r"[\s-]+".join(words) + r"(?:s|es)?\b"
        out = re.sub(pattern, lambda m: f"<mark>{m.group(0)}</mark>", out, flags=re.IGNORECASE)
    return Markup(out)


templates.env.filters["highlight"] = _highlight
templates.env.globals["pct"] = lambda v, total: round(100 * float(v) / float(total), 3)


def create_app(
    database_url: str | None = None,
    allowed_hosts: list[str] | None = None,
    engine=None,
    embedder=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if app.state.ollama is not None:  # an Ollama we started for "Re-translate"
            app.state.ollama.stop()
        if getattr(app.state.embedder, "unload", None):  # BGE-M3 loaded for search
            app.state.embedder.unload()

    app = FastAPI(
        title="pdftr review", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.database_url = database_url
    app.state.engine = engine  # tests pass a fake; otherwise started on first use
    app.state.embedder = embedder  # likewise, for search
    app.state.ollama = None
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["127.0.0.1", "localhost"]
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    from pdftr.render.thai_page import FONT_DIR

    app.mount("/fonts", StaticFiles(directory=FONT_DIR), name="fonts")  # same Thai font as the PDF

    @app.middleware("http")
    async def actions_only_from_the_page(request: Request, call_next):
        # Other websites cannot add this header to a cross-site request, so a POST
        # without it did not come from this page (CSRF protection).
        if request.method == "POST" and request.headers.get("HX-Request") != "true":
            return PlainTextResponse("Actions must come from the review page.", status_code=403)
        return await call_next(request)

    @app.exception_handler(ReviewError)
    async def review_error(_request: Request, exc: ReviewError):
        return PlainTextResponse(str(exc), status_code=409)

    with connect(database_url) as conn:  # make sure the schema is current
        init_schema(conn)

    _routes(app)
    _r4_routes(app)
    _r5_routes(app)
    _search_routes(app)
    return app


def _engine(app: FastAPI):
    """The translation engine: Claude, or Typhoon with Ollama started on first use
    (stopped at shutdown)."""
    if app.state.engine is None:
        from pdftr.translate.engine import make_engine
        from pdftr.translate.ollama import OllamaServer

        settings = get_settings()
        if settings.translate_engine == "claude":
            app.state.engine = make_engine(settings, name="claude")
            return app.state.engine
        guard = GpuGuard.from_settings(settings, log=lambda _msg: None)
        if not guard.available:
            raise ReviewError("Cannot read the GPU temperature, so the translator will not run.")
        server = OllamaServer(settings, log=lambda _msg: None)
        server.start()
        app.state.ollama = server
        app.state.engine = make_engine(settings, server.client, guard)
    return app.state.engine


def _api_errors() -> tuple[type[Exception], ...]:
    """Errors of a remote engine (Claude) to show as a message instead of a crash."""
    try:
        import anthropic
    except ImportError:
        return ()
    return (anthropic.APIError,)


def _gpu_cool_enough() -> None:
    """Refuse a GPU request while the card is hot or over its power limit (Typhoon only)."""
    s = get_settings()
    if s.translate_engine == "claude":
        return
    guard = GpuGuard.from_settings(s, log=lambda _msg: None)
    problem = guard.power_problem(s.gpu_max_power_w) if s.gpu_device == "cuda" else None
    if problem:
        raise ReviewError(problem)
    temp = guard.temperature()
    if temp is not None and temp >= s.gpu_pause_at_c:
        raise ReviewError(
            f"The GPU hot spot is at {temp}°C. Wait until it is below "
            f"{s.gpu_resume_at_c}°C, then try again."
        )


@contextmanager
def db(request: Request) -> Iterator[psycopg.Connection]:
    conn = connect(request.app.state.database_url)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _doc(conn: psycopg.Connection, doc_id: int) -> dict:
    doc = conn.execute("SELECT * FROM documents WHERE id=%s", (doc_id,)).fetchone()
    if doc is None:
        raise HTTPException(404, f"Document {doc_id} not found")
    return doc


def _base(request: Request, doc: dict | None, **ctx) -> dict:
    out = {"request": request, "doc": doc, **ctx}
    if doc is not None:
        out["steps"] = stages.step_states(doc["stage"])
    return out


def _refresh() -> Response:
    return Response(status_code=204, headers={"HX-Refresh": "true"})


def _toast_and_refresh(message: str) -> Response:
    return Response(
        status_code=204, headers={"HX-Trigger": json.dumps({"pdftr:toast-reload": message})}
    )


def _routes(app: FastAPI) -> None:
    # ------------------------------------------------------------------ index
    @app.get("/")
    def index(request: Request):
        with db(request) as conn:
            docs = conn.execute(
                """SELECT d.*, count(p.*) AS pages_in, min(p.page_no) AS first_page,
                          max(p.page_no) AS last_page
                   FROM documents d LEFT JOIN pages p ON p.doc_id = d.id
                   GROUP BY d.id ORDER BY d.id"""
            ).fetchall()
            for d in docs:
                d["r1"] = rc.progress(conn, d["id"])
                d["steps"] = stages.step_states(d["stage"])
        return templates.TemplateResponse(request, "index.html", _base(request, None, docs=docs))

    @app.get("/doc/{doc_id}")
    def doc_home(request: Request, doc_id: int):
        with db(request) as conn:
            doc = _doc(conn, doc_id)
        step = STEP_URL.get(doc["stage"], "r3")
        return RedirectResponse(f"/doc/{doc_id}/{step}", status_code=303)

    @app.get("/doc/{doc_id}/page/{page_no}.png")
    def page_image(request: Request, doc_id: int, page_no: int):
        import pymupdf

        path = get_settings().data_dir / f"doc_{doc_id}" / "pages" / f"p{page_no:04d}.png"
        if not path.exists():
            with db(request) as conn:
                doc = _doc(conn, doc_id)
            pdf = pymupdf.open(doc["file_path"])
            if not 1 <= page_no <= pdf.page_count:
                raise HTTPException(404, "No such page")
            path.parent.mkdir(parents=True, exist_ok=True)
            pdf[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(PAGE_ZOOM, PAGE_ZOOM)).save(path)
        return FileResponse(
            path, media_type="image/png", headers={"Cache-Control": "max-age=86400"}
        )

    # --------------------------------------------------------------------- R1
    @app.get("/doc/{doc_id}/r1")
    def r1(request: Request, doc_id: int, page: int | None = None, only: str = "all"):
        with db(request) as conn:
            doc = _doc(conn, doc_id)
            pages = conn.execute(
                "SELECT page_no FROM pages WHERE doc_id=%s ORDER BY page_no", (doc_id,)
            ).fetchall()
            numbers = [p["page_no"] for p in pages]
            if not numbers:
                raise HTTPException(404, "This document has no ingested pages")
            pending = rc.pending_pages(conn, doc_id)
            if page is None or page not in numbers:
                page = pending[0] if pending else numbers[0]
            view = rc.page_view(conn, doc_id, page)
            flagged = rc.flagged_pages(conn, doc_id)
            chapters = rc.chapters(conn, doc_id)
            ctx = _r1_context(conn, doc, view, only)
        i = numbers.index(page)
        current_chapter = next(
            (c["title"] for c in reversed(chapters) if c["first_page"] <= page), None
        )
        return templates.TemplateResponse(
            request,
            "r1.html",
            _base(
                request,
                doc,
                **ctx,
                chapters=chapters,
                current_chapter=current_chapter,
                prev_page=numbers[i - 1] if i > 0 else None,
                next_page=numbers[i + 1] if i + 1 < len(numbers) else None,
                next_flagged=next(
                    (p for p in flagged if p > page), flagged[0] if flagged else None
                ),
                n_flagged_pages=len(flagged),
                next_pending=next(
                    (p for p in pending if p > page), pending[0] if pending else None
                ),
            ),
        )

    def _r1_card(request: Request, conn: psycopg.Connection, doc_id: int, chunk_id: int, page: int):
        doc = _doc(conn, doc_id)
        view = rc.page_view(conn, doc_id, page)
        ctx = _r1_context(conn, doc, view, "all")
        chunk = next((c for c in view["chunks"] if c["id"] == chunk_id), None)
        if chunk is None:
            return _refresh()
        return templates.TemplateResponse(
            request, "_chunk_response.html", _base(request, doc, **ctx, c=chunk)
        )

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/approve")
    def chunk_approve(
        request: Request,
        doc_id: int,
        chunk_id: int,
        page: int = Form(...),
        approved: int = Form(...),
    ):
        with db(request) as conn:
            rc.set_approved(conn, chunk_id, bool(approved))
            return _r1_card(request, conn, doc_id, chunk_id, page)

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/embed")
    def chunk_embed(
        request: Request, doc_id: int, chunk_id: int, page: int = Form(...), embed: int = Form(...)
    ):
        with db(request) as conn:
            rc.set_embed(conn, chunk_id, bool(embed))
            return _r1_card(request, conn, doc_id, chunk_id, page)

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/kind")
    def chunk_kind(
        request: Request, doc_id: int, chunk_id: int, page: int = Form(...), kind: str = Form(...)
    ):
        with db(request) as conn:
            rc.set_kind(conn, chunk_id, kind)
            return _r1_card(request, conn, doc_id, chunk_id, page)

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/edit")
    def chunk_edit(
        request: Request, doc_id: int, chunk_id: int, page: int = Form(...), text: str = Form(...)
    ):
        with db(request) as conn:
            rc.edit_text(conn, chunk_id, text)
            return _r1_card(request, conn, doc_id, chunk_id, page)

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/merge")
    def chunk_merge(request: Request, doc_id: int, chunk_id: int):
        with db(request) as conn:
            rc.merge_with_previous(conn, chunk_id)
        return _refresh()

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/split")
    def chunk_split(request: Request, doc_id: int, chunk_id: int, at: int = Form(...)):
        with db(request) as conn:
            rc.split_chunk(conn, chunk_id, at)
        return _refresh()

    @app.post("/doc/{doc_id}/r1/approve-page")
    def r1_approve_page(request: Request, doc_id: int, page: int = Form(...)):
        with db(request) as conn:
            n = rc.approve_page(conn, doc_id, page)
        return _toast_and_refresh(f"Approved {n} chunks on page {page}")

    @app.post("/doc/{doc_id}/r1/approve-chapter")
    def r1_approve_chapter(request: Request, doc_id: int, page: int = Form(...)):
        with db(request) as conn:
            title, n = rc.approve_chapter(conn, doc_id, page)
        return _toast_and_refresh(f"Approved {n} chunks in “{title}”")

    @app.post("/doc/{doc_id}/r1/approve-all")
    def r1_approve_all(request: Request, doc_id: int):
        with db(request) as conn:
            n = rc.approve_all(conn, doc_id)
        return _toast_and_refresh(f"Approved {n} chunks")

    # ----------------------------------------------------------------- R2 / R3
    @app.get("/doc/{doc_id}/r2")
    def r2(
        request: Request,
        doc_id: int,
        tab: str = "key",
        status: str = "pending",
        q: str = "",
        p: int = 1,
    ):
        return _terms_page(request, doc_id, "r2", tab, status, q, p)

    @app.get("/doc/{doc_id}/r3")
    def r3(request: Request, doc_id: int, status: str = "missing", q: str = "", p: int = 1):
        return _terms_page(request, doc_id, "r3", "all", status, q, p)

    def _terms_page(
        request: Request, doc_id: int, step: str, tab: str, status: str, q: str, p: int
    ):
        with db(request) as conn:
            doc = _doc(conn, doc_id)
            state = next(s["state"] for s in stages.step_states(doc["stage"]) if s["step"] == step)
            if state == "locked":
                return RedirectResponse(f"/doc/{doc_id}", status_code=303)
            data = rt.term_rows(conn, doc_id, tab=tab, step=step, status=status, q=q, page=p)
            counts = rt.tab_counts(conn, doc_id, step)
            blocker = stages.blockers(conn, doc_id, step)
            kept_english = conn.execute(
                """SELECT count(*) AS n FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
                   WHERE dt.doc_id=%s AND t.decision='keep_en'""",
                (doc_id,),
            ).fetchone()["n"]
        return templates.TemplateResponse(
            request,
            f"{step}.html",
            _base(
                request,
                doc,
                step=step,
                tab=tab,
                status=status,
                q=q,
                data=data,
                counts=counts,
                tabs=rt.TABS,
                decisions=rt.DECISION_LABELS,
                blocker=blocker,
                step_state=state,
                kept_english=kept_english,
            ),
        )

    def _term_row(request: Request, conn: psycopg.Connection, doc_id: int, term_id: int, step: str):
        doc = _doc(conn, doc_id)
        t = rt.term_row(conn, doc_id, term_id)
        return templates.TemplateResponse(
            request,
            f"_term_{step}.html",
            _base(request, doc, t=t, decisions=rt.DECISION_LABELS, step=step),
        )

    @app.post("/doc/{doc_id}/terms/{term_id}/decision")
    def term_decision(request: Request, doc_id: int, term_id: int, decision: str = Form(...)):
        with db(request) as conn:
            rt.set_decision(conn, doc_id, term_id, decision)
            return _term_row(request, conn, doc_id, term_id, "r2")

    @app.post("/doc/{doc_id}/terms/{term_id}/rename")
    def term_rename(request: Request, doc_id: int, term_id: int, term_en: str = Form(...)):
        with db(request) as conn:
            rt.rename(conn, doc_id, term_id, term_en)
            return _term_row(request, conn, doc_id, term_id, "r2")

    @app.post("/doc/{doc_id}/terms/{term_id}/approve")
    def term_approve(
        request: Request,
        doc_id: int,
        term_id: int,
        step: str = Form(...),
        approved: int = Form(...),
    ):
        with db(request) as conn:
            if step == "r3":
                rt.approve_th(conn, doc_id, [term_id], bool(approved))
            else:
                rt.approve_en(conn, doc_id, [term_id], bool(approved))
            return _term_row(request, conn, doc_id, term_id, "r3" if step == "r3" else "r2")

    @app.post("/doc/{doc_id}/terms/{term_id}/thai")
    def term_thai(request: Request, doc_id: int, term_id: int, term_th: str = Form("")):
        with db(request) as conn:
            rt.set_thai(conn, doc_id, term_id, term_th)
            return _term_row(request, conn, doc_id, term_id, "r3")

    @app.post("/doc/{doc_id}/terms/approve-shown")
    def terms_approve_shown(
        request: Request, doc_id: int, step: str = Form(...), ids: list[int] = Form(default=[])
    ):
        with db(request) as conn:
            if step == "r3":
                n = rt.approve_th(conn, doc_id, ids)
            else:
                n = rt.approve_en(conn, doc_id, ids)
        return _toast_and_refresh(f"Approved {n} terms")

    @app.post("/doc/{doc_id}/terms/approve-all")
    def terms_approve_all(request: Request, doc_id: int):
        with db(request) as conn:
            n = rt.approve_all_en(conn, doc_id)
        return _toast_and_refresh(f"Approved all {n} pending terms")

    @app.post("/doc/{doc_id}/terms/add")
    def terms_add(
        request: Request, doc_id: int, term_en: str = Form(...), decision: str = Form("keep_en")
    ):
        with db(request) as conn:
            rt.add_term(conn, doc_id, term_en, decision)
        return _toast_and_refresh(f"Added “{term_en.strip()}”")

    @app.post("/doc/{doc_id}/{step}/complete")
    def complete_step(request: Request, doc_id: int, step: str):
        if step not in ("r1", "r2", "r3", "r4", "r5"):
            raise HTTPException(404)
        with db(request) as conn:
            new_stage = stages.complete(conn, doc_id, step)
        target = STEP_URL.get(new_stage)
        url = f"/doc/{doc_id}/{target}" if target else "/"
        return Response(status_code=204, headers={"HX-Redirect": url})

    # ---------------------------------------------------------------- glossary
    @app.get("/glossary.csv")
    def glossary_csv(request: Request):
        with db(request) as conn:
            text = glossary.export_csv(conn)
        return Response(
            "\ufeff" + text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="glossary.csv"'},
        )

    @app.post("/glossary/import")
    async def glossary_import(request: Request, file: UploadFile):
        raw = await file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ReviewError("Save the CSV as UTF-8 (Thai text needs it).") from exc
        try:
            with db(request) as conn:
                result = glossary.import_csv(conn, text)
        except ValueError as exc:
            raise ReviewError(str(exc)) from exc
        return _toast_and_refresh(
            f"Glossary import: {result['added']} added, {result['updated']} updated, "
            f"{result['skipped']} skipped"
        )


def _r1_context(conn: psycopg.Connection, doc: dict, view: dict, only: str) -> dict:
    label_of, chunk_of = {}, {}
    for c in view["chunks"]:
        for bid in c["block_ids"]:
            label_of.setdefault(bid, c["n"])
            chunk_of.setdefault(bid, c["id"])
    for b in view["blocks"]:
        b["n"] = label_of.get(b["id"])
        b["chunk_id"] = chunk_of.get(b["id"])
    return {
        "page": view["page"],
        "blocks": view["blocks"],
        "chunks": view["chunks"],
        "only": only if only in ("all", "flagged", "pending") else "all",
        "prog": rc.progress(conn, doc["id"]),
        "blocker": stages.blockers(conn, doc["id"], "r1"),
        "step": "r1",
        "kinds": rc.KINDS,
        "flag_help": rc.FLAG_HELP,
        "r1_state": next(s["state"] for s in stages.step_states(doc["stage"]) if s["step"] == "r1"),
    }


# ----------------------------------------------------------------------------- R4
def _r4_routes(app: FastAPI) -> None:
    from pdftr.render.preview import preview

    def context(conn: psycopg.Connection, doc: dict, page: int, only: str, view: str) -> dict:
        data = rc.page_view(conn, doc["id"], page)
        version, fits = "", []
        if view == "thai":
            png, fits = preview(conn, doc["id"], page, get_settings().data_dir)
            version = png.stem
        overflow = {f.block_id for f in fits if f.overflow}
        shrunk = {f.block_id for f in fits if f.scale < 0.85 and not f.overflow}
        ctx = _r1_context(conn, doc, data, only)  # box labels and page data
        for c in data["chunks"]:
            c["overflow"] = any(b in overflow for b in c["block_ids"])
            c["shrunk"] = any(b in shrunk for b in c["block_ids"])
            c["th_flagged"] = bool(c["tr_flags"]) or c["overflow"]
        rth.add_glossary_issues(conn, doc["id"], data["chunks"])
        ctx.update(
            others=rth.other_text(conn, doc["id"], page),
            step="r4",
            only=only if only in ("all", "pending", "flagged") else "all",
            view=view if view in ("thai", "english") else "thai",
            preview_version=version,
            prog=rth.progress(conn, doc["id"]),
            blocker=stages.blockers(conn, doc["id"], "r4"),
            r4_state=next(
                s["state"] for s in stages.step_states(doc["stage"]) if s["step"] == "r4"
            ),
            tr_flag_help=TR_FLAG_HELP,
            source_label=rth.TR_SOURCE_LABEL,
        )
        return ctx

    @app.get("/doc/{doc_id}/r4")
    def r4(
        request: Request,
        doc_id: int,
        page: int | None = None,
        only: str = "all",
        view: str = "thai",
    ):
        with db(request) as conn:
            doc = _doc(conn, doc_id)
            state = next(s["state"] for s in stages.step_states(doc["stage"]) if s["step"] == "r4")
            if state == "locked":
                return RedirectResponse(f"/doc/{doc_id}", status_code=303)
            numbers = [
                r["page_no"]
                for r in conn.execute(
                    "SELECT page_no FROM pages WHERE doc_id=%s ORDER BY page_no", (doc_id,)
                )
            ]
            pending = rth.pending_pages(conn, doc_id)
            if page is None or page not in numbers:
                page = pending[0] if pending else numbers[0]
            ctx = context(conn, doc, page, only, view)
            flagged = rth.flagged_pages(conn, doc_id)
            chapters = rc.chapters(conn, doc_id, review_col="review_th")
        i = numbers.index(page)
        current_chapter = next(
            (c["title"] for c in reversed(chapters) if c["first_page"] <= page), None
        )
        return templates.TemplateResponse(
            request,
            "r4.html",
            _base(
                request,
                doc,
                **ctx,
                chapters=chapters,
                current_chapter=current_chapter,
                prev_page=numbers[i - 1] if i > 0 else None,
                next_page=numbers[i + 1] if i + 1 < len(numbers) else None,
                next_flagged=next(
                    (p for p in flagged if p > page), flagged[0] if flagged else None
                ),
                n_flagged_pages=len(flagged),
                next_pending=next(
                    (p for p in pending if p > page), pending[0] if pending else None
                ),
            ),
        )

    @app.get("/doc/{doc_id}/page/{page_no}/thai.png")
    def thai_png(request: Request, doc_id: int, page_no: int):
        with db(request) as conn:
            _doc(conn, doc_id)
            png, _fits = preview(conn, doc_id, page_no, get_settings().data_dir)
        return FileResponse(png, media_type="image/png", headers={"Cache-Control": "no-cache"})

    def card(request: Request, conn: psycopg.Connection, doc_id: int, chunk_id: int, page: int):
        doc = _doc(conn, doc_id)
        ctx = context(conn, doc, page, "all", "thai")
        chunk = next((c for c in ctx["chunks"] if c["id"] == chunk_id), None)
        if chunk is None:
            return _refresh()
        return templates.TemplateResponse(
            request, "_chunk_th_response.html", _base(request, doc, **ctx, c=chunk)
        )

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/th/approve")
    def th_approve(
        request: Request,
        doc_id: int,
        chunk_id: int,
        page: int = Form(...),
        approved: int = Form(...),
    ):
        with db(request) as conn:
            rth.set_approved(conn, chunk_id, bool(approved))
            return card(request, conn, doc_id, chunk_id, page)

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/th/edit")
    def th_edit(request: Request, doc_id: int, chunk_id: int, text: str = Form(...)):
        with db(request) as conn:
            rth.edit_thai(conn, chunk_id, text)
        return _refresh()  # the Thai page preview changes too

    @app.post("/doc/{doc_id}/chunks/{chunk_id}/th/retranslate")
    def th_retranslate(request: Request, doc_id: int, chunk_id: int):
        _gpu_cool_enough()
        engine = _engine(request.app)
        with db(request) as conn:
            try:
                rth.retranslate(conn, chunk_id, engine)
            except GpuTooHot as exc:
                raise ReviewError(f"Stopped to protect the GPU: {exc}. Try again later.") from exc
            except _api_errors() as exc:
                raise ReviewError(f"The translator did not answer: {exc}") from exc
        return _refresh()

    @app.post("/doc/{doc_id}/blocks/{block_id}/th/edit")
    def block_th_edit(request: Request, doc_id: int, block_id: int, text: str = Form(...)):
        with db(request) as conn:
            rth.edit_block_thai(conn, doc_id, block_id, text)
        return _refresh()

    @app.post("/doc/{doc_id}/blocks/{block_id}/cells/th/edit")
    def cell_th_edit(
        request: Request, doc_id: int, block_id: int, pos: str = Form(...), text: str = Form(...)
    ):
        with db(request) as conn:
            rth.edit_cell_thai(conn, doc_id, block_id, pos, text)
        return _refresh()

    @app.post("/doc/{doc_id}/outline/{entry_id}/th/edit")
    def outline_th_edit(request: Request, doc_id: int, entry_id: int, text: str = Form(...)):
        with db(request) as conn:
            rth.edit_outline_thai(conn, doc_id, entry_id, text)
        return _refresh()

    @app.post("/doc/{doc_id}/r4/approve-page")
    def r4_approve_page(request: Request, doc_id: int, page: int = Form(...)):
        with db(request) as conn:
            n = rth.approve_page(conn, doc_id, page)
        return _toast_and_refresh(f"Approved {n} Thai chunks on page {page}")

    @app.post("/doc/{doc_id}/r4/approve-chapter")
    def r4_approve_chapter(request: Request, doc_id: int, page: int = Form(...)):
        with db(request) as conn:
            title, n = rth.approve_chapter(conn, doc_id, page)
        return _toast_and_refresh(f"Approved {n} Thai chunks in “{title}”")

    @app.post("/doc/{doc_id}/r4/approve-all")
    def r4_approve_all(request: Request, doc_id: int):
        with db(request) as conn:
            n = rth.approve_all(conn, doc_id)
        return _toast_and_refresh(f"Approved {n} Thai chunks")

    @app.post("/doc/{doc_id}/terms/suggest-thai")
    def suggest_thai(request: Request, doc_id: int):
        _gpu_cool_enough()
        engine = _engine(request.app)
        with db(request) as conn:
            try:
                n, hot = rt.suggest_thai(conn, doc_id, engine)
            except _api_errors() as exc:
                raise ReviewError(f"The translator did not answer: {exc}") from exc
        if hot:
            return _toast_and_refresh(
                f"Suggested Thai for {n} terms, then stopped to protect the GPU ({hot}). "
                "Please check them."
            )
        return _toast_and_refresh(f"Suggested Thai for {n} terms: please check them")


# ----------------------------------------------------------------------------- R5
def _r5_routes(app: FastAPI) -> None:
    @app.get("/doc/{doc_id}/r5")
    def r5(request: Request, doc_id: int, page: int | None = None):
        from pdftr.embed.runner import plan, summary

        with db(request) as conn:
            doc = _doc(conn, doc_id)
            state = next(s["state"] for s in stages.step_states(doc["stage"]) if s["step"] == "r5")
            if state == "locked":
                return RedirectResponse(f"/doc/{doc_id}", status_code=303)
            model = get_settings().embed_model.split("/")[-1].lower()
            plans = plan(conn, doc_id, model)
            blocker = None if doc["embed_approved_at"] else stages.blockers(conn, doc_id, "r5")
        chapters: dict[str, dict] = {}
        for p in plans:
            ch = chapters.setdefault(
                p.chapter, {"title": p.chapter or "(before the first chapter)", "plans": []}
            )
            ch["plans"].append(p)
        rows = [{"title": ch["title"], **summary(ch["plans"])} for ch in chapters.values()]
        pages = sorted({p.page for p in plans})
        if page not in pages:
            page = pages[0] if pages else None
        i = pages.index(page) if page in pages else 0
        return templates.TemplateResponse(
            request,
            "r5.html",
            _base(
                request,
                doc,
                step="r5",
                counts=summary(plans),
                chapters=rows,
                page=page,
                on_page=[p for p in plans if p.page == page],
                prev_page=pages[i - 1] if i > 0 else None,
                next_page=pages[i + 1] if i + 1 < len(pages) else None,
                blocker=blocker,
            ),
        )


# ------------------------------------------------------------------------- search
def _search_routes(app: FastAPI) -> None:
    @app.get("/search")
    def search_page(request: Request, q: str = "", doc: int | None = None):
        from pdftr.embed.search import search as run_search

        hits, error = [], None
        with db(request) as conn:
            docs = conn.execute("SELECT id, title, file_name FROM documents ORDER BY id").fetchall()
            if q.strip():
                try:
                    hits = run_search(conn, _embedder(request.app), q, doc_id=doc, top=12)
                except Exception as exc:  # noqa: BLE001 - shown on the page instead of a 500
                    error = f"Search failed: {exc}"
        return templates.TemplateResponse(
            request,
            "search.html",
            _base(request, None, q=q, hits=hits, docs=docs, doc_filter=doc, error=error),
        )


def _embedder(app: FastAPI):
    """BGE-M3 for search queries, loaded on first use (a query is a moment of GPU work)."""
    if getattr(app.state, "embedder", None) is None:
        from pdftr.embed.model import make_embedder

        app.state.embedder = make_embedder(get_settings(), log=lambda _m: None)
    return app.state.embedder
