#!/usr/bin/env python3
"""
Weekly CCC Research — Mercury Agent
Fetches Currencies, Cryptocurrencies & Commodities data via mercurymcp,
generates Mercury's structured analysis, and produces:
  - data/mercury_YYYYMMDD_HHMM.json       (full data + summary)
  - mercury_latest.json                   (copy of latest for dashboard)
  - mercury_backdrop.yaml                 (structured YAML for cross-agent injection)
  - mercury_summary.md                    (human-readable weekly CCC briefing)
  - mercury_dashboard_fragment.html       (CCC tab injected into main dashboard)

Run order: after weekly_macro.py, before weekly_research.py
  python3 weekly_macro.py      # Atlas macro backdrop
  python3 weekly_mercury.py    # Mercury CCC analysis
  python3 weekly_research.py   # Jupiter stock research (all watchlist tickers)
"""

import requests
import json
import datetime
import time
import sys
import os
import subprocess

# ─── Configuration ────────────────────────────────────────────────────────────
MERCURYMCP_URL  = "http://localhost:8645/mcp"
BASE_DIR        = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
DATA_DIR        = f"{BASE_DIR}/data"
REPORT_DIR      = f"{BASE_DIR}/reports"
WATCHLIST_JSON  = f"{BASE_DIR}/mercury_watchlist.json"
MACRO_BACKDROP  = f"{BASE_DIR}/macro_backdrop.yaml"
MERCURY_BACKDROP  = f"{BASE_DIR}/mercury_backdrop.yaml"
MERCURY_SUMMARY   = f"{BASE_DIR}/mercury_summary.md"
MERCURY_LATEST    = f"{BASE_DIR}/mercury_latest.json"
NEPTUNE_HOLDINGS  = f"{BASE_DIR}/neptune_holdings.json"
CONFIG_FILE       = f"{BASE_DIR}/obsidian_config.json"
TRADE_DECISIONS   = f"{BASE_DIR}/trade_decisions.json"
TRADE_FEEDBACK    = f"{BASE_DIR}/jansky_trade_feedback.json"

# Fragment paths resolved after config load (fragments/ subdir)
# Set in main() once config is loaded
MERCURY_FRAGMENT  = None
ETF_FRAGMENT      = None

# ─── Config Loader ────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load obsidian_config.json. Returns defaults if missing."""
    defaults = {
        "trade_limits": {"min_trade_dollars": 250000, "max_trade_dollars": 1000000},
        "position_limits": {"max_etf_position_pct": 15.0, "max_equity_position_pct": 10.0},
        "cash_floor": {"min_cash_pct": 10.0},
        "fragment_dir": "fragments",
        "mercury_allowed_etfs": ["IAU", "BITQ", "VDE", "FBTC", "IBIT",
                                  "GLD", "SLV", "PDBC", "DBA", "USO", "UNG",
                                  "CPER", "VNQ", "IYR", "XLRE", "TLT", "HYG"],
    }
    if not os.path.exists(CONFIG_FILE):
        return defaults
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return defaults

# ─── Watchlist ────────────────────────────────────────────────────────────────

def load_mercury_watchlist() -> dict:
    """Load mercury_watchlist.json. Returns categories dict."""
    if not os.path.exists(WATCHLIST_JSON):
        print(f"  ⚠ mercury_watchlist.json not found at {WATCHLIST_JSON}")
        return {}
    with open(WATCHLIST_JSON) as f:
        return json.load(f)

# ─── MCP Tool Caller ──────────────────────────────────────────────────────────

_RETRYABLE_ERRORS = ("HTTP 500", "HTTP 429", "HTTP 503", "rate limit",
                     "temporarily unavailable", "server error")


class _RetryableError(Exception):
    def __init__(self, snippet: str, wait: float):
        self.snippet = snippet
        self.wait    = wait
        super().__init__(snippet)


