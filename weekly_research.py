#!/usr/bin/env python3
"""
Weekly Stock Research Collector
Calls webmcp tools directly, saves JSON data for dashboard + LLM summary.
Run via cron: 0 8 * * 1 /usr/bin/python3 /home/jay/stock_dashboard/weekly_research.py
"""

import requests
import json
import datetime
import time
import sys
import os
import subprocess

# ─── Configuration ────────────────────────────────────────────────────────────
WEBMCP_URL    = "http://localhost:8642/mcp"
BASE_DIR      = "/home/jay/stock_dashboard"
DATA_DIR      = f"{BASE_DIR}/data"
REPORT_DIR    = f"{BASE_DIR}/reports"
WATCHLIST_JSON  = f"{BASE_DIR}/watchlist.json"
MACRO_BACKDROP  = f"{BASE_DIR}/macro_backdrop.yaml"
MACRO_SUMMARY   = f"{BASE_DIR}/macro_summary.md"
NOVA_JSON       = f"{BASE_DIR}/nova_supplemental.json"
NEPTUNE_HOLDINGS = f"{BASE_DIR}/neptune_holdings.json"
TRADE_FEEDBACK   = f"{BASE_DIR}/jansky_trade_feedback.json"

# ─── Holdings Aliases ──────────────────────────────────────────────────────────
# Maps a watchlist ticker to additional holdings tickers for the same company.
# Used when dual-class shares (e.g. GOOG/GOOGL) are held under a different
# symbol than what appears in watchlist.json. Add new pairs here as needed.
HOLDINGS_ALIASES = {
    "GOOG": ["GOOGL"],
    # "BRK.B": ["BRK.A"],  # example — add as needed
}

def load_watchlist() -> tuple[list, dict]:
    """Load tickers and sector map from watchlist.json.
    Returns (flat_ticker_list, sectors_dict).
    Falls back to hardcoded list if file not found.
    """
    fallback = ["CRM", "ADBE", "NOW", "SNOW", "MDB", "DDOG"]
    if not os.path.exists(WATCHLIST_JSON):
        print(f"  ⚠ watchlist.json not found — using fallback list")
        return fallback, {}
    with open(WATCHLIST_JSON) as f:
        data = json.load(f)
    sectors = data.get("sectors", {})
    flat    = []
    seen    = set()
    for tickers in sectors.values():
        for t in tickers:
            if t.upper() not in seen:
                flat.append(t.upper())
                seen.add(t.upper())
    if not flat:
        print(f"  ⚠ watchlist.json has no tickers — using fallback list")
        return fallback, {}
    return flat, sectors

# ─── webmcp Tool Caller ────────────────────────────────────────────────────────

# Phrases in tool result text that indicate a retryable upstream failure.
# HTTP 500 = EDGAR/data-source transient error
# HTTP 429 = rate limit — back off and retry
# HTTP 503 = upstream service temporarily unavailable
_RETRYABLE_ERRORS = ("HTTP 500", "HTTP 429", "HTTP 503", "rate limit",
                     "temporarily unavailable", "server error")


class _RetryableError(Exception):
    """Raised when a tool result contains a retryable upstream HTTP error."""
    def __init__(self, snippet: str, wait: float):
        self.snippet = snippet
        self.wait    = wait
        super().__init__(snippet)


