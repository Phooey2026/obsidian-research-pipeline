#!/usr/bin/env python3
"""
jansky_review.py — Jansky Weekly Agent Review
Head of AI Operations, Obsidian Capital

Runs 19 passes reviewing all four specialist agents:
  Pass 1:    Atlas macroeconomic review
  Pass 2:    Mercury CCC review
  Pass 3:    Nova legal & earnings review (with Python pre-checks)
  Pass 4-18: Jupiter sector reviews × 15 (sections 8-13 per ticker)
  Pass 19:   Cross-agent synthesis + Rankings + Delta

Outputs:
  data/jansky_YYYYMMDD_HHMM.json     dated archive
  jansky_latest.json                  current week (dashboard input)
  jansky_dashboard_fragment.html      injected by weekly_research.py
  reports/jansky_fragment_YYYYMMDD_HHMM.html  dated fragment archive

Run manually after the full weekly pipeline:
  python3 jansky_review.py

Options:
  --dry-run       Show what would be processed, no LLM calls
  --pass atlas    Run only the Atlas pass (for testing)
  --pass mercury  Run only the Mercury pass
  --pass nova     Run only the Nova pass
  --pass jupiter  Run only Jupiter passes (all sectors in watchlist.json)
  --pass trades   Run only the trade review pass (Pass 21)
  --pass synthesis Run only the synthesis pass (requires saved pass_notes)
  --sector "Semiconductors"  Run one Jupiter sector only
"""

import json
import os
import re
import sys
import datetime
import subprocess
import time

# ─── Configuration ────────────────────────────────────────────────────────────
BASE_DIR      = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
DATA_DIR      = f"{BASE_DIR}/data"
REPORT_DIR    = f"{BASE_DIR}/reports"

WATCHLIST     = f"{BASE_DIR}/watchlist.json"
NOVA_SUPP     = f"{BASE_DIR}/nova_supplemental.json"
MERCURY_LATEST= f"{BASE_DIR}/mercury_latest.json"
MACRO_BACKDROP= f"{BASE_DIR}/macro_backdrop.yaml"
MACRO_SUMMARY = f"{BASE_DIR}/macro_summary.md"
JANSKY_LATEST = f"{BASE_DIR}/jansky_latest.json"
NEPTUNE_HOLDINGS = f"{BASE_DIR}/neptune_holdings.json"
TRADE_DECISIONS  = f"{BASE_DIR}/trade_decisions.json"
TRADE_FEEDBACK   = f"{BASE_DIR}/jansky_trade_feedback.json"
CONFIG_FILE      = f"{BASE_DIR}/obsidian_config.json"

# Fragment paths resolved after config load
JANSKY_FRAGMENT  = None   # set in main() once config loaded
TRADES_FRAGMENT  = None   # set in main() once config loaded

# ─── Load Config ──────────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load obsidian_config.json. Returns defaults if missing."""
    defaults = {
        "trade_limits": {"min_trade_dollars": 250000, "max_trade_dollars": 1000000},
        "position_limits": {"max_etf_position_pct": 15.0, "max_equity_position_pct": 10.0},
        "cash_floor": {"min_cash_pct": 10.0},
        "staleness_thresholds": {"earnings_stale_days": 180, "legal_stale_days": 90},
        "trade_feedback": {"feedback_persist_weeks": 2},
        "fragment_dir": "fragments",
    }
    if not os.path.exists(CONFIG_FILE):
        return defaults
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return defaults

_config = load_config()

# Stale data thresholds from config
EARNINGS_STALE_DAYS = _config.get("staleness_thresholds", {}).get("earnings_stale_days", 180)
LEGAL_STALE_DAYS    = _config.get("staleness_thresholds", {}).get("legal_stale_days", 90)

# Section extraction: grab from "## 8." through end of summary
SECTION_START_PATTERN = re.compile(r'(## 8\.|##8\.)', re.IGNORECASE)

# Stale-data language Jupiter uses when discarding bad Nova data
STALE_LANGUAGE = [
    "not used", "stale", "pre-dates", "outdated", "ignored",
    "superseded", "discard", "too old", "historical only",
    "not current", "no longer",
]
# Note: year strings (2014, 2015, 2016, 2017) removed from STALE_LANGUAGE
# after Jansky's first run identified them as high false-positive sources.
# ABBV/2014, AMGN/2015-2016, NVDA/2017 are legitimate historical citations
# in legal/regulatory context — not indicators of stale Nova data.

# ─── CLI Parsing ──────────────────────────────────────────────────────────────
args        = sys.argv[1:]
DRY_RUN     = "--dry-run" in args
PASS_FILTER = None
SECTOR_FILTER = None

if "--pass" in args:
    idx = args.index("--pass")
    if idx + 1 < len(args):
        PASS_FILTER = args[idx + 1].lower()

if "--sector" in args:
    idx = args.index("--sector")
    if idx + 1 < len(args):
        SECTOR_FILTER = args[idx + 1]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _stamp() -> str:
    return datetime.datetime.now().strftime('%Y%m%d_%H%M')

def _today() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d')

def _days_ago(date_str: str) -> int:
    """Return how many days ago a YYYY-MM-DD date string was."""
    try:
        d = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
        return (datetime.datetime.now() - d).days
    except Exception:
        return 9999

def _earnings_recently_attempted(earnings: dict, window_days: int = 7) -> bool:
    """True if Nova attempted an earnings search within the last window_days,
    regardless of whether new data was actually found. Used to suppress a
    stale-call_date flag for a ticker Nova has already genuinely checked
    this week — a real recent attempt with nothing new to report is a
    different situation from a ticker nobody has looked at in months, even
    though both can show the same old call_date."""
    last_attempted = earnings.get("last_search_attempted", "")
    if not last_attempted:
        return False
    return _days_ago(last_attempted) <= window_days

def _find_latest_file(prefix: str, directory: str) -> str | None:
    """Find the most recent file matching prefix_*.json in directory."""
    try:
        files = sorted([
            f for f in os.listdir(directory)
            if f.startswith(prefix) and f.endswith('.json')
        ])
        return os.path.join(directory, files[-1]) if files else None
    except Exception:
        return None

def _extract_sections_8_13(summary: str) -> str:
    """
    Extract sections 8-13 + NOVA_FLAG_DATA from a Jupiter summary.
    Returns empty string if sections not found.
    """
    if not summary:
        return ""
    match = SECTION_START_PATTERN.search(summary)
    if match:
        return summary[match.start():].strip()
    # Fallback: take last 5000 chars (likely contains verdict sections)
    return summary[-5000:].strip()

def _check_stale_language(summary: str) -> list[str]:
    """
    Return list of stale-language phrases found in a summary, with context
    awareness to reduce false positives.

    Jansky first-run false positives identified:
    - "stale" in "stale valuation multiples" (Jupiter analytical prose)
    - "not current" in "not current consensus" (Jupiter analytical prose)
    - "outdated" in "outdated regulatory framework" (policy analysis)
    - "no longer" in "no longer a growth stock" (valuation language)

    Fix: require stale-language phrases to appear within 120 chars of a
    data-quality context word (nova, earnings, data, record, filing,
    transcript, api, feed) to avoid flagging analytical prose.
    """
    DATA_QUALITY_CONTEXT = [
        "nova", "earnings call", "transcript", "api", "data feed",
        "record", "filing", "sec", "edgar", "not used", "discarded",
        "pre-dates", "ignored", "superseded",
    ]
    found = []
    lower = summary.lower()
    for phrase in STALE_LANGUAGE:
        phrase_lower = phrase.lower()
        idx = 0
        while True:
            idx = lower.find(phrase_lower, idx)
            if idx == -1:
                break
            # Check 120-char window around the phrase for data quality context
            window_start = max(0, idx - 120)
            window_end   = min(len(lower), idx + len(phrase_lower) + 120)
            window       = lower[window_start:window_end]
            if any(ctx in window for ctx in DATA_QUALITY_CONTEXT):
                found.append(phrase)
                break  # Only flag once per phrase
            idx += len(phrase_lower)
    return found

def _call_jansky(prompt: str, pass_name: str) -> str:
    """
    Call the Jansky Hermes profile in oneshot mode.
    Returns the response text.
    """
    if DRY_RUN:
        return f"[DRY RUN — {pass_name} — no LLM call made]"

    print(f"    asking Jansky...", end=" ", flush=True)
    try:
        result = subprocess.run(
            ["jansky", "-z", prompt],
            capture_output=True, text=True, timeout=600
        )
        output = result.stdout.strip()
        if output:
            print(f"✓ ({len(output):,} chars)")
            return output
        err = result.stderr.strip()
        msg = (f"Jansky unavailable (exit {result.returncode}: {err[:200]})"
               if err else
               f"Jansky unavailable (exit {result.returncode}, no output)")
        print(f"✗ {msg[:80]}")
        return msg
    except FileNotFoundError:
        msg = "Jansky unavailable (jansky profile not found in PATH)"
        print(f"✗ {msg}")
        return msg
    except subprocess.TimeoutExpired:
        msg = "Jansky unavailable (timed out after 600s)"
        print(f"✗ {msg}")
        return msg
    except Exception as e:
        msg = f"Jansky unavailable: {e}"
        print(f"✗ {msg}")
        return msg

# ─── Data Loaders ─────────────────────────────────────────────────────────────

