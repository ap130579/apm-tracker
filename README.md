# APM Tracker

An autonomous job-search system for a single hard problem: **new-grad Product Manager
programs open and close in days, and most of them never appear on a job board you can
subscribe to.**

It scans 86 employer career systems twice a day, decides which openings are genuinely
worth an application, and drafts that application end to end — stopping, deliberately,
one click short of submitting.

**Status:** live in production since July 2026. Digests deliver at 8:07 AM and 7:07 PM ET.

---

## Why this exists

Aggregators lag by days and miss custom career sites entirely. The programs that matter
most — Meta RPM, LinkedIn APB — hold windows open for roughly five to ten days, and
missing day one is effectively missing the cycle. Manually checking 86 sites twice a day
is not a thing a person does for nine months.

So the interesting problem is not "scrape job boards." It is: *given noisy, inconsistent,
partially unscrapeable sources, decide what is real, what is relevant, and what deserves
one of a limited number of applications* — and do it unattended, twice a day, without
ever fabricating an opening or an application detail.

---

## Architecture

Two halves, deliberately separated by what they're allowed to touch.

```
                    ┌─────────────────────────────────────┐
    SENSOR          │  apm-tracker  (this repo)           │
    cloud, 2×/day   │  Claude Code cloud routine          │
                    │                                     │
                    │  ATS APIs ──┐                       │
                    │  community  ├─→ scan ─→ filter ─→   │
                    │  feed     ──┘         dedup    email│
                    └──────────────┬──────────────────────┘
                                   │  pending_scan.json
                                   ▼  (read-only handoff)
                    ┌─────────────────────────────────────┐
    ACTUATOR        │  apm-agent  (local, not published)  │
    this machine    │                                     │
                    │  Retrieval → Job Analysis →         │
                    │  Resume Evaluation → Application     │
                    │      under a coordinator, over       │
                    │      shared persistent memory        │
                    └─────────────────────────────────────┘
```

The sensor runs in a cloud sandbox and holds no personal data. The actuator runs locally
and holds all of it. The handoff is a single read-only JSON file; nothing downstream ever
writes back to the scanner.

**Why the actuator isn't in this repo:** it needs a browser carrying real logged-in
sessions, it stores a résumé and PII, its approval gate requires a human present, and its
memory needs a disk that survives between runs. Every one of those is a reason not to put
it in a cloud sandbox, and the last one is not hypothetical — see below.

---

## Coverage

| Path | Companies | How |
|---|---:|---|
| Direct ATS API | 71 | Greenhouse (46), Ashby (9), Workday (9), SmartRecruiters (3), Lever, Eightfold, amazon.jobs, The Muse |
| Community feed | 15 | SimplifyJobs — the only path to custom career sites (Apple, Meta, Google, Microsoft, Uber…) |
| **Tracked** | **86** | |

Four companies remain genuine blind spots and are surfaced in the digest only when their
historical application window is active, rather than being quietly dropped.

---

## Engineering problems that were actually hard

This is the part worth reading. Most of these were discovered in production, and several
cost days.

**State persistence is impossible, and that's fine.** The cloud sandbox's git proxy
rejects every push, including to the default branch. State cannot survive between runs.
Rather than fight it, the scanner detects the condition and falls back to date-based
detection over a rolling three-day window. The system is designed to be correct without
durable state — which turned out to be a better property than the original design had.

**Closures require proof.** A posting is marked closed only after it goes missing from
**two consecutive successful** API scans of that company. A flaky fetch closes nothing,
and custom-ATS postings are never auto-closed. Silent false closures are the failure mode
that would quietly lose an opportunity, so the bias is heavily toward keeping history.

**The obvious filter is a trap.** Matching bare "product analyst" pulls in roughly 14
experienced-hire roles per day. Analyst titles are therefore admitted only with an
explicit new-grad signal. Similarly, a naive "APM" match surfaces Application Performance
Monitoring engineering roles — a category error that looks like a hit.

**Some sites cannot be scraped at all, and admitting it is the fix.** TikTok's own career
sites are fully client-rendered, and their internal API returns HTTP 200 with an empty
body to unauthenticated callers. No amount of retry logic changes that. The Muse serves
as a structured stand-in, with the community feed as a second path.

**Format landmines, each found the expensive way.** Greenhouse returns entity-escaped HTML
nested inside JSON — parse it naively and you store literal `<div>` tags as "job
description text". Workday job descriptions come from an undocumented CxS endpoint, not
the rendered page. The host Python has no root certificates, so every HTTP path needs an
explicit certificate bundle.

