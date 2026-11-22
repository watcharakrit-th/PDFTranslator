"""Layout analysis with Docling, run in small page batches to keep the GPU cool.

Results are cached per batch (data/layout_cache/<file hash>/), so re-running an
ingest after a change to the cutter or term rules does not touch the GPU again.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from pdftr.gpu import GpuGuard
from pdftr.ingest.models import DoclingItem
from pdftr.settings import Settings

CACHE_FORMAT = 1


def build_converter(settings: Settings):
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    device = AcceleratorDevice.CUDA if settings.gpu_device == "cuda" else AcceleratorDevice.CPU
    options = PdfPipelineOptions(
        do_ocr=False,  # born-digital PDFs: the text layer is exact
        do_table_structure=True,
        # small GPU batches = short bursts of load instead of sustained full load
        layout_batch_size=2,
        table_batch_size=2,
        accelerator_options=AcceleratorOptions(device=device, num_threads=4),
    )
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def page_batches(first: int, last: int, size: int) -> list[tuple[int, int]]:
    return [(a, min(a + size - 1, last)) for a in range(first, last + 1, size)]


def _cache_key() -> dict:
    return {"format": CACHE_FORMAT, "docling": version("docling")}


def extract_items(
    pdf_path: Path,
    first: int,
    last: int,
    settings: Settings,
    guard: GpuGuard,
    log: Callable[[str], None] = print,
    cache_dir: Path | None = None,
) -> list[DoclingItem]:
    converter = None
    items: list[DoclingItem] = []
    batches = page_batches(first, last, settings.docling_batch_pages)
    cached = 0
    for i, (a, b) in enumerate(batches, 1):
        cache_file = cache_dir / f"layout_p{a:04d}-{b:04d}.json" if cache_dir else None
        batch = _load(cache_file)
        if batch is not None:
            cached += 1
        else:
            if converter is None:
                converter = build_converter(settings)
            guard.checkpoint()
            started = time.monotonic()
            with guard.watch() as watch:
                result = converter.convert(str(pdf_path), page_range=(a, b))
            log(
                f"  layout {i}/{len(batches)}: pages {a}-{b} in "
                f"{time.monotonic() - started:.1f}s, GPU hot spot peak {watch.peak_c}°C"
            )
            batch = _items_from(result.document)
            if cache_file:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps({"key": _cache_key(), "items": [asdict(it) for it in batch]}),
                    encoding="utf-8",
                )
        for it in batch:
            it.order = len(items)
            items.append(it)
    if cached:
        log(f"  layout: {cached}/{len(batches)} batches from cache (no GPU work)")
    return items


def _load(cache_file: Path | None) -> list[DoclingItem] | None:
    if cache_file is None or not cache_file.exists():
        return None
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    if data.get("key") != _cache_key():
        return None
    out = []
    for d in data["items"]:
        d["bbox"] = tuple(d["bbox"])
        out.append(DoclingItem(**d))
    return out


def _items_from(doc) -> list[DoclingItem]:
    from docling_core.types.doc import TableItem
    from docling_core.types.doc.common.content_layer import ContentLayer

    layers = {ContentLayer.BODY, ContentLayer.FURNITURE}
    items: list[DoclingItem] = []
    for item, _depth in doc.iterate_items(included_content_layers=layers):
        provs = getattr(item, "prov", None) or []
        if not provs:
            continue
        prov = provs[0]
        page_h = doc.pages[prov.page_no].size.height
        bb = prov.bbox.to_top_left_origin(page_h)
        table_md = cells = None
        if isinstance(item, TableItem):
            table_md = item.export_to_markdown(doc)
            cells = []
            for c in item.data.table_cells:
                cb = c.bbox.to_top_left_origin(page_h) if c.bbox is not None else None
                cells.append(
                    {
                        "row": c.start_row_offset_idx,
                        "col": c.start_col_offset_idx,
                        "row_span": c.row_span,
                        "col_span": c.col_span,
                        "header": bool(c.column_header),
                        "text": c.text,
                        "bbox": [cb.l, cb.t, cb.r, cb.b] if cb else None,
                    }
                )
        items.append(
            DoclingItem(
                page_no=prov.page_no,
                label=item.label.value,
                layer=item.content_layer.value,
                bbox=(bb.l, bb.t, bb.r, bb.b),
                text=getattr(item, "text", "") or "",
                order=len(items),
                marker=getattr(item, "marker", None),
                enumerated=getattr(item, "enumerated", None),
                table_md=table_md,
                table_cells=cells,
                code_language=str(getattr(item, "code_language", "") or "") or None,
                multi_prov=len(provs) > 1,
            )
        )
    return items
