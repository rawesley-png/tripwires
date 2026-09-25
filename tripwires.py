#!/usr/bin/env python3
"""
Tripwires — self-updating engine for the AI-cycle and US-market boards.

    python3 tripwires.py            # fetch everything, score both boards, render docs/index.html, alert on changes
    python3 tripwires.py --digest   # same, and always send the full status table
    python3 tripwires.py --test     # send a test alert only
    python3 tripwires.py --render   # re-render the page from the last saved data (no fetching)

Standard library only.  Settings come from a .env file next to this script (see README.md).

Hard data:   FRED (rates, credit, oil, VIX, CPI, unemployment, Fed target, yen), Yahoo Finance
             (S&P 500, RSP/SPY, IWM, NVDA), SEC EDGAR XBRL (Nvidia, Microsoft, Amazon, Alphabet, Meta).
Soft signals: one Claude call with web search (needs ANTHROPIC_API_KEY); without it those tiles say so.
Boards:      AI cycle (t1–t8), US market (m1–m10), Taiwan (k1–k12), Plumbing (r1–r6, e1–e3, n1–n4, q1–q2), Fiscal dominance (s1–s4, v1–v5, p1–p3).
Output:      docs/index.html (the app, data inlined), docs/data.json, history.json, state.json.
"""

import csv
import datetime as dt
import gzip
import io
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
TEMPLATE = os.path.join(HERE, "template.html")
STATE_FILE = os.path.join(HERE, "state.json")
HISTORY_FILE = os.path.join(HERE, "history.json")
CHANGELOG_FILE = os.path.join(HERE, "changelog.json")
ENV_FILE = os.path.join(HERE, ".env")
TODAY = dt.date.today().isoformat()

CONFIG = {
    "ten_year_amber": 4.75, "ten_year_red": 5.00,
    "hy_amber": 4.00, "hy_red": 5.00, "hy_jump_red": 1.50,
    "brent_amber": 110.0, "brent_red": 120.0, "brent_watch": 100.0,
    "vix_red": 30.0, "vix_amber": 22.0,
    "ma_days": 200, "ma_break_days": 10, "blowoff_return_30d": 0.35,
    "inv_minus_rev_growth_red": 25, "inv_days_jump_red": 1.30,
    "capex_to_ocf_amber": 0.80, "capex_to_ocf_red": 1.00,
    "capex_decel_amber": 15, "capex_decel_red": 30,
    "erp_red": 0.0, "erp_amber": 1.0,          # forward earnings yield minus 10-year, percentage points
    "breadth_amber": 50.0, "breadth_red": 65.0,   # % of S&P 500 members below their own 200-day average (counted by the engine)
    "cpi_amber": 3.5, "cpi_red": 4.0,
    "soft_model": "claude-sonnet-4-6",
    "alert_on": ["RED", "AMBER"],
    "history_cap": 120,
    "changelog_days": 90,           # days of daily change entries kept and shown on the Log tab
    "value_move_pct": 3.0,          # a hard-data value moving this much since the last run is logged even if the status didn't change
    "value_log_skip": ["r4", "q1", "q2", "p2", "m9"],   # countdowns, streaks and mirrors: never log their value moves
}
HYPERSCALERS = {"Microsoft": "0000789019", "Amazon": "0001018724", "Alphabet": "0001652044", "Meta": "0001326801"}
NVDA_CIK = "0001045810"
ORDER = {"GREEN": 0, "AMBER": 1, "RED": 2}


# ----------------------------------------------------------------------------- helpers
def load_env():
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http_get(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def http_post_json(url, body, headers, timeout=240):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def worst(*statuses):
    s = [x for x in statuses if x]
    return max(s, key=lambda x: ORDER.get(x, -1)) if s else "GREEN"


def tile(id, board, group, tag, name, question, trip, status, reading, evidence="", source="", value=None, subs=None, url=None):
    return {"id": id, "board": board, "group": group, "tag": tag, "name": name, "question": question, "trip": trip,
            "status": status, "reading": reading, "evidence": evidence, "source": source, "value": value,
            "subs": subs, "url": url, "checked": TODAY}


def failed(id, board, group, tag, name, question, trip, err):
    return tile(id, board, group, tag, name, question, trip, "AMBER", f"Check failed: {err}", "Fix the data source and re-run.", "")


# ----------------------------------------------------------------------------- data sources
_fred_cache = {}
def fred(series_id, limit=400):
    if series_id in _fred_cache:
        return _fred_cache[series_id]
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY not set")
    url = ("https://api.stlouisfed.org/fred/series/observations?"
           f"series_id={series_id}&api_key={key}&file_type=json&sort_order=desc&limit={limit}")
    obs = json.loads(http_get(url))["observations"]
    vals = [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "")]
    _fred_cache[series_id] = vals
    return vals  # newest first


def fred_ago(vals, days):
    cutoff = (dt.date.fromisoformat(vals[0][0]) - dt.timedelta(days=days)).isoformat()
    older = [v for v in vals if v[0] <= cutoff]
    return older[0][1] if older else None


_yahoo_cache = {}
def yahoo(ticker, rng="2y"):
    if ticker in _yahoo_cache:
        return _yahoo_cache[ticker]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range={rng}&interval=1d"
    d = json.loads(http_get(url))["chart"]["result"][0]
    closes = d["indicators"]["quote"][0]["close"]
    out = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date(), c) for t, c in zip(d["timestamp"], closes) if c is not None]
    _yahoo_cache[ticker] = out
    return out


def ma_state(closes, n):
    ma = sum(closes[-n:]) / n
    streak = 0
    for i in range(len(closes) - 1, n - 1, -1):
        if closes[i] < sum(closes[i - n + 1:i + 1]) / n:
            streak += 1
        else:
            break
    return ma, streak


_edgar_cache = {}
def edgar(cik):
    if cik in _edgar_cache:
        return _edgar_cache[cik]
    ua = os.environ.get("EDGAR_USER_AGENT", "Family office research admin@example.com")
    g = json.loads(http_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", headers={"User-Agent": ua}))["facts"]["us-gaap"]
    _edgar_cache[cik] = g
    return g


def _points(gaap, tags):
    pts = []
    for tag in tags:
        for p in gaap.get(tag, {}).get("units", {}).get("USD", []):
            if "start" in p and "end" in p:
                pts.append((dt.date.fromisoformat(p["start"]), dt.date.fromisoformat(p["end"]), float(p["val"])))
    return pts


def quarterly_flow(gaap, tags):
    pts = _points(gaap, tags)
    q = {}
    for s, e, v in pts:
        if 80 <= (e - s).days <= 100:
            q[e] = v
    by_start = {}
    for s, e, v in pts:
        by_start.setdefault(s, {})[e] = v
    for s, ends in by_start.items():
        prev_end, prev_val = None, 0.0
        for e in sorted(ends):
            if (e - s).days > 380:
                continue
            if e not in q and prev_end is not None and 80 <= (e - prev_end).days <= 100:
                q[e] = ends[e] - prev_val
            prev_end, prev_val = e, ends[e]
    ends = sorted(q)
    for s, e, v in pts:
        if 350 <= (e - s).days <= 380 and e not in q:
            prior = [x for x in ends if e - dt.timedelta(days=300) <= x < e]
            if len(prior) == 3:
                q[e] = v - sum(q[x] for x in prior)
    return dict(sorted(q.items()))


def instant_values(gaap, tags):
    out = {}
    for tag in tags:
        for p in gaap.get(tag, {}).get("units", {}).get("USD", []):
            if "start" not in p:
                out[dt.date.fromisoformat(p["end"])] = float(p["val"])
    return dict(sorted(out.items()))


def yoy(series, end):
    target = end - dt.timedelta(days=365)
    c = [e for e in series if abs((e - target).days) <= 20]
    return series[c[0]] if c else None


# ----------------------------------------------------------------------------- soft signals (Claude + web search)
SOFT_KEYS = ["hyperscaler_language", "cloud_backlog", "ai_lab_financing", "gpu_rental_prices", "hbm_memory",
             "tsmc_packaging", "used_gpu_market", "circular_financing", "earnings_estimates", "breadth",
             "china_taiwan_truce", "financial_accident", "policy_shock", "pandemic_cyber_disaster", "fed_outlook"]

SOFT_PROMPT = """You are refreshing two early-warning boards: one for the end of the AI data-center capex boom, one for a
20%+ decline in the US stock market. Today is {today}. Use web search and report only developments from the LAST 30 DAYS
plus the current level of any number asked for. For each key give: "status" (GREEN = nothing new or still favorable,
AMBER = early softening or elevated risk, RED = clear turn or event), "evidence" (one plain sentence with a date and a
number where possible), "url" (source), and any numeric fields listed. Be skeptical of hype in both directions.

Keys:
1. hyperscaler_language — Microsoft, Amazon, Alphabet, Meta: capex guidance direction and any "digestion", "optimization",
   "efficiency", "lumpy", or power-constraint language. RED on a guidance cut or slower growth guide.
2. cloud_backlog — Azure / AWS / Google Cloud revenue growth and reported backlog: accelerating or decelerating?
3. ai_lab_financing — OpenAI, Anthropic, xAI, neoclouds (CoreWeave etc.): any round delayed, downsized, or compute
   contract renegotiated, deferred, cancelled?
4. gpu_rental_prices — current on-demand median $/hr for H100 and B200; direction over 90 days; waitlists gone?
   numeric: "h100_hourly", "b200_hourly", "change_90d_pct".
5. hbm_memory — HBM/DRAM pricing and contract terms softening? Still sold out?
6. tsmc_packaging — TSMC advanced-packaging (CoWoS) expansion or capex guidance revised down? Latest monthly revenue trend.
7. used_gpu_market — top-tier GPUs (H100/B200) appearing on resale markets in volume?
8. circular_financing — new or unwound vendor financing / equity stakes between Nvidia and its customers, or hyperscaler
   stakes in AI labs affecting reported profits.
9. earnings_estimates — S&P 500: latest FactSet-style forward 12-month P/E ("forward_pe"), estimated next-quarter
   earnings growth ("next_q_growth_pct"), and revision direction over 30 days ("revision": "up" | "flat" | "down").
10. breadth — percent of S&P 500 stocks above their 200-day moving average ("pct_above_200dma"), and any breadth extremes.
11. china_taiwan_truce — US–China trade truce status (chip and rare-earth suspensions), Taiwan military activity
    (drills vs coast-guard inspections or quarantine), Hormuz status. RED on rules snapping back, a Chinese bank sanctioned,
    or a Taiwan quarantine.
12. financial_accident — Treasury market functioning (auction tails, liquidity), yen carry-trade stress, funding spreads,
    any large fund, lender, or private-credit failure. RED on an actual event.
13. policy_shock — broad new tariffs, Fed-independence conflict, debt-ceiling standoff, contested election. RED on an event.
14. pandemic_cyber_disaster — any pandemic, major cyberattack on infrastructure or markets, or disaster with national
    economic impact. Default GREEN.
15. fed_outlook — what the Fed did at its last meeting, what markets expect at the next one ("next_meeting_date"),
    and whether the Chair's guidance leans toward more hikes.

Respond with ONLY a JSON object, no prose and no markdown fences, whose top-level keys are exactly the 15 key names
above; each value is an object with "status", "evidence", "url" and the numeric fields for that key."""


TAIWAN_KEYS = ["taipei_red_lines", "beijing_language", "taiwan_politics", "us_commitment", "pla_readiness",
               "china_war_chest", "substitution_race", "taiwan_clock", "taiwan_will", "drills_to_inspections",
               "mobilization_signs", "war_risk_markets"]

TAIWAN_PROMPT = """You are refreshing an early-warning board for a Chinese blockade or invasion of Taiwan. Today is {today}.
Use web search. Report developments from the LAST 30 DAYS plus the current level of any number asked for. For each key
give "status" (GREEN = nothing new or moving away from conflict, AMBER = movement toward conflict or a condition that
opens the door, RED = a line crossed or an event), "evidence" (one plain sentence with a date and a number where possible),
"url" (source), and any numeric fields listed. Be skeptical of hype in both directions; drills are routine unless they
change form.

Keys:
1. taipei_red_lines — any formal step toward independence by Taiwan's government (referendum, constitutional change to
   name or territory, a declaration), or U.S. diplomatic recognition, defense treaty, or permanent U.S. troops. RED on any.
2. beijing_language — has Beijing dropped "peaceful reunification", invoked the Anti-Secession Law, formally declared the
   peaceful path exhausted, or escalated designations of Taiwan's leaders? Any change in Xi's stated timeline?
3. taiwan_politics — the KMT-DPP balance: latest presidential polling for 2028, the November 2026 local elections, the
   KMT chair's standing, Beijing's engagement with the KMT. AMBER if Beijing's peaceful route via the KMT is failing.
4. us_commitment — Taiwan arms-sale status (paused, delayed, approved), presidential statements on defending Taiwan,
   U.S. carriers and forces in the Western Pacific versus the Middle East, Japan and Philippines posture.
   AMBER when U.S. commitment is visibly weaker; RED on an explicit U.S. statement it will not defend Taiwan.
5. pla_readiness — purges or stability in PLA leadership, the 2027 readiness benchmark, amphibious and RO-RO capacity,
   large-scale exercises. Purges ongoing = brake (GREEN); leadership settled and exercises maturing = AMBER.
6. china_war_chest — China's crude stockpile and import trend ("stockpile_barrels_bn" if reported), grain stockpiles,
   gold buying, U.S. Treasury selling, capital controls. Drawing down = window closing (GREEN); refilling fast = AMBER.
7. substitution_race — China's progress toward chip self-sufficiency (EUV, SMIC nodes) and U.S. progress on non-Chinese
   rare-earth magnets; the status of the US-China trade truce and its expiry dates. AMBER as China nears sufficiency.
8. taiwan_clock — Taiwan's LNG reserve in days ("lng_days"), coal and oil reserves, defense special budget status,
   conscription and reserve reforms, latest Han Kuang results. RED if reserves shrink or the budget dies.
9. taiwan_will — latest polls on willingness to resist ("resist_pct"), trust in the U.S., identity. AMBER if resolve
   falls below 50% or trust in the U.S. keeps falling.
10. drills_to_inspections — any China Coast Guard boarding or inspection of Taiwan-bound shipping, quarantine language,
    changes to the median line or flight paths, fishing-fleet formations, blockade rehearsals; PLA aircraft per day
    around Taiwan, 30-day average ("pla_aircraft_per_day"). RED on any actual inspection or quarantine.
11. mobilization_signs — civilian ferry (RO-RO) requisition drills, reserve call-ups, PLA leave cancellations, blood
    drives, hospital or grain stockpiling orders, mobilization-law changes, evacuation advisories for Chinese nationals,
    sanctions-proofing (CIPS surge, Treasury dumping). AMBER on preparatory steps; RED on multiple simultaneous signs.
12. war_risk_markets — Taiwan Strait war-risk insurance premiums, Taiwan sovereign CDS, foreign outflows from Taiwan
    equities, any prediction-market moves ("polymarket_invasion_2027_pct" if you find it). AMBER on a sharp move.

Respond with ONLY a JSON object, no prose and no markdown fences, whose top-level keys are exactly the 12 key names
above; each value is an object with "status", "evidence", "url" and the numeric fields for that key."""


def soft_signals(prompt=None, keys=None):
    prompt = prompt or SOFT_PROMPT
    keys = keys or SOFT_KEYS
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, "ANTHROPIC_API_KEY not set — soft signals not checked"
    body = {"model": CONFIG["soft_model"], "max_tokens": 6000,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 25}],
            "messages": [{"role": "user", "content": prompt.format(today=TODAY)}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        resp = http_post_json("https://api.anthropic.com/v1/messages", body, headers)
    except urllib.error.HTTPError as e:
        return None, f"Claude API error {e.code}: {e.read()[:200]}"
    except Exception as e:
        return None, f"Claude API error: {e}"
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except Exception:
        return None, f"could not parse Claude output: {text[:200]}"
    for k in keys:
        d = data.get(k) or {}
        st = str(d.get("status", "AMBER")).upper()
        d["status"] = st if st in ORDER else "AMBER"
        data[k] = d
    return data, None


def soft_line(soft, key, fallback=""):
    if not soft:
        return "AMBER", "", ""
    d = soft.get(key, {})
    return d.get("status", "AMBER"), d.get("evidence", ""), d.get("url", "")



# ----------------------------------------------------------------------------- breadth: count the S&P 500 constituents below their 200-day average
CONSTITUENTS_FILE = os.path.join(HERE, "constituents.json")

def sp500_constituents():
    """Ticker list from Wikipedia, cached for 30 days in constituents.json."""
    try:
        if os.path.exists(CONSTITUENTS_FILE):
            c = json.load(open(CONSTITUENTS_FILE))
            if (dt.date.today() - dt.date.fromisoformat(c["fetched"])).days < 30 and len(c["symbols"]) > 450:
                return c["symbols"]
    except Exception:
        pass
    import html as _html, re as _re
    page = http_get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers={"User-Agent": "Mozilla/5.0"}).decode("utf-8", "ignore")
    tbl = page.split('id="constituents"')[1].split("</table>")[0]
    syms = []
    for row in _re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, _re.S)[1:]:
        cells = _re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, _re.S)
        if len(cells) >= 4:
            syms.append(_html.unescape(_re.sub(r"<[^>]+>", "", cells[0])).strip().replace(".", "-"))
    if len(syms) < 450:
        raise RuntimeError(f"constituent list too short ({len(syms)})")
    json.dump({"fetched": TODAY, "symbols": syms}, open(CONSTITUENTS_FILE, "w"))
    return syms


