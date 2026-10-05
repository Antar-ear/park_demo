# Engineering Drawing Classification & Extraction Pipeline

On-premises batch processing for engineering drawing PDFs. Two scripts, run
manually and independently, so classification routing can be reviewed by eye
before any extraction happens.

**Status:** Script A is built and tested. Script B is not written yet.

---

## Setup

```bash
pip install -r requirements.txt
```

### The one-time PaddleOCR model download — read this before reporting a bug

The **first** time PaddleOCR runs on a new machine it downloads its pretrained
detection and recognition models from the internet and caches them on disk
(under `%USERPROFILE%\.paddlex\official_models` on Windows). You will see lines
like `Creating model: ('PP-OCRv6_medium_det', ...)` and a download progress bar,
and that first run takes noticeably longer.

**This is a one-time setup step, not a runtime dependency.** After it completes,
every subsequent run reads the cached models from disk and the pipeline makes no
network calls at all. It is the only exception to the offline requirement.

To get it out of the way on a new laptop before a real batch, run the pipeline
once on the test drawings (below) while the machine still has internet access.

---

## Web platform (upload PDFs, review, download Excel)

```bash
python -m dp_platform          # then open http://127.0.0.1:8000
```

Sign in (default `demo` / `demo123`, set `DP_USER` / `DP_PASSWORD` to change), drop in
PDFs, and watch each one move queued -> processing -> done. Click a row to see the
drawing beside its extracted fields, correct any field, and save: the drawing is
re-routed (a corrected NEEDS_REVIEW row becomes DEFENSE / NON_DEFENSE) without re-running
OCR, and the edit is flagged in the Excel's "Manually Edited" column. Past batches are
kept and reopen from the home page.

It uses the same engine and routing as Script A, so results match. Everything runs on
this machine; data lives in `./data` (SQLite + the uploaded PDFs). Delete that folder to
reset. It binds to `127.0.0.1` only; set `DP_HOST=0.0.0.0` to expose it to the LAN.
Branding and limits are in `dp_platform/config.py` (or `DP_APP_NAME`, `DP_BRAND_COLOR`,
`DP_PORT`, `DP_MAX_UPLOAD_MB`). One worker processes drawings in order; scanned drawings
take about 80–90 seconds each (see Performance notes).

Layout: `dp_platform/main.py` (API), `worker.py` (queue), `pipeline.py` (calls the
engine), `db.py`, `auth.py`, `static/` (pages), plus `excel_export.py` (workbook).

The Excel has an *Extraction Summary* sheet (DEFENSE / NON_DEFENSE) and a *Needs
Attention* sheet (NEEDS_REVIEW / FAILED, with reasons).

---

## Folder layout

```
CenterFolder/
├── (incoming PDF drawings dropped here, unsorted)
├── DEFENSE/                    created by Script A
├── NON_DEFENSE/                created by Script A
├── NEEDS_REVIEW/               created by Script A
├── FAILED/                     created by Script A
├── script_a_run_<timestamp>.log        written by each Script A run
└── Extraction_Summary_<date>.xlsx      written by Script B
```

## The files in this project

| File | What it is |
|---|---|
| `script_a_classify.py` | **Script A** — classify and sort. Run this first. |
| `script_b_extract.py` | **Script B** — extract fields, build the Excel. *Not built yet.* |
| `drawing_common.py` | The shared extraction engine both scripts import. |
| `make_test_drawings.py` | **Dev only.** Generates 12 synthetic test drawings. |
| `requirements.txt` | Pinned dependencies. |

`drawing_common.py` exists so Script A and Script B cannot disagree. If each had
its own extraction code, Script A could pass a file as "all 4 fields readable"
and Script B would then write a blank cell for it. One engine, called twice.
The two scripts stay fully independent to *run* — Script A never calls Script B.

---

## Script A — classify & sort

```bash
python script_a_classify.py                     # uses DEFAULT_CENTER_FOLDER
python script_a_classify.py "D:\Drawings"       # explicit folder
python script_a_classify.py --dry-run           # decide and log, move NOTHING
python script_a_classify.py --debug-images      # dump what OCR actually saw
python script_a_classify.py --force-ocr         # ignore text layers, OCR everything
```

**Start with `--dry-run`.** It logs every decision and every extracted field but
moves no files, so you can review the routing before committing to it.

### Routing, in strict precedence order

