"""
make_test_drawings.py  --  DEVELOPMENT / TESTING ONLY
=====================================================
Generates a set of synthetic engineering drawings that exercise every branch of
Script A's routing, so the pipeline can be validated before real drawings arrive.

This script is NOT part of the production pipeline. It is the only file that needs
`reportlab`, and it is the only place where an internet-free "sample batch" comes
from. Delete it (and reportlab) on the client laptop if you prefer.

    python make_test_drawings.py                      # writes into ./CenterFolder
    python make_test_drawings.py --folder D:\\Drawings  # somewhere else
    python make_test_drawings.py --clean              # wipe existing test files first

What gets generated, and what each file is FOR
----------------------------------------------
    01_bracket_assembly.pdf        vector text, inline labels, block bottom-right
                                   -> NON_DEFENSE
    02_defense_housing.pdf         carries "PRODUCT FOR DEFENSE"
                                   -> DEFENSE
    03_classified_manifold.pdf     carries "CLASSIFIED"
                                   -> DEFENSE
    04_only_for_listed.pdf         "Only for <a company on the list>"
                                   -> DEFENSE
    05_only_for_reference.pdf      "Only for reference" -- benign drafting
                                   boilerplate that must NOT trigger DEFENSE
                                   -> NON_DEFENSE
    06_only_for_unknown.pdf        "Only for <company NOT on the list>"
                                   -> NEEDS_REVIEW  (ambiguous, needs your eyes)
    07_missing_revision.pdf        Revision cell label present but value blank
                                   -> NEEDS_REVIEW  (strict 4-field rule)
    08_topleft_stacked_block.pdf   title block in the TOP-LEFT, stacked cells,
                                   different label wording (DRG NO / DESCRIPTION /
                                   ISSUE DATE / REV NO) -- proves field finding is
                                   label-driven, not position-driven
                                   -> NON_DEFENSE
    09_scan_handdrawn_shaft.pdf    rasterised at 200 DPI and skewed 3 degrees, no
                                   text layer, hand-printed lettering
                                   -> NON_DEFENSE via the OCR path
    10_scan_defense_valve.pdf      same, plus a defense stamp
                                   -> DEFENSE via the OCR path
    11_corrupt_truncated.pdf       deliberately truncated bytes
                                   -> FAILED
    12_blank_no_text.pdf           geometry only, no text anywhere
                                   -> FAILED  (nothing extractable)

HONEST LIMITATION
-----------------
The "hand-drawn" files (09, 10) approximate hand printing by jittering each
character's position, rotation and size. That is a fair stand-in for neat drafting
block lettering, and it genuinely exercises the deskew + binarize + upscale + OCR
path. It CANNOT reproduce real cursive or messy freehand pencil. Those must be
validated on your actual scans -- synthetic data cannot tell you that answer.
"""

from __future__ import annotations

import argparse
import io
import math
import random
import shutil
from pathlib import Path

from reportlab.lib.pagesizes import landscape, A3
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

# Fixed seed: the generated batch must be identical run to run, or a routing
# regression cannot be told apart from a different random drawing.
SEED = 20260917

PAGE = landscape(A3)
PAGE_W, PAGE_H = PAGE

# A company that IS on Script A's COMPANY_NAMES list, and one that is not.
LISTED_COMPANY = "Acme Aerospace Systems"
UNLISTED_COMPANY = "Vertex Dynamics Ltd"


# =============================================================================
#  DRAWING PRIMITIVES
# =============================================================================

def draw_frame(c: rl_canvas.Canvas) -> None:
    """The sheet border and zone ticks every drawing sheet carries."""
    c.setLineWidth(1.6)
    c.rect(10 * mm, 10 * mm, PAGE_W - 20 * mm, PAGE_H - 20 * mm)
    c.setLineWidth(0.4)
    c.rect(13 * mm, 13 * mm, PAGE_W - 26 * mm, PAGE_H - 26 * mm)
    for i in range(1, 8):
        x = 13 * mm + i * (PAGE_W - 26 * mm) / 8.0
        c.line(x, 10 * mm, x, 13 * mm)
        c.line(x, PAGE_H - 10 * mm, x, PAGE_H - 13 * mm)