def load_all_data() -> dict:
    """Load all pipeline outputs needed for the review."""
    print("  Loading pipeline data...")

    result = {}

    # Watchlist
    with open(WATCHLIST) as f:
        wl = json.load(f)
    result["sectors"] = wl.get("sectors", wl)  # handle both structures
    # Compute dynamic counts — used in prompts to avoid hardcoded ticker/sector numbers
    result["n_sectors"] = len(result["sectors"])
    result["n_tickers"] = sum(len(v) for v in result["sectors"].values())
    print(f"    ✓ watchlist.json  — {result['n_sectors']} sectors, {result['n_tickers']} tickers")

    # Latest research JSON
    research_path = _find_latest_file("research_", DATA_DIR)
    if not research_path:
        print("    ✗ No research JSON found in data/")
        result["research"] = []
    else:
        with open(research_path) as f:
            result["research"] = json.load(f)
        result["research_path"] = research_path
        print(f"    ✓ {os.path.basename(research_path)}  — "
              f"{len(result['research'])} ticker records")

    # Build ticker→record lookup
    result["by_ticker"] = {r["ticker"]: r for r in result["research"]}

    # Nova supplemental
    if os.path.exists(NOVA_SUPP):
        with open(NOVA_SUPP) as f:
            result["nova"] = json.load(f)
        print(f"    ✓ nova_supplemental.json  — {len(result['nova'])} records")
    else:
        result["nova"] = {}
        print("    ⚠ nova_supplemental.json not found")

    # Mercury latest
    if os.path.exists(MERCURY_LATEST):
        with open(MERCURY_LATEST) as f:
            result["mercury"] = json.load(f)
        print(f"    ✓ mercury_latest.json  — {len(result['mercury'].get('summary',''))} chars")
    else:
        result["mercury"] = {}
        print("    ⚠ mercury_latest.json not found")

    # Atlas macro backdrop
    result["macro_backdrop"] = ""
    if os.path.exists(MACRO_BACKDROP):
        with open(MACRO_BACKDROP) as f:
            result["macro_backdrop"] = f.read()
        print(f"    ✓ macro_backdrop.yaml  — {len(result['macro_backdrop'])} chars")

    result["macro_summary"] = ""
    if os.path.exists(MACRO_SUMMARY):
        with open(MACRO_SUMMARY) as f:
            result["macro_summary"] = f.read()
        print(f"    ✓ macro_summary.md  — {len(result['macro_summary'])} chars")

    # Latest rankings JSON
    rankings_path = _find_latest_file("rankings_", DATA_DIR)
    if rankings_path:
        with open(rankings_path) as f:
            result["rankings"] = json.load(f)
        result["rankings_path"] = rankings_path
        print(f"    ✓ {os.path.basename(rankings_path)}")
    else:
        result["rankings"] = {}
        print("    ⚠ No rankings JSON found")

    # Neptune portfolio holdings (paper trading account)
    if os.path.exists(NEPTUNE_HOLDINGS):
        with open(NEPTUNE_HOLDINGS) as f:
            result["holdings"] = json.load(f)
        print(f"    ✓ neptune_holdings.json  — "
              f"{len(result['holdings'].get('positions', {}))} positions")
    else:
        result["holdings"] = {}
        print("    ⚠ neptune_holdings.json not found")

    # Trade decisions (Jupiter + Mercury pitches)
    if os.path.exists(TRADE_DECISIONS):
        with open(TRADE_DECISIONS) as f:
            result["trade_decisions"] = json.load(f)
        j_count = len(result["trade_decisions"].get("jupiter", {}).get("trades", []))
        m_count = len(result["trade_decisions"].get("mercury", {}).get("trades", []))
        print(f"    ✓ trade_decisions.json  — Jupiter: {j_count} trades, Mercury: {m_count} trades")
    else:
        result["trade_decisions"] = {}
        print("    ⚠ trade_decisions.json not found — run sector_ranking.py and weekly_mercury.py first")

    # Obsidian config (trade limits, position limits)
    result["config"] = _config

    return result

# ─── Pass 1: Atlas ────────────────────────────────────────────────────────────

def pass_atlas(data: dict) -> str:
    """Review Atlas macroeconomic output."""
    print("\n  ── Pass 1: Atlas ──────────────────────────────────────────")

    if not data.get("macro_backdrop") and not data.get("macro_summary"):
        note = "Atlas data not found — macro_backdrop.yaml and macro_summary.md missing."
        print(f"    ⚠ {note}")
        return note

    prompt = f"""You are Jansky, Head of AI Operations at Obsidian Capital.

You are reviewing this week's work by Atlas, your Macroeconomic Advisor.
Atlas produces the macro_backdrop.yaml and macro_summary.md that are injected
into every Jupiter stock summary and Mercury's CCC analysis.

Your job:
1. Is Atlas's macro stance internally consistent? Do the data points support
   the conclusions?
2. Are there any data gaps or anomalies Atlas should have flagged but didn't?
3. What are the 1-2 most important macro signals the team needs to be aware
   of this week?
4. Rate Atlas's output: STRONG / ADEQUATE / NEEDS IMPROVEMENT
5. If Atlas is doing well, say so warmly. If there are issues, be specific
   about what needs to improve.

Be concise — 200-250 words. You are writing a section of your weekly briefing
to Jay, the portfolio manager.

ATLAS MACRO BACKDROP:
{data['macro_backdrop'][:20000]}

ATLAS MACRO SUMMARY:
{data['macro_summary'][:20000]}
"""

    return _call_jansky(prompt, "Atlas")

# ─── Pass 2: Mercury ──────────────────────────────────────────────────────────

def pass_mercury(data: dict) -> str:
    """Review Mercury CCC output."""
    print("\n  ── Pass 2: Mercury ─────────────────────────────────────────")

    mercury = data.get("mercury", {})
    if not mercury:
        note = "Mercury data not found — mercury_latest.json missing."
        print(f"    ⚠ {note}")
        return note

    summary = mercury.get("summary", "")
    run_date = mercury.get("run_date", "unknown")

    # Extract Mercury's self-reported data gap section
    gap_section = ""
    if "Data gaps" in summary or "DATA GAPS" in summary:
        gap_match = re.search(
            r'(Data gaps?[^\n]*:.*?)(?=\n---|\n#|\Z)',
            summary, re.IGNORECASE | re.DOTALL
        )
        if gap_match:
            gap_section = gap_match.group(1)[:800]

    # Extract confidence rating if present
    confidence = "UNKNOWN"
    conf_match = re.search(
        r'[Aa]nalysis confidence[:\s]+([A-Z\-]+)',
        summary
    )
    if conf_match:
        confidence = conf_match.group(1).strip()

    prompt = f"""You are Jansky, Head of AI Operations at Obsidian Capital.

You are reviewing this week's work by Mercury, your Currencies,
Cryptocurrencies & Commodities analyst. Mercury covers the Three C's (CCC)
and produces a weekly report that is displayed on the Obsidian Capital
dashboard.

Mercury's self-reported confidence: {confidence}
Report date: {run_date}

Your job:
1. Is the data gap list acceptable, or are any gaps critical enough to
   affect the usefulness of Mercury's analysis?
2. Did Mercury use the Atlas macro backdrop appropriately as context?
3. Is Mercury's cross-market synthesis coherent and useful?
4. Rate Mercury's output: STRONG / ADEQUATE / NEEDS IMPROVEMENT
5. Note any specific gaps that should be resolved before next week's run,
   with a specific recommendation (e.g., "fix EIA series ID for RBOB").
6. If Mercury delivered strong work, acknowledge it.

Be concise — 200-250 words. Write as a section of your weekly briefing to Jay.

MERCURY'S SELF-REPORTED DATA GAPS:
{gap_section if gap_section else '(none explicitly flagged)'}

MERCURY FULL REPORT (first 200000 chars):
{summary[:20000]}
"""

    return _call_jansky(prompt, "Mercury")

# ─── Pass 3: Nova ─────────────────────────────────────────────────────────────

