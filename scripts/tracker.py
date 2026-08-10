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
    r"|\bmanager\s+(i{1,3}|[2-9])\b"
    # "APM" also expands to non-product titles (e.g. TikTok's "Agency Partnerships Manager (APM)")
    r"|agency partnership|account partner|area partner|partnerships manager",
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

# ---- Urgent-source handling (TikTok) --------------------------------------
# TikTok caps Early Career applications at 2 per period and reviews in submission order,
# so its hits are pinned to the top of the digest and alerted on immediately.
APPLICATIONS_FILE = ROOT / "state" / "applications.json"
APPLICATION_CAP = 2

# Locations that do not reduce priority. Anything else is deprioritised, never suppressed.
PREFERRED_LOC_RE = re.compile(
    r"san francisco|bay area|mountain view|palo alto|san jose|sunnyvale|santa clara|menlo park"
    r"|redwood city|cupertino|oakland|berkeley|seattle|bellevue|redmond|kirkland",
    re.I,
)
# Graduation windows that exclude a May 2027 conferral date.
GRAD_OK_RE = re.compile(r"(may|spring|summer|winter|fall)?\s*2027|2026\s*[-–—/to]+\s*2027|2027\s*[-–—/to]+\s*2028", re.I)
MANDARIN_RE = re.compile(r"(fluent|fluency|proficien\w*|native|bilingual)[^.]{0,60}(mandarin|chinese)"
                         r"|(mandarin|chinese)[^.]{0,60}(fluent|fluency|proficien\w*|required|must)", re.I)


def location_priority(loc):
    """'preferred' for Bay Area / Seattle, else 'other'. Never suppresses — only ranks."""
    return "preferred" if loc and PREFERRED_LOC_RE.search(loc) else "other"


def grad_window_excludes_may_2027(text):
    """True when an explicit graduation window is stated AND it cannot include May 2027.
    Absent or unparseable text returns False — we never suppress on missing evidence."""
    if not text:
        return False
    m = re.search(r"[^.]*graduat\w*[^.]*\.", text, re.I)
    if not m:
        return False
    sent = m.group(0)
    if not re.search(r"20\d\d", sent):
        return False
    return not GRAD_OK_RE.search(sent)


def requires_fluent_mandarin(text):
    return bool(text and MANDARIN_RE.search(text))


def load_applications(ref=None):
    """Application counter for the current period. Periods reset Jan 1 and Jul 1."""
    ref = ref or today()
    period = f"{ref.year}-H{1 if ref.month <= 6 else 2}"
    data = load_json(APPLICATIONS_FILE, {})
    if data.get("period") != period:
        data = {"period": period, "used": 0, "entries": []}
    return data, period


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


def fetch_themuse(cfg):
    """The Muse public API — the only reachable structured source for TikTok.

    TikTok's own sites (careers.tiktok.com, lifeattiktok.com) are fully client-rendered and
    their internal search API returns an empty body to unauthenticated callers, so they cannot
    be scraped from the sandbox. The Muse carries TikTok's postings and is queried here instead.
    """
    company = cfg["company"]
    queries = [q.lower() for q in cfg.get("queries", [])]
    pages = cfg.get("pages", 60)
    out, seen = [], set()
    for p in range(pages):
        try:
            body = json.loads(http_get(f"https://www.themuse.com/api/public/jobs?company={urllib.parse.quote(company)}&page={p}"))
        except Exception:
            break
        results = body.get("results", [])
        if not results:
            break
        for j in results:
            if (j.get("company") or {}).get("name", "").lower() != company.lower():
                continue
            title = (j.get("name") or "").strip()
            if not title or title in seen:
                continue
            # A posting qualifies if it matches the shared new-grad classifier OR any configured query
            # (all query terms must appear in the title).
            qmatch = any(all(term in title.lower() for term in q.split()) for q in queries)
            if not (classify_title(title) or qmatch):
                continue
            locs = [l.get("name", "") for l in (j.get("locations") or [])]
            loc_str = "; ".join(locs)
            # Location-level non-US filter: the title-based check cannot see a Brazil/Singapore posting
            # whose title is location-free. Keep anything with at least one US-plausible location.
            if loc_str and NON_US_RE.search(loc_str) and not PREFERRED_LOC_RE.search(loc_str) \
               and not re.search(r"\b(usa|united states|remote\s*[-–]?\s*us|,\s*[A-Z]{2}\b)", loc_str):
                continue
            seen.add(title)
            out.append(
                {
                    "role_title": title,
                    "url": ((j.get("refs") or {}).get("landing_page") or ""),
                    "posting_date": (j.get("publication_date") or "")[:10],
                    "location": loc_str,
                    "job_id": str(j.get("id", "")),
                    "team": "; ".join(c for c in (j.get("categories") and [x.get("name", "") for x in j["categories"]] or [])),
                    "levels": "; ".join(x.get("name", "") for x in (j.get("levels") or [])),
                }
            )
        if p + 1 >= body.get("page_count", 0):
            break
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
        elif tier == "apm":
            # Any tier-1 title at a non-target company. INCLUDE_RE already demands a new-grad
            # signal, so this stays quiet — and it catches odd phrasings like
            # "Product Manager (2027 Graduates)" that a narrower regex would drop.
            other.append({**entry, "company": j.get("company_name", "?")})
    return by_company, other