def call_tool(tool_name: str, arguments: dict,
              retries: int = 3, backoff: float = 5.0) -> str:
    """
    Call a mercurymcp tool via StreamableHTTP MCP protocol.
    Mirrors the call_tool() pattern from weekly_research.py exactly.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json, text/event-stream"
    }

    last_result = "No data returned"

    for attempt in range(retries + 1):
        session = requests.Session()
        try:
            init_r = session.post(MERCURYMCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities":    {},
                    "clientInfo":      {"name": "weekly_mercury", "version": "1.0"}
                }
            }, timeout=30)

            sid = init_r.headers.get("mcp-session-id", "")
            if sid:
                headers["mcp-session-id"] = sid

            session.post(MERCURYMCP_URL, headers=headers, json={
                "jsonrpc": "2.0",
                "method":  "notifications/initialized",
                "params":  {}
            }, timeout=10)

            r = session.post(MERCURYMCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 2,
                "method":  "tools/call",
                "params":  {"name": tool_name, "arguments": arguments}
            }, timeout=120)

            # NOTE: r.text is deliberately NOT used here. requests falls back to
            # ISO-8859-1 when a text/* content-type (e.g. text/event-stream) has
            # no explicit charset= parameter, which mojibakes any UTF-8 multi-byte
            # character (─, —, ↑, ⚠, etc.) into 2-3 wrong characters each.
            # Decoding r.content explicitly avoids that guesswork.
            text   = r.content.decode('utf-8', errors='replace').strip()
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

            result_upper = result.upper()
            retryable    = any(e.upper() in result_upper for e in _RETRYABLE_ERRORS)
            if retryable and attempt < retries:
                last_result = result
                wait        = backoff * (2 ** attempt)
                raise _RetryableError(result[:80], wait)

            return result

        except _RetryableError:
            raise

        except Exception as e:
            last_result = f"ERROR: {str(e)}"
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            return last_result

    return last_result


def run_tool(tool_name: str, arguments: dict = {}) -> str:
    """Convenience wrapper with console progress output."""
    print(f"    [{tool_name}]...", end=" ", flush=True)

    MAX_OUTER = 3
    data      = None
    for outer in range(MAX_OUTER + 1):
        try:
            data = call_tool(tool_name, arguments)
            break
        except _RetryableError as e:
            if outer < MAX_OUTER:
                print(f"\u21ba error ({e.snippet[:40].strip()}), "
                      f"waiting {e.wait:.0f}s...", end=" ", flush=True)
                time.sleep(e.wait)
            else:
                data = f"ERROR: max retries exceeded ({e.snippet[:60]})"
                break

    if data is None:
        data = f"ERROR: max retries exceeded for {tool_name}"

    status = "✓" if not str(data).startswith("ERROR") else "✗"
    print(f"{status} ({len(str(data)):,} chars)")
    time.sleep(1)
    return data

# ─── Atlas Macro Context ──────────────────────────────────────────────────────

def load_macro_backdrop() -> str:
    """Load Atlas macro_backdrop.yaml for injection into Mercury's prompt."""
    if not os.path.exists(MACRO_BACKDROP):
        return ""
    try:
        with open(MACRO_BACKDROP, "r") as f:
            return f.read().strip()
    except Exception:
        return ""

# ─── Mercury LLM Summary ─────────────────────────────────────────────────────

def generate_mercury_summary(collected_data: dict,
                              macro_backdrop: str = "") -> str:
    """
    Call Mercury (Hermes profile) to synthesize the collected CCC data
    into a structured weekly report.
    """
    macro_preamble = ""
    if macro_backdrop:
        macro_preamble = f"""MACROECONOMIC CONTEXT (provided by Atlas — Obsidian Capital Economics Advisor):
The following structured economic backdrop was generated this week by Atlas.
Use it as the macro foundation for your CCC analysis — it directly informs
currency, commodity, and crypto price dynamics.

{macro_backdrop}

─────────────────────────────────────────────────────────────────────────────
END OF MACRO CONTEXT — BEGIN CCC ANALYSIS BELOW
─────────────────────────────────────────────────────────────────────────────

"""

    # ── Neptune portfolio holdings context ─────────────────────────────────
    holdings_preamble = ""
    if os.path.exists(NEPTUNE_HOLDINGS):
        try:
            with open(NEPTUNE_HOLDINGS) as f:
                hdata = json.load(f)
            pos = hdata.get("positions", {})
            summary = hdata.get("summary", {})
            if pos:
                total_val = summary.get("total_portfolio_value", 0)
                cash_val = summary.get("cash_value", 0)
                total_gain = summary.get("total_gain_loss_pct", 0)
                # Extract CCC-related positions
                etf_lines = []
                for ticker in ["IAU", "BITQ", "VDE"]:
                    p = pos.get(ticker)
                    if p:
                        mv = p.get("market_value", 0)
                        gl = p.get("gain_loss_pct", 0)
                        etf_lines.append(f"    {ticker:<6} ${mv:>8,.0f}  ({gl:>+.1f}%)")
                if etf_lines:
                    etf_str = "\n".join(etf_lines)
                    holdings_preamble = f"""PORTFOLIO HOLDINGS CONTEXT (Neptune Paper Trading — {hdata.get('last_updated', 'unknown')}):
The firm holds the following ETF positions relevant to the CCC universe.
Use this context to tailor your analysis — these are live portfolio exposures.

  Total Portfolio: ${total_val:,.0f}  |  Cash: ${cash_val:,.0f} ({summary.get('cash_pct',0):.1f}%)  |  Total Gain: {total_gain:+.2f}%
  CCC-Relevant Positions:
{etf_str}

─────────────────────────────────────────────────────────────────────────────

"""
        except Exception:
            pass

    # Serialize collected data, omitting the backdrop YAML (already in preamble)
    data_for_prompt = {k: v for k, v in collected_data.items()
                       if k != "mercury_backdrop"}

    prompt = f"""{macro_preamble}{holdings_preamble}You are Mercury, the Currencies, Cryptocurrencies & Commodities analyst
at Obsidian Capital. Analyze the data provided below and produce your weekly
CCC report structured exactly as follows:

## 1. Forex & Currencies

**DXY Overview**
State the current DXY level, 4-week direction, and its primary implication
for the rest of your asset classes this week.

**Major Pair Highlights**
Focus on the 2-3 most significant currency moves. For each: current rate,
direction, driver (rate differential, risk sentiment, data), and implication.

**Central Bank Divergence**
Which policy divergences are most relevant this week? What trades do they
support?

**Money Supply Signals**
Any notable M2 trends worth flagging?

---

## 2. Cryptocurrencies

**BTC & ETH**
Current prices, 7d/30d trend, key levels to watch, and what the trend
signals about overall risk appetite.

**Crypto ETFs (BITQ, FBTC, IBIT)**
Performance vs spot crypto. Any flow signals?

**News & Catalysts**
Top 2-3 crypto news themes this week and their market implication.

---

## 3. Commodities

**Energy**
WTI and Brent trend, key driver (inventory, geopolitics, demand), and
implication for energy sector equities.

**Metals**
Gold safe-haven signal, copper growth proxy reading. Gold/Silver ratio
interpretation. Any metals with notable moves?

**Agriculture**
WASDE highlights, crop progress, drought conditions. Which crops have the
most significant supply/demand story this week?

**Livestock**
Key price moves and drivers.

**Baltic Dry Index**
Current level, trend, and what it signals about global demand.

**COT Positioning**
Which contracts show extreme speculator positioning? Contrarian signals?

---

## 4. Cross-Market Synthesis

**Risk Appetite Signal**
What are the Three C's collectively signaling about global risk appetite
this week? (Risk-on / Risk-off / Mixed — and why.)

**Cross-Asset Conflicts or Confirmations**
Any notable divergences between what currencies, crypto, and commodities
are signaling?

**Buying Opportunities (Top 2-3)**
For each: instrument, rationale, what to watch for confirmation, key risk.

**Risk Warnings (Top 1-2)**
Specific asymmetric risks in the CCC universe this week.

---

IMPORTANT RULES:
- Base analysis ONLY on data provided below
- Flag missing data explicitly — do not invent metrics
- DXY direction is your first-order filter — establish it early
- COT extreme positioning is contrarian, not momentum
- Be direct and concise — institutional research for a sophisticated reader

DATA:
{json.dumps(data_for_prompt, indent=2)[:80000]}
"""

    try:
        result = subprocess.run(
            ["mercury", "-z", prompt, "--reasoning", "high"],
            capture_output=True, text=True, timeout=420
        )
        output = result.stdout.strip()
        if output:
            return output
        err = result.stderr.strip()
        return (f"Mercury summary unavailable (exited {result.returncode}: {err[:200]})"
                if err else
                f"Mercury summary unavailable (exited {result.returncode} with no output)")
    except FileNotFoundError:
        return "Mercury summary unavailable (mercury profile not found in PATH)"
    except subprocess.TimeoutExpired:
        return "Mercury summary unavailable (timed out after 420s)"
    except Exception as e:
        return f"Mercury summary unavailable: {e}"

