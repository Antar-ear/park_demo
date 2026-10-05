"""
Glue between the platform and the parsing engine.

No parsing logic lives here. It calls the same drawing_common.analyze_drawing and
script_a_classify.decide_route that Script A uses, so the platform and the batch script
cannot disagree.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Tuple

import drawing_common as dc
import script_a_classify as sa


def process_pdf(path: Path) -> Dict[str, Any]:
    """Parse and classify one PDF. Never raises for a bad file (it comes back FAILED)."""
    started = time.time()
    result = dc.analyze_drawing(path)
    decision = sa.decide_route(path, result)
    out = {
        "status": decision.folder,
        "reason": decision.reason,
        "method": result.method_label,
        "seconds": round(time.time() - started, 1),
        "full_text": result.full_text,
        "warnings": "; ".join(result.warnings),
    }
    for name in dc.FIELD_ORDER:
        out[name] = result.value(name)
    return out


def reroute(full_text: str, fields: Dict[str, str]) -> Tuple[str, str]:
    """
    Re-decide a drawing's routing after a person corrected its fields.

    Builds a DrawingResult from the stored text and the corrected values and hands it to
    the same decide_route Script A uses, so precedence is identical: a still-missing field
    goes to NEEDS_REVIEW, then defense keywords, then an ambiguous "Only for", then
    NON_DEFENSE. No OCR is re-run.
    """
    result = dc.DrawingResult(path=Path("edited.pdf"), ok=True, full_text=full_text)
    for name in dc.FIELD_ORDER:
        result.fields[name] = dc.Field(name=name, value=fields.get(name, "").strip(),
                                       source="manual", conf=1.0)
    decision = sa.decide_route(result.path, result)
    return decision.folder, decision.reason


PREVIEW_DPI = 110


def preview_path(pdf_path: Path) -> Path:
    return Path(str(pdf_path) + ".png")


def render_preview(pdf_path: Path) -> None:
    """
    Render page 1 to a PNG next to the stored PDF, so the browser can show it without
    needing a PDF plugin. Runs on the worker thread (never the web threads) because the
    rasteriser is not safe to call concurrently with the OCR path. Best effort: a corrupt
    PDF simply has no preview.
    """
    import pdfplumber
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            pdf.pages[0].to_image(resolution=PREVIEW_DPI).original.save(str(preview_path(pdf_path)))
    except Exception:
        pass
