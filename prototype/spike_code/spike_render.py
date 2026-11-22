"""Spike: mirror-render one Thai page and build a side-by-side sheet.

Usage: python spike_render.py <pdf> <page_index> <en.json> <th.json> <fonts_dir> <out_dir>
"""
import html
import json
import pathlib
import re
import sys

import pymupdf
from pythainlp.tokenize import word_tokenize

THAI_RUN = re.compile(r"[฀-๿]+")
TAG = re.compile(r"(</?[ckb]>)")
ZWSP = "​"

BASE_CSS = """
@font-face {font-family: th; src: url(IBMPlexSansThai-Regular.ttf);}
@font-face {font-family: th; src: url(IBMPlexSansThai-Bold.ttf); font-weight: bold;}
@font-face {font-family: mono; src: url(IBMPlexMono-Medium.ttf);}
@font-face {font-family: mono; src: url(IBMPlexMono-Bold.ttf); font-weight: bold;}
* {font-family: th;}
body {margin: 0; color: #231f20;}
p {margin: 0;}
code {font-family: mono; font-size: 0.97em;}
.k, .k code {color: #0072bc; font-weight: bold;}
"""


def add_breaks(text: str) -> str:
    def seg(m):
        return ZWSP.join(w for w in word_tokenize(m.group(0), engine="newmm") if w)
    return THAI_RUN.sub(seg, text)


def to_html(markup: str, breaks: bool) -> str:
    out, in_code = [], 0
    for part in TAG.split(markup):
        if part == "<c>":
            out.append("<code>")
            in_code += 1
        elif part == "</c>":
            out.append("</code>")
            in_code -= 1
        elif part == "<k>":
            out.append('<span class="k">')
        elif part == "</k>":
            out.append("</span>")
        elif part in ("<b>", "</b>"):
            out.append(part)
        else:
            t = html.escape(part, quote=False)
            out.append(t if (in_code or not breaks) else add_breaks(t))
    return "".join(out)


def inset_v(r: pymupdf.Rect, frac=0.25) -> pymupdf.Rect:
    h = r.height * frac
    return pymupdf.Rect(r.x0 + 0.3, r.y0 + h, r.x1 - 0.3, r.y1 - h)


def line_pitch(line_rects, size):
    if len(line_rects) > 1:
        return (line_rects[-1][1] - line_rects[0][1]) / (len(line_rects) - 1)
    return size * 1.45