# ─── Dashboard Fragment Generator ─────────────────────────────────────────────

def generate_dashboard_fragment(summary: str, collected_data: dict,
                                 run_date: str) -> str:
    """
    Generate the self-contained CCC tab HTML fragment.
    Injected into the main dashboard by weekly_research.py / generate_dashboard().
    Contains only the nav button JS injection and panel HTML — no full page wrapper.
    """
    # Extract key metrics for the header cards
    forex_data  = collected_data.get("forex_rates", "")
    crypto_data = collected_data.get("crypto_prices", "")
    metals_data = collected_data.get("metals_prices", "")
    energy_data = collected_data.get("energy_prices", "")
    bdi_data    = collected_data.get("baltic_dry", "")

    def _extract(label: str, text: str) -> str:
        """Pull the first value found after a label in a text block."""
        for line in text.split('\n'):
            if label.lower() in line.lower() and ':' in line:
                val = line.split(':', 1)[-1].strip()
                # Take just the first token (the number)
                val = val.split()[0] if val.split() else val
                return val[:20]
        return "N/A"

    # Escape summary for JS embedding
    summary_escaped = (summary
                       .replace("\\", "\\\\")
                       .replace("`", "\\`")
                       .replace("${", "\\${"))

    fragment = f"""<!-- Mercury CCC Dashboard Fragment — generated {run_date} -->
<!-- Injected by weekly_mercury.py into main dashboard -->
<script>
(function() {{
  // ── Build CCC panel ──────────────────────────────────────────────────────
  const cccSummaryMd = `{summary_escaped}`;

  const cccPanel = document.createElement('div');
  cccPanel.className = 'ticker-panel';
  cccPanel.id = 'panel-CCC';

  cccPanel.innerHTML = `
    <div style="margin-bottom:20px;">
      <span style="font-family:'Instrument Serif',serif;font-size:13px;
                   font-style:italic;color:var(--muted);">
        Mercury — Currencies, Cryptocurrencies &amp; Commodities &nbsp;·&nbsp; {run_date}
      </span>
    </div>

    <!-- CCC Summary Card -->
    <div style="background:var(--surface);border:1px solid var(--border);
                border-radius:6px;padding:24px;margin-bottom:20px;">
      <div style="font-size:12px;text-transform:uppercase;letter-spacing:1.5px;
                  color:#a78bfa;margin-bottom:14px;padding-bottom:8px;
                  border-bottom:1px solid var(--border);">
        ⚡ Mercury Weekly CCC Report
      </div>
      <div id="ccc-summary-body" style="font-family:'Instrument Serif',serif;
           font-size:16px;line-height:1.8;color:#dce8f0;"></div>
    </div>

    <!-- Raw Data Accordion -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">

      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:16px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                    color:var(--accent);margin-bottom:10px;">
          Forex &amp; DXY
        </div>
        <pre style="font-size:11px;color:var(--text);white-space:pre-wrap;
                    word-break:break-word;max-height:280px;overflow-y:auto;
                    line-height:1.6;">{forex_data[:2000] if forex_data else 'No data'}</pre>
      </div>

      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:16px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                    color:var(--accent2);margin-bottom:10px;">
          Crypto
        </div>
        <pre style="font-size:11px;color:var(--text);white-space:pre-wrap;
                    word-break:break-word;max-height:280px;overflow-y:auto;
                    line-height:1.6;">{crypto_data[:2000] if crypto_data else 'No data'}</pre>
      </div>

      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:16px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                    color:#a78bfa;margin-bottom:10px;">
          Metals
        </div>
        <pre style="font-size:11px;color:var(--text);white-space:pre-wrap;
                    word-break:break-word;max-height:280px;overflow-y:auto;
                    line-height:1.6;">{metals_data[:2000] if metals_data else 'No data'}</pre>
      </div>

      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:16px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                    color:#a78bfa;margin-bottom:10px;">
          Energy
        </div>
        <pre style="font-size:11px;color:var(--text);white-space:pre-wrap;
                    word-break:break-word;max-height:280px;overflow-y:auto;
                    line-height:1.6;">{energy_data[:2000] if energy_data else 'No data'}</pre>
      </div>

      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:16px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                    color:#a78bfa;margin-bottom:10px;">
          Baltic Dry Index
        </div>
        <pre style="font-size:11px;color:var(--text);white-space:pre-wrap;
                    word-break:break-word;max-height:280px;overflow-y:auto;
                    line-height:1.6;">{bdi_data[:1500] if bdi_data else 'No data'}</pre>
      </div>

      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:16px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                    color:#a78bfa;margin-bottom:10px;">
          COT Positioning
        </div>
        <pre style="font-size:11px;color:var(--text);white-space:pre-wrap;
                    word-break:break-word;max-height:280px;overflow-y:auto;
                    line-height:1.6;">{collected_data.get('cot_report', 'No data')[:2000]}</pre>
      </div>

    </div>
  `;

  document.getElementById('main').appendChild(cccPanel);

  // Render markdown summary after DOM insert
  setTimeout(() => {{
    const el = document.getElementById('ccc-summary-body');
    if (el && typeof marked !== 'undefined') {{
      el.innerHTML = marked.parse(cccSummaryMd);
    }} else if (el) {{
      el.textContent = cccSummaryMd;
    }}
  }}, 100);

  // ── Inject CCC nav button ────────────────────────────────────────────────
  const nav = document.getElementById('nav');
  if (nav) {{
    const btn = document.createElement('button');
    btn.id          = 'btn-CCC';
    btn.textContent = 'CCC';
    btn.style.color = '#a78bfa';
    btn.onclick = () => {{
      document.querySelectorAll('.ticker-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('nav button').forEach(b => {{
        b.classList.remove('active');
        b.style.borderBottomColor = '';
      }});
      document.getElementById('panel-CCC').classList.add('active');
      btn.classList.add('active');
      btn.style.borderBottomColor = '#a78bfa';
    }};
    // Insert after OUTLOOK button (SECTORS | DELTA | OUTLOOK | CCC | tickers)
    const outlookBtn = document.getElementById('btn-OUTLOOK');
    if (outlookBtn && outlookBtn.nextSibling) {{
      nav.insertBefore(btn, outlookBtn.nextSibling);
    }} else {{
      nav.insertBefore(btn, nav.firstChild);
    }}
  }}
}})();
</script>
"""
    return fragment

