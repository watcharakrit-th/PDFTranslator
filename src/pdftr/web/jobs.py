"""Translation started from the review page ("Translate all" in R4).

It runs the same code as ``pdftr translate`` in a thread of the review server, with its
own database connection, and the page polls its status. Only one runs at a time (one
GPU). A stop lets the pieces in flight finish; everything finished is saved, so
starting again continues where it stopped.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from pdftr.gpu import GpuGuard, GpuTooHot


class TranslationJob:
    def __init__(
        self,
        doc_id: int,
        engine,
        database_url: str,
        *,
        guard: GpuGuard | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self.doc_id = doc_id
        self.engine = engine
        self.database_url = database_url
        self.guard = guard
        self.on_done = on_done
        self.state = "running"  # running | stopping | finished | stopped | failed
        self.info: dict = {"phase": "starting", "done": 0, "total": 0}
        self.lines: deque[str] = deque(maxlen=4)
        self.summary: dict | None = None
        self.error: str | None = None
        self.started = time.time()
        self.ended: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pdftr-translate", daemon=True)

    def start(self) -> TranslationJob:
        self._thread.start()
        return self

    @property
    def running(self) -> bool:
        return self.state in ("running", "stopping")

    def stop(self) -> None:
        if self.running:
            self.state = "stopping"
            self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        from pdftr.db import connect
        from pdftr.translate.runner import translate_document

        guard_log = self.guard.log if self.guard is not None else None
        if self.guard is not None:  # show "cooling down" on the page too
            self.guard.log = self.lines.append
        try:
            with connect(self.database_url) as conn:
                self.summary = translate_document(
                    conn,
                    self.doc_id,
                    self.engine,
                    guard=self.guard,
                    database_url=self.database_url,
                    log=self.lines.append,
                    progress=self._progress,
                    stop=self._stop.is_set,
                )
            self.state = "stopped" if self.summary.get("stopped") else "finished"
        except GpuTooHot as exc:
            self.state, self.error = "failed", f"stopped to protect the GPU ({exc})"
        except Exception as exc:  # noqa: BLE001 - a background job must report any failure
            self.state, self.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self.ended = time.time()
            if self.guard is not None:
                self.guard.log = guard_log
            if self.on_done is not None:
                self.on_done()

    def _progress(self, info: dict) -> None:
        if info.get("total"):
            self.info = info
        else:  # tables, contents, bookmarks: keep the chunk counts on show
            self.info = {**self.info, "phase": info["phase"], "eta_s": None}

    def view(self) -> dict:
        """What the R4 page shows."""
        info = self.info
        total, done = info.get("total") or 0, info.get("done") or 0
        eta = info.get("eta_s")
        temp = self.guard.temperature() if self.guard is not None and self.running else None
        return {
            "state": self.state,
            "running": self.running,
            "phase": info.get("phase", ""),
            "done": done,
            "total": total,
            "percent": round(100 * done / total) if total else 0,
            "translated": info.get("translated", 0),
            "memory": info.get("memory", 0),
            "flagged": info.get("flagged", 0),
            "eta": f"{eta // 60} min" if eta and eta >= 60 else ("under a minute" if eta else ""),
            "gpu_c": temp,
            "last": self.lines[-1].strip() if self.lines else "",
            "error": self.error,
        }