**A negative result is still a result.** Y Combinator's job boards were evaluated as a
possible source: 3,886 jobs across 100 boards yielded exactly one APM role, located in
São Paulo. The integration was rejected and the finding documented so it doesn't get
re-litigated.

**Scarce resources get counted.** Some employers cap early-career applications per period
and review them in submission order. Those get a persistent counter, a pinned digest
section, and a standalone alert on first detection — because there, the cost of applying
to the wrong opening is not zero.

---

## The decision layer

The scanner answers *what exists*. The local pipeline answers *what's worth doing*, as
four agents over shared persistent memory (SQLite), coordinated by ordinary Python — the
agents exercise judgment, dispatch doesn't need a model to do it.

| Agent | Responsibility |
|---|---|
| **Retrieval** | Pull job descriptions per ATS; JSON endpoints where they exist, HTML fallback where they don't |
| **Job Analysis** | Extract atomic requirements, eligibility, screening questions — reporting only what a posting states, never inferring eligibility generously |
| **Resume Evaluation** | Score résumé against requirements, every match citing specific evidence |
| **Application** | Draft the tailored résumé, cover letter, and screening answers |

**Grounding is enforced in code, not requested in a prompt.** A fluent model asked to
"tailor a résumé" will invent a metric — that is the single worst output this system could
produce. So two invariants are checked mechanically and reported on every run whether or
not they fire:

- A tailored bullet citing a source that doesn't exist is dropped rather than rendered.
- A reworded bullet may not introduce a number absent from the original; if it does, the
  text reverts to the source.

Measured on live postings: **38 requirement matches, 35 citing verifiable source
evidence, 0 fabricated.** The three unmatched requirements were correctly scored as having
no supporting evidence — including a five-day onsite requirement scored 0.00 rather than
rationalized.

**Confidence routes, it doesn't decorate.** Role-level scores below threshold stay
digest-only instead of entering the application queue. Answer-level, an exact match to a
previously answered question is reused; anything weaker is flagged for human review.
Demographic, veteran, disability, salary, and sponsorship questions are never
auto-answered at any confidence.

**The agent never clicks Submit.** It fills, screenshots, writes a review sheet with
per-field confidence and provenance, and stops.

---

## Roadmap

Shipped:

- **Scanner** — 86 sources, twice-daily digests, live in production
- **Memory & retrieval** — résumé decomposition, answer bank with full-text search, tracker bridge
- **Analysis & evaluation** — job analysis, résumé scoring, coordinator, ranked shortlist
- **Application drafting** — tailored résumé, cover letter, screening answers, review sheet

In progress:

- **Browser autofill** — Playwright-driven form completion for the 59 anonymous-apply
  employers, behind a one-click human approval gate
- **Authenticated and manual tiers** — 11 login-walled systems where the user authenticates
  once; 16 bespoke sites where the agent drafts and the human drives
- **Outcome feedback** — application results logged back to memory, so evaluation calibrates
  against what actually converted

### Where this could go

The architecture generalizes past its original scope. The underlying shape — *monitor
fragmented sources, extract structure from unstructured postings, score against a personal
corpus, generate grounded artifacts, gate on human approval* — describes recruiting
coordination, grant and RFP tracking, and compliance filing equally well.

The parts that would carry over are the parts that were hard: correctness without durable
state, evidence-cited generation, confidence-based routing to human review, and honest
accounting of what a system genuinely cannot see. The scarcest resource in an application
pipeline is not compute — it's the applicant's credibility, and every design decision here
follows from protecting it.

---

## Repository layout

| Path | Contents |
|---|---|
| `companies.json` | Target list: per-company ATS config, program names, historical windows |
| `scripts/tracker.py` | scan / add / remove / finalize / apps — Python 3 standard library only |
| `state/postings.json` | Persistent posting state, when the environment permits it |
| `output/digest-*.md` | Generated digests |
| `ROUTINE.md` | The exact prompt the cloud routine executes |

## Running it

```bash
python3 scripts/tracker.py scan
python3 scripts/tracker.py finalize --dry-run   # preview digest, no writes
python3 scripts/tracker.py finalize             # write state + digest
```

No dependencies beyond the standard library. The scanner is deliberately boring so that
the parts exercising judgment are the only parts that can surprise you.