# ─── Mercury Trade Pitch ──────────────────────────────────────────────────────

def pitch_etf_trades(holdings: dict, etf_data: str, config: dict,
                     feedback: dict, macro_backdrop: str) -> list[dict]:
    """
    Ask Mercury to pitch ETF trades based on full portfolio holdings and
    fresh ETF data. Returns list of trade suggestion dicts.
    """
    if not holdings or not holdings.get("positions"):
        return []

    pos       = holdings["positions"]
    summary   = holdings.get("summary", {})
    total_val = summary.get("total_portfolio_value", 0)
    cash_val  = summary.get("cash_value", 0)
    cash_pct  = summary.get("cash_pct", 0)
    total_gain= summary.get("total_gain_loss_pct", 0)
    min_trade = config.get("trade_limits", {}).get("min_trade_dollars", 250000)
    max_trade = config.get("trade_limits", {}).get("max_trade_dollars", 1000000)
    max_etf_pct = config.get("position_limits", {}).get("max_etf_position_pct", 15.0)
    cash_floor  = config.get("cash_floor", {}).get("min_cash_pct", 10.0)
    allowed_etfs = config.get("mercury_allowed_etfs",
                               ["IAU", "BITQ", "VDE", "FBTC", "IBIT",
                                "GLD", "SLV", "PDBC", "DBA", "USO", "UNG",
                                "CPER", "VNQ", "IYR", "XLRE", "TLT", "HYG"])

    # Build ETF position summary
    etf_pos_lines = []
    for ticker, p in pos.items():
        if p.get("asset_type") == "etf":
            mv  = p.get("market_value", 0)
            gl  = p.get("gain_loss_pct", 0)
            wt  = (mv / total_val * 100) if total_val else 0
            etf_pos_lines.append(
                f"  {ticker:<6} ${mv:>10,.0f}  {wt:>5.1f}%  {gl:>+6.1f}%  "
                f"{p.get('shares',0):>10,.0f} shares @ ${p.get('avg_cost_per_share',0):.2f}"
            )

    # All equity positions for full context
    eq_lines = []
    sorted_pos = sorted(pos.items(), key=lambda x: x[1].get("market_value",0), reverse=True)
    for ticker, p in sorted_pos:
        if p.get("asset_type") != "etf":
            mv  = p.get("market_value", 0)
            wt  = (mv / total_val * 100) if total_val else 0
            eq_lines.append(f"  {ticker:<6} ${mv:>10,.0f}  {wt:>5.1f}%")

    # Prior Jansky feedback on ETF tickers
    feedback_lines = []
    for etf in allowed_etfs:
        fb = feedback.get(etf, {})
        if fb:
            feedback_lines.append(
                f"  {etf}: Jansky previously {fb.get('decision','?')} "
                f"a {fb.get('action','?')} pitch on {fb.get('date','?')} — "
                f"{fb.get('rationale','')[:120]}"
            )
    feedback_block = (
        "\nJANSKY'S PRIOR ETF TRADE FEEDBACK (last 2 weeks):\n"
        + "\n".join(feedback_lines)
    ) if feedback_lines else ""

    macro_block = f"\nMACRO BACKDROP:\n{macro_backdrop[:3000]}\n" if macro_backdrop else ""

    prompt = f"""You are Mercury, Obsidian Capital's Currencies, Cryptocurrencies
& Commodities analyst. You are pitching ETF trade recommendations to Jansky,
Head of AI Operations, who will approve or reject each one.

Write your pitches like a real analyst making the case. Be specific — cite
current price, trend, macro tailwinds, and portfolio context. Jansky enforces
limits; your job is to make the most compelling case the data supports.

═══════════════════════════════════════════════════════════════════════
PORTFOLIO CONTEXT (as of {holdings.get('last_updated','unknown')})
═══════════════════════════════════════════════════════════════════════
Total Portfolio: ${total_val:,.0f}
Cash (VMRXX):    ${cash_val:,.0f}  ({cash_pct:.1f}%)  ← Floor: {cash_floor:.0f}%
Total Gain/Loss: {total_gain:+.2f}%

CURRENT ETF POSITIONS:
  {'TICKER':<6} {'MKT VALUE':>10}  {'WT%':>5}  {'G/L%':>6}  {'SHARES':>10}  AVG COST
  {'─'*70}
{chr(10).join(etf_pos_lines) if etf_pos_lines else '  (no ETF positions)'}

EQUITY POSITIONS (for portfolio balance context):
{chr(10).join(eq_lines[:15])}

TRADE CONSTRAINTS (Jansky enforces):
  Min trade size:   ${min_trade:,.0f}
  Max trade size:   ${max_trade:,.0f}
  Max single ETF:   {max_etf_pct:.0f}% of portfolio
  Cash floor:       {cash_floor:.0f}% minimum
  Your ETF universe: {', '.join(allowed_etfs)}
═══════════════════════════════════════════════════════════════════════
{feedback_block}{macro_block}
TRADE PITCH RULES:
- Only pitch trades where CCC data gives you HIGH conviction
- Pitch 0-3 ETF trades maximum (quality over quantity)
- Dollar amounts must be between ${min_trade:,.0f} and ${max_trade:,.0f}
- ADD_TO_POSITION: ETF already held (IAU, BITQ, VDE)
- NEW_POSITION: ETF not currently held
- REDUCE_POSITION: ETF held, thesis weakening or overbought
- REQUIRED: before pitching anything, review our 3 held ETFs (IAU,
  BITQ, VDE) for signs one should be trimmed — overbought technicals,
  a weakening macro thesis, or a broken correlation to whatever trade
  justified the position originally. A REDUCE_POSITION pitch is just
  as valuable as a buy pitch and should be given equal weight, not
  treated as a fallback only considered when nothing else looks
  attractive.
- Cash is a scarce, shared resource across the whole portfolio: if the
  case for a new or added position isn't clearly stronger than the
  case for trimming a weaker held ETF, prefer proposing the reduce.
  The portfolio cannot fund every buy pitch without selling something
  first, and there have not been enough reduce candidates proposed to
  do that.
- If no compelling trade exists, respond with empty trades array

REQUIRED OUTPUT FORMAT — valid JSON only:
{{
  "trades": [
    {{
      "ticker": "GLD",
      "action": "NEW_POSITION",
      "dollars": 750000,
      "rationale": "Full analyst pitch — cite ETF data, macro tailwind, entry thesis. 3-5 sentences.",
      "conviction": "HIGH",
      "key_risk": "One sentence on biggest risk"
    }},
    {{
      "ticker": "IAU",
      "action": "REDUCE_POSITION",
      "dollars": 500000,
      "rationale": "Full analyst pitch for TRIMMING a held ETF — 3-5 sentences citing why it's overbought or the thesis has weakened, what changed since entry, and why now. Make the sell case as rigorously as a buy case.",
      "conviction": "HIGH",
      "key_risk": "One sentence on the biggest risk to trimming (e.g. selling too early if the trend re-accelerates)"
    }}
  ]
}}

ETF DATA (from get_etf_data tool):
{etf_data[:15000]}
"""

    print(f"    [trade pitch] asking Mercury...", end=" ", flush=True)
    try:
        result = subprocess.run(
            ["mercury", "-z", prompt, "--reasoning", "high"],
            capture_output=True, text=True, timeout=420
        )
        output = result.stdout.strip()
        if not output:
            print("✗ (no output)")
            return []

        # Parse JSON — try extracting from potential prose wrapper
        import re
        parsed = None
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            m = re.search(r'(\{.*\})', output, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group(1))
                except Exception:
                    pass

        if parsed is None:
            print("✗ (JSON parse failed)")
            return []

        trades = parsed.get("trades", [])
        print(f"✓ ({len(trades)} trade(s) pitched)")
        return trades

    except subprocess.TimeoutExpired:
        print("✗ (timeout)")
        return []
    except Exception as e:
        print(f"✗ ({e})")
        return []