def call_tool(tool_name: str, arguments: dict,
              retries: int = 3, backoff: float = 5.0) -> str:
    """
    Call a webmcp tool via StreamableHTTP MCP protocol.

    Retries on both Python exceptions (network errors, timeouts) AND
    HTTP-level failures returned inside the result text (500, 429, 503).
    Uses exponential backoff: waits backoff * 2^attempt seconds between
    retries so EDGAR transient load spikes have time to clear.

    retries:  max retry attempts after the first try (default 3)
    backoff:  base wait seconds (default 5 -> 5s, 10s, 20s)
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }

    last_result = "No data returned"

    for attempt in range(retries + 1):
        session = requests.Session()
        try:
            # Initialize MCP session
            init_r = session.post(WEBMCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "weekly_research", "version": "1.0"}
                }
            }, timeout=30)

            sid = init_r.headers.get("mcp-session-id", "")
            if sid:
                headers["mcp-session-id"] = sid

            # Notify initialized
            session.post(WEBMCP_URL, headers=headers, json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {}
            }, timeout=10)

            # Call the tool
            r = session.post(WEBMCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}
            }, timeout=90)

            # Parse SSE or JSON response
            # NOTE: r.text is deliberately NOT used here. requests falls back to
            # ISO-8859-1 when a text/* content-type (e.g. text/event-stream) has
            # no explicit charset= parameter, which mojibakes any UTF-8 multi-byte
            # character (─, —, ↑, ⚠, etc.) into 2-3 wrong characters each.
            # Decoding r.content explicitly avoids that guesswork.
            text = r.content.decode('utf-8', errors='replace').strip()
            result = ""
            for line in text.split('\n'):
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                        for item in data.get("result", {}).get("content", []):
                            if item.get("type") == "text":
                                result += item.get("text", "")
                    except Exception:
                        continue
            if not result:
                try:
                    data = json.loads(r.content.decode('utf-8', errors='replace'))
                    for item in data.get("result", {}).get("content", []):
                        if item.get("type") == "text":
                            result += item.get("text", "")
                except Exception:
                    result = text

            result = result or "No data returned"

            # Check for HTTP-level errors embedded in the result text.
            # These come back as HTTP 200 from webmcp but carry upstream
            # failure messages from EDGAR or data providers.
            result_upper = result.upper()
            retryable = any(e.upper() in result_upper
                            for e in _RETRYABLE_ERRORS)
            if retryable and attempt < retries:
                last_result = result
                wait = backoff * (2 ** attempt)
                raise _RetryableError(result[:80], wait)

            return result

        except _RetryableError:
            raise   # bubble up to research_ticker for console output + sleep

        except Exception as e:
            last_result = f"ERROR: {str(e)}"
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            return last_result

    return last_result


# ─── Per-ticker Research ───────────────────────────────────────────────────────
def research_ticker(ticker: str) -> dict:
    """Collect all data for one ticker. Returns structured dict."""
    print(f"  {chr(9472)*40}")
    print(f"  {ticker}")
    print(f"  {chr(9472)*40}")

    result = {
        "ticker": ticker,
        "collected_at": datetime.datetime.now().isoformat(),
        "data": {}
    }

    tasks = [
        ("stock_info",        "get_stock_info",        {"ticker": ticker}),
        ("sec_earnings",      "get_sec_earnings",      {"ticker": ticker}),
        ("analyst_ratings",   "get_analyst_ratings",   {"ticker": ticker}),
        ("stock_news",        "get_stock_news",        {"ticker": ticker, "max_results": 5}),
        ("short_interest",    "get_short_interest",    {"ticker": ticker}),
        ("insider_trades",    "get_insider_trades",    {"ticker": ticker, "days": 90}),
        ("institutional",     "get_institutional",     {"ticker": ticker}),
        ("fundamentals",      "get_fundamentals",      {"ticker": ticker}),
        ("technicals",        "get_technicals",        {"ticker": ticker}),
        ("prices_1y",         "get_stock_prices",      {"ticker": ticker, "period": "1y"}),
        ("prices_6mo",        "get_stock_prices",      {"ticker": ticker, "period": "6mo"}),
        ("prices_5y",         "get_stock_prices",      {"ticker": ticker, "period": "5y"}),
        ("legal_proceedings", "get_legal_proceedings", {"ticker": ticker}),
    ]

    for key, tool, args in tasks:
        print(f"    [{key}]...", end=" ", flush=True)

        data = None
        # Outer retry loop handles _RetryableError (HTTP 500/429/503).
        # call_tool manages its own attempt counter internally; we re-call
        # it here only to surface the wait message in the console and sleep.
        MAX_OUTER = 3
        for outer in range(MAX_OUTER + 1):
            try:
                data = call_tool(tool, args)
                break
            except _RetryableError as e:
                if outer < MAX_OUTER:
                    print(f"\u21ba HTTP error ({e.snippet[:40].strip()}), "
                          f"waiting {e.wait:.0f}s...", end=" ", flush=True)
                    time.sleep(e.wait)
                else:
                    data = f"ERROR: max retries exceeded ({e.snippet[:60]})"
                    break

        if data is None:
            data = f"ERROR: max retries exceeded for {tool}"

        result["data"][key] = data
        status = "\u2713" if not data.startswith("ERROR") else "\u2717"
        print(f"{status} ({len(data):,} chars)")
        time.sleep(1)

    return result

# ─── Price History Parser ──────────────────────────────────────────────────────
def parse_prices(raw: str) -> list:
    """Extract date/price pairs from get_stock_prices output."""
    prices = []
    for line in raw.split('\n'):
        line = line.strip()
        # Format: "2025-11-10: $240.72"
        if line and ':' in line and '$' in line:
            try:
                parts = line.split(':')
                date = parts[0].strip()
                price = float(parts[1].replace('$', '').strip())
                if len(date) == 10 and date[4] == '-':  # YYYY-MM-DD
                    prices.append({"date": date, "price": price})
            except Exception:
                continue
    return prices

# ─── Atlas Macro Backdrop ─────────────────────────────────────────────────────
def load_macro_backdrop() -> tuple[str, str]:
    """
    Load the Atlas macro backdrop YAML and summary prose.
    Returns (yaml_text, summary_prose) — both empty strings if files missing.
    Silently skips if weekly_macro.py has not been run yet.
    """
    yaml_text    = ""
    summary_text = ""

    if os.path.exists(MACRO_BACKDROP):
        try:
            with open(MACRO_BACKDROP, "r") as f:
                yaml_text = f.read().strip()
        except Exception:
            pass

    if os.path.exists(MACRO_SUMMARY):
        try:
            with open(MACRO_SUMMARY, "r") as f:
                summary_text = f.read().strip()
        except Exception:
            pass

    return yaml_text, summary_text

# ─── Add Nova Supplemental Research ──────────────────────────────────
def load_nova_supplemental() -> dict:
    """
    Load nova_supplemental.json. Returns empty dict if absent or unreadable.
    Called once in main() and passed to generate_summary() for each ticker.
    """
    nova_json = os.path.join(
        os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard"),
        "nova_supplemental.json"
    )
    if not os.path.exists(nova_json):
        return {}
    try:
        with open(nova_json, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _nova_is_current(rec: dict, section: str, stale_days: int = 90) -> bool:
    """Return True if a nova record section (legal/earnings) is within stale_days."""
    sub = rec.get(section, {})
    rd  = sub.get("research_date", "")
    if not rd:
        return False
    try:
        d = datetime.datetime.strptime(rd, "%Y-%m-%d").date()
        return (datetime.date.today() - d).days <= stale_days
    except Exception:
        return False


def _build_nova_preamble(ticker: str, nova_data: dict) -> str:
    """
    Build the NOVA SUPPLEMENTAL CONTEXT block for injection into the prompt.
    Returns empty string if no relevant Nova data exists for this ticker.
    """
    rec = nova_data.get(ticker.upper(), {})
    if not rec:
        return ""

    sections = []

    # ── Legal section ──────────────────────────────────────────────────────
    legal = rec.get("legal", {})
    if legal and _nova_is_current(rec, "legal"):
        flag_type   = legal.get("flag_type", "")
        summary     = legal.get("nova_legal_summary", "")
        risk        = legal.get("risk_level", "Unknown")
        confidence  = legal.get("research_confidence", "Unknown")
        rd          = legal.get("research_date", "unknown date")

        if flag_type == "needs_data":
            intro = (
                f"This company was previously flagged for INSUFFICIENT LEGAL DATA "
                f"in its SEC 10-K filing. Nova, our legal AI assistant, has "
                f"conducted a deep dive and provided the following findings:"
            )
        elif flag_type == "needs_research":
            intro = (
                f"This company was previously flagged by Jupiter for SERIOUS LEGAL "
                f"EXPOSURE requiring additional research. Nova has conducted a "
                f"comprehensive legal investigation and provides the following:"
            )
        else:
            intro = (
                f"Nova has previously researched the legal profile of this company "
                f"and provides the following summary:"
            )

        if summary:
            legal_block = (
                f"⚖ NOVA LEGAL RESEARCH  (researched {rd})\n"
                f"{intro}\n\n"
                f"{summary}\n\n"
                f"Nova Risk Assessment: {risk}  |  "
                f"Research Confidence: {confidence}"
            )
            sections.append(legal_block)

    elif legal and not _nova_is_current(rec, "legal"):
        rd = legal.get("research_date", "unknown date")
        sections.append(
            f"⚖ NOVA LEGAL RESEARCH  (researched {rd} — DATA IS NOW STALE)\n"
            f"Nova previously researched this company's legal profile. "
            f"The data is over 90 days old and has been queued for refresh. "
            f"Prior risk level: {legal.get('risk_level', 'Unknown')}. "
            f"Weight this information accordingly."
        )

    # ── Earnings section ───────────────────────────────────────────────────
    earnings = rec.get("earnings", {})
    if earnings and _nova_is_current(rec, "earnings"):
        call_date    = earnings.get("call_date", "N/A")
        fiscal_q     = earnings.get("fiscal_quarter", "Most Recent Quarter")
        source       = earnings.get("source", "")
        summary_text = earnings.get("nova_earnings_summary", "")
        rd           = earnings.get("research_date", "unknown date")

        if summary_text:
            source_note = f"Quartr" if source == "quartr" else "web transcript"
            earnings_block = (
                f"📞 NOVA EARNINGS CALL SUMMARY  "
                f"({fiscal_q}, call date {call_date}, source: {source_note})\n"
                f"{summary_text}"
            )
            sections.append(earnings_block)

    if not sections:
        return ""

    divider = "─" * 69
    preamble = (
        f"\n{divider}\n"
        f"NOVA SUPPLEMENTAL RESEARCH "
        f"(provided by Nova — Obsidian Capital Intelligence Agent)\n"
        f"{divider}\n\n"
        + "\n\n".join(sections)
        + f"\n\n{divider}\n"
        f"END OF NOVA CONTEXT — BEGIN STANDARD ANALYSIS BELOW\n"
        f"{divider}\n\n"
    )
    return preamble

# ─── LLM Summary via Hermes CLI ───────────────────────────────────────────────
def generate_summary(ticker: str, data: dict,
                     macro_backdrop: str = "",
                     nova_data: dict = {},
                     holdings_data: dict = {},
                     sectors: dict = {},
                     trade_feedback: dict = {}) -> str:
    """Ask Hermes to summarize the research data via oneshot CLI (-z)."""
    summary_data = {
        k: v for k, v in data.items()
        if k not in ("prices_1y", "prices_6mo", "prices_5y")
    }

    # ── Dynamic watchlist counts (avoids hardcoding ticker/sector numbers) ──
    if sectors:
        n_sectors = len(sectors)
        n_tickers = sum(len(v) for v in sectors.values())
        sector_list_text = "\n".join(
            f'    "{s}": {json.dumps(t)},' for s, t in sectors.items()
        )
    else:
        # Fallback if sectors not passed — generic language
        n_sectors = 0
        n_tickers = 0
        sector_list_text = "    (sector list unavailable)"

    # ── Atlas macro preamble (injected when macro_backdrop.yaml is present) ──
    macro_preamble = ""
    if macro_backdrop:
        macro_preamble = f"""MACROECONOMIC CONTEXT (provided by Atlas — Obsidian Capital Economics Advisor):
The following structured economic backdrop was generated this week by Atlas.
Use it to contextualize your stock analysis — weight the macro environment
appropriately when assessing valuation, growth outlook, and overall verdict.

{macro_backdrop}

─────────────────────────────────────────────────────────────────────────────
END OF MACRO CONTEXT — BEGIN STOCK ANALYSIS BELOW
─────────────────────────────────────────────────────────────────────────────

