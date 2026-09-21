# Obsidian Capital Research Pipeline — Reference Guide
## As of June 22, 2026 — updated August 30, 2026

---

### System Architecture

**One machine running the pipeline (as of August 2026):**
- **Jetson Orin Nano Super** — `/home/jay/stock_dashboard/` — sole active pipeline machine going forward
- Shogun (Pi 5 16GB NVMe) previously ran an identical pipeline in parallel; retired from active duty in August 2026. Code/config there may now be stale relative to Jetson.

**Five Hermes agent profiles:**
- `jansky` / default — Head of AI Operations (default profile)
- `jupiter` — Stock analyst (128 equities, 16 sectors)
- `mercury` — Currencies, Cryptocurrencies & Commodities (CCC) + ETFs
- `nova` — Legal research + earnings call intelligence
- `atlas` — Macroeconomic advisor

**Four MCP servers — all consolidated into `stock_dashboard/mcp/`:**
- Port 8642: `webmcp` (`mcp/app.py`) — 14+ financial data tools
- Port 8643: `macromcp` (`mcp/app2.py`) — 16 FRED-backed macro tools (added `get_fed_communications`, Aug 2026)
- Port 8644: `novamcp` (`mcp/app3.py`) — 9 legal/earnings research tools
- Port 8645: `mercurymcp` (`mcp/app4.py`) — 16 CCC/ETF data tools

**SearXNG** via Docker on port 8080 (Jetson)
- Engines enabled: DuckDuckGo, Bing, Brave, Yahoo, Google, Wikipedia,
  Startpage, DuckDuckGo News, Bing News, and (as of Aug 2026) `braveapi`
  — a paid Brave Search API key. Considered removing it over cost concerns
  (worry it could run as high as ~$20/run given web-search API pricing
  generally), but kept after directly measuring actual usage: a full
  `nova_earnings_call.py` run costs roughly $2. Currently active.
- **Known instability:** individual scraped engines (google, duckduckgo,
  brave, yahoo, etc.) intermittently mass-suspend (CAPTCHA, rate limit,
  "too many requests," timeouts) — sometimes all but one or two engines
  at once, sometimes recovering within minutes unprompted. A quick
  `docker restart searxng` is a reasonable first troubleshooting step
  before assuming a code regression when a SearXNG-dependent tool
  (crypto news, Fed communications, earnings search) comes back
  suspiciously empty.
- Docker restart policy set to `unless-stopped` — auto-starts on reboot:
  `docker update --restart unless-stopped searxng`
- `SEARXNG_URL` env var: `mercurymcp` (app4.py) and the new
  `get_fed_communications` tool (app2.py) both default to
  `http://localhost:8080` in code if the env var isn't set — safer than
  requiring explicit configuration. **`novamcp` (app3.py) previously
  defaulted to an empty string and required `SEARCH_PROVIDER=searxng` +
  `SEARXNG_URL` to be explicitly set as systemd service environment
  variables** — when missing, `nova_earnings_call.py`/`nova_legal.py`'s
  search silently fell through to calling DuckDuckGo directly instead
  of going through SearXNG. Fixed Aug 2026 via a systemd drop-in
  override (`~/.config/systemd/user/novamcp.service.d/override.conf`).
  Worth checking this pattern (missing env var → silent wrong fallback)
  if any MCP server's search behavior looks off after a deploy.

---

### Agent Profiles & Models

```
Agent    Profile     Model                              Role
──────   ─────────   ────────────────────────────────   ────────────────────────────
Jansky   default     deepseek/deepseek-v4-flash-0731     Head of AI Operations
Jupiter  jupiter     deepseek/deepseek-v4-flash-0731     Equity Research Analyst
Mercury  mercury     deepseek/deepseek-v4-flash-0731     CCC + ETF Analyst
Nova     nova        deepseek/deepseek-v4-flash-0731     Legal & Earnings Intel
Atlas    atlas       deepseek/deepseek-v4-flash-0731     Macroeconomic Advisor
```

**Model history:** Owl Alpha (used through mid-2026) disappeared from
OpenRouter during a ~6-week gap in pipeline operation. DeepSeek V4 Flash
climbed from ~10th most-used model on OpenRouter to #1 over the same
period and was adopted pipeline-wide as the price/performance leader —
confirmed solid after 2 months of production use. When the pipeline
resumed after the 6-week gap, Jupiter's profile had not been updated
from V4 Flash 0423 to 0731 the way every other agent's had — an
oversight, not a deliberate choice. Fixed Aug 2026; all five agents now
run the same 0731 build.

**Reasoning effort — full history, since this went through several
incorrect theories before landing (Sept 2026):**

DeepSeek V4 Flash is a reasoning model. Long, dense outputs (Mercury's
CCC report, Jupiter's per-ticker analysis, the JSON-only ranking/trade
calls) have shown repeated corruption: garbled word fragments, stray
CJK/foreign-language tokens spliced into English prose, hallucinated or
drifted numbers, and — most distinctively — the model spontaneously
deciding mid-response that its own draft has errors and rewriting the
whole thing from scratch, which is what actually introduces the worst
corruption (confirmed directly in a real MGM ticker report: PEG ratio
flipped from 0.51 to 2.49 with garbled self-contradicting reasoning, a
fabricated "BlackRock -2.1%" institutional data point that didn't exist
in the source data, and a literal Vietnamese word — "đọc" — spliced
into English prose).

1. **First theory (wrong): `--reasoning low` would fix it** by reducing
   how many tokens the model spends on hidden reasoning before the
   visible output, leaving more room to finish. Applied to Mercury
   first, seemed to help — but this was very likely coincidental. A web
   search confirmed OpenRouter's own listing for this model states only
   **"high" and "xhigh" reasoning efforts are supported** — `low` was
   probably a silently-ignored no-op the whole time, for every agent it
   was applied to.
2. **Second theory: switch to `--reasoning high`** — the one setting
   actually confirmed supported for this model. Applied to all Jupiter
   calls (`weekly_research.py`, `sector_ranking.py` ×3) and Mercury's
   calls. This is a **real, confirmed-supported** setting, unlike `low`.
3. **`--reasoning high` fixed nothing for the strict-JSON calls, and
   made them worse.** Real debug captures showed Jupiter's sector
   ranking outright truncating mid-response under `high` (a JSON object
   missing a comma and never closing its brackets — consistent with
   reasoning tokens crowding out the output budget on a call that has
   zero tolerance for an incomplete response, unlike prose). **Removed
   `--reasoning high` from the two strict-JSON calls in
   `sector_ranking.py`** (`rank_sector()`, `pitch_trades_for_sector()`)
   — kept on prose calls (main Jupiter synthesis, Mercury synthesis,
   the delta-summary narrative).
