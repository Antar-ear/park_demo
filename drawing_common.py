"""
drawing_common.py
=================
Shared extraction engine for the engineering-drawing pipeline.

BOTH Script A (classify & sort) and Script B (extract & build Excel) import this
module and call analyze_drawing(). That is deliberate: if each script had its own
extraction code they could disagree -- Script A could pass a file as "all fields
readable" and Script B would then write a blank cell for it. One engine, called
twice, makes that impossible.

This module is POLICY-FREE. It extracts text and the ten REVISIONS-table /
title-block fields (see FIELD_ORDER). It does NOT decide DEFENSE vs NON_DEFENSE
-- that policy (the keyword list) lives at the top of script_a_classify.py where
it is easy to tune.

Extraction paths
----------------
    1. TEXT -- pdfplumber's text layer (vector / CAD-generated PDFs). Fast, exact.
    2. OCR  -- used only when the text layer is unusable (scanned or hand-drawn):
               THREE independent region crops, each rasterised straight from the
               PDF (render_region_image), not from one full-page render sliced
               afterward:
                 a. CLASSIFICATION_CROP_REGION (top-left), coarse -- feeds the
                    DEFENSE keyword scan (run_keyword_scan).
                 b. REVISIONS_TABLE_REGION (top-right), fine -- the REVISIONS
                    table (deskew -> contrast boost -> binarize -> PaddleOCR,
                    at its FIELD_CROP_DPIS resolutions).
                 c. ID_BLOCK_REGION (bottom-right), fine -- Name/Size/Cage
                    Code/Drawing No., same pipeline as b.

Both paths are normalised into ONE token model (text + bounding box + confidence,
in PDF point coordinates with y increasing downward), so every piece of logic
downstream of extraction is written exactly once.

OFFLINE NOTE
------------
There are no network calls at runtime. The single exception is PaddleOCR's FIRST
run on a new machine, which downloads its pretrained detection/recognition models
and caches them on disk. That is a one-time setup step, not a runtime dependency.
See README.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("drawing")


# =============================================================================
#  TUNING CONSTANTS -- the knobs worth turning when accuracy is off
# =============================================================================

# --- Text-layer vs OCR decision ----------------------------------------------
# A PDF whose text layer yields fewer than this many non-whitespace characters is
# treated as image-based and sent to OCR. A bare threshold of 0 is not enough:
# some scanned drawings carry a near-empty text layer (a stray stamp, a watermark)
# which would otherwise wrongly skip OCR. 20 was not enough either in practice --
# real Parker exports carry a "Delivered from Parker Vault - VERIFY REVISION
# BEFORE USE" watermark line that alone runs to ~77 characters on an otherwise
# fully scanned page, which used to be enough to wrongly take the text path and
# come back with every field blank.
MIN_TEXT_CHARS = 150

# --- Rasterisation / preprocessing -------------------------------------------
CLAHE_CLIP = 2.0        # contrast boost strength; raise for washed-out scans
MAX_DESKEW_DEG = 15.0   # ignore computed skew beyond this (means detection failed)

# Each field crop is read at two fixed resolutions, (render_dpi, detect_dpi),
# independent of how big the crop is:
#   render_dpi -- the crop is rendered at this, and the recogniser reads each
#                 text box from that image.
#   detect_dpi -- Paddle's detector (which finds the text boxes) works on a copy
#                 scaled down to this.
# These are the effective resolutions each crop had in the configuration that
# first read all four real samples correctly. They used to fall out of crop
# size instead (crops were upscaled toward a 6000px budget), so resizing a crop
# silently changed both. OCR is sensitive to them: moving the REVISIONS crop
# from ~390 to 350 DPI was enough to misread a revision letter on one sample.
FIELD_CROP_DPIS = {
    "revisions": (390, 165),
    "id_block": (350, 150),
}

# --- OCR filtering ------------------------------------------------------------
# PaddleOCR returns a confidence per text box. Boxes below this are dropped as
# noise rather than being allowed to pollute a title-block field. Raise it if you
# see garbage values in the Excel; lower it if good fields are being missed.
MIN_OCR_CONFIDENCE = 0.30

# --- Region-cropped OCR: three independent crops instead of one whole page ---
# Rather than OCR the whole sheet (dense with dimensions, GD&T callouts, and
# notes that have nothing to do with any of these regions and can confuse
# header/keyword matching by coincidence -- see extract_table_fields's own
# `region` argument, which exists for exactly this reason) each of these is
# rendered and OCR'd SEPARATELY, straight from the PDF via pdfplumber's own
# page.crop(bbox).to_image() -- not by rendering the full page and slicing the
# raster afterward. That distinction matters: deskewing one big crop rotates
# the whole thing around ONE pivot, which is the wrong correction for content
# that isn't actually centred in it; three small, independent crops each get
# their own correctly-centred deskew, and each is far smaller and faster to
# OCR on top of that. All three are given as fractions of page width/height
# (x0, y0, x1, y1). REVISIONS_TABLE_REGION and ID_BLOCK_REGION are defined
# below (they double as the post-OCR token-filter regions extract_table_fields
# uses) and reused here as the OCR crop boxes too -- one box, two jobs.

# The classification keyword scan (DEFENSE_KEYWORDS in script_a_classify.py):
# narrowed to the top-left corner, where Parker's proprietary/export-control
# legend is consistently printed (confirmed directly on real samples), rather
# than the whole page. A center-page classification stamp elsewhere on the
# sheet would be missed by this -- a real scope trade made deliberately, to
# stop OCR noise from the rest of a dense CAD drawing from interfering with
# field extraction. Revisit if real batches show stamps landing outside it.
CLASSIFICATION_CROP_REGION = (0.00, 0.00, 0.50, 0.18)

# The classification text is large and plainly printed (a stamp, a legend
# paragraph) -- nowhere near as demanding as small title-block lettering, so
# this pass renders at a low resolution (~2000px across the crop), skips
# deskew/CLAHE, and uses Paddle's own default detector budget; it only needs
# to find the text, not resolve fine detail.
CLASSIFICATION_RENDER_DPI = 120
CLASSIFICATION_DETECT_SIDE = 960    # Paddle's own default; plenty for stamp-sized text

# --- Title-block / revision-block field labels ---------------------------------
# Label positions vary by division and by drafting team, so fields are found by
# LABEL TEXT, not by page position. Add synonyms here as new formats turn up.
# Matching is case-insensitive and ignores punctuation, so "DWG. NO." matches
# "DWG NO". Longer / more specific synonyms are tried first (the code sorts them
# by word count, descending), so "DRAWING NUMBER" wins over bare "DRAWING".
#
# "name" has no entry: Parker's title block never prints a label for the part
# name/description -- it just sits under the company/division block with
# nothing labelling it. See extract_name_field(), which locates it positionally.
LABEL_SYNONYMS: Dict[str, List[str]] = {
    # -- REVISIONS table columns (top-right, one row per change) --
    "change_no": ["CHANGE NO", "CHANGE NUMBER", "ECO NO"],
    "zone": ["ZONE"],
    "ltr": ["LTR"],
    "description": ["DESCRIPTION"],
    "date": ["DATE"],
    "approved": ["APPROVED", "APPD", "APPR"],
    # -- title block row (bottom-right): SIZE | CAGE CODE | DRAWING NO. --
    "size": ["SIZE"],
    "cage_code": ["CAGE CODE", "CAGE"],
    "drawing_number": [
        "DRAWING NUMBER", "DRAWING NO", "DRAWING NUM",
        "DWG NUMBER", "DWG NO", "DRG NO",
        "PART NUMBER", "PART NO",
    ],
}

# --- Geometry tolerances ------------------------------------------------------
LINE_BAND_RATIO = 0.60       # tokens whose centres sit within this * height are one line

# Page regions (as fractions of page width/height: x0, y0, x1, y1) that the
# REVISIONS table and the SIZE/CAGE CODE/DRAWING NO. row are searched within.
# This is deliberately tight, not just a starting point: a real engineering
# drawing's body is dense with its own labels (GD&T zone callouts, notes,
# "PREPARED"/"ENGINEER" sign-off lines), several of which are spelled exactly
# like a column header ("ZONE", "APPROVED", ...). Searching the whole page for
# header text lets those coincidences win; bounding the search to the corner
# each table actually lives in does not.
REVISIONS_TABLE_REGION = (0.55, 0.00, 1.00, 0.30)
# Just the company block, the unlabelled Name, and the SIZE / CAGE CODE /
# DRAWING NO. row. It used to start at x=0.50, which also pulled in the Next
# Assy, Application, tolerances and Prepared/Engineer sub-boxes: ~79 OCR text
# boxes where ~10 are needed, and OCR cost scales with box count (this crop was
# about half of each drawing's processing time). Measured on the Gas Turbine
# Fuel Systems template: SIZE header starts at x~0.83, company block at y~0.84,
# the data row ends at y~0.96. Widen this if another division's title block
# sits further left or lower.
ID_BLOCK_REGION = (0.80, 0.83, 1.00, 0.97)

# Boilerplate markers used to find the BOTTOM of the company/division/address
# block in the title block, so extract_name_field() knows where that block ends
# and the (unlabelled) part name begins. Deliberately not a division name --
# Parker has many divisions, and hardcoding one would silently miss the rest.
COMPANY_BLOCK_MARKERS = ["PARKER", "CORPORATION", "DIVISION"]

# --- Field validation ---------------------------------------------------------
# What counts as "readable": a field must survive noise-stripping and still hold at
# least one alphanumeric character. Date/Ltr get SOFT sanity checks -- a
# suspicious value is flagged in the run log but still accepted, so an unusual but
# legitimate format does not force a file into NEEDS_REVIEW.
#   -> Set this True to make those checks HARD (suspicious value => NEEDS_REVIEW).
STRICT_DATE_REVISION = False
MAX_REVISION_LEN = 8
MAX_FIELD_LEN = 120          # values longer than this are truncated (bad cell split)

# Full field set extracted from a Parker drawing: the REVISIONS table's current
# (latest) row, plus the title block's Name/Size/Cage Code/Drawing Number row.
FIELD_ORDER = ["change_no", "zone", "ltr", "description", "date", "approved",
              "name", "size", "cage_code", "drawing_number"]
FIELD_LABELS = {             # human-readable names, used in logs and the Excel header
    "change_no": "Change No.",
    "zone": "Zone",
    "ltr": "Ltr",
    "description": "Description",
    "date": "Date",
    "approved": "Approved",
    "name": "Name",
    "size": "Size",
    "cage_code": "Cage Code",
    "drawing_number": "Drawing Number",
}

# Fields that must be readable for a drawing to be considered fully understood.
# A miss on any of these routes the file to NEEDS_REVIEW (see
# DrawingResult.missing_required_fields and script_a_classify.py's routing).
# The rest are extracted best-effort: a miss is logged but does not block
# routing, since the change-history columns are noisier to OCR and less
# essential than knowing what the part is and which revision it's at.
REQUIRED_FIELDS = ["ltr", "name", "size", "cage_code", "drawing_number"]


# =============================================================================
#  DATA MODEL
# =============================================================================

@dataclass
class Token:
    """One word of text with its bounding box, in PDF points, y increasing DOWN."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 1.0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)

    @property
    def width(self) -> float:
        return max(self.x1 - self.x0, 1.0)