def pass_nova(data: dict) -> str:
    """Review Nova legal & earnings output with Python pre-checks."""
    print("\n  ── Pass 3: Nova ────────────────────────────────────────────")

    n_tickers = data.get("n_tickers", len(data.get("by_ticker", {})))
    nova = data.get("nova", {})
    if not nova:
        note = "Nova supplemental data not found."
        print(f"    ⚠ {note}")
        return note

    # ── Python pre-checks ────────────────────────────────────────────────
    stale_earnings  = []
    stale_legal     = []
    high_risk       = []
    low_confidence  = []
    missing_earnings= []
    missing_legal   = []

    for ticker, record in nova.items():
        # Earnings staleness
        earnings = record.get("earnings", {})
        call_date = earnings.get("call_date", "")
        if not call_date:
            missing_earnings.append(ticker)
        elif _days_ago(call_date) > EARNINGS_STALE_DAYS and not _earnings_recently_attempted(earnings):
            stale_earnings.append(
                f"{ticker} ({call_date}, {_days_ago(call_date)}d ago)"
            )

        # Legal staleness + risk
        legal = record.get("legal", {})
        legal_date = legal.get("research_date", "")
        if not legal_date:
            missing_legal.append(ticker)
        elif _days_ago(legal_date) > LEGAL_STALE_DAYS:
            stale_legal.append(
                f"{ticker} ({legal_date}, {_days_ago(legal_date)}d ago)"
            )

        risk = legal.get("risk_level", "")
        if risk in ("Critical", "High"):
            high_risk.append(
                f"{ticker} [{risk}] — {legal.get('flag_detail','')[:100]}"
            )

        conf = legal.get("research_confidence", "")
        if conf == "Low":
            low_confidence.append(ticker)

    # Print pre-check summary
    print(f"    Python pre-checks:")
    print(f"      Stale earnings (>{EARNINGS_STALE_DAYS}d): {len(stale_earnings)}")
    print(f"      Stale legal (>{LEGAL_STALE_DAYS}d):    {len(stale_legal)}")
    print(f"      High/Critical risk:              {len(high_risk)}")
    print(f"      Low confidence legal:            {len(low_confidence)}")

    # Build condensed Nova table for Jansky (top entries only)
    nova_table_lines = ["NOVA SUPPLEMENTAL SUMMARY (condensed):"]
    nova_table_lines.append(f"{'TICKER':<8} {'RISK':<10} {'CONF':<8} "
                             f"{'LEGAL DATE':<12} {'CALL DATE':<12} NOTES")
    nova_table_lines.append("─" * 80)

    for ticker, record in sorted(nova.items()):
        legal    = record.get("legal", {})
        earnings = record.get("earnings", {})
        risk     = legal.get("risk_level", "—")
        conf     = legal.get("research_confidence", "—")
        ldate    = legal.get("research_date", "—")[:10]
        cdate    = earnings.get("call_date", "—")[:10]
        note     = ""
        if risk in ("Critical", "High"):
            note += "⚠RISK "
        if ldate != "—" and _days_ago(ldate) > LEGAL_STALE_DAYS:
            note += "STALE-L "
        if cdate != "—" and _days_ago(cdate) > EARNINGS_STALE_DAYS:
            note += "STALE-E "
        nova_table_lines.append(
            f"{ticker:<8} {risk:<10} {conf:<8} {ldate:<12} {cdate:<12} {note}"
        )

    nova_table = "\n".join(nova_table_lines)

    prompt = f"""You are Jansky, Head of AI Operations at Obsidian Capital.

You are reviewing this week's work by Nova, your Legal Research &
Earnings Call Intelligence agent. Nova maintains legal risk profiles
and earnings call summaries for all {n_tickers} covered tickers.

PYTHON PRE-CHECK RESULTS (automated flags):

Stale Earnings Records (>{EARNINGS_STALE_DAYS} days old — need refresh):
{chr(10).join(stale_earnings[:20]) if stale_earnings else '  None'}

Stale Legal Records (>{LEGAL_STALE_DAYS} days old — need refresh):
{chr(10).join(stale_legal[:20]) if stale_legal else '  None'}

High/Critical Risk Tickers (Jupiter must be aware):
{chr(10).join(high_risk[:15]) if high_risk else '  None'}

Low Confidence Legal Research (may need re-run):
{', '.join(low_confidence[:20]) if low_confidence else 'None'}

Missing Earnings Data: {', '.join(missing_earnings[:10]) if missing_earnings else 'None'}
Missing Legal Data:    {', '.join(missing_legal[:10]) if missing_legal else 'None'}

Your job:
1. Assess the overall health of Nova's research coverage.
2. Call out any stale earnings records that are particularly concerning
   — especially if they involve high-risk tickers.
3. Are the HIGH/CRITICAL risk tickers being properly tracked?
4. Make specific refresh recommendations (e.g., "run nova_earnings_call.py
   on TICKER, TICKER, TICKER").
5. Rate Nova's coverage: STRONG / ADEQUATE / NEEDS IMPROVEMENT
6. Acknowledge strong coverage where it exists.

Be concise — 200-250 words. Write as a section of your weekly briefing to Jay.

{nova_table[:12000]}
"""

    return _call_jansky(prompt, "Nova")

# ─── Passes 4-18: Jupiter Sectors ─────────────────────────────────────────────

def pass_jupiter_sector(sector: str, tickers: list[str],
                        data: dict) -> tuple[str, dict]:
    """
    Review one Jupiter sector. Returns (jansky_notes, python_flags_dict).
    """
    by_ticker = data.get("by_ticker", {})
    nova      = data.get("nova", {})

    # ── Extract sections 8-13 per ticker ─────────────────────────────────
    ticker_sections = []
    python_flags    = {}

    for ticker in tickers:
        record  = by_ticker.get(ticker)
        flags   = []

        if not record:
            ticker_sections.append(
                f"── {ticker} ──\n[NO DATA — ticker not found in research JSON]\n"
            )
            python_flags[ticker] = ["MISSING FROM RESEARCH JSON"]
            continue

        summary = record.get("summary", "")

        # Use full summary — Jansky reviews Jupiter's complete analysis
        extracted = summary if summary else "[empty summary]"
        if not summary:
            flags.append("EMPTY SUMMARY — repair_summaries.py needed")

        # Check for missing verdict
        has_verdict = any(v in summary for v in
                          ["ACCUMULATE", "WATCH", "AVOID"])
        if not has_verdict:
            flags.append("MISSING VERDICT — repair_summaries.py needed")

        # Check for stale language Jupiter may have flagged
        stale_found = _check_stale_language(summary)
        if stale_found:
            # Cross-reference Nova earnings date
            nova_rec  = nova.get(ticker, {})
            nova_earnings = nova_rec.get("earnings", {})
            call_date = nova_earnings.get("call_date", "")
            if (call_date and _days_ago(call_date) > EARNINGS_STALE_DAYS
                    and not _earnings_recently_attempted(nova_earnings)):
                flags.append(
                    f"STALE DATA DETECTED in summary ({', '.join(stale_found[:3])}) "
                    f"+ Nova earnings date {call_date} is stale — "
                    f"run nova_earnings_call.py {ticker}"
                )
            elif stale_found:
                flags.append(
                    f"Stale-data language in summary: {', '.join(stale_found[:3])}"
                )

        # Check NOVA_FLAG_DATA
        nova_flag_match = re.search(
            r'NOVA_FLAG_DATA:\s*(\{[^}]+\})', summary
        )
        if nova_flag_match:
            try:
                flag_data = json.loads(nova_flag_match.group(1))
                needs_research = flag_data.get("needs_research", False)
                nova_rec = nova.get(ticker, {})
                legal = nova_rec.get("legal", {})
                legal_date = legal.get("research_date", "")
                if needs_research and not legal_date:
                    flags.append(
                        f"Jupiter flagged needs_research=true but Nova has "
                        f"no legal record — run nova_legal.py {ticker}"
                    )
                elif needs_research and legal_date and _days_ago(legal_date) > LEGAL_STALE_DAYS:
                    flags.append(
                        f"Jupiter flagged needs_research=true, Nova legal "
                        f"record is {_days_ago(legal_date)}d old — "
                        f"consider re-run nova_legal.py {ticker}"
                    )
            except Exception:
                pass
        else:
            flags.append("NOVA_FLAG_DATA line missing from summary")

        if flags:
            python_flags[ticker] = flags

        # Build the ticker block for Jansky
        ticker_block = (
            f"── {ticker} ──\n"
            + (f"⚠ AUTO-FLAGS: {' | '.join(flags)}\n" if flags else "")
            + extracted
            + "\n"
        )
        ticker_sections.append(ticker_block)

    # ── Assemble Nova context for this sector ─────────────────────────────
    sector_nova_context = []
    for ticker in tickers:
        nova_rec = nova.get(ticker, {})
        if nova_rec:
            legal    = nova_rec.get("legal", {})
            earnings = nova_rec.get("earnings", {})
            risk     = legal.get("risk_level", "—")
            cdate    = earnings.get("call_date", "—")[:10]
            sector_nova_context.append(
                f"  {ticker}: Legal={risk}, EarningsCall={cdate}"
            )

    nova_context = "\n".join(sector_nova_context)
    all_ticker_text = "\n\n".join(ticker_sections)

    # Estimate size and warn if large
    total_chars = len(all_ticker_text)
    print(f"    Context: {total_chars:,} chars across {len(tickers)} tickers (full summaries)")

    prompt = f"""You are Jansky, Head of AI Operations at Obsidian Capital.

You are reviewing Jupiter's weekly analysis for the {sector} sector
({len(tickers)} tickers: {', '.join(tickers)}).

You are seeing Jupiter's COMPLETE analysis for each ticker (all 13 sections),
plus any automated flags our system detected. Review the full report — pay
particular attention to sections 7 (Insider Activity), 8 (Institutional
Ownership), 9 (Legal & Regulatory), 11 (Data Gaps), 12 (Re-entry Signal),
and 13 (Overall Verdict).

NOVA COVERAGE FOR THIS SECTOR:
{nova_context}

Your review priorities:
1. VERDICT CONSISTENCY — does each ticker's narrative in sections 9-12
   logically support the verdict in section 13? Flag any mismatches.
2. DATA GAP QUALITY — did Jupiter properly acknowledge gaps in section 11?
   Are there obvious gaps Jupiter should have flagged but didn't?
3. NOVA INTEGRATION — for HIGH/CRITICAL risk tickers, did Jupiter's section
   9 properly incorporate Nova's findings?
4. ATLAS INTEGRATION — for tickers where macro context is highly relevant
   (e.g., rate-sensitive, China-exposed, commodity-dependent), did Jupiter
   reference the macro backdrop?
5. AUTOMATED FLAGS — address any ⚠ AUTO-FLAGS above with specific guidance.

Rate each ticker briefly (ONE LINE: ticker + OK / FLAG + one-sentence note).
Then provide a 3-5 sentence sector summary.

Do NOT re-summarize what Jupiter wrote. Focus on quality assessment.
Total response: 250-350 words.

JUPITER'S {sector.upper()} SECTOR ANALYSIS:
{all_ticker_text}
"""

    return _call_jansky(prompt, f"Jupiter/{sector}"), python_flags

# ─── Pass 19: Synthesis ───────────────────────────────────────────────────────

