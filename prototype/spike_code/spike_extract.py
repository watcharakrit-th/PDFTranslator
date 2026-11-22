"""Spike: extract translatable blocks of one page as tagged markup.

Tags: <c>inline code</c>, <k>key term (blue bold)</k>.
Usage: python spike_extract.py <pdf> <page_index> <out.json>
"""
import json
import sys

import pymupdf

KEY_COLOR = 0x0072BC


def span_kind(s):
    if "SourceCode" in s["font"]:
        return "c"
    if s["color"] == KEY_COLOR and ("Bold" in s["font"] or s["flags"] & 16):
        return "k"
    return "t"


def to_markup(spans):
    out, cur, buf = [], None, []
    for s in spans:
        k = span_kind(s)
        if k != cur:
            if buf:
                out.append((cur, "".join(buf)))
            cur, buf = k, []
        buf.append(s["text"])
    if buf:
        out.append((cur, "".join(buf)))
    parts = []
    for k, t in out:
        parts.append(t if k == "t" else f"<{k}>{t}</{k}>")
    return "".join(parts).replace("</c><c>", "").replace("</k><k>", "")


def main():
    pdf, pno, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    page = pymupdf.open(pdf)[pno]
    d = page.get_text("dict")
    tables = page.find_tables().tables
    table_rect = pymupdf.Rect(tables[0].bbox) if tables else None
    blocks = []
    for i, b in enumerate(d["blocks"]):
        if b["type"] != 0:
            continue
        lines = b["lines"]
        spans = [s for ln in lines for s in ln["spans"] if s["text"].strip()]
        if not spans:
            continue
        rect = pymupdf.Rect(b["bbox"])
        fonts = {s["font"] for s in spans}
        sizes = sorted({round(s["size"], 1) for s in spans})
        role = "paragraph"
        if all("SourceCode" in f for f in fonts):
            role = "code"
        elif sizes[0] <= 5.5:
            role = "footer"
        elif rect.y1 < 50:
            role = "header"
        elif max(sizes) >= 14:
            role = "heading"
        elif table_rect and rect.intersects(table_rect):
            role = "table_row"
        # join lines with a space; hyphenated line ends are kept as-is here
        line_texts = []
        for ln in lines:
            ls = [s for s in ln["spans"] if s["text"].strip() or s["text"] == " "]
            line_texts.append(to_markup(ls))
        first_x = lines[0]["bbox"][0]
        blocks.append({
            "id": i,
            "role": role,
            "bbox": [round(x, 1) for x in b["bbox"]],
            "first_line_indent": round(first_x - b["bbox"][0], 1),
            "line_height": round((b["bbox"][3] - b["bbox"][1]) / max(1, len(lines)), 1),
            "size": max(sizes, key=lambda z: sum(len(s["text"]) for s in spans
                                                 if round(s["size"], 1) == z)),
            "color": f"#{max(spans, key=lambda s: len(s['text']))['color']:06x}",
            "text": " ".join(t.strip() for t in line_texts),
            "line_rects": [[round(x, 1) for x in ln["bbox"]] for ln in lines],
        })
    cells = []
    if tables:
        t = tables[0]
        for r, row in enumerate(t.rows):
            for c, cell in enumerate(row.cells):
                if cell:
                    cells.append({"row": r, "col": c,
                                  "bbox": [round(x, 1) for x in cell],
                                  "text": page.get_textbox(pymupdf.Rect(cell)).strip()})
    json.dump({"page_rect": list(page.rect), "blocks": blocks, "table_cells": cells},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for b in blocks:
        print(f"#{b['id']} [{b['role']}] indent={b['first_line_indent']} "
              f"lh={b['line_height']} size={b['size']} color={b['color']}")
        print("   ", b["text"])
    print("--- table cells")
    for c in cells:
        print(f"  r{c['row']}c{c['col']} {c['bbox']}: {c['text']!r}")


if __name__ == "__main__":
    main()