| # | Condition | Folder | Filename |
|---|---|---|---|
| 1 | Cannot be opened / nothing extractable | `FAILED/` | unchanged |
| 2 | **Any** required field unreadable (`dc.REQUIRED_FIELDS`) | `NEEDS_REVIEW/` | unchanged |
| 3 | Ambiguous `Only for <unknown company>` | `NEEDS_REVIEW/` | unchanged |
| 4 | A DEFENSE keyword matched | `DEFENSE/` | `DEFENSE_<name>` |
| 5 | Nothing matched | `NON_DEFENSE/` | `NON_DEFENSE_<name>` |

Rule 2 sits above the classification rules on purpose: a missing required field
means the document was not fully understood, and a document that was not fully
understood is not classified at all — it is set aside for a human. Nothing in
this pipeline guesses a classification from a partial or low-confidence read.

**"A DEFENSE keyword matched" now includes export-control legends**, not just
"Product for Defense" / "Classified" stamps: some real Parker drawings print a
"U.S. EXPORT CONTROLLED..." (or ITAR) notice that others don't, and that's just
as deterministic a signal printed on the page. This is EXPORT-CONTROL status
specifically, not a general Military/non-military determination — that
determination hasn't been defined yet, so nothing here guesses at it. When it
is defined, its rules belong in `DEFENSE_KEYWORDS`/`COMPANY_NAMES` alongside
these, the same way every other classification signal in this file works.

### Re-run safety

- Files already named `DEFENSE_*` or `NON_DEFENSE_*` are **skipped** and left
  where they are, so a file you moved back for a second look does not become
  `DEFENSE_DEFENSE_foo.pdf`.
- A filename collision in a destination folder gets a `_1`, `_2` suffix.
  Nothing is ever overwritten — two files with one name may be real, different
  revisions.
- A file that cannot be moved (locked, open in a viewer) is logged and left in
  place; the rest of the batch continues.

---

## What to tune, and where

Everything you are likely to change is in a commented block at the top of a file.

### `script_a_classify.py` — classification policy

| Constant | What it controls |
|---|---|
| `DEFENSE_KEYWORDS` | The phrases that mark a drawing DEFENSE. Plain text, no regex needed. Spaces match any whitespace, so a phrase still matches when OCR wrapped it across lines. |
| `COMPANY_NAMES` | Company names that mean DEFENSE. **Add your real names here.** |
| `ONLY_FOR_MODE` | How to treat `Only for ...`. See below. |
| `BENIGN_ONLY_FOR_TERMS` | Drafting boilerplate after "only for" that means nothing — `reference`, `information`, and so on. Checked first, in every mode. |
| `DEFAULT_CENTER_FOLDER` | The folder used when no path is given on the command line. |

**`ONLY_FOR_MODE`** — the spec asked for both a generic `Only for <anything>`
regex and an editable company list, which conflict, since the generic regex
makes the list redundant. All three behaviours are available:

- `"list_plus_review"` **(default)** — a listed company is DEFENSE; an
  unrecognised one goes to `NEEDS_REVIEW`. Nothing is silently called
  non-defense, and no unrelated wording is silently called defense.
- `"list_only"` — only listed names count; other `Only for` text is ignored.
  Fewest reviews, but a new customer name is a silent miss until you add it.
- `"any"` — any `Only for <something>` is DEFENSE. Misses nothing, but ordinary
  boilerplate will misfire (the benign list is still applied first).

### `drawing_common.py` — extraction tuning

| Constant | What it controls |
|---|---|
| `LABEL_SYNONYMS` | **The most useful knob.** The label wordings each column header is found by. Add new ones as new drafting formats turn up (e.g. `"E.C.O. NO"` alongside `"CHANGE NO"`). |
| `REQUIRED_FIELDS` | Which fields must be readable for a drawing to route normally. A miss on any other field is logged but does not force `NEEDS_REVIEW`. |
| `COMPANY_BLOCK_MARKERS` | Words used to find the bottom of the company/division block, so the unlabelled Name field can be found positionally below it. |
| `MIN_TEXT_CHARS` | Below this many characters in the text layer, the file is treated as scanned and sent to OCR. |
| `REVISIONS_TABLE_REGION` / `ID_BLOCK_REGION` / `CLASSIFICATION_CROP_REGION` | The three page regions that get OCR'd (fractions of page width/height). **The main speed lever** — see the performance notes. Widen one if a division's layout puts its table outside it. |
| `FIELD_CROP_DPIS` | Per field crop: the resolution it's rendered and read at, and the resolution the text detector sees. Tuned to what reads the real samples correctly — re-run the samples after changing. |
| `MIN_OCR_CONFIDENCE` | OCR boxes below this are dropped as noise. |
| `CLAHE_CLIP` | Contrast boost strength; raise for washed-out scans. |
| `STRICT_DATE_REVISION` | `False` (default): a Date that does not parse, or an over-long Ltr, is flagged in the log but accepted. `True`: such a value is cleared, which routes the file to `NEEDS_REVIEW`. |

