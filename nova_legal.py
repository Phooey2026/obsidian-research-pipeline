#!/usr/bin/env python3
"""
nova_legal.py — Nova Legal Research Agent
Obsidian Capital  |  Part of the Nova intelligence suite

Reads the most recent weekly_research JSON, finds companies Jupiter has
flagged for missing or insufficient legal data, and runs a deep-dive
research pass using the novamcp tools (port 8644).

Results are saved to nova_supplemental.json for injection into the
following week's weekly_research.py prompts.

Usage:
    python3 nova_legal.py                  # auto-detects latest research JSON
    python3 nova_legal.py TICKER [TICKER]  # research specific tickers only
    python3 nova_legal.py --json path.json # use a specific research JSON file

Run manually after weekly_research.py completes.
"""

import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time

import requests

# ─── Configuration ────────────────────────────────────────────────────────────
NOVAMCP_URL   = "http://localhost:8644/mcp"
BASE_DIR      = "/home/jay/stock_dashboard"
DATA_DIR      = f"{BASE_DIR}/data"
NOVA_JSON     = f"{BASE_DIR}/nova_supplemental.json"
STALE_DAYS    = 90

# Flag detection: text patterns Jupiter writes when flagging a company.
# These match against the Legal & Regulatory Risk section of the AI summary.
FLAG_PATTERNS = [
    r'NOVA_FLAG_DATA\s*:\s*(\{[^\}]+\})',          # structured flag (preferred)
    r'flagged?\s+for\s+(?:further|additional)\s+legal\s+research',
    r'legal\s+proceedings\s+(?:section\s+)?could\s+not\s+be\s+extracted',
    r'insufficient\s+(?:legal\s+)?data',
    r'no\s+(?:legal\s+)?data\s+(?:was\s+)?extracted',
    r'flag(?:ged)?\s+(?:for\s+)?(?:follow[- ]?up|more\s+research)',
]

# ─── Retry / MCP Helpers (mirrors weekly_research.py pattern) ─────────────────
_RETRYABLE = ("HTTP 500", "HTTP 429", "HTTP 503", "rate limit",
              "temporarily unavailable", "server error")


class _RetryableError(Exception):
    def __init__(self, snippet, wait):
        self.snippet = snippet
        self.wait    = wait
        super().__init__(snippet)


def call_tool(tool_name: str, arguments: dict,
              mcp_url: str = NOVAMCP_URL,
              retries: int = 3, backoff: float = 5.0) -> str:
    """Call a novamcp tool via StreamableHTTP MCP protocol."""
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json, text/event-stream"
    }
    last_result = "No data returned"

    for attempt in range(retries + 1):
        session = requests.Session()
        try:
            init_r = session.post(mcp_url, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities":    {},
                    "clientInfo":      {"name": "nova_legal", "version": "1.0"}
                }
            }, timeout=30)

            sid = init_r.headers.get("mcp-session-id", "")
            if sid:
                headers["mcp-session-id"] = sid

            session.post(mcp_url, headers=headers, json={
                "jsonrpc": "2.0",
                "method":  "notifications/initialized",
                "params":  {}
            }, timeout=10)

            r = session.post(mcp_url, headers=headers, json={
                "jsonrpc": "2.0", "id": 2,
                "method":  "tools/call",
                "params":  {"name": tool_name, "arguments": arguments}
            }, timeout=120)

            text   = r.text.strip()
            result = ""
            for line in text.split('\n'):
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                        for item in (data.get("result", {})
                                         .get("content", [])):
                            if item.get("type") == "text":
                                result += item.get("text", "")
                    except Exception:
                        continue
            if not result:
                try:
                    data = r.json()
                    for item in data.get("result", {}).get("content", []):
                        if item.get("type") == "text":
                            result += item.get("text", "")
                except Exception:
                    result = text

            result = result or "No data returned"

            result_upper = result.upper()
            if any(e.upper() in result_upper for e in _RETRYABLE) \
                    and attempt < retries:
                last_result = result
                wait = backoff * (2 ** attempt)
                raise _RetryableError(result[:80], wait)

            return result

        except _RetryableError:
            raise
        except Exception as e:
            last_result = f"ERROR: {e}"
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            return last_result

    return last_result


