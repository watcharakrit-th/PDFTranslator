"""Profile sample PDFs: structure, fonts, code, bullets, images, tables, columns.

Usage: python analyze_pdfs.py <samples_dir> <out_dir>
Writes <out_dir>/summary.json and renders representative pages as PNG.
"""
import collections
import json
import pathlib
import statistics
import sys

import pymupdf

MONO_HINTS = ("mono", "courier", "consol", "code", "menlo", "inconsolata",
              "lucidaconsole", "firacode", "jetbrains", "typewriter")
BULLETS = set("•▪◦‣∙●○■□➢►▶✓✔❖◆◇·")


def is_mono(font: str) -> bool:
    f = font.lower().replace(" ", "").replace("-", "")
    return any(h in f for h in MONO_HINTS)


def page_profile(page):
    d = page.get_text("dict")
    nchars = 0
    mono_chars = 0
    bullet_lines = 0
    sizes = collections.Counter()
    fonts = collections.Counter()
    blocks = []
    replacement = 0
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        btxt = []
        for line in b["lines"]:
            first = True
            for s in line["spans"]:
                t = s["text"]
                st = t.strip()
                if not st:
                    continue
                nchars += len(st)
                replacement += t.count("�")
                fonts[s["font"]] += len(st)
                sizes[round(s["size"], 1)] += len(st)
                if is_mono(s["font"]):
                    mono_chars += len(st)
                if first and st[:1] in BULLETS:
                    bullet_lines += 1
                first = False
                btxt.append(st)
        if btxt:
            blocks.append(b["bbox"])
    return dict(nchars=nchars, mono_chars=mono_chars, bullet_lines=bullet_lines,
                sizes=sizes, fonts=fonts, blocks=blocks, replacement=replacement)


def two_column_score(blocks, width):
    """Fraction of narrow text blocks starting right of the page middle."""
    narrow = [b for b in blocks if (b[2] - b[0]) < 0.5 * width]
    if len(blocks) < 4:
        return 0.0
    right = [b for b in narrow if b[0] > 0.48 * width]
    return round(len(right) / len(blocks), 2)


def analyze(path: pathlib.Path, outdir: pathlib.Path, short: str):
    doc = pymupdf.open(path)
    n = doc.page_count
    info = {
        "file": path.name,
        "short": short,
        "pages": n,
        "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
        "needs_pass": doc.needs_pass,
    }
    toc = doc.get_toc()
    info["toc_entries"] = len(toc)
    info["toc_levels"] = dict(collections.Counter(t[0] for t in toc))
    info["toc_head"] = [[lvl, title[:70], pg] for lvl, title, pg in toc[:20]]

    sizes = collections.Counter()
    fonts = collections.Counter()
    font_sizes = collections.Counter()
    chars, mono_pages, bullet_pages, img_pages, empty = [], [], [], [], []
    col_scores = []
    replacement = 0
    profiles = {}
    for pno in range(n):
        page = doc[pno]
        r = page.rect
        sizes[(round(r.width), round(r.height))] += 1
        p = page_profile(page)
        profiles[pno] = p
        chars.append(p["nchars"])
        fonts.update(p["fonts"])
        font_sizes.update(p["sizes"])
        replacement += p["replacement"]
        if p["nchars"] < 30:
            empty.append(pno + 1)
        if p["mono_chars"] > 40:
            mono_pages.append(pno + 1)
        if p["bullet_lines"] >= 2:
            bullet_pages.append(pno + 1)
        if page.get_images(full=True):
            img_pages.append(pno + 1)
        if 0.1 * n < pno < 0.9 * n and pno % max(1, n // 40) == 0:
            col_scores.append(two_column_score(p["blocks"], r.width))

    info["page_sizes_pt"] = {f"{w}x{h}": c for (w, h), c in sizes.most_common(5)}
    info["chars_per_page_median"] = int(statistics.median(chars)) if chars else 0
    info["near_empty_pages"] = len(empty)
    info["replacement_chars"] = replacement
    info["top_fonts"] = [[f, c, is_mono(f)] for f, c in fonts.most_common(12)]
    info["top_font_sizes"] = font_sizes.most_common(8)
    info["pages_with_code"] = len(mono_pages)
    info["pages_with_bullets"] = len(bullet_pages)
    info["pages_with_images"] = len(img_pages)
    info["two_column_score_median"] = (statistics.median(col_scores)
                                       if col_scores else None)

    # Tables and vector drawings on a sample of body pages
    sample = [p for p in range(int(0.1 * n), int(0.9 * n), max(1, n // 25))]
    tables_found, table_pages, drawings = 0, [], []
    for pno in sample:
        page = doc[pno]
        try:
            tabs = page.find_tables()
            k = len(tabs.tables)
        except Exception:  # noqa: BLE001 - analysis only
            k = 0
        if k:
            tables_found += k
            table_pages.append(pno + 1)
        drawings.append(len(page.get_drawings()))
    info["table_sample"] = {"pages_checked": len(sample), "tables": tables_found,
                            "pages": table_pages[:10]}
    info["drawings_per_page_median"] = (statistics.median(drawings)
                                        if drawings else 0)

    # Pick representative pages to render
    def mid(lst):
        body = [p for p in lst if 0.15 * n < p < 0.85 * n]
        return body[len(body) // 2] if body else (lst[len(lst) // 2] if lst else None)

    picks = {
        "body": int(n * 0.4) + 1,
        "code": mid(mono_pages),
        "bullets": mid(bullet_pages),
        "image": mid(img_pages),
        "table": table_pages[len(table_pages) // 2] if table_pages else None,
    }
    # TOC page: first page in the first 30 pages with many dotted leaders/numbers
    for pno in range(min(30, n)):
        t = doc[pno].get_text()
        if "contents" in t.lower()[:400]:
            picks["toc"] = pno + 1
            break
    rendered = {}
    seen = set()
    for label, pg in picks.items():
        if not pg or pg in seen:
            continue
        seen.add(pg)
        pix = doc[pg - 1].get_pixmap(dpi=72)
        out = outdir / f"{short}_p{pg:04d}_{label}.png"
        pix.save(out)
        rendered[label] = {"page": pg, "png": str(out)}
    info["rendered"] = rendered

    # Short text sample from the body page (structure only)
    body_pg = picks["body"] - 1
    info["body_text_sample"] = doc[body_pg].get_text()[:700]
    return info


def main():
    samples = pathlib.Path(sys.argv[1])
    outdir = pathlib.Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, pdf in enumerate(sorted(samples.glob("*.pdf"))):
        short = ["A", "B", "C"][i] if i < 3 else f"X{i}"
        if pymupdf.open(pdf).page_count == 0:
            print(f"== {short}: SKIPPED (0 pages, damaged): {pdf.name[:60]}")
            continue
        results.append(analyze(pdf, outdir, short))
    (outdir / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in results:
        print(f"== {r['short']}: {r['file'][:70]}")
        for k in ("pages", "page_sizes_pt", "chars_per_page_median",
                  "near_empty_pages", "replacement_chars", "toc_entries",
                  "toc_levels", "pages_with_code", "pages_with_bullets",
                  "pages_with_images", "two_column_score_median",
                  "table_sample", "drawings_per_page_median", "top_font_sizes"):
            print(f"  {k}: {r[k]}")
        print("  metadata:", {k: r["metadata"][k] for k in r["metadata"]
                              if k in ("creator", "producer", "format")})
        print("  top_fonts:", r["top_fonts"][:8])
        print("  rendered:", {k: v["page"] for k, v in r["rendered"].items()})


if __name__ == "__main__":
    main()