def draw_geometry(c: rl_canvas.Canvas, seed: int, with_text: bool = True) -> None:
    """
    Plausible-looking drawing content: views, holes, centre lines, dimensions.

    None of this is read by the pipeline. It exists so the title block is
    surrounded by realistic visual noise rather than sitting on blank paper --
    which is exactly the condition that makes OCR and box detection hard.
    """
    rnd = random.Random(seed)
    cx, cy = PAGE_W * 0.38, PAGE_H * 0.58

    # Front view: a plate with holes.
    pw, ph = 150 * mm, 90 * mm
    c.setLineWidth(1.2)
    c.rect(cx - pw / 2, cy - ph / 2, pw, ph)
    c.setLineWidth(0.5)
    c.setDash(6, 3)
    c.line(cx - pw / 2 - 8 * mm, cy, cx + pw / 2 + 8 * mm, cy)
    c.line(cx, cy - ph / 2 - 8 * mm, cx, cy + ph / 2 + 8 * mm)
    c.setDash()
    for i in range(4):
        hx = cx - pw / 2 + (i + 1) * pw / 5.0
        hy = cy + rnd.choice([-1, 1]) * ph * 0.22
        r = rnd.choice([5, 6, 8]) * mm
        c.setLineWidth(1.0)
        c.circle(hx, hy, r)
        c.setLineWidth(0.4)
        c.setDash(4, 2)
        c.line(hx - r - 4 * mm, hy, hx + r + 4 * mm, hy)
        c.line(hx, hy - r - 4 * mm, hx, hy + r + 4 * mm)
        c.setDash()

    # Section view: a stepped shaft.
    sx, sy = PAGE_W * 0.70, PAGE_H * 0.70
    c.setLineWidth(1.2)
    c.rect(sx, sy, 70 * mm, 26 * mm)
    c.rect(sx + 70 * mm, sy + 6 * mm, 45 * mm, 14 * mm)
    c.setLineWidth(0.4)
    for i in range(14):                      # section hatching
        ox = sx + i * 5 * mm
        c.line(ox, sy, ox + 26 * mm, sy + 26 * mm)

    # Dimension lines with arrow ticks.
    c.setLineWidth(0.5)
    dy = cy - ph / 2 - 16 * mm
    c.line(cx - pw / 2, dy, cx + pw / 2, dy)
    for ex in (cx - pw / 2, cx + pw / 2):
        c.line(ex, dy - 2.5 * mm, ex, dy + 2.5 * mm)
    if with_text:
        # Omitted for the textless test case: a dimension value is still text, and
        # OCR would find it, which would route the file to NEEDS_REVIEW instead of
        # the FAILED path that case is meant to exercise.
        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, dy + 1.5 * mm, "150")


def draw_notes(c: rl_canvas.Canvas, notes, top_offset_mm: float = 30.0) -> None:
    """
    The general-notes block, where classification statements usually live.

    `top_offset_mm` is pushed down when the title block sits in the top-left
    corner, so the two never overlap. Overlapping text is not a realistic drawing
    and it corrupts extraction for an uninteresting reason -- pdfplumber merges
    glyphs that share the same space into nonsense words.
    """
    x, y = 20 * mm, PAGE_H - top_offset_mm * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x, y, "NOTES:")
    c.setFont("Helvetica", 8.5)
    for i, line in enumerate(notes):
        c.drawString(x, y - (i + 1) * 5.2 * mm, line)


def draw_stamp(c: rl_canvas.Canvas, text: str) -> None:
    """A boxed classification stamp, top-right, as a real drawing would carry it."""
    c.setFont("Helvetica-Bold", 13)
    w = c.stringWidth(text, "Helvetica-Bold", 13)
    x, y = PAGE_W - 25 * mm - w, PAGE_H - 34 * mm
    c.setLineWidth(1.4)
    c.rect(x - 4 * mm, y - 2.5 * mm, w + 8 * mm, 10 * mm)
    c.drawString(x, y, text)


# =============================================================================
#  TITLE BLOCK
# =============================================================================
#
#  A 2x2 grid:  top-left = Drawing Number   top-right = Title
#               bottom-left = Date          bottom-right = Revision
#
#  Two rendering styles, because real sheets use both:
#     "inline"  -- "DWG NO: 12345-A"      (label and value on one line)
#     "stacked" -- "DRG NO" / "12345-A"   (label above its value)
#
#  Two placements, to prove the extractor does not depend on position.
# =============================================================================

TB_W, TB_H = 170 * mm, 30 * mm


def _tb_origin(where: str):
    """Bottom-left corner of the title block, in reportlab (y-up) coordinates."""
    if where == "bottom-right":
        return PAGE_W - 13 * mm - TB_W, 13 * mm
    if where == "top-left":
        return 13 * mm, PAGE_H - 13 * mm - TB_H
    raise ValueError("unknown title-block placement: %r" % where)