# ─── Nova JSON helpers ─────────────────────────────────────────────────────────
def load_nova_json() -> dict:
    if not os.path.exists(NOVA_JSON):
        return {}
    try:
        with open(NOVA_JSON, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def days_since(date_str: str) -> int:
    try:
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        return (datetime.date.today() - d).days
    except Exception:
        return 9999


def is_current(nova_data: dict, ticker: str) -> bool:
    """Return True if this ticker has a current (non-stale) legal record."""
    rec = nova_data.get(ticker.upper(), {})
    legal = rec.get("legal", {})
    rd = legal.get("research_date", "")
    if not rd:
        return False
    return days_since(rd) <= STALE_DAYS


# ─── Flag detection ────────────────────────────────────────────────────────────
def extract_structured_flag(summary: str) -> dict | None:
    """Try to parse a NOVA_FLAG_DATA JSON line from the summary."""
    m = re.search(r'NOVA_FLAG_DATA\s*:\s*(\{[^\n\}]+\})', summary)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def is_flagged(summary: str) -> tuple[bool, str, str]:
    """
    Returns (flagged, flag_type, flag_detail).
    flag_type: 'needs_data' | 'needs_research' | ''
    """
    if not summary:
        return False, "", ""

    # Prefer structured flag
    structured = extract_structured_flag(summary)
    if structured:
        nd = structured.get("needs_data", False)
        nr = structured.get("needs_research", False)
        detail = structured.get("flag_detail", "")
        if nd:
            return True, "needs_data", detail
        if nr:
            return True, "needs_research", detail
        return False, "", ""

    # Fallback: pattern matching on free text
    summary_lower = summary.lower()
    data_patterns = [
        'legal proceedings section could not be extracted',
        'insufficient legal data',
        'no legal data was extracted',
        'flagged for further legal research',
        'flagged for additional legal research',
    ]
    research_patterns = [
        'significant legal exposure',
        'serious legal issues',
        'critical legal risk',
        'high legal risk',
        'flagged for follow-up research',
        'requires additional legal research',
    ]
    for pat in data_patterns:
        if pat in summary_lower:
            return True, "needs_data", pat
    for pat in research_patterns:
        if pat in summary_lower:
            return True, "needs_research", pat

    return False, "", ""


# ─── Find latest research JSON ─────────────────────────────────────────────────
def find_latest_research_json() -> str | None:
    pattern = os.path.join(DATA_DIR, "research_*.json")
    files   = sorted(glob.glob(pattern))
    return files[-1] if files else None


# ─── LLM synthesis via Hermes ─────────────────────────────────────────────────
def synthesize_legal(ticker: str, company_name: str,
                     flag_type: str, flag_detail: str,
                     deep_result: str, news_result: str,
                     court_result: str, sec_result: str,
                     doj_result: str) -> dict:
    """
    Ask Hermes to synthesize all legal research findings into a
    nova_legal_summary, risk_level, and research_confidence.
    Returns dict with those three keys.
    """
    prompt = f"""You are Nova, Obsidian Capital's legal research AI assistant.
You have conducted deep legal research on {company_name} ({ticker.upper()}).
This company was flagged by Jupiter (our stock analyst) with flag type: {flag_type}.
Flag detail: {flag_detail or 'Not specified'}

Here is the research data gathered:

═══════════════════════════════════════════
SEC 10-K DEEP EXTRACTION
═══════════════════════════════════════════
{deep_result[:8000]}

═══════════════════════════════════════════
WEB / NEWS SEARCH RESULTS
═══════════════════════════════════════════
{news_result[:4000]}

═══════════════════════════════════════════
COURTLISTENER FEDERAL DOCKETS
═══════════════════════════════════════════
{court_result[:3000]}

═══════════════════════════════════════════
SEC ENFORCEMENT ACTIONS
═══════════════════════════════════════════
{sec_result[:3000]}

═══════════════════════════════════════════
DOJ PRESS RELEASES
═══════════════════════════════════════════
{doj_result[:3000]}

Based on ALL of the above research, provide:

1. A comprehensive legal summary paragraph (200-400 words) covering:
   - Active lawsuits, class actions, or regulatory investigations
   - DOJ, FTC, SEC, or other government enforcement activity
   - Estimated financial exposure if mentioned
   - Management's stated posture toward litigation
   - Any recent legal developments or settlements

2. Risk Level: choose one of: Low / Moderate / High / Critical
   - Low: No material litigation, routine ordinary-course matters only
   - Moderate: Some litigation present but manageable, no existential threat
   - High: Significant active litigation with material financial exposure
   - Critical: Existential legal threats, criminal exposure, or massive fines

3. Research Confidence: choose one of: High / Medium / Low
   - High: Multiple corroborating sources, clear picture
   - Medium: Some data available, some gaps
   - Low: Minimal data found, picture is unclear

Format your response EXACTLY as follows (no preamble):
SUMMARY: [your summary paragraph here]
RISK_LEVEL: [Low|Moderate|High|Critical]
CONFIDENCE: [High|Medium|Low]
"""
    try:
        result = subprocess.run(
            ["nova", "-z", prompt],
            capture_output=True, text=True, timeout=300
        )
        output = (result.stdout or "").strip()
        if not output:
            raise ValueError(f"nova returned no output (exit {result.returncode})")

        # Parse structured response.
        # Tolerates both plain and markdown-bold formats, e.g.:
        #   RISK_LEVEL: High
        #   **RISK_LEVEL:** High
        summary_match = re.search(
            r'SUMMARY:\s*(.*?)(?=\n\*{0,2}RISK_LEVEL:|\Z)', output,
            re.DOTALL | re.IGNORECASE)
        risk_match = re.search(
            r'\*{0,2}RISK_LEVEL:\*{0,2}\s*(Low|Moderate|High|Critical)',
            output, re.IGNORECASE)
        conf_match = re.search(
            r'\*{0,2}CONFIDENCE:\*{0,2}\s*(High|Medium|Low)',
            output, re.IGNORECASE)

        summary_text = summary_match.group(1).strip() if summary_match \
                       else output[:2000]
        risk_level   = risk_match.group(1).title() if risk_match \
                       else "Moderate"
        confidence   = conf_match.group(1).title() if conf_match \
                       else "Low"

        return {
            "nova_legal_summary":  summary_text,
            "risk_level":          risk_level,
            "research_confidence": confidence,
        }

    except subprocess.TimeoutExpired:
        return {
            "nova_legal_summary":  "Nova synthesis timed out — review raw data.",
            "risk_level":          "Moderate",
            "research_confidence": "Low",
        }
    except Exception as e:
        return {
            "nova_legal_summary":  f"Nova synthesis error: {e}",
            "risk_level":          "Moderate",
            "research_confidence": "Low",
        }


# ─── Per-ticker legal research ─────────────────────────────────────────────────
def research_legal(ticker: str, company_name: str,
                   flag_type: str, flag_detail: str) -> None:
    """Run the full legal research pipeline for one ticker and save to Nova JSON."""
    print(f"\n  {'─'*48}")
    print(f"  {ticker}  ({company_name})")
    print(f"  Flag: {flag_type}  |  {flag_detail or 'no detail'}")
    print(f"  {'─'*48}")

    today = datetime.date.today().isoformat()

    # ── Tool calls ─────────────────────────────────────────────────────────
    results = {}
    tools = [
        ("deep_sec",    "get_legal_proceedings_deep",
                        {"ticker": ticker}),
        ("news",        "search_legal_news",
                        {"ticker": ticker, "company_name": company_name,
                         "max_results": 10}),
        ("court",       "fetch_courtlistener",
                        {"company_name": company_name, "max_results": 10}),
        ("sec_enforce", "fetch_sec_enforcement",
                        {"ticker": ticker, "company_name": company_name}),
        ("doj",         "fetch_doj_press",
                        {"company_name": company_name, "ticker": ticker,
                         "max_results": 5}),
    ]

    for key, tool, args in tools:
        print(f"    [{key}]...", end=" ", flush=True)
        MAX_OUTER = 2
        data = None
        for outer in range(MAX_OUTER + 1):
            try:
                data = call_tool(tool, args)
                break
            except _RetryableError as e:
                if outer < MAX_OUTER:
                    print(f"↺ retrying ({e.snippet[:30]}), "
                          f"wait {e.wait:.0f}s...", end=" ", flush=True)
                    time.sleep(e.wait)
                else:
                    data = f"ERROR: max retries ({e.snippet[:60]})"
        if data is None:
            data = f"ERROR: no data returned"
        results[key] = data
        status = "✓" if not data.startswith("ERROR") else "✗"
        print(f"{status} ({len(data):,} chars)")
        time.sleep(3.0)     # be polite to external APIs / DDG rate limits

    # ── LLM synthesis ──────────────────────────────────────────────────────
    print(f"    [synthesis] asking Nova...", end=" ", flush=True)
    synthesis = synthesize_legal(
        ticker       = ticker,
        company_name = company_name,
        flag_type    = flag_type,
        flag_detail  = flag_detail,
        deep_result  = results.get("deep_sec", ""),
        news_result  = results.get("news", ""),
        court_result = results.get("court", ""),
        sec_result   = results.get("sec_enforce", ""),
        doj_result   = results.get("doj", ""),
    )
    print(f"✓  risk={synthesis['risk_level']}  "
          f"confidence={synthesis['research_confidence']}")

    # ── Save to Nova JSON ──────────────────────────────────────────────────
    record = {
        "ticker":       ticker,
        "company_name": company_name,
        "legal": {
            "flag_type":           flag_type,
            "flag_detail":         flag_detail,
            "research_date":       today,
            "nova_status":         "current",
            "sec_deep_result":     results.get("deep_sec", "")[:3000],
            "web_findings_raw":    results.get("news", "")[:2000],
            "courtlistener_raw":   results.get("court", "")[:2000],
            "sec_enforcement_raw": results.get("sec_enforce", "")[:2000],
            "doj_raw":             results.get("doj", "")[:1000],
            "nova_legal_summary":  synthesis["nova_legal_summary"],
            "risk_level":          synthesis["risk_level"],
            "research_confidence": synthesis["research_confidence"],
        }
    }

    print(f"    [save]...", end=" ", flush=True)
    save_result = call_tool("write_nova_record", {
        "ticker":      ticker,
        "record_json": json.dumps(record),
    })
    print(f"✓  {save_result[:80]}")


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'═'*52}")
    print(f"  Nova Legal Research Agent")
    print(f"  Obsidian Capital")
    print(f"  {datetime.date.today().isoformat()}")
    print(f"{'═'*52}\n")

    args = sys.argv[1:]

    # ── Determine research JSON ────────────────────────────────────────────
    research_json_path = None
    explicit_tickers   = []

    if "--json" in args:
        idx = args.index("--json")
        if idx + 1 < len(args):
            research_json_path = args[idx + 1]
            args = [a for i, a in enumerate(args)
                    if i != idx and i != idx + 1]

    # Remaining args treated as explicit tickers
    explicit_tickers = [t.upper() for t in args if not t.startswith("--")]

    if not research_json_path:
        research_json_path = find_latest_research_json()
        if not research_json_path:
            print(f"  ✗ No research JSON found in {DATA_DIR}")
            print(f"    Run weekly_research.py first, or use --json <path>")
            sys.exit(1)

    print(f"  Research data: {research_json_path}")

    # ── Load research data ─────────────────────────────────────────────────
    try:
        with open(research_json_path, "r") as f:
            research_data = json.load(f)
    except Exception as e:
        print(f"  ✗ Failed to load research JSON: {e}")
        sys.exit(1)

    print(f"  Loaded {len(research_data)} tickers from research file")

    # ── Load existing Nova data ────────────────────────────────────────────
    nova_data = load_nova_json()
    print(f"  Nova supplemental: {len(nova_data)} existing records")

    # ── Build company name map from research data ──────────────────────────
    # Extract company names from stock_info strings in the research data
    company_name_map: dict[str, str] = {}
    for item in research_data:
        ticker = item.get("ticker", "").upper()
        if not ticker:
            continue
        # Try to extract company name from stock_info field
        stock_info = item.get("data", {}).get("stock_info", "")
        name_match = re.search(r'===\s+(.+?)\s+\(', stock_info)
        if name_match:
            company_name_map[ticker] = name_match.group(1).strip()
        else:
            company_name_map[ticker] = ticker

    # ── Enrich company names via SEC CIK lookup for any that fell back ────
    # Tickers like "B" (Barrick) are too short for search tools to handle
    # without the full company name. Look them up from SEC's ticker file.
    needs_enrichment = [t for t, n in company_name_map.items() if n == t]
    if needs_enrichment:
        try:
            import urllib.request as _urllib
            req = _urllib.Request(
                'https://www.sec.gov/files/company_tickers.json',
                headers={'User-Agent': 'Jay jay@example.com'}
            )
            with _urllib.urlopen(req, timeout=15) as r:
                tickers_data = json.loads(r.read())
            sec_name_map = {
                entry.get('ticker', '').upper(): entry.get('title', '')
                for entry in tickers_data.values()
                if entry.get('ticker') and entry.get('title')
            }
            for t in needs_enrichment:
                if t in sec_name_map and sec_name_map[t]:
                    company_name_map[t] = sec_name_map[t]
                    print(f"  ✓ Resolved '{t}' → '{sec_name_map[t]}' via SEC CIK lookup")
        except Exception as e:
            print(f"  ⚠ SEC CIK name enrichment failed: {e}")

    # ── Find flagged companies ─────────────────────────────────────────────
    flagged: list[tuple[str, str, str, str]] = []   # (ticker, name, type, detail)

    if explicit_tickers:
        # CLI override: treat all specified tickers as needs_data
        for t in explicit_tickers:
            name = company_name_map.get(t, t)
            flagged.append((t, name, "needs_data", "Manually specified via CLI"))
        print(f"\n  ⚠ CLI override: {len(flagged)} ticker(s) specified manually")
    else:
        # Auto-detect from Jupiter's summaries
        for item in research_data:
            ticker  = item.get("ticker", "").upper()
            summary = item.get("summary", "")
            if not ticker or not summary:
                continue
            flagged_bool, flag_type, flag_detail = is_flagged(summary)
            if flagged_bool:
                name = company_name_map.get(ticker, ticker)
                flagged.append((ticker, name, flag_type, flag_detail))

        print(f"\n  Flagged companies found: {len(flagged)}")

    if not flagged:
        print("  ✓ No companies flagged for legal research this week.")
        print(f"{'═'*52}\n")
        return

    # ── Filter: skip current records ──────────────────────────────────────
    to_research: list[tuple] = []
    skipped:     list[str]   = []

    for ticker, name, flag_type, flag_detail in flagged:
        if is_current(nova_data, ticker):
            skipped.append(ticker)
        else:
            to_research.append((ticker, name, flag_type, flag_detail))

    if skipped:
        print(f"  Skipping {len(skipped)} ticker(s) with current Nova records: "
              f"{', '.join(skipped)}")
    print(f"  Researching {len(to_research)} ticker(s): "
          f"{', '.join(t for t, *_ in to_research)}\n")

    if not to_research:
        print("  ✓ All flagged companies already have current Nova research.")
        print(f"{'═'*52}\n")
        return

    # ── Research loop ──────────────────────────────────────────────────────
    success = 0
    errors  = 0

    for ticker, name, flag_type, flag_detail in to_research:
        try:
            research_legal(ticker, name, flag_type, flag_detail)
            success += 1
        except Exception as e:
            print(f"\n  ✗ Error researching {ticker}: {e}")
            errors += 1
        print()

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'═'*52}")
    print(f"  Nova Legal Research Complete")
    print(f"  Researched: {success}  |  Errors: {errors}  |  Skipped: {len(skipped)}")
    print(f"  Nova data: {NOVA_JSON}")
    print(f"{'═'*52}\n")


if __name__ == "__main__":
    main()
