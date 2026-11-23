"""Make the Thai on rendered pages copyable and searchable.

MuPDF draws Thai correctly (HarfBuzz shaping), but the text layer it writes has
three problems that show up when a reader copies or searches the text:

1. Mark variants such as ``uni0E48.small`` map to their glyph number ("ทีǪ").
2. Sara am (ำ) is drawn as nikhahit + sara aa, so it copies as two characters.
3. The zero-width spaces that let lines break between Thai words are written as
   space glyphs cancelled by a kerning step, so every word copies with a space.

``fix_text_layer`` repairs a rendered page in place. It rewrites the ToUnicode
maps of the IBM Plex fonts from their glyph names, removes the cancelled spaces,
and tags each sara am with /ActualText: "ำ" on the nikhahit glyph and nothing on
the sara aa glyph. The marks are tagged one glyph each because MuPDF hands out
ActualText one character per glyph. The page looks exactly the same.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).parent / "fonts"
FACES = {
    ("sans", False): "IBMPlexSansThai-Regular.ttf",
    ("sans", True): "IBMPlexSansThai-Bold.ttf",
    ("mono", False): "IBMPlexMono-Medium.ttf",
    ("mono", True): "IBMPlexMono-Bold.ttf",
}
SARA_AM = chr(0x0E33)
# one text operation: a font change, a TJ array or a single Tj string
OP_RE = re.compile(rb"(/[^\s/<>\[\]()]+)\s+[-+.\d]+\s+Tf|\[([^\]]*)\]\s*TJ|<([0-9A-Fa-f]*)>\s*Tj")
ITEM_RE = re.compile(rb"<([0-9A-Fa-f]*)>|([-+]?(?:\d+\.?\d*|\.\d+))")
REF_RE = re.compile(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R")


@dataclass
class FaceInfo:
    space_gid: int
    space_width: float  # in TJ units (1/1000 of the font size)
    nikhahit: set[int] = field(default_factory=set)
    sara_aa: set[int] = field(default_factory=set)
    cmap: bytes = b""


@dataclass
class Report:
    fonts: int = 0
    spaces: int = 0
    sara_am: int = 0


def _unicode_of(name: str, by_cmap: dict[str, int]) -> str:
    """The text a glyph stands for: from its name first ("uni0E48.small" -> U+0E48,
    "uni0E310E4A" -> two characters), then the font's cmap, then the glyph list.
    Latin ligatures copy as separate letters ("fi", not U+FB01), so search finds them."""
    text = _glyph_text(name, by_cmap)
    return "".join(
        unicodedata.normalize("NFKC", ch) if 0xFB00 <= ord(ch) <= 0xFB06 else ch for ch in text
    )


def _glyph_text(name: str, by_cmap: dict[str, int]) -> str:
    from fontTools import agl

    base = name.split(".")[0]
    m = re.fullmatch(r"uni((?:[0-9A-F]{4})+)", base)
    if m:
        hexs = m.group(1)
        return "".join(chr(int(hexs[i : i + 4], 16)) for i in range(0, len(hexs), 4))
    m = re.fullmatch(r"u([0-9A-F]{4,6})", base)
    if m:
        return chr(int(m.group(1), 16))
    cp = by_cmap.get(name)
    if cp is not None and not 0xE000 <= cp <= 0xF8FF:  # skip private-use forms
        return chr(cp)
    return agl.toUnicode(base) if base and base != ".notdef" else ""


@lru_cache(maxsize=4)
def face_info(file_name: str) -> FaceInfo:
    from fontTools.ttLib import TTFont

    font = TTFont(str(FONT_DIR / file_name))
    order = font.getGlyphOrder()
    by_cmap: dict[str, int] = {}
    for cp, name in sorted(font.getBestCmap().items()):
        by_cmap.setdefault(name, cp)
    units = font["head"].unitsPerEm
    space = next((n for n, cp in by_cmap.items() if cp == 0x20), "space")
    info = FaceInfo(
        space_gid=order.index(space),
        space_width=font["hmtx"][space][0] * 1000 / units,
    )
    entries = []
    for gid, name in enumerate(order):
        text = _unicode_of(name, by_cmap)
        if name.split(".")[0] == "uni0E4D":
            info.nikhahit.add(gid)
        elif name.split(".")[0] == "uni0E32":
            info.sara_aa.add(gid)
        if text:
            entries.append(f"<{gid:04X}> <{text.encode('utf-16-be').hex().upper()}>")
    blocks = [entries[i : i + 100] for i in range(0, len(entries), 100)]
    body = "".join(f"{len(b)} beginbfchar\n" + "\n".join(b) + "\nendbfchar\n" for b in blocks)
    info.cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{body}endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")
    return info


def _face_of(base_font: str) -> str | None:
    name = re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), base_font)
    if "Plex" not in name:
        return None
    kind = "mono" if "Mono" in name else "sans" if "Thai" in name else None
    return FACES.get((kind, "Bold" in name)) if kind else None


def _fonts_of(doc, xref: int) -> dict[bytes, str]:
    """Resource name -> font file, for the IBM Plex fonts a form uses."""
    kind, value = doc.xref_get_key(xref, "Resources/Font")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "dict":
        return {}
    fonts = {}
    for name, ref in REF_RE.findall(value):
        face = _face_of(doc.xref_get_key(int(ref), "BaseFont")[1])
        if face:
            fonts[b"/" + name.encode()] = face
    return fonts


def _parse(body: bytes) -> list[tuple[str, float]]:
    """A TJ array as ("g", gid) and ("n", adjustment) items."""
    items: list[tuple[str, float]] = []
    for hexs, num in ITEM_RE.findall(body):
        if num:
            items.append(("n", float(num)))
        else:
            hexs = re.sub(rb"\s+", b"", hexs)
            items += [("g", int(hexs[i : i + 4], 16)) for i in range(0, len(hexs), 4)]
    return items


def _drop_spaces(items: list[tuple[str, float]], info: FaceInfo) -> tuple[list, int]:
    """Remove space glyphs that the next kerning step cancels (zero-width spaces)."""
    out, dropped, i = [], 0, 0
    while i < len(items):
        kind, value = items[i]
        nxt = items[i + 1] if i + 1 < len(items) else None
        if (
            kind == "g"
            and value == info.space_gid
            and nxt is not None
            and nxt[0] == "n"
            and nxt[1] >= 0.8 * info.space_width
        ):
            rest = nxt[1] - info.space_width
            if abs(rest) > 0.01:
                out.append(("n", rest))
            dropped += 1
            i += 2
            continue
        out.append(items[i])
        i += 1
    return out, dropped


def _fmt(number: float) -> bytes:
    return (f"{number:.4f}".rstrip("0").rstrip(".") or "0").encode()


def _tj(items: list[tuple[str, float]]) -> bytes:
    if not items:
        return b""
    out = b"["
    for kind, value in items:
        out += b"<%04x>" % int(value) if kind == "g" else _fmt(value)
    return out + b"]TJ "


def _actual(text: str, gid: int) -> bytes:
    hexs = text.encode("utf-16-be").hex().upper().encode() if text else b""
    return (
        b"/Span <</ActualText <"
        + (b"FEFF" + hexs if text else b"")
        + b">>> BDC [<%04x>]TJ EMC " % gid
    )


def fix_form(stream: bytes, fonts: dict[bytes, str], report: Report) -> bytes:
    """Fix one content stream that draws with ``fonts`` (resource name -> font file)."""
    if b"/ActualText" in stream:  # fixed already
        return stream
    ops = list(OP_RE.finditer(stream))
    current: str | None = None
    parsed: list[tuple[re.Match, str | None, list]] = []
    for m in ops:
        if m.group(1) is not None:
            current = fonts.get(m.group(1))
            parsed.append((m, None, []))
            continue
        if current is None:
            parsed.append((m, None, []))
            continue
        info = face_info(current)
        body = m.group(2) if m.group(2) is not None else b"<" + m.group(3) + b">"
        items, dropped = _drop_spaces(_parse(body), info)
        report.spaces += dropped
        parsed.append((m, current, items))
    # sara am: a nikhahit glyph followed by a sara aa glyph, also across operations
    marks: dict[tuple[int, int], str] = {}
    previous: tuple[int, int, int, str] | None = None  # (op index, item index, gid, face)
    for oi, (_m, face, items) in enumerate(parsed):
        if face is None:
            continue
        info = face_info(face)
        for ii, (kind, value) in enumerate(items):
            if kind != "g":
                continue
            gid = int(value)
            if (
                previous
                and previous[3] == face
                and previous[2] in info.nikhahit
                and gid in info.sara_aa
            ):
                marks[(previous[0], previous[1])] = SARA_AM
                marks[(oi, ii)] = ""
                report.sara_am += 1
            previous = (oi, ii, gid, face)
    out, pos = b"", 0
    for oi, (m, face, items) in enumerate(parsed):
        if face is None:
            continue
        out += stream[pos : m.start()]
        plain: list[tuple[str, float]] = []
        rebuilt = b""
        for ii, item in enumerate(items):
            if (oi, ii) in marks:
                rebuilt += _tj(plain) + _actual(marks[(oi, ii)], int(item[1]))
                plain = []
            else:
                plain.append(item)
        out += rebuilt + _tj(plain)
        pos = m.end()
    return out + stream[pos:]


def fix_text_layer(doc) -> Report:
    """Repair the text layer of every form that MuPDF's HTML layout drew (in place)."""
    report = Report()
    for xref in range(1, doc.xref_length()):
        subtype = doc.xref_get_key(xref, "Subtype")[1]
        if subtype == "/Type0":
            face = _face_of(doc.xref_get_key(xref, "BaseFont")[1])
            tu = doc.xref_get_key(xref, "ToUnicode")
            if face and tu[0] == "xref":
                doc.update_stream(int(tu[1].split()[0]), face_info(face).cmap)
                report.fonts += 1
        elif subtype == "/Form":
            fonts = _fonts_of(doc, xref)
            if fonts:
                stream = doc.xref_stream(xref) or b""
                fixed = fix_form(stream, fonts, report)
                if fixed != stream:
                    doc.update_stream(xref, fixed)
    return report
