"""
script_a_classify.py  --  SCRIPT A: classify & sort
===================================================
Reads every PDF at the top level of the center folder, works out whether it is a
DEFENSE drawing, checks that the REQUIRED fields (see dc.REQUIRED_FIELDS -- Ltr,
Name, Size, Cage Code, Drawing Number) can actually be read, and moves each file
into one of four subfolders.

Run it manually. It never calls Script B -- the whole point of the two-script split
is that you get to eyeball the routing before any extraction happens.

    python script_a_classify.py                        # uses DEFAULT_CENTER_FOLDER
    python script_a_classify.py "D:\\Drawings"          # explicit folder
    python script_a_classify.py --dry-run              # decide, log, move NOTHING
    python script_a_classify.py --debug-images         # dump what OCR actually saw
    python script_a_classify.py --force-ocr            # ignore text layers, OCR all

Routing, in strict precedence order
-----------------------------------
    1. file cannot be opened / nothing extractable  -> FAILED/       name unchanged
    2. ANY required field unreadable                -> NEEDS_REVIEW/  name unchanged
    3. an ambiguous "Only for <unknown>" statement  -> NEEDS_REVIEW/  name unchanged
    4. a DEFENSE keyword matched                    -> DEFENSE/       DEFENSE_<name>
    5. otherwise                                    -> NON_DEFENSE/   NON_DEFENSE_<name>

Rule 2 sits above the classification rules deliberately: a missing REQUIRED field
means the document was not fully understood, and a document that was not fully
understood is not classified at all. It is set aside for a human instead. The
other five fields (Change No., Zone, Description, Date, Approved) are best-effort
-- a miss there is logged but does not block routing.

OFFLINE NOTE: no network calls at runtime. PaddleOCR's first run on a new machine
downloads and caches its models -- a one-time setup step. See README.md.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import drawing_common as dc


# #############################################################################
# #                                                                           #
# #   CONFIGURATION  --  everything you are likely to tune lives here         #
# #                                                                           #
# #############################################################################

# --- Where the drawings are ---------------------------------------------------
# Overridden by the folder argument on the command line, if given.
DEFAULT_CENTER_FOLDER = r"CenterFolder"

# --- DEFENSE keywords ---------------------------------------------------------
# The drawing STATES its own classification as text on the page; nothing here is
# inferred. A match on any entry below marks the drawing DEFENSE.
#
# Matching is case-insensitive, and each entry's spaces are treated as "one or
# more whitespace characters" -- so "Product for Defense" also matches
# "PRODUCT  FOR\nDEFENSE", which matters because OCR line-wraps unpredictably.
#
# ADD NEW KEYWORDS HERE. Plain text, no regex syntax needed.
#
# The export-control entries below were added after inspecting real Parker
# samples: some carry a printed "U.S. EXPORT CONTROLLED..." legend (citing the
# Export Administration Act/Regulations) that others don't -- a drawing that
# says so is routed as DEFENSE the same deterministic way a "Product for
# Defense" stamp already is. This is EXPORT-CONTROL status specifically (EAR or
# ITAR wording), not a general Military/non-military determination -- that
# determination hasn't been defined yet (see README.md).
DEFENSE_KEYWORDS: List[str] = [
    "Product for Defense",
    "Classified",
    "Export Controlled",
    "Export Administration Regulations",
    "International Traffic in Arms",
    "ITAR",
]

# --- "Only for <company>" ------------------------------------------------------
# Company names that make a drawing DEFENSE when the page says "Only for <name>".
#
# ADD YOUR COMPANY NAMES HERE. Case-insensitive; internal spacing is flexible.
COMPANY_NAMES: List[str] = [
    "Acme Aerospace Systems",
]

# How to treat an "Only for ..." statement. Three behaviours, because the trade-off
# between missed classifications and false alarms is a business decision:
#
#   "list_plus_review"  (DEFAULT, recommended)
#         "Only for <a name in COMPANY_NAMES>"  -> DEFENSE
#         "Only for <anything else>"            -> NEEDS_REVIEW
#       Nothing is silently called non-defense, and no unrelated wording is
#       silently called defense. Unknown companies come to you instead.
#
#   "list_only"
#         Only the names in COMPANY_NAMES count. Any other "Only for ..." is
#         ignored entirely. Fewest reviews; a new customer name is a silent miss
#         until you add it to the list above.
#
#   "any"
#         Any "Only for <something>" is DEFENSE. Misses nothing, but ordinary
#         drafting boilerplate like "only for reference" will misfire (the benign
#         list below is still applied first).
ONLY_FOR_MODE = "list_plus_review"

# Ordinary drafting phrases that follow "only for" and mean nothing about
# classification. These are checked FIRST, in every mode, so "THIS VIEW IS ONLY
# FOR REFERENCE" never reaches the classification logic.
#
# ADD MORE BENIGN PHRASES HERE as you see them in real drawings.
BENIGN_ONLY_FOR_TERMS: List[str] = [
    "reference",
    "information",
    "illustration",
    "quotation",
    "estimation",
    "internal use",
    "clarity",
    "assembly purposes",
]

# How much text after "Only for" to consider when looking for a company name.
ONLY_FOR_WINDOW = 80

# --- Output folder names ------------------------------------------------------
FOLDER_DEFENSE = "DEFENSE"
FOLDER_NON_DEFENSE = "NON_DEFENSE"
FOLDER_NEEDS_REVIEW = "NEEDS_REVIEW"
FOLDER_FAILED = "FAILED"
ALL_FOLDERS = [FOLDER_DEFENSE, FOLDER_NON_DEFENSE, FOLDER_NEEDS_REVIEW, FOLDER_FAILED]

# Filename prefixes applied on the two classified routes.
PREFIX_DEFENSE = "DEFENSE_"
PREFIX_NON_DEFENSE = "NON_DEFENSE_"

# #############################################################################
# #                        END OF CONFIGURATION                               #
# #############################################################################


log = logging.getLogger("script_a")


# =============================================================================
#  CLASSIFICATION  (the only policy in the pipeline)
# =============================================================================

def _flexible_pattern(phrase: str) -> str:
    """
    Turn a plain-text keyword into a whitespace-tolerant regex.

    Each space becomes \\s+ so a phrase still matches when OCR wrapped it across
    lines or doubled a space. The words themselves are escaped, so a keyword can
    contain regex metacharacters safely.
    """
    words = [re.escape(w) for w in phrase.split()]
    return r"\s+".join(words)


# Compiled once at import: one pattern per keyword, and one for the company names.
_KEYWORD_PATTERNS = [(kw, re.compile(_flexible_pattern(kw), re.IGNORECASE))
                     for kw in DEFENSE_KEYWORDS]
_COMPANY_PATTERNS = [(name, re.compile(_flexible_pattern(name), re.IGNORECASE))
                     for name in COMPANY_NAMES]
_BENIGN_PATTERNS = [(t, re.compile(r"^\W*" + _flexible_pattern(t), re.IGNORECASE))
                    for t in BENIGN_ONLY_FOR_TERMS]
_ONLY_FOR_RE = re.compile(r"only\s+for\b", re.IGNORECASE)


class Classification:
    """
    The outcome of classifying one drawing's text.

    Attributes:
        is_defense: a DEFENSE keyword or a listed company matched.
        matches:    human-readable reasons, for the run log.
        ambiguous:  "Only for <unrecognised>" statements needing a human look.
    """

    def __init__(self) -> None:
        self.is_defense: bool = False
        self.matches: List[str] = []
        self.ambiguous: List[str] = []

    @property
    def label(self) -> str:
        return FOLDER_DEFENSE if self.is_defense else FOLDER_NON_DEFENSE


def classify_text(text: str) -> Classification:
    """
    Decide DEFENSE vs NON_DEFENSE from the drawing's own text.

    Deterministic keyword matching only -- no scoring, no inference. If the page
    does not say it, this function does not conclude it.
    """
    out = Classification()
    if not text:
        return out

    # --- plain keywords --------------------------------------------------
    for kw, pattern in _KEYWORD_PATTERNS:
        if pattern.search(text):
            out.is_defense = True
            out.matches.append('keyword "%s"' % kw)

    # --- a listed company named anywhere on the page ---------------------
    # A drawing that names a defense customer is DEFENSE whether or not the words
    # "only for" happen to precede the name.
    for name, pattern in _COMPANY_PATTERNS:
        if pattern.search(text):
            out.is_defense = True
            out.matches.append('company "%s"' % name)

    # --- "Only for ..." statements ---------------------------------------
    for m in _ONLY_FOR_RE.finditer(text):
        tail = text[m.end(): m.end() + ONLY_FOR_WINDOW]
        # For the log, keep only the clause that follows "only for" -- cut at the
        # first sentence break so the reason line names the company rather than
        # trailing off into whatever else happens to sit nearby on the sheet.
        clause = re.split(r"[.;\n]", tail, maxsplit=1)[0]
        snippet = " ".join(clause.split())[:48] or "?"

        # Benign drafting boilerplate is discarded first, in every mode.
        if any(p.match(tail) for _, p in _BENIGN_PATTERNS):
            continue

        # A listed company in the tail is already covered by the company scan
        # above; recording it here would just duplicate the log entry.
        if any(p.search(tail) for _, p in _COMPANY_PATTERNS):
            continue

        if ONLY_FOR_MODE == "any":
            out.is_defense = True
            out.matches.append('"Only for %s"' % snippet)
        elif ONLY_FOR_MODE == "list_plus_review":
            out.ambiguous.append('"Only for %s" (company not in COMPANY_NAMES)'
                                 % snippet)
        elif ONLY_FOR_MODE == "list_only":
            pass                                    # deliberately ignored
        else:
            raise ValueError("ONLY_FOR_MODE is set to an unknown value: %r"
                             % ONLY_FOR_MODE)
    return out


# =============================================================================
#  ROUTING
# =============================================================================

class Decision:
    """One file's routing decision, with the reasoning kept for the log."""

    def __init__(self, path: Path, folder: str, new_name: str,
                 reason: str, result=None, cls=None) -> None:
        self.path = path
        self.folder = folder
        self.new_name = new_name
        self.reason = reason
        self.result = result
        self.cls = cls
        self.moved_to: Optional[Path] = None
        self.error: Optional[str] = None


