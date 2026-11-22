// pdftr review page helpers (htmx does the server round-trips).
"use strict";

window.pdftr = {
  toggle(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = !el.hidden;
    if (!el.hidden) {
      const field = el.querySelector("textarea, input");
      if (field) field.focus();
    }
  },
  toast(message, ms = 2800) {
    const t = document.getElementById("toast");
    if (!t) return;
    t.textContent = message;
    t.classList.add("show");
    clearTimeout(this._timer);
    this._timer = setTimeout(() => t.classList.remove("show"), ms);
  },
};

// Server says no (409 with a reason) or something broke.
document.addEventListener("htmx:responseError", (e) => {
  const xhr = e.detail.xhr;
  pdftr.toast(xhr.responseText || `Error ${xhr.status}`, 6000);
});
document.addEventListener("htmx:sendError", () => {
  pdftr.toast("Cannot reach the review server. Is `pdftr review` still running?", 7000);
});

// Bulk actions: show what happened, then reload the page.
document.addEventListener("pdftr:toast-reload", (e) => {
  pdftr.toast(e.detail.value);
  setTimeout(() => location.reload(), 900);
});

// Link chunk cards and their boxes on the page image.
function highlight(chunkId, on) {
  document.querySelectorAll(`[data-chunk="${chunkId}"]`)
    .forEach((el) => el.classList.toggle("hl", on));
}
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-chunk]");
  if (el) highlight(el.dataset.chunk, true);
});
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest("[data-chunk]");
  if (el) highlight(el.dataset.chunk, false);
});
document.addEventListener("click", (e) => {
  const box = e.target.closest(".box[data-chunk]");
  if (!box) return;
  const card = document.getElementById("chunk-" + box.dataset.chunk);
  if (!card) return;
  card.classList.remove("hidden");
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("pulse");
  setTimeout(() => card.classList.remove("pulse"), 1200);
});

// Keyboard: n / p pages, f next flagged page, Shift+A approve page.
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag) || e.target.isContentEditable) return;
  const go = (id) => {
    const el = document.getElementById(id);
    if (el) el.click();
  };
  if (e.key === "n") go("next-page");
  else if (e.key === "p") go("prev-page");
  else if (e.key === "f") go("next-flagged");
  else if (e.key === "A") go("approve-page");
});