@dataclass
class Field:
    """One extracted title-block field, with provenance so the log explains itself."""
    name: str
    value: str = ""
    source: Optional[str] = None      # "label" | "grid" | None (not found)
    conf: float = 0.0
    label_seen: Optional[str] = None  # which label synonym matched, if any
    note: Optional[str] = None        # soft sanity-check warning, if any

    @property
    def readable(self) -> bool:
        """Non-empty and holding at least one alphanumeric character."""
        return bool(self.value) and any(ch.isalnum() for ch in self.value)


@dataclass
class DrawingResult:
    """Everything the engine learned about one PDF."""
    path: Path
    ok: bool = False                  # False => FAILED (file unreadable altogether)
    failure_reason: Optional[str] = None
    method: Optional[str] = None      # "text" | "ocr"
    page_count: int = 0
    full_text: str = ""               # page 1 only, for keyword scanning
    tokens: List[Token] = dc_field(default_factory=list)   # page 1 only
    fields: Dict[str, Field] = dc_field(default_factory=dict)
    warnings: List[str] = dc_field(default_factory=list)

    @property
    def method_label(self) -> str:
        return {"text": "Text (pdfplumber)",
                "ocr": "OCR (PaddleOCR)"}.get(self.method or "", "")

    @property
    def missing_fields(self) -> List[str]:
        """Every unreadable field, required or not -- for the run log."""
        return [n for n in FIELD_ORDER
                if n not in self.fields or not self.fields[n].readable]

    @property
    def missing_required_fields(self) -> List[str]:
        """Unreadable REQUIRED_FIELDS only -- this is what drives NEEDS_REVIEW."""
        return [n for n in REQUIRED_FIELDS
                if n not in self.fields or not self.fields[n].readable]

    @property
    def all_fields_readable(self) -> bool:
        return not self.missing_required_fields

    def value(self, name: str) -> str:
        f = self.fields.get(name)
        return f.value if (f and f.readable) else ""