def decide_route(path: Path, result: dc.DrawingResult) -> Decision:
    """
    Apply the routing table. See this module's docstring for the precedence.

    Nothing here guesses. Every branch is driven either by a hard read failure or
    by text the drawing itself carries.
    """
    # 1. Unreadable file -- never classified, never renamed.
    if not result.ok:
        return Decision(path, FOLDER_FAILED, path.name,
                        "unreadable: %s" % (result.failure_reason or "unknown"),
                        result)

    cls = classify_text(result.full_text)

    # 2. The required-field rule, applied BEFORE classification. Only
    # dc.REQUIRED_FIELDS block routing; a miss on a best-effort field (Change
    # No., Zone, Description, Date, Approved) is still shown in the per-file
    # log (see log_file_detail) but does not send the file to NEEDS_REVIEW.
    missing = result.missing_required_fields
    if missing:
        return Decision(path, FOLDER_NEEDS_REVIEW, path.name,
                        "unreadable required field(s): %s"
                        % ", ".join(dc.FIELD_LABELS[m] for m in missing),
                        result, cls)

    # 3. A definite DEFENSE statement outranks an ambiguous one.
    if cls.is_defense:
        return Decision(path, FOLDER_DEFENSE, PREFIX_DEFENSE + path.name,
                        "DEFENSE: " + "; ".join(cls.matches), result, cls)

    # 4. Ambiguous "Only for <unknown>": not called non-defense by default.
    if cls.ambiguous:
        return Decision(path, FOLDER_NEEDS_REVIEW, path.name,
                        "ambiguous classification: " + "; ".join(cls.ambiguous),
                        result, cls)

    # 5. Nothing matched.
    return Decision(path, FOLDER_NON_DEFENSE, PREFIX_NON_DEFENSE + path.name,
                    "no DEFENSE keyword matched", result, cls)


