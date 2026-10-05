"""
Background processing.

ONE worker thread drains a queue, one drawing at a time. PaddleOCR's engine is not
safe to share across threads, and its CPU inference already keeps every core busy, so
a second worker would only contend for the same cores while doubling memory use. The
web thread only inserts rows and enqueues ids, so uploads return immediately and the
page polls for progress.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

from . import db, pipeline

log = logging.getLogger("dp.worker")

_queue: "queue.Queue[int]" = queue.Queue()
_thread: threading.Thread | None = None


def enqueue(drawing_id: int) -> None:
    _queue.put(drawing_id)


def _run() -> None:
    # Load the OCR models up front so the first scanned drawing does not pay the
    # start-up cost. The web server is unaffected.
    import drawing_common as dc
    try:
        dc.get_ocr()
    except Exception:
        log.exception("OCR warm-up failed; it will be retried on first use")
    while True:
        drawing_id = _queue.get()
        try:
            row = db.get_drawing(drawing_id)
            if row is None or row["state"] == "done":
                continue
            db.mark_processing(drawing_id)
            try:
                result = pipeline.process_pdf(Path(row["stored_path"]))
            except Exception as exc:              # analyze_drawing should not raise; belt and braces
                log.exception("drawing %s crashed the pipeline", drawing_id)
                # Built from dc.FIELD_ORDER rather than listed by hand, so this
                # fallback can't go stale against the schema again the way it
                # did after the field-extraction rework (it was still the old
                # 4-field dict, which made db.save_result's generic field loop
                # raise KeyError here -- caught by the outer handler below, so
                # the drawing never reached 'done' OR 'FAILED', just stuck in
                # 'processing' forever).
                result = {"status": "FAILED", "reason": "unexpected error: %s" % exc,
                          "method": "", "seconds": 0.0, "full_text": "", "warnings": ""}
                result.update({f: "" for f in dc.FIELD_ORDER})
            pipeline.render_preview(Path(row["stored_path"]))
            db.save_result(drawing_id, result)
        except Exception:
            log.exception("worker error on drawing %s", drawing_id)
        finally:
            _queue.task_done()


def start() -> None:
    """Start the worker once and re-queue anything a previous run left unfinished."""
    global _thread
    if _thread is not None:
        return
    for drawing_id in db.unfinished_ids():
        _queue.put(drawing_id)
    _thread = threading.Thread(target=_run, name="dp-worker", daemon=True)
    _thread.start()
