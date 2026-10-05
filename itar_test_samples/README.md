# ITAR lookup test samples

Copies of the 8 drawings from `make_test_drawings.py`'s synthetic batch that
actually reach `DEFENSE/` or `NON_DEFENSE/` under Script A's routing (the ones
that land in `NEEDS_REVIEW/` or `FAILED/` — files 06, 07, 11, 12 — never get an
ITAR lookup, since Script A already set them aside for a human, so they aren't
included here).

`expected_results.csv` pairs each drawing's real drawing number with a **made-up**
`fake_itar_status` (ITAR / NON_ITAR / NOT_FOUND) — these are not real ITAR
determinations, just fixed answers for a stub/mock version of the internal
portal to hand back during testing, so Script C's behavior can be verified
end-to-end (login -> search -> agent decision -> CSV row) without touching the
real portal or real credentials, per the plan's verification steps 1-3.

The extra `ZZ-00000-X` row (no matching PDF) is for testing the "portal has no
record of this number" path, which should route to `NEEDS_REVIEW`, never a
guessed status.