def pass_synthesis(pass_notes: dict, data: dict) -> str:
    """
    Final synthesis pass — fed only Jansky's own notes from passes 1-18.
    Also reviews rankings and delta.
    """
    print("\n  ── Pass 19: Synthesis + Rankings + Delta ────────────────────")

    # Build condensed rankings summary
    rankings_summary = ""
    rankings = data.get("rankings", {})
    if rankings:
        lines = ["SECTOR RANKINGS SUMMARY:"]
        # Handle various rankings JSON structures
        sectors_data = rankings if isinstance(rankings, dict) else {}
        if "sectors" in sectors_data:
            sectors_data = sectors_data["sectors"]

        for sector, sector_data in sectors_data.items():
            if isinstance(sector_data, dict):
                accum = sector_data.get("ACCUMULATE", [])
                avoid = sector_data.get("AVOID", [])
                lines.append(
                    f"  {sector:<15} ACCUMULATE: {', '.join(accum):<25} "
                    f"AVOID: {', '.join(avoid)}"
                )
        rankings_summary = "\n".join(lines)

    # Build verdict distribution from research
    verdict_dist = {"ACCUMULATE": 0, "WATCH": 0, "AVOID": 0, "MISSING": 0}
    for record in data.get("research", []):
        summary = record.get("summary", "")
        if "ACCUMULATE" in summary:
            verdict_dist["ACCUMULATE"] += 1
        elif "AVOID" in summary:
            verdict_dist["AVOID"] += 1
        elif "WATCH" in summary:
            verdict_dist["WATCH"] += 1
        else:
            verdict_dist["MISSING"] += 1

    # Build portfolio holdings summary (Neptune paper trading account)
    holdings_summary = ""
    holdings_data = data.get("holdings", {})
    if holdings_data and holdings_data.get("positions"):
        pos = holdings_data["positions"]
        summary = holdings_data.get("summary", {})
        total_val = summary.get("total_portfolio_value", 0)
        cash_val = summary.get("cash_value", 0)
        cash_pct = summary.get("cash_pct", 0)
        total_gain_pct = summary.get("total_gain_loss_pct", 0)

        # Top 10 positions by market value
        sorted_pos = sorted(pos.items(), key=lambda x: x[1].get("market_value", 0), reverse=True)
        top_lines = []
        for ticker, p in sorted_pos[:10]:
            mv = p.get("market_value", 0)
            gl = p.get("gain_loss_pct", 0)
            top_lines.append(f"    {ticker:<6} ${mv:>8,.0f}  ({gl:>+.1f}%)")
        top_str = "\n".join(top_lines)

        holdings_summary = (
            f"\nPORTFOLIO HOLDINGS (Neptune Paper Trading — {holdings_data.get('last_updated','unknown')}):\n"
            f"  Total Value: ${total_val:,.0f}  |  Cash: ${cash_val:,.0f} ({cash_pct:.1f}%)  |  "
            f"Total Gain: {total_gain_pct:+.2f}%\n"
            f"  Top 10 Positions:\n"
            f"{top_str}\n"
            f"  Total positions tracked: {len(pos)}\n"
        )

    # Compile all sector notes
    sector_notes_text = []
    for key, note in pass_notes.items():
        if key not in ("atlas", "mercury", "nova", "synthesis"):
            sector_notes_text.append(f"[{key}]\n{note}\n")

    n_tickers = data.get("n_tickers", len(data.get("by_ticker", {})))
    n_sectors = data.get("n_sectors", len(data.get("sectors", {})))

    prompt = f"""You are Jansky, Head of AI Operations at Obsidian Capital.

You have completed your weekly review of all four specialist agents.
Below are your notes from each review pass. Now write your FINAL
SYNTHESIS and EXECUTIVE BRIEFING for Jay, the portfolio manager.

VERDICT DISTRIBUTION (across {n_tickers} tickers):
  ACCUMULATE: {verdict_dist['ACCUMULATE']}
  WATCH:      {verdict_dist['WATCH']}
  AVOID:      {verdict_dist['AVOID']}
  MISSING:    {verdict_dist['MISSING']}

{rankings_summary[:10000]}

{holdings_summary}

YOUR ATLAS REVIEW NOTES:
{pass_notes.get('atlas', '[not available]')[:20000]}

YOUR MERCURY REVIEW NOTES:
{pass_notes.get('mercury', '[not available]')[:20000]}

YOUR NOVA REVIEW NOTES:
{pass_notes.get('nova', '[not available]')[:20000]}

YOUR JUPITER SECTOR REVIEW NOTES (all {n_sectors} sectors):
{chr(10).join(sector_notes_text)[:20000]}

Your synthesis must include:

1. EXECUTIVE BRIEFING (200-300 words in VP tone):
   A clear, direct briefing to Jay covering: overall portfolio posture,
   team performance this week, critical items requiring his attention,
   and your overall confidence in this week's research output.
   Be upbeat where warranted. Be direct about problems. This is your
   mic-drop moment — make it count.

2. CROSS-PIPELINE OBSERVATIONS (3-5 bullet points):
   Patterns visible only when looking across all agents simultaneously.
   Conflicts, confirmations, or gaps that span multiple agents.

3. PIPELINE RECOMMENDATIONS (numbered list):
   Specific actions before next Sunday's run. Format each as:
   "ACTION: [what to do] — REASON: [why]"
   Examples: re-run nova_earnings_call.py on X, fix FRED series ID for Y,
   investigate data gap Z, upgrade model for agent W.

4. OVERALL POSTURE: One of CONSTRUCTIVE / CAUTIOUS / MIXED / DEFENSIVE

Write with authority. You are the Head of AI Operations and this is
your weekly report to the boss.
"""

    return _call_jansky(prompt, "Synthesis")

# ─── Pass 21: Trade Review ────────────────────────────────────────────────────

def _salvage_ticker_entries(output: str) -> list[dict]:
    """
    Last-resort recovery: find every individual {"ticker": ...} object in
    the raw text and parse each one independently, using bracket-depth
    counting (not a greedy regex) to find each object's true boundaries.
    Same technique used in sector_ranking.py — a mid-generation
    self-narration monologue can break the overall JSON structure while
    individual ticker entries elsewhere in the same response are still
    perfectly intact. Discards only the specific entries that are
    themselves corrupted, rather than losing every decision because one
    entry (or the narration itself) broke the parse.
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


def pass_trade_review(data: dict) -> tuple[str, list[dict]]:
    """
    Review all Jupiter and Mercury trade pitches.
    Enforces position limits, cash floor, and trade size constraints.
    Returns (jansky_notes_str, list_of_decision_dicts).
    """
    print("\n  ── Pass 21: Trade Review ────────────────────────────────────────")

    trade_decisions = data.get("trade_decisions", {})
    holdings        = data.get("holdings", {})
    config          = data.get("config", _config)

    if not trade_decisions:
        note = "No trade_decisions.json found — run sector_ranking.py and weekly_mercury.py first."
        print(f"    ⚠ {note}")
        return note, []

    jupiter_trades = trade_decisions.get("jupiter", {}).get("trades", [])
    mercury_trades = trade_decisions.get("mercury", {}).get("trades", [])
    all_trades     = jupiter_trades + mercury_trades

    if not all_trades:
        note = "No trade pitches this week — Jupiter and Mercury found no compelling opportunities."
        print(f"    ℹ {note}")
        return note, []

    # ── Python pre-checks (hard rules Jansky enforces before LLM) ─────────
    pos        = holdings.get("positions", {}) if holdings else {}
    summary    = holdings.get("summary", {})   if holdings else {}
    total_val  = summary.get("total_portfolio_value", 0)
    cash_val   = summary.get("cash_value", 0)
    cash_pct   = summary.get("cash_pct", 0)
    min_trade  = config.get("trade_limits", {}).get("min_trade_dollars", 250000)
    max_trade  = config.get("trade_limits", {}).get("max_trade_dollars", 1000000)
    max_etf_pct   = config.get("position_limits", {}).get("max_etf_position_pct", 15.0)
    max_eq_pct    = config.get("position_limits", {}).get("max_equity_position_pct", 10.0)
    cash_floor_pct= config.get("cash_floor", {}).get("min_cash_pct", 10.0)

    # Total buy dollars pitched (for cash floor check)
    total_buy_dollars = sum(
        t.get("dollars", 0) for t in all_trades
        if t.get("action") in ("ADD_TO_POSITION", "NEW_POSITION")
    )
    post_buy_cash_pct = (
        ((cash_val - total_buy_dollars) / total_val * 100) if total_val else 0
    )

    # Pre-check flags per trade
    pre_flags = {}
    for trade in all_trades:
        ticker  = trade.get("ticker", "?")
        action  = trade.get("action", "?")
        dollars = trade.get("dollars", 0)
        flags   = []

        # Trade size
        if dollars < min_trade:
            flags.append(f"BELOW MIN TRADE SIZE (${dollars:,.0f} < ${min_trade:,.0f})")
        if dollars > max_trade:
            flags.append(f"EXCEEDS MAX TRADE SIZE (${dollars:,.0f} > ${max_trade:,.0f})")

        # Position limit check for buys
        if action in ("ADD_TO_POSITION", "NEW_POSITION") and total_val:
            curr_mv  = pos.get(ticker, {}).get("market_value", 0)
            post_mv  = curr_mv + dollars
            post_pct = post_mv / total_val * 100
            is_etf   = pos.get(ticker, {}).get("asset_type") == "etf" or trade.get("sector") == "ETF"
            limit    = max_etf_pct if is_etf else max_eq_pct
            if post_pct > limit:
                flags.append(
                    f"POSITION LIMIT BREACH: post-buy {post_pct:.1f}% > {limit:.0f}% max"
                    f" ({'ETF' if is_etf else 'equity'} limit)"
                )

        # ADD_TO_POSITION validation
        if action == "ADD_TO_POSITION" and ticker not in pos:
            flags.append(f"ADD_TO_POSITION but {ticker} not in holdings")

        # NEW_POSITION validation
        if action == "NEW_POSITION" and ticker in pos:
            flags.append(f"NEW_POSITION but {ticker} already held — should be ADD_TO_POSITION")

        # REDUCE validation
        if action == "REDUCE_POSITION" and ticker not in pos:
            flags.append(f"REDUCE_POSITION but {ticker} not in holdings")

        if flags:
            pre_flags[ticker] = flags

    # Cash floor check (aggregate)
    cash_warning = ""
    if total_buy_dollars > 0 and post_buy_cash_pct < cash_floor_pct:
        cash_warning = (
            f"⚠ CASH FLOOR WARNING: If all buy pitches approved (${total_buy_dollars:,.0f}), "
            f"cash drops to {post_buy_cash_pct:.1f}% — below {cash_floor_pct:.0f}% floor. "
            f"Jansky must prioritize which buys to approve."
        )
        print(f"    {cash_warning}")

    print(f"    Pre-checks: {len(all_trades)} trades, {len(pre_flags)} flagged")
    for ticker, flags in pre_flags.items():
        for f in flags:
            print(f"      ⚠ {ticker}: {f}")

    # ── Build trade summary for Jansky's LLM review ───────────────────────
    trade_lines = []
    for trade in all_trades:
        ticker  = trade.get("ticker", "?")
        action  = trade.get("action", "?")
        dollars = trade.get("dollars", 0)
        agent   = trade.get("pitched_by", "?").upper()
        rat     = trade.get("rationale", "")
        risk    = trade.get("key_risk", "")
        conv    = trade.get("conviction", "")
        sector  = trade.get("sector", "")
        flags   = pre_flags.get(ticker, [])

        curr_pos = pos.get(ticker, {})
        curr_mv  = curr_pos.get("market_value", 0)
        curr_gl  = curr_pos.get("gain_loss_pct", 0)
        curr_wt  = (curr_mv / total_val * 100) if total_val and curr_mv else 0

        pos_line = (
            f"  Current position: ${curr_mv:,.0f} ({curr_wt:.1f}%, {curr_gl:+.1f}%)"
            if curr_mv else "  Current position: None (new)"
        )

        flag_line = f"  ⚠ PRE-FLAGS: {' | '.join(flags)}" if flags else ""

        trade_lines.append(
            f"── {agent} pitches {action} on {ticker} (${dollars:,.0f}) [{sector}] ──\n"
            f"  Conviction: {conv}\n"
            f"{pos_line}\n"
            f"  Pitch: {rat}\n"
            f"  Key Risk: {risk}\n"
            f"{flag_line}"
        )

    trades_text = "\n\n".join(trade_lines)
    holdings_line = (
        f"Portfolio: ${total_val:,.0f} total | Cash: ${cash_val:,.0f} ({cash_pct:.1f}%) | "
        f"Floor: {cash_floor_pct:.0f}%"
        if total_val else "Portfolio data unavailable"
    )

    prompt = f"""You are Jansky, Head of AI Operations at Obsidian Capital.