### The ten extracted fields

| Field | Source |
|---|---|
| Change No., Zone, Ltr, Description, Date, Approved | The REVISIONS table (top-right), **current (bottom-most) row only** — not the full change history. |
| Name | The title block (bottom-right); has no printed label, so it's found positionally between the company block and the Size/Cage Code/Drawing No. row. |
| Size, Cage Code, Drawing Number | The title block's bottom row. |

`Ltr`, `Name`, `Size`, `Cage Code`, and `Drawing Number` are **required**
(`REQUIRED_FIELDS`) — these identify the part and its current revision. The
rest are best-effort: extracted when readable, blank and logged when not,
never blocking routing.

### How a field is actually found

Two anchored reads, plus one positional read for the field with no label:

1. **Table-anchored** (`extract_table_fields`). Find the header row by its
   column labels (`CHANGE NO. | ZONE | LTR | DESCRIPTION | DATE | APPROVED`,
   or `SIZE | CAGE CODE | DRAWING NO.`), derive each column's x-position band
   from its header, then read the bottom-most data row under those bands —
   that row is the drawing's current state. Used for all fields except Name.
2. **Positional** (`extract_name_field`). Name has no label to anchor on: it's
   read from whatever sits between the bottom of the company/division block
   (found via `COMPANY_BLOCK_MARKERS`) and the top of the Size/Cage Code/
   Drawing No. row, bounded left/right by the title-block box so unrelated
   text elsewhere on the sheet is never pulled in.
3. **Give up.** A field neither of the above finds stays unreadable, which
   routes the file to `NEEDS_REVIEW` if it's in `REQUIRED_FIELDS`.

Name is the least reliable of the ten — it's the only one without a label to
lock onto. Expect it to need the most `LABEL_SYNONYMS`/`COMPANY_BLOCK_MARKERS`
tuning after a first real batch.

---

## Performance notes