# =============================================================================
#  TEXT NORMALISATION HELPERS
# =============================================================================

_PUNCT_RE = re.compile(r"[^A-Za-z0-9]+")
_WS_RE = re.compile(r"\s+")

# Leading separators between a label and its value: "DWG NO: 123", "REV - C"
_SEP_STRIP_RE = re.compile(r"^[\s:;=|/\\.,\-–—]+")

# OCR noise: a value made only of these characters is not a real value
_NOISE_ONLY_RE = re.compile(r"^[\s\W_]+$")


def norm_label(s: str) -> str:
    """Normalise for label comparison: uppercase, punctuation -> single space."""
    return _PUNCT_RE.sub(" ", s or "").upper().strip()


def clean_value(s: str) -> str:
    """Tidy an extracted field value: collapse whitespace, strip separators/noise."""
    s = _WS_RE.sub(" ", s or "").strip()
    s = _SEP_STRIP_RE.sub("", s)
    s = s.strip(" :;=|/\\.,-")
    if not s or _NOISE_ONLY_RE.match(s):
        return ""
    if len(s) > MAX_FIELD_LEN:
        s = s[:MAX_FIELD_LEN].rstrip()
    return s


def _sorted_synonyms(field_name: str) -> List[str]:
    """Synonyms for a field, most specific (most words) first."""
    syns = [norm_label(s) for s in LABEL_SYNONYMS.get(field_name, [])]
    return sorted(syns, key=lambda s: (-len(s.split()), -len(s)))


# =============================================================================
#  STAGE 1-2: TEXT-LAYER EXTRACTION (pdfplumber)
# =============================================================================

def tokens_from_page_text(page) -> List[Token]:
    """
    Words from a pdfplumber page's text layer, as Tokens.

    pdfplumber reports word boxes as x0/x1 plus top/bottom, where 'top' is measured
    downward from the top of the page -- the same orientation as a rasterised image,
    so the whole pipeline can share one coordinate convention.
    """
    out: List[Token] = []
    try:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    except Exception as exc:                      # a damaged content stream
        log.debug("extract_words failed: %s", exc)
        return out
    for w in words:
        txt = (w.get("text") or "").strip()
        if not txt:
            continue
        out.append(Token(
            text=txt,
            x0=float(w["x0"]), y0=float(w["top"]),
            x1=float(w["x1"]), y1=float(w["bottom"]),
            conf=1.0,
        ))
    return out


# =============================================================================
#  STAGE 3-4: RASTERISE + PREPROCESS (for the OCR path)
# =============================================================================

def render_region_image(page, region: Tuple[float, float, float, float],
                        dpi: int) -> Tuple[np.ndarray, float, float]:
    """
    Rasterise only `region` of a page (fractions of page width/height),
    straight from the PDF via pdfplumber's own page.crop() -- not by rendering
    the whole page and slicing the raster afterward.

    That distinction is deliberate: cropping first means deskew (run later, in
    preprocess_for_ocr) rotates around THIS region's own centre, not the whole
    page's, which is the wrong pivot for content that isn't actually centred on
    the page. It's also strictly less work -- a fraction of the pixels.

    Returns (image, offset_x_points, offset_y_points). The offset is the
    region's own top-left corner in PAGE-POINT space; add it to any token
    coordinates read back from this image (which come out relative to the
    region's own top-left) to get page-point coordinates.
    """
    page_w, page_h = float(page.width), float(page.height)
    x0f, y0f, x1f, y1f = region
    offset_x, offset_y = x0f * page_w, y0f * page_h
    bbox = (offset_x, offset_y, x1f * page_w, y1f * page_h)
    pil = page.crop(bbox).to_image(resolution=dpi).original.convert("RGB")
    bgr = np.asarray(pil)[:, :, ::-1].copy()          # RGB -> BGR for OpenCV
    return bgr, offset_x, offset_y


def _deskew(gray: np.ndarray) -> Tuple[np.ndarray, float]:
    """
    Straighten a tilted scan.

    The text is thresholded to a mask of dark pixels, and minAreaRect over those
    pixels gives the dominant tilt of the content. Only small corrections are
    trusted: a computed angle beyond MAX_DESKEW_DEG means detection latched onto a
    border or a drawing feature rather than the text, so it is discarded.
    """
    import cv2
    inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = cv2.findNonZero(inv)
    if coords is None:
        return gray, 0.0
    angle = cv2.minAreaRect(coords)[-1]
    # minAreaRect reports 0..90; fold it into a signed correction near zero.
    if angle > 45:
        angle -= 90
    if abs(angle) < 0.15 or abs(angle) > MAX_DESKEW_DEG:
        return gray, 0.0
    h, w = gray.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    rotated = cv2.warpAffine(gray, m, (w, h),
                             flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_REPLICATE)
    return rotated, float(angle)


def preprocess_for_ocr(img_bgr: np.ndarray,
                       debug_path: Optional[Path] = None) -> Tuple[np.ndarray, float]:
    """
    Prepare a rendered field crop for OCR.

    Order matters, and each step earns its place on hand-drawn scans:
        1. grayscale  -- colour carries nothing here
        2. deskew     -- rotate the crop level; OCR line grouping degrades on tilt
        3. CLAHE      -- local contrast boost, for faint pencil and uneven scans
        4. Otsu       -- binarize, separating strokes from paper texture

    No resizing: the crop is already rendered at its FIELD_CROP_DPIS resolution.
    Returns (processed_image, detected_skew_degrees).
    """
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray, skew = _deskew(gray)
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    binar = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    if debug_path is not None:
        # --debug-images writes what OCR actually saw; invaluable when tuning.
        try:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(debug_path), binar)
        except Exception as exc:
            log.debug("could not write debug image: %s", exc)
    return binar, skew


# =============================================================================
#  STAGE 5: OCR (PaddleOCR)
# =============================================================================