You are reviewing this week's trade pitches from Jupiter (equities) and
Mercury (ETFs). Your job is to APPROVE or REJECT each pitch, then write
your feedback so Jupiter and Mercury can improve future pitches.

{holdings_line}
Total pitched buys: ${total_buy_dollars:,.0f}
{cash_warning}

Trade limits (hard rules):
  Min: ${min_trade:,.0f}  |  Max: ${max_trade:,.0f}
  Max single ETF: {max_etf_pct:.0f}%  |  Max single equity: {max_eq_pct:.0f}%
  Cash floor: {cash_floor_pct:.0f}% — do NOT approve buys that breach this

For EACH trade write your decision and rationale. Be specific — cite the
data, coach the agents, explain every rejection clearly.

TRADE PITCHES THIS WEEK:
{trades_text}
"""

    notes = _call_jansky(prompt, "Trade Review")

    # ── Step 2: dedicated JSON decisions call ──────────────────────────────
    # Separate call asking ONLY for structured JSON — no prose, no narrative.
    # This is far more reliable than asking Jansky to append JSON to prose.
    ticker_list = ", ".join(t.get("ticker", "?") for t in all_trades)
    json_prompt = f"""You just reviewed {len(all_trades)} trade pitches for Obsidian Capital.
The tickers reviewed were: {ticker_list}

Now output ONLY a JSON object — no prose, no explanation, no markdown fences.
Your entire response must be a single valid JSON object starting with {{ and ending with }}.

For each ticker provide your APPROVE or REJECT decision and a one-sentence rationale.
Base your decisions on the review you just completed and these hard constraints:
- Cash floor: {cash_floor_pct:.0f}% minimum (current cash: {cash_pct:.1f}%)
- Max single equity position: {max_eq_pct:.0f}% of portfolio
- Max single ETF position: {max_etf_pct:.0f}% of portfolio
- If approving all buys would breach the cash floor, reject the lowest-conviction ones first

Required format:
{{"decisions": [{{"ticker": "AVGO", "decision": "APPROVE", "rationale": "one sentence"}}, {{"ticker": "MU", "decision": "REJECT", "rationale": "one sentence"}}]}}