"""

    # ── Nova supplemental preamble ─────────────────────────────────────────
    nova_preamble = _build_nova_preamble(ticker, nova_data)

    # ── Neptune portfolio holdings preamble ────────────────────────────────
    holdings_preamble = ""
    if holdings_data and holdings_data.get("positions"):
        pos = holdings_data["positions"]
        ticker_upper = ticker.upper()

        # Collect this ticker plus any alias tickers (e.g. GOOG → also GOOGL)
        tickers_to_check = [ticker_upper] + HOLDINGS_ALIASES.get(ticker_upper, [])
        matched_positions = [(t, pos[t]) for t in tickers_to_check if t in pos]

        if matched_positions:
            if len(matched_positions) == 1:
                # Single position — standard note
                t, p = matched_positions[0]
                mv        = p.get("market_value", 0)
                gl        = p.get("gain_loss_pct", 0)
                shares    = p.get("shares", 0)
                avg_cost  = p.get("avg_cost_per_share", 0)
                cost_basis= p.get("cost_basis", 0)
                holdings_preamble = (
                    f"\nPORTFOLIO HOLDINGS NOTE (Neptune Paper Trading):\n"
                    f"The firm holds {shares:,.0f} shares of {t} "
                    f"at an average cost of ${avg_cost:.2f} (cost basis ${cost_basis:,.0f}).\n"
                    f"Current market value: ${mv:,.0f} ({gl:+.1f}% return).\n"
                    f"Use this context when assessing conviction — this is a live position.\n\n"
                )
            else:
                # Multiple share classes — combined note
                total_mv     = sum(p.get("market_value", 0)  for _, p in matched_positions)
                total_cost   = sum(p.get("cost_basis", 0)    for _, p in matched_positions)
                total_gl     = total_mv - total_cost
                total_gl_pct = (total_gl / total_cost * 100) if total_cost else 0
                lines = []
                for t, p in matched_positions:
                    lines.append(
                        f"  {t}: {p.get('shares', 0):,.0f} shares @ "
                        f"${p.get('avg_cost_per_share', 0):.2f} avg cost "
                        f"(mkt value ${p.get('market_value', 0):,.0f}, "
                        f"{p.get('gain_loss_pct', 0):+.1f}%)"
                    )
                holdings_preamble = (
                    f"\nPORTFOLIO HOLDINGS NOTE (Neptune Paper Trading):\n"
                    f"The firm holds multiple share classes of this company:\n"
                    + "\n".join(lines) + "\n"
                    f"Combined exposure: ${total_mv:,.0f} total market value "
                    f"(cost basis ${total_cost:,.0f}, {total_gl_pct:+.1f}% return).\n"
                    f"Use this context when assessing conviction — these are live positions.\n\n"
                )

    # ── Jansky trade feedback preamble ────────────────────────────────────
    feedback_preamble = ""
    if trade_feedback:
        fb = trade_feedback.get(ticker.upper(), {})
        if fb:
            decision = fb.get("decision", "?")
            action   = fb.get("action", "?").replace("_", " ")
            dollars  = fb.get("dollars", 0)
            rat      = fb.get("rationale", "")
            date     = fb.get("date", "unknown date")
            feedback_preamble = (
                f"\nJANSKY TRADE FEEDBACK (from {date}):\n"
                f"Jansky {decision}D a pitch to {action} ${dollars:,.0f} of {ticker.upper()}.\n"
                f"Jansky's rationale: {rat}\n"
                f"Use this context when forming your analysis — if APPROVED, note what "
                f"strengthened the thesis; if REJECTED, address the concern raised.\n\n"
            )

    prompt = f"""{macro_preamble}{nova_preamble}{holdings_preamble}{feedback_preamble}You are a financial analyst at Obsidian Capital providing coverage for
{n_tickers} stocks organized into {n_sectors} sectors. Look to identify out-of-favor stocks with re-entry 
potential and when companies appear to be overbought, warranting a position reduction.
Here are the companies and sectors you follow:
 
{sector_list_text}
    
    Note: Coverage initiation for all tickers was 5/24/2026, Except CSCO, V, AAPL, BBY, MSFT, GOOG,
          TSLA, META which begin Jupiter coverage on 6/21/2026

Analyze the data provided for {ticker} and structure your response exactly as follows.