def draw_title_block(c: rl_canvas.Canvas, values: dict, labels: dict,
                     where: str = "bottom-right", style: str = "inline",
                     hand: bool = False) -> None:
    """
    Draw the 2x2 title block.

    `values` keys: drawing_number, title, date, revision. An empty string leaves
    that cell's value blank but still prints its label -- which is exactly the
    07_missing_revision.pdf case that must route to NEEDS_REVIEW.
    """
    ox, oy = _tb_origin(where)
    cw, ch = TB_W / 2.0, TB_H / 2.0

    c.setLineWidth(1.4)
    c.rect(ox, oy, TB_W, TB_H)
    c.setLineWidth(0.9)
    c.line(ox, oy + ch, ox + TB_W, oy + ch)     # horizontal divider
    c.line(ox + cw, oy, ox + cw, oy + TB_H)     # vertical divider

    # (row, col) -> cell origin. Row "top" is the upper half of the box.
    cells = {
        ("top", "left"): (ox, oy + ch),
        ("top", "right"): (ox + cw, oy + ch),
        ("bottom", "left"): (ox, oy),
        ("bottom", "right"): (ox + cw, oy),
    }
    mapping = {
        ("top", "left"): "drawing_number",
        ("top", "right"): "title",
        ("bottom", "left"): "date",
        ("bottom", "right"): "revision",
    }

    for key, field in mapping.items():
        x, y = cells[key]
        label = labels[field]
        value = values.get(field, "")
        if style == "inline":
            text = "%s: %s" % (label, value) if value else "%s:" % label
            _put(c, x + 3 * mm, y + ch / 2 - 1.5 * mm, text, 9.5, hand)
        else:
            _put(c, x + 3 * mm, y + ch - 5.0 * mm, label, 7.5, hand)
            if value:
                _put(c, x + 3 * mm, y + 2.2 * mm, value, 10.5, hand)


def _put(c: rl_canvas.Canvas, x: float, y: float, text: str,
         size: float, hand: bool) -> None:
    """Draw text either as clean CAD lettering or as jittered hand printing."""
    if hand:
        draw_hand_text(c, x, y, text, size)
    else:
        c.setFont("Helvetica-Bold", size)
        c.drawString(x, y, text)


def draw_hand_text(c: rl_canvas.Canvas, x: float, y: float, text: str,
                   size: float) -> None:
    """
    Approximate hand-printed drafting lettering.

    Each character is placed individually with a small random offset, rotation and
    size wobble, which is what distinguishes hand printing from typeset text as far
    as an OCR engine is concerned. Uppercase throughout, as drafting convention
    requires. Seeded per string so output is reproducible.

    This models NEAT block lettering. It does not model cursive or messy freehand.
    """
    rnd = random.Random(hash(text) & 0xFFFF)
    cur = x
    for ch in text.upper():
        if ch == " ":
            cur += size * 0.34
            continue
        s = size * rnd.uniform(0.94, 1.10)
        dy = rnd.uniform(-0.055, 0.055) * size
        rot = rnd.uniform(-3.2, 3.2)
        c.saveState()
        c.translate(cur, y + dy)
        c.rotate(rot)
        c.setFont("Helvetica", s)
        c.drawString(0, 0, ch)
        c.restoreState()
        cur += c.stringWidth(ch, "Helvetica", s) * rnd.uniform(1.02, 1.16)


# =============================================================================
#  SHEET ASSEMBLY
# =============================================================================

# Label wording variants. The "alt" set uses entirely different synonyms, which is
# how 08_topleft_stacked_block.pdf proves that extraction follows labels rather
# than assuming one house style.
LABELS_STD = {"drawing_number": "DWG NO", "title": "TITLE",
              "date": "DATE", "revision": "REV"}
LABELS_ALT = {"drawing_number": "DRG NO", "title": "DESCRIPTION",
              "date": "ISSUE DATE", "revision": "REV NO"}

BASE_NOTES = [
    "1. ALL DIMENSIONS IN MILLIMETRES UNLESS STATED OTHERWISE.",
    "2. REMOVE ALL BURRS AND SHARP EDGES.",
    "3. MATERIAL: AL 6061-T6 UNLESS NOTED.",
    "4. SURFACE FINISH Ra 3.2 MICROMETRES.",
]


