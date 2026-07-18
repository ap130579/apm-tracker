# APM Tracker

Daily automated tracker for APM / new-grad PM / rotational PM postings across ~50 target
companies, scoped to **2027-start programs**. Runs as a Claude Code cloud routine every
morning at ~8 AM ET, diffs fresh results against known state, and emails a digest of
**newly opened** postings plus upcoming historical application windows.

## How it works

1. `scripts/tracker.py scan` — hits public ATS APIs directly (Greenhouse, Lever, Ashby,
   SmartRecruiters, Workday, Eightfold, amazon.jobs) for ~37 companies in one pass.
   No scraping, no web search needed for these. Companies on custom ATSes (Google, Meta,
   Apple, Microsoft, Uber, etc.) are listed as `manual_check` for the routine to verify
   via targeted web search.
2. The routine verifies new findings live, adds web-search finds via `tracker.py add`,
   drops false positives via `tracker.py remove`.
3. `scripts/tracker.py finalize` — merges into `state/postings.json`, writes
   `output/digest-YYYY-MM-DD.md`, prints the digest.
4. The routine commits state + digest back to this repo and emails the digest.

## Key design decisions

- **Closures need proof**: a posting is only marked `closed` after missing from
  **2 consecutive successful API scans** of its company — a flaky fetch never closes
  anything. Custom-ATS postings are never auto-closed.
- **History is kept**: closed postings stay in state with `closed_on`, never deleted.
- **Dedup** by company + normalized title (multi-location postings collapse to one entry).
- **Windows are priors, not truth**: `window_start`/`window_end` in `companies.json`
  come from the 2025 cycle; the digest flags companies entering their window in the
  next 5 days so you can also check manually.
- Title filter excludes senior/staff/intern/MBA roles and "APM" product false positives
  (e.g. Datadog's Application Performance Monitoring roles).

## Files

- `companies.json` — target list with per-company ATS config and historical windows
- `scripts/tracker.py` — scan/add/remove/finalize (Python 3 stdlib only)
- `state/postings.json` — persistent state (committed by the routine after each run)
- `output/digest-*.md` — daily digests
- `ROUTINE.md` — the exact prompt the cloud routine runs

## Manual run

```bash
python3 scripts/tracker.py scan
python3 scripts/tracker.py finalize --dry-run   # preview digest, no writes
python3 scripts/tracker.py finalize             # write state + digest
```
