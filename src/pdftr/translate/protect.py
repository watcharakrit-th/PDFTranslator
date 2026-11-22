"""Keep what must not be translated out of the model's reach, and rebuild markup after.

Before translation:
- inline code (<c>...</c>), URLs, e-mail addresses, domains and file paths become
  numbered placeholders that the model copies unchanged;
- key-term (<k>) and bold (<b>) tags are removed but remembered.
After translation the placeholders become the original markup again, and key terms
and a leading label ("Note 1" -> "หมายเหตุ 1") are tagged again.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from pdftr.ingest.terms import normalize
from pdftr.ingest.textutil import strip_markup

PH_OPEN, PH_CLOSE = chr(0x27E6), chr(0x27E7)  # mathematical white square brackets
PH_RE = re.compile(re.escape(PH_OPEN) + r"\s*(\d+)\s*" + re.escape(PH_CLOSE))

CODE_RE = re.compile(r"<c>.*?</c>", re.DOTALL)
TAGGED_SPAN_RE = re.compile(r"<([ckb])>.*?</\1>", re.DOTALL)
KEY_RE = re.compile(r"<k>(.*?)</k>", re.DOTALL)
LEAD_BOLD_RE = re.compile(r"^\s*<b>(.*?)</b>", re.DOTALL)
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>()]+[^\s<>().,;:!?'\"]")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
DOMAIN_RE = re.compile(
    r"\b[\w-]+(?:\.[\w-]+)*\.(?:org|com|net|io|edu|gov|dev|info)\b(?:/[^\s<>()]*[^\s<>().,;:])?"
)
# /etc/fstab, ~/.bashrc, ./script.sh; not "and/or", "TCP/IP" or "1/2"
PATH_RE = re.compile(r"(?<![\w/~.])(?:~|\.{1,2})?/[\w.+@-]+(?:/[\w.+@-]*)*")

LABELS_TH = {
    "note": "หมายเหตุ", "tip": "เคล็ดลับ", "caution": "ข้อควรระวัง", "warning": "คำเตือน",
    "important": "สำคัญ", "answer": "คำตอบ", "explanation": "คำอธิบาย", "solution": "เฉลย",
    "objective": "วัตถุประสงค์", "description": "รายละเอียด", "estimated time": "เวลาโดยประมาณ",
}  # fmt: skip


@dataclass
class Protected:
    text: str  # plain text with placeholders, for the model
    slots: dict[int, str] = field(default_factory=dict)  # placeholder number -> markup
    keys: list[str] = field(default_factory=list)  # key terms (plain text, no code)
    label: tuple[str, str] | None = None  # ("Note", "หมายเหตุ")


def placeholder(n: int) -> str:
    return f"{PH_OPEN}{n}{PH_CLOSE}"


def protect(markup: str) -> Protected:
    slots: dict[int, str] = {}

    def slot(piece: str) -> str:
        n = len(slots) + 1
        slots[n] = piece
        return placeholder(n)

    keys = [strip_markup(k).strip() for k in KEY_RE.findall(markup) if "<c>" not in k]
    label = None
    lead = LEAD_BOLD_RE.match(markup)
    if lead:
        word = re.sub(r"[\s\d:|]+$", "", strip_markup(lead.group(1))).strip().lower()
        if word in LABELS_TH:
            label = (
                word.capitalize() if word != "estimated time" else "Estimated Time",
                LABELS_TH[word],
            )

    work = CODE_RE.sub(lambda m: slot(m.group(0)), markup)
    work = html.unescape(re.sub(r"</?[kb]>", "", work))
    for rx in (URL_RE, EMAIL_RE, DOMAIN_RE, PATH_RE):
        work = rx.sub(lambda m: slot(html.escape(m.group(0), quote=False)), work)
    return Protected(text=work, slots=slots, keys=[k for k in keys if k], label=label)


def restore(thai: str, prot: Protected, glossary_th: dict[str, str]) -> tuple[str, list[str]]:
    """Model output (plain Thai with placeholders) -> Thai markup, plus problem flags."""
    flags: list[str] = []
    found = [int(n) for n in PH_RE.findall(thai)]
    missing = [n for n in prot.slots if n not in found]
    if missing:
        flags.append("placeholder_missing")
    if len(found) != len(set(found)) or any(n not in prot.slots for n in found):
        flags.append("placeholder_extra")

    pieces = PH_RE.split(thai)  # text, number, text, number, ...
    out: list[str] = []
    used: set[int] = set()
    for i, piece in enumerate(pieces):
        if i % 2 == 0:
            out.append(html.escape(piece, quote=False))
        else:
            n = int(piece)
            if n in prot.slots and n not in used:
                out.append(prot.slots[n])
                used.add(n)
    markup = "".join(out)
    if missing:  # never lose code: append what the model dropped
        markup += " " + " ".join(prot.slots[n] for n in missing)
    markup = _tag_keys(markup.strip(), prot.keys, glossary_th)
    if prot.label:
        markup = _tag_label(markup, prot.label[1])
    return markup, flags


def _tag_keys(markup: str, keys: list[str], glossary_th: dict[str, str]) -> str:
    for key in keys:
        thai_form = glossary_th.get(normalize(key))
        candidates = [thai_form] if thai_form else []
        candidates += [key, normalize(key)]
        for form in candidates:
            wrapped = _wrap_first(form, markup) if form else None
            if wrapped is not None:
                markup = wrapped
                break
    return markup


def _wrap_first(form: str, markup: str) -> str | None:
    """Wrap the first occurrence of ``form`` that is not already inside a tag pair."""
    escaped = html.escape(form, quote=False)
    flags = re.IGNORECASE if form.isascii() else 0
    pattern = re.compile(re.escape(escaped) + (r"(?:s|es)?" if form.isascii() else ""), flags)
    protected = [m.span() for m in TAGGED_SPAN_RE.finditer(markup)]
    for m in pattern.finditer(markup):
        if any(a <= m.start() < b for a, b in protected):
            continue
        return markup[: m.start()] + f"<k>{m.group(0)}</k>" + markup[m.end() :]
    return None


def _tag_label(markup: str, label_th: str) -> str:
    m = re.match(r"^(\s*)(" + re.escape(label_th) + r"(?:\s*\d+)?(?:\s*:)?)", markup)
    if not m or markup.startswith("<b>"):
        return markup
    return m.group(1) + f"<b>{m.group(2)}</b>" + markup[m.end() :]