All {len(all_trades)} tickers must appear. "decision" must be exactly APPROVE or REJECT.
"""

    print(f"    [decisions JSON] asking Jansky...", end=" ", flush=True)
    json_response = _call_jansky(json_prompt, "Trade Decisions JSON")

    # ── Parse the dedicated JSON response ─────────────────────────────────
    def _try_parse(text: str) -> dict:
        """Try direct parse, then extract the outermost { } block."""
        attempts = [text]
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            attempts.append(m.group(0))
        for attempt in attempts:
            if not attempt:
                continue
            try:
                parsed = json.loads(attempt)
                result = {}
                for entry in parsed.get("decisions", []):
                    t = entry.get("ticker", "").upper()
                    if t:
                        result[t] = {
                            "decision":  entry.get("decision", "PENDING").upper(),
                            "rationale": entry.get("rationale", "")[:300],
                        }
                if result:
                    return result
            except (json.JSONDecodeError, AttributeError):
                continue
        return {}

    parsed_map     = _try_parse(json_response)
    all_responses  = [json_response]
    expected_count = len(all_trades)

    # ── Retry once with a stricter re-prompt if incomplete — either a
    # parse failure (empty) or a syntactically valid but short response
    # (e.g. only the pre-flagged tickers, ignoring the rest) — both need
    # a retry, not just the former, since a "successful" parse of a
    # handful of entries previously counted as full success and silently
    # dropped every other real decision.
    if len(parsed_map) < expected_count:
        print(f"retry(json)...", end=" ", flush=True)
        retry_prompt = json_prompt + (
            "\n\nCRITICAL: Your previous response was incomplete or could "
            "not be parsed as JSON. Every one of the tickers listed above "
            "must appear in the \"decisions\" array — do not stop early "
            "and do not only address tickers you consider flagged or "
            "problematic. Output ONLY the JSON object this time — nothing "
            "before it, nothing after it, no self-correction or second "
            "attempt at any field."
        )
        retry_response = _call_jansky(retry_prompt, "Trade Decisions JSON (retry)")
        all_responses.append(retry_response)
        retry_map = _try_parse(retry_response)
        # Merge (union) rather than replace — the two attempts may cover
        # different, non-identical subsets of tickers.
        for t, v in retry_map.items():
            parsed_map.setdefault(t, v)

    if len(parsed_map) >= expected_count:
        print(f"✓ ({len(parsed_map)} decisions parsed)")
    elif parsed_map:
        # Partial: still short after the retry. Try salvaging any
        # additional entries from raw text before accepting the gap.
        for resp in all_responses:
            for entry in _salvage_ticker_entries(resp):
                t = entry.get("ticker", "").upper()
                if t and t not in parsed_map:
                    parsed_map[t] = {
                        "decision":  entry.get("decision", "PENDING").upper(),
                        "rationale": entry.get("rationale", "")[:300],
                    }
        if len(parsed_map) >= expected_count:
            print(f"✓ ({len(parsed_map)} decisions parsed, via salvage)")
        else:
            print(f"⚠ ({len(parsed_map)} of {expected_count} decisions — rest PENDING)")
    else:
        # ── Last resort: salvage individual ticker entries from whichever
        # response has the most content, rather than losing every real
        # decision because the overall JSON structure broke somewhere.
        best_response = max(all_responses, key=len, default="")
        salvaged = _salvage_ticker_entries(best_response)
        for entry in salvaged:
            t = entry.get("ticker", "").upper()
            if t:
                parsed_map[t] = {
                    "decision":  entry.get("decision", "PENDING").upper(),
                    "rationale": entry.get("rationale", "")[:300],
                }
        if parsed_map:
            print(f"⚠ (salvaged {len(parsed_map)} of {expected_count} decisions)")
        else:
            print(f"✗ (JSON parse failed — all PENDING)")

    decisions = []
    for trade in all_trades:
        ticker    = trade.get("ticker", "?")
        entry     = parsed_map.get(ticker.upper(), {})
        decision  = entry.get("decision",  "PENDING")
        rationale = entry.get("rationale", "See full trade review")

        if decision not in ("APPROVE", "REJECT"):
            decision = "PENDING"

        # Hard override: reject any pre-flagged breach items automatically
        if ticker in pre_flags:
            for flag in pre_flags[ticker]:
                if any(kw in flag for kw in ("BREACH", "BELOW MIN", "EXCEEDS MAX",
                                              "not in holdings")):
                    decision  = "REJECT"
                    rationale = f"Auto-rejected: {flag}"
                    break

        decisions.append({
            "ticker":     ticker,
            "action":     trade.get("action"),
            "dollars":    trade.get("dollars"),
            "sector":     trade.get("sector"),
            "pitched_by": trade.get("pitched_by"),
            "pitched_at": trade.get("pitched_at"),
            "run_date":   trade.get("run_date"),
            "decision":   decision,
            "rationale":  rationale,
            "pre_flags":  pre_flags.get(ticker, []),
        })

        icon = "✅" if decision == "APPROVE" else ("❌" if decision == "REJECT" else "⏳")
        print(f"    {icon} {ticker}: {decision}")

    return notes, decisions


# ─── Write Trade Feedback ──────────────────────────────────────────────────────

def write_trade_feedback(decisions: list[dict], config: dict) -> None:
    """
    Write jansky_trade_feedback.json — per-ticker feedback for injection
    into next week's Jupiter and Mercury prompts.
    Prunes entries older than feedback_persist_weeks.
    """
    persist_weeks = config.get("trade_feedback", {}).get("feedback_persist_weeks", 2)
    cutoff = (datetime.datetime.now() - datetime.timedelta(weeks=persist_weeks)).isoformat()

    # Load existing
    existing = {}
    if os.path.exists(TRADE_FEEDBACK):
        try:
            with open(TRADE_FEEDBACK) as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    # Prune stale entries
    pruned = {
        ticker: entry for ticker, entry in existing.items()
        if entry.get("date", "") >= cutoff[:10]
    }

    # Add this week's decisions
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    for d in decisions:
        ticker = d.get("ticker", "?")
        pruned[ticker] = {
            "ticker":     ticker,
            "action":     d.get("action"),
            "dollars":    d.get("dollars"),
            "decision":   d.get("decision"),
            "rationale":  d.get("rationale"),
            "pitched_by": d.get("pitched_by"),
            "date":       today,
        }

    with open(TRADE_FEEDBACK, "w") as f:
        json.dump(pruned, f, indent=2)
    print(f"  ✓ Trade feedback written: {TRADE_FEEDBACK} ({len(pruned)} entries, {persist_weeks}w retention)")


# ─── Report Builder ───────────────────────────────────────────────────────────

def build_report(pass_notes: dict, python_flags: dict,
                 data: dict, stamp: str,
                 trade_decisions: list = None) -> dict:
    """Assemble the full jansky_report JSON structure."""

    synthesis = pass_notes.get("synthesis", "")

    # Extract overall posture from synthesis
    posture = "MIXED"
    for p in ["CONSTRUCTIVE", "CAUTIOUS", "DEFENSIVE", "MIXED"]:
        if p in synthesis.upper():
            posture = p
            break

    # Extract executive summary (first major paragraph of synthesis)
    exec_summary = synthesis if synthesis else ""

    # Verdict distribution
    verdict_dist = {"ACCUMULATE": 0, "WATCH": 0, "AVOID": 0, "MISSING": 0}
    for record in data.get("research", []):
        summary = record.get("summary", "")
        if "ACCUMULATE" in summary:
            verdict_dist["ACCUMULATE"] += 1
        elif "AVOID" in summary:
            verdict_dist["AVOID"] += 1
        elif "WATCH" in summary:
            verdict_dist["WATCH"] += 1
        else:
            verdict_dist["MISSING"] += 1

    # Sector notes for Jupiter
    sectors = list(data.get("sectors", {}).keys())
    by_sector = {}
    all_sector_flags = []
    for sector in sectors:
        note = pass_notes.get(sector, "")
        flags = python_flags.get(sector, {})
        status = "FLAG" if flags else "OK"
        # Upgrade to CRITICAL if any flag contains MISSING VERDICT
        if any("MISSING VERDICT" in str(f) for f in flags.values()):
            status = "CRITICAL"
        tickers_flagged = list(flags.keys())
        by_sector[sector] = {
            "status":                  status,
            "notes":                   note,
            "flags":                   flags,
            "tickers_needing_attention": tickers_flagged,
        }
        if tickers_flagged:
            all_sector_flags.extend(tickers_flagged)

    # Nova pre-check flags (recompute for report)
    nova       = data.get("nova", {})
    stale_earn = [t for t, r in nova.items()
                  if _days_ago(r.get("earnings", {}).get("call_date","")) > EARNINGS_STALE_DAYS
                  and not _earnings_recently_attempted(r.get("earnings", {}))]
    stale_leg  = [t for t, r in nova.items()
                  if _days_ago(r.get("legal", {}).get("research_date","")) > LEGAL_STALE_DAYS]
    high_risk  = [t for t, r in nova.items()
                  if r.get("legal", {}).get("risk_level","") in ("Critical","High")]
    low_conf   = [t for t, r in nova.items()
                  if r.get("legal", {}).get("research_confidence","") == "Low"]

    report = {
        "run_date":          _today(),
        "generated_at":      datetime.datetime.now().isoformat(),
        "research_source":   os.path.basename(data.get("research_path", "unknown")),
        "model_used":        "deepseek/deepseek-chat-v4-flash",
        "overall_posture":   posture,
        "executive_summary": exec_summary,
        "agent_reviews": {
            "atlas": {
                "status": "OK" if pass_notes.get("atlas") else "FLAG",
                "notes":  pass_notes.get("atlas", ""),
            },
            "mercury": {
                "status": "OK" if pass_notes.get("mercury") else "FLAG",
                "notes":  pass_notes.get("mercury", ""),
            },
            "nova": {
                "status":           "FLAG" if (stale_earn or stale_leg) else "OK",
                "notes":            pass_notes.get("nova", ""),
                "stale_earnings":   stale_earn,
                "stale_legal":      stale_leg,
                "high_risk_tickers":high_risk,
                "low_confidence":   low_conf,
            },
            "jupiter": {
                "by_sector":         by_sector,
                "synthesis":         pass_notes.get("synthesis", ""),
                "verdict_distribution": verdict_dist,
            },
        },
        "python_flags":             python_flags,
        "tickers_needing_attention": list(set(all_sector_flags)),
        "pipeline_recommendations": [],  # extracted by LLM in synthesis
        "pass_notes":               pass_notes,
        "trade_review": {
            "decisions":    trade_decisions or [],
            "approved":     [d for d in (trade_decisions or []) if d.get("decision") == "APPROVE"],
            "rejected":     [d for d in (trade_decisions or []) if d.get("decision") == "REJECT"],
        },
    }

    return report

# ─── Dashboard Fragment Builder ───────────────────────────────────────────────

def build_dashboard_fragment(report: dict, run_date: str) -> str:
    """Build the Jansky CCC-style dashboard tab fragment."""

    posture      = report.get("overall_posture", "MIXED")
    exec_summary = report.get("executive_summary", "")
    reviews      = report.get("agent_reviews", {})

    posture_colors = {
        "CONSTRUCTIVE": "#2dd4bf",
        "CAUTIOUS":     "#f59e0b",
        "MIXED":        "#a78bfa",
        "DEFENSIVE":    "#ef4444",
    }
    posture_color = posture_colors.get(posture, "#a78bfa")

    def status_badge(status: str) -> str:
        colors = {
            "OK":       ("#2dd4bf", "✓ OK"),
            "FLAG":     ("#f59e0b", "⚠ FLAG"),
            "CRITICAL": ("#ef4444", "✗ CRITICAL"),
        }
        color, label = colors.get(status, ("#6b7280", status))
        return (f'<span style="background:{color}22;color:{color};'
                f'padding:2px 8px;border-radius:3px;font-size:11px;'
                f'font-weight:600;letter-spacing:1px;">{label}</span>')

    atlas_status   = reviews.get("atlas", {}).get("status", "OK")
    mercury_status = reviews.get("mercury", {}).get("status", "OK")
    nova_status    = reviews.get("nova", {}).get("status", "OK")

    # Jupiter: worst status across sectors
    jup_statuses = [s.get("status", "OK")
                    for s in reviews.get("jupiter", {})
                              .get("by_sector", {}).values()]
    if "CRITICAL" in jup_statuses:
        jupiter_status = "CRITICAL"
    elif "FLAG" in jup_statuses:
        jupiter_status = "FLAG"
    else:
        jupiter_status = "OK"

    # Tickers needing attention
    flagged_tickers = report.get("tickers_needing_attention", [])

    # Nova stale/risk lists
    nova_review = reviews.get("nova", {})
    stale_earn  = nova_review.get("stale_earnings", [])
    high_risk   = nova_review.get("high_risk_tickers", [])

    # Build sector status rows for Jupiter accordion
    sector_rows = []
    by_sector = reviews.get("jupiter", {}).get("by_sector", {})
    for sector, sdata in by_sector.items():
        status  = sdata.get("status", "OK")
        flagged = sdata.get("tickers_needing_attention", [])
        notes   = sdata.get("notes", "")[:300].replace("`", "'")
        flag_str = f' — ⚠ {", ".join(flagged)}' if flagged else ""
        sector_rows.append(
            f'<div style="padding:6px 0;border-bottom:1px solid var(--border);">'
            f'{status_badge(status)} <span style="font-size:12px;color:var(--text);">'
            f'<strong>{sector}</strong>{flag_str}</span></div>'
        )

    sector_html = "\n".join(sector_rows)

    # Escape summary for JS
    exec_escaped = (exec_summary
                    .replace("\\", "\\\\")
                    .replace("`", "\\`")
                    .replace("${", "\\${"))

    atlas_notes   = reviews.get("atlas",{}).get("notes","").replace("`","'")
    mercury_notes = reviews.get("mercury",{}).get("notes","").replace("`","'")
    nova_notes    = reviews.get("nova",{}).get("notes","").replace("`","'")
    synth_notes   = reviews.get("jupiter",{}).get("synthesis","").replace("`","'")

    verdict_dist  = reviews.get("jupiter",{}).get("verdict_distribution",{})

    fragment = f"""<!-- Jansky Weekly Review Fragment — generated {run_date} -->