FORMATTING — follow this exactly, every time, with no exceptions:
- Use a markdown header exactly in this form for each section: "## N. SECTION NAME"
  (## symbol, section number, period, section name in capitals) — identical style
  for all 13 sections, every report, every ticker.
- Present every data point / metric as a vertical bulleted list, one bullet per
  line, using "-". Never condense bullets into a single sentence or a
  comma-separated inline list.
- Use plain prose ONLY for the narrative/interpretive sentences each section
  calls for (Company Overview, the Legal Risk narrative before its flag line,
  News implications, Re-entry reasoning, Overall Verdict rationale) — never
  merge those with the bulleted data above them.
- Output the COMPLETE report in a SINGLE PASS, start to finish, once. Do not
  critique, restate, second-guess, or rewrite your own response after writing
  it — there is no "corrected final version" step. If you notice a mistake
  while writing, silently correct that one value and continue forward; never
  stop to announce a correction or produce a second draft. Every number you
  write should be read directly from the DATA below, not recalled or
  re-derived from earlier in your own response.

## 1. COMPANY OVERVIEW (2-3 sentences)
   Full name, business model, key products/services, market position, why it is
   out of favor or why it is overbought. It may not fit either description.

## 2. VALUATION
   - Current price vs analyst mean/median/high/low targets
   - Upside % to mean target
   - Drawdown from 52-week high
   - P/E TTM and Forward P/E
   - PEG ratio if available

## 3. EARNINGS & REVENUE TREND
   - Revenue trajectory (accelerating/decelerating/declining)
   - EPS trend (expanding/contracting margins)
   - Most recent quarter beat/miss vs estimate and surprise %
   - Annual revenue and EPS growth rate

## 4. ANALYST SENTIMENT
   - Consensus rating and score (1.0=Strong Buy → 5.0=Strong Sell)
   - Buy/Hold/Sell breakdown
   - Recent upgrade/downgrade momentum (last 60 days)
   - Bullish/Neutral/Bearish signal

## 5. TECHNICAL SIGNAL
   - RSI(14) — overbought/oversold/neutral
   - MA Signal — Golden Cross / Death Cross / Neutral
   - Price vs 50D and 200D MA

## 6. SHORT INTEREST
   - Short % of float (low <5% / moderate 5-15% / high >15%)
   - Days to cover
   - Month-over-month change direction
   - Short squeeze potential

## 7. INSIDER ACTIVITY
   - Open market buys/sells (most significant signal)
   - Net insider sentiment: Bullish / Neutral / Bearish
   - Highlight and flag large sales from C-Level executives

## 8. INSTITUTIONAL OWNERSHIP
   - Percent owned by Institutions vs. Public
   - Net institutional sentiment: Bullish / Neutral / Bearish
   - Top institutional holders (Note: BlackRock, Vanguard and State Street are likely indexes)
   - Big moves up or down

## 9. LEGAL & REGULATORY RISK
   - Active material lawsuits or regulatory actions
   - Risk level: Low / Moderate / High / Critical
   - If the SEC EDGAR 10-K legal tool fails to extract any data or provides
     insufficient data to form a legal opinion, you MUST flag the company.
   - If Nova has provided legal research above, use it as the primary source
     for this section. Do NOT re-flag a company that Nova has already researched.
   - At the END of this section, on its own line, output a structured flag:
     NOVA_FLAG_DATA: {{"needs_data": true/false, "needs_research": true/false, "flag_detail": "brief reason or empty string"}}
     Set needs_data=true if 10-K extraction failed or returned only boilerplate.
     Set needs_research=true if legal exposure is serious and warrants deeper research.
     Set both to false if legal picture is clear and low-risk.
     If Nova data was injected above for this ticker, set both to false.

## 10. NEWS & CATALYSTS
   - Top 2-3 recent news themes
   - Bullish or bearish implication for each
   - News section may contain off-topic articles from Yahoo Finance —
     only reference news that mentions {ticker} or the company name

## 11. DATA GAPS
   - List any requested metrics not available in the provided data

## 12. RE-ENTRY SIGNAL
   - Is the selloff fundamentally justified or sentiment-driven?
   - Entry risk level: Low / Moderate / High
   - For growth/momentum stocks look for growth at a reasonable price.

## 13. OVERALL VERDICT: WATCH / ACCUMULATE / AVOID
    2-3 sentence rationale focused on risk/reward asymmetry.
    Include a one-line stop-loss thesis. Be stingy with the ACCUMULATE
    verdict. Companies with bearish momentum and weak earnings or revenues,
    or have huge litigation risk, should be rated "AVOID". Good companies 
    that are seeing revenue and earnings weakness because of Macro economic
    or political issues, but otherwise offer a lot of upside should be rated WATCH.

IMPORTANT RULES:
- Base analysis ONLY on data provided
- Flag ALL missing data explicitly in section 11
- Never invent metrics not present in the data
- Legal risk is a first-class consideration
- Insider open market trades outweigh analyst ratings
- NEWS WARNING: Yahoo Finance sometimes returns off-topic articles.
  Only reference news items that clearly relate to {ticker}.
  Ignore any news about other companies entirely.

DATA:
{json.dumps(summary_data, indent=2)[:85000]}
"""

    try:
        result = subprocess.run(
            ["jupiter", "-z", prompt, "--reasoning", "high"],   # Jupiter — default Hermes profile
            capture_output=True, text=True, timeout=420
        )
        output = result.stdout.strip()
        if output:
            return output
        err = result.stderr.strip()
        return f"Summary unavailable (Jupiter exited {result.returncode}: {err[:200]})" if err \
               else f"Summary unavailable (Jupiter exited {result.returncode} with no output)"
    except FileNotFoundError:
        return "Summary unavailable (hermes not found in PATH — check cron environment)"
    except subprocess.TimeoutExpired:
        return "Summary unavailable (Jupiter/hermes timed out after 420s)"
    except Exception as e:
        return f"Summary unavailable: {e}"
        
# ───────────────────────────────────────────────────────
def generate_dashboard(all_results: list, run_date: str,
                       macro_backdrop: str = "",
                       macro_summary: str = "",
                       mercury_fragment: str = "",
                       jansky_fragment: str = "",
                       sectors_delta_fragment: str = "",
                       trades_fragment: str = "",
                       etf_fragment: str = "") -> str:
    """Generate the HTML dashboard with Chart.js price graphs."""

    # Build chart data for each ticker
    chart_configs = []
    ticker_summaries = []

    for res in all_results:
        ticker = res["ticker"]
        data   = res["data"]
        summary = res.get("summary", "Summary not available.")

        # Parse price histories
        prices_1y  = parse_prices(data.get("prices_1y", ""))
        prices_6mo = parse_prices(data.get("prices_6mo", ""))

        # Extract key metrics from stock_info
        info_raw = data.get("stock_info", "")
        def extract(label, text):
            for line in text.split('\n'):
                if label in line:
                    val = line.split(':', 1)[-1].strip()
                    return val
            return "N/A"

        current_price  = extract("Current Price", info_raw).replace('$','').replace(',','')
        pe_ratio       = extract("P/E Ratio (TTM)", info_raw)
        forward_pe     = extract("Forward P/E", info_raw)
        analyst_target = extract("Analyst Target", info_raw).replace('$','').replace(',','')
        recommendation = extract("Recommendation", info_raw)
        week52_high    = extract("52-Week High", info_raw).replace('$','').replace(',','')
        week52_low     = extract("52-Week Low", info_raw).replace('$','').replace(',','')
        peg            = extract("PEG Ratio", info_raw)
        ps_ratio       = extract("Price/Sales", info_raw)
        pfcf           = extract("Price/FCF", info_raw)
        fcf            = extract("Free Cash Flow", info_raw)
        debt_equity    = extract("Debt/Equity", info_raw)

        # Drawdown from 52W high
        try:
            drawdown = ((float(current_price) - float(week52_high))
                        / float(week52_high) * 100)
            drawdown_str = f"{drawdown:.1f}%"
        except Exception:
            drawdown_str = "N/A"

        # Extract technical signals
        tech_raw = data.get("technicals", "")
        rsi       = extract("RSI (14)", tech_raw)
        ma_signal = extract("MA Signal", tech_raw)
        vs_50d    = extract("vs 50-Day MA", tech_raw)
        vs_200d   = extract("vs 200-Day MA", tech_raw)

        try:
            upside = ((float(analyst_target) - float(current_price))
                      / float(current_price) * 100)
            upside_str = f"+{upside:.1f}%" if upside > 0 else f"{upside:.1f}%"
        except Exception:
            upside_str = "N/A"

        # Build chart datasets
        labels_1y  = [p["date"] for p in prices_1y]
        values_1y  = [p["price"] for p in prices_1y]
        labels_6mo = [p["date"] for p in prices_6mo]
        values_6mo = [p["price"] for p in prices_6mo]

        chart_configs.append({
            "ticker":        ticker,
            "labels_1y":     labels_1y,
            "values_1y":     values_1y,
            "labels_6mo":    labels_6mo,
            "values_6mo":    values_6mo,
            "current_price": current_price,
            "pe":            pe_ratio,
            "fpe":           forward_pe,
            "peg":           peg,
            "ps":            ps_ratio,
            "pfcf":          pfcf,
            "fcf":           fcf,
            "debt_equity":   debt_equity,
            "target":        analyst_target,
            "upside":        upside_str,
            "drawdown":      drawdown_str,
            "rec":           recommendation,
            "high52":        week52_high,
            "low52":         week52_low,
            "rsi":           rsi,
            "ma_signal":     ma_signal,
            "vs_50d":        vs_50d,
            "vs_200d":       vs_200d,
        })

        ticker_summaries.append({
            "ticker":            ticker,
            "summary":           summary,
            "news":              data.get("stock_news", ""),
            "insider":           data.get("insider_trades", ""),
            "short":             data.get("short_interest", ""),
            "analyst":           data.get("analyst_ratings", ""),
            "institutional":     data.get("institutional", ""),
            "fundamentals":      data.get("fundamentals", ""),
            "technicals":        data.get("technicals", ""),
            "legal_proceedings": data.get("legal_proceedings", ""),
        })

    # Embed data as JSON for JS
    chart_json    = json.dumps(chart_configs)
    summary_json  = json.dumps(ticker_summaries)

    # ── Parse macro backdrop YAML into scorecard data for Outlook tab ────────
    # Simple line-by-line parser — avoids a PyYAML dependency
    def _parse_backdrop(yaml_text: str) -> list[dict]:
        """
        Extract the six theme blocks from the backdrop YAML.
        Returns a list of dicts: {theme, stance, evidence_strength,
        evidence_consensus, data_freshness, key_metrics[], risk_flags[]}
        """
        if not yaml_text:
            return []

        themes = []
        current: dict | None = None
        in_metrics = False
        in_flags   = False

        for raw_line in yaml_text.splitlines():
            line = raw_line.rstrip()
            stripped = line.lstrip()

            # Top-level theme block (e.g. "monetary_policy:")
            if (not line.startswith(" ") and not line.startswith("#")
                    and line.endswith(":") and line[0].isalpha()):
                block_name = line[:-1]
                # Only capture the six named theme blocks
                if block_name in ("monetary_policy", "inflation", "labor_market",
                                  "growth", "credit", "housing", "sentiment"):
                    if current:
                        themes.append(current)
                    current = {
                        "block":              block_name,
                        "theme":              block_name.replace("_", " ").title(),
                        "stance":             "",
                        "evidence_strength":  "",
                        "evidence_consensus": "",
                        "data_freshness":     "",
                        "key_metrics":        [],
                        "risk_flags":         [],
                    }
                    in_metrics = False
                    in_flags   = False
                else:
                    current    = None
                    in_metrics = False
                    in_flags   = False
                continue

            if current is None:
                continue

            # Two-space indented fields inside a theme block
            if stripped.startswith("theme:"):
                current["theme"] = stripped.split(":", 1)[1].strip().strip('"')
            elif stripped.startswith("stance:"):
                current["stance"] = stripped.split(":", 1)[1].strip().strip('"')
            elif stripped.startswith("evidence_strength:"):
                current["evidence_strength"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("evidence_consensus:"):
                current["evidence_consensus"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("data_freshness:"):
                current["data_freshness"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("key_metrics:"):
                in_metrics = True
                in_flags   = False
            elif stripped.startswith("risk_flags:"):
                in_flags   = True
                in_metrics = False
            elif in_metrics and stripped.startswith("-"):
                # Should not happen (metrics are key:val, not list) — skip
                pass
            elif in_metrics and ":" in stripped and not stripped.startswith("#"):
                key, val = stripped.split(":", 1)
                val = val.strip().strip('"')
                if val and val != "N/A":
                    current["key_metrics"].append(
                        f"{key.strip().replace('_', ' ')}: {val}"
                    )
            elif in_flags and stripped.startswith("- "):
                flag = stripped[2:].strip().strip('"')
                if flag and flag.lower() != "none_identified":
                    current["risk_flags"].append(flag)

        if current:
            themes.append(current)

        return themes

    scorecard_themes = _parse_backdrop(macro_backdrop)
    scorecard_json   = json.dumps(scorecard_themes)
    macro_prose_json = json.dumps(macro_summary)      # escape for JS string

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Weekly Stock Research — {run_date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:        #0a0e14;
    --surface:   #111720;
    --border:    #1e2a38;
    --accent:    #00d4aa;
    --accent2:   #f0a500;
    --danger:    #e05c5c;
    --text:      #c8d8e8;
    --muted:     #5a7a94;
    --up:        #00d4aa;
    --down:      #e05c5c;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 15px;
    line-height: 1.6;
  }}

  /* ── Header ── */
  header {{
    padding: 32px 40px 24px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
  }}
  header h1 {{
    font-family: 'Instrument Serif', serif;
    font-size: 32px;
    font-weight: 400;
    font-style: italic;
    color: #fff;
    letter-spacing: -0.5px;
  }}
  header h1 span {{ color: var(--accent); font-style: normal; }}
  .run-date {{ color: var(--muted); font-size: 13px; }}

  /* ── Nav tabs ── */
  nav {{
    padding: 0 40px;
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 4px;
    overflow-x: auto;
  }}
  nav button {{
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--muted);
    cursor: pointer;
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    padding: 12px 16px 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    transition: all 0.15s;
    white-space: nowrap;
  }}
  nav button:hover {{ color: var(--text); }}
  nav button.active {{
    border-bottom-color: var(--accent);
    color: var(--accent);
  }}

  /* ── Main content ── */
  main {{
    padding: 32px 40px;
    max-width: 1400px;
  }}

  .ticker-panel {{ display: none; }}
  .ticker-panel.active {{ display: block; }}

  /* ── Metrics row ── */
  .metrics {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 12px;
    margin-bottom: 28px;
  }}
  .metric {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
  }}
  .metric-label {{
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 6px;
  }}
  .metric-value {{
    font-size: 20px;
    font-weight: 500;
    color: #fff;
  }}
  .metric-value.up   {{ color: var(--up); }}
  .metric-value.down {{ color: var(--down); }}
  .metric-value.buy  {{ color: var(--accent); }}
  .metric-value.hold {{ color: var(--accent2); }}
  .metric-value.sell {{ color: var(--danger); }}

  /* ── Charts ── */
  .charts {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 28px;
  }}
  @media (max-width: 900px) {{
    .charts {{ grid-template-columns: 1fr; }}
  }}
  .chart-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 20px;
  }}
  .chart-title {{
    color: var(--muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 14px;
  }}
  .chart-card canvas {{ width: 100% !important; }}

  /* ── Summary + detail sections ── */
  .sections {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
  }}
  @media (max-width: 900px) {{
    .sections {{ grid-template-columns: 1fr; }}
  }}
  .section-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 20px;
  }}
  .section-card.full {{ grid-column: 1 / -1; }}
  .section-title {{
    color: var(--accent);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }}
  .section-body {{
    color: var(--text);
    font-size: 13px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 300px;
    overflow-y: auto;
  }}
  .section-body::-webkit-scrollbar {{ width: 4px; }}
  .section-body::-webkit-scrollbar-track {{ background: var(--bg); }}
  .section-body::-webkit-scrollbar-thumb {{ background: var(--border); }}

  /* Summary card special styling */
  .summary-card {{ grid-column: 1 / -1; }}
  .summary-body {{
    font-family: 'Instrument Serif', serif;
    font-size: 16px;
    line-height: 1.8;
    color: #dce8f0;
  }}
  .summary-body h1, .summary-body h2, .summary-body h3 {{
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--accent);
    margin: 16px 0 8px;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--border);
  }}
  .summary-body h1 {{ font-size: 14px; }}
  .summary-body p {{ margin-bottom: 8px; }}
  .summary-body ul, .summary-body ol {{
    padding-left: 20px;
    margin-bottom: 10px;
  }}
  .summary-body li {{ margin-bottom: 3px; }}
  .summary-body strong {{ color: #fff; font-weight: 500; }}
  .summary-body em {{ color: var(--accent2); font-style: italic; }}
  .summary-body hr {{
    border: none;
    border-top: 1px solid var(--border);
    margin: 14px 0;
  }}
  .summary-body code {{
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    background: var(--bg);
    padding: 1px 5px;
    border-radius: 3px;
    color: var(--accent2);
  }}

  /* ── Verdict badge ── */
  .verdict {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 3px;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 1px;
    text-transform: uppercase;
    margin-left: 12px;
    vertical-align: middle;
  }}
  .verdict-watch      {{ background: rgba(240,165,0,0.15); color: var(--accent2); border: 1px solid var(--accent2); }}
  .verdict-accumulate {{ background: rgba(0,212,170,0.15); color: var(--accent);  border: 1px solid var(--accent); }}
  .verdict-avoid      {{ background: rgba(224,92,92,0.15);  color: var(--danger);  border: 1px solid var(--danger); }}

  /* ── Scrollbar global ── */
  ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  ::-webkit-scrollbar-track {{ background: var(--bg); }}
  ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}

  /* ── Outlook Tab — Scorecard ── */
  .outlook-header {{
    font-family: 'Instrument Serif', serif;
    font-size: 13px;
    font-style: italic;
    color: var(--muted);
    margin-bottom: 24px;
    letter-spacing: 0.5px;
  }}
  .scorecard {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
    margin-bottom: 40px;
  }}
  .sc-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 18px 20px;
    border-top: 3px solid var(--border);
  }}
  .sc-card.sc-green  {{ border-top-color: var(--accent); }}
  .sc-card.sc-yellow {{ border-top-color: var(--accent2); }}
  .sc-card.sc-red    {{ border-top-color: var(--danger); }}
  .sc-theme {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--muted);
    margin-bottom: 6px;
  }}
  .sc-stance {{
    font-size: 17px;
    font-weight: 500;
    color: #fff;
    margin-bottom: 12px;
  }}
  .sc-stance.sc-green  {{ color: var(--accent); }}
  .sc-stance.sc-yellow {{ color: var(--accent2); }}
  .sc-stance.sc-red    {{ color: var(--danger); }}
  .sc-badges {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }}
  .sc-badge {{
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 2px 7px;
    border-radius: 3px;
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    color: var(--muted);
  }}
  .sc-metrics {{
    border-top: 1px solid var(--border);
    padding-top: 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .sc-metric-row {{
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: var(--text);
  }}
  .sc-metric-key {{ color: var(--muted); }}
  .sc-risk-flags {{
    margin-top: 10px;
    border-top: 1px solid var(--border);
    padding-top: 8px;
  }}
  .sc-risk-flag {{
    font-size: 10px;
    color: var(--danger);
    line-height: 1.5;
    margin-top: 3px;
  }}
  .sc-risk-flag::before {{ content: "⚠ "; }}

  /* ── Outlook Tab — Prose Report ── */
  .outlook-divider {{
    border: none;
    border-top: 1px solid var(--border);
    margin: 32px 0;
  }}
  .outlook-report-title {{
    font-family: 'Instrument Serif', serif;
    font-size: 22px;
    font-weight: 400;
    font-style: italic;
    color: #fff;
    margin-bottom: 24px;
  }}
  .outlook-report-title span {{ color: var(--accent2); font-style: normal; }}
  .outlook-body {{
    font-family: 'Instrument Serif', serif;
    font-size: 16px;
    line-height: 1.85;
    color: #dce8f0;
    max-width: 860px;
  }}
  .outlook-body h1, .outlook-body h2, .outlook-body h3 {{
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--accent2);
    margin: 28px 0 10px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }}
  .outlook-body p {{ margin-bottom: 10px; }}
  .outlook-body ul, .outlook-body ol {{
    padding-left: 22px;
    margin-bottom: 12px;
  }}
  .outlook-body li {{ margin-bottom: 4px; }}
  .outlook-body strong {{ color: #fff; font-weight: 500; }}
  .outlook-body em {{ color: var(--accent); font-style: italic; }}
  .outlook-body hr {{
    border: none;
    border-top: 1px solid var(--border);
    margin: 16px 0;
  }}
  .outlook-no-data {{
    color: var(--muted);
    font-size: 14px;
    font-style: italic;
    padding: 40px 0;
  }}</style>
</head>
<body>

<header>
  <h1>Watchlist <span>Research</span></h1>
  <div class="run-date">Generated {run_date} · Local AI Research Pipeline</div>
</header>

<nav id="nav"></nav>
<main id="main"></main>

<script>
const CHARTS   = {chart_json};
const SUMMARIES = {summary_json};
const SCORECARD  = {scorecard_json};
const MACRO_PROSE = {macro_prose_json};

// ── Stance → color class mapping ──────────────────────────────────────────
function stanceColor(stance) {{
  if (!stance) return 'sc-yellow';
  const s = stance.toLowerCase();
  // Green stances (supportive / benign)
  const greens = ['accommodative', 'at or below target', 'tight', 'trend growth',
                  'above trend', 'benign', 'stable', 'confident'];
  // Red stances (headwind / risk)
  const reds   = ['restrictive', 'significantly above target', 'above target',
                  'slack', 'contraction', 'stressed', 'pessimistic', 'stressed'];
  if (greens.some(g => s.includes(g))) return 'sc-green';
  if (reds.some(r  => s.includes(r)))  return 'sc-red';
  return 'sc-yellow';
}}

// ── Build Outlook tab panel ──────────────────────────────────────────────
function buildOutlookPanel() {{
  const main = document.getElementById('main');
  const panel = document.createElement('div');
  panel.className = 'ticker-panel';
  panel.id = 'panel-OUTLOOK';

  let scorecardHtml = '';

  if (SCORECARD && SCORECARD.length > 0) {{
    scorecardHtml = '<div class="scorecard">';
    SCORECARD.forEach(theme => {{
      const color  = stanceColor(theme.stance);
      const badges = [
        theme.evidence_strength  ? `Evidence: ${{theme.evidence_strength}}`  : null,
        theme.evidence_consensus ? `Consensus: ${{theme.evidence_consensus}}` : null,
        theme.data_freshness     ? `Data: ${{theme.data_freshness}}`          : null,
      ].filter(Boolean);

      const badgeHtml = badges.map(b =>
        `<span class="sc-badge">${{b}}</span>`
      ).join('');

      const metricsHtml = (theme.key_metrics || []).slice(0, 6).map(m => {{
        const parts = m.split(':');
        const key   = parts[0].trim();
        const val   = parts.slice(1).join(':').trim();
        return `<div class="sc-metric-row">
          <span class="sc-metric-key">${{key}}</span>
          <span>${{val}}</span>
        </div>`;
      }}).join('');

      const flagsHtml = (theme.risk_flags || []).map(f =>
        `<div class="sc-risk-flag">${{f}}</div>`
      ).join('');

      scorecardHtml += `
        <div class="sc-card ${{color}}">
          <div class="sc-theme">${{theme.theme}}</div>
          <div class="sc-stance ${{color}}">${{theme.stance || 'Unknown'}}</div>
          <div class="sc-badges">${{badgeHtml}}</div>
          ${{metricsHtml ? `<div class="sc-metrics">${{metricsHtml}}</div>` : ''}}
          ${{flagsHtml   ? `<div class="sc-risk-flags">${{flagsHtml}}</div>` : ''}}
        </div>`;
    }});
    scorecardHtml += '</div>';
  }} else {{
    scorecardHtml = '<p class="outlook-no-data">No macro scorecard data available — run weekly_macro.py first.</p>';
  }}

  let proseHtml = '';
  if (MACRO_PROSE && MACRO_PROSE.trim()) {{
    proseHtml = `
      <hr class="outlook-divider">
      <div class="outlook-report-title">Weekly <span>Economic Briefing</span></div>
      <div class="outlook-body" id="outlook-prose-body"></div>`;
  }} else {{
    proseHtml = '<p class="outlook-no-data" style="margin-top:24px">No economic briefing available — run weekly_macro.py first.</p>';
  }}

  panel.innerHTML = `
    <div class="outlook-header">
      Atlas — Obsidian Capital Economics Advisor &nbsp;·&nbsp; {run_date}
    </div>
    ${{scorecardHtml}}
    ${{proseHtml}}
  `;

  main.appendChild(panel);

  // Render markdown prose after DOM insert
  if (MACRO_PROSE && MACRO_PROSE.trim()) {{
    setTimeout(() => {{
      const el = document.getElementById('outlook-prose-body');
      if (el) el.innerHTML = marked.parse(MACRO_PROSE);
    }}, 50);
  }}
}}

// ── Inject Outlook nav button ──────────────────────────────────────────────
function injectOutlookTab() {{
  const nav = document.getElementById('nav');
  const btn = document.createElement('button');
  btn.id        = 'btn-OUTLOOK';
  btn.textContent = 'OUTLOOK';
  btn.style.color = 'var(--accent2)';
  btn.onclick = () => {{
    document.querySelectorAll('.ticker-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('nav button').forEach(b => {{
      b.classList.remove('active');
      b.style.borderBottomColor = '';
    }});
    document.getElementById('panel-OUTLOOK').classList.add('active');
    btn.classList.add('active');
    btn.style.borderBottomColor = 'var(--accent2)';
  }};
  // Insert before the first ticker button. The sectors_delta_fragment injects
  // SECTORS + DELTA buttons at position 0 *after* this runs (fragments are
  // appended at end of </body>), so final order is: SECTORS | DELTA | OUTLOOK | tickers
  nav.insertBefore(btn, nav.firstChild);
}}

buildOutlookPanel();
injectOutlookTab();


function recColor(rec) {{
  if (!rec) return '';
  const r = rec.toUpperCase();
  if (r.includes('STRONG BUY') || r.includes('BUY')) return 'buy';
  if (r.includes('HOLD'))   return 'hold';
  if (r.includes('SELL'))   return 'sell';
  return '';
}}

function extractVerdict(summary) {{
  // Parse the actual "Overall Verdict" line Jupiter is instructed to
  // write (see prompt section 13), rather than scanning the whole
  // document for a bare keyword — the prose routinely discusses and
  // explicitly REJECTS a verdict word ("ACCUMULATE is wrong here"),
  // which a naive whole-text .includes() would misread as confirming it.
  const m = summary.match(/Overall Verdict\\**:?\\s*(ACCUMULATE|WATCH|AVOID)/i);
  if (m) return m[1].toUpperCase();
  // Fallback only if the expected section header wasn't found at all
  // (e.g. a malformed/truncated summary) — same old behavior as a
  // last resort, not the primary path.
  const s = summary.toUpperCase();
  if (s.includes('ACCUMULATE')) return 'ACCUMULATE';
  if (s.includes('AVOID'))      return 'AVOID';
  return 'WATCH';
}}

function verdictClass(summary) {{
  return extractVerdict(summary).toLowerCase();
}}

function verdictLabel(summary) {{
  return extractVerdict(summary);
}}

function makeChart(canvasId, labels, values, color, label) {{
  const ctx = document.getElementById(canvasId).getContext('2d');

  // Gradient fill
  const gradient = ctx.createLinearGradient(0, 0, 0, 200);
  gradient.addColorStop(0, color + '33');
  gradient.addColorStop(1, color + '00');

  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels,
      datasets: [{{
        label,
        data: values,
        borderColor: color,
        backgroundColor: gradient,
        borderWidth: 1.5,
        pointRadius: 0,
        pointHoverRadius: 4,
        fill: true,
        tension: 0.3
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ intersect: false, mode: 'index' }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1e2a38',
          titleColor: '#5a7a94',
          bodyColor: '#c8d8e8',
          borderColor: '#1e2a38',
          borderWidth: 1,
          callbacks: {{
            label: ctx => ` ${{ctx.parsed.y.toFixed(2)}}`
          }}
        }}
      }},
      scales: {{
        x: {{
          grid: {{ color: '#1e2a38', drawTicks: false }},
          ticks: {{
            color: '#5a7a94',
            maxTicksLimit: 8,
            maxRotation: 0,
            font: {{ family: 'DM Mono', size: 10 }}
          }}
        }},
        y: {{
          position: 'right',
          grid: {{ color: '#1e2a38', drawTicks: false }},
          ticks: {{
            color: '#5a7a94',
            callback: v => '$' + v.toFixed(0),
            font: {{ family: 'DM Mono', size: 10 }}
          }}
        }}
      }}
    }}
  }});
}}

function buildPanel(c, s) {{
  const vClass = verdictClass(s.summary);
  const vLabel = verdictLabel(s.summary);

  // Chart color based on price trend
  const trending = c.values_1y.length > 1
    ? (c.values_1y[c.values_1y.length-1] >= c.values_1y[0] ? '#00d4aa' : '#e05c5c')
    : '#00d4aa';

  // RSI color
  function rsiColor(rsi) {{
    const v = parseFloat(rsi);
    if (isNaN(v)) return '';
    if (v <= 30) return 'up';
    if (v >= 70) return 'down';
    return '';
  }}

  // MA signal color
  function maColor(sig) {{
    if (!sig) return '';
    const s = sig.toUpperCase();
    if (s.includes('GOLDEN')) return 'buy';
    if (s.includes('DEATH'))  return 'sell';
    return '';
  }}

  // Drawdown color
  function ddColor(dd) {{
    const v = parseFloat(dd);
    if (isNaN(v)) return '';
    if (v <= -30) return 'down';
    if (v <= -15) return 'hold';
    return '';
  }}

  const panel = document.createElement('div');
  panel.className = 'ticker-panel';
  panel.id = `panel-${{c.ticker}}`;
  panel.innerHTML = `
    <div class="metrics">
      <div class="metric">
        <div class="metric-label">Current Price</div>
        <div class="metric-value">$${{c.current_price}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">P/E (TTM)</div>
        <div class="metric-value">${{c.pe}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Forward P/E</div>
        <div class="metric-value">${{c.fpe}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">PEG Ratio</div>
        <div class="metric-value">${{c.peg}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Price/Sales</div>
        <div class="metric-value">${{c.ps}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Price/FCF</div>
        <div class="metric-value">${{c.pfcf}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Free Cash Flow</div>
        <div class="metric-value">${{c.fcf}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Debt/Equity</div>
        <div class="metric-value">${{c.debt_equity}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Analyst Target</div>
        <div class="metric-value up">$${{c.target}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Upside</div>
        <div class="metric-value up">${{c.upside}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">52W Drawdown</div>
        <div class="metric-value ${{ddColor(c.drawdown)}}">${{c.drawdown}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">52W High</div>
        <div class="metric-value muted">$${{c.high52}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">52W Low</div>
        <div class="metric-value muted">$${{c.low52}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">RSI (14)</div>
        <div class="metric-value ${{rsiColor(c.rsi)}}">${{c.rsi}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">MA Signal</div>
        <div class="metric-value ${{maColor(c.ma_signal)}}">${{c.ma_signal}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">vs 50D MA</div>
        <div class="metric-value">${{c.vs_50d}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">vs 200D MA</div>
        <div class="metric-value">${{c.vs_200d}}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Consensus</div>
        <div class="metric-value ${{recColor(c.rec)}}">${{c.rec}}</div>
      </div>
    </div>

    <div class="charts">
      <div class="chart-card">
        <div class="chart-title">1 Year Price History</div>
        <div style="height:200px">
          <canvas id="chart1y-${{c.ticker}}"></canvas>
        </div>
      </div>
      <div class="chart-card">
        <div class="chart-title">6 Month Price History</div>
        <div style="height:200px">
          <canvas id="chart6mo-${{c.ticker}}"></canvas>
        </div>
      </div>
    </div>

    <div class="sections">
      <div class="section-card summary-card">
        <div class="section-title">
          AI Analysis
          <span class="verdict verdict-${{vClass}}">${{vLabel}}</span>
        </div>
        <div class="summary-body" id="summary-${{c.ticker}}"></div>
      </div>
      <div class="section-card">
        <div class="section-title">Technical Signals</div>
        <div class="section-body">${{s.technicals}}</div>
      </div>
      <div class="section-card">
        <div class="section-title">Fundamentals &amp; Valuation</div>
        <div class="section-body">${{s.fundamentals}}</div>
      </div>
      <div class="section-card">
        <div class="section-title">Institutional Ownership</div>
        <div class="section-body">${{s.institutional}}</div>
      </div>
      <div class="section-card">
        <div class="section-title">Recent News</div>
        <div class="section-body">${{s.news}}</div>
      </div>
      <div class="section-card">
        <div class="section-title">Analyst Ratings</div>
        <div class="section-body">${{s.analyst}}</div>
      </div>
      <div class="section-card">
        <div class="section-title">Insider Activity</div>
        <div class="section-body">${{s.insider}}</div>
      </div>
      <div class="section-card">
        <div class="section-title">Short Interest</div>
        <div class="section-body">${{s.short}}</div>
      </div>
      <div class="section-card full">
        <div class="section-title">⚖ Legal &amp; Regulatory Risk — 10-K</div>
        <div class="section-body">${{s.legal_proceedings}}</div>
      </div>
    </div>
  `;

  document.getElementById('main').appendChild(panel);

  // Render markdown + draw charts after DOM insert
  setTimeout(() => {{
    const summaryEl = document.getElementById(`summary-${{c.ticker}}`);
    if (summaryEl && s.summary) {{
      summaryEl.innerHTML = marked.parse(s.summary);
    }}
    makeChart(`chart1y-${{c.ticker}}`,  c.labels_1y,  c.values_1y,  trending,  '1Y');
    makeChart(`chart6mo-${{c.ticker}}`, c.labels_6mo, c.values_6mo, trending, '6Mo');
  }}, 50);
}}

function showPanel(ticker) {{
  document.querySelectorAll('.ticker-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(`panel-${{ticker}}`).classList.add('active');
  document.getElementById(`btn-${{ticker}}`).classList.add('active');
}}

// Build nav + panels
const nav = document.getElementById('nav');
CHARTS.forEach((c, i) => {{
  const s = SUMMARIES.find(x => x.ticker === c.ticker) || {{ summary: '', news: '', insider: '', short: '', analyst: '', institutional: '', fundamentals: '', technicals: '', legal_proceedings: '' }};

  // Nav button
  const btn = document.createElement('button');
  btn.id = `btn-${{c.ticker}}`;
  btn.textContent = c.ticker;
  if (i === 0) btn.classList.add('active');
  btn.onclick = () => showPanel(c.ticker);
  nav.appendChild(btn);

  // Panel
  buildPanel(c, s);
}});

// Show first panel
if (CHARTS.length > 0) {{
  document.getElementById(`panel-${{CHARTS[0].ticker}}`).classList.add('active');
}}
</script>
</body>
</html>"""

    # Inject Mercury CCC dashboard fragment if available
    if mercury_fragment:
        html = html.replace("</body>\n</html>", mercury_fragment + "\n</body>\n</html>")

    # Inject Jansky review fragment if available
    if jansky_fragment:
        html = html.replace("</body>\n</html>", jansky_fragment + "\n</body>\n</html>")

    # Inject Trades fragment if available (produced by jansky_review.py Pass 21)
    if trades_fragment:
        html = html.replace("</body>\n</html>", trades_fragment + "\n</body>\n</html>")

    # Inject ETF fragment if available (produced by weekly_mercury.py)
    if etf_fragment:
        html = html.replace("</body>\n</html>", etf_fragment + "\n</body>\n</html>")

    # Inject Sectors + Delta fragment if available (produced by sector_ranking.py)
    # Injected last so its JS runs after all other nav buttons exist
    if sectors_delta_fragment:
        html = html.replace("</body>\n</html>", sectors_delta_fragment + "\n</body>\n</html>")

    return html

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    os.makedirs(DATA_DIR,   exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    args = sys.argv[1:]
    run_sector_ranking = False
    sector_label       = None

    if "--sector" in args:
        # ── --sector <SectorName> ──────────────────────────────────────────
        idx = args.index("--sector")
        if idx + 1 >= len(args):
            print("  ✗ --sector requires a sector name, e.g. --sector Casinos")
            sys.exit(1)

        sector_name = args[idx + 1]
        _, all_sectors = load_watchlist()

        # Case-insensitive match
        matched = next(
            (k for k in all_sectors if k.lower() == sector_name.lower()),
            None
        )
        if not matched:
            available = ", ".join(all_sectors.keys())
            print(f"  ✗ Sector '{sector_name}' not found in watchlist.json")
            print(f"  Available sectors: {available}")
            sys.exit(1)

        tickers      = [t.upper() for t in all_sectors[matched]]
        sectors      = all_sectors   # full sector map for prompt context
        sector_label = matched
        print(f"  ✓ --sector {matched} — {len(tickers)} tickers: {', '.join(tickers)}")

    elif args:
        # ── Explicit ticker list ───────────────────────────────────────────
        tickers = [t.upper() for t in args]
        _, sectors = load_watchlist()   # load sectors for prompt context
        print(f"  ⚠ CLI override — running {len(tickers)} ticker(s), skipping sector ranking")

    else:
        # ── Full watchlist run ─────────────────────────────────────────────
        tickers, sectors = load_watchlist()
        run_sector_ranking = bool(sectors)

    run_date = datetime.datetime.now().strftime('%Y-%m-%d')
    stamp    = datetime.datetime.now().strftime('%Y%m%d_%H%M')

    # Append sector name to stamp so files don't collide with full runs
    if sector_label:
        stamp = f"{stamp}_{sector_label.lower().replace(' ', '_')}"

    print(f"\n{'═'*52}")
    print(f"  Weekly Stock Research Pipeline")
    if sector_label:
        print(f"  Sector: {sector_label}")
    print(f"  {run_date}  ·  {len(tickers)} tickers")
    print(f"{'═'*52}\n")

    # ── Load Atlas macro backdrop (silent skip if not present) ────────────────
    macro_backdrop, macro_summary_prose = load_macro_backdrop()
    if macro_backdrop:
        print(f"  ✓ Atlas macro backdrop loaded ({len(macro_backdrop)} chars)")
    else:
        print(f"  ⚠ No macro backdrop found — run weekly_macro.py first for macro context")

    # ── Load Nova supplemental data ──────────────────────────────────────────
    nova_data = load_nova_supplemental()
    if nova_data:
        print(f"  ✓ Nova supplemental loaded ({len(nova_data)} ticker records)")
    else:
        print(f"  ⚠ No Nova data — run nova_legal.py / nova_earnings_call.py first")

    # ── Load Neptune portfolio holdings ──────────────────────────────────────
    holdings_data = {}
    if os.path.exists(NEPTUNE_HOLDINGS):
        try:
            with open(NEPTUNE_HOLDINGS) as f:
                holdings_data = json.load(f)
            print(f"  ✓ Neptune holdings loaded ({len(holdings_data.get('positions', {}))} positions)")
        except Exception as e:
            print(f"  ⚠ Could not load Neptune holdings: {e}")

    # ── Load Jansky trade feedback (injected when ticker was previously pitched) ──
    trade_feedback = {}
    if os.path.exists(TRADE_FEEDBACK):
        try:
            with open(TRADE_FEEDBACK) as f:
                trade_feedback = json.load(f)
            print(f"  ✓ Jansky trade feedback loaded ({len(trade_feedback)} entries)")
        except Exception as e:
            print(f"  ⚠ Could not load trade feedback: {e}")

    all_results = []

    for ticker in tickers:
        res = research_ticker(ticker)

        # Generate LLM summary (with macro, Nova, and holdings preambles)
        print(f"    [summary] asking Jupiter...", end=" ", flush=True)
        summary = generate_summary(ticker, res["data"],
                                   macro_backdrop=macro_backdrop,
                                   nova_data=nova_data,
                                   holdings_data=holdings_data,
                                   sectors=sectors,
                                   trade_feedback=trade_feedback)
        res["summary"] = summary
        print(f"✓ ({len(summary)} chars)")

        all_results.append(res)
        print()

    # Save raw JSON data
    json_path = f"{DATA_DIR}/research_{stamp}.json"
    with open(json_path, 'w') as f:
        json.dump(all_results, f, indent=2)
    print(f"✓ Data saved: {json_path}")

    # ── Load config for fragment directory ────────────────────────────────────
    _obs_config = {}
    _config_path = os.path.join(BASE_DIR, "obsidian_config.json")
    if os.path.exists(_config_path):
        try:
            with open(_config_path) as _cf:
                _obs_config = json.load(_cf)
        except Exception:
            pass
    frag_dir = os.path.join(BASE_DIR, _obs_config.get("fragment_dir", "fragments"))
    os.makedirs(frag_dir, exist_ok=True)

    def _load_fragment(name: str, label: str) -> str:
        """Try fragments/ dir first, then root for backward compat."""
        for path in [os.path.join(frag_dir, name), os.path.join(BASE_DIR, name)]:
            if os.path.exists(path):
                try:
                    content = open(path).read()
                    print(f"  ✓ {label} loaded ({len(content):,} chars)")
                    return content
                except Exception:
                    pass
        return ""

    # Generate HTML dashboard (pass macro data for Outlook tab)
    mercury_fragment       = _load_fragment("mercury_dashboard_fragment.html", "Mercury CCC fragment")
    jansky_fragment        = _load_fragment("jansky_dashboard_fragment.html",  "Jansky fragment")
    trades_fragment        = _load_fragment("trades_dashboard_fragment.html",  "Trades fragment")
    etf_fragment           = _load_fragment("etf_dashboard_fragment.html",     "ETF fragment")

    html = generate_dashboard(all_results, run_date,
                              macro_backdrop=macro_backdrop,
                              macro_summary=macro_summary_prose,
                              mercury_fragment=mercury_fragment,
                              jansky_fragment=jansky_fragment,
                              trades_fragment=trades_fragment,
                              etf_fragment=etf_fragment)
    html_path   = f"{REPORT_DIR}/dashboard_{stamp}.html"
    latest_path = f"{BASE_DIR}/dashboard.html"
    with open(html_path, 'w') as f:
        f.write(html)
    with open(latest_path, 'w') as f:
        f.write(html)

    print(f"✓ Dashboard: {html_path}")
    print(f"✓ Latest:    {latest_path}")

    # ── Pass 2: Sector Ranking ──────────────────────────────────────────────
    if run_sector_ranking:
        print(f"\n{'─'*52}")
        print(f"  Starting Pass 2: Sector Ranking...")
        print(f"{'─'*52}")
        try:
            sector_ranking_path = os.path.join(BASE_DIR, "sector_ranking.py")
            result = subprocess.run(
                ["python3", sector_ranking_path, json_path],
                check=False
            )
            if result.returncode != 0:
                print(f"  ⚠ sector_ranking.py exited with code {result.returncode}")
        except Exception as e:
            print(f"  ✗ Could not run sector_ranking.py: {e}")

        # ── Rebuild dashboard with all fragments (including Sectors+Delta) ────
        sectors_delta_fragment = _load_fragment("sectors_delta_fragment.html", "Sectors+Delta fragment")

        if sectors_delta_fragment:
            print(f"  Rebuilding dashboard with all fragments...")
            html = generate_dashboard(all_results, run_date,
                                      macro_backdrop=macro_backdrop,
                                      macro_summary=macro_summary_prose,
                                      mercury_fragment=mercury_fragment,
                                      jansky_fragment=jansky_fragment,
                                      sectors_delta_fragment=sectors_delta_fragment,
                                      trades_fragment=trades_fragment,
                                      etf_fragment=etf_fragment)
            with open(html_path, 'w') as f:
                f.write(html)
            with open(latest_path, 'w') as f:
                f.write(html)
            print(f"  ✓ Dashboard rebuilt with all tabs")
    else:
        print(f"\n  ⚠ Skipping sector ranking (CLI override or no sectors defined)")

    print(f"\n  Open: http://localhost:8090")
    print(f"{'═'*52}\n")


if __name__ == "__main__":
    main()