def render(pdf, pno, en, th, fonts_dir, breaks=True):
    src = pymupdf.open(pdf)
    out = pymupdf.open()
    out.insert_pdf(src, from_page=pno, to_page=pno)
    page = out[0]
    archive = pymupdf.Archive(fonts_dir)
    blocks = {b["id"]: b for b in en["blocks"]}
    COL_RIGHT = 506

    # obstacles: every text block + filled boxes (top edges matter)
    obstacles = [pymupdf.Rect(b["bbox"]) for b in en["blocks"]]
    fills = [d["rect"] for d in page.get_drawings()
             if d.get("fill") is not None and d["rect"].width > 20]
    obstacles += fills

    jobs = []  # (rect_to_redact list, insert_rect, html, css)

    # --- paragraphs / captions / notes
    for bid, markup in th["blocks"].items():
        b = blocks[int(bid)]
        rect = pymupdf.Rect(b["bbox"])
        container = next((f for f in fills if f.contains(rect)
                          and f.width < page.rect.width * 0.9), None)
        below = [o.y0 for o in obstacles
                 if o.y0 >= rect.y1 - 1 and o.x0 < rect.x1 and o.x1 > rect.x0
                 and not o.contains(rect)]
        y1 = (min(below) - 3) if below else rect.y1 + 20
        if container:
            y1 = min(y1, container.y1 - 4)
        x1 = max(rect.x1, COL_RIGHT if not container else container.x1 - 4)
        ins = pymupdf.Rect(rect.x0, rect.y0 - 1, x1, y1)
        pitch = line_pitch(b["line_rects"], b["size"])
        lh = max(pitch / b["size"], 1.42)
        css = (BASE_CSS + f"body {{font-size:{b['size']}pt; line-height:{lh:.2f};"
               f" text-indent:{b['first_line_indent']}pt;}}")
        redact = [inset_v(pymupdf.Rect(r)) for r in b["line_rects"]]
        jobs.append((f"block {bid}", redact, ins, to_html(markup, breaks), css))

    # --- running header (keep page number)
    d = page.get_text("dict")
    hdr = next(b for b in d["blocks"] if b["type"] == 0 and b["bbox"][3] < 50)
    spans = [s for ln in hdr["lines"] for s in ln["spans"] if s["text"].strip()]
    title_spans = [s for s in spans if "Montserrat" not in s["font"]]
    num_span = next(s for s in spans if "Montserrat" in s["font"])
    hrect = pymupdf.Rect(title_spans[0]["bbox"])
    for s in title_spans[1:]:
        hrect |= s["bbox"]
    ins = pymupdf.Rect(page.rect.width * 0.35, hrect.y0 - 1,
                       num_span["bbox"][0] - 8, hrect.y1 + 4)
    css = BASE_CSS + "body {font-size:10pt; text-align:right; line-height:1.3;}"
    head_markup = th["header"].replace("บทที่ 7", "<b>บทที่ 7</b>")
    jobs.append(("header", [inset_v(pymupdf.Rect(s["bbox"]), 0.2) for s in title_spans],
                 ins, to_html(head_markup, breaks), css))

    # --- table description cells (column 1)
    cells = {f"r{c['row']}c{c['col']}": c for c in en["table_cells"]}
    for key, markup in th["cells"].items():
        c = cells[key]
        crect = pymupdf.Rect(c["bbox"])
        cd = page.get_text("dict", clip=crect)
        lines = [ln for b2 in cd["blocks"] if b2["type"] == 0 for ln in b2["lines"]
                 if any(s["text"].strip() for s in ln["spans"])]
        first = pymupdf.Rect(lines[0]["bbox"])
        header_row = c["row"] == 0
        color = "#ffffff" if header_row else "#231f20"
        weight = "bold" if header_row else "normal"
        ins = pymupdf.Rect(first.x0, first.y0 - 1.5, crect.x1 - 4, crect.y1 - 1)
        css = (BASE_CSS + f"body {{font-size:8.5pt; line-height:1.35; color:{color};"
               f" font-weight:{weight};}}")
        redact = [inset_v(pymupdf.Rect(ln["bbox"])) for ln in lines]
        jobs.append((key, redact, ins, to_html(markup, breaks), css))

    # 1) remove English text only (keep images and vector graphics)
    for _, redact, *_ in jobs:
        for r in redact:
            page.add_redact_annot(r, fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    # 2) draw Thai text
    report = []
    for name, _, ins, html_text, css in jobs:
        spare, scale = page.insert_htmlbox(ins, html_text, css=css, archive=archive,
                                           scale_low=0.7)
        report.append((name, round(spare, 1), round(scale, 2)))
    return src, out, report


def side_by_side(src, pno, thai_doc):
    w, h = src[pno].rect.width, src[pno].rect.height
    sheet_doc = pymupdf.open()
    sheet = sheet_doc.new_page(width=2 * w, height=h)
    sheet.show_pdf_page(pymupdf.Rect(0, 0, w, h), src, pno)
    sheet.show_pdf_page(pymupdf.Rect(w, 0, 2 * w, h), thai_doc, 0)
    sheet.draw_line((w, 0), (w, h), color=(0.75, 0.75, 0.75), width=0.6)
    return sheet_doc


def main():
    pdf, pno = sys.argv[1], int(sys.argv[2])
    en = json.load(open(sys.argv[3], encoding="utf-8"))
    th = json.load(open(sys.argv[4], encoding="utf-8"))
    fonts_dir, outdir = sys.argv[5], pathlib.Path(sys.argv[6])
    outdir.mkdir(parents=True, exist_ok=True)

    src, thai, report = render(pdf, pno, en, th, fonts_dir, breaks=True)
    print("fit report (name, spare_height, scale):")
    for r in report:
        flag = "  <-- DID NOT FIT" if r[1] < 0 else ("  (shrunk)" if r[2] < 1 else "")
        print("  ", r, flag)
    thai.save(outdir / "spike_thai_page.pdf", garbage=3, deflate=True)
    sheet = side_by_side(src, pno, thai)
    sheet.save(outdir / "spike_sheet.pdf", garbage=3, deflate=True)
    sheet[0].get_pixmap(dpi=80).save(outdir / "spike_sheet.png")
    thai[0].get_pixmap(dpi=130).save(outdir / "spike_thai_page.png")
    clip = pymupdf.Rect(60, 110, 520, 290)
    thai[0].get_pixmap(dpi=260, clip=clip).save(outdir / "spike_zoom_with_breaks.png")

    _, thai_nb, _ = render(pdf, pno, en, th, fonts_dir, breaks=False)
    thai_nb[0].get_pixmap(dpi=260, clip=clip).save(outdir / "spike_zoom_no_breaks.png")
    print("text layer check:", thai[0].get_text()[:160].replace("\n", " | "))
    print("fonts embedded:", sorted({f[3] for f in thai[0].get_fonts()}))


if __name__ == "__main__":
    main()