_OCR_SINGLETON = None


def get_ocr():
    """
    Build the PaddleOCR engine once per process and reuse it.

    Model load is expensive, so a batch of 200 drawings must not pay for it 200
    times. On a machine that has never run PaddleOCR, this first construction also
    downloads the pretrained models and caches them on disk -- a one-time setup
    step; every run afterwards is fully offline.

    One engine reads all three crops, one after another, on Paddle's default 10
    CPU threads. Each alternative was measured head to head (interleaved, same
    process) and didn't help, so none of them is in the code: an engine per crop
    (no difference), 5 threads instead of 10 (~15% slower), and the three crops
    in parallel (no difference -- CPU inference already uses every core).
    """
    global _OCR_SINGLETON
    if _OCR_SINGLETON is None:
        from paddleocr import PaddleOCR
        log.info("Initialising PaddleOCR (first run on a new machine downloads "
                 "and caches its models -- this is a one-time setup step)")
        # Orientation and unwarping stages are disabled: deskew is handled in our
        # own preprocessing, and those extra models cost time on every page.
        _OCR_SINGLETON = PaddleOCR(
            lang="en",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            # paddlepaddle's oneDNN CPU kernels raise
            # "ConvertPirAttribute2RuntimeAttribute not support" on the PP-OCR
            # models in this build, so oneDNN is switched off. Inference then
            # runs on the portable CPU path -- slower per page, but it actually
            # works. Retest this if paddlepaddle is ever upgraded.
            enable_mkldnn=False,
        )
    return _OCR_SINGLETON


def _ocr_predict(img_bgr: np.ndarray, detect_side: int):
    """
    OCR one image, detecting text at `detect_side` pixels on the image's longest
    side (Paddle shrinks a larger image down to that for the detector only; the
    recogniser still reads each box at full resolution).
    """
    return get_ocr().predict(img_bgr, text_det_limit_type="max",
                             text_det_limit_side_len=int(detect_side))


def _iter_paddle_results(raw):
    """
    Yield (text, polygon, score) from a PaddleOCR result.

    PaddleOCR changed its result shape between 2.x and 3.x, and 3.x itself returns
    an object that behaves like a dict. All three shapes are accepted so an engine
    upgrade does not silently break extraction.
    """
    if not raw:
        return
    for page in raw:
        if page is None:
            continue
        # --- 3.x: dict-like with parallel lists ---
        texts = polys = scores = None
        if isinstance(page, dict):
            texts = page.get("rec_texts")
            polys = page.get("rec_polys")
            if polys is None:
                polys = page.get("dt_polys")
            scores = page.get("rec_scores")
        else:
            for attr in ("json", "res"):
                inner = getattr(page, attr, None)
                if isinstance(inner, dict):
                    inner = inner.get("res", inner)
                    texts = inner.get("rec_texts")
                    polys = inner.get("rec_polys") or inner.get("dt_polys")
                    scores = inner.get("rec_scores")
                    break
        if texts is not None and polys is not None:
            scores = scores if scores is not None else [1.0] * len(texts)
            for t, p, s in zip(texts, polys, scores):
                yield t, p, float(s)
            continue
        # --- 2.x: list of [polygon, (text, score)] ---
        try:
            for line in page:
                poly, rec = line[0], line[1]
                yield rec[0], poly, float(rec[1])
        except (TypeError, IndexError, KeyError):
            log.debug("unrecognised PaddleOCR result shape; skipping a page")


def run_keyword_scan(img_bgr: np.ndarray) -> str:
    """
    Fast, coarse OCR pass over CLASSIFICATION_CROP_REGION, for the
    classification keyword scan only.

    Deliberately skips what preprocess_for_ocr does for the field crops (deskew,
    CLAHE): a classification signal (a "Product for Defense" stamp, a
    proprietary/export-control legend) is large, plainly printed text, nowhere
    near as demanding as small title-block lettering, so the crop is rendered
    at CLASSIFICATION_RENDER_DPI and only binarized.

    Returns the crop's text, joined, with no position information -- this feeds
    classify_text()'s keyword search in script_a_classify.py, which does not
    need token boxes the way field extraction does.
    """
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    binar = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    raw = _ocr_predict(cv2.cvtColor(binar, cv2.COLOR_GRAY2BGR), CLASSIFICATION_DETECT_SIDE)
    texts = [t.strip() for t, _poly, score in _iter_paddle_results(raw)
            if score >= MIN_OCR_CONFIDENCE and (t or "").strip()]
    return " ".join(texts)


def _ocr_field_crop(crop: str, img: np.ndarray, offset_x: float, offset_y: float,
                    debug_path: Optional[Path]) -> Tuple[List[Token], float]:
    """
    Preprocess and OCR one field crop (rendered at its FIELD_CROP_DPIS render
    DPI); return (tokens in page-point space, skew degrees).

    Box coordinates come back in crop pixels: dividing by pixels-per-point and
    adding the crop's page-point offset puts every crop's tokens in the one
    page-point coordinate system the field extraction functions all assume.
    """
    import cv2
    render_dpi, detect_dpi = FIELD_CROP_DPIS[crop]
    proc, skew = preprocess_for_ocr(img, debug_path)
    # Preprocessing leaves a single-channel binary image, but the detector
    # expects 3 channels (it unpacks h, w, c), so widen it back to BGR.
    proc = cv2.cvtColor(proc, cv2.COLOR_GRAY2BGR)
    raw = _ocr_predict(proc, max(proc.shape[:2]) * detect_dpi / render_dpi)

    px_per_pt = render_dpi / 72.0
    tokens: List[Token] = []
    for text, poly, score in _iter_paddle_results(raw):
        text = (text or "").strip()
        if not text or score < MIN_OCR_CONFIDENCE:
            continue
        pts = np.asarray(poly, dtype=float).reshape(-1, 2)
        tokens.append(Token(
            text=text,
            x0=pts[:, 0].min() / px_per_pt + offset_x,
            y0=pts[:, 1].min() / px_per_pt + offset_y,
            x1=pts[:, 0].max() / px_per_pt + offset_x,
            y1=pts[:, 1].max() / px_per_pt + offset_y,
            conf=score,
        ))
    return tokens, skew


