#!/usr/bin/env python3
"""
Sector Ranking Pass (Pass 2)
Reads the latest research JSON + watchlist.json, groups tickers by sector,
asks Jupiter to rank each sector by conviction (verdict is taken as ground
truth from each ticker's own summary, not re-decided by the ranking call),
then regenerates the dashboard with a Sector Rankings overview tab prepended.

Also runs Jupiter trade pitch pass: for each sector, Jupiter reviews
full holdings context and pitches ADD/NEW/REDUCE trades to Jansky.
Trade pitches are written to trade_decisions.json for Jansky's review.

Run standalone:  python3 ~/stock_dashboard/sector_ranking.py
Auto-called by:  weekly_research.py at end of pipeline
"""

import json
import re
import os
import sys
import glob
import subprocess
import datetime
import shutil

BASE_DIR    = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
DATA_DIR    = f"{BASE_DIR}/data"
REPORT_DIR  = f"{BASE_DIR}/reports"
WATCHLIST   = f"{BASE_DIR}/watchlist.json"
CONFIG_FILE = f"{BASE_DIR}/obsidian_config.json"
NEPTUNE_HOLDINGS   = f"{BASE_DIR}/neptune_holdings.json"
TRADE_DECISIONS    = f"{BASE_DIR}/trade_decisions.json"
TRADE_FEEDBACK     = f"{BASE_DIR}/jansky_trade_feedback.json"

# ─── Verdict Ground Truth ──────────────────────────────────────────────────────
_VERDICT_RE = re.compile(
    r'Overall Verdict\**:?\s*\**\s*(ACCUMULATE|WATCH|AVOID)', re.IGNORECASE
)
_VALID_VERDICTS = {"ACCUMULATE", "WATCH", "AVOID"}

def extract_summary_verdict(summary: str) -> str | None:
    """
    Parse the analyst's own stated Overall Verdict out of a ticker's full
    Jupiter summary (see weekly_research.py generate_summary(), section 13).
    This is ground truth. The ranking pass below assigns rank/score/
    strengths/risks, but must never re-decide ACCUMULATE/WATCH/AVOID
    independently of what the analyst actually concluded.

    Confirmed root cause (Sept 2026, Jansky weekly review): the ranking
    pass previously forced a hardcoded ACCUMULATE/WATCH/AVOID quota via
    prompt instruction, completely independent of what each ticker's own
    summary concluded — producing the mechanically identical 2/4/2
    distribution Jansky flagged across 14 of 16 sectors, and a 48%
    verdict/summary mismatch (28 of those dangerous-direction on held
    or pending-add tickers). This function plus the override logic in
    rank_sector() replace the quota with ground truth from the summary.
    """
    m = _VERDICT_RE.search(summary or "")
    if m:
        v = m.group(1).upper()
        return v if v in _VALID_VERDICTS else None
    return None

# ─── Load Config ──────────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load obsidian_config.json. Returns defaults if missing."""
    defaults = {
        "trade_limits": {"min_trade_dollars": 250000, "max_trade_dollars": 1000000},
        "position_limits": {"max_etf_position_pct": 15.0, "max_equity_position_pct": 10.0},
        "cash_floor": {"min_cash_pct": 10.0},
        "fragment_dir": "fragments",
    }
    if not os.path.exists(CONFIG_FILE):
        return defaults
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return defaults

# ─── Load Watchlist ────────────────────────────────────────────────────────────
def load_sectors() -> dict:
    """Load sector groupings from watchlist.json."""
    if not os.path.exists(WATCHLIST):
        print(f"  ✗ watchlist.json not found at {WATCHLIST}")
        return {}
    with open(WATCHLIST) as f:
        data = json.load(f)
    return data.get("sectors", {})


# ─── Load Latest Research Data ────────────────────────────────────────────────
def load_latest_research(json_path: str = None) -> list:
    """Load the most recent research JSON, or a specific path if provided."""
    if json_path:
        path = json_path
    else:
        files = sorted(glob.glob(f"{DATA_DIR}/research_*.json"))
        if not files:
            print(f"  ✗ No research JSON files found in {DATA_DIR}")
            return []
        path = files[-1]
    print(f"  Loading: {path}")
    with open(path) as f:
        return json.load(f)


# ─── Load Neptune Holdings ─────────────────────────────────────────────────────
def load_holdings() -> dict:
    """Load neptune_holdings.json. Returns empty dict if missing."""
    if not os.path.exists(NEPTUNE_HOLDINGS):
        print(f"  ⚠ neptune_holdings.json not found at {NEPTUNE_HOLDINGS}")
        return {}
    try:
        with open(NEPTUNE_HOLDINGS) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ Could not load neptune_holdings.json: {e}")
        return {}


# ─── Load Jansky Trade Feedback ───────────────────────────────────────────────
def load_trade_feedback() -> dict:
    """Load jansky_trade_feedback.json for injection into trade prompts."""
    if not os.path.exists(TRADE_FEEDBACK):
        return {}
    try:
        with open(TRADE_FEEDBACK) as f:
            return json.load(f)
    except Exception:
        return {}


# ─── Archive Trade Decisions ──────────────────────────────────────────────────
def archive_trade_decisions(stamp: str) -> None:
    """Archive previous trade_decisions.json before overwriting."""
    if os.path.exists(TRADE_DECISIONS):
        archive_path = f"{REPORT_DIR}/trade_decisions_{stamp}.json"
        shutil.copy2(TRADE_DECISIONS, archive_path)
        print(f"  ✓ Previous trade_decisions archived: {archive_path}")


# ─── Build Holdings Context String ────────────────────────────────────────────
def build_holdings_context(holdings: dict, config: dict) -> str:
    """Build a full portfolio holdings block for injection into trade prompts."""
    if not holdings or not holdings.get("positions"):
        return ""
    pos     = holdings["positions"]
    summary = holdings.get("summary", {})
    total_val   = summary.get("total_portfolio_value", 0)
    cash_val    = summary.get("cash_value", 0)
    cash_pct    = summary.get("cash_pct", 0)
    total_gain  = summary.get("total_gain_loss_pct", 0)
    cash_floor  = config.get("cash_floor", {}).get("min_cash_pct", 10.0)
    min_trade   = config.get("trade_limits", {}).get("min_trade_dollars", 250000)
    max_trade   = config.get("trade_limits", {}).get("max_trade_dollars", 1000000)
    max_eq_pct  = config.get("position_limits", {}).get("max_equity_position_pct", 10.0)

    sorted_pos = sorted(pos.items(), key=lambda x: x[1].get("market_value", 0), reverse=True)
    pos_lines = []
    for ticker, p in sorted_pos:
        mv   = p.get("market_value", 0)
        gl   = p.get("gain_loss_pct", 0)
        wt   = (mv / total_val * 100) if total_val else 0
        cost = p.get("avg_cost_per_share", 0)
        shr  = p.get("shares", 0)
        atype = p.get("asset_type", "equity")
        pos_lines.append(
            f"  {ticker:<6} ${mv:>10,.0f}  {wt:>5.1f}%  {gl:>+6.1f}%  "
            f"{shr:>10,.0f} shares @ ${cost:.2f}  [{atype}]"
        )

    pos_text = "\n".join(pos_lines)

    return f"""
═══════════════════════════════════════════════════════════════════════
NEPTUNE PORTFOLIO HOLDINGS (as of {holdings.get('last_updated','unknown')})
═══════════════════════════════════════════════════════════════════════
Total Portfolio: ${total_val:,.0f}
Cash (VMRXX):    ${cash_val:,.0f}  ({cash_pct:.1f}%)  ← Floor: {cash_floor:.0f}%
Total Gain/Loss: {total_gain:+.2f}%

POSITIONS (sorted by market value):
  {'TICKER':<6} {'MKT VALUE':>10}  {'WT%':>5}  {'G/L%':>6}  {'SHARES':>10}  AVG COST  TYPE
  {'─'*75}
{pos_text}

TRADE CONSTRAINTS (Jansky enforces):
  Min trade size:       ${min_trade:,.0f}
  Max trade size:       ${max_trade:,.0f}
  Max single equity:    {max_eq_pct:.0f}% of portfolio
  Cash floor:           {cash_floor:.0f}% minimum
═══════════════════════════════════════════════════════════════════════
"""


