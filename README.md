# Tripwires — the self-updating app

One script fetches every source, scores both boards, rebuilds the app page, and alerts you
when a tile changes color. No Claude session is involved in the update. Standard library only.

| Board | Tiles | Sources |
|---|---|---|
| AI cycle tripwires | Buyers borrowing · Nvidia financing customers · Spending growth slowing · Chips easier to get · Rates · Lenders · Nvidia price · Oil | SEC filings (XBRL), FRED, Yahoo Finance, Claude + web search |
| US market tripwires | Rates · Stocks vs bonds · Earnings · Breadth · Credit and fear · Outside shock (five sub-lamps) · Economy · AI bell · Fed | FRED, Yahoo Finance, Claude + web search |
| Taiwan tripwires | Taipei's red lines · Beijing's language · Taiwan's politics · U.S. commitment · PLA readiness · China's war chest · Substitution race · Taiwan's clock · Taiwan's will · Drills to inspections · Mobilization signs · Markets smell it | Claude + web search, Yahoo Finance (Taiwan dollar, Taiwan ETF), Polymarket public API |

**Hard data** (rates, spreads, oil, VIX, CPI, unemployment, claims, Fed target, yen, S&P 500, equal-weight
vs cap-weight, Nvidia price and inventory, hyperscaler capex vs cash flow) comes from FRED, Yahoo, and the SEC.
**Soft signals** (two Claude calls per run — one for the AI and market boards, one for Taiwan: earnings-call language, cloud backlog, AI-lab financing, GPU rental prices, memory pricing,
TSMC, forward P/E and estimate revisions, breadth statistics, China/Taiwan, financial-accident indicators,
policy shocks, Fed outlook) come from Claude with web search. Without an Anthropic key, those tiles
say "not checked" and the hard-data tiles still work.

## Files

- `tripwires.py` — the engine. Thresholds are in the `CONFIG` block at the top.
- `template.html` — the app. The engine injects the data and writes `docs/index.html`.
- `docs/` — the built app (`index.html`, `data.json`). This is what you open or host.
- `history.json` — every reading, per tile, for the trend lines. `state.json` — last statuses, for alerts.

## Setup (15 minutes)

1. Copy `.env.example` to `.env` and fill in:
   - `FRED_API_KEY` — free, instant: https://fred.stlouisfed.org/docs/api/api_key.html
   - `ANTHROPIC_API_KEY` — https://console.anthropic.com (about $0.20–0.50 per run)
   - `EDGAR_USER_AGENT` — your name and email (the SEC requires it in the request header)
   - Alerts: Gmail address plus an *app password* (Google Account → Security → 2-Step Verification → App passwords),
     and/or Pushover for phone push.
2. `python3 tripwires.py --test` — confirms alert delivery.
3. `python3 tripwires.py` — first run. Sets the baseline and alerts on anything already amber or red.
4. Open `docs/index.html`.

## Two ways to run it

**A. Your Mac, viewed anywhere.** Schedule it with the included `com.tripwires.plist`
(edit the path, then `cp com.tripwires.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.tripwires.plist`).
Put the folder in iCloud Drive and `docs/index.html` opens on your phone from the Files app. It's a single
self-contained file, so it works offline.

**B. GitHub, no computer needed.** Push the folder to a GitHub repository, add the `.env` values as repository
Secrets (same names), and turn on GitHub Pages (Settings → Pages → Deploy from a branch → `/docs`). The included
workflow runs every weekday, rebuilds the app, and commits it; the page is at
`https://<you>.github.io/<repo>/`. Note: GitHub Pages sites are public unless the repository is on a paid
plan with private Pages — the page contains only market data, nothing personal. Set `SITE_URL` so alerts link to it.

## Reading it

- Colors: green calm, amber watch, red tripped. A tile whose data fetch fails shows amber with the error, so
  silence never means "broken."
- The bell (AI board) rings when spending growth and chip availability both leave calm.
- The gate (market board) counts the three trigger tiles only; amplifiers and drivers don't count.
- The Taiwan gate counts three conditions: a reason (red lines, Beijing's language), an opening (three or more of the six opening tiles off green), and a decision (inspections, mobilization). The markets tile confirms; it doesn't count.
- The Taiwan board is mostly web-checked, so it needs the Anthropic key to be meaningful; without it only the markets tile is live.
- Any change to amber or red sends an alert; Monday's run sends the full table regardless.

## Relationship to the two claude.ai dashboards

The pages published in claude.ai are refreshed only when a Claude session writes to them. This app is the
script-refreshed version of the same boards with the same rules. Use this one for the unattended daily update;
ask Claude to refresh the claude.ai pages when you want a reasoned write-up alongside the numbers.

## Hand-off to Claude Code

Paste this into Claude Code (model: Claude Opus 5.5, effort: medium) with the folder open:

> Set up the tripwires app in this folder. Read README.md first. Create `.env` from `.env.example` and ask me for each value one at a time: FRED_API_KEY, ANTHROPIC_API_KEY, my name and email for EDGAR_USER_AGENT, and my Gmail address and app password for alerts. Run `python3 tripwires.py --test` and confirm I received the test email. Run `python3 tripwires.py` and open `docs/index.html` in my browser so I can check it. Then schedule it on this Mac with launchd every weekday at 7:30am using `com.tripwires.plist` as the template, and verify the job is loaded. If I say I want it hosted instead, create a private GitHub repository, push the folder, add the same values as repository Secrets, enable GitHub Pages from the `/docs` folder, set SITE_URL to the Pages address, and run the workflow once manually. Do not change any thresholds. When done, tell me exactly what will arrive in my inbox, when, and where the app lives.


## Added 2026-09-25: Plumbing and Fiscal dominance boards

Two more boards, rendered as tabs on the same page:

- **Plumbing tripwires** (`#plumbing`): the overnight repo market, the offshore dollar market, the new shadow-money
  floors, and the episode-to-meltdown sequence. Gate is a four-stage ladder (spike → spike stays → transmission →
  Fed fails to act). Hard data: FRED `SOFR`, `IORB`, `DFEDTARU`, `RPONTSYD`, `RRPONTSYD`, `WRESBAL`, `WLCFLPCL`,
  `SWPT`, `DGS10`, `DTWEXBGS`; Yahoo `^MOVE`, `JPY=X`, `SPY`. Soft signals: one extra Claude call (`PLUMBING_PROMPT`).
- **Fiscal dominance tripwires** (`#fiscal`): the four steps from central bank to Treasury financier, the market's
  verdict, and the pressure on the Fed. Gate is a four-step ladder. Hard data: FRED `WALCL`, `WRESBAL`, `T5YIFR`,
  `T10YIE`, `DFII30`, `DGS30`, `DTWEXBGS`, `GDP`, `A091RC1Q027SBEA`; Yahoo `GC=F`. Soft signals: one extra Claude
  call (`FISCAL_PROMPT`).

Each run now makes four Claude calls instead of two (about $0.40–0.90 per run at Sonnet prices). Tile ids are
unique across boards: `r*`, `e*`, `n*`, `q1`/`q2` on plumbing; `s*`, `v*`, `p*` on fiscal. History and state are keyed
by tile id, so existing `history.json` and `state.json` files carry forward untouched and the new tiles start fresh.