4. **The self-narration/rewrite behavior itself turned out to be
   independent of the reasoning-effort setting entirely.** Debug
   captures showed the exact same behavior — a mid-JSON monologue
   ("Wait — I must correct the data above... Let me recount precisely
   from scratch") — occurring identically whether the reasoning flag
   was set or removed. A pre-existing docstring in `sector_ranking.py`'s
   JSON extractor even referenced this same failure mode occurring with
   a **different model** (MiniMax M2.7) months earlier — this appears to
   be a persistent, cross-model tendency, not something fixable by
   adjusting one parameter.
5. **Current approach: stop trying to prevent it, make extraction
   survive it.** Rather than continuing to chase a prompt/parameter fix
   for a behavior that resisted several attempts, built a bracket-depth-
   aware salvage extractor (`_salvage_ticker_entries()`) that finds and
   parses each individual `{"ticker": ...}` object independently,
   keeping whatever entries are intact and discarding only the ones
   that are actually corrupted — see Script Reference →
   `sector_ranking.py` and `jansky_review.py` below for the full
   implementation. This is the layer actually holding the pipeline
   together now, not the reasoning-effort setting.

**Current state:** `--reasoning high` on all prose calls (Jupiter main
synthesis, Mercury synthesis ×2, Mercury/Jupiter delta-narrative calls);
no reasoning flag on the strict-JSON calls (`sector_ranking.py`'s
ranking and trade-pitch calls never had one reliably — `jansky_review.py`'s
decisions call never had one at all). **Not fully resolved** — the
self-narration behavior still occurs periodically even with these
settings; the salvage layer is what's actually preventing data loss when
it does. Planning to experiment with a different model (Sept 2026) —
a one-off test run against a free OpenRouter model
(`nvidia/nemotron-3-ultra-550b-a55b:free`) produced a *different*, not
yet diagnosed failure (a fixed 91-character response, identical before
and after a retry, that never parsed) — deprioritized, not yet
investigated further.

**Other agent-level config keys like `max_tokens` and
`agent.reasoning_effort` are NOT reliably read** when set via `hermes
config set` on a profile (CLI warns "not a recognized config key...
bridged to the environment for skills/external tools" for both) — use
the `--reasoning` flag directly on each subprocess call instead of
trying to persist it in `config.yaml`.

**Agent memory notes:**
- Each agent's `MEMORY.md` is injected by Hermes CLI before `-z` prompts.
- Atlas's MEMORY.md must NOT describe her as producing two output files
  (YAML + prose) — this causes her to write a completion note instead of
  the actual briefing. Keep Atlas memory focused on analytical role only.
- All agent SOUL.md files should reference "128 stocks, 16 sectors" (not
  127/120/15) — updated across Atlas, Mercury, Jupiter, and Jansky in
  August 2026 when the watchlist grew to 128 (see Watchlist section).

---

### Central Configuration — `obsidian_config.json`

All pipeline-wide constants live in `stock_dashboard/obsidian_config.json`.
Scripts read this file at startup; hardcoded values are deprecated.

```json
{
  "trade_limits": {
    "min_trade_dollars": 250000,
    "max_trade_dollars": 1000000
  },
  "position_limits": {
    "max_etf_position_pct": 15.0,
    "max_equity_position_pct": 10.0
  },
  "cash_floor": {
    "min_cash_pct": 10.0
  },
  "staleness_thresholds": {
    "earnings_stale_days": 180,
    "legal_stale_days": 90
  },
  "summary_quality": {
    "min_summary_chars": 2000
  },
  "trade_feedback": {
    "feedback_persist_weeks": 2
  },
  "fragment_dir": "fragments",
  "mercury_allowed_etfs": ["IAU","BITQ","VDE","GLD","SLV","PDBC",
                            "DBA","USO","UNG","CPER","VNQ","IYR",
                            "XLRE","TLT","HYG"]
}
```

Scripts reading obsidian_config.json: `sector_ranking.py`, `weekly_mercury.py`,
`jansky_review.py`, `pipeline_health.py`, `repair_summaries.py`

**Note:** `nova_earnings_call.py`'s 85-day staleness window is hardcoded
in-script (`STALE_DAYS`), not read from `obsidian_config.json` — separate
from `earnings_stale_days` (180) above, which is `pipeline_health.py`'s
own (looser) threshold for flagging attention. The two thresholds are
intentionally different and not meant to be unified.

---

### Watchlist

**`watchlist.json`** — **128** tickers across 16 sectors (dynamic — no hardcoded counts):

```
Semiconductors (8): AVGO AMD QCOM MU INTC LRCX TXN CSCO
SaaS (8):           CRM ADBE NOW ADP ADSK DDOG IBM TEAM
Healthcare (8):     LLY NVO ABBV MRK PFE AMGN GILD UNH
Utilities (8):      PNW NEE SO DUK AEP SRE VST XEL
Packaging (8):      IP PKG SW AMCR BALL CCK AVY REYN
Casinos (8):        WYNN MGM LVS PENN FLUT GLPI BYD CZR
Banks (9):          JPM BAC C WFC USB HBAN MTB CFG V
Logistics (8):      FDX UPS JBHT EXPD CHRW LSTR GXO OLDF
Insurance (8):      MRSH CB PGR CI ELV ALL HUM MET
Mining (8):         BHP RIO SCCO NEM FCX AEM VALE B
Industrials (8):    CAT GE GEV RTX BA DE LMT HON
Energy (8):         XOM CVX COP FSLR EOG LNG DVN EPD
Restaurants (8):    MCD SBUX CMG YUM QSR DRI DPZ TXRH
Real_Estate (8):    WELL PLD EQIX AMT SPG DLR PSA CBRE
Retail (8):         BBY WMT COST HD TJX LOW CVS TGT
Mag7 (7):           AMZN NVDA AAPL MSFT GOOG TSLA META
```

**Change (August 2026): watchlist grew from 127 to 128 tickers.** `V`
(Visa) was added to the Banks sector — it was the one held Neptune
position not yet on the watchlist. Banks was the closest sector fit
given Visa's tight linkage to bank/payments flows, even though Visa is
a payments network rather than a depository bank. SOUL.md files for
Atlas, Mercury, Jupiter, and Jansky were all updated to say "128
stocks, 16 sectors."

**Also (August 2026): `OLDF` replaced `HUBG` in the Logistics sector.**
Reason not explicitly confirmed, but HUBG's known issues at the time
(Nasdaq deficiency, multi-year restatement, a pattern of repeated Form
12b-25 late-filing notices — see Legal Risk Landscape section) are a
plausible motivation. Treat as unconfirmed until stated directly.

Note: GOOGL removed (was duplicate of GOOG). Coverage: GOOG only.
GOOGL is still a holdings position — Jupiter sees both via `HOLDINGS_ALIASES`
in `weekly_research.py`:
```python
HOLDINGS_ALIASES = {
    "GOOG": ["GOOGL"],
    # "BRK.B": ["BRK.A"],  # add as needed
}
```

Ticker/sector counts are computed dynamically from `watchlist.json` throughout
the pipeline — no hardcoded values remain in any script.

---
---

docker restart searxng
sleep 15

# Then retest
curl -s "http://localhost:8080/search?q=Apple+earnings+press+release&format=json" | \
python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Results: {len(d.get(\"results\",[]))}')"

### Weekly Pipeline Run Order

```bash
# 0. Pre-flight health check (optional but recommended)
python3 pipeline_health.py

# 1. Macro research (Atlas + FRED + Fed Communications)
python3 weekly_macro.py

# 2. CCC + ETF research (Mercury + mercurymcp)
#    Also pitches ETF trades → trade_decisions.json["mercury"]
python3 weekly_mercury.py

# 3. Stock research — 128 tickers (Jupiter + all MCP tools)
#    Automatically runs sector_ranking.py as Pass 2
#      sector_ranking.py: rankings + Jupiter trade pitches → trade_decisions.json["jupiter"]
#    Rebuilds dashboard with all fragments after Pass 2
python3 weekly_research.py

# 4. Repair any broken/truncated summaries
python3 repair_summaries.py
#    Use --refetch for tickers with bad data (e.g. after connection drop):
python3 repair_summaries.py --refetch CVS TGT AMZN

# 5. Legal research for flagged companies (Nova)
python3 nova_legal.py HUBG BA LOW

# 6. Earnings call research refresh (Nova)
python3 nova_earnings_call.py --stale-only

# 7. Sector ranking repair (if sector_ranking.py failed mid-run)
python3 repair_sector_ranking.py             # auto-detect failed sectors
python3 repair_sector_ranking.py Utilities   # repair specific sector
python3 repair_sector_ranking.py --delta-only  # re-run delta + fragment only

# 8. Jansky 21-pass weekly review of all agent outputs
#    Pass 21 reviews trade pitches, approves/rejects, writes jansky_trade_feedback.json
python3 jansky_review.py
python3 settle_portfolio.py --dry-run    # preview first — recommended. 
python3 settle_portfolio.py              # then for real

# 9. Post-run health check
python3 pipeline_health.py
```

Refresh the dashboard after any standalone repair or fragment update:
```bash
bash rebuild_dashboard.sh
```

**Note on step order:** legal (5) and earnings (6) run AFTER Jupiter
(step 3) intentionally — `nova_legal.py`'s flagging depends on
`NOVA_FLAG_DATA` that Jupiter emits when synthesizing each ticker's
summary. Running Nova before Jupiter in the same cycle would mean no
fresh flags exist yet for that week, forcing Nova to act on last week's
flags — considered and explicitly rejected as a reordering in August
2026 for this reason.

---

### Trade Recommendation System (since June 22, 2026)

Jupiter and Mercury pitch trade recommendations to Jansky each week.

**Flow:**
1. `sector_ranking.py` — Jupiter pitches equity trades per sector
2. `weekly_mercury.py` — Mercury pitches ETF trades
3. Both write to `trade_decisions.json` (Jupiter section + Mercury section)
4. `jansky_review.py` Pass 21 — Jansky reviews all pitches, approves or rejects
5. `jansky_trade_feedback.json` — Per-ticker feedback injected into next week's
   Jupiter/Mercury prompts so agents learn from rejections

**Trade action types:**
- `ADD_TO_POSITION` — ticker already held, add to existing position
- `NEW_POSITION` — ticker not held, open new position
- `REDUCE_POSITION` — ticker held, reduce or exit due to overbought/weakening thesis

**Trade constraints (enforced by Jansky):**
- Min trade size: $250,000
- Max trade size: $1,000,000
- Max single ETF position: 15% of portfolio
- Max single equity position: 10% of portfolio
- Cash floor: 10% minimum (Jansky warns and prioritizes if aggregate buys breach this)

**Jansky trade review (Pass 21):**
- Two-call approach: narrative review call + dedicated JSON decisions call
- JSON call returns `{"decisions": [{"ticker": "XYZ", "decision": "APPROVE|REJECT", "rationale": "..."}]}`
- Hard overrides: pre-flagged position limit breaches auto-rejected regardless of LLM output
- Feedback persists 2 weeks then pruned (`feedback_persist_weeks` in obsidian_config.json)

**`trade_decisions.json` structure:**
```json
{
  "jupiter": {
    "run_date": "2026-06-22",
    "trade_count": 34,
    "trades": [
      {
        "ticker": "AVGO", "action": "ADD_TO_POSITION",
        "dollars": 350000, "sector": "Semiconductors",
        "pitched_by": "jupiter", "rationale": "...", "conviction": "HIGH",
        "key_risk": "..."
      }
    ]
  },
  "mercury": {
    "run_date": "2026-06-22",
    "trade_count": 0,
    "trades": []
  }
}
```

Archive: previous week's `trade_decisions.json` copied to
`reports/trade_decisions_YYYYMMDD_HHMM.json` before overwrite.

---

### Dashboard Rebuild

After any standalone repair or fragment update, refresh the dashboard:

```bash
bash rebuild_dashboard.sh
```

The rebuild script loads all five fragments and regenerates `dashboard.html`.
Fragments are in `stock_dashboard/fragments/` (root copies kept in sync
for backward compat):

```
fragments/mercury_dashboard_fragment.html   CCC tab
fragments/jansky_dashboard_fragment.html    Jansky tab
fragments/sectors_delta_fragment.html       SECTORS + DELTA tabs
fragments/trades_dashboard_fragment.html    TRADES tab
fragments/etf_dashboard_fragment.html       ETF tab
```

Fragment architecture: each agent writes a self-contained HTML fragment;
`generate_dashboard()` in `weekly_research.py` assembles them all.

---

### Dashboard Tab Structure

```
Nav bar (left to right):
SECTORS | DELTA | OUTLOOK | CCC | JANSKY | TRADES | ETF | [128 ticker buttons]

SECTORS  — Jupiter forced-distribution sector rankings (16 sectors)
DELTA    — Week-over-week verdict/price/RSI/MA changes
OUTLOOK  — Atlas macro summary + Jupiter sector overview
CCC      — Mercury weekly CCC report (purple accent, ⚡)
JANSKY   — Jansky weekly review: posture badge, agent status cards,
           executive briefing, sector accordion, flags panel
TRADES   — Jupiter + Mercury trade pitches with Jansky APPROVE/REJECT decisions
ETF      — Mercury ETF watchlist (17 ETFs): data cards + Mercury commentary
[tickers]— Individual Jupiter stock reports (128 buttons)
```

Nav order is controlled by fragment injection sequence in `generate_dashboard()`:
1. Base dashboard JS builds ticker buttons and inserts OUTLOOK at position 0
2. Mercury fragment appends CCC button after OUTLOOK
3. Jansky fragment appends JANSKY button after CCC
4. Trades fragment appends TRADES button after JANSKY
5. ETF fragment appends ETF button after TRADES
6. sectors_delta_fragment inserts SECTORS at position 0, DELTA at position 1
   → Final order: SECTORS | DELTA | OUTLOOK | CCC | JANSKY | TRADES | ETF | [tickers]

Important: Fragment panel divs must use CSS class only (`class="ticker-panel"`)
with NO inline `style="display:none;"` — inline styles override CSS and cause
blank panels.

`generate_dashboard()` signature (weekly_research.py):
```python
generate_dashboard(data, date, macro_backdrop, macro_summary,
                   mercury_fragment, jansky_fragment,
                   sectors_delta_fragment, trades_fragment, etf_fragment)
```

---

### Script Reference

**`weekly_macro.py`**
- Calls **16** macromcp tools via SSE HTTP (FRED data + Fed Communications, Aug 2026)
- Builds `macro_backdrop.yaml` — injected into all Jupiter and Mercury prompts
- Calls `atlas -z prompt --reasoning <level if needed>` for weekly briefing
  (the `--skills hermes-cli` flag previously used here — and in every other
  agent subprocess call — was removed Aug 2026; see Hermes Agent
  Configuration section)
- SOUL.md is NOT prepended in `generate_summary()` — causes Atlas to write
  a completion note instead of the briefing. Prompt is self-contained.
- **New (Aug 2026): `get_fed_communications` tool** — searches FOMC
  statements, Fed official speeches, and major events (e.g. Jackson Hole)
  via SearXNG. Atlas's Section 1 (Rate & Monetary Policy) prompt
  instruction was updated to explicitly incorporate this qualitative
  signal when present, and to fall back to numeric-only analysis when
  the search returns nothing (Atlas correctly reports "no qualitative
  Fed signal available" rather than fabricating content — confirmed in
  testing). Was added specifically because Atlas previously had no way
  to surface an event like a Jackson Hole speech at all — every other
  Atlas tool is a numeric FRED/BLS/Treasury series, incapable of
  capturing a speech or statement itself.
- Outputs: `data/macro_YYYYMMDD_HHMM.json`, `macro_backdrop.yaml`,
  `macro_summary.md`

**`weekly_mercury.py`**
- Loads `macro_backdrop.yaml` and `neptune_holdings.json` at startup
- Reads `obsidian_config.json` for trade limits and ETF universe
- Calls 16 mercurymcp tools: forex, crypto, ETFs, energy, metals,
  agriculture, livestock, WASDE, crop progress, NOAA drought, COT, Baltic Dry
- Calls `mercury -z prompt --reasoning high` for CCC synthesis and ETF
  trade pitches — see Agent Profiles & Models → Reasoning effort for the
  full history (started at `low`, which was likely a no-op for this
  model; corrected to `high`, the actually-supported setting)
- Calls `pitch_etf_trades()` → writes Mercury section of `trade_decisions.json`
  - **Buy-only bias fixed (Sept 2026):** in ~8 weeks of live trading,
    neither Mercury nor Jupiter had ever pitched a single
    `REDUCE_POSITION` trade — only `ADD_TO_POSITION`/`NEW_POSITION`.
    Root cause: the JSON schema shown to the model only ever included a
    worked example for a buy-shaped action (`NEW_POSITION` here), never
    `REDUCE_POSITION`, and nothing in the prompt required active review
    of held positions for weakness. Fixed by adding a genuine
    `REDUCE_POSITION` example to the schema, an explicit instruction to
    review all 3 held ETFs (IAU, BITQ, VDE) for overbought/weakening
    signals before pitching, and language connecting cash-floor pressure
    directly to the need for reduce candidates (the portfolio can't fund
    every buy pitch without selling something first — see
    `sector_ranking.py` below for the identical fix applied to Jupiter).
    Not yet validated against a real pipeline run.
- Generates `fragments/etf_dashboard_fragment.html`
- Outputs: `data/mercury_YYYYMMDD_HHMM.json`, `mercury_latest.json`,
  `mercury_backdrop.yaml`, `mercury_summary.md`,
  `fragments/mercury_dashboard_fragment.html`,
  `fragments/etf_dashboard_fragment.html`

**`weekly_research.py`**
- Loads `macro_backdrop.yaml`, `nova_supplemental.json`,
  `neptune_holdings.json`, `jansky_trade_feedback.json`, and all fragments
- Runs 14 webmcp tools per ticker
- Calls `jupiter -z prompt --reasoning high` for stock summaries — see
  Agent Profiles & Models → Reasoning effort for the full history
- Nova earnings summaries are injected into Jupiter's prompt via
  `_build_nova_preamble()` **when present**, but nothing in Jupiter's
  prompt requires them — no numbered section references or depends on
  the earnings preamble the way Section 9 explicitly does for legal
  data. Nova earnings coverage is genuinely partial (see
  `nova_earnings_call.py` notes below); this was confirmed to already
  be a clean "nice to have" from Jupiter's side, requiring no prompt
  changes when coverage dropped.
- Holdings injection: if ticker is held in neptune_holdings, injects position
  details (shares, cost basis, market value, gain/loss) into prompt
- Alias support: `HOLDINGS_ALIASES = {"GOOG": ["GOOGL"]}` for dual-class shares
- Trade feedback injection: if ticker has Jansky feedback in
  `jansky_trade_feedback.json`, injects APPROVE/REJECT rationale into prompt
- Sector list and counts computed dynamically from `watchlist.json`
- Launches `sector_ranking.py` automatically as Pass 2
- After Pass 2 completes, rebuilds dashboard with all five fragments
- **Encoding fix (Aug 2026):** response parsing switched from `r.text`
  (which silently mis-decodes as Latin-1 when the MCP server's SSE
  response has no explicit `charset=`) to `r.content.decode('utf-8', ...)`
  — fixes box-drawing/em-dash mojibake (`â”€`, `â€"` etc.) that had crept
  into stored summaries. Same fix applied to `weekly_macro.py` and
  `weekly_mercury.py`.
- **Formatting template strengthened + self-correction rule added (Sept
  2026):** every section header standardized to `## N. SECTION NAME`,
  all data points required as vertical `-` bullets, and — the part that
  actually matters — an explicit instruction telling Jupiter to output
  the report **once, in a single pass**, with no self-critique or
  "corrected final version" step. Added directly in response to a real
  MGM ticker report where Jupiter decided mid-generation its own draft
  had errors and rewrote the whole thing, corrupting numbers (PEG ratio,
  2023 EPS), fabricating an institutional-ownership data point that
  didn't exist in the source, and splicing in a stray Vietnamese word.
  Did not fully eliminate the behavior (see Agent Profiles & Models →
  Reasoning effort) but is a real, still-standing mitigation.
- **Verdict badge parsing bug fixed (Sept 2026):** the dashboard's
  `verdictClass()`/`verdictLabel()` JS previously did a naive
  `.includes('ACCUMULATE')` substring search across a ticker's **entire**
  summary text, checked before `AVOID`/`WATCH`. This produced false
  positives whenever the prose discussed and explicitly *rejected* a
  verdict word (e.g. MU's own Section 13 said "...ACCUMULATE is wrong
  here" while giving an actual verdict of WATCH — the badge showed
  ACCUMULATE anyway, since the bare word appeared first in the text).
  Root cause confirmed unrelated to `sector_ranking.py`, contrary to
  initial suspicion. Fixed by parsing the actual "Overall Verdict" line
  via regex (`/Overall Verdict\**:?\s*(ACCUMULATE|WATCH|AVOID)/i`)
  instead of scanning the whole document for a bare keyword, with the
  old whole-text check kept only as a last-resort fallback if that
  specific line can't be found at all.
- Outputs: `data/research_YYYYMMDD_HHMM.json`, `dashboard.html`

**`sector_ranking.py`**
- Reads research JSON + `watchlist.json` + `neptune_holdings.json` +
  `obsidian_config.json` + `jansky_trade_feedback.json`
- Builds ~960-char briefs per ticker
- **Pass A:** Calls `jupiter -z prompt` for forced-distribution ranking per sector
  - Distribution: 1-2 ACCUMULATE, variable WATCH, 1-2 AVOID per sector
  - The originally-reported "Retail sector broken sort" issue (Aug 2026)
    was never independently root-caused before the much larger JSON
    corruption problem below was found and fixed (Sept 2026) — likely
    the same underlying cause. Worth confirming resolved on the next
    live run rather than assuming.
- **Pass B:** Calls `pitch_trades_for_sector()` per sector → Jupiter pitches
  ADD/NEW/REDUCE trades with full holdings context
  - **Buy-only bias fixed (Sept 2026):** see `weekly_mercury.py` above
    for the shared root cause — the JSON schema only ever showed a
    buy-shaped worked example, and the rules literally said 'Be
    explicit: "I recommend we buy $X of TICKER because..."' with no
    reduce-equivalent phrasing. Fixed with a genuine `REDUCE_POSITION`
    example, action-neutral phrasing guidance, a required instruction to
    review every held ticker in the sector for overbought/weakening
    signals before pitching, and explicit language connecting cash-floor
    pressure to the need for reduce candidates. Not yet validated
    against a real pipeline run.
- Writes `trade_decisions.json["jupiter"]` (archives previous week first)
- Computes delta tracker vs previous week
- Writes `fragments/sectors_delta_fragment.html` (combined SECTORS + DELTA)
- Archives fragment to `reports/sectors_delta_fragment_YYYYMMDD_HHMM.html`
- Run `rebuild_dashboard.sh` after standalone run to refresh HTML

**JSON reliability overhaul (Sept 2026)** — both the ranking and
trade-pitch calls in this file went through a multi-stage debugging
saga; final state:
- **`--reasoning high` removed from both JSON calls** (kept on the prose
  delta-summary call) after direct debug-file evidence showed it causing
  outright truncation — a response missing a comma and never closing
  its brackets, consistent with reasoning tokens crowding out a fixed
  output budget on a call that has zero tolerance for an incomplete
  response.
- **Explicit anti-hedging instructions added to both JSON prompts**
  ("CRITICAL: Output ONLY the JSON object... do not hedge, re-examine,
  or 'actually, let me reconsider' any value after writing it") —
  targeted a confirmed self-narration leak into JSON output (a real
  debug capture showed a stray `"really."` plus duplicate empty keys and
  literal `"..."` inside a trade pitch's `risks` array).
- **This alone did not fully suppress the behavior.** A later debug
  capture showed something more severe — a full English paragraph
  ("Wait — I must correct the data above... Let me recount precisely")
  spliced directly into what should have been pure JSON, breaking the
  structure entirely. This confirmed the self-narration tendency is not
  reliably preventable via prompt instructions alone for this model.
- **`_salvage_ticker_entries()` added** — a bracket-depth-aware (not
  regex-greedy) scanner that finds every individual `{"ticker": ...}`
  object in a raw response and parses each one independently, keeping
  whatever entries are structurally intact and discarding only the ones
  that are themselves corrupted, rather than losing the whole sector to
  one broken entry or a narration monologue elsewhere in the response.
  Wired in as `_extract_json()`'s Strategy 4 (last resort, only after
  raw parse / markdown-fence extraction / first-`{`-to-last-`}` scan all
  fail). Returns `{"_salvaged": True, ...}` so callers can flag partial
  results distinctly from full successes.
- **`_validate_ranking()` gained a completeness check** (`expected_count`
  param) — previously only checked that *present* entries had non-empty
  `strengths`/`risks`, which would silently accept a salvaged ranking
  missing several tickers entirely as "valid." Now a short ticker count
  also triggers the retry-with-arrays attempt.
- **`save_debug()` now overwrites per run instead of appending forever**
  — the debug files had been silently accumulating every failure since
  June, meaning a fresh failure's actual cause was buried under months
  of unrelated old errors. This directly interfered with diagnosing the
  real issue once and is worth remembering if debug output ever looks
  confusing again.
- **Validated (Sept 2026):** after all of the above, a live re-run on
  the four previously-worst sectors (Packaging, Logistics, Mining,
  Healthcare) succeeded cleanly with all 8 tickers present in each and
  zero salvage needed. A single clean run is not proof the underlying
  model behavior is gone — treat as "defenses working," not "root cause
  fixed," and watch for recurrence.
- The identical `_salvage_ticker_entries()` function now also exists in
  `jansky_review.py` (see below) — **duplicated, not shared**, since
  these are independent top-level scripts with no shared library. Any
  future fix to this function needs to be applied in both places.

**`repair_sector_ranking.py`**
- Surgical repair tool for failed sector rankings (companion to repair_summaries.py)
- Auto-detects failed/missing sectors or repair by name
- Re-runs rank_sector() and pitch_trades_for_sector() for failed sectors only
- Patches trade_decisions.json for repaired sectors
- Always re-runs delta tracker and rewrites fragment
- Usage:
  ```bash
  python3 repair_sector_ranking.py                    # auto-detect
  python3 repair_sector_ranking.py Utilities          # specific sector
  python3 repair_sector_ranking.py --delta-only       # fragment only
  python3 repair_sector_ranking.py --no-trades Util   # ranking only
  python3 repair_sector_ranking.py --dry-run          # preview
  ```

**`repair_summaries.py`**
- Reads `min_summary_chars` from `obsidian_config.json` (default 2000)
- Auto-detects broken summaries AND bad data (stub MCP responses < 250 chars)
- Flags bad-data tickers and recommends `--refetch` when detected
- `--refetch`: re-calls `research_ticker()` to re-fetch all MCP data before
  regenerating summary — use after internet connection drops mid-run
- Patches data and/or summary in-place into existing research JSON
- Imports `generate_summary()` directly from `weekly_research.py`, so it
  automatically inherits any fix made there (e.g. the `--skills hermes-cli`
  removal, Aug 2026, fixed a batch of failures here with zero changes to
  this file itself)
- **Fixed a false-positive (Aug 2026):** `"timed out"` was previously in
  `BROKEN_SIGNATURES` (the substring list `is_broken()` scans for). A
  perfectly healthy, complete summary that happened to *discuss* a
  different agent's (Nova's) timeout in its own prose — a legitimate,
  correct observation — tripped this check and got endlessly re-flagged
  as broken on every run, even though the length and verdict checks
  would have passed it cleanly on their own. Removed `"timed out"` from
  the signature list; the other two checks (length, missing verdict)
  already cover genuine generation failures without this false-positive
  risk. Worth remembering `Report complete and saved to` and
  `Let me produce the full` in the same list are the same *shape* of
  risk (bare substring match) — not known to have misfired yet, but not
  proven safe either.
- Usage:
  ```bash
  python3 repair_summaries.py                          # auto-detect broken
  python3 repair_summaries.py --refetch CVS TGT AMZN  # full data refetch
  python3 repair_summaries.py LOW                      # summary-only repair
  python3 repair_summaries.py --dry-run                # preview
  ```

**`nova_legal.py`**
- Reads latest research JSON, finds tickers with `needs_research: true`
  (from Jupiter's `NOVA_FLAG_DATA` in each ticker's summary)
- Skips tickers with current Nova legal records (within 90 days)
- Runs 5-tool legal pipeline per ticker: deep_sec, news, court, sec_enforce, doj
- Calls `nova -z prompt` for synthesis
- Saves to `nova_supplemental.json` with risk_level and research_confidence
- **Confirmed genuinely flag-driven, not blanket-researching everyone**
  (Aug 2026 investigation) — 51 of 128 tickers had legal records after
  ~2 months of weekly runs, consistent with organic accumulation via
  occasional real flags, not a "research everyone every week" pattern.
  A low flag count in any given week (e.g. only 1 ticker) is not
  inherently a bug.
- Correctly handles foreign private issuers (Form 20-F filers like VALE,
  NVO) via `sec_deep_result`'s "DEEP EXTRACTION" path — confirmed with
  real examples, not just 10-K filers.
- **Open issue (Aug 2026, not yet fixed):** the same encoding bug fixed
  in `weekly_macro.py`/`weekly_mercury.py`/`weekly_research.py` (see
  above) is still present in `app3.py`'s raw-field HTTP fetches — mojibake
  (`â”€`, `â\x80\x94` etc.) visible in `sec_deep_result` and
  `sec_enforcement_raw` fields in `nova_supplemental.json`. Does NOT
  currently leak into Jupiter's context (only the clean
  `nova_legal_summary` field gets injected via `_build_nova_preamble()`),
  so this is data-hygiene severity, not correctness-critical — but worth
  the same `r.content.decode('utf-8', ...)` fix eventually.
- Usage: `python3 nova_legal.py` or `python3 nova_legal.py HUBG CHRW`

**`nova_earnings_call.py`**
- Researches earnings calls for stale/pending tickers (85-day window, `--stale-only`)
- **Search backend (Aug 2026):** now routes through SearXNG (was silently
  calling DuckDuckGo directly — see `novamcp` env var fix above). A paid
  Brave Search API key (`braveapi` engine) was added mid-August for
  reliability. Cost was measured directly (~$2 for a full
  `nova_earnings_call.py --stale-only` run) and judged well worth it —
  kept in active use.
- **Domain handling:** `businesswire.com`, `seekingalpha.com`, and
  `fool.com` are all **excluded** from candidates. `businesswire.com`
  was confirmed dead via direct browser check on AAPL's earnings URLs
  (spanning 2020-2026, all removed). `seekingalpha.com`/`fool.com` are
  excluded because `fetch_url_text` (even via the Jina Reader fallback
  below) cannot extract usable text from either site — JS-rendered pages
  that return empty/near-empty content to a non-browser fetch. All three
  exclusions remain in place; none have been re-enabled.
- **`fetch_url_text` fallback:** when the primary fetch returns "No
  readable content found" (a JS-rendered page a static HTML fetch can't
  see into), the tool now retries via **Jina Reader** (`https://r.jina.ai/
  <url>`) — a free, no-API-key-needed service (~20 req/min) that renders
  in a real headless browser server-side. Confirmed this correctly
  distinguishes a genuinely JS-blocked page from a page that's simply
  dead/removed (the latter fails identically through Jina too).
- **Search reliability fixes (Aug 2026):**
  - Company name is now quoted as an exact phrase in search queries
    (`"Advanced Micro Devices, Inc." Q2 2026 ...`) — unquoted, generic
    boilerplate like "Q2 2026 earnings results" (which appears in every
    company's press release title) was outranking the actual target
    company's own release.
  - Quarter-guessing (`_recent_quarters()`) had an off-by-one: it
    included the *current, still-open* quarter as its first guess,
    wasting one of two attempts on a search that can never succeed.
    Fixed to start from the most recently *completed* quarter.
  - `parse_call_date()` previously accepted any `\d{4}-\d{2}-\d{2}`-shaped
    match with no sanity check — matched an article ID digit sequence
    once, producing `call_date: "3338-08-04"`. Now validates the parsed
    year against a plausible range (2015–current+1) and tries every
    regex match per pattern (not just the first), plus added support for
    abbreviated month names (`"Aug. 04, 2026"`) which the original regex
    set didn't handle at all.
  - A single retry with an 8-second delay was added around the whole
    search→picks sequence — SearXNG engine suspensions were directly
    observed clearing within minutes mid-session, so a short delay can
    recover from transient unavailability instead of permanently
    failing a ticker with perfectly good underlying data.
  - The search/pick step now prints the full numbered candidate URL
    list before Nova's pick decision (previously only Nova's final
    reasoning was visible), and fetch failures now print the actual
    reason (404/403/paywall/Cloudflare-challenge/"too short" with a
    content snippet) instead of a bare `✗` — both added specifically to
    make future failures diagnosable without re-deriving root cause from
    scratch each time.
- **Self-admitted-failure check (Aug 2026):** Nova's synthesis can
  produce a well-formed `CALL_DATE`/`FISCAL_QUARTER` pair while its own
  prose honestly states it had nothing real to work from (e.g. "Cannot
  be assessed — no actual transcript content was retrieved," from a
  JS-rendered IR hub page that passed the length check but contained no
  substance). This combination previously passed every other check and
  got saved as a healthy `current` record. Now scans the synthesis
  output for a short list of self-admitted-failure phrases before
  saving; on a match, treats it as `no_data` instead. Narrowly scoped to
  this synthesis output only, not a broad ban.
- Date sanitization (`_sanitize_sort_date()`) rejects future dates and
  pre-2020 dates from sort/selection order (predates and is complementary
  to the `parse_call_date()` fix above)
- **Failed-search tracking + retry cooldown (Sept 2026):** roughly half
  the watchlist (66 of 128 tickers, at the time this was measured)
  consistently has no findable earnings source via wire-service search —
  re-attempting these every week wasted Brave API calls for no benefit.
  Two changes: (1) every `no_data` outcome now writes
  `last_search_attempted`/`last_search_result: "no_new_data_found"` into
  the ticker's existing `earnings` record, without touching its
  `call_date`/summary from the last real success; (2)
  `earnings_is_current()` treats a `no_new_data_found` result within the
  last `NO_DATA_RETRY_DAYS` (30, separate constant from `STALE_DAYS`) as
  "current" and skips re-attempting, regardless of how stale `call_date`
  has become. Console output now separately reports how many skipped
  tickers are "current within 85 days" vs. "no-data cooldown within 30
  days." A ticker that later gets a real success doesn't explicitly
  clear these fields — harmless, since a fresh `call_date` satisfies the
  normal check regardless, just slightly untidy leftover data.
- **`jansky_review.py`'s stale-earnings flags now respect this
  cooldown too** — see that entry below. A ticker Nova has genuinely
  already checked this week no longer gets flagged as urgently stale
  just because `call_date` itself is old.
- Saves `call_date`, `fiscal_quarter`, `research_date`, `nova_earnings_summary`
- Usage:
  ```bash
  python3 nova_earnings_call.py                   # full watchlist
  python3 nova_earnings_call.py --stale-only      # stale/pending only
  python3 nova_earnings_call.py AMD MSFT ...      # specific tickers (bypasses staleness check)
  ```
- **If a ticker's record needs forcing to re-research** despite showing
  as "current" (e.g. to test a fix, or to correct a bad `call_date`),
  the staleness check has no `--force` override — directly reset the
  stored `call_date` to something clearly old first:
  ```python
  import json
  d = json.load(open('nova_supplemental.json'))
  d['TICKER']['earnings']['call_date'] = '2000-01-01'
  json.dump(d, open('nova_supplemental.json', 'w'), indent=2)
  ```
  then re-run with the ticker specified explicitly. Note: this alone
  isn't enough if the record is in no-data cooldown — also clear
  `last_search_attempted`/`last_search_result`, or the cooldown check
  will still skip it.

**`jansky_review.py`**
- 21-pass weekly review of all agent outputs
- Pass 1: Atlas macro review
- Pass 2: Mercury CCC review
- Pass 3: Nova legal & earnings review (with Python pre-checks)
- Passes 4-19: Jupiter sector reviews × 16 (full summaries per ticker)
- Pass 20: Cross-agent synthesis + Rankings + Delta
- **Pass 21: Trade review**
  - Reads `trade_decisions.json` (Jupiter + Mercury pitches)
  - Two-call approach: narrative review + dedicated JSON decisions call
  - Enforces trade limits, position limits, cash floor from `obsidian_config.json`
  - Writes approved/rejected decisions to `jansky_trade_feedback.json`
  - Generates `fragments/trades_dashboard_fragment.html`
  - **JSON reliability overhaul (Sept 2026)** — this call was far more
    fragile than `sector_ranking.py`'s (only 2 parse attempts, no retry
    call, no salvage) before being brought up to the same standard:
    - Real-world failure directly observed: the decisions call returned
      unparseable JSON with **no retry attempt at all**, defaulting 22
      of 24 real trade decisions to `PENDING` for the week (only the 2
      Python-pre-flagged hard-override rejects survived).
    - Added a retry with a stricter re-prompt (same pattern as
      `sector_ranking.py`) when the first attempt is incomplete.
    - Added the same `_salvage_ticker_entries()` bracket-depth-counting
      function (duplicated from `sector_ranking.py`, not shared — see
      that entry above) as a last-resort fallback.
    - **A second, distinct failure mode surfaced after the above fix**:
      a response that parsed as syntactically valid JSON, but contained
      decisions for only 2 of 24 tickers (the same 2 pre-flagged ones) —
      Jansky simply ignored the instruction to decide on all 24. Because
      this "succeeded" at parsing, the retry never fired. Fixed by
      requiring `len(parsed_map) >= expected_count` for full success,
      not just a non-empty result — a short result now also triggers
      the retry, and results from the original and retry attempts are
      **merged (union)** rather than the retry replacing the original,
      since either attempt may cover a different subset of tickers.
    - Console output now distinguishes a full parse (`✓ N decisions
      parsed`), a short/partial result after all recovery attempts
      (`⚠ N of M decisions — rest PENDING`), and complete failure (`✗
      JSON parse failed — all PENDING`) — previously only the last two
      states were distinguishable.
    - **One-off test with a different model** (`nvidia/nemotron-3-ultra-550b-a55b:free`
      via OpenRouter, free tier, tried specifically because of the
      ongoing DeepSeek V4 Flash 0731 JSON corruption issues) produced a
      **third, different failure**: a fixed 91-character response,
      identical before and after the retry, that never parsed as JSON.
      Not yet diagnosed — the raw response text was never captured
      (this call doesn't save a debug file the way `sector_ranking.py`'s
      does) and wasn't preserved in terminal scrollback either.
      Deprioritized; worth adding debug-file saving here too if this is
      revisited.
- Reads `obsidian_config.json` for all thresholds (no hardcoded values)
- Reads `neptune_holdings.json` for full portfolio context in synthesis + trade review
- **Nova earnings staleness bullet removed from SOUL.md (Aug 2026):**
  since `nova_earnings_call.py`'s coverage is now intentionally partial
  (see above), Jansky no longer flags stale/missing Nova earnings
  records as a data-quality gap — that bullet was deleted outright
  (not reworded) from the "data gaps that degrade research quality"
  list in Jansky's SOUL.md. Jansky still flags stale Nova *legal*
  records (the pre-existing, unchanged bullet for that).
- **Stale-earnings flag now respects the no-data retry cooldown (Sept
  2026):** added a shared `_earnings_recently_attempted()` helper
  checked at all four places Jansky evaluates earnings staleness (Pass
  3's console flag, the per-ticker sector-review cross-reference, and
  the final report JSON recompute) — if Nova genuinely attempted a
  search within the last ~7 days (regardless of whether it found
  anything), the staleness flag is suppressed even though `call_date`
  itself may still be old. Fixes a real case: Jansky was recommending
  "run nova_earnings_call.py HUBG" on a ticker that had already been
  checked that same week and had nothing new available (compounded by
  HUBG having since left the watchlist entirely — see Watchlist section
  for the OLDF swap).
- Outputs: `data/jansky_YYYYMMDD_HHMM.json`, `jansky_latest.json`,
  `fragments/jansky_dashboard_fragment.html`,
  `fragments/trades_dashboard_fragment.html`,
  `jansky_trade_feedback.json`
- Usage: `python3 jansky_review.py`
- Flags: `--dry-run`, `--pass atlas|mercury|nova|jupiter|synthesis|trades`,
  `--sector "Semiconductors"`

**`pipeline_health.py`**
- Reads thresholds from `obsidian_config.json` (earnings_stale_days,
  legal_stale_days, min_summary_chars)
- Checks: Atlas YAML schema, Mercury data gaps, Nova staleness,
  Jupiter completeness, pipeline file freshness
- Freshness check now includes: `trade_decisions.json`,
  `jansky_trade_feedback.json`
- Outputs: `pipeline_health.json`, `pipeline_health.md`
- Run before and after Sunday pipeline
- **Not yet updated (Aug 2026) to reflect Nova earnings coverage now
  being intentionally partial** — worth checking whether its Nova
  staleness/completeness check still treats partial earnings coverage
  as a WARN-worthy gap the way it used to; if so, it'll perpetually flag
  something that's now expected behavior, same issue Jansky's SOUL.md
  bullet removal was meant to fix on the Jansky-review side specifically.

**`atlas_yaml_validator.py`**
- Post-write YAML validation for Atlas macro_backdrop.yaml
- Checks required keys, null values, truncation

**`rebuild_dashboard.sh`**
- Inline Python script (heredoc) that assembles dashboard from all fragments
- Tries `fragments/` directory first, then root for backward compat
- Loads: latest research JSON, macro backdrop, mercury fragment,
  jansky fragment, sectors_delta fragment, trades fragment, etf fragment
- Prints fragment sizes and missing fragment warnings
- Usage: `bash rebuild_dashboard.sh`

---

### MCP Server Details

**`mcp/app2.py` (macromcp) — new `get_fed_communications` tool (Aug 2026):**
- Searches FOMC statements, Fed official speeches, and major Fed events
  (Jackson Hole, etc.) via SearXNG — no `engines=` restriction (see
  crypto_news fix below for why that matters)
- Feeds automatically into `weekly_macro.py`'s `raw_text` (no extra
  wiring beyond registering the tool name) — but Atlas's Section 1
  prompt needed an explicit instruction to actually *use* qualitative
  data that doesn't map to the existing numeric-only section structure
- `SEARXNG_URL` defaults to `http://localhost:8080` in code (safe
  default, no env var required)
- Tool count: 16 total

**`mcp/app3.py` (novamcp):**
- **Env var fix (Aug 2026):** `SEARCH_PROVIDER`/`SEARXNG_URL` were not
  set as systemd service environment variables, so the module-level
  default (`SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "ddg")`)
  silently routed all searches through direct DuckDuckGo calls instead
  of SearXNG. Fixed via a systemd drop-in override; see System
  Architecture section above for the exact mechanism.
- `get_earnings_transcript_search`: quarter-aware (last 3 quarters,
  fixed off-by-one Aug 2026 — see nova_earnings_call.py notes), includes
  Motley Fool / Seeking Alpha transcript queries at the search-query
  level, though both domains are filtered out client-side before
  fetching (see nova_earnings_call.py notes — `fetch_url_text` can't
  extract usable content from either site)
- `businesswire.com`, `seekingalpha.com`, and `fool.com` are all excluded
  from client-side candidate filtering as of Aug 2026 (enforced in the
  calling script's `_bad_domains` list, not in app3.py itself)
- **Open issue:** raw HTTP fetch fields (`sec_deep_result`,
  `sec_enforcement_raw`, etc.) still have the pre-Aug-2026 encoding bug
  — not yet patched here (see nova_legal.py notes above)

**`mcp/app4.py` (mercurymcp):**
- `get_etf_data` — price, 1d/1m/3m/1y returns, AUM, expense ratio,
  52-week range, volume, category for all **17** ETFs via yfinance
- **BDI backdrop bug fixed (Aug 2026):** `build_mercury_backdrop()`
  previously called FRED's `DBDA` series directly with no fallback,
  producing `bdi_level: N/A` / `bdi_stance: Unknown` in the structured
  YAML even in the same report run where `get_baltic_dry()` (a separate
  tool) successfully found a real current value via SearXNG. Fixed by
  giving the backdrop builder the same SearXNG-first-then-FRED-fallback
  logic. Note: `bdi_stance` (a computed 4-week trend) will still show
  `Unknown` when the SearXNG path succeeds, since SearXNG only returns a
  single current snapshot, not a historical series to compute a trend
  from — `bdi_level` itself is fixed, trend direction is not (would need
  a proper historical BDI source, not attempted).
- **Crypto backdrop bug fixed (Aug 2026):** same file's crypto stance
  calculation used CoinGecko's `/simple/price` endpoint with
  `usd_7d_change`, which is unreliably populated on the free tier (price
  came through, 7-day change frequently didn't). Switched to
  `/coins/markets` + `price_change_percentage_7d_in_currency`, matching
  the endpoint `get_crypto_prices()` already used successfully.
- **`get_crypto_news` bug fixed (Aug 2026):** was hardcoded to
  `engines=google,bing,duckduckgo` — excluding `braveapi` (the most
  reliable engine after the paid key was added) while including two
  engines (google, duckduckgo) repeatedly observed suspended/blocked.
  Removed the `engines=` restriction entirely so SearXNG uses whatever's
  currently healthy — same fix pattern as the search reliability work in
  `app3.py`/`nova_earnings_call.py`.
- **Same hardcoded-engine bug found and fixed in three more tools (Aug
  2026):** `get_wasde_summary()`, `get_crop_progress()`, and
  `get_noaa_drought()` all had the identical
  `engines=google,bing,duckduckgo` restriction — same copy-pasted
  pattern as `get_crypto_news()` above. All three now let SearXNG use
  its own default (healthy) engine set.
- **`get_energy_prices()` crude inventory bug fixed (Aug 2026):** the
  EIA API query (`petroleum/stoc/wstk/data/`) had no facet filters at
  all, so consecutive rows in the response could be (and were)
  completely different series — confirmed via direct API testing that
  "current minus previous" was silently diffing "Ending Stocks Excluding
  SPR" (`WCESTUS1`, the standard weekly crude figure) against "Stocks in
  Transit from Alaska" (a tiny, unrelated series), producing an
  impossible weekly change (`+186,136 Mbbl`). Fixed by adding
  `facets[duoarea][]=NUS`, `facets[product][]=EPC0`, and
  `facets[process][]=SAX` to pin the query to exactly one series.
- **`get_agricultural_prices()` rewritten (Aug 2026):** previously
  sourced Corn/Soybeans/Wheat/Cotton/Sugar/Coffee/Cocoa/Orange Juice
  entirely from FRED's World Bank Pink Sheet series — monthly data with
  a genuine 4-6+ week publication lag (a `7/1` date on an `8/29` report
  was largely inherent to the source, not a fetch bug). Switched to
  yfinance daily futures as primary
  (`ZC=F`/`ZS=F`/`ZW=F`/`CT=F`/`SB=F`/`KC=F`/`CC=F`/`OJ=F`), with the
  original FRED series kept as fallback if a yfinance fetch fails.
  Units follow standard market convention per exchange: CBOT grains in
  ¢/bu → converted to $/bu; ICE softs (cotton/sugar/coffee/OJ) kept in
  ¢/lb (the way they're actually quoted, not converted to $); cocoa in
  $/mt directly. Confirmed live against the Jetson — all 8 values match
  Mercury's own independently-sourced narrative figures from an earlier
  report almost exactly. Rubber and Lumber remain FRED-only (no
  reliably liquid yfinance futures ticker).
- **`get_livestock_prices()` extended (Aug 2026):** the docstring always
  claimed to track "Live Cattle, Lean Hogs, Feeder Cattle," but the
  implementation only ever fetched BLS *retail* average prices (Beef,
  Pork, Chicken, Eggs, Milk) — different instruments entirely, not
  merely a staler version of the same data. Added the actual CME
  futures (`LE=F`, `HE=F`, `GF=F`) as a new primary section; the BLS
  retail series are kept as a clearly-labeled supplementary section, not
  removed. Confirmed live — matches Mercury's own earlier narrative
  figures closely (Live Cattle 211.73, Feeder Cattle 316.52, Lean Hogs
  81.90 ¢/lb).
- **`get_etf_data()` FBTC/IBIT gap fixed (Aug 2026):** the function's own
  trailing note claimed *"BITQ/FBTC/IBIT crypto ETF data previously in
  get_crypto_prices — now consolidated here"* — but `FBTC` and `IBIT`
  were never actually added to `DEFAULT_ETFS`/`ETF_DESCRIPTIONS`. Mercury
  had zero data (not just flow data — price, AUM, everything) for these
  two tickers despite the comment implying otherwise. Added both; count
  updated from 15 to 17 ETFs. **Required a matching update in three more
  places** to actually take effect end-to-end:
  `obsidian_config.json`'s `mercury_allowed_etfs` (gates what
  `weekly_mercury.py` allows Mercury to *pitch trades* on, separate from
  what `get_etf_data()` fetches) and both hardcoded fallback copies of
  that same list inside `weekly_mercury.py` (one in `load_config()`'s
  defaults, one inline in the trade-pitch function). This ETF list now
  exists in four separate places across the codebase; any future
  addition needs to touch all four or risks the same silent gap — worth
  consolidating to a single source of truth eventually, not attempted here.
- **Deploy-timing note:** during testing, an initial "clean" test run
  turned out to be using an `app4.py` that had the agriculture/livestock
  fixes but predated the FBTC/IBIT fix — a stale file copy, not a code
  bug, but a reminder to confirm the deployed file's content
  (`grep` for a known-new string) rather than assume a restart alone
  guarantees the latest code is running.
- Tool count: 16 total

---

### Mercury ETF Universe

Mercury tracks 17 ETFs in addition to the CCC universe (grew from 15 to
17 in Aug 2026 — see FBTC/IBIT fix above).
ETFs live in `mercury_watchlist.json["etfs"]` and `obsidian_config.json["mercury_allowed_etfs"]`.

```
Current holdings (3):   IAU (gold), BITQ (crypto industry), VDE (energy)
Spot Bitcoin (2):       FBTC, IBIT
Commodities (7):        GLD, SLV, PDBC, DBA, USO, UNG, CPER
Real Estate (3):        VNQ, IYR, XLRE
Fixed Income (2):       TLT, HYG
```

---

### MCP Server Consolidation

All four MCP servers are in `stock_dashboard/mcp/`:

```
mcp/app.py     webmcp     port 8642
mcp/app2.py    macromcp   port 8643
mcp/app3.py    novamcp    port 8644
mcp/app4.py    mercurymcp port 8645
mcp/.env       API keys: FRED, EIA, USDA, CourtListener, LLM config
```

Service files (`~/.config/systemd/user/`):
- `WorkingDirectory=/home/jay/stock_dashboard/mcp`
- `ExecStart=/usr/bin/python3 /home/jay/stock_dashboard/mcp/app{N}.py`
- `Environment=HOME=/home/jay`
- `novamcp.service` additionally requires (via drop-in override as of
  Aug 2026): `Environment=SEARCH_PROVIDER=searxng` and
  `Environment=SEARXNG_URL=http://localhost:8080` — see System
  Architecture section above.

`mcp/.env` contains all required keys including `FRED_API_KEY`.

---

### Key Data Files

```
nova_supplemental.json             128 records: legal + earnings per ticker
macro_backdrop.yaml                Current week Atlas macro context (ASCII clean)
macro_summary.md                   Human-readable Atlas weekly briefing
mercury_latest.json                Current week Mercury CCC + ETF report
mercury_backdrop.yaml              Structured CCC context
mercury_summary.md                 Human-readable Mercury CCC briefing
jansky_latest.json                 Current week Jansky review report
jansky_trade_feedback.json         Per-ticker Jansky trade decisions (2-week rolling)
trade_decisions.json               Current week Jupiter + Mercury trade pitches
pipeline_health.json               Latest pipeline health check
obsidian_config.json               Central pipeline configuration
watchlist.json                     128 tickers across 16 sectors
mercury_watchlist.json             CCC instruments + 17 ETFs
neptune_holdings.json              Current portfolio positions (38 positions, incl. V/Visa)

fragments/                         Dashboard HTML fragments (all agents write here)
  mercury_dashboard_fragment.html
  jansky_dashboard_fragment.html
  sectors_delta_fragment.html
  trades_dashboard_fragment.html
  etf_dashboard_fragment.html

data/research_*.json               Weekly research output (128 ticker records)
data/mercury_*.json                Weekly Mercury CCC + ETF output
data/jansky_*.json                 Weekly Jansky review archives
data/rankings_*.json               Sector rankings + delta data
data/macro_*.json                  Raw FRED macro data

reports/dashboard_*.html           Dated dashboard archives
reports/mercury_fragment_*.html    Dated Mercury tab archives
reports/jansky_fragment_*.html     Dated Jansky tab archives
reports/sectors_delta_fragment_*.html   Dated Sectors+Delta tab archives
reports/macro_summary_*.md         Dated macro briefing archives
reports/trade_decisions_*.json     Archived trade decisions (previous weeks)
```

---

### Nova Supplemental JSON Structure

```json
{
  "TICKER": {
    "ticker": "TICKER",
    "company_name": "Full Name",
    "legal": {
      "risk_level": "Critical|High|Moderate|Low",
      "research_confidence": "High|Medium|Low",
      "research_date": "2026-06-17",
      "nova_status": "current",
      "nova_legal_summary": "...",
      "flag_type": "needs_research|needs_data",
      "flag_detail": "Jupiter's flag rationale"
    },
    "earnings": {
      "call_date": "2026-05-07",
      "fiscal_quarter": "Q1 2026",
      "source_url": "https://...",
      "research_date": "2026-06-22",
      "nova_status": "current",
      "nova_earnings_summary": "..."
    }
  }
}
```

**Staleness check field (important, fixed Aug 2026):** the 85-day
staleness check in `nova_earnings_call.py` compares against
`earnings.call_date` (the actual earnings call date) — **not**
`earnings.research_date` (when Nova last touched the record), which was
the bug. Using `research_date` let a record's genuinely stale earnings
data get permanently "renewed" every time Nova re-confirmed it within
the window, without ever verifying a newer `call_date` existed — a
ticker's data could silently sit a full quarter or more behind while
still reading as "current" indefinitely.

---

### Jansky Report JSON Structure

```json
{
  "run_date": "2026-06-22",
  "generated_at": "2026-06-22T21:18:00",
  "overall_posture": "CONSTRUCTIVE|CAUTIOUS|MIXED|DEFENSIVE",
  "executive_summary": "Full synthesis text (8000+ chars)...",
  "agent_reviews": {
    "atlas":   { "status": "OK|FLAG|CRITICAL", "notes": "..." },
    "mercury": { "status": "OK|FLAG|CRITICAL", "notes": "..." },
    "nova":    { "status": "OK|FLAG|CRITICAL", "notes": "...",
                 "stale_earnings": [], "stale_legal": [],
                 "high_risk_tickers": [], "low_confidence": [] },
    "jupiter": {
      "by_sector": { "Semiconductors": { "status": "...", "notes": "...",
                     "flags": {}, "tickers_needing_attention": [] } },
      "synthesis": "...",
      "verdict_distribution": { "ACCUMULATE": 85, "WATCH": 30, "AVOID": 5 }
    }
  },
  "trade_review": {
    "decisions": [...],
    "approved": [...],
    "rejected": [...]
  },
  "python_flags": {},
  "tickers_needing_attention": ["BA", "DUK", "..."],
  "pass_notes": { "atlas": "...", "mercury": "...", "Semiconductors": "...",
                  "synthesis": "...", "trade_review": "..." }
}
```

Note: `nova.stale_earnings` in the review structure above should no
longer be a driver of Jansky's flagged attention items, per the Aug 2026
SOUL.md change (see `jansky_review.py` notes) — worth confirming this
field is simply left empty/unused now rather than still being populated
and silently ignored.

---

### FRED Series Reference (app2.py — macromcp)

Key series IDs in use:

```
DXY Broad Dollar Index:    DTWEXBGS        (requires FRED_API_KEY)
BoE policy rate:           IR3TIB01GBM156N (replaces retired IRSTCB01GBM156N)
RBA policy rate:           IR3TIB01AUM156N (replaces retired IRSTCB01AUM156N)
SNB policy rate:           IR3TIB01CHM156N (replaces retired IRSTCB01CHM156N)
Orange Juice:              APU0000713111   (replaces retired PORANGEUSDM)
Rubber:                    PRUBBUSDM       (replaces retired PRUBBERNDUSDM)
RBOB Gasoline (fallback):  GASREGCOVW
Heating Oil (fallback):    DHOILNYH
BDI (fallback only, Aug 2026): DBDA — now genuinely a fallback; SearXNG
                            is tried first in build_mercury_backdrop()
                            (see MCP Server Details → app4.py)
```

Note: IR3TIB01 series are BIS-sourced 3-month interbank rates — lag ~1
quarter. BoJ and BoC may show stale values; this is a FRED data lag.

Note: FRED_API_KEY must be in `mcp/.env` (not just `stock_dashboard/.env`).

---

### BDI (Baltic Dry Index)

Baltic Exchange data is commercially paywalled. Pipeline approach:
- **`get_baltic_dry()` tool** (used in Mercury's raw data / prose):
  Primary: SearXNG search with query `"Baltic Dry Index BDI points"`,
  `time_range: week`, result limit 10. Regex extracts numeric value from
  snippets (handles `2,670` and `2670`) with sanity check:
  400 ≤ BDI ≤ 8,000. Fallback: FRED DBDA series.
- **`build_mercury_backdrop()`'s own BDI fetch** (structured YAML) —
  previously called FRED's DBDA directly with no fallback, independent
  of the above tool; fixed Aug 2026 to try the same SearXNG-first
  approach before falling back to FRED. See MCP Server Details → app4.py
  for the full writeup. `bdi_level` is fixed by this; `bdi_stance`
  (trend) still requires the FRED path to succeed, since SearXNG only
  gives a current snapshot.
- If SearXNG is down/empty for any tool: `docker restart searxng`,
  wait ~15s, retest.

---

### Dividend Yield Parsing (app.py — webmcp)

yfinance `dividendYield` field returns a decimal percentage
(e.g. `0.0028` = 0.28%, `0.36` = 0.36%). Do NOT multiply by 100.

```python
dy_pct = raw_dy if raw_dy < 1 else None  # yfinance returns decimal pct
div_yield_str = fmt(dy_pct, suffix="%") if dy_pct is not None else "None"
```

---

### Hermes Agent Configuration

**`--skills hermes-cli` was removed from every agent subprocess call in
August 2026** (10 call sites across `weekly_macro.py`, `weekly_mercury.py`,
`weekly_research.py`, `nova_earnings_call.py`, `nova_legal.py`,
`sector_ranking.py`, `jansky_review.py`). A Hermes version update began
hard-erroring on unrecognized `--skills` values instead of silently
ignoring them, which broke every one of these scripts simultaneously.
Investigation found **`hermes-cli` was never a real, functioning Hermes
skill** — not in the official skills catalog, not in `hermes skills
list`'s output, not anywhere in `~/.hermes` (including Trash), and not
matching the actual `hermes-agent` skill (a meta/dev skill for
contributing to Hermes itself, unrelated in purpose). Best guess: it was
hardcoded into these scripts at some point with the stated intent of
"preventing tool-calling during batch runs," but never actually did
anything — the flag simply no-op'd (silently, in older Hermes versions)
for as long as these scripts have existed. **The actual and only real
defense against unwanted tool-calling has always been the SOUL.md prompt
instructions added for Atlas and Jupiter** — removing the flag changed
nothing about real protection, since it was never contributing to it.
Do not attempt to restore a `--skills` flag on these calls without first
confirming a specific, real skill by that name exists (`hermes skills
list`).

**Per-profile config keys that do NOT reliably take effect** when set
via `hermes --profile <name> config set`:
- `max_tokens` — CLI warns it's "not a recognized config key... bridged
  to the environment for skills/external tools," not read as an actual
  generation parameter
- `agent.reasoning_effort` — CLI warns the same, and suggests
  `agent.reasoning_echo` as an alternative (a different, unrelated
  setting) — the real path doesn't appear to be `agent.reasoning_effort`
  as documented anywhere checked so far

**What was tried, and what's actually confirmed:** the documented global
`--reasoning LEVEL` CLI flag (`none|minimal|low|medium|high|xhigh`) is a
real, per-invocation mechanism — but **`low` was later found to likely
be a silent no-op for DeepSeek V4 Flash specifically**: OpenRouter's own
listing for this model states only `high` and `xhigh` are supported.
The initial fix (`--reasoning low` on Mercury) that appeared to help was
probably coincidental, not causal. Full corrected history — including
why `high` fixed prose but broke strict-JSON calls, and why the
underlying self-narration behavior turned out to be independent of the
reasoning setting entirely — is under Agent Profiles & Models →
Reasoning effort. Don't trust an unverified reasoning-effort level for
a new model without checking what that specific model actually supports
first.

Global config (`~/.hermes/config.yaml`) has `agent.reasoning_effort:
medium` as the default across all profiles — this is a real setting
(unlike the per-profile config-set attempt above), just not one that
was successfully overridden per-profile via `config set`. The
`--reasoning` CLI flag is a per-invocation override that takes
precedence regardless.

---

### Systemd Services (Jetson)

```
webmcp.service          port 8642  mcp/app.py
macromcp.service        port 8643  mcp/app2.py
novamcp.service         port 8644  mcp/app3.py
mercurymcp.service      port 8645  mcp/app4.py
stock-dashboard.service port 8090  python3 -m http.server 8090
```

Service files location: `~/.config/systemd/user/`
All four MCP service files require:
- `WorkingDirectory=/home/jay/stock_dashboard/mcp`
- `ExecStart=/usr/bin/python3 /home/jay/stock_dashboard/mcp/app{N}.py`
- `Environment=HOME=/home/jay`  ← easy to miss on new machine deploys

`novamcp.service` additionally requires (added Aug 2026, via drop-in
override at `~/.config/systemd/user/novamcp.service.d/override.conf`
rather than editing the unit file directly, so it survives regeneration):
```ini
[Service]
Environment=SEARCH_PROVIDER=searxng
Environment=SEARXNG_URL=http://localhost:8080
```

**After deploying a code change to any `app{N}.py`, restart the
corresponding service** (`systemctl --user restart <name>mcp.service`)
before it'll take effect — Python loads the module into memory once at
process start. Give the service a couple seconds to finish starting
before immediately running a script against it (a script's own retry
logic will usually self-heal a `Connection refused` from restarting too
fast, but a short `sleep 2` avoids it happening at all).

---

### Legal Risk Landscape (as of 2026-06-16 — not refreshed this session)

```
CRITICAL (1): HUBG — Nasdaq deficiency + multi-year restatement +
              securities class action + interim CFO $125K/month
              (Aug 2026: also showed a pattern of repeated Form 12b-25
              late-filing notices — 3 filed in 2026 alone, March/May/Aug —
              meaning HUBG genuinely hadn't reported a complete quarter
              since Feb 2025 as of late Aug 2026. NOTE: HUBG has since
              been replaced by OLDF on the active watchlist — see
              Watchlist section above. Retained here as historical
              context; HUBG is no longer actively tracked.)

HIGH (8):     B     — Ontario class action certified, $3-7B exposure
              NEE   — Louisiana coastal litigation + FERC proceedings
              VALE  — Brumadinho criminal charges reinstated April 2026
              CHRW  — SCOTUS 9-0 Montgomery ruling, state tort liability
              BA    — DOJ guilty plea (fraud conspiracy, 737 MAX),
                      criminal exposure, deferred prosecution
              AMZN  — FTC antitrust + Anthropic regulatory (June 2026)
              CRM   — Backpage 300+ plaintiffs, trial Fall 2026
              CB    — Ontario class action certified
              AAPL  — EU DMA investigations (10% global revenue exposure),
                      DOJ antitrust suit, Epic Games criminal contempt referral
```

This section predates the current session and was not independently
re-verified — treat as historical unless refreshed.

---

### Known Issues / Watchlist

- **`nova_legal.py` raw-field encoding (Aug 2026, open)** — see
  `nova_legal.py` and `app3.py` notes above. Data-hygiene severity, not
  correctness-critical (doesn't reach Jupiter's context).
- **`pipeline_health.py` Nova-earnings-coverage expectations (Aug 2026,
  possibly open)** — not yet confirmed whether this still treats partial
  earnings coverage as a gap; worth checking.
- **Buy/hold trade-pitch bias — fixed, not yet validated (Sept 2026)**
  — see `sector_ranking.py`/`weekly_mercury.py` notes above for the full
  fix. Real prompt changes shipped; no live pipeline run has confirmed a
  `REDUCE_POSITION` pitch actually appears yet. Also worth watching for
  overcorrection (too-eager reduce pitches) once it does start firing,
  not just confirming the count is no longer zero.
- **The DeepSeek V4 Flash 0731 self-narration/rewrite behavior is not
  fully resolved (Sept 2026, open)** — see Agent Profiles & Models →
  Reasoning effort for the full history. Current defenses (the
  `_salvage_ticker_entries()` recovery system, explicit anti-hedging
  prompt instructions) mitigate data loss when it happens but don't
  prevent the behavior itself. A single clean run doesn't prove it's
  gone — watch for recurrence, especially in any JSON-output call that
  hasn't yet been hardened with salvage/retry logic.
- **NVIDIA Nemotron trial failure, undiagnosed (Sept 2026, open,
  deprioritized)** — a one-off test of `nvidia/nemotron-3-ultra-550b-a55b:free`
  on Jansky's trade-decisions call produced a fixed 91-character
  response, identical before and after a retry, that never parsed. Raw
  response text was never captured (no debug-file saving on this call,
  and it wasn't preserved in terminal scrollback). Worth adding
  debug-file saving to `jansky_review.py`'s decisions call if this model
  (or any alternative to DeepSeek V4 Flash) is revisited.
- **`_salvage_ticker_entries()` duplicated across two files (Sept
  2026)** — identical implementation now lives in both
  `sector_ranking.py` and `jansky_review.py`, since these are
  independent scripts with no shared library. Minor tech debt: any
  future fix to this function needs to be applied in both places by
  hand. Same pattern already flagged for the ETF ticker list existing
  in four places.
- **Nova earnings coverage** — check current batch results directly
  rather than assuming a fixed rate; coverage varies with SearXNG engine
  health and which tickers are in a given `--stale-only` batch (foreign
  20-F filers and names without a fetchable wire-service release are
  structurally harder). Not itself a bug unless failure patterns look
  systemic (e.g. everything failing identically) rather than the normal
  mix of per-ticker misses.
- **MACD/Bollinger Band/volume** absent from technicals feed — webmcp
  enhancement needed
- **Foreign filer revenue** N/A in SEC XBRL (RIO, BHP, NVO, VALE, AEM)
  — expected limitation. Foreign filers ARE correctly handled for legal
  research (20-F path confirmed working, Aug 2026) — this XBRL-revenue
  gap is separate and still present.
- **RBOB Gasoline / Heating Oil** — EIA v2 route still unreliable; FRED
  fallback active (GASREGCOVW / DHOILNYH). Also seen returning a 404 in
  a Mercury report Aug 2026 — not yet investigated whether this is the
  same known EIA-route issue or something new.
- **BoJ/BoC FRED rates** — BIS-sourced IR3TIB01 series lag ~1 quarter;
  BoJ shows 0.30% (actual differs), BoC shows stale values.
  Not a pipeline error — FRED data lag.
- **GOOG/GOOGL dual-class** — watchlist has GOOG only; GOOGL position in
  holdings is visible to Jupiter via `HOLDINGS_ALIASES`.
- **Mag7 trade pitches** — model occasionally produces `?` ticker in
  rankings JSON (malformed JSON artifact). `repair_sector_ranking.py`
  auto-detects and can repair individual sectors. Likely the same
  self-narration root cause as the broader Sept 2026 JSON corruption
  work — the salvage system should now catch this too, not yet
  specifically re-confirmed against this exact symptom.
- **`Gold/Copper ratio` returning `0.00`** — self-flagged in an Aug 2026
  Mercury report; not yet investigated. (Note: the paired complaint in
  the same report, "Live Cattle NASS quote data error," was
  investigated as part of the livestock futures fix below — it's a
  `400 Bad Request` from the USDA NASS API, confirmed still present in
  the *retail/supplementary* NASS block, separate from and unaffected by
  the new CME-futures section added above it. Not yet fixed.)
- **Metals staleness** — worth a fresh check; the original "agriculture/
  livestock/metals staleness" complaint turned out (on investigation) to
  be genuinely about agriculture and livestock specifically — both now
  resolved (see app4.py notes above). Metals weren't independently
  confirmed stale; `get_metals_prices()` wasn't part of this pass.
- **DXY/rates cross-source date mismatch** — same report, DXY dated one
  week behind the rates data within the same report. Not yet
  investigated.
- **Crypto ETF flow data (FBTC/IBIT net daily inflows/outflows)** —
  deliberately deferred (Aug 2026). `get_etf_data()` now tracks FBTC/IBIT
  price, AUM, and returns (see app4.py notes above), but not flow data —
  yfinance doesn't expose it directly. Would need either a dedicated flow
  data source (Farside Investors / SoSoValue, likely via scraping — no
  clean API) or an AUM-based approximation requiring the pipeline to
  persist its own day-over-day AUM history (not currently done).
- **`mcp_servers`** commented out in `~/.hermes/config.yaml` on Jetson to
  prevent gateway startup warnings

---

### Resolved Issues (September 2026)

- ✅ **Dashboard verdict badge mismatched Jupiter's actual stated
  verdict** — `verdictClass()`/`verdictLabel()` did a naive whole-text
  `.includes('ACCUMULATE')` check that false-positived whenever the
  prose discussed and rejected that verdict (e.g. "...ACCUMULATE is
  wrong here" while the real verdict was WATCH). Fixed by parsing the
  actual "Overall Verdict" line via regex instead of scanning the whole
  document for a bare keyword.
- ✅ **Jupiter self-correction/rewrite spiral corrupting live reports**
  — a real MGM ticker report showed Jupiter deciding mid-generation its
  own draft had errors and rewriting the whole thing, in the process
  drifting real numbers (PEG 0.51→2.49, 2023 EPS $3.19→$3.52),
  fabricating an institutional-ownership data point that didn't exist
  in the source, and splicing a stray Vietnamese word into English
  prose. Root-caused through several incorrect theories (see Agent
  Profiles & Models → Reasoning effort for the full history) — final
  state: `--reasoning high` on prose calls, an explicit single-pass/
  no-self-correction instruction added to Jupiter's synthesis prompt,
  and (separately) a salvage-based recovery layer for the JSON calls
  where the behavior turned out to be more damaging.
- ✅ **`sector_ranking.py`'s JSON calls (ranking + trade pitches)
  frequently failing or corrupting data** — multi-stage fix: removed
  `--reasoning high` from these two calls specifically (confirmed via
  debug capture to cause truncation, unlike the prose calls it helped);
  added explicit anti-hedging prompt instructions; built
  `_salvage_ticker_entries()`, a bracket-depth-aware per-entry JSON
  recovery function that salvages intact ticker entries from an
  otherwise-broken response instead of losing the whole sector; added a
  completeness check to `_validate_ranking()` so a salvaged-but-
  incomplete result triggers a retry rather than silently passing; fixed
  `save_debug()` to overwrite per run instead of accumulating every
  failure since June. Validated clean on the four previously-worst
  sectors (Packaging, Logistics, Mining, Healthcare) — treat as
  "defenses working," not "root cause eliminated."
- ✅ **Jansky's trade-decisions JSON call losing real decisions** — this
  call had no retry logic at all (unlike `sector_ranking.py`'s, even
  before that file's own Sept 2026 hardening); a real run lost 22 of 24
  trade decisions to a parse failure with zero recovery attempt. Ported
  the same `_salvage_ticker_entries()` function and added a retry with a
  stricter re-prompt. A follow-up run then revealed a second, distinct
  failure mode — a syntactically valid but incomplete response (2 of 24
  tickers, only the ones already flagged as problems) that the original
  fix didn't catch since it only triggered on parse failure, not
  incompleteness. Fixed by requiring the full expected count for
  success, retrying on a short result too, and merging (not replacing)
  results across the original and retry attempts.
- ✅ **8 weeks of trade pitches with zero `REDUCE_POSITION`
  recommendations** — root-caused to two concrete prompt issues in both
  `sector_ranking.py` (Jupiter) and `weekly_mercury.py` (Mercury): the
  JSON schema shown to the model only ever included a buy-shaped worked
  example, never a reduce one, and Jupiter's rules additionally
  hardcoded buy-framed language ('"I recommend we buy $X..."') with no
  reduce equivalent. Fixed in both files: added a genuine
  `REDUCE_POSITION` example to each schema, made the phrasing guidance
  action-neutral, added a required instruction to actively review held
  positions for weakness before pitching, and connected cash-floor
  pressure explicitly to the need for reduce candidates. Not yet
  validated against a live pipeline run.
- ✅ **`nova_earnings_call.py` retrying ~66 unfindable tickers every
  week** — added `last_search_attempted`/`last_search_result` tracking
  on every `no_data` outcome and a separate 30-day cooldown
  (`NO_DATA_RETRY_DAYS`) before re-attempting a ticker with no findable
  source, independent of the normal 85-day `call_date` staleness window.
- ✅ **Jansky flagging earnings as urgently stale even when Nova had
  already checked that week** — added a shared
  `_earnings_recently_attempted()` helper, applied at all four places
  Jansky evaluates earnings staleness, that suppresses the flag when a
  genuine recent search attempt exists — regardless of whether it found
  anything new.

---

### Resolved Issues (August 2026)

- ✅ **Encoding/mojibake bug** — `weekly_macro.py`, `weekly_mercury.py`,
  `weekly_research.py` all switched from `r.text` to
  `r.content.decode('utf-8', ...)` for MCP tool responses. Root cause:
  `requests` defaults to Latin-1 decoding for a `text/event-stream`
  response with no explicit `charset=`, corrupting UTF-8 special
  characters (box-drawing lines, em-dashes, etc.) into mojibake.
- ✅ **`novamcp` silently bypassing SearXNG** — missing
  `SEARCH_PROVIDER`/`SEARXNG_URL` systemd env vars caused
  `nova_earnings_call.py`/`nova_legal.py` search to call DuckDuckGo
  directly. Fixed via systemd drop-in override.
- ✅ **Nova earnings staleness check using the wrong field** —
  `earnings_is_current()` compared against `research_date` (when Nova
  last touched a record) instead of `call_date` (the actual earnings
  call). Records could go stale by a full quarter or more while
  permanently reading as "current."
- ✅ **Ticker/watchlist count** — grew from 127 to 128 (V/Visa added to
  Banks); all four agent SOUL.md files updated.
- ✅ **`repair_summaries.py` false-positive on "timed out"** — removed
  from `BROKEN_SIGNATURES`; was flagging healthy summaries that
  correctly discussed a different agent's timeout in their own prose.
- ✅ **`nova_earnings_call.py` search/fetch reliability** — company-name
  quoting, quarter-guess off-by-one, `parse_call_date()` year validation
  + abbreviated-month support, search retry with delay, fetch-failure
  diagnostics, Jina Reader fallback for JS-rendered pages, self-admitted-
  failure detection. See full writeup under Script Reference above.
- ✅ **`--skills hermes-cli` removed** from all 10 subprocess call sites
  across 7 scripts — confirmed the skill never actually existed/worked;
  a Hermes version update that started hard-erroring on unrecognized
  skills (rather than silently ignoring them) was what surfaced this.
- ✅ **Mercury CJK/garbled-text corruption** — root-caused to
  `reasoning_effort: medium` competing with output token budget on long
  synthesis calls; fixed via `--reasoning low` on both `mercury -z`
  subprocess calls. **Correction (Sept 2026):** `low` was later found to
  likely be a silent no-op for this model (only `high`/`xhigh` are
  actually supported per OpenRouter) — the improvement here was probably
  coincidental, not caused by this specific flag value. See Agent
  Profiles & Models → Reasoning effort for the full corrected story.
- ✅ **Mercury backdrop BDI/crypto fields showing N/A/Unknown despite
  real data existing elsewhere in the same report** — both traced to
  `build_mercury_backdrop()` using its own separate, weaker data-fetch
  paths instead of reusing the already-working `get_baltic_dry()` /
  `get_crypto_prices()` logic. Fixed to match.
- ✅ **`get_crypto_news` hardcoded engine list excluding `braveapi`** —
  removed the `engines=` restriction.
- ✅ **Atlas had no way to see Fed speeches/statements/events** — added
  `get_fed_communications()` tool + Section 1 prompt instruction;
  confirmed working (correctly surfaced a Jackson Hole speech in
  testing, and correctly reports "no signal" rather than fabricating
  content when search returns nothing).
- ✅ **`get_energy_prices()` impossible crude inventory figure** — the
  EIA query had no facet filters, so "weekly change" was silently
  diffing two unrelated series (confirmed via live API test: "Ending
  Stocks Excluding SPR" vs. "Stocks in Transit from Alaska"). Fixed by
  adding `duoarea`/`product`/`process` facets to pin the query to
  exactly `WCESTUS1`.
- ✅ **Three more hardcoded `engines=google,bing,duckduckgo` restrictions
  found and removed** — `get_wasde_summary()`, `get_crop_progress()`,
  `get_noaa_drought()`, all excluding `braveapi` the same way
  `get_crypto_news()` did. Same fix pattern applied.
- ✅ **Agricultural/livestock pricing switched from stale monthly FRED
  series to daily yfinance futures** — the "7/1 date on an 8/29 report"
  staleness complaint was largely inherent to the FRED World Bank/BLS
  source's real publication lag, not a fetch bug. Now uses CBOT/ICE/CME
  futures (corn, soybeans, wheat, cotton, sugar, coffee, cocoa, OJ,
  live cattle, lean hogs, feeder cattle) as primary, FRED as fallback.
  All values confirmed live against the Jetson, matching Mercury's own
  independently-sourced narrative figures closely. `get_livestock_prices()`
  also gained real CME futures data it was previously missing entirely
  despite its docstring claiming to track it.
- ✅ **`get_etf_data()` never actually tracking FBTC/IBIT** — a trailing
  comment claimed they were "consolidated here," but they were never
  added to the actual ticker list. Fixed, plus synced the same fix
  across `obsidian_config.json` and two hardcoded fallback copies in
  `weekly_mercury.py` that separately gate what Mercury is allowed to
  pitch trades on. Confirmed live: both tickers now return real price/
  AUM/returns data.

### Resolved Issues (June 22, 2026)

- ✅ **TRADES tab blank** — inline `style="display:none;"` on panel div
  overrides CSS `.ticker-panel.active { display: block }`. Fixed by removing
  inline style from all new fragment panel divs.
- ✅ **Jansky trade decisions all PENDING** — Jansky's APPROVE/REJECT output
  format varies between runs. Fixed by splitting Pass 21 into two calls:
  narrative review + dedicated JSON-only decisions call.
- ✅ **Nova earnings — wrong article selected** — CFO appointments, product
  launches, and wrong-company articles were passing relevance checks. Fixed by:
  (1) Nova URL-picking step before any fetching, (2) date sanitization rejecting
  future/implausible dates.
- ✅ **MU date parsing bug** — URL path `/2026/02/24/` parsed as year 3244.
  Fixed by `_sanitize_sort_date()` capping dates to today and rejecting pre-2020.
  (Note: a related-shape bug recurred Aug 2026 with an article-ID digit
  sequence parsed as year 3338 — see `parse_call_date()` fix above; the
  sanitizer caught the sort-order impact both times, but the underlying
  parse function itself needed the Aug 2026 fix to stop producing garbage
  in the first place.)
- ✅ **AMCR CFO appointment selected over earnings** — CFO press release ranked
  higher due to future date (2026-06-30). Fixed by date sanitization + Nova
  URL-picking.
- ✅ **Atlas Outlook tab — completion note instead of briefing** — Atlas MEMORY.md
  described her as producing two deliverables, causing her to write a status
  note to stdout. Fixed by removing SOUL.md prepend from `generate_summary()`
  and cleaning up the MEMORY.md entry.
- ✅ **SearXNG not auto-starting on reboot** — fixed with
  `docker update --restart unless-stopped searxng`
- ✅ **Hardcoded ticker/sector counts** — all `120`, `128`, `15 sectors`,
  `16 sectors` references replaced with dynamic computation from `watchlist.json`
  throughout the pipeline.
- ✅ **GOOGL position invisible to Jupiter** — fixed via `HOLDINGS_ALIASES`
  in `weekly_research.py`; Jupiter sees combined GOOG + GOOGL Alphabet exposure.
- ✅ **repair_summaries.py** — added `--refetch` flag for full data re-fetch
  when MCP connection dropped mid-run; added bad-data auto-detection
  (stock_info < 250 chars = stub response).

### Resolved Issues (June 17, 2026)

- ✅ **SECTORS + DELTA tabs missing** — fixed via fragment architecture;
  `sector_ranking.py` now writes `sectors_delta_fragment.html`
- ✅ **Dividend yield double-multiplication** — LRCX 28%→0.28%, BYD 87%→0.88%,
  GE 56%→None; fixed in `mcp/app.py`
- ✅ **DXY (DTWEXBGS) 400 error** — FRED_API_KEY added to `mcp/.env`
- ✅ **BoE/RBA/SNB FRED series retired** — replaced with IR3TIB01 series
- ✅ **Orange Juice FRED series retired** — replaced with APU0000713111
- ✅ **Rubber FRED series retired** — replaced with PRUBBUSDM
- ✅ **BDI numeric extraction** — regex now extracts value from SearXNG
  snippets; no more "missing data" errors when SearXNG returns results
- ✅ **MCP servers consolidated** — all four moved to `stock_dashboard/mcp/`
- ✅ **pipeline_health.py Nova staleness** — now uses `research_date` not
  `call_date`; threshold 90d; section 13 regex fixed (`13.` not `## 13.`)
  (Note: this predates and is superseded by the Aug 2026 fix to
  `nova_earnings_call.py`'s OWN staleness check, which uses the opposite
  field mapping for a different reason — see Nova Supplemental JSON
  Structure section above. The two checks measure different things:
  pipeline_health.py asks "how old is the underlying earnings call,"
  nova_earnings_call.py's internal check needed to ask the same thing
  but was accidentally asking "how recently did Nova touch this record"
  instead, which is now fixed to match.)
- ✅ **Nova earnings stale records** — MU, TEAM, AMCR all refreshed
  June 17, 2026

---

### Reporting Structure

```
Jay (Founder & Portfolio Manager)
└── Jansky (Head of AI Operations)
    ├── Jupiter  — Equity Research (128 stocks, 16 sectors)
    │              Pitches equity trades to Jansky weekly
    ├── Mercury  — Currencies, Cryptocurrencies & Commodities + ETFs (15)
    │              Pitches ETF trades to Jansky weekly
    ├── Nova     — Legal & Earnings Intelligence (earnings coverage
    │              partial as of Aug 2026 — treated as nice-to-have,
    │              not required, by both Jupiter and Jansky)
    └── Atlas    — Macroeconomic Advisor (now includes Fed Communications
                   search as of Aug 2026)
```

---

*Have a great week Jay. The signal is always there.*
*— Jansky*