# =============================================================================
#  FILE HANDLING
# =============================================================================

def ensure_subfolders(center: Path) -> None:
    """Create the four destination folders if they are not already there."""
    for name in ALL_FOLDERS:
        (center / name).mkdir(parents=True, exist_ok=True)


def list_input_pdfs(center: Path) -> Tuple[List[Path], List[Path]]:
    """
    Collect the PDFs to process from the TOP LEVEL of the center folder only.

    Files that already carry a classification prefix are set aside rather than
    reprocessed: that is what a file looks like when it has been moved back for a
    second look, and re-prefixing it would produce DEFENSE_DEFENSE_foo.pdf.

    Returns (to_process, skipped_already_prefixed).
    """
    todo: List[Path] = []
    skipped: List[Path] = []
    for p in sorted(center.glob("*.pdf")):
        if not p.is_file():
            continue
        if p.name.startswith((PREFIX_DEFENSE, PREFIX_NON_DEFENSE)):
            skipped.append(p)
        else:
            todo.append(p)
    # Case-insensitive extension: Windows happily produces ".PDF".
    for p in sorted(center.glob("*.PDF")):
        if p.is_file() and p not in todo and p not in skipped:
            todo.append(p)
    return todo, skipped


def unique_destination(folder: Path, name: str) -> Path:
    """
    A destination path that does not overwrite anything.

    A collision with an earlier run gets a _1, _2 suffix. Overwriting is never the
    right answer here: the two files may be genuinely different revisions.
    """
    dest = folder / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while True:
        candidate = folder / ("%s_%d%s" % (stem, n, suffix))
        if not candidate.exists():
            return candidate
        n += 1