def breadth_below_200d(max_workers=8):
    """Percent of S&P 500 members closing below their own 200-day average. Returns (pct_below, counted)."""
    import concurrent.futures as cf
    syms = sp500_constituents()
    def one(t):
        for _ in range(2):
            try:
                cl = [c for _, c in yahoo(t, "1y")]
                if len(cl) < 200:
                    return None
                return cl[-1] < sum(cl[-200:]) / 200
            except Exception:
                continue
        return None
    res = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(one, syms):
            if r is not None:
                res.append(r)
    if len(res) < 400:
        raise RuntimeError(f"only {len(res)} constituents priced")
    return 100.0 * sum(res) / len(res), len(res)


# ----------------------------------------------------------------------------- hard checks used by both boards
def rates_state():
    v = fred("DGS10")
    latest = v[0][1]
    above = 0
    for _, x in v:
        if x >= CONFIG["ten_year_red"]:
            above += 1
        else:
            break
    st = "RED" if latest >= CONFIG["ten_year_red"] else "AMBER" if latest >= CONFIG["ten_year_amber"] else "GREEN"
    if latest < CONFIG["ten_year_red"] and max(x for _, x in v[:10]) >= CONFIG["ten_year_red"]:
        st = worst(st, "AMBER")
    return latest, above, v[0][0], st


def credit_state():
    v = fred("BAMLH0A0HYM2")
    latest = v[0][1]
    jump = latest - (fred_ago(v, 60) or latest)
    st = "GREEN"
    if latest >= CONFIG["hy_amber"]:
        st = "AMBER"
    if latest >= CONFIG["hy_red"] or jump >= CONFIG["hy_jump_red"]:
        st = "RED"
    return latest, jump, st


def brent_state():
    v = fred("DCOILBRENTEU")
    latest = v[0][1]
    st = "RED" if latest >= CONFIG["brent_red"] else "AMBER" if latest >= CONFIG["brent_watch"] else "GREEN"
    return latest, v[0][0], st


def cpi_state():
    v = fred("CPIAUCSL", limit=30)
    now, ago = v[0][1], v[12][1]
    yoy_pct = (now / ago - 1) * 100
    st = "RED" if yoy_pct >= CONFIG["cpi_red"] else "AMBER" if yoy_pct >= CONFIG["cpi_amber"] else "GREEN"
    return yoy_pct, v[0][0], st