# =============================================================================
#  STAGE 7: REVISIONS-TABLE / TITLE-BLOCK FIELD EXTRACTION
# =============================================================================
#
#  Two column-header-anchored table reads (extract_table_fields, for the
#  REVISIONS table and the SIZE/CAGE CODE/DRAWING NO. row) plus one positional
#  read (extract_name_field, for the one field with no printed label). A field
#  none of these find is left unreadable, which routes the file to
#  NEEDS_REVIEW if it's a REQUIRED_FIELDS entry -- never guessed, never
#  inferred.
# =============================================================================

def group_into_lines(tokens: List[Token]) -> List[List[Token]]:
    """Group tokens into visual text lines by vertical centre, left-to-right."""
    if not tokens:
        return []
    ordered = sorted(tokens, key=lambda t: (t.cy, t.x0))
    lines: List[List[Token]] = [[ordered[0]]]
    # Each token is compared against the line's ANCHOR (its first token), not the
    # last one added. Comparing against the last token lets a run of slowly
    # drifting baselines chain together, merging several rows of a title block
    # into one apparent line.
    anchors: List[Token] = [ordered[0]]
    for tok in ordered[1:]:
        ref = anchors[-1]
        tol = LINE_BAND_RATIO * max(tok.height, ref.height)
        if abs(tok.cy - ref.cy) <= tol:
            lines[-1].append(tok)
        else:
            lines.append([tok])
            anchors.append(tok)
    for ln in lines:
        ln.sort(key=lambda t: t.x0)
    return lines


def _match_label_in_line(line: List[Token], synonym: str) -> Optional[Tuple[int, int, str]]:
    """
    Find `synonym` (already normalised) as a run of consecutive tokens in `line`.

    Returns (start_index, end_index_exclusive, leftover_text). `leftover_text` covers
    the "DWGNO:12345" case, where OCR merged label and value into one token -- the
    value part is handed straight back rather than being lost.
    """
    words = synonym.split()
    n = len(words)
    for i in range(len(line)):
        # Case A: the label spans n consecutive tokens. Internal spaces are
        # collapsed on both sides before comparing, so a single OCR token that
        # glued punctuation-separated letters together (e.g. "E.C.O." normalising
        # to "E C O") still lines up with the synonym's own word split.
        if i + n <= len(line):
            run = [norm_label(t.text).replace(" ", "") for t in line[i:i + n]]
            if run == words:
                return i, i + n, ""
        # Case B: label and value share a single token -- "REV:C", "DWGNO:12345",
        # or "DWG NO: HD-1042". OCR merges a label with its value routinely, since
        # on a ruled title block they sit close together with no wide gap between
        # them, so this case is the norm on the OCR path rather than an oddity.
        #
        # Comparison is done on alphanumerics only, which makes it indifferent to
        # how the label's internal spacing and punctuation came out of OCR:
        # "DWG NO", "DWGNO" and "DWG. NO." all reduce to "DWGNO".
        raw = line[i].text
        compact_tok = norm_label(raw).replace(" ", "")
        compact_syn = synonym.replace(" ", "")
        # Case B0: a multi-word label OCR merged into exactly one token, with
        # NOTHING else stuck to it ("DRAWING NO." recognised as a single text
        # region) -- a label-only match, no value, same as the Case A label
        # spanning separate tokens would be with no leftover.
        if compact_tok == compact_syn:
            return i, i + 1, ""
        if compact_tok.startswith(compact_syn):
            # Walk the raw token to just past the label's own characters.
            seen = 0
            idx = len(raw) - len(raw.lstrip())
            while idx < len(raw) and seen < len(compact_syn):
                if raw[idx].isalnum():
                    seen += 1
                idx += 1
            # The next character must not be alphanumeric, or this is a longer
            # word that merely begins with the label ("DWGNOTE" is not "DWG NO").
            if idx >= len(raw) or not raw[idx].isalnum():
                leftover = clean_value(raw[idx:])
                if leftover:
                    return i, i + 1, leftover
    return None


def _region_tokens(tokens: List[Token], page_w: float, page_h: float,
                   region: Tuple[float, float, float, float]) -> List[Token]:
    """Tokens whose centre falls inside `region` (x0, y0, x1, y1 as page fractions)."""
    x0f, y0f, x1f, y1f = region
    x0, y0, x1, y1 = x0f * page_w, y0f * page_h, x1f * page_w, y1f * page_h
    return [t for t in tokens if x0 <= t.cx <= x1 and y0 <= t.cy <= y1]


def _find_header_row(lines: List[List[Token]], columns: List[str]
                     ) -> Optional[Tuple[int, Dict[str, Tuple[float, float]]]]:
    """
    Find the line that best matches `columns`' labels, left to right.

    Returns (line_index, {field_name: (x0, x1)}) for whichever line matches the
    most columns -- not necessarily all of them, since a best-effort column's
    header could itself be an OCR miss. Returns None if fewer than 2 columns
    matched anywhere, which is not enough to trust as a real header row.
    """
    best_idx, best_hits = None, {}
    for idx, line in enumerate(lines):
        hits: Dict[str, Tuple[float, float]] = {}
        for name in columns:
            for synonym in _sorted_synonyms(name):
                hit = _match_label_in_line(line, synonym)
                if hit:
                    start, end, _leftover = hit
                    hits[name] = (line[start].x0, line[end - 1].x1)
                    break
        if len(hits) > len(best_hits):
            best_idx, best_hits = idx, hits
    if best_idx is None or len(best_hits) < 2:
        return None
    return best_idx, best_hits


