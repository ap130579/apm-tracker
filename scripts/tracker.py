#!/usr/bin/env python3
"""APM tracker: fetch postings from ATS APIs, diff against state, emit a daily digest.

Subcommands (run from repo root):
  scan                 Fetch all structured-ATS companies, diff vs state, write state/pending_scan.json
  add                  Add a posting found manually/via web search to the pending scan
                       (--company --title --url [--posting-date] [--deadline])
  remove               Drop a false positive from pending new postings (--company --title)
  finalize             Merge pending scan into state/postings.json and write output/digest-YYYY-MM-DD.md
                       (prints the digest to stdout; use --dry-run to preview without writing)

Designed for Python 3 stdlib only. State is committed to git by the routine after finalize.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "postings.json"
PENDING_FILE = ROOT / "state" / "pending_scan.json"
COMPANIES_FILE = ROOT / "companies.json"
OUTPUT_DIR = ROOT / "output"

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}

# Tier 1: the core target — APM / rotational / new-grad PM programs.
INCLUDE_RE = re.compile(
    r"associate product manager|\bapm\b|apprentice product manager|rotational product"
    r"|product builder|product manager.*(new grad|university grad|early career|rotational|graduate program|2027)"
    r"|(new grad|university grad|early career).*product manager",
    re.I,
)
# Tier 2: product/data/business analyst roles, but ONLY with an explicit new-grad signal.
# A bare "Product Analyst" match would surface ~14 experienced-hire roles/day, so it is deliberately excluded.
TIER2_RE = re.compile(
    r"associate product analyst"
    r"|(product|business|data)\s+analyst.*(new grad|new-grad|university grad|early career|graduate program|rotational|2027)"
    r"|(new grad|new-grad|university grad|early career|graduate)\s.*(product|business|data)\s+analyst"
    r"|product analyst.*(program|rotation)",
    re.I,
)
EXCLUDE_RE = re.compile(
    # seniority/ineligible + titles where "APM" means Application Performance Monitoring (e.g. Datadog)
    r"\bsenior\b|\bsr\.?\b|\bstaff\b|principal|director|\blead\b|head of|intern\b|internship|\bmba\b|phd"
    r"|product marketing|engineering|serverless|solutions engineer|sales|customer success|support engineer"
    r"|\bmanager\s+(i{1,3}|[2-9])\b",
    re.I,
)
# Roles clearly outside the US job market for a US-based 2027 grad.
NON_US_RE = re.compile(
    r"\b(israel|india|bangalore|hyderabad|pune|gurgaon|beer sheva|tel aviv|tokyo|osaka|seoul|beijing"
    r"|shanghai|singapore|sydney|melbourne|dublin|london|manchester|berlin|munich|paris|amsterdam"
    r"|warsaw|krakow|bucharest|toronto|vancouver|montreal|mexico city|sao paulo|buenos aires)\b",
    re.I,
)

# Community-maintained new-grad job feed. Covers companies with no public ATS API
# (Apple, Meta, Google, Microsoft, ...) and supplies a direct link on the day a role is added.
AGGREGATOR_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
AGGREGATOR_LOOKBACK_DAYS = 21

# Postings must be missing from this many consecutive successful scans before being marked closed.
CLOSE_AFTER_MISSES = 2
WINDOW_LOOKAHEAD_DAYS = 5
CURRENT_CYCLE_NOTE = "2027-start programs (2027 grads)"

# Stateless fallback: if state/postings.json fails to persist between runs (e.g. the routine
# cannot push to git), diffing is impossible. Rather than flood the digest with every open role,
# fall back to "posted within the last N days" — which still answers "what opened recently?".
RECENT_DAYS = 3


def http_get(url, timeout=15):
    return _request(urllib.request.Request(url, headers=UA), timeout)


def http_post_json(url, payload, timeout=15):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={**UA, "Content-Type": "application/json"}
    )
    return _request(req, timeout)


def _request(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.URLError as e:
        # Local macOS python often lacks root certs; retry unverified before giving up.
        if isinstance(getattr(e, "reason", None), ssl.SSLError) or "CERTIFICATE" in str(e):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        raise


def title_matches(title):
    return bool(INCLUDE_RE.search(title)) and not EXCLUDE_RE.search(title)


def classify_title(title):
    """Return 'apm' (tier 1), 'analyst' (tier 2), or None if the title is not of interest."""
    if EXCLUDE_RE.search(title) or NON_US_RE.search(title):
        return None
    if INCLUDE_RE.search(title):
        return "apm"
    if TIER2_RE.search(title):
        return "analyst"
    return None


# ---------------------------------------------------------------- ATS fetchers
# Each returns a list of {"role_title", "url", "posting_date"} for matching roles.

def fetch_greenhouse(cfg):
    body = json.loads(http_get(f"https://boards-api.greenhouse.io/v1/boards/{cfg['board']}/jobs?content=false"))
    return [
        {"role_title": j["title"].strip(), "url": j["absolute_url"], "posting_date": (j.get("updated_at") or "")[:10]}
        for j in body.get("jobs", [])
        if title_matches(j["title"])
    ]


def fetch_lever(cfg):
    body = json.loads(http_get(f"https://api.lever.co/v0/postings/{cfg['board']}?mode=json"))
    if not isinstance(body, list):
        raise RuntimeError(f"lever error payload: {str(body)[:120]}")
    out = []
    for j in body:
        if title_matches(j.get("text", "")):
            ts = j.get("createdAt")
            date = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date().isoformat() if ts else ""
            out.append({"role_title": j["text"].strip(), "url": j["hostedUrl"], "posting_date": date})
    return out


def fetch_ashby(cfg):
    body = json.loads(http_get(f"https://api.ashbyhq.com/posting-api/job-board/{cfg['board']}?includeCompensation=false"))
    out = []
    for j in body.get("jobs", []):
        if title_matches(j.get("title", "")):
            url = j.get("jobUrl") or j.get("applyUrl") or ""
            out.append({"role_title": j["title"].strip(), "url": url, "posting_date": (j.get("publishedAt") or "")[:10]})
    return out


def fetch_smartrecruiters(cfg):
    company = cfg["company"]
    out, offset = [], 0
    while offset < 1000:
        body = json.loads(
            http_get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset={offset}")
        )
        postings = body.get("content", [])
        for j in postings:
            name = j.get("name", "")
            if title_matches(name):
                out.append(
                    {
                        "role_title": name.strip(),
                        "url": f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
                        "posting_date": (j.get("releasedDate") or "")[:10],
                    }
                )
        offset += 100
        if offset >= body.get("totalFound", 0) or not postings:
            break
    return out


def fetch_workday(cfg):
    tenant, host, site = cfg["tenant"], cfg["host"], cfg["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    while offset < 200:
        body = json.loads(
            http_post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": cfg.get("search", "product manager")})
        )
        postings = body.get("jobPostings", [])
        for j in postings:
            title = j.get("title", "")
            if title_matches(title):
                out.append(
                    {
                        "role_title": title.strip(),
                        "url": f"{base}/en-US/{site}{j.get('externalPath', '')}",
                        "posting_date": j.get("postedOn", ""),
                    }
                )
        offset += 20
        if offset >= body.get("total", 0) or not postings:
            break
    return out


def fetch_eightfold(cfg):
    url = (
        f"https://{cfg['host']}/api/apply/v2/jobs?domain={cfg['domain']}"
        f"&query={urllib.parse.quote('product manager')}&num=100&sort_by=timestamp"
    )
    body = json.loads(http_get(url))
    out = []
    for j in body.get("positions", []):
        name = j.get("name", "")
        if title_matches(name):
            ts = j.get("t_create") or j.get("t_update")
            date = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat() if ts else ""
            out.append({"role_title": name.strip(), "url": j.get("canonicalPositionUrl", ""), "posting_date": date})
    return out


def fetch_amazon(cfg):
    out = []
    for q in ("associate product manager", "product manager new grad"):
        url = f"https://www.amazon.jobs/en/search.json?base_query={urllib.parse.quote(q)}&result_limit=100&offset=0"
        body = json.loads(http_get(url))
        for j in body.get("jobs", []):
            title = j.get("title", "")
            if title_matches(title):
                out.append(
                    {
                        "role_title": title.strip(),
                        "url": "https://www.amazon.jobs" + j.get("job_path", ""),
                        "posting_date": (j.get("posted_date") or "")[:12],
                    }
                )
    return out


def _norm_company(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def fetch_aggregator(companies):
    """Pull the SimplifyJobs new-grad feed and return matches for our target companies.

    This is the only source that covers custom-ATS companies (Apple, Meta, Google, Microsoft,
    Uber, ...), and it carries a direct application URL plus an accurate posting date.
    Returns (by_company: {company_name: [posting, ...]}, other: [posting, ...]) where `other`
    holds strong APM programs at companies not on the target list.
    """
    data = json.loads(http_get(AGGREGATOR_URL, timeout=45))
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - AGGREGATOR_LOOKBACK_DAYS * 86400
    lookup = {}
    for c in companies:
        lookup[_norm_company(c["name"])] = c["name"]
        # a few feed aliases that differ from our canonical names
        for alias in {"Snap": ["snapinc", "snapchat"], "Meta": ["metafacebook", "facebook"],
                      "Expedia Group": ["expedia"], "Perplexity AI": ["perplexity"],
                      "American Express": ["americanexpressamex", "amex"]}.get(c["name"], []):
            lookup[alias] = c["name"]

    by_company, other = {}, []
    for j in data:
        if not j.get("active") or not j.get("is_visible", True):
            continue
        if (j.get("date_posted") or 0) < cutoff:
            continue
        title = j.get("title", "")
        locs = " ".join(j.get("locations") or [])
        tier = classify_title(title)
        if not tier or NON_US_RE.search(locs):
            continue
        entry = {
            "role_title": title.strip(),
            "url": j.get("url", ""),
            "posting_date": dt.datetime.fromtimestamp(j["date_posted"], dt.timezone.utc).date().isoformat()
            if j.get("date_posted") else "",
            "tier": tier,
            "source": "simplify-feed",
            "locations": locs,
        }
        target = lookup.get(_norm_company(j.get("company_name")))
        if target:
            by_company.setdefault(target, []).append(entry)
        elif tier == "apm" and re.search(r"associate product manager|\bapm\b|rotational product", title, re.I):
            other.append({**entry, "company": j.get("company_name", "?")})
    return by_company, other


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "eightfold": fetch_eightfold,
    "amazon": fetch_amazon,
}


# ---------------------------------------------------------------- state helpers

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def load_state():
    return load_json(STATE_FILE, {"meta": {"baseline_done": False, "last_run": None}, "postings": []})


def load_companies():
    return json.loads(COMPANIES_FILE.read_text())["companies"]


_FILLER_RE = re.compile(
    r"\((?:[^)]*)\)"                       # parentheticals: "(2027 Start)", "(Remote)"
    r"|\b(20\d\d|start|starting|program|programme|the|a|an)\b",
    re.I,
)


def norm_key(company, title):
    """Company + fuzzy title. Fuzzy so the same job found via two sources collapses to one entry —
    e.g. 'Associate Product Manager, New Grad (2027 Start)' == 'Associate Product Manager New Grad'."""
    t = _FILLER_RE.sub(" ", title.lower())
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return (company.lower().strip(), t)


def norm_url(url):
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def today():
    return dt.date.today()


# ---------------------------------------------------------------- window logic

def parse_window(mmdd, year):
    m, d = (int(x) for x in mmdd.split("-"))
    return dt.date(year, m, d)


def upcoming_windows(companies, ref):
    """Companies whose historical window opens within the next WINDOW_LOOKAHEAD_DAYS (or is currently open)."""
    soon, open_now = [], []
    for c in companies:
        if not c.get("window_start"):
            continue
        start = parse_window(c["window_start"], ref.year)
        end = parse_window(c["window_end"], ref.year) if c.get("window_end") else start
        if end < start:  # window wraps the year boundary
            end = end.replace(year=ref.year + 1)
        if start <= ref <= end:
            open_now.append((c, start, end))
        elif 0 < (start - ref).days <= WINDOW_LOOKAHEAD_DAYS:
            soon.append((c, start, end))
    return soon, open_now


# ---------------------------------------------------------------- commands

def is_recent(posting_date, ref, days=RECENT_DAYS):
    """True if posting_date is within `days` of ref. Unparseable/missing dates count as recent
    (better to surface a role with an unknown date than to silently drop it)."""
    if not posting_date:
        return True
    try:
        return (ref - dt.date.fromisoformat(posting_date[:10])).days <= days
    except ValueError:
        # Workday-style relative strings: "Posted Today", "Posted 3 Days Ago"
        m = re.search(r"(\d+)\s*day", posting_date, re.I)
        if m:
            return int(m.group(1)) <= days
        return bool(re.search(r"today|yesterday", posting_date, re.I))


def cmd_scan(args):
    companies = load_companies()
    state = load_state()
    known = {}
    for p in state["postings"]:
        known[norm_key(p["company"], p["role_title"])] = p

    # If state never persisted, diffing would report every open role as "new" on every run.
    # Detect that and switch to date-based detection instead.
    state_healthy = bool(state["meta"].get("baseline_done")) and bool(state["postings"])

    structured = [c for c in companies if c["ats"]["type"] in FETCHERS]
    custom = [c for c in companies if c["ats"]["type"] == "custom"]

    results, errors = {}, {}

    def run(c):
        return c["name"], FETCHERS[c["ats"]["type"]](c["ats"])

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(run, c): c for c in structured}
        for fut in cf.as_completed(futures):
            c = futures[fut]
            try:
                name, postings = fut.result()
                results[name] = postings
            except Exception as e:
                errors[c["name"]] = f"{type(e).__name__}: {str(e)[:140]}"

    # Community feed: the only source covering custom-ATS companies (Apple, Meta, Google, ...).
    agg_by_company, agg_other, agg_error = {}, [], None
    try:
        agg_by_company, agg_other = fetch_aggregator(companies)
    except Exception as e:
        agg_error = f"{type(e).__name__}: {str(e)[:140]}"

    ref = today()

    def is_new(key, p):
        """Healthy state -> anything unseen is new. Degraded state -> only recently-posted roles."""
        if state_healthy:
            return key not in known
        return key not in known and is_recent(p.get("posting_date", ""), ref)

    new_postings, seen_keys = [], set()
    for name, postings in results.items():
        for p in postings:
            key = norm_key(name, p["role_title"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if is_new(key, p):
                new_postings.append({"company": name, "source": "ats-api", "tier": classify_title(p["role_title"]) or "apm", **p})

    for name, postings in agg_by_company.items():
        for p in postings:
            key = norm_key(name, p["role_title"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if is_new(key, p):
                new_postings.append({"company": name, **p})

    soon, open_now = upcoming_windows(companies, ref)
    pending = {
        "scan_date": ref.isoformat(),
        "state_healthy": state_healthy,
        "checked_companies": sorted(results.keys()),
        "fetch_errors": errors,
        "aggregator_error": agg_error,
        "aggregator_companies_covered": sorted(agg_by_company.keys()),
        "other_apm_programs": agg_other[:15],
        "new_postings": new_postings,
        "seen_open": sorted(f"{k[0]}||{k[1]}" for k in seen_keys),
        "manual_check": [
            {
                "company": c["name"],
                "program": c["program"],
                "careers_url": c["careers_url"],
                "window": f"{c.get('window_start', '?')} to {c.get('window_end', '?')}",
                "note": c.get("window_note", ""),
            }
            for c in custom
        ],
        "windows_opening_soon": [
            {"company": c["name"], "opens_around": s.isoformat(), "ends_around": e.isoformat(), "note": c.get("window_note", "")}
            for c, s, e in soon
        ],
        "windows_open_now": [
            {"company": c["name"], "opens_around": s.isoformat(), "ends_around": e.isoformat(), "note": c.get("window_note", "")}
            for c, s, e in open_now
        ],
    }
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(pending, indent=2))
    print(json.dumps(
        {
            "scan_date": pending["scan_date"],
            "health": {
                "state_healthy": state_healthy,
                "detection_mode": "diff" if state_healthy else f"date-based (last {RECENT_DAYS}d) - STATE NOT PERSISTING",
                "api_companies_ok": len(results),
                "api_companies_total": len(structured),
                "api_errors": len(errors),
                "aggregator_ok": agg_error is None,
                "aggregator_hits": sum(len(v) for v in agg_by_company.values()),
            },
            "fetch_errors": errors,
            "aggregator_error": agg_error,
            "new_postings": new_postings,
            "other_apm_programs": pending["other_apm_programs"],
            "manual_check_needed": [m["company"] for m in pending["manual_check"]],
            "windows_opening_soon": pending["windows_opening_soon"],
            "windows_open_now": [w["company"] for w in pending["windows_open_now"]],
        },
        indent=2,
    ))


def cmd_add(args):
    pending = load_json(PENDING_FILE, None)
    if pending is None:
        sys.exit("No pending scan — run `scan` first.")
    entry = {
        "company": args.company,
        "role_title": args.title,
        "url": args.url,
        "posting_date": args.posting_date or "",
        "source": "web-search",
    }
    if args.deadline:
        entry["deadline"] = args.deadline
    key = norm_key(args.company, args.title)
    for p in pending["new_postings"]:
        if norm_key(p["company"], p["role_title"]) == key:
            sys.exit("Already in pending new postings.")
    state = load_state()
    for p in state["postings"]:
        if norm_key(p["company"], p["role_title"]) == key and p["status"] == "open":
            sys.exit("Already tracked as open in state — not new.")
    pending["new_postings"].append(entry)
    pending["seen_open"].append(f"{key[0]}||{key[1]}")
    PENDING_FILE.write_text(json.dumps(pending, indent=2))
    print(f"Added: {args.company} — {args.title}")


def cmd_remove(args):
    pending = load_json(PENDING_FILE, None)
    if pending is None:
        sys.exit("No pending scan — run `scan` first.")
    key = norm_key(args.company, args.title)
    before = len(pending["new_postings"])
    pending["new_postings"] = [p for p in pending["new_postings"] if norm_key(p["company"], p["role_title"]) != key]
    if len(pending["new_postings"]) == before:
        sys.exit("No matching pending posting found.")
    PENDING_FILE.write_text(json.dumps(pending, indent=2))
    print(f"Removed: {args.company} — {args.title}")


def cmd_finalize(args):
    pending = load_json(PENDING_FILE, None)
    if pending is None:
        sys.exit("No pending scan — run `scan` first.")
    state = load_state()
    ref = dt.date.fromisoformat(pending["scan_date"])
    baseline = not state["meta"].get("baseline_done", False)
    seen = set(pending["seen_open"])
    checked = set(pending["checked_companies"])

    # Mark misses / closures only for companies whose API fetch succeeded this run.
    closed_today = []
    for p in state["postings"]:
        if p["status"] != "open":
            continue
        if p["company"] not in checked:
            continue  # custom-ATS companies: only closed via manual verification
        if f"{norm_key(p['company'], p['role_title'])[0]}||{norm_key(p['company'], p['role_title'])[1]}" in seen:
            p["miss_count"] = 0
            p["last_seen"] = ref.isoformat()
        else:
            p["miss_count"] = p.get("miss_count", 0) + 1
            if p["miss_count"] >= CLOSE_AFTER_MISSES:
                p["status"] = "closed"
                p["closed_on"] = ref.isoformat()
                closed_today.append(p)

    for p in pending["new_postings"]:
        state["postings"].append(
            {
                "company": p["company"],
                "role_title": p["role_title"],
                "url": norm_url(p["url"]) if p.get("url") else "",
                "first_seen": ref.isoformat(),
                "last_seen": ref.isoformat(),
                "posting_date": p.get("posting_date", ""),
                "deadline": p.get("deadline", ""),
                "source": p.get("source", "unknown"),
                "tier": p.get("tier", "apm"),
                "status": "open",
                "miss_count": 0,
            }
        )

    # Digest
    title = "Initial Baseline" if baseline else ref.isoformat()
    lines = [f"# APM Tracker — {title}", ""]
    if baseline:
        lines += [
            "_First run: every posting below seeds the baseline — these are currently-open matches, not necessarily opened today._",
            "",
        ]
    news = pending["new_postings"]
    tier1 = [p for p in news if p.get("tier", "apm") == "apm"]
    tier2 = [p for p in news if p.get("tier") == "analyst"]

    def fmt(p):
        date_bit = f" — posted {p['posting_date']}" if p.get("posting_date") else ""
        return f"- **{p['company']}** — {p['role_title']}{date_bit} — {p.get('url', '')}"

    lines.append("## New postings today" if not baseline else "## Currently open matching postings (baseline)")
    if tier1:
        lines += [fmt(p) for p in sorted(tier1, key=lambda x: x["company"])]
    else:
        lines.append("- No new postings today.")
    lines.append("")

    if tier2:
        lines.append("## Analyst roles — wider net (new-grad product/data analyst)")
        lines += [fmt(p) for p in sorted(tier2, key=lambda x: x["company"])]
        lines.append("")

    other = pending.get("other_apm_programs", [])
    if other:
        lines.append("## Other APM programs spotted (not on your target list)")
        for p in other[:8]:
            date_bit = f" — posted {p['posting_date']}" if p.get("posting_date") else ""
            lines.append(f"- {p.get('company', '?')} — {p['role_title']}{date_bit} — {p.get('url', '')}")
        lines.append("")

    lines.append(f"## Windows opening soon (next {WINDOW_LOOKAHEAD_DAYS} days, historical timing)")
    soon = pending.get("windows_opening_soon", [])
    if soon:
        for w in soon:
            note = f" — {w['note']}" if w.get("note") else ""
            lines.append(f"- {w['company']} — historically opens around {w['opens_around']}{note}")
    else:
        lines.append("- None in the next 5 days.")
    lines.append("")

    open_now = pending.get("windows_open_now", [])
    if open_now:
        lines.append("## Historical windows open right now (check manually too)")
        for w in open_now:
            note = f" — {w['note']}" if w.get("note") else ""
            lines.append(f"- {w['company']} — window ~{w['opens_around']} to {w['ends_around']}{note}")
        lines.append("")

    reminders = []
    for p in state["postings"]:
        if p["status"] == "open" and p.get("deadline"):
            try:
                dl = dt.date.fromisoformat(p["deadline"])
                days = (dl - ref).days
                if 0 <= days <= 7:
                    reminders.append(f"- {p['company']} — {p['role_title']} deadline in {days} day(s) ({p['deadline']})")
            except ValueError:
                pass
    if reminders:
        lines += ["## Reminders", *reminders, ""]
    if closed_today:
        lines.append("## Closed since last run")
        for p in closed_today:
            lines.append(f"- {p['company']} — {p['role_title']}")
        lines.append("")
    if pending.get("fetch_errors"):
        lines.append("## Fetch errors (checked via fallback or skipped)")
        for c, e in pending["fetch_errors"].items():
            lines.append(f"- {c}: {e}")
        lines.append("")

    # Health footer — lets a degrading system be spotted before it silently misses a window.
    n_api_ok = len(pending["checked_companies"])
    n_api_err = len(pending.get("fetch_errors", {}))
    agg_ok = pending.get("aggregator_error") is None
    agg_n = len(pending.get("aggregator_companies_covered", []))
    lines += [
        "## Summary",
        f"{len(tier1)} new APM posting(s)"
        + (f", {len(tier2)} analyst role(s)" if tier2 else "")
        + f" · {n_api_ok} companies checked via API"
        + (f" ({n_api_err} errored)" if n_api_err else "")
        + " · community feed "
        + (f"OK, covering {agg_n} target companies" if agg_ok else "FAILED"),
    ]
    if not pending.get("state_healthy", True):
        lines.append(f"  ⚠️ STATE NOT PERSISTING — showing roles posted in the last {RECENT_DAYS} days instead of a true diff. Fix the routine's git push to restore exact tracking.")
    if not agg_ok:
        lines.append(f"  ⚠️ community feed error: {pending.get('aggregator_error')} — custom-ATS companies (Apple, Meta, Google) had reduced coverage this run.")
    if n_api_err > len(pending["checked_companies"]) // 2:
        lines.append("  ⚠️ MOST API CHECKS FAILED — likely a network/sandbox problem, treat this digest as unreliable.")
    digest = "\n".join(lines) + "\n"

    # Machine-readable status for the routine to build the email subject line from.
    status = {
        "new_apm": len(tier1),
        "new_analyst": len(tier2),
        "api_ok": n_api_ok,
        "api_errors": n_api_err,
        "aggregator_ok": agg_ok,
        "state_healthy": pending.get("state_healthy", True),
        "degraded": (not agg_ok) or n_api_err > len(pending["checked_companies"]) // 2,
    }
    status["subject_suffix"] = (
        f"{status['new_apm']} NEW" if status["new_apm"]
        else ("nothing new" if not status["degraded"] else "DEGRADED - check digest")
    )

    if args.dry_run:
        print(digest)
        print("STATUS_JSON " + json.dumps(status), file=sys.stderr)
        return

    state["meta"]["baseline_done"] = True
    state["meta"]["last_run"] = ref.isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"digest-{ref.isoformat()}.md"
    out_path.write_text(digest)
    PENDING_FILE.unlink()
    print(digest)
    print("STATUS_JSON " + json.dumps(status), file=sys.stderr)
    print(f"[written: {out_path.relative_to(ROOT)}; state updated]", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    p_add = sub.add_parser("add")
    p_add.add_argument("--company", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--posting-date", default="")
    p_add.add_argument("--deadline", default="")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--company", required=True)
    p_rm.add_argument("--title", required=True)
    p_fin = sub.add_parser("finalize")
    p_fin.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    {"scan": cmd_scan, "add": cmd_add, "remove": cmd_remove, "finalize": cmd_finalize}[args.cmd](args)


if __name__ == "__main__":
    main()