A scanned drawing takes about **80–90 seconds** on CPU (measured on the real
samples, 8-core laptop, engine already loaded; the same drawing varies ±15%
run to run with the laptop's thermal state). Vector/CAD PDFs take well under a
second, since they never touch OCR.

Only three small regions of **page 1** are OCR'd, never the whole sheet:

| Crop | Region | Time |
|---|---|---|
| REVISIONS table (top-right) | `REVISIONS_TABLE_REGION` | ~36s |
| Classification legend (top-left) | `CLASSIFICATION_CROP_REGION` | ~27s |
| Title block: Name / Size / Cage Code / Drawing No. (bottom-right) | `ID_BLOCK_REGION` | ~16s |

Rendering, preprocessing, and field extraction are ~3s in total. Almost all the
time is PaddleOCR reading text boxes, so **cost scales with how many text boxes
a crop contains**: tightening `ID_BLOCK_REGION` from 79 boxes to 21 cut that
crop by about two-thirds. That's the main lever. Widen a region only when a
layout needs it.

Things that were measured head to head (interleaved in one process, so the
laptop's drift doesn't skew them) and **don't** help, so they aren't in the code:

- **OCR'ing the three crops in parallel.** Total time didn't change; Paddle's
  CPU inference already uses every core.
- **A separate OCR engine per crop.** No difference versus one shared engine.
- **Fewer CPU threads.** 5 threads was ~15% slower than Paddle's default of 10.
- **Lower render resolution.** Each field crop's `FIELD_CROP_DPIS` are the
  effective resolutions that read all four samples correctly; changing them
  changed which text OCR could read (it lost the lone "D" in the Size cell, and
  misread a revision letter), not just the speed.

Still worth retesting: **oneDNN is switched off.** `paddlepaddle` 3.3.1's oneDNN
CPU kernels raise `ConvertPirAttribute2RuntimeAttribute not support` on the
PP-OCR models in this build, so `enable_mkldnn=False` in
`drawing_common.get_ocr()` falls back to the slower portable CPU path. If a
`paddlepaddle` upgrade fixes that, it's likely the next big speed win.

---

## Hand-drawn drawings — what to actually expect

The pipeline preprocesses before OCR (deskew → contrast boost → binarize →
upscale) and uses PaddleOCR rather than Tesseract, both of which help materially
on hand lettering. Stating the limits plainly rather than implying they are
solved:

- **Neat hand-printed block lettering** — uppercase, drafting-style, uniform
  spacing — sees the biggest gain. On the synthetic hand-printed test scans,
  all four fields read correctly with confidence 0.96–1.00, through a 3° skew.
- **True cursive or messy freehand pencil may still fail.** When it does, the
  pipeline degrades into `NEEDS_REVIEW/` (a field could not be read) or
  `FAILED/` (nothing extractable). It does not guess, and it does not classify
  a document it could not fully read.
- **Synthetic tests cannot answer this question for you.** Files 09 and 10
  approximate neat block lettering by jittering each character's position,
  rotation and size. That genuinely exercises the preprocessing and OCR path,
  but it is not real pencil on real paper. The `NEEDS_REVIEW` share on your
  first real batch is the number that matters.

**After the first real batch**, check the run log's closing line — it reports
needs-review + failed as a share of the batch. If that share is higher than you
can absorb, in order of effort:

1. Add the label wordings you see in `NEEDS_REVIEW` files to `LABEL_SYNONYMS`.
   Cheapest fix by far, and usually the biggest win.
2. Run `--debug-images` on a few failures and look at what OCR was given (one
   image per field crop). Washed out? Raise `CLAHE_CLIP`. Table cut off at the
   edge? Widen that crop's region constant.

---

## Testing with synthetic drawings

**Stale since the real-drawing field extraction rework.** This generator still
produces the OLD 4-field layout (Drawing Number / Title / Date / Revision in a
2×2 block) that `drawing_common.py` no longer extracts by — real Parker
drawings use a REVISIONS table plus a Size/Cage Code/Drawing No. row instead
(see "The ten extracted fields" above). These 12 files still exercise the
DEFENSE/NON_DEFENSE keyword routing fine, but no longer exercise field
extraction meaningfully. Regenerating this batch for the real layout is a
reasonable follow-up; until then, test field extraction against real sample
drawings instead.

```bash
python make_test_drawings.py            # 12 test PDFs into ./CenterFolder
python make_test_drawings.py --clean    # wipe existing top-level PDFs first
python script_a_classify.py --dry-run   # review the routing
```

The batch covers every branch of the routing table:

| File | Exercises | Expected |
|---|---|---|
| `01_bracket_assembly.pdf` | vector text, inline labels, block bottom-right | `NON_DEFENSE` |
| `02_defense_housing.pdf` | `PRODUCT FOR DEFENSE` stamp | `DEFENSE` |
| `03_classified_manifold.pdf` | `CLASSIFIED` stamp | `DEFENSE` |
| `04_only_for_listed.pdf` | `Only for <listed company>` | `DEFENSE` |
| `05_only_for_reference.pdf` | benign `only for reference` must **not** fire | `NON_DEFENSE` |
| `06_only_for_unknown.pdf` | `Only for <unlisted company>` | `NEEDS_REVIEW` |
| `07_missing_revision.pdf` | Revision cell blank → strict 4-field rule | `NEEDS_REVIEW` |
| `08_topleft_stacked_block.pdf` | block in the **top-left**, stacked cells, different label wording | `NON_DEFENSE` |
| `09_scan_handdrawn_shaft.pdf` | rasterised, 3° skew, hand-printed, no text layer | `NON_DEFENSE` via OCR |
| `10_scan_defense_valve.pdf` | same plus a defense stamp | `DEFENSE` via OCR |
| `11_corrupt_truncated.pdf` | truncated bytes | `FAILED` |
| `12_blank_no_text.pdf` | geometry only, no text anywhere | `FAILED` |

File 08 is the one that matters most for real batches: its title block is in a
different corner, uses stacked cells, and uses entirely different label wording
(`DRG NO` / `DESCRIPTION` / `ISSUE DATE` / `REV NO`). It proves field finding
follows labels rather than assuming one house style.

All 12 route as expected as of the last test run, which takes about 170 seconds
(the three OCR files account for nearly all of it).

---

## Script B — extract & build Excel

Not built yet. When it is, it will read **only** `DEFENSE/` and `NON_DEFENSE/`
— `NEEDS_REVIEW/` and `FAILED/` are excluded entirely, to be resolved by hand
first — and write `Extraction_Summary_<date>.xlsx` to the `CenterFolder` root
with one row per document: Filename, then the ten fields in `dc.FIELD_ORDER`
(Change No., Zone, Ltr, Description, Date, Approved, Name, Size, Cage Code,
Drawing Number), Classification, Extraction Method, Source Subfolder, Date
Processed.