<script>
(function() {{
  const janskyExec = `{exec_escaped}`;

  const janskyPanel = document.createElement('div');
  janskyPanel.className = 'ticker-panel';
  janskyPanel.id = 'panel-JANSKY';

  janskyPanel.innerHTML = `
    <div style="margin-bottom:20px;">
      <span style="font-family:'Instrument Serif',serif;font-size:13px;
                   font-style:italic;color:var(--muted);">
        Jansky — Head of AI Operations &nbsp;·&nbsp; {run_date}
      </span>
    </div>

    <!-- Posture Header -->
    <div style="background:var(--surface);border:1px solid {posture_color}44;
                border-left:4px solid {posture_color};border-radius:6px;
                padding:16px 20px;margin-bottom:16px;
                display:flex;align-items:center;gap:16px;">
      <div>
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:2px;
                    color:{posture_color};margin-bottom:4px;">Overall Posture</div>
        <div style="font-size:22px;font-weight:700;color:{posture_color};">
          {posture}
        </div>
      </div>
      <div style="flex:1;font-size:12px;color:var(--muted);">
        Verdict Distribution: &nbsp;
        <span style="color:#2dd4bf;">▲ {verdict_dist.get('ACCUMULATE',0)} ACCUMULATE</span> &nbsp;
        <span style="color:var(--muted);">● {verdict_dist.get('WATCH',0)} WATCH</span> &nbsp;
        <span style="color:#ef4444;">▼ {verdict_dist.get('AVOID',0)} AVOID</span>
        {f'&nbsp; <span style="color:#f59e0b;">⚠ {verdict_dist.get("MISSING",0)} MISSING</span>' if verdict_dist.get('MISSING',0) > 0 else ''}
      </div>
    </div>

    <!-- Agent Status Row -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;
                gap:12px;margin-bottom:16px;">
      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:14px;text-align:center;">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.5px;
                    color:var(--muted);margin-bottom:8px;">Atlas</div>
        {status_badge(atlas_status)}
      </div>
      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:14px;text-align:center;">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.5px;
                    color:var(--muted);margin-bottom:8px;">Mercury</div>
        {status_badge(mercury_status)}
      </div>
      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:14px;text-align:center;">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.5px;
                    color:var(--muted);margin-bottom:8px;">Nova</div>
        {status_badge(nova_status)}
      </div>
      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:14px;text-align:center;">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.5px;
                    color:var(--muted);margin-bottom:8px;">Jupiter</div>
        {status_badge(jupiter_status)}
      </div>
    </div>

    <!-- Executive Briefing -->
    <div style="background:var(--surface);border:1px solid var(--border);
                border-radius:6px;padding:20px;margin-bottom:16px;">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                  color:#a78bfa;margin-bottom:12px;padding-bottom:8px;
                  border-bottom:1px solid var(--border);">
        ⚡ Executive Briefing
      </div>
      <div id="jansky-exec-body" style="font-family:'Instrument Serif',serif;
           font-size:16px;line-height:1.8;color:#dce8f0;"></div>
    </div>

    <!-- Two-column: Agent Notes + Flags -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">

      <!-- Agent Notes accordion -->
      <div style="background:var(--surface);border:1px solid var(--border);
                  border-radius:6px;padding:16px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                    color:#a78bfa;margin-bottom:12px;">Agent Reviews</div>

        <details style="margin-bottom:8px;">
          <summary style="cursor:pointer;font-size:12px;color:var(--text);
                          padding:4px 0;">
            {status_badge(atlas_status)} Atlas — Macroeconomics
          </summary>
          <div style="font-size:12px;color:var(--muted);padding:8px 0;
                      line-height:1.6;white-space:pre-wrap;">{atlas_notes}</div>
        </details>

        <details style="margin-bottom:8px;">
          <summary style="cursor:pointer;font-size:12px;color:var(--text);
                          padding:4px 0;">
            {status_badge(mercury_status)} Mercury — CCC
          </summary>
          <div style="font-size:12px;color:var(--muted);padding:8px 0;
                      line-height:1.6;white-space:pre-wrap;">{mercury_notes}</div>
        </details>

        <details style="margin-bottom:8px;">
          <summary style="cursor:pointer;font-size:12px;color:var(--text);
                          padding:4px 0;">
            {status_badge(nova_status)} Nova — Legal &amp; Earnings
          </summary>
          <div style="font-size:12px;color:var(--muted);padding:8px 0;
                      line-height:1.6;white-space:pre-wrap;">{nova_notes}</div>
        </details>

        <details>
          <summary style="cursor:pointer;font-size:12px;color:var(--text);
                          padding:4px 0;">
            {status_badge(jupiter_status)} Jupiter — Cross-Sector Synthesis
          </summary>
          <div style="font-size:12px;color:var(--muted);padding:8px 0;
                      line-height:1.6;white-space:pre-wrap;">{synth_notes}</div>
        </details>
      </div>

      <!-- Flags + Recommendations -->
      <div>
        <!-- Critical flags -->
        {'<div style="background:#ef444411;border:1px solid #ef444444;border-radius:6px;padding:16px;margin-bottom:12px;"><div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:#ef4444;margin-bottom:10px;">🔴 Tickers Needing Attention</div>' + ''.join(f'<div style="font-size:12px;color:#ef4444;padding:2px 0;">⚠ {t}</div>' for t in flagged_tickers[:20]) + '</div>' if flagged_tickers else ''}

        <!-- Stale earnings -->
        {'<div style="background:#f59e0b11;border:1px solid #f59e0b44;border-radius:6px;padding:16px;margin-bottom:12px;"><div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:#f59e0b;margin-bottom:10px;">⚠ Stale Earnings Records</div>' + ''.join(f'<div style="font-size:12px;color:#f59e0b;padding:2px 0;">{t}</div>' for t in stale_earn[:15]) + '</div>' if stale_earn else ''}

        <!-- High risk -->
        {'<div style="background:#ef444411;border:1px solid #ef444444;border-radius:6px;padding:16px;"><div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:#ef4444;margin-bottom:10px;">🔴 High/Critical Legal Risk</div>' + ''.join(f'<div style="font-size:12px;color:#ef4444;padding:2px 0;">{t}</div>' for t in high_risk[:10]) + '</div>' if high_risk else ''}
      </div>

    </div>

    <!-- Jupiter Sector Status Grid -->
    <div style="background:var(--surface);border:1px solid var(--border);
                border-radius:6px;padding:16px;">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                  color:#a78bfa;margin-bottom:12px;">Jupiter — Sector Review</div>
      {sector_html}
    </div>
  `;

  document.getElementById('main').appendChild(janskyPanel);

  // Render exec summary markdown
  setTimeout(() => {{
    const el = document.getElementById('jansky-exec-body');
    if (el && typeof marked !== 'undefined') {{
      el.innerHTML = marked.parse(janskyExec);
    }} else if (el) {{
      el.textContent = janskyExec;
    }}
  }}, 150);

  // Inject JANSKY nav button
  const nav = document.getElementById('nav');
  if (nav) {{
    const btn = document.createElement('button');
    btn.id          = 'btn-JANSKY';
    btn.textContent = 'JANSKY';
    btn.style.color = '#a78bfa';
    btn.onclick = () => {{
      document.querySelectorAll('.ticker-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('nav button').forEach(b => {{
        b.classList.remove('active');
        b.style.borderBottomColor = '';
      }});
      document.getElementById('panel-JANSKY').classList.add('active');
      btn.classList.add('active');
      btn.style.borderBottomColor = '#a78bfa';
    }};
    // Insert after CCC button if present, otherwise after OUTLOOK
    const cccBtn     = document.getElementById('btn-CCC');
    const outlookBtn = document.getElementById('btn-OUTLOOK');
    const anchor     = cccBtn || outlookBtn;
    if (anchor && anchor.nextSibling) {{
      nav.insertBefore(btn, anchor.nextSibling);
    }} else {{
      nav.insertBefore(btn, nav.firstChild);
    }}
  }}
}})();
</script>
"""
    return fragment

# ─── Save & Archive ───────────────────────────────────────────────────────────

def build_trades_fragment(decisions: list[dict], run_date: str) -> str:
    """
    Build the TRADES dashboard tab fragment.
    Shows Jupiter and Mercury trade pitches with Jansky's APPROVE/REJECT decisions.
    """
    if not decisions:
        approved_html = "<div style='color:var(--muted);font-size:13px;padding:20px;'>No trade pitches this week.</div>"
        rejected_html = ""
        summary_html  = "<div style='color:var(--muted);font-size:12px;'>No trades reviewed this week.</div>"
    else:
        approved = [d for d in decisions if d.get("decision") == "APPROVE"]
        rejected = [d for d in decisions if d.get("decision") == "REJECT"]
        pending  = [d for d in decisions if d.get("decision") == "PENDING"]

        total_approved_dollars = sum(d.get("dollars", 0) for d in approved
                                     if d.get("action") in ("ADD_TO_POSITION","NEW_POSITION"))
        total_reduce_dollars   = sum(d.get("dollars", 0) for d in approved
                                     if d.get("action") == "REDUCE_POSITION")

        summary_html = f"""
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px;">
          <div style="background:var(--surface);border:1px solid #22c55e44;border-radius:6px;padding:12px;text-align:center;">
            <div style="font-size:20px;font-weight:700;color:#22c55e;">{len(approved)}</div>
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);">Approved</div>
          </div>
          <div style="background:var(--surface);border:1px solid #ef444444;border-radius:6px;padding:12px;text-align:center;">
            <div style="font-size:20px;font-weight:700;color:#ef4444;">{len(rejected)}</div>
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);">Rejected</div>
          </div>
          <div style="background:var(--surface);border:1px solid #22c55e44;border-radius:6px;padding:12px;text-align:center;">
            <div style="font-size:16px;font-weight:700;color:#22c55e;">${total_approved_dollars:,.0f}</div>
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);">Approved Buys</div>
          </div>
          <div style="background:var(--surface);border:1px solid #f59e0b44;border-radius:6px;padding:12px;text-align:center;">
            <div style="font-size:16px;font-weight:700;color:#f59e0b;">${total_reduce_dollars:,.0f}</div>
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);">Approved Reduces</div>
          </div>
        </div>"""

        def trade_card(d: dict, border_color: str, badge_color: str,
                       badge_bg: str, badge_text: str) -> str:
            ticker  = d.get("ticker", "?")
            action  = d.get("action", "?").replace("_", " ")
            dollars = d.get("dollars", 0)
            agent   = d.get("pitched_by", "?").upper()
            sector  = d.get("sector", "")
            rat     = d.get("rationale", "")
            flags   = d.get("pre_flags", [])
            flag_html = (
                f'<div style="margin-top:8px;font-size:11px;color:#f59e0b;">'
                f'⚠ {" | ".join(flags)}</div>'
            ) if flags else ""
            return f"""
            <div style="background:var(--surface);border:1px solid {border_color};
                        border-radius:6px;padding:16px;margin-bottom:12px;">
              <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;">
                <div style="font-size:18px;font-weight:700;color:var(--text);">{ticker}</div>
                <div style="font-size:11px;background:{badge_bg};color:{badge_color};
                            padding:3px 8px;border-radius:4px;font-weight:600;">
                  {badge_text}
                </div>
                <div style="font-size:11px;color:var(--muted);">{action} · ${dollars:,.0f}</div>
                <div style="margin-left:auto;font-size:10px;color:var(--muted);">
                  {agent} · {sector}
                </div>
              </div>
              <div style="font-size:12px;color:var(--text);line-height:1.6;">{rat}</div>
              {flag_html}
            </div>"""

        approved_cards = "".join(
            trade_card(d, "#22c55e44", "#22c55e", "#22c55e22", "✅ APPROVED")
            for d in approved
        )
        rejected_cards = "".join(
            trade_card(d, "#ef444444", "#ef4444", "#ef444422", "❌ REJECTED")
            for d in rejected
        )
        pending_cards = "".join(
            trade_card(d, "#f59e0b44", "#f59e0b", "#f59e0b22", "⏳ PENDING")
            for d in pending
        )

        approved_html = approved_cards or "<div style='color:var(--muted);font-size:13px;'>No approved trades.</div>"
        rejected_html = (
            f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;'
            f'color:#ef4444;margin:20px 0 10px;">Rejected Pitches</div>'
            + rejected_cards + pending_cards
        ) if (rejected_cards or pending_cards) else ""

    fragment = f"""