def extract_table_fields(tokens: List[Token], columns: List[str], anchor: str,
                         page_w: float, page_h: float,
                         region: Tuple[float, float, float, float],
                         max_rows: int = 40, open_ends: bool = True) -> Dict[str, Field]:
    """
    Read a ruled table by column position: find the header row, then the LAST
    data row below it (the table's current state -- e.g. the most recent
    revision), and read each column's value from wherever it sits under that
    column's header.

    This differs from a simple label/value pair (one label, one adjacent value)
    because a table has one header shared by many rows, so a value is found by
    X-POSITION under a header, not by adjacency to a label. Used for the
    REVISIONS table (change_no/zone/ltr/description/date/approved) and the
    title block's size/cage_code/drawing_number row.

    `region` restricts the search to the corner of the sheet the table actually
    lives in (REVISIONS_TABLE_REGION / ID_BLOCK_REGION). This matters on a real
    drawing: the page body is full of its own "ZONE" callouts, notes, and
    sign-off lines that can spell a column header exactly, and without a region
    restriction the header search has no way to tell those apart from the real
    table.

    `anchor` is the one column a line must contain to count as a real data row
    (e.g. "ltr"): it must be a column with a header on both sides so stray text
    is unlikely to land in its band by coincidence. Rows are read top to bottom
    and simply overwrite one another, so whichever qualifying row is LOWEST on
    the page wins -- that is the current state, even when earlier rows (e.g. an
    initial release with no revision letter yet) left some columns blank.

    `open_ends`: whether the genuinely-first/last column in `columns` gets an
    open (-inf/+inf) bound on its outer side, so a value wider than its header
    (a long Description) is still captured whole. Real title blocks pack other
    small sub-boxes tightly around the id-block row (a sheet/rev reference
    table, "DATA BASE MODEL"), and those get pulled onto the same OCR-grouped
    line as the true row -- an open bound there would swallow them. Pass False
    for a table whose values are always short (the id block); leave True for
    the REVISIONS table, where Description genuinely needs the room.
    """
    tokens = _region_tokens(tokens, page_w, page_h, region)
    lines = group_into_lines(tokens)
    header = _find_header_row(lines, columns)
    if header is None:
        return {}
    header_idx, header_hits = header
    if anchor not in header_hits:
        return {}                          # can't tell which row is "current" without it

    # Column bands: each header owns the space out to the midpoint with its
    # neighbours. Only a column that is genuinely first/last in the FULL
    # `columns` schema is left open-ended on that side (so a long Description
    # is still captured in full); a column that merely happens to be first/last
    # among the ones OCR actually found a header for gets a fixed margin
    # instead. Without that distinction, a missed header (e.g. Change No.'s, if
    # OCR mangled it) would let its neighbour's band silently swallow its data.
    # The margin is generous (2.5x the header's own width) because a value can
    # run noticeably wider than its header -- a drawing number like "6041096"
    # measured wider than the "DRAWING NO." label above it on a real sample.
    MARGIN_RATIO = 2.5
    schema_pos = {name: i for i, name in enumerate(columns)}
    ordered = sorted(header_hits.items(), key=lambda kv: kv[1][0])
    bands: List[Tuple[str, float, float]] = []
    for i, (name, (x0, x1)) in enumerate(ordered):
        width = x1 - x0
        if i == 0:
            is_first = open_ends and schema_pos[name] == 0
            left = float("-inf") if is_first else x0 - MARGIN_RATIO * width
        else:
            left = (ordered[i - 1][1][1] + x0) / 2.0
        if i == len(ordered) - 1:
            is_last = open_ends and schema_pos[name] == len(columns) - 1
            right = float("inf") if is_last else x1 + MARGIN_RATIO * width
        else:
            right = (x1 + ordered[i + 1][1][0]) / 2.0
        bands.append((name, left, right))

    header_line = lines[header_idx]
    header_top = min(t.y0 for t in header_line)
    header_bottom = max(t.y1 for t in header_line)

    last_row: Dict[str, List[Token]] = {}
    for line in lines[header_idx + 1: header_idx + 1 + max_rows]:
        # A line is "still the header" only if its own vertical CENTRE falls
        # within the header line's y-range -- i.e. group_into_lines split one
        # visual line into two buckets, not that it's simply close below the
        # header. A margin-based cutoff (e.g. "within 25% of a header token's
        # height") was tried here before and was too fragile: on a real sample
        # the true data row started only fractions of a point closer to the
        # header than that margin allowed, so this check swallowed the entire
        # row and the file silently lost Size/Cage Code/Drawing No.
        line_cy = (min(t.y0 for t in line) + max(t.y1 for t in line)) / 2.0
        if header_top <= line_cy <= header_bottom:
            continue                       # a stray token still on the header's own line
        row: Dict[str, List[Token]] = {}
        for tok in line:
            for name, lo, hi in bands:
                if lo <= tok.cx < hi:
                    row.setdefault(name, []).append(tok)
                    break
        if anchor not in row:
            continue                       # not this table (or a blank/skipped row): keep looking
        last_row = row                     # overwritten each hit -> the bottom-most wins

    found: Dict[str, Field] = {}
    for name, toks in last_row.items():
        toks = sorted(toks, key=lambda t: t.x0)
        value = clean_value(" ".join(t.text for t in toks))
        if value:
            found[name] = Field(name=name, value=value, source="table",
                                conf=min(t.conf for t in toks))
    return found


def extract_name_field(tokens: List[Token], page_w: float, page_h: float,
                       id_header_row: Optional[Tuple[int, Dict[str, Tuple[float, float]]]]
                       ) -> Optional[Field]:
    """
    Locate the part Name/description -- the one title-block field with no
    printed label. It sits directly below the company/division/address block
    and directly above the SIZE/CAGE CODE/DRAWING NO. row, with nothing else
    marking it, so it is found positionally between those two anchors rather
    than by label:

      1. The lower bound is the top of the already-located SIZE/CAGE CODE/
         DRAWING NO. header row (`id_header_row`, from _find_header_row).
      2. The upper bound is the bottom of the company/division block. A marker
         match is per-TOKEN and by SUBSTRING, not by an exact equal-string line
         match: OCR sometimes merges the whole company line into one token
         ("PARKER HANNIFIN CORPORATION") and sometimes leaves it split
         ("Parker" + "GAS TURBINE FUEL SYSTEMS DIVISION"), and either way that
         line often also carries an unrelated stray token from a neighbouring
         box (an "ES6-..." spec reference, a lone revision letter) that must
         NOT stretch the block's x-span -- so only the tokens that actually
         contain a marker set that span, not the rest of their line.
      3. Left/right bounds come from the union of the id-header row's and the
         company block's own x-span (from those marker tokens specifically), not
         the whole ID_BLOCK_REGION -- that region also contains the title
         block's other sub-boxes (Next Assy/Application/Material, tolerances,
         Engineer/Prepared sign-off lines), several of which sit in the SAME
         vertical gap between the company block and the id-header row, and are
         excluded here only by NOT overlapping this x-band.

    Name itself is not confined to one line above the header -- real title
    blocks have 3-5 unrelated rows in that gap (sign-offs, Next Assy, etc.) to
    Name's left, so every token in the vertical gap is a candidate and the
    x-band is what excludes the noise, not line adjacency.

    Returns None if any anchor can't be found -- this is the least reliable of
    the ten fields (no label to lock onto), so a miss here is expected to need
    LABEL_SYNONYMS-style tuning after a first real batch, same as any other
    field. See README.md.
    """
    if id_header_row is None:
        return None
    _idx, id_hits = id_header_row
    region_tokens = _region_tokens(tokens, page_w, page_h, ID_BLOCK_REGION)
    lines = group_into_lines(region_tokens)
    header_line = lines[_idx]
    id_header_top = min(t.y0 for t in header_line)
    id_x0 = min(x0 for x0, _x1 in id_hits.values())
    id_x1 = max(x1 for _x0, x1 in id_hits.values())

    company_bottom = None
    company_x0, company_x1 = None, None
    for line in lines:
        matched = [t for t in line
                  if any(m in norm_label(t.text) for m in COMPANY_BLOCK_MARKERS)]
        if not matched:
            continue
        bottom = max(t.y1 for t in matched)
        if company_bottom is None or bottom > company_bottom:
            company_bottom = bottom        # the block is usually >1 line; take the last one
            company_x0 = min(t.x0 for t in matched)
            company_x1 = max(t.x1 for t in matched)
    if company_bottom is None:
        return None

    margin = 6.0
    bx0 = min(id_x0, company_x0) - margin
    bx1 = max(id_x1, company_x1) + margin

    candidates = [
        t for t in region_tokens
        if bx0 <= t.cx <= bx1
        and company_bottom <= t.y0 < id_header_top
    ]
    if not candidates:
        return None
    ordered = [t for ln in group_into_lines(candidates) for t in ln]
    value = clean_value(" ".join(t.text for t in ordered))
    if not value:
        return None
    return Field(name="name", value=value, source="position",
                conf=min(t.conf for t in ordered))