def build_brief(res: dict) -> str:
    """Condense a full research result to ~1500 chars for ranking prompt."""
    ticker  = res["ticker"]
    data    = res["data"]
    summary = res.get("summary", "")

    def extract(label, text):
        for line in text.split('\n'):
            if label in line:
                return line.split(':', 1)[-1].strip()
        return "N/A"

    info  = data.get("stock_info", "")
    tech  = data.get("technicals", "")
    fund  = data.get("fundamentals", "")

    # Key metrics
    price    = extract("Current Price", info).replace('$','').replace(',','')
    pe       = extract("P/E Ratio (TTM)", info)
    fpe      = extract("Forward P/E", info)
    peg      = extract("PEG Ratio", info)
    ps       = extract("Price/Sales", info)
    pfcf     = extract("Price/FCF", info)
    target   = extract("Analyst Target", info).replace('$','').replace(',','')
    rec      = extract("Recommendation", info)
    hi52     = extract("52-Week High", info).replace('$','').replace(',','')
    fcf      = extract("Free Cash Flow", info)
    de       = extract("Debt/Equity", info)
    rsi      = extract("RSI (14)", tech)
    ma_sig   = extract("MA Signal", tech)
    vs50     = extract("vs 50-Day MA", tech)
    vs200    = extract("vs 200-Day MA", tech)
    rev_g    = extract("Revenue Growth YoY", fund)
    earn_g   = extract("Earnings Growth YoY", fund)

    try:
        upside   = (float(target) - float(price)) / float(price) * 100
        upside_s = f"{upside:+.1f}%"
    except Exception:
        upside_s = "N/A"

    try:
        drawdown = (float(price) - float(hi52)) / float(hi52) * 100
        dd_s     = f"{drawdown:.1f}%"
    except Exception:
        dd_s = "N/A"

    # Truncate the per-stock AI summary to first 600 chars
    summary_short = summary[:600].rsplit(' ', 1)[0] + "..." if len(summary) > 600 else summary
    stated_verdict = extract_summary_verdict(summary) or "UNSTATED"

    brief = f"""── {ticker} ── [Analyst Verdict: {stated_verdict}]
Price: ${price}  |  P/E: {pe}  |  Fwd P/E: {fpe}  |  PEG: {peg}  |  P/S: {ps}  |  P/FCF: {pfcf}
FCF: {fcf}  |  D/E: {de}  |  Rev Growth: {rev_g}  |  EPS Growth: {earn_g}
52W Drawdown: {dd_s}  |  Analyst Target Upside: {upside_s}  |  Consensus: {rec}
RSI: {rsi}  |  MA Signal: {ma_sig}  |  vs 50D: {vs50}  |  vs 200D: {vs200}
AI Summary: {summary_short}"""

    return brief


# ─── Jupiter Trade Pitch ──────────────────────────────────────────────────────
def pitch_trades_for_sector(sector_name: str, tickers: list[str],
                             briefs: list[tuple], holdings: dict,
                             config: dict, feedback: dict) -> list[dict]:
    """
    Ask Jupiter to pitch trades for this sector based on full holdings context.
    Returns list of trade suggestion dicts.

    Action types:
      ADD_TO_POSITION — ticker already held, compelling case to increase
      NEW_POSITION    — ticker not held, strong entry signal
      REDUCE_POSITION — ticker held, overbought/weakening fundamentals/legal risk
    """
    pos = holdings.get("positions", {}) if holdings else {}
    total_val = holdings.get("summary", {}).get("total_portfolio_value", 0) if holdings else 0
    min_trade = config.get("trade_limits", {}).get("min_trade_dollars", 250000)
    max_trade = config.get("trade_limits", {}).get("max_trade_dollars", 1000000)

    holdings_context = build_holdings_context(holdings, config)
    briefs_text = "\n\n".join(b for _, b in briefs)

    # Build prior feedback block for this sector's tickers
    feedback_lines = []
    for ticker in tickers:
        fb = feedback.get(ticker, {})
        if fb:
            feedback_lines.append(
                f"  {ticker}: Jansky previously {fb.get('decision','?')} "
                f"a {fb.get('action','?')} pitch on {fb.get('date','?')} — "
                f"{fb.get('rationale','')[:120]}"
            )
    feedback_block = (
        "\nJANSKY'S PRIOR TRADE FEEDBACK (last 2 weeks — consider before pitching):\n"
        + "\n".join(feedback_lines)
    ) if feedback_lines else ""

    prompt = f"""You are Jupiter, Obsidian Capital's equity research analyst.
You are reviewing the {sector_name} sector and pitching trade recommendations
to Jansky, Head of AI Operations, who will approve or reject each one.

Write your pitches like a real sell-side analyst making the case to a portfolio
manager. Be specific, cite the data, and make a compelling argument.
Jansky will enforce position limits and cash floor — you just need to make
the best case the data supports.

{holdings_context}{feedback_block}

TRADE PITCH RULES:
- Only pitch trades where conviction is HIGH based on the data below
- Pitch 0-3 trades per sector maximum (quality over quantity)
- Dollar amounts must be between ${min_trade:,.0f} and ${max_trade:,.0f}
- For ADD_TO_POSITION: ticker must already appear in holdings above
- For REDUCE_POSITION: ticker must already appear in holdings above
- For NEW_POSITION: ticker must NOT be in holdings
- Be explicit about the action: e.g. "I recommend we buy $X of TICKER
  because..." for a new or added position, or "I recommend we trim $X
  from our TICKER position because..." for a reduce.
- REQUIRED: before pitching anything, review every ticker in this sector
  that we currently HOLD (see holdings above) for signs it should be
  trimmed — overbought technicals (RSI, extended well above moving
  averages), weakening fundamentals, deteriorating legal/regulatory
  risk, or a thesis that's played out or broken. A REDUCE_POSITION
  pitch is just as valuable as a buy pitch and should be given equal
  weight, not treated as a fallback only considered when nothing else
  looks attractive.
- Cash is a scarce, shared resource across the whole portfolio: if the
  case for a new or added position isn't clearly stronger than the
  case for trimming a weaker held position in this sector, prefer
  proposing the reduce. The portfolio cannot fund every buy pitch
  without selling something first, and there have not been enough
  reduce candidates proposed to do that.
- If no compelling trade exists this week, respond with: NO_TRADES

REQUIRED OUTPUT FORMAT — respond with valid JSON only:
{{
  "sector": "{sector_name}",
  "trades": [
    {{
      "ticker": "XYZ",
      "action": "ADD_TO_POSITION",
      "dollars": 500000,
      "rationale": "Full analyst pitch — 3-5 sentences citing specific data: P/E, earnings trend, technical setup, catalyst. Make the case.",
      "conviction": "HIGH",
      "key_risk": "One sentence on biggest risk to this trade"
    }},
    {{
      "ticker": "ABC",
      "action": "REDUCE_POSITION",
      "dollars": 500000,
      "rationale": "Full analyst pitch for TRIMMING an existing position — 3-5 sentences citing specific data: why it's overbought or the fundamentals/legal picture has weakened, what changed since we bought it, and why now. Make the sell case as rigorously as a buy case.",
      "conviction": "HIGH",
      "key_risk": "One sentence on the biggest risk to trimming this position (e.g. selling too early if the thesis re-accelerates)"
    }}
  ]
}}

If no trades: {{"sector": "{sector_name}", "trades": []}}

CRITICAL: Output ONLY the JSON object above — nothing before it, nothing
after it. Write each field once, correctly, and move directly to the next
one. Do not hedge, re-examine, or "actually, let me reconsider" any value
after writing it, and do not add any stray words, notes, or a second
attempt at a field. A single unexpected token anywhere in this response
will break JSON parsing entirely.

{sector_name.upper()} SECTOR DATA:
{briefs_text}
"""

    print(f"    [trade pitch] asking Jupiter for {sector_name}...", end=" ", flush=True)
    try:
        result = subprocess.run(
            ["jupiter", "-z", prompt],
            capture_output=True, text=True, timeout=420
        )
        output = result.stdout.strip()
        if not output:
            print("✗ (no output)")
            return []

        parsed = _extract_json(output, array_key="trades", sector_name=sector_name)
        if parsed is None:
            print("✗ (JSON parse failed)")
            return []

        trades = parsed.get("trades", [])
        if parsed.get("_salvaged"):
            print(f"⚠ (salvaged {len(trades)} trade(s) — full response failed to parse)")
        else:
            print(f"✓ ({len(trades)} trade(s) pitched)")
        return trades

    except subprocess.TimeoutExpired:
        print("✗ (timeout)")
        return []
    except Exception as e:
        print(f"✗ ({e})")
        return []