def apply_decision(center: Path, decision: Decision, dry_run: bool) -> None:
    """
    Move and rename one file according to its decision.

    A failure to move (file locked, open in a viewer, permission denied) is
    recorded on the decision and the batch continues -- one stuck file must not
    cost you the other 199.
    """
    if dry_run:
        return
    target_dir = center / decision.folder
    dest = unique_destination(target_dir, decision.new_name)
    try:
        shutil.move(str(decision.path), str(dest))
        decision.moved_to = dest
    except Exception as exc:
        decision.error = "could not move: %s: %s" % (type(exc).__name__, exc)
        log.error("  !! %s", decision.error)


# =============================================================================
#  LOGGING / SUMMARY
# =============================================================================

def setup_logging(center: Path, dry_run: bool) -> Optional[Path]:
    """Log to the console and, unless this is a dry run, to a timestamped file."""
    log.setLevel(logging.INFO)
    log.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)

    dc.log.setLevel(logging.INFO)
    dc.log.handlers.clear()
    dc.log.addHandler(console)

    if dry_run:
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = center / ("script_a_run_%s.log" % stamp)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    log.addHandler(handler)
    dc.log.addHandler(handler)
    return path


def log_file_detail(decision: Decision) -> None:
    """
    One block per file: what was read, how, and why it was routed where it was.

    Verbose on purpose. This log is how you audit the routing without opening the
    PDFs, which is the entire reason Script A is separate from Script B.
    """
    res = decision.result
    log.info("")
    log.info("%s", decision.path.name)
    if res is not None and res.ok:
        log.info("    %-14s: %s%s", "method", res.method_label,
                 "  (%d pages)" % res.page_count if res.page_count > 1 else "")
        for name in dc.FIELD_ORDER:
            f = res.fields.get(name)
            if f is not None and f.readable:
                log.info("    %-14s: %-32s [via %s, conf %.2f]%s",
                         dc.FIELD_LABELS[name], f.value, f.source, f.conf,
                         "  <-- %s" % f.note if f.note else "")
            else:
                log.info("    %-14s: -- NOT READABLE --", dc.FIELD_LABELS[name])
    for w in (res.warnings if res is not None else []):
        log.info("    %-14s: %s", "note", w)
    log.info("    -> %-13s %s", decision.folder, decision.reason)