# --- soft sanity checks -------------------------------------------------------

_DATE_HINT_RE = re.compile(
    r"(\d{1,4}[\-/.]\d{1,2}[\-/.]\d{1,4})"                      # 02/04/26, 2026-04-02
    r"|(\d{1,2}\s*[A-Za-z]{3,9}\s*\d{2,4})"                      # 2 APR 2026
    r"|([A-Za-z]{3,9}\s*\d{1,2},?\s*\d{2,4})"                    # APR 2, 2026
)


def apply_sanity_checks(fields: Dict[str, Field]) -> List[str]:
    """
    Flag values that look wrong without discarding them.

    A Date that matches no recognisable date shape, or an Ltr longer than a
    short token, is suspicious but may still be legitimate, so by default it is
    noted in the run log and accepted. Set STRICT_DATE_REVISION = True to clear
    such values instead, which routes the file to NEEDS_REVIEW.
    """
    notes: List[str] = []
    date_f = fields.get("date")
    if date_f and date_f.readable and not _DATE_HINT_RE.search(date_f.value):
        date_f.note = "date does not look like a date"
        notes.append("Date %r does not parse as a date" % date_f.value)
        if STRICT_DATE_REVISION:
            date_f.value = ""

    ltr_f = fields.get("ltr")
    if ltr_f and ltr_f.readable and len(ltr_f.value) > MAX_REVISION_LEN:
        ltr_f.note = "ltr longer than expected"
        notes.append("Ltr %r is longer than %d characters"
                     % (ltr_f.value, MAX_REVISION_LEN))
        if STRICT_DATE_REVISION:
            ltr_f.value = ""
    return notes


REVISIONS_TABLE_COLUMNS = ["change_no", "zone", "ltr", "description", "date", "approved"]
REVISIONS_TABLE_ANCHOR = "ltr"

ID_BLOCK_COLUMNS = ["size", "cage_code", "drawing_number"]
ID_BLOCK_ANCHOR = "cage_code"


def extract_title_block(tokens: List[Token], page_w: float,
                        page_h: float) -> Tuple[Dict[str, Field], List[str]]:
    """
    Extract all ten fields and run the soft sanity checks. Returns (fields, notes).

    Three independent lookups, since Parker's real layout has no single title
    block to split into cells the way the old 2x2 model assumed:
      1. The REVISIONS table (top-right)   -> extract_table_fields, current row.
      2. The SIZE/CAGE CODE/DRAWING NO. row (bottom-right) -> extract_table_fields.
      3. Name (bottom-right, unlabelled)   -> extract_name_field, positioned
         between the id-block row above and the company block below it.
    A field neither lookup finds is left unreadable (Field(name=name)) rather
    than guessed -- same "give up" end state the old Layer 3 had.
    """
    fields: Dict[str, Field] = {}
    fields.update(extract_table_fields(tokens, REVISIONS_TABLE_COLUMNS,
                                       REVISIONS_TABLE_ANCHOR, page_w, page_h,
                                       REVISIONS_TABLE_REGION))
    # max_rows=2: the id block is structurally ONE data row directly under its
    # header, unlike the REVISIONS table's real multi-row history. Scanning
    # further risked a later, unrelated row (the SCALE/weight block, which sits
    # lower in the same region) coincidentally landing inside an open-ended
    # column band and silently overwriting the correct row.
    # open_ends=False: Size/Cage Code/Drawing Number are always short codes, so
    # there's no long value that needs an open edge -- and leaving one open
    # let an unrelated sub-box sharing the header row's OCR line (a sheet/rev
    # reference table, "DATA BASE MODEL") get pulled into Size's value.
    fields.update(extract_table_fields(tokens, ID_BLOCK_COLUMNS, ID_BLOCK_ANCHOR,
                                       page_w, page_h, ID_BLOCK_REGION,
                                       max_rows=2, open_ends=False))

    id_block_tokens = _region_tokens(tokens, page_w, page_h, ID_BLOCK_REGION)
    id_header_row = _find_header_row(group_into_lines(id_block_tokens), ID_BLOCK_COLUMNS)
    name_field = extract_name_field(tokens, page_w, page_h, id_header_row)
    if name_field:
        fields["name"] = name_field

    for name in FIELD_ORDER:
        fields.setdefault(name, Field(name=name))
    notes = _split_merged_size_cage(fields)
    notes.extend(apply_sanity_checks(fields))
    return fields, notes


