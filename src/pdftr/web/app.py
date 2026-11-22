"""Local review web page: FastAPI, server-rendered HTML, htmx for in-place updates."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
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
from pdftr.review import ReviewError, stages
from pdftr.review import chunks as rc
from pdftr.review import terms as rt
from pdftr.review.markup import markup_html
from pdftr.settings import get_settings

HERE = Path(__file__).parent
PAGE_ZOOM = 1.6  # page images at ~115 dpi
STEP_URL = {"r1_chunks_en": "r1", "r2_terms_en": "r2", "r3_terms_th": "r3"}

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


def create_app(database_url: str | None = None, allowed_hosts: list[str] | None = None) -> FastAPI:
    app = FastAPI(title="pdftr review", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database_url = database_url
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["127.0.0.1", "localhost"]
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

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
    return app


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

    @app.post("/doc/{doc_id}/terms/add")
    def terms_add(
        request: Request, doc_id: int, term_en: str = Form(...), decision: str = Form("keep_en")
    ):
        with db(request) as conn:
            rt.add_term(conn, doc_id, term_en, decision)
        return _toast_and_refresh(f"Added “{term_en.strip()}”")

    @app.post("/doc/{doc_id}/{step}/complete")
    def complete_step(request: Request, doc_id: int, step: str):
        if step not in ("r1", "r2", "r3"):
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