<div id="panel-TRADES" class="ticker-panel">
  <div style="padding:24px;max-width:1400px;margin:0 auto;">
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:2px;
                color:#a78bfa;margin-bottom:6px;">Jansky · Trade Review</div>
    <div style="font-size:22px;font-weight:600;color:var(--text);
                margin-bottom:4px;">Trade Decisions — {run_date}</div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:24px;">
      Jupiter pitches equities · Mercury pitches ETFs · Jansky approves or rejects
    </div>

    {summary_html}

    <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                color:#22c55e;margin-bottom:10px;">Approved Trades</div>
    {approved_html}
    {rejected_html}
  </div>
</div>

<script>
(function() {{
  const nav = document.getElementById('nav');
  if (nav) {{
    const btn = document.createElement('button');
    btn.id          = 'btn-TRADES';
    btn.textContent = 'TRADES';
    btn.style.color = '#a78bfa';
    btn.onclick = () => {{
      document.querySelectorAll('.ticker-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('nav button').forEach(b => {{
        b.classList.remove('active');
        b.style.borderBottomColor = '';
      }});
      document.getElementById('panel-TRADES').classList.add('active');
      btn.classList.add('active');
      btn.style.borderBottomColor = '#a78bfa';
    }};
    // Insert after JANSKY button
    const janskyBtn = document.getElementById('btn-JANSKY');
    if (janskyBtn && janskyBtn.nextSibling) {{
      nav.insertBefore(btn, janskyBtn.nextSibling);
    }} else {{
      nav.appendChild(btn);
    }}
    const main = document.getElementById('main');
    const panel = document.getElementById('panel-TRADES');
    if (panel && main && panel.parentNode !== main) main.appendChild(panel);
  }}
}})();
</script>
"""
    return fragment


def save_outputs(report: dict, fragment: str, stamp: str,
                 jansky_fragment_path: str = None) -> None:
    """Save all Jansky outputs with archiving."""
    import shutil
    os.makedirs(DATA_DIR,   exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    fpath = jansky_fragment_path or os.path.join(BASE_DIR, "jansky_dashboard_fragment.html")
    legacy = os.path.join(BASE_DIR, "jansky_dashboard_fragment.html")

    # Dated archive JSON
    dated_json = f"{DATA_DIR}/jansky_{stamp}.json"
    with open(dated_json, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  ✓ Archived:  {dated_json}")

    # Latest JSON (overwritten)
    with open(JANSKY_LATEST, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  ✓ Latest:    {JANSKY_LATEST}")

    # Dashboard fragment (fragments/ dir)
    with open(fpath, "w") as f:
        f.write(fragment)
    if fpath != legacy:
        shutil.copy2(fpath, legacy)
    print(f"  ✓ Fragment:  {fpath}")

    # Dated fragment archive
    dated_frag = f"{REPORT_DIR}/jansky_fragment_{stamp}.html"
    with open(dated_frag, "w") as f:
        f.write(fragment)
    print(f"  ✓ Archived:  {dated_frag}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    stamp    = _stamp()
    run_date = _today()

    # Resolve fragment paths from config
    config   = _config
    frag_dir = os.path.join(BASE_DIR, config.get("fragment_dir", "fragments"))
    os.makedirs(frag_dir, exist_ok=True)
    jansky_fragment_path = os.path.join(frag_dir, "jansky_dashboard_fragment.html")
    trades_fragment_path = os.path.join(frag_dir, "trades_dashboard_fragment.html")
    # Legacy root paths kept in sync
    legacy_jansky_frag = os.path.join(BASE_DIR, "jansky_dashboard_fragment.html")
    legacy_trades_frag = os.path.join(BASE_DIR, "trades_dashboard_fragment.html")

    print(f"\n{'═'*56}")
    print(f"  Jansky — Weekly Agent Review")
    print(f"  {run_date}")
    if DRY_RUN:
        print(f"  *** DRY RUN — no LLM calls will be made ***")
    if PASS_FILTER:
        print(f"  Pass filter: {PASS_FILTER}")
    if SECTOR_FILTER:
        print(f"  Sector filter: {SECTOR_FILTER}")
    print(f"{'═'*56}\n")

    # ── Load data ─────────────────────────────────────────────────────────
    data = load_all_data()

    sectors    = data.get("sectors", {})
    sector_list= list(sectors.keys())
    pass_notes : dict[str, str] = {}
    python_flags: dict[str, dict] = {}
    trade_decisions_list: list[dict] = []

    # ── Try to load existing pass_notes (for --pass synthesis reruns) ─────
    if os.path.exists(JANSKY_LATEST):
        try:
            with open(JANSKY_LATEST) as f:
                existing = json.load(f)
            existing_notes = existing.get("pass_notes", {})
            if existing_notes and existing.get("run_date") == run_date:
                pass_notes = existing_notes
                print(f"  ✓ Loaded existing pass_notes from today's run")
        except Exception:
            pass

    # ── Pass 1: Atlas ─────────────────────────────────────────────────────
    if not PASS_FILTER or PASS_FILTER == "atlas":
        pass_notes["atlas"] = pass_atlas(data)
        time.sleep(2)

    # ── Pass 2: Mercury ───────────────────────────────────────────────────
    if not PASS_FILTER or PASS_FILTER == "mercury":
        pass_notes["mercury"] = pass_mercury(data)
        time.sleep(2)

    # ── Pass 3: Nova ──────────────────────────────────────────────────────
    if not PASS_FILTER or PASS_FILTER == "nova":
        pass_notes["nova"] = pass_nova(data)
        time.sleep(2)

    # ── Passes 4-19: Jupiter Sectors ──────────────────────────────────────
    if not PASS_FILTER or PASS_FILTER in ("jupiter", "all"):
        for i, sector in enumerate(sector_list):
            if SECTOR_FILTER and sector != SECTOR_FILTER:
                continue
            tickers = sectors[sector]
            print(f"\n  ── Pass {i+4}: Jupiter / {sector} "
                  f"({', '.join(tickers)}) ──────────")
            note, flags = pass_jupiter_sector(sector, tickers, data)
            pass_notes[sector]     = note
            python_flags[sector]   = flags
            time.sleep(2)

    # ── Pass 20: Synthesis ────────────────────────────────────────────────
    if not PASS_FILTER or PASS_FILTER == "synthesis":
        missing = [s for s in sector_list if s not in pass_notes]
        if missing and not SECTOR_FILTER:
            print(f"\n  ⚠ Missing sector notes for synthesis: {missing}")
            print(f"  Run full pass or --pass jupiter first")
        else:
            pass_notes["synthesis"] = pass_synthesis(pass_notes, data)

    # ── Pass 21: Trade Review ─────────────────────────────────────────────
    if not PASS_FILTER or PASS_FILTER in ("trades", "all"):
        trade_note, trade_decisions_list = pass_trade_review(data)
        pass_notes["trade_review"] = trade_note
        if trade_decisions_list and not DRY_RUN:
            write_trade_feedback(trade_decisions_list, config)
        time.sleep(2)

    # ── Build & Save ──────────────────────────────────────────────────────
    if not DRY_RUN:
        import shutil
        print(f"\n  {'─'*54}")
        print(f"  Building report and dashboard fragments...")

        report   = build_report(pass_notes, python_flags, data, stamp,
                                 trade_decisions=trade_decisions_list)
        fragment = build_dashboard_fragment(report, run_date)
        trades_frag = build_trades_fragment(trade_decisions_list, run_date)

        # Write Jansky fragment
        with open(jansky_fragment_path, "w") as f:
            f.write(fragment)
        shutil.copy2(jansky_fragment_path, legacy_jansky_frag)

        # Write Trades fragment
        with open(trades_fragment_path, "w") as f:
            f.write(trades_frag)
        shutil.copy2(trades_fragment_path, legacy_trades_frag)

        save_outputs(report, fragment, stamp,
                     jansky_fragment_path=jansky_fragment_path)

        print(f"\n{'═'*56}")
        print(f"  Jansky review complete. (21 passes)")
        print(f"  Posture: {report.get('overall_posture','UNKNOWN')}")
        flagged = report.get("tickers_needing_attention", [])
        if flagged:
            print(f"  Tickers needing attention: {', '.join(flagged[:10])}")
        approved = [d for d in trade_decisions_list if d.get("decision") == "APPROVE"]
        rejected = [d for d in trade_decisions_list if d.get("decision") == "REJECT"]
        print(f"  Trades: {len(approved)} approved, {len(rejected)} rejected")
        print(f"{'═'*56}\n")
    else:
        print(f"\n{'═'*56}")
        print(f"  DRY RUN complete — no files written.")
        print(f"  Sectors that would be reviewed: {', '.join(sector_list)}")
        print(f"  Total passes: 21")
        print(f"{'═'*56}\n")


if __name__ == "__main__":
    main()