# Drawing sheet sizes per ASME Y14.1 (I is skipped, as in the standard).
SHEET_SIZES = set("ABCDEFGHJK")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def _split_merged_size_cage(fields: Dict[str, Field]) -> List[str]:
    """
    Undo OCR merging the Size cell into the Cage Code cell.

    The two cells sit almost touching, and OCR sometimes reads them as one box
    ("D2Y939", "D|09523"); the whole token then lands in Cage Code's column and
    Size comes back blank. A CAGE code is always exactly 5 characters and a
    sheet size is one letter, so when Size is blank and Cage Code is a sheet-size
    letter followed by exactly 5 characters, the split is unambiguous. Anything
    else is left alone.
    """
    size_f, cage_f = fields["size"], fields["cage_code"]
    if size_f.readable or not cage_f.readable:
        return []
    compact = _NON_ALNUM_RE.sub("", cage_f.value).upper()
    if len(compact) != 6 or compact[0] not in SHEET_SIZES:
        return []
    merged = cage_f.value
    size_f.value, size_f.source, size_f.conf = compact[0], "split", cage_f.conf
    cage_f.value = compact[1:]
    return ["Size and Cage Code were read as one token %r; split into %r / %r"
            % (merged, size_f.value, cage_f.value)]


# =============================================================================
#  TOP-LEVEL ENTRY POINT
# =============================================================================

def analyze_drawing(path, debug_dir=None, force_ocr: bool = False) -> DrawingResult:
    """
    Read one drawing PDF and extract its text and the ten fields in FIELD_ORDER.

    Never raises for a bad input file: an unreadable or corrupt PDF comes back with
    ok=False and a failure_reason, which the caller routes to FAILED/. That keeps a
    single bad file from aborting a batch of hundreds.

    Args:
        path:      the PDF to read.
        debug_dir: if given, the preprocessed image handed to OCR is written here
                   (--debug-images), which is how you see what OCR actually saw.
        force_ocr: skip the text layer and go straight to OCR. For testing the OCR
                   path against a PDF that happens to have a text layer.

    Returns:
        DrawingResult -- ok / method / full_text / tokens / fields / warnings.
    """
    import pdfplumber

    path = Path(path)
    res = DrawingResult(path=path)

    try:
        pdf_cm = pdfplumber.open(str(path))
    except Exception as exc:
        res.failure_reason = "cannot open PDF: %s: %s" % (type(exc).__name__, exc)
        return res

    try:
        with pdf_cm as pdf:
            if not pdf.pages:
                res.failure_reason = "PDF contains no pages"
                return res
            res.page_count = len(pdf.pages)
            if res.page_count > 1:
                res.warnings.append(
                    "%d-page file: only page 1 is read -- REVISIONS/title-block "
                    "fields and the classification scan both live there, and the "
                    "other pages (schedules, notes-only sheets) are typically "
                    "reference copies of a different drawing's own pages, not this "
                    "one's" % res.page_count)

            # --- Stage 2: the text layer, PAGE 1 ONLY -------------------------
            # A multi-page file's fields and classification signal are both on
            # page 1; OCR'ing every page (as this used to do) was most of the
            # cost on a large multi-page batch for no benefit.
            first = pdf.pages[0]
            page_w = float(first.width or 0) or 612.0
            page_h = float(first.height or 0) or 792.0
            page1_tokens = tokens_from_page_text(first)
            page1_text = " ".join(t.text for t in page1_tokens)
            text_chars = len(page1_text.replace(" ", ""))

            if text_chars >= MIN_TEXT_CHARS and not force_ocr:
                # --- TEXT PATH ------------------------------------------------
                res.method = "text"
                res.tokens = page1_tokens
                res.full_text = page1_text
            else:
                # --- OCR PATH -------------------------------------------------
                # No usable text layer: scanned or hand-drawn. THREE independent
                # region crops, each rendered straight from the PDF (see
                # render_region_image) rather than one full-page render sliced
                # afterward -- page 1 only, per above:
                #   1. CLASSIFICATION_CROP_REGION, coarse (run_keyword_scan) --
                #      feeds classify_text()'s keyword search.
                #   2. REVISIONS_TABLE_REGION, fine -- the REVISIONS table.
                #   3. ID_BLOCK_REGION, fine -- Name/Size/Cage Code/Drawing No.
                # Each crop is a fraction of the page, both faster to OCR than
                # the whole sheet and immune to the rest of a dense CAD drawing
                # (dimensions, GD&T callouts, notes) confusing header/keyword
                # matching by coincidence -- see extract_table_fields's own
                # `region` filter, which exists for the same reason one layer
                # further in.
                res.method = "ocr"
                if force_ocr:
                    res.warnings.append("OCR path forced")
                elif text_chars:
                    res.warnings.append(
                        "text layer held only %d characters; used OCR" % text_chars)

                cls_img, _, _ = render_region_image(first, CLASSIFICATION_CROP_REGION,
                                                    CLASSIFICATION_RENDER_DPI)
                res.full_text = run_keyword_scan(cls_img)

                all_toks: List[Token] = []
                for crop, region in (("revisions", REVISIONS_TABLE_REGION),
                                     ("id_block", ID_BLOCK_REGION)):
                    region_img, offset_x, offset_y = render_region_image(
                        first, region, FIELD_CROP_DPIS[crop][0])
                    dbg = None
                    if debug_dir is not None:
                        dbg = Path(debug_dir) / ("%s_p1_%s_preprocessed.png"
                                                 % (path.stem, crop))
                    toks, skew = _ocr_field_crop(crop, region_img, offset_x, offset_y, dbg)
                    all_toks.extend(toks)
                    if abs(skew) >= 0.15:
                        res.warnings.append(
                            "deskewed %s crop by %.2f degrees" % (crop, skew))
                res.tokens = all_toks

            if not res.full_text.strip() and not res.tokens:
                # On the OCR path these come from three independent region
                # crops (the classification scan and the two field crops);
                # check both before giving up, since some succeeding while
                # others come back empty is a real possible outcome, not a
                # failure -- e.g. a drawing with no classification legend at
                # all still has fields.
                res.failure_reason = ("no text could be extracted by either "
                                      "pdfplumber or PaddleOCR")
                return res

            # --- Stage 7: the ten REVISIONS-table / title-block fields -------
            res.fields, notes = extract_title_block(res.tokens, page_w, page_h)
            res.warnings.extend(notes)
            res.ok = True
            return res

    except Exception as exc:
        # Any unexpected failure on one file is contained here so the batch survives.
        log.debug("analyze_drawing failed for %s", path, exc_info=True)
        res.ok = False
        res.failure_reason = "%s: %s" % (type(exc).__name__, exc)
        return res
