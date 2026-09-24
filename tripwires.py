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
    "breadth_amber": 50.0,                       # % of S&P 500 below 200-day (from the soft check when available)
    "cpi_amber": 3.5, "cpi_red": 4.0,
    "soft_model": "claude-sonnet-4-6",
    "alert_on": ["RED", "AMBER"],
    "history_cap": 120,
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


def soft_signals():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, "ANTHROPIC_API_KEY not set — soft signals not checked"
    body = {"model": CONFIG["soft_model"], "max_tokens": 6000,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 25}],
            "messages": [{"role": "user", "content": SOFT_PROMPT.format(today=TODAY)}]}
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
    for k in SOFT_KEYS:
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
        above = b.get("pct_above_200dma")
        below = 100 - float(above) if isinstance(above, (int, float)) else None
        st = "RED" if streak >= CONFIG["ma_break_days"] else "GREEN"
        if below is not None and below >= CONFIG["breadth_amber"]:
            st = worst(st, "AMBER")
        if rel <= -0.05:
            st = worst(st, "AMBER")
        T.append(tile("m4", G, "amplifier", "Amplifier", "The average stock is already falling", "Is the index rising on a few giant companies while most stocks fall?",
                      "Watch at half the stocks below their 200-day average; trips when the index itself breaks its 200-day average.", st,
                      f"S&P 500 {spx[-1]:,.0f} vs 200-day {ma:,.0f} ({streak} closes below). Equal-weight vs cap-weight over 60 sessions: {rel:+.1%}. "
                      + (f"{below:.0f}% of members below their 200-day. " if below is not None else "") + b.get("evidence", ""),
                      "Narrow leadership is how the last two bear markets began.", "Yahoo Finance; breadth data via web check", value=below if below is not None else round(rel * 100, 1)))
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
    ai = ai_board(soft)
    mk = market_board(soft, ai)
    ai_gate, mk_gate = gates(ai, mk)
    update_history(ai + mk)
    s1, s2 = summaries(ai, mk, ai_gate, mk_gate)
    data = {"updated": TODAY, "soft_checked": soft is not None, "site_url": os.environ.get("SITE_URL", ""),
            "boards": [
                {"key": "ai", "title": "AI cycle tripwires", "summary": s1, "gate": ai_gate,
                 "groups": [{"key": "trigger", "title": "What comes first", "blurb": "The two signals that showed up a year before the 2000 crash."},
                            {"key": "bell", "title": "What rings the bell", "blurb": "The two that together have led the crash by one to two quarters."},
                            {"key": "pressure", "title": "What adds pressure", "blurb": "Rates, credit, price action, oil."}],
                 "tiles": ai},
                {"key": "market", "title": "US market tripwires", "summary": s2, "gate": mk_gate,
                 "groups": [{"key": "trigger", "title": "What starts a bear market", "blurb": "The three conditions the gate counts."},
                            {"key": "amplifier", "title": "What decides how far it falls", "blurb": "Not triggers — they set the size of the drop."},
                            {"key": "driver", "title": "What could flip a trigger", "blurb": "Early warning for the top section."}],
                 "tiles": mk}]}
    render(data)

    prev = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    changes = []
    for t in ai + mk:
        old = prev.get(t["id"])
        if old != t["status"] and t["status"] in CONFIG["alert_on"]:
            changes.append(f"{t['name']}: {old or 'new'} -> {t['status']}\n   {t['reading']}")
    json.dump({t["id"]: t["status"] for t in ai + mk}, open(STATE_FILE, "w"), indent=1)

    table = "\n".join(f"[{t['status']:5}] {t['name']}: {t['reading']}" for t in ai + mk)
    print(TODAY, "\n" + s1 + "\n" + s2 + "\n" + table)
    link = f"\n\n{data['site_url']}" if data["site_url"] else ""
    if changes:
        subject = f"Tripwires {'RED' if any('-> RED' in c for c in changes) else 'AMBER'}: {len(changes)} change(s)"
        print("alert sent via:", alert(subject, "CHANGES\n" + "\n".join(changes) + "\n\n" + s1 + "\n" + s2 + link))
    elif digest:
        print("digest sent via:", alert(f"Tripwires digest — {TODAY}", s1 + "\n" + s2 + "\n\n" + table + link))


if __name__ == "__main__":
    load_env()
    if "--test" in sys.argv:
        print("test alert via:", alert("Tripwires: test", "If you can read this, alerts work."))
    elif "--render" in sys.argv:
        render(json.load(open(os.path.join(DOCS, "data.json"))))
        print("re-rendered docs/index.html")
    else:
        run(digest="--digest" in sys.argv)