# ─── ETF Dashboard Fragment ───────────────────────────────────────────────────

def generate_etf_fragment(etf_data: str, mercury_summary: str,
                          run_date: str) -> str:
    """
    Generate self-contained ETF tab HTML fragment.
    Shows all 15 ETFs with data cards and Mercury's summary.
    Injected into main dashboard by generate_dashboard().
    """
    fragment = f"""
<div id="panel-ETF" class="ticker-panel">
  <div style="padding:24px;max-width:1400px;margin:0 auto;">
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:2px;
                color:#a78bfa;margin-bottom:6px;">Mercury · ETF Watchlist</div>
    <div style="font-size:22px;font-weight:600;color:var(--text);
                margin-bottom:4px;">ETF Universe — {run_date}</div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:24px;">
      15 ETFs across commodities, real estate, and fixed income.
      IAU · BITQ · VDE are current portfolio holdings.
    </div>

    <!-- Mercury ETF Commentary -->
    <div style="background:var(--surface);border:1px solid #a78bfa44;
                border-radius:6px;padding:20px;margin-bottom:24px;">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                  color:#a78bfa;margin-bottom:12px;">⚡ Mercury — ETF Commentary</div>
      <div id="etf-mercury-body" style="font-size:13px;color:var(--text);
                                         line-height:1.7;white-space:pre-wrap;"></div>
    </div>

    <!-- Raw ETF Data -->
    <div style="background:var(--surface);border:1px solid var(--border);
                border-radius:6px;padding:20px;">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                  color:#a78bfa;margin-bottom:12px;">ETF Data</div>
      <pre style="font-size:11px;color:var(--text);white-space:pre-wrap;
                  word-break:break-word;line-height:1.6;max-height:600px;
                  overflow-y:auto;">{etf_data[:8000] if etf_data else 'No ETF data available'}</pre>
    </div>
  </div>
</div>

<script>
(function() {{
  const etfSummaryMd = {json.dumps(mercury_summary[:4000] if mercury_summary else 'No ETF commentary available.')};

  // Render markdown summary
  setTimeout(() => {{
    const el = document.getElementById('etf-mercury-body');
    if (el && typeof marked !== 'undefined') {{
      el.innerHTML = marked.parse(etfSummaryMd);
    }} else if (el) {{
      el.textContent = etfSummaryMd;
    }}
  }}, 100);

  // Inject ETF nav button — after TRADES if present, else after JANSKY
  const nav = document.getElementById('nav');
  if (nav) {{
    const btn = document.createElement('button');
    btn.id          = 'btn-ETF';
    btn.textContent = 'ETF';
    btn.style.color = '#a78bfa';
    btn.onclick = () => {{
      document.querySelectorAll('.ticker-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('nav button').forEach(b => {{
        b.classList.remove('active');
        b.style.borderBottomColor = '';
      }});
      document.getElementById('panel-ETF').classList.add('active');
      btn.classList.add('active');
      btn.style.borderBottomColor = '#a78bfa';
    }};
    const tradesBtn  = document.getElementById('btn-TRADES');
    const janskyBtn  = document.getElementById('btn-JANSKY');
    const cccBtn     = document.getElementById('btn-CCC');
    const anchor     = tradesBtn || janskyBtn || cccBtn;
    if (anchor && anchor.nextSibling) {{
      nav.insertBefore(btn, anchor.nextSibling);
    }} else {{
      nav.appendChild(btn);
    }}

    // Move panel into main
    const main = document.getElementById('main');
    const panel = document.getElementById('panel-ETF');
    if (panel && main && panel.parentNode !== main) main.appendChild(panel);
  }}
}})();
</script>
"""
    return fragment


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    import shutil
    os.makedirs(DATA_DIR,   exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    run_date = datetime.datetime.now().strftime('%Y-%m-%d')
    stamp    = datetime.datetime.now().strftime('%Y%m%d_%H%M')

    # Allow manual ticker/category override via CLI args (for reruns)
    args = sys.argv[1:]

    print(f"\n{'═'*52}")
    print(f"  Mercury — Weekly CCC Research Pipeline")
    print(f"  {run_date}")
    print(f"{'═'*52}\n")

    # ── Load config and resolve fragment paths ────────────────────────────
    config   = load_config()
    frag_dir = os.path.join(BASE_DIR, config.get("fragment_dir", "fragments"))
    os.makedirs(frag_dir, exist_ok=True)

    mercury_fragment_path = os.path.join(frag_dir, "mercury_dashboard_fragment.html")
    etf_fragment_path     = os.path.join(frag_dir, "etf_dashboard_fragment.html")
    # Legacy root paths kept in sync for rebuild_dashboard.sh
    legacy_mercury_frag   = os.path.join(BASE_DIR, "mercury_dashboard_fragment.html")
    legacy_etf_frag       = os.path.join(BASE_DIR, "etf_dashboard_fragment.html")

    # ── Load Atlas macro backdrop ─────────────────────────────────────────
    macro_backdrop = load_macro_backdrop()
    if macro_backdrop:
        print(f"  ✓ Atlas macro backdrop loaded ({len(macro_backdrop)} chars)")
    else:
        print(f"  ⚠ No macro backdrop found — run weekly_macro.py first")

    # ── Load Neptune holdings ─────────────────────────────────────────────
    holdings = {}
    if os.path.exists(NEPTUNE_HOLDINGS):
        try:
            with open(NEPTUNE_HOLDINGS) as f:
                holdings = json.load(f)
            n_pos     = len(holdings.get("positions", {}))
            total_val = holdings.get("summary", {}).get("total_portfolio_value", 0)
            print(f"  ✓ Neptune holdings loaded ({n_pos} positions, ${total_val:,.0f} total)")
        except Exception as e:
            print(f"  ⚠ Could not load Neptune holdings: {e}")

    # ── Load Jansky trade feedback ────────────────────────────────────────
    feedback = {}
    if os.path.exists(TRADE_FEEDBACK):
        try:
            with open(TRADE_FEEDBACK) as f:
                feedback = json.load(f)
            print(f"  ✓ Jansky trade feedback loaded ({len(feedback)} entries)")
        except Exception:
            pass

    # ── Collect all CCC data via mercurymcp ──────────────────────────────
    print(f"\n  {'─'*48}")
    print(f"  Collecting CCC data via mercurymcp (port 8645)")
    print(f"  {'─'*48}")

    collected = {}

    # Forex & Currencies
    print(f"\n  [Forex & Currencies]")
    collected["forex_rates"]       = run_tool("get_forex_rates")
    collected["central_bank_rates"]= run_tool("get_central_bank_rates")
    collected["money_supply"]      = run_tool("get_money_supply")

    # Cryptocurrencies
    print(f"\n  [Cryptocurrencies]")
    collected["crypto_prices"]     = run_tool("get_crypto_prices")
    collected["crypto_news"]       = run_tool("get_crypto_news")

    # ETFs (new — all 15 ETFs via dedicated tool)
    print(f"\n  [ETFs]")
    collected["etf_data"]          = run_tool("get_etf_data")

    # Energy
    print(f"\n  [Energy]")
    collected["energy_prices"]     = run_tool("get_energy_prices")

    # Metals
    print(f"\n  [Metals]")
    collected["metals_prices"]     = run_tool("get_metals_prices")

    # Agriculture
    print(f"\n  [Agriculture]")
    collected["agricultural_prices"]= run_tool("get_agricultural_prices")
    collected["livestock_prices"]   = run_tool("get_livestock_prices")
    collected["wasde_summary"]      = run_tool("get_wasde_summary")
    collected["crop_progress"]      = run_tool("get_crop_progress")
    collected["noaa_drought"]       = run_tool("get_noaa_drought")

    # Market Structure
    print(f"\n  [Market Structure & Positioning]")
    collected["cot_report"]        = run_tool("get_cot_report")
    collected["baltic_dry"]        = run_tool("get_baltic_dry")

    # Mercury backdrop YAML
    print(f"\n  [Mercury Backdrop]")
    collected["mercury_backdrop"]  = run_tool("build_mercury_backdrop")

    # ── Save backdrop YAML ────────────────────────────────────────────────
    backdrop_text = collected.get("mercury_backdrop", "")
    if backdrop_text and not backdrop_text.startswith("ERROR"):
        with open(MERCURY_BACKDROP, "w") as f:
            f.write(backdrop_text)
        dated_backdrop = f"{REPORT_DIR}/mercury_backdrop_{stamp}.yaml"
        with open(dated_backdrop, "w") as f:
            f.write(backdrop_text)
        print(f"\n  ✓ Mercury backdrop saved: {MERCURY_BACKDROP}")
    else:
        print(f"\n  ⚠ Mercury backdrop unavailable — check mercurymcp logs")

    # ── Generate LLM summary via Mercury profile ──────────────────────────
    print(f"\n  {'─'*48}")
    print(f"  Calling Mercury agent for weekly CCC synthesis...")
    print(f"  {'─'*48}")
    print(f"    [summary] asking Mercury...", end=" ", flush=True)

    summary = generate_mercury_summary(collected, macro_backdrop=macro_backdrop)
    print(f"✓ ({len(summary):,} chars)")

    # ── Mercury ETF trade pitches ─────────────────────────────────────────
    print(f"\n  {'─'*48}")
    print(f"  Mercury ETF Trade Pitches")
    print(f"  {'─'*48}")

    etf_trades = []
    if holdings:
        etf_trades = pitch_etf_trades(
            holdings, collected.get("etf_data", ""),
            config, feedback, macro_backdrop
        )
        # Tag each trade
        for trade in etf_trades:
            trade["pitched_by"] = "mercury"
            trade["pitched_at"] = datetime.datetime.now().isoformat()
            trade["run_date"]   = run_date
            trade["sector"]     = "ETF"
    else:
        print(f"    ⚠ No holdings data — skipping trade pitches")

    # ── Write Mercury section to trade_decisions.json ─────────────────────
    existing_decisions = {}
    if os.path.exists(TRADE_DECISIONS):
        try:
            with open(TRADE_DECISIONS) as f:
                existing_decisions = json.load(f)
        except Exception:
            existing_decisions = {}

    existing_decisions["mercury"] = {
        "run_date":     run_date,
        "generated_at": datetime.datetime.now().isoformat(),
        "trade_count":  len(etf_trades),
        "trades":       etf_trades,
    }
    existing_decisions["_meta"] = {
        "last_updated": datetime.datetime.now().isoformat(),
        "note": "Jupiter and Mercury pitches reviewed and approved/rejected by Jansky (Pass 21)"
    }

    with open(TRADE_DECISIONS, 'w') as f:
        json.dump(existing_decisions, f, indent=2)
    print(f"  ✓ Mercury trade pitches written to trade_decisions.json ({len(etf_trades)} trades)")

    # ── Save markdown summary ─────────────────────────────────────────────
    if summary and not summary.startswith("Mercury summary unavailable"):
        with open(MERCURY_SUMMARY, "w") as f:
            f.write(f"# Mercury Weekly CCC Report — {run_date}\n\n")
            f.write(summary)
        dated_summary = f"{REPORT_DIR}/mercury_summary_{stamp}.md"
        with open(dated_summary, "w") as f:
            f.write(f"# Mercury Weekly CCC Report — {run_date}\n\n")
            f.write(summary)
        print(f"  ✓ Mercury summary saved: {MERCURY_SUMMARY}")

    # ── Build and save full JSON ──────────────────────────────────────────
    output = {
        "run_date":      run_date,
        "generated_at":  datetime.datetime.now().isoformat(),
        "summary":       summary,
        "data":          collected,
    }

    json_path = f"{DATA_DIR}/mercury_{stamp}.json"
    with open(json_path, "w") as f:
        json.dump(output, f, indent=2)

    # mercury_latest.json — always points to current week
    with open(MERCURY_LATEST, "w") as f:
        json.dump(output, f, indent=2)

    print(f"  ✓ Data saved:    {json_path}")
    print(f"  ✓ Latest copy:   {MERCURY_LATEST}")

    # ── Generate CCC dashboard fragment ───────────────────────────────────
    fragment = generate_dashboard_fragment(summary, collected, run_date)
    with open(mercury_fragment_path, "w") as f:
        f.write(fragment)
    shutil.copy2(mercury_fragment_path, legacy_mercury_frag)  # keep root in sync

    dated_fragment = f"{REPORT_DIR}/mercury_fragment_{stamp}.html"
    with open(dated_fragment, "w") as f:
        f.write(fragment)
    print(f"  ✓ CCC fragment:  {mercury_fragment_path}")

    # ── Generate ETF dashboard fragment ───────────────────────────────────
    # Extract Mercury's ETF-specific commentary from summary if present
    etf_commentary = ""
    if "## 2. Cryptocurrencies" in summary or "ETF" in summary:
        # Use the full summary as context — ETF section will be in there
        etf_commentary = summary
    etf_frag = generate_etf_fragment(
        collected.get("etf_data", ""), etf_commentary, run_date
    )
    with open(etf_fragment_path, "w") as f:
        f.write(etf_frag)
    shutil.copy2(etf_fragment_path, legacy_etf_frag)  # keep root in sync

    dated_etf_frag = f"{REPORT_DIR}/etf_fragment_{stamp}.html"
    with open(dated_etf_frag, "w") as f:
        f.write(etf_frag)
    print(f"  ✓ ETF fragment:  {etf_fragment_path}")

    print(f"\n{'═'*52}")
    print(f"  Mercury pipeline complete.")
    print(f"  Next: python3 weekly_research.py")
    print(f"{'═'*52}\n")


if __name__ == "__main__":
    main()