FETCHERS = {
    "themuse": fetch_themuse,
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
    r"\b(20\d\d|start|starting|program|programme|the|a|an)\b",
    re.I,
)
# A parenthetical is noise only if it carries nothing but year/start/grad words — e.g. "(2027 Start)".
# One naming a team, like TikTok's "(Global Ecommerce)", is the only thing distinguishing two
# otherwise-identical postings, so it must be kept.
_NOISE_PAREN_RE = re.compile(
    r"\(\s*(?:20\d\d|start(?:ing|s)?|new\s*grad|grad(?:uate)?s?|remote|hybrid|onsite|us|usa"
    r"|spring|summer|fall|autumn|winter"
    r"|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?"
    r"|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r"|[-–—,/&]|\s)*\s*\)",
    re.I,
)


def norm_key(company, title):
    """Company + fuzzy title. Fuzzy so the same job found via two sources collapses to one entry —
    e.g. 'Associate Product Manager, New Grad (2027 Start)' == 'Associate Product Manager New Grad' —
    while still distinguishing sibling postings that differ only by team, e.g. TikTok's
    '... Graduate (TikTok Ads)' vs '... Graduate (Global Ecommerce)'."""
    t = _NOISE_PAREN_RE.sub(" ", title.lower())
    t = _FILLER_RE.sub(" ", t)
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

    urgent_names = {c["name"] for c in companies if c.get("urgent")}
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
                entry = {"company": name, "source": "ats-api",
                         "tier": classify_title(p["role_title"]) or "apm", **p}
                if name in urgent_names:
                    entry["urgent"] = True
                    entry["priority"] = location_priority(entry.get("location", ""))
                    # Detail fields (graduation window, Mandarin requirement, salary) are not in any
                    # structured feed — the routine enriches these by fetching the posting itself.
                    entry["needs_detail"] = True
                new_postings.append(entry)

    for name, postings in agg_by_company.items():
        for p in postings:
            key = norm_key(name, p["role_title"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if is_new(key, p):
                entry = {"company": name, **p}
                if name in urgent_names:
                    entry["urgent"] = True
                    entry["priority"] = location_priority(entry.get("location", ""))
                    entry["needs_detail"] = True
                new_postings.append(entry)

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
        "urgent_new": [p for p in new_postings if p.get("urgent")],
        "seen_open": sorted(f"{k[0]}||{k[1]}" for k in seen_keys),
        # Only surface custom-ATS companies when their window is actually near — listing all 15
        # year-round reads as a coverage hole when most are watched by the community feed.
        "manual_check": [
            {
                "company": c["name"],
                "program": c["program"],
                "careers_url": c["careers_url"],
                "window": f"{c.get('window_start', '?')} to {c.get('window_end', '?')}",
                "note": c.get("window_note", ""),
                "coverage": c["ats"].get("feed_coverage", "feed-listed"),
                "in_feed": c["name"] in agg_by_company,
            }
            for c in custom
            if c["name"] in {w["company"] for w in [
                {"company": x[0]["name"]} for x in soon + open_now]}
        ],
        "coverage_summary": {
            "api": len(structured),
            "feed_proven": [c["name"] for c in custom if c["ats"].get("feed_coverage") == "feed-proven"],
            "feed_listed": [c["name"] for c in custom if c["ats"].get("feed_coverage") == "feed-listed"],
            "no_coverage": [c["name"] for c in custom if c["ats"].get("feed_coverage") == "none"],
        },
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
            "urgent_new": pending["urgent_new"],
            "applications_used": load_applications()[0]["used"],
            "other_apm_programs": pending["other_apm_programs"],
            "manual_check_needed": [f"{m['company']} (coverage: {m['coverage']})" for m in pending["manual_check"]],
            "coverage_summary": pending["coverage_summary"],
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
    for k in ("location", "team", "job_id", "salary", "grad_window", "qualifications"):
        if getattr(args, k, ""):
            entry[k] = getattr(args, k)
    if getattr(args, "mandarin", ""):
        entry["mandarin"] = args.mandarin
    if getattr(args, "grad_window_ok", ""):
        entry["grad_window_ok"] = args.grad_window_ok
    companies = {c["name"]: c for c in load_companies()}
    if companies.get(args.company, {}).get("urgent"):
        entry["urgent"] = True
        entry["priority"] = location_priority(entry.get("location", ""))
        entry["needs_detail"] = not (entry.get("grad_window") or entry.get("qualifications"))
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


def cmd_apps(args):
    data, period = load_applications()
    if args.set is not None:
        data["used"] = max(0, args.set)
    if args.log:
        data["entries"].append({"role": args.log, "date": today().isoformat()})
        data["used"] = int(data.get("used", 0)) + 1
    if args.set is not None or args.log:
        APPLICATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        APPLICATIONS_FILE.write_text(json.dumps(data, indent=2))
    print(json.dumps({"period": period, "used": data.get("used", 0),
                      "remaining": max(0, APPLICATION_CAP - int(data.get("used", 0))),
                      "cap": APPLICATION_CAP, "entries": data.get("entries", [])}, indent=2))


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

    # Suppression for urgent sources, applied only when the routine supplied evidence.
    # Missing evidence never suppresses — an unverified posting is surfaced, flagged.
    suppressed = []
    kept = []
    for p in news:
        if p.get("urgent"):
            detail = " ".join(str(p.get(k, "")) for k in ("qualifications", "description", "grad_window"))
            if str(p.get("mandarin", "")).lower() in ("yes", "true", "required") or requires_fluent_mandarin(detail):
                p["suppressed_reason"] = "requires fluent Mandarin"
                suppressed.append(p); continue
            if str(p.get("grad_window_ok", "")).lower() in ("no", "false") or grad_window_excludes_may_2027(detail):
                p["suppressed_reason"] = "graduation window excludes May 2027"
                suppressed.append(p); continue
        kept.append(p)
    news = kept
    urgent = [p for p in news if p.get("urgent")]
    urgent.sort(key=lambda x: (0 if x.get("priority") == "preferred" else 1, x.get("company", "")))
    tier1 = [p for p in news if p.get("tier", "apm") == "apm" and not p.get("urgent")]
    tier2 = [p for p in news if p.get("tier") == "analyst" and not p.get("urgent")]

    def fmt(p):
        date_bit = f" — posted {p['posting_date']}" if p.get("posting_date") else ""
        return f"- **{p['company']}** — {p['role_title']}{date_bit} — {p.get('url', '')}"

    apps, period = load_applications(ref)
    remaining = max(0, APPLICATION_CAP - int(apps.get("used", 0)))

    if urgent:
        lines.append(f"## 📌 TikTok — ACT NOW ({len(urgent)} new · {remaining} of {APPLICATION_CAP} applications left this period)")
        lines.append("_Rolling review in submission order; Early Career applications are capped per period._")
        for p in urgent:
            flags = []
            if p.get("priority") == "other":
                flags.append(f"⬇ outside Bay Area/Seattle ({p.get('location', 'location unknown')})")
            if p.get("needs_detail"):
                flags.append("⚠ grad window / Mandarin / salary not yet verified")
            for k, label in (("team", "team"), ("job_id", "job id"), ("salary", "salary"), ("grad_window", "grad window")):
                if p.get(k):
                    flags.append(f"{label}: {p[k]}")
            date_bit = f" — posted {p['posting_date']}" if p.get("posting_date") else ""
            lines.append(f"- **{p['role_title']}**{date_bit} — {p.get('url', '')}")
            if p.get("location") and p.get("priority") == "preferred":
                lines.append(f"    - {p['location']}")
            for f in flags:
                lines.append(f"    - {f}")
        lines.append("")
    if suppressed:
        lines.append("## TikTok — suppressed by your filters")
        for p in suppressed:
            lines.append(f"- ~~{p['role_title']}~~ — {p['suppressed_reason']} — {p.get('url', '')}")
        lines.append("")

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

    manual = pending.get("manual_check", [])
    if manual:
        lines.append("## Manual check — these companies' windows are active and they have no API")
        for m in manual:
            label = {"none": "NO automated coverage — check this yourself",
                     "feed-listed": "in community feed, but no PM role has ever come through it",
                     "feed-proven": "community feed has caught its PM roles before"}.get(m["coverage"], m["coverage"])
            lines.append(f"- **{m['company']}** — {m['program']} — {label} — {m['careers_url']}")
        lines.append("")

    # Health footer — lets a degrading system be spotted before it silently misses a window.
    n_api_ok = len(pending["checked_companies"])
    n_api_err = len(pending.get("fetch_errors", {}))
    agg_ok = pending.get("aggregator_error") is None
    agg_n = len(pending.get("aggregator_companies_covered", []))
    lines += [
        "## Summary",
        f"TikTok applications used this period ({period}): {apps.get('used', 0)}/{APPLICATION_CAP}"
        f" · {len(urgent)} new TikTok posting(s)"
        + (f" · {len(suppressed)} suppressed" if suppressed else "")
        + f" · {len(tier1)} new APM posting(s)"
        + (f", {len(tier2)} analyst role(s)" if tier2 else "")
        + f" · {n_api_ok} companies checked via API"
        + (f" ({n_api_err} errored)" if n_api_err else "")
        + " · community feed "
        + (f"OK, covering {agg_n} target companies" if agg_ok else "FAILED"),
    ]
    cs = pending.get("coverage_summary", {})
    if cs:
        lines.append(
            f"  Coverage: {cs.get('api', 0)} companies via direct API"
            f" · {len(cs.get('feed_proven', []))} via community feed (proven)"
            f" · {len(cs.get('feed_listed', []))} feed-listed but PM-unproven"
            f" · {len(cs.get('no_coverage', []))} need manual checks in-window ("
            + ", ".join(cs.get("no_coverage", [])) + ")"
        )
    if not pending.get("state_healthy", True):
        lines.append(f"  ⚠️ STATE NOT PERSISTING — showing roles posted in the last {RECENT_DAYS} days instead of a true diff. Fix the routine's git push to restore exact tracking.")
    if not agg_ok:
        lines.append(f"  ⚠️ community feed error: {pending.get('aggregator_error')} — custom-ATS companies (Apple, Meta, Google) had reduced coverage this run.")
    if n_api_err > len(pending["checked_companies"]) // 2:
        lines.append("  ⚠️ MOST API CHECKS FAILED — likely a network/sandbox problem, treat this digest as unreliable.")
    digest = "\n".join(lines) + "\n"

    # Machine-readable status for the routine to build the email subject line from.
    status = {
        "urgent_new": len(urgent),
        "urgent_suppressed": len(suppressed),
        "applications_used": apps.get("used", 0),
        "applications_remaining": remaining,
        "period": period,
        "new_apm": len(tier1),
        "new_analyst": len(tier2),
        "api_ok": n_api_ok,
        "api_errors": n_api_err,
        "aggregator_ok": agg_ok,
        "state_healthy": pending.get("state_healthy", True),
        "degraded": (not agg_ok) or n_api_err > len(pending["checked_companies"]) // 2,
    }
    status["subject_suffix"] = (
        f"TIKTOK x{len(urgent)} + {status['new_apm']} NEW" if urgent
        else f"{status['new_apm']} NEW" if status["new_apm"]
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
    for f in ("--location", "--team", "--job-id", "--salary", "--grad-window", "--qualifications"):
        p_add.add_argument(f, default="")
    p_add.add_argument("--mandarin", default="", choices=["", "yes", "no"],
                       help="whether fluent Mandarin is a MINIMUM requirement")
    p_add.add_argument("--grad-window-ok", default="", choices=["", "yes", "no"],
                       help="whether the stated graduation window includes May 2027")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--company", required=True)
    p_rm.add_argument("--title", required=True)
    p_apps = sub.add_parser("apps", help="TikTok application counter (2 per period; resets Jan 1 / Jul 1)")
    p_apps.add_argument("--log", default="", help="record an application you submitted (role title)")
    p_apps.add_argument("--set", type=int, default=None, help="set the used count explicitly")
    p_fin = sub.add_parser("finalize")
    p_fin.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    {"scan": cmd_scan, "add": cmd_add, "remove": cmd_remove,
     "finalize": cmd_finalize, "apps": cmd_apps}[args.cmd](args)


if __name__ == "__main__":
    main()