# ─── Rank Sector via Hermes ────────────────────────────────────────────────────
def _salvage_ticker_entries(output: str) -> list[dict]:
    """
    Last-resort recovery: find every individual {"ticker": ...} object in
    the raw text and parse each one independently, using bracket-depth
    counting (not a greedy regex) to find each object's true boundaries.
    Used when the response as a whole can't be parsed as one JSON blob —
    confirmed in practice (Sept 2026) that a mid-generation self-narration
    monologue ("Wait — I must correct...") can break the overall JSON
    structure while individual ticker entries elsewhere in the same
    response are still perfectly intact. Discards only the specific
    entries that are themselves corrupted, rather than failing the whole
    sector because one entry (or the narration itself) broke the parse.
    """
    salvaged = []
    seen_tickers = set()
    for m in re.finditer(r'\{\s*"ticker"', output):
        start = m.start()
        depth, end = 0, None
        in_string, escape = False, False
        for i in range(start, len(output)):
            ch = output[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            continue  # unbalanced — likely the truncated/aborted entry
        try:
            obj = json.loads(output[start:end])
        except json.JSONDecodeError:
            continue  # this specific entry is corrupted — skip it, keep others
        ticker = obj.get("ticker")
        if not ticker or ticker in seen_tickers or not isinstance(obj, dict):
            continue
        seen_tickers.add(ticker)
        salvaged.append(obj)
    return salvaged


def _extract_json(output: str, array_key: str = None, sector_name: str = None) -> dict | None:
    """
    Robustly extract JSON from Hermes output regardless of prose wrapping.
    MiniMax M2.7 occasionally prepends/appends conversational text to JSON
    responses even when instructed not to.

    Strategy 1: raw parse — clean JSON, no wrapping
    Strategy 2: regex extract from markdown fences — handles prose preamble
    Strategy 3: first-{ to last-} scan — handles prose with no fence
    Strategy 4 (only if array_key given): salvage individual per-ticker
    objects one at a time — handles a mid-response self-narration
    monologue that breaks the overall structure while individual entries
    remain intact. Returns a dict with "_salvaged": true so callers can
    tell this happened and may want extra scrutiny or a warning.
    """
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', output, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r'(\{.*\})', output, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    if array_key:
        salvaged = _salvage_ticker_entries(output)
        if salvaged:
            return {
                "sector": sector_name,
                "ranking_rationale": "(salvaged — full response could not be parsed as one JSON object)",
                array_key: salvaged,
                "_salvaged": True,
            }

    return None


def _validate_ranking(ranking: dict, expected_count: int = None) -> tuple[bool, str]:
    """
    Validate that a ranking response is complete and usable.
    Returns (is_valid, reason_if_not).

    Checks:
    - Has 'stocks' list with at least one entry
    - Every stock has non-empty 'strengths' and 'risks' arrays
      (M2.7 occasionally returns [] for these even when the rest is correct)
    - If expected_count given, the stocks list has that many entries —
      catches a salvaged ranking that's missing tickers entirely (not
      just empty arrays on a present entry), which the checks above
      wouldn't otherwise notice.
    """
    stocks = ranking.get("stocks", [])
    if not stocks:
        return False, "empty stocks list"
    if expected_count is not None and len(stocks) < expected_count:
        return False, f"only {len(stocks)} of {expected_count} tickers present"
    empty = [
        s.get("ticker", "?") for s in stocks
        if not s.get("strengths") or not s.get("risks")
    ]
    if empty:
        return False, f"empty strengths/risks for: {', '.join(empty)}"
    return True, ""


def rank_sector(sector_name: str, briefs: list[tuple], verdict_map: dict) -> dict:
    """
    Ask MiniMax to rank stocks in a sector by conviction. Verdicts are
    taken from verdict_map (ground truth from each ticker's own summary),
    not decided by this LLM call — see the override block near the end
    of this function.
    briefs: list of (ticker, brief_text) tuples.
    Returns dict keyed by ticker with verdict + rationale.

    Retry logic:
      Attempt 1 — standard prompt
      Attempt 2 — strict JSON-only header (if attempt 1 returns prose wrapper)
      Attempt 3 — explicit strengths/risks reminder (if arrays came back empty)
    """
    n = len(briefs)
    tickers_list = [t for t, _ in briefs]
    briefs_text  = "\n\n".join(b for _, b in briefs)

    def build_prompt(strict_json: bool = False,
                     remind_arrays: bool = False) -> str:
        """
        strict_json:   prepend a hard JSON-only instruction to suppress
                       prose preambles (used on attempt 2)
        remind_arrays: append an explicit reminder that strengths/risks must
                       be populated (used on attempt 3)
        """
        header = (
            "OUTPUT ONLY RAW JSON. NO prose, NO explanation, NO markdown "
            "fences. Your entire response must be a single JSON object "
            "starting with { and ending with }.\n\n"
            if strict_json else ""
        )
        array_reminder = (
            "\nCRITICAL: Every stock object MUST have non-empty 'strengths' "
            "and 'risks' arrays. Do NOT leave them as []. Provide at least "
            "2 bullet points for each field for every stock.\n"
            if remind_arrays else ""
        )
        verdict_lines = "\n".join(
            f"  {t}: {verdict_map.get(t.upper()) or 'UNSTATED — infer conservatively from data'}"
            for t in tickers_list
        )
        return f"""{header}You are a senior portfolio manager at Obsidian Capital evaluating {n} stocks in the {sector_name} sector to identify the single best re-entry opportunity among out-of-favor names.

EACH STOCK'S ANALYST VERDICT HAS ALREADY BEEN DECIDED — DO NOT CHANGE IT:
{verdict_lines}

Your job here is NOT to re-decide ACCUMULATE/WATCH/AVOID. It is to rank
these stocks by conviction, assign a 1-10 score, and write one_liner/
strengths/risks that are consistent with the verdict each stock was
already given above. Copy the given verdict into the "verdict" field
of your output for every stock, exactly as stated.

Tickers to rank: {', '.join(tickers_list)}

Evaluation criteria (in priority order):
1. Magnitude of drawdown from 52W high vs fundamental quality (FCF, revenue growth, margins)
2. Valuation attractiveness (Forward P/E, PEG, P/FCF) relative to sector peers
3. Institutional accumulation or distribution momentum
4. Technical setup — RSI, MA cross, price vs key MAs
5. Analyst upgrade/downgrade momentum and target upside
6. Insider buying conviction (open market purchases > RSU grants)
7. Balance sheet strength (debt/equity, cash)
8. Short interest as squeeze catalyst risk/reward

REQUIRED OUTPUT FORMAT — respond with valid JSON only, no prose before or after:
{{
  "sector": "{sector_name}",
  "ranking_rationale": "2-3 sentence overview of the key differentiators within this sector cohort",
  "stocks": [
    {{
      "ticker": "XXXX",
      "rank": 1,
      "verdict": "ACCUMULATE",
      "score": 8.5,
      "one_liner": "Single sentence why this is the top pick",
      "strengths": ["strength 1", "strength 2"],
      "risks": ["risk 1", "risk 2"]
    }}
  ]
}}

Score each stock 1-10 where 10 = strongest re-entry conviction.
List stocks in rank order (best first).
{array_reminder}

CRITICAL: Output ONLY the JSON object above — nothing before it, nothing
after it. Write each field once, correctly, and move directly to the next
one. Do not hedge, re-examine, or "actually, let me reconsider" any value
after writing it, and do not add any stray words, notes, or a second
attempt at a field. A single unexpected token anywhere in this response
will break JSON parsing entirely.

STOCK DATA:
{briefs_text}
"""

    def call_hermes(prompt: str) -> tuple[str, str]:
        result = subprocess.run(
            ["jupiter", "-z", prompt],
            capture_output=True, text=True, timeout=420
        )
        return result.stdout.strip(), result.stderr.strip()

    def save_debug(label: str, raw: str):
        path = f"{DATA_DIR}/debug_ranking_{sector_name.lower()}.txt"
        # Overwrite per run (not append) — this file previously accumulated
        # every past failure forever, mixing months-old unrelated errors in
        # with the current run's and actively complicating diagnosis.
        mode = 'w' if label == "attempt1-no-json" else 'a'
        with open(path, mode) as f:
            if mode == 'w':
                f.write(f"=== Run: {datetime.date.today().isoformat()} ===\n")
            f.write(f"\n=== {label} ===\n{raw}\n")

    print(f"    [ranking] asking Hermes for {sector_name} ({n} stocks)...",
          end=" ", flush=True)

    output = ""
    try:
        # ── Attempt 1: standard prompt ─────────────────────────────────────
        output, err = call_hermes(build_prompt())
        if not output:
            print(f"✗ (no output: {err[:120]})")
            return {}

        ranking = _extract_json(output, array_key="stocks", sector_name=sector_name)

        # ── Attempt 2: strict JSON-only header (prose wrapper) ─────────────
        if ranking is None:
            print("retry(json)...", end=" ", flush=True)
            save_debug("attempt1-no-json", output)
            output, _ = call_hermes(build_prompt(strict_json=True))
            if output:
                ranking = _extract_json(output, array_key="stocks", sector_name=sector_name)

        if ranking is None:
            save_debug("attempt2-no-json", output)
            print(f"✗ (JSON not found after 2 attempts — see debug file)")
            return {"error": "JSON extraction failed", "raw": output[:500]}

        # ── Attempt 3: populate empty strengths/risks arrays ───────────────
        valid, reason = _validate_ranking(ranking, expected_count=len(briefs))
        if not valid:
            print("retry(arrays)...", end=" ", flush=True)
            save_debug("attempt2-empty-arrays", output)
            output, _ = call_hermes(
                build_prompt(strict_json=True, remind_arrays=True))
            if output:
                ranking2 = _extract_json(output, array_key="stocks", sector_name=sector_name)
                if ranking2 is not None:
                    valid2, _ = _validate_ranking(ranking2, expected_count=len(briefs))
                    if valid2:
                        ranking = ranking2  # use the complete response
                    else:
                        # Keep attempt-2 ranking (verdicts/scores are right)
                        # but patch in whatever arrays attempt-3 produced
                        stocks2 = {s["ticker"]: s
                                   for s in ranking2.get("stocks", [])}
                        for s in ranking.get("stocks", []):
                            t = s.get("ticker")
                            if t in stocks2:
                                if not s.get("strengths"):
                                    s["strengths"] = stocks2[t].get(
                                        "strengths", [])
                                if not s.get("risks"):
                                    s["risks"] = stocks2[t].get("risks", [])

        if ranking.get("_salvaged"):
            n = len(ranking.get("stocks", []))
            print(f"⚠ (salvaged {n} of {len(briefs)} — full response failed to parse)")
        else:
            print("✓")

        # ── Enforce verdict ground truth — never trust the LLM to
        # re-derive ACCUMULATE/WATCH/AVOID on its own; also catches typos
        # like "ACCUMINITE" reaching the dashboard. ─────────────────────
        if ranking and "stocks" in ranking:
            for s in ranking["stocks"]:
                t = s.get("ticker", "").upper()
                true_verdict = verdict_map.get(t)
                llm_verdict  = str(s.get("verdict", "")).upper()
                if true_verdict:
                    if llm_verdict != true_verdict:
                        print(f"      ⚠ {t}: verdict overridden "
                              f"({llm_verdict or '?'} → {true_verdict}, per analyst summary)")
                    s["verdict"] = true_verdict
                elif llm_verdict not in _VALID_VERDICTS:
                    print(f"      ⚠ {t}: invalid verdict '{s.get('verdict')}' — defaulting to WATCH")
                    s["verdict"] = "WATCH"

        return ranking

    except FileNotFoundError:
        print("✗ (hermes not in PATH)")
        return {}
    except subprocess.TimeoutExpired:
        print("✗ (timeout after 300s)")
        return {}
    except Exception as e:
        print(f"✗ ({e})")
        if output:
            save_debug("exception", output)
        return {}


# ─── Delta Tracker ────────────────────────────────────────────────────────────

DELTA_THRESHOLDS = {
    "rsi":           5.0,    # RSI points
    "price_pct":     3.0,    # % price change
    "drawdown":      2.0,    # drawdown % change
    "short_pct":     1.5,    # short interest % points
    "upside":        3.0,    # analyst target upside % change
}

def extract_metric(label: str, text: str) -> str:
    """Pull a value from labelled text output."""
    for line in text.split('\n'):
        if label in line:
            return line.split(':', 1)[-1].strip()
    return "N/A"


def parse_float(s: str) -> float | None:
    """Parse a numeric string, stripping $, %, x, commas."""
    if not s or s == "N/A":
        return None
    try:
        return float(s.replace('$','').replace('%','').replace('x','')
                      .replace(',','').replace('+','').strip())
    except Exception:
        return None


def load_prev_research() -> list:
    """Load the second-most-recent research JSON (last week's data)."""
    files = sorted(glob.glob(f"{DATA_DIR}/research_*.json"))
    if len(files) < 2:
        return []
    with open(files[-2]) as f:
        return json.load(f)


def load_prev_rankings() -> list:
    """Load the second-most-recent rankings JSON."""
    files = sorted(glob.glob(f"{DATA_DIR}/rankings_*.json"))
    if len(files) < 2:
        return []
    with open(files[-2]) as f:
        return json.load(f)


def compute_deltas(curr_results: list, prev_results: list,
                   curr_rankings: list, prev_rankings: list) -> list:
    """
    Compare current vs previous research for each ticker.
    Returns list of delta dicts sorted by significance score.
    """
    prev_by_ticker    = {r["ticker"].upper(): r for r in prev_results}
    curr_by_ticker    = {r["ticker"].upper(): r for r in curr_results}

    # Build verdict maps from rankings
    prev_verdicts = {}
    for ranking in prev_rankings:
        for s in ranking.get("stocks", []):
            if s.get("ticker"):
                prev_verdicts[s["ticker"].upper()] = s.get("verdict", "")

    curr_verdicts = {}
    for ranking in curr_rankings:
        for s in ranking.get("stocks", []):
            if s.get("ticker"):
                curr_verdicts[s["ticker"].upper()] = s.get("verdict", "")

    deltas = []

    for ticker in curr_by_ticker:
        if ticker not in prev_by_ticker:
            continue

        curr = curr_by_ticker[ticker]
        prev = prev_by_ticker[ticker]

        cd = curr["data"]
        pd = prev["data"]

        ci = cd.get("stock_info", "")
        pi = pd.get("stock_info", "")
        ct = cd.get("technicals", "")
        pt = pd.get("technicals", "")
        cs = cd.get("short_interest", "")
        ps = pd.get("short_interest", "")

        # Extract metrics both weeks
        c_price   = parse_float(extract_metric("Current Price", ci))
        p_price   = parse_float(extract_metric("Current Price", pi))
        c_rsi     = parse_float(extract_metric("RSI (14)", ct))
        p_rsi     = parse_float(extract_metric("RSI (14)", pt))
        c_ma      = extract_metric("MA Signal", ct)
        p_ma      = extract_metric("MA Signal", pt)
        c_target  = parse_float(extract_metric("Analyst Target", ci))
        p_target  = parse_float(extract_metric("Analyst Target", pi))
        c_hi52    = parse_float(extract_metric("52-Week High", ci))
        p_hi52    = parse_float(extract_metric("52-Week High", pi))
        c_short   = parse_float(extract_metric("Short % of Float", cs))
        p_short   = parse_float(extract_metric("Short % of Float", ps))

        # Compute changes
        price_chg   = ((c_price - p_price) / p_price * 100
                       if c_price and p_price else None)
        rsi_chg     = (c_rsi - p_rsi if c_rsi and p_rsi else None)
        short_chg   = (c_short - p_short if c_short and p_short else None)

        c_upside = ((c_target - c_price) / c_price * 100
                    if c_target and c_price else None)
        p_upside = ((p_target - p_price) / p_price * 100
                    if p_target and p_price else None)
        upside_chg = (c_upside - p_upside if c_upside and p_upside else None)

        c_dd = ((c_price - c_hi52) / c_hi52 * 100
                if c_price and c_hi52 else None)
        p_dd = ((p_price - p_hi52) / p_hi52 * 100
                if p_price and p_hi52 else None)
        dd_chg = (c_dd - p_dd if c_dd and p_dd else None)

        # MA signal flip
        ma_flip = (c_ma != p_ma and p_ma != "N/A" and c_ma != "N/A")

        # Verdict change
        prev_v = prev_verdicts.get(ticker, "")
        curr_v = curr_verdicts.get(ticker, "")
        verdict_changed = (prev_v and curr_v and prev_v != curr_v)

        # Significance score — how much did this ticker change?
        sig = 0.0
        if verdict_changed:          sig += 10.0
        if ma_flip:                  sig += 6.0
        if rsi_chg and abs(rsi_chg) >= DELTA_THRESHOLDS["rsi"]:
            sig += abs(rsi_chg) / DELTA_THRESHOLDS["rsi"] * 2
        if price_chg and abs(price_chg) >= DELTA_THRESHOLDS["price_pct"]:
            sig += abs(price_chg) / DELTA_THRESHOLDS["price_pct"]
        if short_chg and abs(short_chg) >= DELTA_THRESHOLDS["short_pct"]:
            sig += abs(short_chg) / DELTA_THRESHOLDS["short_pct"] * 1.5
        if upside_chg and abs(upside_chg) >= DELTA_THRESHOLDS["upside"]:
            sig += abs(upside_chg) / DELTA_THRESHOLDS["upside"]

        # Only include tickers with meaningful change
        if sig < 1.0:
            continue

        deltas.append({
            "ticker":          ticker,
            "significance":    round(sig, 1),
            "verdict_prev":    prev_v,
            "verdict_curr":    curr_v,
            "verdict_changed": verdict_changed,
            "price_curr":      c_price,
            "price_chg":       price_chg,
            "rsi_curr":        c_rsi,
            "rsi_prev":        p_rsi,
            "rsi_chg":         rsi_chg,
            "ma_curr":         c_ma,
            "ma_prev":         p_ma,
            "ma_flip":         ma_flip,
            "short_curr":      c_short,
            "short_chg":       short_chg,
            "upside_curr":     c_upside,
            "upside_chg":      upside_chg,
            "drawdown_curr":   c_dd,
            "drawdown_chg":    dd_chg,
        })

    # Sort by significance descending, cap at top 15
    deltas.sort(key=lambda x: x["significance"], reverse=True)
    return deltas[:15]


def generate_delta_summary(deltas: list, curr_date: str, prev_date: str) -> str:
    """Ask MiniMax to write a narrative summary of the most significant changes."""
    if not deltas:
        return "No significant week-over-week changes detected."

    # Build a compact change table for the prompt
    lines = [f"Week-over-week changes ({prev_date} → {curr_date})\n"]
    for d in deltas[:10]:
        t = d["ticker"]
        lines.append(f"{t}:")
        if d["verdict_changed"]:
            lines.append(f"  VERDICT CHANGE: {d['verdict_prev']} → {d['verdict_curr']}")
        if d["ma_flip"]:
            lines.append(f"  MA FLIP: {d['ma_prev']} → {d['ma_curr']}")
        if d["rsi_chg"]:
            lines.append(f"  RSI: {d['rsi_prev']:.1f} → {d['rsi_curr']:.1f} ({d['rsi_chg']:+.1f})")
        if d["price_chg"]:
            lines.append(f"  Price: ${d['price_curr']:.2f} ({d['price_chg']:+.1f}%)")
        if d["short_chg"]:
            lines.append(f"  Short Interest: {d['short_curr']:.1f}% ({d['short_chg']:+.1f}pp)")
        if d["upside_chg"]:
            lines.append(f"  Analyst Upside: {d['upside_curr']:.1f}% ({d['upside_chg']:+.1f}pp)")

    prompt = f"""You are a portfolio analyst writing a brief weekly market update for a watchlist of stocks.

Based on these week-over-week changes, write a concise 3-4 paragraph narrative covering:
1. The most significant developments — what changed most and why it matters
2. Any verdict changes (ACCUMULATE/WATCH/AVOID) and whether they appear warranted
3. Technical developments (MA flips, RSI extremes) worth watching
4. The 2-3 tickers you'd focus on most closely this week and why

Be direct and specific. Reference tickers by name. No generic filler.

CHANGE DATA:
{chr(10).join(lines)}
"""

    print(f"    [delta summary] asking Hermes...", end=" ", flush=True)
    try:
        result = subprocess.run(
            ["jupiter", "-z", prompt, "--reasoning", "high"],
            capture_output=True, text=True, timeout=180
        )
        output = result.stdout.strip()
        if output:
            print("✓")
            return output
        print("✗ (no output)")
        return "Delta summary unavailable."
    except Exception as e:
        print(f"✗ ({e})")
        return "Delta summary unavailable."


def fmt_chg(val, suffix="", invert=False):
    """Format a change value with arrow and color class."""
    if val is None:
        return '<span class="delta-na">—</span>'
    arrow = "▲" if val > 0 else ("▼" if val < 0 else "─")
    # For most metrics, up=good. For short interest/drawdown, up=bad (invert)
    if invert:
        cls = "delta-down" if val > 0 else ("delta-up" if val < 0 else "delta-flat")
    else:
        cls = "delta-up" if val > 0 else ("delta-down" if val < 0 else "delta-flat")
    return f'<span class="{cls}">{arrow}{abs(val):.1f}{suffix}</span>'


def generate_delta_html(deltas: list, delta_narrative: str,
                        curr_date: str, prev_date: str) -> str:
    """Generate the DELTA tab panel HTML."""

    if not deltas:
        return f"""
<div class="delta-empty">
  <div class="delta-empty-title">No Significant Changes</div>
  <div class="delta-empty-sub">Run the pipeline again next week to see week-over-week comparisons.</div>
</div>"""

    # Narrative section
    narrative_html = f"""
<div class="delta-narrative">
  <div class="delta-narrative-label">Weekly Briefing · {prev_date} → {curr_date}</div>
  <div class="delta-narrative-body" id="delta-narrative-body"></div>
</div>"""

    # Change table
    rows = ""
    for d in deltas:
        t   = d["ticker"]
        sig = d["significance"]

        # Verdict change badge
        if d["verdict_changed"]:
            vc_html = (f'<span class="delta-verdict-change">'
                       f'{d["verdict_prev"]} → {d["verdict_curr"]}</span>')
        elif d["verdict_curr"]:
            v = d["verdict_curr"]
            vc = {"ACCUMULATE":"accumulate","WATCH":"watch","AVOID":"avoid"}.get(v,"watch")
            vc_html = f'<span class="verdict verdict-{vc}">{v}</span>'
        else:
            vc_html = "—"

        # MA flip badge
        if d["ma_flip"]:
            ma_html = (f'<span class="delta-ma-flip">'
                       f'{d["ma_curr"].split("(")[0].strip()}</span>')
        else:
            ma_html = d["ma_curr"].split("(")[0].strip() if d["ma_curr"] != "N/A" else "—"

        rows += f"""
        <tr class="{'delta-row-verdict' if d['verdict_changed'] else ''}">
          <td class="delta-ticker">{t}</td>
          <td>{vc_html}</td>
          <td>{fmt_chg(d['price_chg'], '%')}</td>
          <td>{fmt_chg(d['rsi_chg'], '', False)}<span class="delta-curr"> ({d['rsi_curr']:.0f})</span></td>
          <td>{ma_html}</td>
          <td>{fmt_chg(d['short_chg'], 'pp', invert=True)}</td>
          <td>{fmt_chg(d['upside_chg'], 'pp', False)}</td>
          <td>{fmt_chg(d['drawdown_chg'], '%', invert=True)}</td>
          <td class="delta-sig">{sig}</td>
        </tr>"""

    table_html = f"""
<div class="delta-table-wrap">
  <div class="delta-table-title">Most Significant Changes ({prev_date} → {curr_date})</div>
  <table class="delta-table">
    <thead>
      <tr>
        <th>Ticker</th>
        <th>Verdict</th>
        <th>Price Δ</th>
        <th>RSI Δ</th>
        <th>MA Signal</th>
        <th>Short Δ</th>
        <th>Upside Δ</th>
        <th>Drawdown Δ</th>
        <th>Sig.</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""

    # Store narrative text for marked.js rendering
    narrative_escaped = delta_narrative.replace('`', '\\`').replace('${', '\\${')

    narrative_js = f"""
<script id="delta-narrative-src" type="text/plain">{delta_narrative}</script>"""

    return narrative_html + table_html + narrative_js


DELTA_CSS = """
  /* ── Delta Tracker ── */
  .delta-narrative {
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent2);
    border-radius: 6px;
    padding: 24px 28px;
    margin-bottom: 28px;
  }
  .delta-narrative-label {
    color: var(--accent2);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    margin-bottom: 14px;
  }
  .delta-narrative-body {
    font-family: 'Instrument Serif', serif;
    font-size: 16px;
    line-height: 1.8;
    color: #dce8f0;
  }
  .delta-narrative-body h1, .delta-narrative-body h2, .delta-narrative-body h3 {
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--accent2);
    margin: 16px 0 8px;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--border);
  }
  .delta-narrative-body p { margin-bottom: 10px; }
  .delta-narrative-body strong { color: #fff; }
  .delta-table-wrap {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 20px 24px;
    overflow-x: auto;
  }
  .delta-table-title {
    color: var(--accent);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .delta-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .delta-table thead tr {
    border-bottom: 1px solid var(--border);
  }
  .delta-table th {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 8px 12px;
    text-align: left;
    white-space: nowrap;
  }
  .delta-table td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  .delta-table tr:last-child td { border-bottom: none; }
  .delta-table tr:hover td { background: rgba(255,255,255,0.02); }
  .delta-row-verdict td { background: rgba(240,165,0,0.04); }
  .delta-ticker {
    font-weight: 500;
    color: #fff;
    font-size: 15px;
  }
  .delta-up   { color: var(--up); }
  .delta-down { color: var(--down); }
  .delta-flat { color: var(--muted); }
  .delta-na   { color: var(--muted); }
  .delta-curr { color: var(--muted); font-size: 11px; }
  .delta-sig  { color: var(--muted); font-size: 12px; }
  .delta-verdict-change {
    background: rgba(240,165,0,0.15);
    color: var(--accent2);
    padding: 3px 8px;
    border-radius: 3px;
    font-size: 12px;
    white-space: nowrap;
  }
  .delta-ma-flip {
    background: rgba(224,92,92,0.15);
    color: var(--danger);
    padding: 3px 8px;
    border-radius: 3px;
    font-size: 12px;
  }
  .delta-empty {
    text-align: center;
    padding: 80px 40px;
    color: var(--muted);
  }
  .delta-empty-title { font-size: 18px; margin-bottom: 10px; color: var(--text); }
  .delta-empty-sub   { font-size: 13px; }
"""


def write_sectors_delta_fragment(rankings_html: str, delta_html: str,
                                 fragment_path: str):
    """
    Write a self-contained HTML fragment containing both the SECTORS and DELTA
    tab panels plus their nav buttons and all required CSS.

    The fragment is consumed by generate_dashboard() in weekly_research.py
    (injected just before </body>) and by the rebuild_dashboard inline script.

    Nav insertion order (executed at fragment load time, after the base
    dashboard JS has already run):
      • SECTORS inserted at position 0  → becomes the first nav button
      • DELTA   inserted after SECTORS  → becomes the second nav button
    Result: SECTORS | DELTA | OUTLOOK | CCC | JANSKY | [tickers]
    """
    # ── Combine all CSS needed for both tabs ──────────────────────────────────
    rankings_css = """
  /* ── Sector Rankings ── */
  .sector-block {
    margin-bottom: 40px;
  }
  .sector-header {
    font-family: 'Instrument Serif', serif;
    font-size: 22px;
    font-style: italic;
    color: #fff;
    margin-bottom: 6px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .sector-rationale {
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 20px;
    line-height: 1.7;
  }
  .rank-rows {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .rank-row {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px 20px;
    display: grid;
    grid-template-columns: 40px 80px 140px 70px 1fr;
    align-items: start;
    gap: 16px;
  }
  .rank-row.rank-accumulate { border-left: 3px solid var(--accent); }
  .rank-row.rank-watch      { border-left: 3px solid var(--accent2); }
  .rank-row.rank-avoid      { border-left: 3px solid var(--danger); }
  .rank-num {
    color: var(--muted);
    font-size: 20px;
    font-weight: 500;
    padding-top: 2px;
  }
  .rank-ticker {
    font-size: 20px;
    font-weight: 500;
    color: #fff;
    padding-top: 2px;
  }
  .rank-verdict { padding-top: 4px; }
  .score-label {
    color: var(--muted);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .score-val {
    font-size: 16px;
    font-weight: 500;
    color: #fff;
  }
  .rank-oneliner {
    font-family: 'Instrument Serif', serif;
    font-size: 14px;
    color: #dce8f0;
    margin-bottom: 10px;
    line-height: 1.5;
  }
  .rank-bullets {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }
  .bull-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    display: block;
    margin-bottom: 4px;
  }
  .rank-strengths ul, .rank-risks ul {
    padding-left: 14px;
    color: var(--text);
    font-size: 11px;
    line-height: 1.7;
  }
  @media (max-width: 900px) {
    .rank-row { grid-template-columns: 40px 60px 1fr; }
    .rank-score, .rank-verdict { display: none; }
    .rank-bullets { grid-template-columns: 1fr; }
  }"""

    fragment = f"""<!-- ── Sectors + Delta fragment (sectors_delta_fragment.html) ── -->
<style>
{rankings_css}
{DELTA_CSS}
</style>

<div class="ticker-panel" id="panel-SECTORS">
{rankings_html}
</div>

<div class="ticker-panel" id="panel-DELTA">
{delta_html}
</div>

<script>
(function() {{
  const nav  = document.getElementById('nav');
  const main = document.getElementById('main');

  // ── Helper: register a nav button ──────────────────────────────────────
  function makeNavBtn(id, label, color, panelId, onShow) {{
    const btn = document.createElement('button');
    btn.id        = id;
    btn.textContent = label;
    btn.style.color = color;
    btn.onclick = () => {{
      document.querySelectorAll('.ticker-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('nav button').forEach(b => {{
        b.classList.remove('active');
        b.style.borderBottomColor = '';
      }});
      document.getElementById(panelId).classList.add('active');
      btn.classList.add('active');
      btn.style.borderBottomColor = color;
      if (onShow) onShow();
    }};
    return btn;
  }}

  // ── SECTORS button — inserted at position 0 ────────────────────────────
  const sectorsBtn = makeNavBtn(
    'btn-SECTORS', 'SECTORS', 'var(--accent2)', 'panel-SECTORS', null
  );
  nav.insertBefore(sectorsBtn, nav.firstChild);

  // ── DELTA button — inserted immediately after SECTORS ──────────────────
  const deltaBtn = makeNavBtn(
    'btn-DELTA', 'DELTA', 'var(--accent2)', 'panel-DELTA',
    function() {{
      // Render markdown narrative once on first click
      const narEl = document.getElementById('delta-narrative-body');
      const src   = document.getElementById('delta-narrative-src');
      if (narEl && src && !narEl.dataset.rendered) {{
        narEl.innerHTML = marked.parse(src.textContent);
        narEl.dataset.rendered = '1';
      }}
    }}
  );
  // Insert after SECTORS (which is now firstChild)
  nav.insertBefore(deltaBtn, sectorsBtn.nextSibling);

  // Move both panels into <main> so the existing panel show/hide logic
  // can find them (the fragment divs are appended after </main> by the
  // injection, so we relocate them here at runtime).
  const sectorsPanel = document.getElementById('panel-SECTORS');
  const deltaPanel   = document.getElementById('panel-DELTA');
  if (sectorsPanel && sectorsPanel.parentNode !== main) main.appendChild(sectorsPanel);
  if (deltaPanel   && deltaPanel.parentNode   !== main) main.appendChild(deltaPanel);
}})();
</script>
"""
    with open(fragment_path, 'w') as f:
        f.write(fragment)
    print(f"  ✓ Sectors+Delta fragment written: {fragment_path}")


# ─── Generate Rankings HTML ────────────────────────────────────────────────────
def generate_rankings_html(all_rankings: list, run_date: str) -> str:
    """Generate the Sector Rankings overview panel HTML (injected into dashboard)."""

    panels_html = ""

    for ranking in all_rankings:
        if not ranking or "stocks" not in ranking:
            continue

        sector = ranking.get("sector", "Unknown")
        rationale = ranking.get("ranking_rationale", "")
        stocks = ranking.get("stocks", [])

        rows = ""
        for s in stocks:
            verdict  = s.get("verdict", "WATCH").upper()
            ticker   = s.get("ticker", "")
            rank     = s.get("rank", "")
            score    = s.get("score", "")
            one_liner = s.get("one_liner", "")
            strengths = s.get("strengths", [])
            risks     = s.get("risks", [])

            v_class = {"ACCUMULATE": "accumulate", "AVOID": "avoid"}.get(verdict, "watch")

            strengths_html = "".join(f"<li>{x}</li>" for x in strengths)
            risks_html     = "".join(f"<li>{x}</li>" for x in risks)

            rows += f"""
            <div class="rank-row rank-{v_class}">
              <div class="rank-num">#{rank}</div>
              <div class="rank-ticker">{ticker}</div>
              <div class="rank-verdict">
                <span class="verdict verdict-{v_class}">{verdict}</span>
              </div>
              <div class="rank-score">
                <div class="score-label">Score</div>
                <div class="score-val">{score}/10</div>
              </div>
              <div class="rank-detail">
                <div class="rank-oneliner">{one_liner}</div>
                <div class="rank-bullets">
                  <div class="rank-strengths"><span class="bull-label up">▲ Strengths</span><ul>{strengths_html}</ul></div>
                  <div class="rank-risks"><span class="bull-label down">▼ Risks</span><ul>{risks_html}</ul></div>
                </div>
              </div>
            </div>"""

        panels_html += f"""
        <div class="sector-block">
          <div class="sector-header">{sector}</div>
          <div class="sector-rationale">{rationale}</div>
          <div class="rank-rows">{rows}</div>
        </div>"""

    return panels_html


# ─── inject_rankings_into_dashboard — RETIRED ─────────────────────────────────
# Rankings + Delta are now written as sectors_delta_fragment.html by
# write_sectors_delta_fragment() and consumed by generate_dashboard() in
# weekly_research.py.  This function is no longer called.


# ─── Main ──────────────────────────────────────────────────────────────────────
def main(json_path: str = None):
    run_date = datetime.datetime.now().strftime('%Y-%m-%d')
    stamp    = datetime.datetime.now().strftime('%Y%m%d_%H%M')

    print(f"\n{'═'*52}")
    print(f"  Sector Ranking Pass")
    print(f"  {run_date}")
    print(f"{'═'*52}\n")

    # Load config, data, holdings, feedback
    config      = load_config()
    sectors     = load_sectors()
    all_results = load_latest_research(json_path)
    holdings    = load_holdings()
    feedback    = load_trade_feedback()
    frag_dir    = os.path.join(BASE_DIR, config.get("fragment_dir", "fragments"))
    os.makedirs(frag_dir, exist_ok=True)

    if holdings:
        total_val = holdings.get("summary", {}).get("total_portfolio_value", 0)
        n_pos     = len(holdings.get("positions", {}))
        print(f"  ✓ Neptune holdings loaded ({n_pos} positions, ${total_val:,.0f} total)")
    else:
        print(f"  ⚠ No holdings data — trade pitches will lack portfolio context")

    if not sectors:
        print("  ✗ No sectors defined in watchlist.json — skipping ranking pass.")
        return
    if not all_results:
        print("  ✗ No research data found — run weekly_research.py first.")
        return

    # Index results by ticker for quick lookup
    results_by_ticker = {r["ticker"].upper(): r for r in all_results}

    all_rankings   = []
    all_trade_pitches = []   # Accumulates Jupiter trade pitches across all sectors

    for sector_name, tickers in sectors.items():
        print(f"\n  {sector_name}")
        print(f"  {'─'*40}")

        available = [t for t in tickers if t.upper() in results_by_ticker]
        missing   = [t for t in tickers if t.upper() not in results_by_ticker]

        if missing:
            print(f"    ⚠ Missing data for: {', '.join(missing)} — skipping them")

        if len(available) < 2:
            print(f"    ⚠ Need at least 2 tickers with data to rank — skipping {sector_name}")
            continue

        briefs = []
        verdict_map = {}
        for ticker in available:
            res   = results_by_ticker[ticker.upper()]
            brief = build_brief(res)
            briefs.append((ticker, brief))
            verdict_map[ticker.upper()] = extract_summary_verdict(res.get("summary", ""))
            print(f"    ✓ Brief built for {ticker} ({len(brief):,} chars)")

        # ── Pass A: Sector ranking ─────────────────────────────────────────
        ranking = rank_sector(sector_name, briefs, verdict_map)
        if ranking and "stocks" in ranking:
            ranking["sector"] = sector_name
            all_rankings.append(ranking)

            for s in ranking["stocks"]:
                v = s.get("verdict", "?")
                t = s.get("ticker", "?")
                score = s.get("score", "?")
                print(f"    {'🟢' if v=='ACCUMULATE' else '🟡' if v=='WATCH' else '🔴'} "
                      f"{t:6s} {v:10s} score={score}/10")
        else:
            print(f"    ✗ Ranking failed for {sector_name}")

        # ── Pass B: Jupiter trade pitches ──────────────────────────────────
        if holdings:
            trades = pitch_trades_for_sector(
                sector_name, available, briefs, holdings, config, feedback
            )
            if trades:
                for trade in trades:
                    trade["sector"]     = sector_name
                    trade["pitched_by"] = "jupiter"
                    trade["pitched_at"] = datetime.datetime.now().isoformat()
                    trade["run_date"]   = run_date
                all_trade_pitches.extend(trades)

    if not all_rankings:
        print("\n  ✗ No rankings generated — fragment not written.")
        return

    # Save rankings JSON
    rankings_path = f"{DATA_DIR}/rankings_{stamp}.json"
    with open(rankings_path, 'w') as f:
        json.dump(all_rankings, f, indent=2)
    print(f"\n  ✓ Rankings saved: {rankings_path}")

    # ── Write trade_decisions.json (Jupiter section) ───────────────────────
    archive_trade_decisions(stamp)

    # Load existing file to preserve Mercury's section if present
    existing_decisions = {}
    if os.path.exists(TRADE_DECISIONS):
        try:
            with open(TRADE_DECISIONS) as f:
                existing_decisions = json.load(f)
        except Exception:
            existing_decisions = {}

    existing_decisions["jupiter"] = {
        "run_date":   run_date,
        "generated_at": datetime.datetime.now().isoformat(),
        "trade_count": len(all_trade_pitches),
        "trades":     all_trade_pitches,
    }
    existing_decisions["_meta"] = {
        "last_updated": datetime.datetime.now().isoformat(),
        "note": "Jupiter and Mercury pitches reviewed and approved/rejected by Jansky (Pass 21)"
    }

    with open(TRADE_DECISIONS, 'w') as f:
        json.dump(existing_decisions, f, indent=2)
    print(f"  ✓ Jupiter trade pitches written: {TRADE_DECISIONS} ({len(all_trade_pitches)} trades)")

    # ── Delta Tracker ──────────────────────────────────────────────────────────
    print(f"\n  {'─'*40}")
    print(f"  Delta Tracker")
    print(f"  {'─'*40}")

    prev_results  = load_prev_research()
    prev_rankings = load_prev_rankings()

    if not prev_results:
        print("  ⚠ No previous research data found — delta tab will show 'first run' message.")
        delta_html = generate_delta_html([], "", run_date, "first run")
    else:
        # Get date of previous run from filename
        prev_files  = sorted(glob.glob(f"{DATA_DIR}/research_*.json"))
        prev_date   = prev_files[-2].split("research_")[1][:8] if len(prev_files) >= 2 else "prev"
        prev_date_f = f"{prev_date[:4]}-{prev_date[4:6]}-{prev_date[6:8]}"

        print(f"  Comparing: {prev_date_f} → {run_date}")

        deltas = compute_deltas(all_results, prev_results, all_rankings, prev_rankings)
        print(f"  {len(deltas)} tickers with significant changes (of {len(all_results)} total)")

        if deltas:
            for d in deltas[:5]:
                flags = []
                if d["verdict_changed"]:  flags.append(f"VERDICT {d['verdict_prev']}→{d['verdict_curr']}")
                if d["ma_flip"]:          flags.append("MA FLIP")
                if d["rsi_chg"]:          flags.append(f"RSI {d['rsi_chg']:+.1f}")
                if d["price_chg"]:        flags.append(f"PRICE {d['price_chg']:+.1f}%")
                print(f"    {d['ticker']:6s} sig={d['significance']}  {' | '.join(flags)}")

        delta_narrative = generate_delta_summary(deltas, run_date, prev_date_f)
        delta_html      = generate_delta_html(deltas, delta_narrative, run_date, prev_date_f)

    # ── Write combined Sectors + Delta fragment ────────────────────────────────
    rankings_html = generate_rankings_html(all_rankings, run_date)
    fragment_path = os.path.join(frag_dir, "sectors_delta_fragment.html")
    # Keep legacy root path in sync for rebuild_dashboard.sh compatibility
    legacy_path   = f"{BASE_DIR}/sectors_delta_fragment.html"
    write_sectors_delta_fragment(rankings_html, delta_html, fragment_path)
    shutil.copy2(fragment_path, legacy_path)

    # Archive a dated copy
    archive_path = f"{REPORT_DIR}/sectors_delta_fragment_{stamp}.html"
    shutil.copy2(fragment_path, archive_path)
    print(f"  ✓ Fragment archived: {archive_path}")

    print(f"\n  ✓ Sector ranking pass complete")
    print(f"  Run rebuild_dashboard to refresh the HTML dashboard.")
    print(f"{'═'*52}\n")


if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else None
    main(json_path)