def build_sheet(path: Path, values: dict, *, labels=None, notes=None,
                stamp=None, where="bottom-right", style="inline",
                hand=False, geometry_seed=1, with_text=True) -> None:
    """Render one complete vector drawing sheet to `path`."""
    labels = labels or LABELS_STD
    c = rl_canvas.Canvas(str(path), pagesize=PAGE)
    draw_frame(c)
    draw_geometry(c, geometry_seed, with_text=with_text)
    if with_text:
        # Clear the notes block past a top-left title block (13mm margin + 30mm
        # block + a 6mm gap) so the two never draw over each other.
        notes_top = 52.0 if where == "top-left" else 30.0
        draw_notes(c, notes if notes is not None else BASE_NOTES, notes_top)
        if stamp:
            draw_stamp(c, stamp)
        draw_title_block(c, values, labels, where=where, style=style, hand=hand)
    c.showPage()
    c.save()


def rasterise_and_skew(src: Path, dst: Path, dpi: int = 200,
                       skew_deg: float = 3.0) -> None:
    """
    Turn a vector PDF into an image-only PDF, tilted like a real scan.

    This is what forces Script A down the OCR path: the output carries no text
    layer at all, so pdfplumber finds nothing and the preprocess-then-PaddleOCR
    fallback has to do the work -- including undoing this very skew.
    """
    import pdfplumber
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    with pdfplumber.open(str(src)) as pdf:
        page = pdf.pages[0]
        pil = page.to_image(resolution=dpi).original.convert("L")

    rotated = pil.rotate(skew_deg, resample=Image.BICUBIC,
                         expand=True, fillcolor=255)
    buf = io.BytesIO()
    rotated.save(buf, format="PNG")
    buf.seek(0)

    c = rl_canvas.Canvas(str(dst), pagesize=PAGE)
    # Fit the tilted raster inside the page, preserving aspect ratio.
    iw, ih = rotated.size
    scale = min(PAGE_W / iw, PAGE_H / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(ImageReader(buf), (PAGE_W - dw) / 2.0, (PAGE_H - dh) / 2.0,
                width=dw, height=dh)
    c.showPage()
    c.save()


def make_corrupt(path: Path, tmp_dir: Path) -> None:
    """
    Write a genuinely unreadable PDF.

    A valid sheet is generated, then the trailer and xref are cut off and the
    header is damaged. pdfplumber cannot open the result, which is precisely the
    FAILED/ case -- a file the pipeline must set aside rather than guess at.
    """
    tmp = tmp_dir / "_corrupt_source.pdf"
    build_sheet(tmp, {"drawing_number": "CR-0001", "title": "CORRUPT TEST",
                      "date": "2026-05-04", "revision": "A"})
    data = tmp.read_bytes()
    truncated = bytearray(data[: int(len(data) * 0.55)])
    truncated[0:8] = b"%PDF-XX\n"
    path.write_bytes(bytes(truncated))
    tmp.unlink(missing_ok=True)


# =============================================================================
#  THE TEST BATCH
# =============================================================================

def generate(folder: Path, clean: bool = False) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    tmp_dir = folder / "_tmp_generator"
    tmp_dir.mkdir(exist_ok=True)

    if clean:
        for p in folder.glob("*.pdf"):
            p.unlink()

    random.seed(SEED)
    made = []

    def note(path: Path, expected: str) -> None:
        made.append((path.name, expected))

    # --- 01: the plain baseline --------------------------------------------
    p = folder / "01_bracket_assembly.pdf"
    build_sheet(p, {"drawing_number": "AC-41207-B", "title": "BRACKET ASSEMBLY",
                    "date": "2026-04-02", "revision": "B"}, geometry_seed=11)
    note(p, "NON_DEFENSE")

    # --- 02: explicit defense statement ------------------------------------
    p = folder / "02_defense_housing.pdf"
    build_sheet(p, {"drawing_number": "AC-55810-C", "title": "ACTUATOR HOUSING",
                    "date": "2026-03-18", "revision": "C"},
                stamp="PRODUCT FOR DEFENSE", geometry_seed=12)
    note(p, "DEFENSE (Product for Defense)")

    # --- 03: the CLASSIFIED keyword ----------------------------------------
    p = folder / "03_classified_manifold.pdf"
    build_sheet(p, {"drawing_number": "AC-60934-A", "title": "HYDRAULIC MANIFOLD",
                    "date": "2026-01-29", "revision": "A"},
                stamp="CLASSIFIED", geometry_seed=13)
    note(p, "DEFENSE (Classified)")

    # --- 04: "Only for" a company on the list ------------------------------
    p = folder / "04_only_for_listed.pdf"
    build_sheet(p, {"drawing_number": "AC-33115-D", "title": "SEAL RETAINER PLATE",
                    "date": "2026-02-11", "revision": "D"},
                notes=BASE_NOTES + ["5. ONLY FOR %s. DO NOT DISTRIBUTE."
                                    % LISTED_COMPANY.upper()],
                geometry_seed=14)
    note(p, "DEFENSE (Only for <listed company>)")

    # --- 05: benign "only for" wording that must NOT trigger ---------------
    p = folder / "05_only_for_reference.pdf"
    build_sheet(p, {"drawing_number": "AC-22640-A", "title": "COVER PANEL",
                    "date": "2026-05-06", "revision": "A"},
                notes=BASE_NOTES + ["5. THIS VIEW IS ONLY FOR REFERENCE."],
                geometry_seed=15)
    note(p, "NON_DEFENSE (benign 'only for' must not fire)")

    # --- 06: "Only for" an unrecognised company ----------------------------
    p = folder / "06_only_for_unknown.pdf"
    build_sheet(p, {"drawing_number": "AC-71002-B", "title": "PUMP END CAP",
                    "date": "2026-06-23", "revision": "B"},
                notes=BASE_NOTES + ["5. ONLY FOR %s." % UNLISTED_COMPANY.upper()],
                geometry_seed=16)
    note(p, "NEEDS_REVIEW (ambiguous 'Only for')")

    # --- 07: a blank Revision cell -> the strict 4-field rule --------------
    p = folder / "07_missing_revision.pdf"
    build_sheet(p, {"drawing_number": "AC-48820-A", "title": "SPACER RING",
                    "date": "2026-04-15", "revision": ""}, geometry_seed=17)
    note(p, "NEEDS_REVIEW (Revision unreadable)")

    # --- 08: different corner, different wording, stacked cells ------------
    p = folder / "08_topleft_stacked_block.pdf"
    build_sheet(p, {"drawing_number": "VD-9004-2", "title": "GUIDE RAIL SUPPORT",
                    "date": "12 MAR 2026", "revision": "02"},
                labels=LABELS_ALT, where="top-left", style="stacked",
                geometry_seed=18)
    note(p, "NON_DEFENSE (proves label-driven, position-independent extraction)")

    # --- 09: hand-printed, rasterised, skewed -> OCR path ------------------
    src = tmp_dir / "_09_vector.pdf"
    build_sheet(src, {"drawing_number": "HD-1042", "title": "DRIVE SHAFT",
                      "date": "2026-02-27", "revision": "A"},
                hand=True, geometry_seed=19)
    p = folder / "09_scan_handdrawn_shaft.pdf"
    rasterise_and_skew(src, p, dpi=200, skew_deg=3.0)
    src.unlink(missing_ok=True)
    note(p, "NON_DEFENSE via OCR path")

    # --- 10: hand-printed scan carrying a defense stamp --------------------
    src = tmp_dir / "_10_vector.pdf"
    build_sheet(src, {"drawing_number": "HD-2077", "title": "RELIEF VALVE BODY",
                      "date": "2026-03-09", "revision": "B"},
                stamp="PRODUCT FOR DEFENSE", hand=True, geometry_seed=20)
    p = folder / "10_scan_defense_valve.pdf"
    rasterise_and_skew(src, p, dpi=200, skew_deg=-2.4)
    src.unlink(missing_ok=True)
    note(p, "DEFENSE via OCR path")

    # --- 11: corrupt file ---------------------------------------------------
    p = folder / "11_corrupt_truncated.pdf"
    make_corrupt(p, tmp_dir)
    note(p, "FAILED (unreadable)")

    # --- 12: geometry only, no text at all ---------------------------------
    src = tmp_dir / "_12_vector.pdf"
    build_sheet(src, {}, with_text=False, geometry_seed=21)
    p = folder / "12_blank_no_text.pdf"
    rasterise_and_skew(src, p, dpi=200, skew_deg=0.0)
    src.unlink(missing_ok=True)
    note(p, "FAILED (nothing extractable)")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return made


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate synthetic engineering drawings for testing the pipeline.")
    ap.add_argument("--folder", default="CenterFolder",
                    help="where to write the PDFs (default: ./CenterFolder)")
    ap.add_argument("--clean", action="store_true",
                    help="delete existing top-level PDFs in that folder first")
    args = ap.parse_args()

    folder = Path(args.folder).resolve()
    made = generate(folder, clean=args.clean)

    print("\nGenerated %d test drawings in %s\n" % (len(made), folder))
    width = max(len(n) for n, _ in made)
    print("  %-*s  EXPECTED ROUTING" % (width, "FILE"))
    print("  %s  %s" % ("-" * width, "-" * 52))
    for name, expected in made:
        print("  %-*s  %s" % (width, name, expected))
    print("\nNote: files 09 and 10 approximate NEAT hand-printed block lettering.")
    print("Real cursive/freehand pencil can only be validated on your own scans.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