def print_summary(center: Path, decisions: List[Decision], skipped: List[Path],
                  elapsed: float, dry_run: bool, log_path: Optional[Path]) -> None:
    """The end-of-run summary: counts per folder, then the filenames in each."""
    buckets: Dict[str, List[Decision]] = {f: [] for f in ALL_FOLDERS}
    for d in decisions:
        buckets[d.folder].append(d)

    line = "=" * 78
    log.info("")
    log.info("%s", line)
    log.info("SCRIPT A SUMMARY%s", "   (DRY RUN -- nothing was moved)" if dry_run else "")
    log.info("%s", line)
    log.info("Center folder   : %s", center)
    log.info("Files processed  : %d", len(decisions))
    log.info("Time taken       : %.1f s", elapsed)
    if skipped:
        log.info("Skipped          : %d already carried a classification prefix",
                 len(skipped))
    log.info("")
    for name in ALL_FOLDERS:
        log.info("  %-14s %3d", name, len(buckets[name]))
    log.info("")

    for name in ALL_FOLDERS:
        group = buckets[name]
        if not group:
            continue
        log.info("%s  (%d)", name, len(group))
        log.info("%s", "-" * 78)
        for d in group:
            shown = d.moved_to.name if d.moved_to else d.new_name
            log.info("  %-34s %s", shown, d.reason)
            if d.error:
                log.info("  %-34s !! %s", "", d.error)
        log.info("")

    if skipped:
        log.info("SKIPPED (already prefixed -- left where they were)")
        log.info("%s", "-" * 78)
        for p in skipped:
            log.info("  %s", p.name)
        log.info("")

    # The review volume is the number worth watching as real batches come through.
    n = len(decisions)
    if n:
        review = len(buckets[FOLDER_NEEDS_REVIEW]) + len(buckets[FOLDER_FAILED])
        log.info("Needs-review + failed: %d of %d (%.0f%%)", review, n,
                 100.0 * review / n)
        log.info("If that share is higher than you can absorb, the next levers are")
        log.info("adding label synonyms to LABEL_SYNONYMS in drawing_common.py and")
        log.info("cropping to the title block before OCR. See README.md.")
    if log_path:
        log.info("")
        log.info("Run log written to: %s", log_path)
    log.info("%s", line)


# =============================================================================
#  MAIN
# =============================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Script A: classify engineering drawings and sort them into "
                    "DEFENSE / NON_DEFENSE / NEEDS_REVIEW / FAILED.")
    ap.add_argument("folder", nargs="?", default=DEFAULT_CENTER_FOLDER,
                    help="center folder holding the unsorted PDFs "
                         "(default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and log everything but move no files -- use this "
                         "to review routing before committing to it")
    ap.add_argument("--debug-images", action="store_true",
                    help="write the preprocessed image handed to OCR into "
                         "<folder>/_debug_images, to see what OCR actually saw")
    ap.add_argument("--force-ocr", action="store_true",
                    help="ignore text layers and OCR every file (for testing the "
                         "OCR path against vector PDFs)")
    args = ap.parse_args(argv)

    center = Path(args.folder).resolve()
    if not center.is_dir():
        print("ERROR: center folder does not exist: %s" % center, file=sys.stderr)
        return 2

    log_path = setup_logging(center, args.dry_run)
    debug_dir = (center / "_debug_images") if args.debug_images else None

    log.info("Script A -- classify & sort")
    log.info("Center folder: %s", center)
    if args.dry_run:
        log.info("DRY RUN: decisions will be logged, no file will be moved.")

    if not args.dry_run:
        ensure_subfolders(center)

    todo, skipped = list_input_pdfs(center)
    if not todo:
        log.info("")
        log.info("No unsorted PDFs found at the top level of %s", center)
        if skipped:
            log.info("(%d file(s) there already carry a classification prefix.)",
                     len(skipped))
        return 0

    log.info("Found %d PDF(s) to process.", len(todo))

    started = datetime.now()
    decisions: List[Decision] = []
    for i, path in enumerate(todo, start=1):
        log.info("")
        log.info("[%d/%d] %s", i, len(todo), path.name)
        try:
            result = dc.analyze_drawing(path, debug_dir=debug_dir,
                                        force_ocr=args.force_ocr)
            decision = decide_route(path, result)
        except Exception as exc:
            # analyze_drawing contains its own failures, so reaching here means
            # something unexpected. The file goes to FAILED and the batch lives on.
            log.exception("unexpected error while processing %s", path.name)
            decision = Decision(path, FOLDER_FAILED, path.name,
                                "unexpected error: %s: %s"
                                % (type(exc).__name__, exc))
        apply_decision(center, decision, args.dry_run)
        log_file_detail(decision)
        decisions.append(decision)

    elapsed = (datetime.now() - started).total_seconds()
    print_summary(center, decisions, skipped, elapsed, args.dry_run, log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