# ----------------------------------------------------------------------------- AI board (t1–t8)
def ai_board(soft):
    T = []
    G = "ai"
    # t1 buyers borrowing
    try:
        rows, reds, ambers = [], 0, 0
        for name, cik in HYPERSCALERS.items():
            g = edgar(cik)
            capex = quarterly_flow(g, ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"])
            ocf = quarterly_flow(g, ["NetCashProvidedByUsedInOperatingActivities"])
            ends = sorted(e for e in capex if e in ocf)[-4:]
            r = sum(capex[e] for e in ends) / sum(ocf[e] for e in ends)
            reds += r >= CONFIG["capex_to_ocf_red"]; ambers += CONFIG["capex_to_ocf_amber"] <= r < CONFIG["capex_to_ocf_red"]
            rows.append(f"{name} {r:.2f}×")
        st = "RED" if reds >= 2 else "AMBER" if (reds or ambers >= 2) else "GREEN"
        T.append(tile("t1", G, "trigger", "Comes first", "The buyers are borrowing to buy chips",
                      "Are Microsoft, Amazon, Alphabet, and Meta spending more than the cash their businesses generate?",
                      "Trips when two of the four spend above their operating cash flow (trailing four quarters, cash capex only — leases excluded, so this understates).",
                      st, f"Trailing-year capex as a share of operating cash flow: {', '.join(rows)}. {reds} above 1.0, {ambers} above 0.8.",
                      "Companies that spend all their cash and borrow the rest eventually slow down.", "SEC filings", value=reds + 0.5 * ambers))
    except Exception as e:
        T.append(failed("t1", G, "trigger", "Comes first", "The buyers are borrowing to buy chips", "", "", e))
    # t2 circular financing
    st, ev, url = soft_line(soft, "circular_financing", "not checked (no Claude key)")
    T.append(tile("t2", G, "trigger", "Comes first", "Nvidia is financing its own customers",
                  "Is the chip seller lending or investing in the companies that buy its chips?",
                  "A structural condition, not a level — it stays lit until the circular deals unwind.",
                  "RED" if soft is None else worst("AMBER", st), (ev or "Present as of September 2026: Nvidia stakes in OpenAI and the neoclouds; hyperscaler gains on AI-lab stakes flowing into reported profits.") + ("" if soft else " Not re-checked this run (no Claude key)."),
                  "Lucent and Nortel financed their own customers in 1999, a year before they collapsed.", "Company disclosures", value=1, url=url))
    # t3 spending growth slowing (hard capex growth + soft language)
    try:
        rows, statuses = [], []
        for name, cik in HYPERSCALERS.items():
            g = edgar(cik)
            capex = quarterly_flow(g, ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"])
            ends = sorted(capex)
            ttm = lambda i: sum(capex[e] for e in ends[i - 3:i + 1])
            i = len(ends) - 1
            g_now = ttm(i) / ttm(i - 4) - 1
            g_prev = ttm(i - 1) / ttm(i - 5) - 1
            decel = (g_prev - g_now) * 100
            s = "RED" if decel >= CONFIG["capex_decel_red"] else "AMBER" if decel >= CONFIG["capex_decel_amber"] else "GREEN"
            statuses.append(s); rows.append(f"{name} {g_now:+.0%} (was {g_prev:+.0%})")
        hard = "RED" if statuses.count("RED") >= 2 else "AMBER" if ("RED" in statuses or statuses.count("AMBER") >= 2) else "GREEN"
        s1, e1, u1 = soft_line(soft, "hyperscaler_language", "call language not checked")
        s2, e2, _ = soft_line(soft, "cloud_backlog", "backlog not checked")
        st = worst(hard, s1 if soft else "GREEN", s2 if soft else "GREEN")
        T.append(tile("t3", G, "bell", "Rings the bell", "Spending growth is slowing",
                      "Are the big buyers still raising next year's spending forecasts, or starting to talk about 'digestion'?",
                      "Trips on the first cut to the growth rate — not the level — or the word 'digestion' on a call.",
                      st, f"Trailing-year capex growth: {'; '.join(rows)}. {e1} {e2}".strip(),
                      "This is the bell. Historically it rings one to two quarters before the guidance cut everyone waits for.", "SEC filings; earnings calls", value=round(sum(1 for s in statuses if s != 'GREEN')), url=u1))
    except Exception as e:
        T.append(failed("t3", G, "bell", "Rings the bell", "Spending growth is slowing", "", "", e))
    # t4 chips easier to get
    try:
        g = edgar(NVDA_CIK)
        rev = quarterly_flow(g, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"])
        cogs = quarterly_flow(g, ["CostOfRevenue", "CostOfGoodsAndServicesSold"])
        inv = instant_values(g, ["InventoryNet"])
        end = max(e for e in rev if e in inv)
        inv_g = inv[end] / yoy(inv, end) - 1
        rev_g = rev[end] / yoy(rev, end) - 1
        gap = (inv_g - rev_g) * 100
        days = inv[end] / cogs[end] * 91 if end in cogs else None
        hard = "RED" if gap >= CONFIG["inv_minus_rev_growth_red"] else "AMBER" if gap >= CONFIG["inv_minus_rev_growth_red"] / 2 else "GREEN"
        s1, e1, u1 = soft_line(soft, "gpu_rental_prices", "rental prices not checked")
        s2, e2, _ = soft_line(soft, "hbm_memory", "")
        s3, e3, _ = soft_line(soft, "used_gpu_market", "")
        st = worst(hard, *( [s1, s2, s3] if soft else [] ))
        rental = soft.get("gpu_rental_prices", {}) if soft else {}
        h100 = rental.get("h100_hourly")
        T.append(tile("t4", G, "bell", "Rings the bell", "Chips are getting easier to get",
                      "Are rental prices for AI computing falling, waitlists disappearing, inventory piling up?",
                      "Trips on a sustained 20–30% drop in rental prices, or Nvidia inventory growing 25 points faster than sales.",
                      st, f"Nvidia inventory {inv_g:+.0%} vs sales {rev_g:+.0%} (quarter ended {end}); inventory days {days:.0f}. {e1} {e2} {e3}".strip(),
                      "In 2000 Cisco's inventory outran its sales two quarters before the crash — visible in the public filings.",
                      "SEC filings; GPU rental indexes", value=float(h100) if isinstance(h100, (int, float)) else round(gap, 1), url=u1))
    except Exception as e:
        T.append(failed("t4", G, "bell", "Rings the bell", "Chips are getting easier to get", "", "", e))
    # t5 rates
    try:
        latest, above, date, st = rates_state()
        T.append(tile("t5", G, "pressure", "Pressure", "Interest rates keep rising", "Is the 10-year Treasury yield holding above 5%?",
                      "Amber at 4.75%; trips at 5% sustained.", st, f"{latest:.2f}% on {date}; {above} consecutive readings at or above 5%.",
                      "Higher rates make the borrowing in tile 1 more expensive and expensive stocks worth less on paper.", "Federal Reserve (FRED)", value=latest))
    except Exception as e:
        T.append(failed("t5", G, "pressure", "Pressure", "Interest rates keep rising", "", "", e))
    # t6 lenders
    try:
        latest, jump, hard = credit_state()
        s1, e1, u1 = soft_line(soft, "ai_lab_financing", "AI-lab financing not checked")
        st = worst(hard, s1 if soft else "GREEN")
        T.append(tile("t6", G, "pressure", "Pressure", "Lenders are getting nervous",
                      "Are bond investors demanding more to lend to the AI borrowers, and are the labs still raising money easily?",
                      "Trips on spreads above 5%, a 1.5-point jump in 60 days, or a delayed or downsized AI-lab round.",
                      st, f"High-yield spread {latest:.2f}% ({jump:+.2f} pts in 60 days). {e1}".strip(),
                      "Watch Oracle- and CoreWeave-type borrowers first; they are the leveraged link between the labs and Nvidia.", "FRED; financing headlines", value=latest, url=u1))
    except Exception as e:
        T.append(failed("t6", G, "pressure", "Pressure", "Lenders are getting nervous", "", "", e))
    # t7 NVDA price
    try:
        px = [c for _, c in yahoo("NVDA")]
        ma, streak = ma_state(px, CONFIG["ma_days"])
        ret30 = px[-1] / px[-31] - 1
        st = "RED" if streak >= CONFIG["ma_break_days"] else "AMBER" if streak else "GREEN"
        if ret30 >= CONFIG["blowoff_return_30d"]:
            st = worst(st, "AMBER")
        T.append(tile("t7", G, "pressure", "Pressure", "The stock is acting wrong", "Has Nvidia broken below its one-year average price, or gone vertical?",
                      "Trips on 10 straight closes below the 200-day average, or +35% in 30 sessions.", st,
                      f"${px[-1]:.0f} vs 200-day average ${ma:.0f}; {streak} closes below it; {ret30:+.0%} over 30 sessions.",
                      "Picks-and-shovels stocks peak before their customers' spending does.", "Yahoo Finance", value=round(px[-1], 2)))
    except Exception as e:
        T.append(failed("t7", G, "pressure", "Pressure", "The stock is acting wrong", "", "", e))
    # t8 oil
    try:
        latest, date, st = brent_state()
        T.append(tile("t8", G, "pressure", "Pressure", "Oil", "Is Brent crude high enough to force more rate hikes?",
                      "Watch at $100, amber at $110, trips at $120.", st, f"Brent ${latest:.0f} on {date}.",
                      "Oil is the reason rates are rising.", "FRED", value=latest))
    except Exception as e:
        T.append(failed("t8", G, "pressure", "Pressure", "Oil", "", "", e))
    return T


# ----------------------------------------------------------------------------- Market board (m1–m10)
def market_board(soft, ai_tiles):
    T = []
    G = "market"
    # m1 rates
    try:
        latest, above, date, st = rates_state()
        T.append(tile("m1", G, "trigger", "Trigger: borrowing costs jump", "Borrowing costs have jumped", "Is the 10-year Treasury yield holding above 5%?",
                      "Trips when the 10-year holds above 5% for more than a few days.", st, f"{latest:.2f}% on {date}; {above} consecutive readings at or above 5%.",
                      "Rates, not earnings, are what has moved this year.", "FRED", value=latest))
    except Exception as e:
        T.append(failed("m1", G, "trigger", "Trigger: borrowing costs jump", "Borrowing costs have jumped", "", "", e))
    # m2 equity risk premium
    try:
        latest, _, _, _ = rates_state()
        est = soft.get("earnings_estimates", {}) if soft else {}
        fpe = est.get("forward_pe")
        fpe = float(fpe) if isinstance(fpe, (int, float)) else float(os.environ.get("FORWARD_PE_FALLBACK", "0") or 0)
        if not fpe:
            raise RuntimeError("no forward P/E (set FORWARD_PE_FALLBACK in .env or add ANTHROPIC_API_KEY)")
        ey = 100 / fpe
        erp = ey - latest
        st = "RED" if erp <= CONFIG["erp_red"] else "AMBER" if erp <= CONFIG["erp_amber"] else "GREEN"
        T.append(tile("m2", G, "amplifier", "Amplifier", "Stocks yield no more than bonds", "Does the market pay you anything extra for owning stocks instead of Treasuries?",
                      "Trips when the earnings yield falls to or below the 10-year yield.", st,
                      f"Forward P/E {fpe:.1f} → earnings yield {ey:.2f}% against a 10-year at {latest:.2f}%: premium {erp:+.2f} points.",
                      "Expensive markets don't fall because they're expensive; they fall when a trigger hits, and valuation decides how far.", "FactSet-style estimate via web check; FRED", value=round(erp, 2)))
    except Exception as e:
        T.append(failed("m2", G, "amplifier", "Amplifier", "Stocks yield no more than bonds", "", "", e))
    # m3 earnings
    est = soft.get("earnings_estimates", {}) if soft else {}
    growth = est.get("next_q_growth_pct")
    rev = str(est.get("revision", "")).lower()
    st = "AMBER" if soft is None else ("RED" if (rev == "down" or (isinstance(growth, (int, float)) and growth < 10)) else "AMBER" if rev == "flat" else "GREEN")
    T.append(tile("m3", G, "trigger", "Trigger: earnings stop growing", "Earnings stop growing", "Are company profits still rising, and are forecasts still going up?",
                  "Trips when forecasts for the next quarter start being cut, or growth falls below 10%.", st,
                  (f"Next-quarter S&P 500 earnings growth estimate {growth}%; revisions {rev or 'unknown'}. " if soft else "Not checked (no Claude key). ") + est.get("evidence", ""),
                  "About half of this year's earnings growth comes from AI-infrastructure companies.", "FactSet-style estimates via web check",
                  value=float(growth) if isinstance(growth, (int, float)) else None, url=est.get("url")))
    # m4 breadth
    try:
        spx = [c for _, c in yahoo("^GSPC")]
        ma, streak = ma_state(spx, CONFIG["ma_days"])
        rsp = [c for _, c in yahoo("RSP")]; spy = [c for _, c in yahoo("SPY")]
        n = min(len(rsp), len(spy)); rel = (rsp[-1] / rsp[-61]) / (spy[-1] / spy[-61]) - 1 if n > 61 else 0.0
        b = soft.get("breadth", {}) if soft else {}
        counted = None
        try:
            below, counted = breadth_below_200d()
            below = round(below, 1)
        except Exception as be:
            print("breadth count failed, using web check:", be)
            above = b.get("pct_above_200dma")
            below = 100 - float(above) if isinstance(above, (int, float)) else None
        st = "RED" if streak >= CONFIG["ma_break_days"] else "GREEN"
        if below is not None and below >= CONFIG["breadth_red"]:
            st = worst(st, "RED")
        elif below is not None and below >= CONFIG["breadth_amber"]:
            st = worst(st, "AMBER")
        if rel <= -0.05:
            st = worst(st, "AMBER")
        T.append(tile("m4", G, "amplifier", "Amplifier", "The average stock is already falling", "Is the index rising on a few giant companies while most stocks fall?",
                      "Watch at half the members below their own 200-day average; red at two-thirds, or when the index itself breaks its 200-day average.", st,
                      f"S&P 500 {spx[-1]:,.0f} vs 200-day {ma:,.0f} ({streak} closes below). "
                      + (f"{below:.0f}% of {counted} members below their own 200-day average (counted). " if counted else (f"{below:.0f}% of members below their 200-day (web check). " if below is not None else ""))
                      + f"Equal-weight vs cap-weight over 60 sessions: {rel:+.1%}. " + b.get("evidence", ""),
                      "Narrow leadership is how the last two bear markets began.", "Yahoo Finance, all constituents; web check as fallback", value=below if below is not None else round(rel * 100, 1)))
    except Exception as e:
        T.append(failed("m4", G, "amplifier", "Amplifier", "The average stock is already falling", "", "", e))
    # m5 credit and fear
    try:
        latest, jump, hard = credit_state()
        vix = fred("VIXCLS")[0][1]
        st = worst(hard, "RED" if vix >= CONFIG["vix_red"] else "AMBER" if vix >= CONFIG["vix_amber"] else "GREEN")
        T.append(tile("m5", G, "amplifier", "Amplifier", "Lenders are getting nervous", "Are bond investors demanding more to lend to risky companies?",
                      "Trips on spreads above 5%, a 1.5-point jump in 60 days, or a VIX that stays above 30.", st,
                      f"High-yield spread {latest:.2f}% ({jump:+.2f} pts in 60 days); VIX {vix:.1f}.",
                      "Credit usually cracks after rates and before earnings.", "FRED", value=latest))
    except Exception as e:
        T.append(failed("m5", G, "amplifier", "Amplifier", "Lenders are getting nervous", "", "", e))
    # m6 outside shock (five sub-lamps, worst wins)
    subs = []
    try:
        brent, bdate, bst = brent_state(); cpi, cdate, cst = cpi_state()
        subs.append({"name": "Energy", "status": worst(bst, cst), "reading": f"Brent ${brent:.0f} ({bdate}); CPI {cpi:.1f}% y/y ({cdate}). Red at $120 or inflation above 4%."})
    except Exception as e:
        subs.append({"name": "Energy", "status": "AMBER", "reading": f"check failed: {e}"})
    for key, name, red_rule in [("china_taiwan_truce", "China, Taiwan, the trade truce", "Red if the chip and rare-earth rules snap back, a Chinese bank is sanctioned, or Taiwan sees a quarantine."),
                                ("financial_accident", "A financial accident", "Red on a Treasury-market seizure, a carry-trade unwind, or a large fund or lender failing."),
                                ("policy_shock", "Policy shock", "Red on broad new tariffs, a Fed-independence fight, or a debt-ceiling standoff."),
                                ("pandemic_cyber_disaster", "Pandemic, cyberattack, disaster", "No warning possible; red only when it happens.")]:
        s, ev, _ = soft_line(soft, key)
        if not soft:
            ev = "Not checked this run (no Claude key)."
        if key == "financial_accident":
            try:
                jpy = fred("DEXJPUS", limit=60); move = (jpy[0][1] / (fred_ago(jpy, 30) or jpy[0][1]) - 1) * 100
                ev = f"{ev} Yen {move:+.1f}% in 30 days.".strip()
                if abs(move) >= 6:
                    s = worst(s, "AMBER")
            except Exception:
                pass
        subs.append({"name": name, "status": s, "reading": f"{ev} {red_rule}".strip()})
    st = worst(*[x["status"] for x in subs])
    T.append(tile("m6", G, "trigger", "Trigger: outside shock — any one of five", "Outside shock", "Has anything from outside the market hit hard enough to force earnings down or rates up?",
                  "Trips the moment any one of the five below goes red.", st,
                  f"{sum(1 for x in subs if x['status'] == 'RED')} red, {sum(1 for x in subs if x['status'] == 'AMBER')} on watch, of five.",
                  "The market treats every oil rise as a rate rise; the financial-accident row matters most while every one of its ingredients is present.", "FRED; web check",
                  value=sum(1 for x in subs if x["status"] != "GREEN"), subs=subs))
    # m7 economy
    try:
        un = fred("UNRATE", limit=24); sahm = fred("SAHMREALTIME", limit=6)[0][1]
        low = min(x for _, x in un); latest = un[0][1]
        claims = fred("ICSA", limit=60); c4 = sum(x for _, x in claims[:4]) / 4; c_low = min(x for _, x in claims)
        st = "RED" if (latest - low >= 0.5 or sahm >= 0.5) else "AMBER" if (latest - low >= 0.3 or c4 >= c_low * 1.25) else "GREEN"
        T.append(tile("m7", G, "driver", "Feeds the earnings trigger", "The economy", "Is a recession starting?",
                      "Trips when unemployment rises half a point from its low (the Sahm rule), or claims jump.", st,
                      f"Unemployment {latest:.1f}% ({un[0][0]}), low {low:.1f}%; Sahm indicator {sahm:.2f}; jobless claims 4-week average {c4:,.0f} vs 52-week low {c_low:,.0f}.",
                      "Strong demand is why the Fed keeps raising.", "FRED", value=latest))
    except Exception as e:
        T.append(failed("m7", G, "driver", "Feeds the earnings trigger", "The economy", "", "", e))
    # m9 AI bell mirror
    t3 = next((t for t in ai_tiles if t["id"] == "t3"), None); t4 = next((t for t in ai_tiles if t["id"] == "t4"), None)
    lit = sum(1 for t in (t3, t4) if t and t["status"] != "GREEN")
    st = "RED" if lit == 2 else "AMBER" if lit == 1 else "GREEN"
    T.append(tile("m9", G, "driver", "Feeds the earnings trigger", "The AI-cycle bell", "Has the AI spending boom started to turn? (Mirrors the AI board.)",
                  "Trips when the AI board's bell rings: spending growth slowing and rental prices falling at the same time.", st,
                  ("Rung. " if lit == 2 else "Half-lit. " if lit == 1 else "Not rung. ") + f"Spending growth: {t3['status'].title() if t3 else '?'}; chips easier: {t4['status'].title() if t4 else '?'}.",
                  "Because AI companies supply half of index earnings growth, that bell is the likeliest trigger for the earnings tile.", "AI board", value=lit))
    # m10 Fed
    try:
        tgt = fred("DFEDTARU", limit=200); latest = tgt[0][1]; ago = fred_ago(tgt, 90) or latest
        ten, _, _, _ = rates_state()
        s1, e1, u1 = soft_line(soft, "fed_outlook", "outlook not checked")
        hiked = latest > ago
        st = worst("RED" if (hiked and ten >= CONFIG["ten_year_red"]) else "AMBER" if hiked else "GREEN", s1 if soft else "GREEN")
        T.append(tile("m10", G, "driver", "Feeds the rates trigger", "The Fed", "Is the central bank tightening into a stretched market?",
                      "Trips on a hike while the 10-year is above 5%, or a surprise at the next meeting.", st,
                      f"Fed funds target (upper) {latest:.2f}%, {'up' if hiked else 'unchanged or down'} from {ago:.2f}% 90 days ago. {e1}".strip(),
                      "The last hiking cycle coincided with a 20%+ decline.", "FRED; Fed communications via web check", value=latest, url=u1))
    except Exception as e:
        T.append(failed("m10", G, "driver", "Feeds the rates trigger", "The Fed", "", "", e))
    return T


# ----------------------------------------------------------------------------- Taiwan board (k1–k12)
def polymarket_taiwan():
    """Public Polymarket prices for the Taiwan markets: {question: yes_probability}."""
    out = {}
    for q in ("taiwan invade", "taiwan blockade", "taiwan military clash"):
        try:
            d = json.loads(http_get(f"https://gamma-api.polymarket.com/public-search?q={urllib.parse.quote(q)}&limit_per_type=10",
                                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}))
            for e in d.get("events", []):
                for m in e.get("markets", []):
                    if m.get("closed"):
                        continue
                    end = (m.get("endDate") or "")[:10]
                    if end and end < TODAY:
                        continue
                    try:
                        yes = float(json.loads(m.get("outcomePrices", "[0]"))[0])
                    except Exception:
                        continue
                    out[m.get("question", "")] = (yes, end)
        except Exception:
            continue
    return out


def taiwan_board(soft):
    T = []
    G = "taiwan"
    def soft_tile(id, group, tag, name, question, trip, key, evidence_note, source, default_status="AMBER", value_key=None):
        st, ev, url = soft_line(soft, key)
        d = soft.get(key, {}) if soft else {}
        val = d.get(value_key) if value_key else None
        reading = ev if soft else "Not checked this run (no Claude key)."
        T.append(tile(id, G, group, tag, name, question, trip, st if soft else default_status, reading, evidence_note, source,
                      value=float(val) if isinstance(val, (int, float)) else None, url=url))
    # Reason
    soft_tile("k1", "reason", "Reason: a red line crossed", "Taipei's red lines",
              "Has Taiwan taken a formal step toward independence, or has the U.S. recognized it?",
              "Trips on a referendum, a constitutional change to name or territory, a declaration, U.S. recognition, or U.S. troops. The one thing that makes a Chinese move near-certain.",
              "taipei_red_lines", "Written into China's 2005 Anti-Secession Law; every leader since has repeated it.", "Web check", default_status="GREEN")
    soft_tile("k2", "reason", "Reason: Beijing declares the peaceful path dead", "Beijing's language",
              "Has Beijing stopped saying 'peaceful reunification', or declared peaceful means exhausted?",
              "Trips when Beijing invokes the Anti-Secession Law's third condition or drops the peaceful framing. Amber when the KMT route is failing.",
              "beijing_language", "The third legal trigger is a judgment call Xi controls — which is exactly why he can wait.", "Web check")
    soft_tile("k3", "reason", "Feeds the reason", "Taiwan's politics",
              "Is Xi's peaceful route — a KMT government in 2028 — still alive?",
              "Amber if the KMT is losing ground or Beijing's engagement backfires; a DPP win in January 2028 is the hinge.",
              "taiwan_politics", "January 2028 is the face trap: if the KMT loses after Xi hosted its chair, the peaceful story loses its vehicle.", "Web check; polls")
    # Opening
    soft_tile("k4", "opening", "Opening", "U.S. commitment",
              "Does Washington look willing and able to fight for Taiwan?",
              "Amber when arms sales are paused, statements hedge, or carriers are elsewhere; red on an explicit statement of non-defense.",
              "us_commitment", "The denial strategy is built for this fight; the president has said he made no commitment either way.", "Web check")
    soft_tile("k5", "opening", "Opening", "PLA readiness",
              "Is the PLA's leadership settled and its amphibious force mature?",
              "Purges under way are a brake (green). Settled command plus maturing exercises is amber.",
              "pla_readiness", "You don't fire your commanders on the eve of a war you expect to win.", "Web check", default_status="GREEN")
    soft_tile("k6", "opening", "Opening", "China's war chest",
              "Are China's oil, grain, and financial reserves being built or drawn down?",
              "Drawing down (as in 2026) closes the window: green. Refilling fast is amber.",
              "china_war_chest", "1.2–1.5 billion barrels bought in 2024–25 is what let China absorb the Iran and Venezuela cuts.", "Web check; customs data", default_status="GREEN", value_key="stockpile_barrels_bn")
    soft_tile("k7", "opening", "Opening", "The substitution race",
              "Is China nearing the point where it can absorb a rupture, before the U.S. can?",
              "Amber as China nears chip sufficiency while U.S. magnet supply is unfinished; the window is 2028–2032.",
              "substitution_race", "The side that finishes first gets to choose the break.", "Web check")
    soft_tile("k8", "opening", "Opening", "Taiwan's own clock",
              "How long could Taiwan last under a blockade, and is it getting longer?",
              "Red if LNG reserves shrink or the defense budget dies; green only when the clock is visibly lengthening.",
              "taiwan_clock", "Eleven days of gas is the most important number in this whole picture.", "Web check", value_key="lng_days")
    soft_tile("k9", "opening", "Opening", "Taiwan's will",
              "Do the Taiwanese still say they would fight, and do they believe help is coming?",
              "Amber if willingness to resist falls below half or trust in the U.S. keeps falling.",
              "taiwan_will", "Giving up is a function of dark cities and whether anyone believes help is coming.", "Polls via web check", default_status="GREEN", value_key="resist_pct")
    # Decision
    soft_tile("k10", "decision", "Decision", "From drills to inspections",
              "Has China moved from exercises to actually stopping ships?",
              "Trips on any coast-guard boarding or inspection of Taiwan-bound shipping, or a declared quarantine. Drills alone stay green.",
              "drills_to_inspections", "Drills are cheap face; inspections are the first act of a blockade.", "Web check; Taiwan MND", default_status="GREEN", value_key="pla_aircraft_per_day")
    soft_tile("k11", "decision", "Decision", "Mobilization signs",
              "Is China doing the things a country does in the months before it fights?",
              "Amber on preparatory steps (ferry requisition drills, reserve call-ups, mobilization-law changes); red on several at once.",
              "mobilization_signs", "The mobilization-law revision of August 2026 is one; watch for the rest.", "Web check")
    # Confirmation from markets (hard data + Polymarket)
    try:
        twd = [c for _, c in yahoo("TWD=X", "1y")]; ewt = [c for _, c in yahoo("EWT", "1y")]; spy = [c for _, c in yahoo("SPY", "1y")]
        twd_move = (twd[-1] / twd[-22] - 1) * 100                # + = Taiwan dollar weakening
        rel = ((ewt[-1] / ewt[-61]) / (spy[-1] / spy[-61]) - 1) * 100
        pm = polymarket_taiwan()
        inv = [(q, p, e) for q, (p, e) in pm.items() if "invade" in q.lower()]
        blk = [(q, p, e) for q, (p, e) in pm.items() if "blockade" in q.lower()]
        far = max(inv, key=lambda x: x[2]) if inv else None
        soon = min(inv, key=lambda x: x[2]) if inv else None
        st = "GREEN"
        if twd_move >= 3 or rel <= -8:
            st = "AMBER"
        if far and far[1] >= 0.25 or twd_move >= 6 or rel <= -15:
            st = "RED"
        s1, e1, u1 = soft_line(soft, "war_risk_markets")
        st = worst(st, s1 if soft else "GREEN")
        pm_txt = "; ".join(f"{q} {p:.0%}" for q, p, e in sorted(inv + blk, key=lambda x: x[2])[:4]) if (inv or blk) else "Polymarket unavailable"
        T.append(tile("k12", G, "decision", "Confirmation, not a trigger", "Markets smell it",
                      "Are money and insurers pricing a conflict?",
                      "Amber on a 3% fall in the Taiwan dollar in a month, Taiwan stocks lagging the S&P by 8% in 60 sessions, or a sharp insurance move; red at 25% invasion odds or double those moves.",
                      st, f"Taiwan dollar {twd_move:+.1f}% vs USD in 30 days; Taiwan ETF vs S&P 500 over 60 sessions {rel:+.1f}%. Prediction markets: {pm_txt}. {e1}".strip(),
                      "Markets price the decision before governments announce it; they also panic for other reasons, so this confirms rather than triggers.",
                      "Yahoo Finance; Polymarket; web check", value=round(far[1] * 100, 1) if far else None, url=u1))
    except Exception as e:
        T.append(failed("k12", G, "decision", "Confirmation, not a trigger", "Markets smell it", "", "", e))
    return T



# ============================================================================= plumbing and fiscal-dominance boards (added 2026-09-25)
PLUMBING_KEYS = ["srf_usage", "auction_results", "bank_stress", "xccy_basis", "boj_policy", "basis_trade",
                 "clearing_transition", "stablecoins", "private_credit", "fed_operations"]

PLUMBING_PROMPT = """You are refreshing an early-warning board for a seizure in the U.S. overnight funding (repo) market and the
offshore dollar market. Today is {today}. Use web search and report only developments from the LAST 30 DAYS plus the
current level of any number asked for. For each key give: "status" (GREEN = calm, AMBER = watch, RED = event under way),
"evidence" (one plain sentence with a date and a number where possible), "url" (source), and any numeric fields listed.

1. "srf_usage": use of the Fed's standing repo facility and any emergency repo operations. AMBER at $10bn+ on a day that
   is not a month-end; RED at $50bn+ or use on consecutive ordinary days. Field "srf_bn" (latest daily amount).
2. "auction_results": recent Treasury coupon auctions and buybacks: tails, bid-to-cover, dealer share, any sign of
   dealers refusing balance sheet or bid-ask widening. RED on a failed or badly tailed auction. Field "tail_bp".
3. "bank_stress": large-bank CDS moves, discount-window use, deposit flight to money funds. RED on a CDS spike or a bank
   drawing the window at size.
4. "xccy_basis": the 3-month cross-currency basis for yen and euro against the dollar. AMBER beyond -30bp outside a
   quarter-end; RED beyond -60bp. Field "basis_bp" (yen, negative = dollars scarce).
5. "boj_policy": Bank of Japan rate decisions and guidance; yen carry-trade unwind signs. AMBER while hiking; RED on a
   surprise hike or a 5% weekly yen move.
6. "basis_trade": size of the hedge-fund Treasury basis trade (OFR, CFTC, Fed notes) and any sign of unwind. Field
   "size_tn".
7. "clearing_transition": the SEC Treasury central-clearing mandate (cash trades Dec 2026, repo June 2027): delays,
   clearing-house margin changes, dealers dropping counterparties.
8. "stablecoins": total stablecoin supply and any large issuer trading below par. RED on a depeg lasting more than a
   day or supply down 10% in a week. Field "supply_bn".
9. "private_credit": redemption gates or limits at large private-credit funds and BDCs; median BDC discount to NAV.
   RED while large funds are gating.
10. "fed_operations": whether the Fed is conducting repo or purchase operations in response to funding stress, and
    whether purchases begun in a stress episode are continuing after it ended.

Respond with ONLY a JSON object, no prose and no markdown fences, whose top-level keys are exactly the 10 key names
above; each value is an object with "status", "evidence", "url" and the numeric fields for that key."""

FISCAL_KEYS = ["fed_purchases_scope", "crisis_purchase_persisting", "political_pressure", "yield_ceiling_talk",
               "foreign_demand", "fed_position"]

FISCAL_PROMPT = """You are refreshing an early-warning board for fiscal dominance: the Fed being turned into the Treasury's
financier by buying bonds to hold yields below inflation. Today is {today}. Use web search and report only developments
from the LAST 30 DAYS plus the current level of any number asked for. For each key give: "status" (GREEN = not
happening, AMBER = pressure or early sign, RED = the step has been taken), "evidence" (one plain sentence with a date
and a number where possible), "url" (source), and any numeric fields listed.

1. "fed_purchases_scope": what the Fed is buying and why. Bill purchases described as reserve management = GREEN or
   AMBER if growing fast; coupon purchases, or purchases described as supporting the Treasury market outside a stress
   episode = RED. Field "monthly_bn".
2. "crisis_purchase_persisting": whether any purchase program begun for financial stability has continued after the
   episode's own indicators (repo rates, auction results) normalized. RED if so.
3. "political_pressure": statements by the President, Treasury Secretary or Congress about long-term yields being too
   high, calls for the Fed to hold them down, challenges to Fed independence, dissents for cuts from governors appointed
   for that purpose. RED when the executive names the 10-year or 30-year as a target in public.
4. "yield_ceiling_talk": any Fed official, speech or minutes mentioning yield-curve control, yield caps, or unlimited
   purchases as an option. AMBER if discussed; RED if announced.
5. "foreign_demand": foreign official participation at auctions (indirect bidder share), TIC data on foreign Treasury
   holdings, central-bank gold purchases, yuan settlement of oil. AMBER on a falling trend; RED on a sharp drop or a
   tailed auction attributed to foreign absence. Field "indirect_pct".
6. "fed_position": the Fed's own financial position (deferred asset, operating losses) becoming a political target,
   legislation aimed at the Fed, leadership changes. Field "deferred_asset_bn".

Respond with ONLY a JSON object, no prose and no markdown fences, whose top-level keys are exactly the 6 key names
above; each value is an object with "status", "evidence", "url" and the numeric fields for that key."""

CONFIG.update({
    "sofr_spread_amber": 0.10, "sofr_spread_red": 0.25,      # SOFR minus IORB, percentage points
    "srf_amber_bn": 10.0, "srf_red_bn": 50.0,
    "rrp_gone_bn": 50.0,
    "move_amber": 100.0, "move_red": 140.0,
    "dw_amber_bn": 10.0, "dw_red_bn": 50.0,
    "swap_amber_bn": 5.0, "swap_red_bn": 50.0,
    "yen_week_amber": 3.0, "yen_week_red": 5.0,
    "breakeven_amber": 2.6, "breakeven_red": 3.0,
    "real30_amber": 2.0,
    "long_end_amber": 5.0, "long_end_red": 5.5,
    "dollar_month_amber": -3.0, "dollar_month_red": -5.0,
    "drain_window_days": 5,
})


def fred_recent(series_id, n):
    """Newest-first list of (date, value), length n."""
    return fred(series_id, limit=max(n, 20))[:n]


def _last_business_days(vals, n):
    return [v for v in vals[:n]]


def drain_dates(today):
    """Upcoming cash-drain dates: quarter-ends, mid-month and month-end coupon settlements, corporate tax dates."""
    t = dt.date.fromisoformat(today)
    out = []
    for m in range(0, 4):
        y, mo = t.year + (t.month - 1 + m) // 12, (t.month - 1 + m) % 12 + 1
        last = (dt.date(y + (mo == 12), (mo % 12) + 1, 1) - dt.timedelta(days=1))
        out.append((last, "quarter-end" if mo in (3, 6, 9, 12) else "month-end settlement"))
        out.append((dt.date(y, mo, 15), "mid-month coupon settlement" + (" and corporate tax date" if mo in (3, 4, 6, 9, 12) else "")))
    out = sorted({d: k for d, k in out if d >= t}.items())
    return out


def plumbing_board(soft):
    T = []
    G = "plumbing"
    c = CONFIG

    def soft_tile(id, group, tag, name, question, trip, key, evidence_note, source, default_status="AMBER", value_key=None):
        st, ev, url = soft_line(soft, key)
        d = soft.get(key, {}) if soft else {}
        val = d.get(value_key) if value_key else None
        reading = ev if soft else "Not checked this run (no Claude key)."
        T.append(tile(id, G, group, tag, name, question, trip, st if soft else default_status, reading, evidence_note, source,
                      value=float(val) if isinstance(val, (int, float)) else None, url=url))

    # --- r1 SOFR against the rate on reserves (hard)
    try:
        sofr = fred_recent("SOFR", 30); iorb = fred_recent("IORB", 30)
        srf_ceiling = fred_recent("DFEDTARU", 5)[0][1]  # top of target range = standing repo minimum bid rate
        latest = sofr[0][1]; spread = latest - iorb[0][1]
        spreads = [s[1] - next((i[1] for i in iorb if i[0] <= s[0]), iorb[-1][1]) for s in sofr[:10]]
        d0 = dt.date.fromisoformat(sofr[0][0]); month_end = d0.day >= 28 or d0.day <= 2
        st = "GREEN"
        if spread >= c["sofr_spread_amber"] and not month_end: st = "AMBER"
        if max(spreads[:5]) >= c["sofr_spread_amber"]: st = worst(st, "AMBER")
        if spread >= c["sofr_spread_red"] or latest >= srf_ceiling: st = "RED"
        stays = spread >= c["sofr_spread_red"] and spreads[1] >= c["sofr_spread_red"]
        T.append(tile("r1", G, "repo", "Overnight", "SOFR against the Fed's rate",
                      "Is overnight cash costing more than the Fed pays banks to hold it?",
                      "Red at +25 basis points over the rate on reserves outside a month-end, or SOFR above the Fed's standing-repo ceiling on any day. A spike that does not come back down the next day is the real thing.",
                      st, f"SOFR {latest:.2f}% on {sofr[0][0]}, {spread * 100:+.0f} bp against the rate on reserves ({iorb[0][1]:.2f}%); ceiling {srf_ceiling:.2f}%. Worst spread in the last five sessions {max(spreads[:5]) * 100:+.0f} bp." + (" The spike has held for two sessions." if stays else ""),
                      "September 2019: 2% to 8% in a morning, the day after a tax date and a Treasury settlement drained cash together.",
                      "FRED: SOFR, IORB, DFEDTARU", value=round(spread * 100, 0)))
    except Exception as e:
        T.append(failed("r1", G, "repo", "Overnight", "SOFR against the Fed's rate", "Is overnight cash costing more than the Fed pays banks to hold it?", "", e))

    # --- r2 the Fed's repo window (hard + soft)
    try:
        rp = fred_recent("RPONTSYD", 15)  # Fed overnight repo lending (the standing facility), $bn, daily
        latest_bn = rp[0][1] / 1000 if rp[0][1] > 5000 else rp[0][1]  # series is in millions on FRED
        recent = [(v[1] / 1000 if v[1] > 5000 else v[1]) for v in rp[:10]]
        d0 = dt.date.fromisoformat(rp[0][0]); month_end = d0.day >= 28 or d0.day <= 2
        st = "GREEN"
        if latest_bn >= c["srf_amber_bn"] and not month_end: st = "AMBER"
        if max(recent[:5]) >= c["srf_amber_bn"]: st = worst(st, "AMBER")
        if latest_bn >= c["srf_red_bn"] or (recent[0] >= c["srf_amber_bn"] and recent[1] >= c["srf_amber_bn"] and not month_end): st = "RED"
        s1, e1, u1 = soft_line(soft, "srf_usage")
        T.append(tile("r2", G, "repo", "Overnight", "The Fed's repo window",
                      "Are banks borrowing from the Fed because private lenders won't lend?",
                      "Red at $50 billion or more, or any sustained use on ordinary days. Amber at $10 billion on a day that isn't month-end.",
                      worst(st, s1 if soft else "GREEN"), f"Fed overnight repo lending ${latest_bn:,.1f} billion on {rp[0][0]}; peak in the last ten sessions ${max(recent):,.1f} billion. {e1}".strip(),
                      "The window is a ceiling only if banks will use it; stigma has kept it under-used, which is why spikes get past it.",
                      "FRED: RPONTSYD; web check", value=round(latest_bn, 1), url=u1))
    except Exception as e:
        T.append(failed("r2", G, "repo", "Overnight", "The Fed's repo window", "Are banks borrowing from the Fed because private lenders won't lend?", "", e))

    # --- r3 the buffer (hard)
    try:
        rrp = fred_recent("RRPONTSYD", 10); res = fred_recent("WRESBAL", 30)
        rrp_bn = rrp[0][1] / 1000 if rrp[0][1] > 5000 else rrp[0][1]
        res_bn = res[0][1] / 1000 if res[0][1] > 50000 else res[0][1]
        res_13w = (res[0][1] - (fred_ago(res, 91) or res[-1][1])) / (1000 if res[0][1] > 50000 else 1)
        st = "RED" if rrp_bn < c["rrp_gone_bn"] else "AMBER" if rrp_bn < 500 else "GREEN"
        T.append(tile("r3", G, "repo", "Overnight", "The buffer",
                      "Is there spare cash left to absorb a shock?",
                      "This lamp is the standing condition, not the event. It stays red while the reverse-repo buffer is gone, until the Fed's balance sheet is growing with the economy on purpose or the repo window is open to everyone.",
                      st, f"Reverse-repo balance ${rrp_bn:,.0f} billion (peak $2,550 billion); bank reserves ${res_bn:,.0f} billion, {res_13w:+,.0f} billion over 13 weeks. Every dollar of Treasury settlement now comes straight out of reserves.",
                      "Nobody knows where scarcity begins; the Fed found it by accident in 2019.",
                      "FRED: RRPONTSYD, WRESBAL", value=round(rrp_bn, 0)))
    except Exception as e:
        T.append(failed("r3", G, "repo", "Overnight", "The buffer", "Is there spare cash left to absorb a shock?", "", e))

    # --- r4 the drain calendar (computed)
    try:
        dd = drain_dates(TODAY); t0 = dt.date.fromisoformat(TODAY)
        nxt = dd[0]; days = (nxt[0] - t0).days
        st = "AMBER" if days <= c["drain_window_days"] else "GREEN"
        upcoming = "; ".join(f"{d.strftime('%b %d')} ({k})" for d, k in dd[:4])
        T.append(tile("r4", G, "repo", "Overnight", "The drain calendar",
                      "Is a day coming when cash leaves the banks all at once?",
                      "Amber inside five business days of a quarter-end, month-end settlement, or tax date; the lamp is a calendar, not a reading. Red only in combination with the SOFR or window tiles.",
                      st, f"Next drain: {nxt[0].strftime('%A %b %d')} ({nxt[1]}), {days} days away. Upcoming: {upcoming}.",
                      "2019's spike and every quarter-end pop since late 2025 landed on these dates. Seizures do not happen on a random Tuesday.",
                      "Treasury settlement and tax calendars", value=days))
    except Exception as e:
        T.append(failed("r4", G, "repo", "Overnight", "The drain calendar", "", "", e))

    # --- r5 Treasury market function (MOVE + soft)
    try:
        mv = [c_ for _, c_ in yahoo("^MOVE", "1y")]; move = mv[-1]
        st = "RED" if move >= c["move_red"] else "AMBER" if move >= c["move_amber"] else "GREEN"
        s1, e1, u1 = soft_line(soft, "auction_results")
        T.append(tile("r5", G, "repo", "Overnight", "Treasury market function",
                      "Can the basis trade be unwound without breaking the market?",
                      "Red on a failed or badly tailed coupon auction, dealers refusing balance sheet, bond volatility (MOVE) above 140, or bid-ask spreads widening the way they did in March 2020. Amber with MOVE above 100.",
                      worst(st, s1 if soft else "GREEN"), f"MOVE index {move:.0f}. {e1}".strip(),
                      "March 2020: repo tightened, the basis trade could not roll, Treasuries were dumped, and the Fed bought $75 billion a day. Ten days start to finish.",
                      "Yahoo Finance ^MOVE; auction results via web check", value=round(move, 0), url=u1))
    except Exception as e:
        T.append(failed("r5", G, "repo", "Overnight", "Treasury market function", "", "", e))

    # --- r6 bank stress (discount window + soft)
    try:
        dw = fred_recent("WLCFLPCL", 10)  # primary credit outstanding, weekly, $mn
        dw_bn = dw[0][1] / 1000
        st = "RED" if dw_bn >= c["dw_red_bn"] else "AMBER" if dw_bn >= c["dw_amber_bn"] else "GREEN"
        s1, e1, u1 = soft_line(soft, "bank_stress")
        T.append(tile("r6", G, "repo", "Overnight", "Bank stress",
                      "Are the lenders worried about each other?",
                      "Red on a spike in a large bank's default-insurance cost or discount-window borrowing above $50 billion. Amber above $10 billion, or if uninsured deposits start moving to money funds again.",
                      worst(st, s1 if soft else "GREEN"), f"Discount-window primary credit ${dw_bn:,.1f} billion (week of {dw[0][0]}). {e1}".strip(),
                      "March 2023: $42 billion left one bank in a day through an app. The next run will be as fast.",
                      "FRED: WLCFLPCL; bank CDS via web check", value=round(dw_bn, 1), url=u1))
    except Exception as e:
        T.append(failed("r6", G, "repo", "Overnight", "Bank stress", "", "", e))

    # --- e1 cross-currency basis (soft)
    soft_tile("e1", "offshore", "Offshore", "The dollar shortage gauge",
              "Are foreigners paying extra to borrow dollars through swaps?",
              "Amber past -30 basis points outside a quarter-end; red at -60 and beyond, the dealers refusing to extend balance sheet. March 2020 reached -150 on the yen.",
              "xccy_basis", "When the swap market seizes, foreign holders do not default — they sell Treasuries. An offshore funding problem becomes a Treasury problem in the same week.",
              "Cross-currency basis via web check", default_status="GREEN", value_key="basis_bp")

    # --- e2 swap lines (hard)
    try:
        sw = fred_recent("SWPT", 10)  # central bank liquidity swaps, weekly, $mn
        sw_bn = sw[0][1] / 1000
        st = "RED" if sw_bn >= c["swap_red_bn"] else "AMBER" if sw_bn >= c["swap_amber_bn"] else "GREEN"
        T.append(tile("e2", G, "offshore", "Offshore", "The Fed's swap lines",
                      "Are foreign central banks drawing dollars from the Fed?",
                      "Red at $50 billion drawn; amber at $5 billion. Any non-trivial draw means the private swap market has stopped working somewhere.",
                      st, f"Central-bank liquidity swaps outstanding ${sw_bn:,.1f} billion (week of {sw[0][0]}). Peaks: $580 billion in 2008, $450 billion in 2020.",
                      "The swap lines have worked every time they were used, for the fourteen countries that have them.",
                      "FRED: SWPT", value=round(sw_bn, 1)))
    except Exception as e:
        T.append(failed("e2", G, "offshore", "Offshore", "The Fed's swap lines", "", "", e))

    # --- e3 Japan and the carry trade (yen + soft)
    try:
        jpy = [c_ for _, c_ in yahoo("JPY=X", "6mo")]
        wk = (jpy[-1] / jpy[-6] - 1) * 100  # + = yen weaker; a sharp negative = yen surging (unwind)
        st = "RED" if abs(wk) >= c["yen_week_red"] else "AMBER" if abs(wk) >= c["yen_week_amber"] else "GREEN"
        s1, e1, u1 = soft_line(soft, "boj_policy")
        T.append(tile("e3", G, "offshore", "Offshore", "Japan and the carry trade",
                      "Is the largest source of swapped dollars being pulled home?",
                      "Red on a yen move of more than 5% in a week against the dollar, or a surprise Bank of Japan hike. Amber at 3%, or while the hiking cycle runs.",
                      worst(st, s1 if soft else "GREEN"), f"Yen {jpy[-1]:.1f} per dollar, {wk:+.1f}% on the week (negative = yen surging). {e1}".strip(),
                      "August 2024: the yen jumped and U.S. stocks fell 8% in three days on no American news.",
                      "Yahoo Finance JPY=X; BoJ via web check", value=round(jpy[-1], 1), url=u1))
    except Exception as e:
        T.append(failed("e3", G, "offshore", "Offshore", "Japan and the carry trade", "", "", e))

    # --- new floors (soft)
    soft_tile("n1", "floors", "New floor", "The basis trade",
              "How much of the Treasury market is held on overnight money?",
              "Red when the trade unwinds — Treasury selling into a falling market with the futures basis blowing out. Amber standing while it exceeds $1 trillion.",
              "basis_trade", "Central clearing (December for cash trades, June 2027 for repo) will force margin on the trade, capping its leverage — safer after, riskier during the changeover.",
              "OFR; CFTC via web check", value_key="size_tn")
    soft_tile("n2", "floors", "New floor", "Central clearing changeover",
              "Will the plumbing survive its own repair?",
              "Red on a delay announced under pressure, a clearing-house margin call that cascades, or dealers dropping smaller counterparties in the run-up. Green once both deadlines have passed cleanly.",
              "clearing_transition", "The clearing house becomes the too-big-to-fail node: 2020 and the 2022 nickel blowup showed what a margin spiral looks like.",
              "SEC; FICC via web check")
    soft_tile("n3", "floors", "New floor", "Stablecoins",
              "How large is the new money-fund lookalike with no backstop?",
              "Red on a large issuer trading below par for more than a day, or total supply falling 10% in a week. Amber standing.",
              "stablecoins", "2022's Terra collapse and 2023's USDC break were the rehearsals, at a tenth of today's size.",
              "Issuer attestations via web check", value_key="supply_bn")
    soft_tile("n4", "floors", "New floor", "Private credit",
              "Has the newest shadow bank closed its doors?",
              "Standing red while large funds gate redemptions; green after a full quarter of redemptions met in full.",
              "private_credit", "About $2 trillion of bank lending done outside banks, funded by insurers and bank credit lines, with no window. The decision lives on Standing Orders.",
              "Fund filings via web check", default_status="RED")

    # --- t1 transmission (hard): Treasuries and stocks falling together with the dollar up
    try:
        y10 = fred_recent("DGS10", 15); spy = yahoo("SPY", "3mo"); dxy = fred_recent("DTWEXBGS", 15)
        ydates = {d: v for d, v in y10}; ddates = {d: v for d, v in dxy}
        spy_by = {d.isoformat(): c_ for d, c_ in spy}
        days = sorted(set(ydates) & set(spy_by) & set(ddates))[-8:]
        streak = 0
        for i in range(len(days) - 1, 0, -1):
            a, b = days[i], days[i - 1]
            if ydates[a] > ydates[b] and spy_by[a] < spy_by[b] and ddates[a] > ddates[b]:
                streak += 1
            else:
                break
        st = "RED" if streak >= 3 else "AMBER" if streak >= 1 else "GREEN"
        T.append(tile("q1", G, "sequence", "Stage 3", "Transmission",
                      "Has the seizure reached the markets — Treasuries and stocks falling together, the dollar jumping?",
                      "Red when 10-year Treasury prices and the S&P 500 fall together for three sessions with the dollar index up. Amber on the first such day.",
                      st, f"Current streak of sessions with yields up, stocks down and the dollar up: {streak}. 10-year {y10[0][1]:.2f}% on {y10[0][0]}.",
                      "In 2019 the spike never left the repo desks; the Fed lent within 48 hours. In 2020 it reached the Treasury market in ten days and every margin call in the world landed the same week.",
                      "FRED: DGS10, DTWEXBGS; Yahoo SPY", value=streak))
    except Exception as e:
        T.append(failed("q1", G, "sequence", "Stage 3", "Transmission", "", "", e))

    # --- t2 the Fed's response (hard + soft)
    try:
        r1 = next(t for t in T if t["id"] == "r1"); r2 = next(t for t in T if t["id"] == "r2")
        sofr = fred_recent("SOFR", 10); iorb = fred_recent("IORB", 10)
        spreads = [s[1] - next((i[1] for i in iorb if i[0] <= s[0]), iorb[-1][1]) for s in sofr[:5]]
        seized = sum(1 for x in spreads[:3] if x >= c["sofr_spread_red"]) >= 3
        ops = r2["status"] != "GREEN"
        st = "RED" if seized and not ops else "AMBER" if ops else "GREEN"
        s1, e1, u1 = soft_line(soft, "fed_operations")
        T.append(tile("q2", G, "sequence", "Stage 4", "The Fed's response",
                      "Once a seizure starts, how fast does the Fed lend — and does it stop when the episode ends?",
                      "Red if a seizure (SOFR 25 bp over the floor for three sessions) runs with no Fed operations. Amber while operations run; green when they end. Purchases that continue after the episode normalizes are step two on the fiscal dominance board.",
                      worst(st, s1 if soft else "GREEN"), ("A seizure is under way with no Fed operations." if seized and not ops else "Fed operations under way." if ops else "Nothing to respond to.") + f" {e1}".rstrip(),
                      "The precedents are days: 48 hours in 2019, ten days in 2020, a weekend in 2023. The Fed has never failed to act on a repo seizure since 1987; the only way this goes red is if it is stopped from acting.",
                      "FRED: SOFR, IORB, RPONTSYD; web check", value=int(seized), url=u1))
    except Exception as e:
        T.append(failed("q2", G, "sequence", "Stage 4", "The Fed's response", "", "", e))
    return T


def plumbing_gate(tiles):
    by = lambda id: next((t for t in tiles if t["id"] == id), None)
    st = lambda id: (by(id) or {}).get("status", "GREEN")
    buffer = st("r3") != "GREEN"; drain = st("r4") != "GREEN"
    spike = st("r1") == "RED"
    stays = spike and (st("r2") == "RED" or st("r5") == "RED" or "held for two sessions" in (by("r1") or {}).get("reading", ""))
    transmission = st("q1") == "RED"; fed_slow = st("q2") == "RED"
    stage = 4 if fed_slow else 3 if transmission else 2 if stays else 1 if spike else 0
    titles = ["Stage 0 of 4 — " + ("every ingredient, no event: the buffer is gone and a drain day is near" if buffer and drain else "the buffer is gone; no drain day near, no event" if buffer else "buffer present; calm"),
              "Stage 1 of 4 — a spike: overnight rates have jumped",
              "Stage 2 of 4 — the spike has stayed: a seizure is under way; the Fed's response decides the rest",
              "Stage 3 of 4 — transmission: Treasuries and stocks falling together; the meltdown path",
              "Stage 4 of 4 — the Fed has not acted: the confidence-break path"]
    parts = [{"name": "Ingredient: buffer gone", "status": "RED" if buffer else "GREEN"},
             {"name": "Ingredient: drain day near", "status": "AMBER" if drain else "GREEN"},
             {"name": "Stage 1: spike", "status": "RED" if spike else "GREEN"},
             {"name": "Stage 2: spike stays — the bell", "status": "RED" if stays else "GREEN"},
             {"name": "Stage 3: transmission to markets", "status": "RED" if transmission else st("q1")},
             {"name": "Stage 4: Fed fails to act", "status": "RED" if fed_slow else st("q2")}]
    return {"kind": "stages", "rung": stage >= 2, "title": titles[stage],
            "text": "A seizure needs two ingredients in place — no buffer, and a drain day — and then runs through four stages: (1) a spike in overnight rates; (2) the spike stays, which rings the bell; (3) transmission into Treasuries and stocks, the meltdown path; (4) the Fed fails to act, the confidence-break path. Stage decides whether it is a plumbing story (2019 stopped at stage 2) or a market one (March 2020 reached stage 3 for ten days).",
            "parts": parts}


def fiscal_board(soft, plumbing_gate_obj):
    T = []
    G = "fiscal"
    c = CONFIG

    def soft_tile(id, group, tag, name, question, trip, key, evidence_note, source, default_status="AMBER", value_key=None):
        st, ev, url = soft_line(soft, key)
        d = soft.get(key, {}) if soft else {}
        val = d.get(value_key) if value_key else None
        reading = ev if soft else "Not checked this run (no Claude key)."
        T.append(tile(id, G, group, tag, name, question, trip, st if soft else default_status, reading, evidence_note, source,
                      value=float(val) if isinstance(val, (int, float)) else None, url=url))

    # --- s1 reserve purchases outgrow reserves (hard + soft)
    try:
        assets = fred_recent("WALCL", 30); res = fred_recent("WRESBAL", 30)
        a_13 = (assets[0][1] - (fred_ago(assets, 91) or assets[-1][1])) / 1000
        r_13 = (res[0][1] - (fred_ago(res, 91) or res[-1][1])) / 1000
        st = "GREEN"
        if a_13 > 25: st = "AMBER"
        if a_13 > r_13 + 100 and a_13 > 100: st = "RED"
        s1, e1, u1 = soft_line(soft, "fed_purchases_scope")
        T.append(tile("s1", G, "step", "Step 1", "Reserve purchases outgrow reserves",
                      "Is the Fed buying more than the plumbing needs?",
                      "Red when the balance sheet grows more than $100 billion beyond the growth in bank reserves over 13 weeks, or when the Fed buys coupons rather than bills. Amber while bill purchases run.",
                      worst(st, s1 if soft else "GREEN"), f"Fed total assets ${assets[0][1] / 1000:,.0f} billion, {a_13:+,.0f} billion over 13 weeks; bank reserves {r_13:+,.0f} billion over the same period. {e1}".strip(),
                      "Compare the Fed's total assets with bank reserve balances, both weekly on the H.4.1. Buying that outruns reserves is buying for the Treasury.",
                      "FRED: WALCL, WRESBAL; web check", value=round(a_13, 0), url=u1))
    except Exception as e:
        T.append(failed("s1", G, "step", "Step 1", "Reserve purchases outgrow reserves", "", "", e))

    soft_tile("s2", "step", "Step 2", "A crisis purchase that outlasts the crisis",
              "Has the Fed bought bonds under a financial-stability banner and kept buying after the episode ended?",
              "Red if purchases begun in an episode continue after the episode's own indicators (repo rates, auction results) have normalized.",
              "crisis_purchase_persisting", "Every crisis purchase since 1987 was justified as temporary; 2008's became permanent. The tell is the second month.",
              "Fed statements; H.4.1 via web check", default_status="GREEN")
    soft_tile("s3", "step", "Step 3", "Political pressure to hold yields down",
              "Is the executive branch or the Treasury talking about long yields as something the Fed should manage?",
              "Red when the Treasury Secretary or the White House names the 10-year as a target in public, or when the Fed's independence is challenged in law.",
              "political_pressure", "The 1951 Accord that freed the Fed from the Treasury followed exactly this pressure, run the other way.",
              "Public statements via web check")
    soft_tile("s4", "step", "Step 4", "A stated ceiling",
              "Has the Fed announced a cap on long yields?",
              "Red the day it is said. Amber if yield-curve control enters Fed speeches as an option.",
              "yield_ceiling_talk", "Japan ran a ceiling from 2016; it held only while inflation stayed near zero and domestic savers had nowhere to go. The U.S. ran one from 1942 to 1951.",
              "Fed communications via web check", default_status="GREEN")

    # --- v1 expected inflation (hard)
    try:
        be5 = fred_recent("T5YIFR", 10); be10 = fred_recent("T10YIE", 10)
        v = be5[0][1]
        st = "RED" if v >= c["breakeven_red"] else "AMBER" if v >= c["breakeven_amber"] else "GREEN"
        T.append(tile("v1", G, "verdict", "Verdict", "Expected inflation",
                      "Do bond investors expect inflation to stay near 2%?",
                      "Red when 5-year-forward breakevens pass 3%; amber at 2.6%. That is the market naming the policy before the Fed does.",
                      st, f"5-year, 5-year-forward breakeven {v:.2f}%; 10-year breakeven {be10[0][1]:.2f}% ({be5[0][0]}).",
                      "Breakevens are the cleanest reading of whether the anchor holds; they stayed anchored through 2021 until they didn't.",
                      "FRED: T5YIFR, T10YIE", value=round(v, 2)))
    except Exception as e:
        T.append(failed("v1", G, "verdict", "Verdict", "Expected inflation", "", "", e))

    # --- v2 gold against real yields (hard)
    try:
        gold = [c_ for _, c_ in yahoo("GC=F", "1y")]; g = gold[-1]; g_hi = max(gold)
        real30 = fred_recent("DFII30", 90); r = real30[0][1]; r_3m = fred_ago(real30, 91) or r
        at_high = g >= 0.97 * g_hi
        st = "GREEN"
        if at_high and r >= c["real30_amber"]: st = "AMBER"
        if g >= g_hi and r > r_3m: st = "RED"
        T.append(tile("v2", G, "verdict", "Verdict", "Gold against real yields",
                      "Is the world buying the one asset that isn't a promise, even while promises pay well?",
                      "Amber when gold is within 3% of its 52-week high while 30-year real yields sit above 2%. Red when gold makes a new high while real yields rise — the referendum going against the pyramid with the odds still in its favor.",
                      st, f"Gold ${g:,.0f} (52-week high ${g_hi:,.0f}); 30-year TIPS real yield {r:.2f}% ({r - r_3m:+.2f} over three months).",
                      "Gold has outrun TIPS since 2022 while breakevens stayed flat. It is hedging the regime, not the index.",
                      "Yahoo GC=F; FRED DFII30", value=round(g, 0)))
    except Exception as e:
        T.append(failed("v2", G, "verdict", "Verdict", "Gold against real yields", "", "", e))

    soft_tile("v3", "verdict", "Verdict", "Foreign demand",
              "Are foreign official holders still showing up for Treasuries?",
              "Red on a badly tailed coupon auction attributed to foreign absence or a sharp fall in the indirect-bidder share; amber on a falling trend, central-bank gold buying, yuan oil settlement.",
              "foreign_demand", "When foreign demand fades, the Fed becomes the marginal buyer whether it wants to or not. That is step one becoming step two by default.",
              "TIC; auction results via web check", value_key="indirect_pct")

    # --- v4 the long end (hard)
    try:
        y30 = fred_recent("DGS30", 90); be10 = fred_recent("T10YIE", 90)
        v = y30[0][1]; be_chg = be10[0][1] - (fred_ago(be10, 91) or be10[-1][1])
        st = "RED" if (v >= c["long_end_red"] and be_chg < 0.2) else "AMBER" if v >= c["long_end_amber"] else "GREEN"
        T.append(tile("v4", G, "verdict", "Verdict", "The long end",
                      "Is the market charging more to hold the government's longest promises?",
                      "Red when the 30-year holds above 5.5% with breakevens unchanged: the premium blowing out on supply alone. Amber above 5%.",
                      st, f"30-year {v:.2f}% on {y30[0][0]}; 10-year breakeven {be_chg:+.2f} over three months — {'a supply premium, not an inflation premium' if be_chg < 0.2 else 'inflation expectations moving too'}.",
                      "A long end rising while expected inflation is flat is the market's own yield-curve control, run against the Treasury.",
                      "FRED: DGS30, T10YIE", value=round(v, 2)))
    except Exception as e:
        T.append(failed("v4", G, "verdict", "Verdict", "The long end", "", "", e))

    # --- v5 the dollar itself (hard)
    try:
        dxy = fred_recent("DTWEXBGS", 40); y10 = fred_recent("DGS10", 40)
        d_m = (dxy[0][1] / (fred_ago(dxy, 30) or dxy[-1][1]) - 1) * 100
        y_m = y10[0][1] - (fred_ago(y10, 30) or y10[-1][1])
        st = "GREEN"
        if d_m <= c["dollar_month_amber"] and y_m > 0: st = "AMBER"
        if d_m <= c["dollar_month_red"] and y_m >= 0.25: st = "RED"
        T.append(tile("v5", G, "verdict", "Verdict", "The dollar itself",
                      "Is money leaving the dollar, or just leaving bonds?",
                      "Red when the dollar falls more than 5% in a month while long yields rise a quarter point or more — capital leaving the system rather than repricing within it. Amber at 3%.",
                      st, f"Broad dollar index {dxy[0][1]:.1f}, {d_m:+.1f}% over a month; 10-year yield {y_m:+.2f} over the same period.",
                      "In 2008 and 2020 the dollar rose in the crisis. The day it falls in one is the day the fourth pillar is being tested. April 2025 was the preview.",
                      "FRED: DTWEXBGS, DGS10", value=round(dxy[0][1], 1)))
    except Exception as e:
        T.append(failed("v5", G, "verdict", "Verdict", "The dollar itself", "", "", e))

    # --- p1 the arithmetic (hard)
    try:
        gdp = fred("GDP", limit=12)  # nominal, quarterly, $bn SAAR, newest first
        ngdp_yoy = (gdp[0][1] / gdp[4][1] - 1) * 100
        y10 = fred_recent("DGS10", 5)[0][1]
        interest = fred("A091RC1Q027SBEA", limit=4)[0][1]  # federal interest payments, $bn SAAR
        st = "RED" if y10 >= ngdp_yoy else "AMBER" if y10 >= ngdp_yoy - 1.0 else "GREEN"
        T.append(tile("p1", G, "pressure", "Pressure", "The arithmetic",
                      "Does the debt cost more than the economy grows?",
                      "Red when the 10-year yield exceeds nominal GDP growth; amber within a point of it. Interest costs are shown for scale.",
                      st, f"Nominal GDP growth {ngdp_yoy:.1f}% over the year to {gdp[0][0]}; 10-year yield {y10:.2f}%; federal interest running at ${interest:,.0f} billion a year.",
                      "Every government at this point on the curve has reached for the bond desk. The arithmetic, not the politics, is what forces the steps.",
                      "FRED: GDP, DGS10, A091RC1Q027SBEA", value=round(y10 - ngdp_yoy, 2)))
    except Exception as e:
        T.append(failed("p1", G, "pressure", "Pressure", "The arithmetic", "", "", e))

    # --- p2 the plumbing door (mirror of the plumbing gate)
    stage_title = plumbing_gate_obj.get("title", "")
    rung = plumbing_gate_obj.get("rung", False)
    buffer_gone = any(p.get("name", "").startswith("Ingredient: buffer") and p.get("status") == "RED" for p in plumbing_gate_obj.get("parts", []))
    st = "RED" if rung else "AMBER" if buffer_gone else "GREEN"
    T.append(tile("p2", G, "pressure", "Pressure", "The plumbing door",
                  "Is a repo or Treasury-market seizure near enough to give the Fed the financial-stability reason to buy?",
                  "Mirrors the plumbing board: amber while the buffer is gone, red once a seizure is under way. Step two enters through this door.",
                  st, stage_title, "The 2019 spike and the March 2020 seizure were both answered with purchases.",
                  "Plumbing board", value=int(rung)))

    soft_tile("p3", "pressure", "Pressure", "The Fed's own position",
              "Is the Fed politically weaker than it needs to be to keep saying no?",
              "Red when the Fed's losses or independence become a legislative target, or a dissent for cuts appears from a governor appointed for that purpose.",
              "fed_position", "An institution that has to defend its own books is easier to pressure than one that doesn't. The deferred asset is about $230 billion.",
              "Fed financials; Congress via web check", value_key="deferred_asset_bn")
    return T


def fiscal_gate(tiles):
    by = lambda id: next((t for t in tiles if t["id"] == id), None)
    st = lambda id: (by(id) or {}).get("status", "GREEN")
    steps = [("s1", "Reserve purchases outgrow reserves"), ("s2", "A crisis purchase that outlasts the crisis"),
             ("s3", "Political pressure to hold yields down"), ("s4", "A stated ceiling")]
    taken = 0
    for i, (id, _) in enumerate(steps):
        if st(id) == "RED": taken = i + 1
    on_one = st("s1") != "GREEN"
    pressing = 0
    for i, (id, _) in enumerate(steps[1:], start=2):
        if st(id) != "GREEN": pressing = i
    words = {2: "two", 3: "three", 4: "four"}
    if taken >= 2:
        title = f"The Fed has taken step {words[taken]} of four" + (" — a stated ceiling" if taken == 4 else "")
    elif on_one:
        title = "The Fed is on step one of four" + (f" — pressure building toward step {words[pressing]}" if pressing else "")
    else:
        title = "The Fed has taken none of the four steps"
    return {"kind": "steps", "rung": taken >= 2, "title": title,
            "text": "The path from central bank to Treasury financier has four steps, each defensible on its own: bill purchases for reserve management; a crisis purchase that outlasts the crisis; political pressure to hold yields down; a stated ceiling. Step one is normal. Step two is the slope. Step three is the pressure. Step four is 1942.",
            "parts": [{"name": name, "status": st(id)} for id, name in steps]}


PLUMBING_GROUPS = [{"key": "repo", "title": "The overnight market", "blurb": "The domestic repo market: about $3 trillion a day, re-agreed every morning. Watch the days, not the level."},
                   {"key": "offshore", "title": "The offshore dollar", "blurb": "The eurodollar system: the same trade done abroad with a currency as the pawn, through the same dealers."},
                   {"key": "floors", "title": "The new floors", "blurb": "The layers stacked on top since 2020, none of them backstopped."},
                   {"key": "sequence", "title": "From episode to meltdown", "blurb": "What decides whether a seizure stays a plumbing story or becomes a market one: how far it travels before the Fed acts."}]
FISCAL_GROUPS = [{"key": "step", "title": "The four steps", "blurb": "The gate counts these. Watch is a step being approached; red is a step taken."},
                 {"key": "verdict", "title": "The market's verdict", "blurb": "What the people holding dollar promises are doing about it, ahead of any announcement."},
                 {"key": "pressure", "title": "The pressure on the Fed", "blurb": "The arithmetic and the plumbing that push toward the next step."}]


# ----------------------------------------------------------------------------- assembly, history, render, alerts
def gates(ai_tiles, mk_tiles):
    by = lambda tiles, id: next((t for t in tiles if t["id"] == id), None)
    t3, t4 = by(ai_tiles, "t3"), by(ai_tiles, "t4")
    rung = bool(t3 and t4 and t3["status"] != "GREEN" and t4["status"] != "GREEN")
    ai_gate = {"kind": "bell", "rung": rung, "title": "The bell has rung" if rung else "The bell has not rung",
               "text": "It rings when two things are true at once: the big buyers' spending growth is slowing, and computing time is getting cheaper to rent. Together they have led the crash by one to two quarters.",
               "parts": [{"name": t["name"], "status": t["status"]} for t in (t3, t4) if t]}
    conds = [{"name": "Earnings stop growing (earnings tile)", "status": (by(mk_tiles, "m3") or {}).get("status", "GREEN")},
             {"name": "Borrowing costs jump (rates tile)", "status": (by(mk_tiles, "m1") or {}).get("status", "GREEN")},
             {"name": "Outside shock (outside-shock tile)", "status": (by(mk_tiles, "m6") or {}).get("status", "GREEN")}]
    present = sum(c["status"] == "RED" for c in conds); watching = sum(c["status"] == "AMBER" for c in conds)
    title = (f"{present} of the three bear-market conditions are present" if present >= 2 else
             f"One of the three bear-market conditions is present" + (f", {watching} on watch" if watching else "") if present == 1 else
             f"No condition present; {watching} on watch" if watching else "No bear-market condition present")
    mk_gate = {"kind": "gate", "rung": present >= 2, "title": title,
               "text": "A 20% decline has always needed one of three things: earnings stop growing, borrowing costs jump, or an outside shock does one of the first two. Any one can start a bear market. Two at once reliably finish one. Only the three trigger tiles count here.",
               "parts": conds}
    return ai_gate, mk_gate


def taiwan_gate(tw_tiles):
    by = lambda id: next((t for t in tw_tiles if t["id"] == id), None)
    st = lambda id: (by(id) or {}).get("status", "GREEN")
    reason = worst(st("k1"), st("k2"))
    opening_tiles = [st(i) for i in ("k4", "k5", "k6", "k7", "k8", "k9")]
    reds, nongreen = opening_tiles.count("RED"), sum(1 for x in opening_tiles if x != "GREEN")
    opening = "RED" if reds >= 3 else "AMBER" if nongreen >= 3 else "GREEN"
    decision = worst(st("k10"), st("k11"))
    conds = [{"name": "A reason (red line crossed, or Beijing declares the peaceful path dead)", "status": reason},
             {"name": "An opening (U.S. unwilling or tied down, PLA ready, China stocked, Taiwan weak)", "status": opening},
             {"name": "A decision (inspections, mobilization)", "status": decision}]
    present = sum(c["status"] == "RED" for c in conds); watching = sum(c["status"] == "AMBER" for c in conds)
    if decision == "RED":
        title = "A decision is showing — inspections or mobilization under way"
    elif present >= 2:
        title = f"{present} of the three invasion conditions are present"
    elif present == 1:
        title = "One of the three invasion conditions is present" + (f", {watching} on watch" if watching else "")
    else:
        title = f"No invasion condition present; {watching} on watch" if watching else "No invasion condition present"
    return {"kind": "gate", "rung": present >= 2 or decision == "RED", "title": title,
            "text": "A blockade or invasion needs three things: a reason (a red line crossed, or Beijing declaring the peaceful path dead), an opening (the U.S. unwilling or tied down, the PLA ready, China's stockpiles full, Taiwan's clock short), and a decision (the mobilization that can't be hidden). Reason plus opening is where the risk lives; a decision means it has started. Only the reason, opening, and decision tiles count here; the markets tile confirms.",
            "parts": conds}


def summaries(ai_tiles, mk_tiles, ai_gate, mk_gate):
    def cnt(tiles):
        return sum(t["status"] == "RED" for t in tiles), sum(t["status"] == "AMBER" for t in tiles)
    r1, a1 = cnt(ai_tiles); r2, a2 = cnt(mk_tiles)
    return (f"{r1} tripped, {a1} on watch, of {len(ai_tiles)}. {ai_gate['title']}.",
            f"{r2} tripped, {a2} on watch, of {len(mk_tiles)}. {mk_gate['title']}.")


def update_history(tiles):
    hist = json.load(open(HISTORY_FILE)) if os.path.exists(HISTORY_FILE) else {}
    for t in tiles:
        h = hist.setdefault(t["id"], [])
        if h and h[-1]["date"] == TODAY:
            h[-1] = {"date": TODAY, "status": t["status"], "value": t["value"]}
        else:
            h.append({"date": TODAY, "status": t["status"], "value": t["value"]})
        del h[:-CONFIG["history_cap"]]
        t["history"] = h
    json.dump(hist, open(HISTORY_FILE, "w"), indent=1)



def build_changelog(boards, prev_state, prev_values, prev_gates):
    """One entry per run: status changes, gate title changes, and notable value moves. Returns the entry and the updated log."""
    log = json.load(open(CHANGELOG_FILE)) if os.path.exists(CHANGELOG_FILE) else []
    items = []
    for b in boards:
        g = b["gate"]; old_title = prev_gates.get(b["key"])
        if old_title and old_title != g["title"]:
            items.append({"board": b["key"], "label": b.get("label", b["title"]), "kind": "gate", "name": "Gate", "old": old_title, "new": g["title"], "reading": ""})
        for t in b["tiles"]:
            old = prev_state.get(t["id"])
            if old is not None and old != t["status"]:
                items.append({"board": b["key"], "label": b.get("label", b["title"]), "kind": "status", "name": t["name"], "old": old, "new": t["status"], "reading": t["reading"][:220]})
                continue
            ov, nv = prev_values.get(t["id"]), t.get("value")
            if t["id"] in CONFIG["value_log_skip"]:
                continue
            if isinstance(ov, (int, float)) and isinstance(nv, (int, float)) and ov != 0 and abs(nv - ov) / abs(ov) * 100 >= CONFIG["value_move_pct"]:
                items.append({"board": b["key"], "label": b.get("label", b["title"]), "kind": "value", "name": t["name"], "old": ov, "new": nv, "reading": t["reading"][:220]})
    first_run = not prev_state
    entry = {"date": TODAY, "first_run": first_run, "items": items,
             "summary": ("First run; nothing to compare." if first_run else
                         f"{sum(i['kind'] == 'status' for i in items)} status change(s), {sum(i['kind'] == 'gate' for i in items)} gate change(s), {sum(i['kind'] == 'value' for i in items)} notable move(s)." if items else "Nothing moved.")}
    if log and log[-1]["date"] == TODAY:
        log[-1] = entry
    else:
        log.append(entry)
    del log[:-CONFIG["changelog_days"]]
    json.dump(log, open(CHANGELOG_FILE, "w"), indent=1, ensure_ascii=False)
    return entry, log


def render(data):
    os.makedirs(DOCS, exist_ok=True)
    tpl = open(TEMPLATE, encoding="utf-8").read()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("/*TRIPWIRE_DATA*/{}", payload)
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(html)
    json.dump(data, open(os.path.join(DOCS, "data.json"), "w"), indent=1, ensure_ascii=False)


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com"); port = int(os.environ.get("SMTP_PORT", "465"))
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"); to = os.environ.get("ALERT_EMAIL", user)
    if not (user and pw and to):
        return False
    msg = MIMEText(body); msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
        s.login(user, pw); s.sendmail(user, [to], msg.as_string())
    return True


def send_pushover(title, body):
    token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if not (token and user):
        return False
    data = urllib.parse.urlencode({"token": token, "user": user, "title": title, "message": body[:1024]}).encode()
    urllib.request.urlopen(urllib.request.Request("https://api.pushover.net/1/messages.json", data=data), timeout=30)
    return True


def alert(subject, body):
    sent = []
    for fn, name in ((send_email, "email"), (send_pushover, "pushover")):
        try:
            if fn(subject, body):
                sent.append(name)
        except Exception as ex:
            print(f"{name} failed: {ex}")
    return sent


def run(digest=False):
    soft, soft_err = soft_signals()
    if soft_err:
        print("soft signals:", soft_err)
    soft_tw, tw_err = soft_signals(TAIWAN_PROMPT, TAIWAN_KEYS)
    if tw_err:
        print("taiwan signals:", tw_err)
    soft_pl, pl_err = soft_signals(PLUMBING_PROMPT, PLUMBING_KEYS)
    if pl_err:
        print("plumbing signals:", pl_err)
    soft_fi, fi_err = soft_signals(FISCAL_PROMPT, FISCAL_KEYS)
    if fi_err:
        print("fiscal signals:", fi_err)
    ai = ai_board(soft)
    mk = market_board(soft, ai)
    tw = taiwan_board(soft_tw)
    pl = plumbing_board(soft_pl)
    pl_gate = plumbing_gate(pl)
    fi = fiscal_board(soft_fi, pl_gate)
    fi_gate = fiscal_gate(fi)
    ai_gate, mk_gate = gates(ai, mk)
    tw_gate = taiwan_gate(tw)
    prev_hist = json.load(open(HISTORY_FILE)) if os.path.exists(HISTORY_FILE) else {}
    prev_values = {k: (v[-1].get("value") if v and v[-1].get("date") != TODAY else (v[-2].get("value") if len(v) > 1 else None)) for k, v in prev_hist.items()}
    prev_state_early = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    prev_gates = {}
    try:
        old_data = json.load(open(os.path.join(DOCS, "data.json")))
        prev_gates = {b["key"]: b["gate"]["title"] for b in old_data.get("boards", [])}
    except Exception:
        pass
    update_history(ai + mk + tw + pl + fi)
    s1, s2 = summaries(ai, mk, ai_gate, mk_gate)
    s3 = f"{sum(t['status'] == 'RED' for t in tw)} tripped, {sum(t['status'] == 'AMBER' for t in tw)} on watch, of {len(tw)}. {tw_gate['title']}."
    s4 = f"{sum(t['status'] == 'RED' for t in pl)} tripped, {sum(t['status'] == 'AMBER' for t in pl)} on watch, of {len(pl)}. {pl_gate['title']}."
    s5 = f"{sum(t['status'] == 'RED' for t in fi)} tripped, {sum(t['status'] == 'AMBER' for t in fi)} on watch, of {len(fi)}. {fi_gate['title']}."
    data = {"updated": TODAY, "soft_checked": soft is not None, "site_url": os.environ.get("SITE_URL", ""),
            "boards": [
                {"key": "plumbing", "label": "Plumbing", "title": "Plumbing tripwires", "summary": s4, "gate": pl_gate, "groups": PLUMBING_GROUPS, "tiles": pl},
                {"key": "market", "label": "Market", "title": "US market tripwires", "summary": s2, "gate": mk_gate,
                 "groups": [{"key": "trigger", "title": "What starts a bear market", "blurb": "The three conditions the gate counts."},
                            {"key": "amplifier", "title": "What decides how far it falls", "blurb": "Not triggers — they set the size of the drop."},
                            {"key": "driver", "title": "What could flip a trigger", "blurb": "Early warning for the top section."}],
                 "tiles": mk},
                {"key": "ai", "label": "AI cycle", "title": "AI cycle tripwires", "summary": s1, "gate": ai_gate,
                 "groups": [{"key": "trigger", "title": "What comes first", "blurb": "The two signals that showed up a year before the 2000 crash."},
                            {"key": "bell", "title": "What rings the bell", "blurb": "The two that together have led the crash by one to two quarters."},
                            {"key": "pressure", "title": "What adds pressure", "blurb": "Rates, credit, price action, oil."}],
                 "tiles": ai},
                {"key": "fiscal", "label": "Fiscal", "title": "Fiscal dominance tripwires", "summary": s5, "gate": fi_gate, "groups": FISCAL_GROUPS, "tiles": fi},
                {"key": "taiwan", "label": "Taiwan", "title": "Taiwan tripwires", "summary": s3, "gate": tw_gate,
                 "groups": [{"key": "reason", "title": "What would force Xi's hand", "blurb": "A red line crossed, or Beijing deciding the peaceful path is dead."},
                            {"key": "opening", "title": "Whether the window is open", "blurb": "Capability and opportunity. Three or more of these off green means the door is open."},
                            {"key": "decision", "title": "Whether a decision has been made", "blurb": "The signs that can't be hidden once a move is under way."}],
                 "tiles": tw}]}
    entry, log = build_changelog(data["boards"], prev_state_early, prev_values, prev_gates)
    data["changelog"] = log[::-1]   # newest first
    for b in data["boards"]:
        b["since_last_run"] = [i for i in entry["items"] if i["board"] == b["key"]]
    render(data)

    prev = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    changes = []
    for t in ai + mk + tw + pl + fi:
        old = prev.get(t["id"])
        if old != t["status"] and t["status"] in CONFIG["alert_on"]:
            changes.append(f"{t['name']}: {old or 'new'} -> {t['status']}\n   {t['reading']}")
    json.dump({t["id"]: t["status"] for t in ai + mk + tw + pl + fi}, open(STATE_FILE, "w"), indent=1)

    table = "\n".join(f"[{t['status']:5}] {t['name']}: {t['reading']}" for t in ai + mk + tw + pl + fi)
    print(TODAY, "\n" + s1 + "\n" + s2 + "\n" + s3 + "\n" + s4 + "\n" + s5 + "\n" + table)
    link = f"\n\n{data['site_url']}" if data["site_url"] else ""
    if changes:
        subject = f"Tripwires {'RED' if any('-> RED' in c for c in changes) else 'AMBER'}: {len(changes)} change(s)"
        print("alert sent via:", alert(subject, "CHANGES\n" + "\n".join(changes) + "\n\n" + s1 + "\n" + s2 + "\n" + s3 + "\n" + s4 + "\n" + s5 + link))
    elif digest:
        print("digest sent via:", alert(f"Tripwires digest — {TODAY}", s1 + "\n" + s2 + "\n" + s3 + "\n" + s4 + "\n" + s5 + "\n\n" + table + link))


if __name__ == "__main__":
    load_env()
    if "--test" in sys.argv:
        print("test alert via:", alert("Tripwires: test", "If you can read this, alerts work."))
    elif "--render" in sys.argv:
        render(json.load(open(os.path.join(DOCS, "data.json"))))
        print("re-rendered docs/index.html")
    else:
        run(digest="--digest" in sys.argv)
