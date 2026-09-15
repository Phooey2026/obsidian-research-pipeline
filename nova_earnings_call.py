#!/usr/bin/env python3
"""
nova_earnings_call.py — Nova Earnings Call Research Agent
Obsidian Capital  |  Part of the Nova intelligence suite

Fetches the most recent earnings call transcript for all 120 watchlist stocks,
synthesizes key highlights and management tone into a compact summary, and
saves results to nova_supplemental.json for injection into weekly_research.py.

Transcript sources (in order of preference):
  1. Quartr API  (requires QUARTR_API_KEY in .env)
  2. DDG search → fetch_url_text  (free fallback)

Records are refreshed when the stored call_date is older than STALE_DAYS (90).
Already-current records are skipped to avoid redundant token spend.

Usage:
    python3 nova_earnings_call.py                   # full watchlist
    python3 nova_earnings_call.py TICKER [TICKER]   # specific tickers only
    python3 nova_earnings_call.py --force TICKER    # force refresh even if current
    python3 nova_earnings_call.py --stale-only      # only refresh stale/pending
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
WATCHLIST_JSON= f"{BASE_DIR}/watchlist.json"
STALE_DAYS    = 85    # slightly under 90 to avoid missing calls on the edge
NO_DATA_RETRY_DAYS = 30  # cooldown before re-attempting search on a ticker
                         # where Nova already tried and found nothing —
                         # roughly half the watchlist consistently can't be
                         # found via wire-service search, so retrying weekly
                         # wastes Brave API calls for no benefit
SLEEP_BETWEEN = 2.0   # seconds between tickers (polite to APIs)

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
                    "clientInfo":      {
                        "name":    "nova_earnings_call",
                        "version": "1.0"
                    }
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
            }, timeout=150)

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


def earnings_is_current(nova_data: dict, ticker: str) -> bool:
    """Return True if this ticker has current (non-stale) earnings data.

    Staleness is measured from call_date (the actual earnings call being
    reported), not research_date (when Nova last touched the record) —
    a record can be "recently confirmed" by Nova while still citing an
    earnings call from a quarter or more ago.
    """
    rec      = nova_data.get(ticker.upper(), {})
    earnings = rec.get("earnings", {})

    # If the last attempt genuinely found nothing, respect a shorter,
    # separate cooldown before retrying — searching weekly for a ticker
    # that consistently can't be found via wire-service search wastes
    # API calls for no benefit. This is independent of the call_date
    # staleness check below.
    last_attempted = earnings.get("last_search_attempted", "")
    last_result    = earnings.get("last_search_result", "")
    if last_attempted and last_result == "no_new_data_found":
        if days_since(last_attempted) <= NO_DATA_RETRY_DAYS:
            return True  # still in cooldown — treat as "current", skip

    cd = earnings.get("call_date", "")
    if not cd:
        return False
    return days_since(cd) <= STALE_DAYS


# ─── Watchlist loader ──────────────────────────────────────────────────────────
def load_watchlist() -> list[str]:
    """Load all tickers from watchlist.json. Falls back to scanning data dir."""
    if os.path.exists(WATCHLIST_JSON):
        try:
            with open(WATCHLIST_JSON) as f:
                data = json.load(f)
            sectors = data.get("sectors", {})
            seen    = set()
            flat    = []
            for tickers in sectors.values():
                for t in tickers:
                    if t.upper() not in seen:
                        flat.append(t.upper())
                        seen.add(t.upper())
            if flat:
                return flat
        except Exception:
            pass

    # Fallback: pull tickers from the most recent research JSON
    pattern = os.path.join(DATA_DIR, "research_*.json")
    files   = sorted(glob.glob(pattern))
    if files:
        try:
            with open(files[-1]) as f:
                data = json.load(f)
            return [item["ticker"].upper() for item in data
                    if item.get("ticker")]
        except Exception:
            pass

    return []


def get_company_name(ticker: str) -> str:
    """Try to find a company name from the most recent research JSON.
    Falls back to SEC CIK lookup when the name can't be resolved.
    """
    name = ticker  # default

    pattern = os.path.join(DATA_DIR, "research_*.json")
    files   = sorted(glob.glob(pattern))
    if files:
        try:
            with open(files[-1]) as f:
                data = json.load(f)
            for item in data:
                if item.get("ticker", "").upper() == ticker.upper():
                    stock_info = item.get("data", {}).get("stock_info", "")
                    m = re.search(r'===\s+(.+?)\s+\(', stock_info)
                    if m:
                        name = m.group(1).strip()
                        break
        except Exception:
            pass

    # If name still equals ticker, the lookup failed — try SEC CIK file.
    # This handles ambiguous tickers like 'B' (Barrick) where stock_info
    # parsing returns nothing useful.
    if name == ticker:
        try:
            import urllib.request as _urllib
            req = _urllib.Request(
                'https://www.sec.gov/files/company_tickers.json',
                headers={'User-Agent': 'Jay jay@example.com'}
            )
            with _urllib.urlopen(req, timeout=15) as r:
                tickers_data = json.loads(r.read())
            for entry in tickers_data.values():
                if entry.get('ticker', '').upper() == ticker.upper():
                    sec_name = entry.get('title', '')
                    if sec_name:
                        name = sec_name.title()
                        break
        except Exception:
            pass

    return name


# ─── LLM synthesis via Hermes ─────────────────────────────────────────────────
def synthesize_earnings(ticker: str, company_name: str,
                        transcript_text: str,
                        call_date: str, fiscal_quarter: str,
                        source: str) -> str:
    """
    Ask Hermes to synthesize the earnings call into a compact summary
    for injection into Jupiter's weekly analysis.
    Returns the summary text string.
    """
    prompt = f"""You are Nova, Obsidian Capital's intelligence assistant.
You have retrieved the most recent earnings call for {company_name} ({ticker.upper()}).
Call Date: {call_date}
Fiscal Quarter: {fiscal_quarter}
Source: {source}

EARNINGS CALL CONTENT:
{transcript_text[:35000]}

Synthesize this earnings call into a focused summary for our stock analyst Jupiter.
Write 200-350 words covering:

1. **Management Tone**: Overall sentiment — confident, cautious, defensive, optimistic?
   Note any evasiveness on analyst questions.

2. **Key Financial Highlights**: Revenue, EPS, margins — beat/miss vs guidance,
   and whether guidance was raised, maintained, or lowered.

3. **Forward Guidance**: What management said about the next quarter and full year.
   Specific numbers if given.

4. **Strategic Themes**: Top 1-2 business priorities management emphasized
   (new products, cost cuts, AI investments, geographic expansion, etc.)

5. **Legal / Regulatory Mentions**: Any mention of lawsuits, investigations,
   regulatory headwinds, or government scrutiny. Note if completely absent.

6. **Analyst Reception**: What analysts pushed back on or probed most. 
   Any notable tension between management and the analyst community?

7. **Key Risk**: The single most important risk or concern raised on the call.

Be direct and analytical. This summary will be read by a financial analyst
making buy/hold/sell decisions. Do not pad or hedge unnecessarily.

After your summary, on two separate lines, output:
CALL_DATE: YYYY-MM-DD
FISCAL_QUARTER: Q1 2026
Use the actual values from the content. If unknown, estimate from context.
"""


# ─── Parse call date from transcript text ─────────────────────────────────────
def parse_call_date(transcript_text: str) -> str:
    """Try to extract a call date (YYYY-MM-DD) from transcript header text."""
    this_year = datetime.date.today().year
    patterns = [
        r'Call Date[:\s]+(\d{4}-\d{2}-\d{2})',
        r'(\b(?:January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+\d{1,2},?\s+\d{4})',
        # Abbreviated month, with or without a trailing period —
        # common in wire-service datelines (e.g. "Aug. 04, 2026").
        r'(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?'
        r'\s+\d{1,2},?\s+\d{4})',
        r'(\d{1,2}/\d{1,2}/\d{4})',
        r'(\d{4}-\d{2}-\d{2})',
    ]
    for pat in patterns:
        # Try every match for this pattern, not just the first — a bare
        # digit pattern (e.g. YYYY-MM-DD) can match an unrelated number
        # (article ID, price, etc.) before it reaches a real date.
        for m in re.finditer(pat, transcript_text[:2000], re.IGNORECASE):
            raw = m.group(1)
            # Normalize quirks strptime's %b/%B can't handle directly:
            # "Sept" isn't a valid %b token, a trailing period on the
            # month isn't either, and all-caps/lowercase datelines need
            # title-casing for %b/%B to match.
            norm = re.sub(r'\bSept\b', 'Sep', raw, flags=re.IGNORECASE)
            norm = re.sub(r'^([A-Za-z]+)\.', r'\1', norm)
            for candidate in {raw, norm, raw.title(), norm.title()}:
                for fmt in ('%B %d, %Y', '%B %d %Y', '%b %d, %Y',
                            '%b %d %Y', '%m/%d/%Y', '%Y-%m-%d'):
                    try:
                        parsed = datetime.datetime.strptime(candidate, fmt)
                        # Reject implausible years rather than trusting any
                        # match shaped like a date — a real earnings call
                        # date will always fall in this range.
                        if 2015 <= parsed.year <= this_year + 1:
                            return parsed.strftime('%Y-%m-%d')
                    except ValueError:
                        continue
    return datetime.date.today().isoformat()


def parse_fiscal_quarter(transcript_text: str) -> str:
    """Try to extract fiscal quarter from transcript header text."""
    m = re.search(
        r'(?:Fiscal Quarter|Q(?:uarter)?)[:\s]+'
        r'(Q[1-4]\s*\d{4}|\d{4}\s*Q[1-4])',
        transcript_text[:2000], re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Fallback: look for Q1/Q2/Q3/Q4 + year pattern anywhere in first 2000 chars
    m = re.search(r'(Q[1-4]\s*(?:20\d{2}|FY\s*\d{2}))', transcript_text[:2000])
    if m:
        return m.group(1)
    return "Most Recent Quarter"


# ─── Per-ticker earnings research ─────────────────────────────────────────────
def research_earnings(ticker: str, company_name: str,
                      force: bool = False) -> str:
    """
    Fetch and synthesize earnings call for one ticker.
    Returns status string: 'ok', 'skipped', 'no_data', 'error'
    """
    transcript_text  = ""
    call_date        = ""
    fiscal_quarter   = ""
    source           = ""
    source_url       = ""

    # ── Search for earnings press release ──────────────────────────────────
    # Retry once on a fully empty/rejected outcome — SearXNG engine
    # availability has been observed to fluctuate within minutes, so a
    # short delay + fresh search can recover from transient engine
    # suspension rather than permanently failing a ticker with good data.
    candidate_items = []
    picked_urls     = []
    for _search_attempt in range(2):
        print(f"    [search]...", end=" ", flush=True)
        search_result = call_tool("get_earnings_transcript_search", {
            "ticker":       ticker,
            "company_name": company_name,
        })

        if search_result.startswith("ERROR") or len(search_result) < 100:
            print(f"✗ no results")
            if _search_attempt == 0:
                print(f"    retrying search in 8s...")
                time.sleep(8)
                continue
            return "no_data"

        # Extract candidate URLs + metadata — skip known-bad domains
        _bad_domains = (
            'yahoo.com/quote', 'youtube.com', 'twitter.com', 'x.com',
            'morningstar.com', 'bloomberg.com', 'wsj.com', 'ft.com',
            'bankofamerica.com', 'jpmorgan.com', 'goldmansachs.com',
            'morganstanley.com', 'wellsfargo.com',
            'q4cdn.com', '.pdf',
            'wheatonpreciousmetals', 'wheaton.com',
            'wheaton-precious', 'wheaton_precious',
            'cnn.com',
            # JS-rendered, or otherwise unfetchable even via the Jina
            # Reader fallback — confirmed across multiple distinct URLs
            # on MSFT/BHP (seekingalpha.com, fool.com) and AAPL
            # (businesswire.com: 4 separate URLs spanning 2020-2026,
            # independently confirmed dead/404 via direct browser check —
            # these appear to be stale search-index entries for pages
            # BusinessWire has since taken down, not a fetch-tool issue).
            'seekingalpha.com', 'fool.com', 'businesswire.com',
        )

        candidate_items = []   # list of (url, title, description)
        seen_urls = set()
        for _m in re.finditer(
            r'\[(\d+)\]\s*(.+?)\n\s*URL:\s*(https?://\S+)(?:\n\s*(.*))?',
            search_result
        ):
            _url   = _m.group(3).strip()
            _title = _m.group(2).strip()
            _desc  = (_m.group(4) or "").strip()
            if _url not in seen_urls and not any(bad in _url for bad in _bad_domains):
                candidate_items.append((_url, _title, _desc))
                seen_urls.add(_url)

        if not candidate_items:
            print(f"✗ no usable URLs in search results")
            if _search_attempt == 0:
                print(f"    retrying search in 8s...")
                time.sleep(8)
                continue
            return "no_data"

        print(f"✓ ({len(candidate_items)} candidates)")
        for i, (url, title, desc) in enumerate(candidate_items, 1):
            print(f"      [{i}] {url}")

        # ── Nova picks the best URLs from search summaries ─────────────────
        # Lightweight call — Nova reads titles/descriptions only, picks 1-3 URLs
        # most likely to be actual earnings results, or declares none relevant.
        print(f"    [nova picks]...", end=" ", flush=True)

        _candidate_lines = "\n".join(
            f"[{i+1}] {url}\n    Title: {title}\n    Desc:  {desc or '(none)'}"
            for i, (url, title, desc) in enumerate(candidate_items)
        )

        _pick_prompt = f"""You are Nova, Obsidian Capital's intelligence assistant.
Select the best URL(s) for {company_name} ({ticker.upper()}) quarterly earnings data.

We want the MOST RECENT quarterly earnings press release or earnings call transcript
containing actual financial results: revenue, EPS, guidance, management commentary.

REJECT URLs that appear to be:
- An announcement of a future earnings date (not the results themselves)
- A CFO/executive appointment, dividend notice, or product launch
- Results for a DIFFERENT company that happens to mention {company_name}
- A news article summarizing results (we want the primary source)

If 1-3 URLs look like genuine earnings results for {company_name}, respond with JSON:
{{"picks": [1, 3], "reason": "one sentence explaining your choice"}}

If NONE appear to be genuine earnings results for {company_name}, respond with:
{{"picks": [], "reason": "brief explanation"}}

SEARCH RESULTS:
{_candidate_lines}

JSON only. No preamble."""

        picked_urls = []
        try:
            _pick_result = subprocess.run(
                ["nova", "-z", _pick_prompt],
                capture_output=True, text=True, timeout=120
            )
            _pick_raw = (_pick_result.stdout or "").strip()

            _pick_data = None
            _json_m = re.search(r'\{.*\}', _pick_raw, re.DOTALL)
            for _parse_attempt in [_pick_raw, _json_m.group(0) if _json_m else ""]:
                if not _parse_attempt:
                    continue
                try:
                    _pick_data = json.loads(_parse_attempt)
                    break
                except json.JSONDecodeError:
                    continue

            if _pick_data is None:
                print(f"✗ (JSON parse failed — fetching top 4)")
                picked_urls = [url for url, _, _ in candidate_items[:4]]
            else:
                _picks  = _pick_data.get("picks", [])
                _reason = _pick_data.get("reason", "")
                if not _picks:
                    print(f"✗ no relevant results ({_reason})")
                    if _search_attempt == 0:
                        print(f"    retrying search in 8s...")
                        time.sleep(8)
                        continue
                    return "no_data"
                for idx in _picks:
                    if 1 <= idx <= len(candidate_items):
                        picked_urls.append(candidate_items[idx - 1][0])
                if not picked_urls:
                    print(f"✗ (invalid indices)")
                    if _search_attempt == 0:
                        print(f"    retrying search in 8s...")
                        time.sleep(8)
                        continue
                    return "no_data"
                print(f"✓ ({len(picked_urls)} picked — {_reason[:80]})")

        except subprocess.TimeoutExpired:
            print(f"✗ (timeout — fetching top 4)")
            picked_urls = [url for url, _, _ in candidate_items[:4]]
        except Exception as e:
            print(f"✗ ({e} — fetching top 4)")
            picked_urls = [url for url, _, _ in candidate_items[:4]]

        break  # got usable picked_urls — no need for a second attempt

    # ── Fetch Nova's selected URLs ─────────────────────────────────────────
    _today     = datetime.date.today()
    _today_str = _today.isoformat()

    def _sanitize_sort_date(date_str: str) -> str:
        if not date_str or date_str == _today_str:
            return "1900-01-01"
        try:
            d = datetime.date.fromisoformat(date_str)
            if d > _today or d.year < 2020:
                return "1900-01-01"
            return date_str
        except ValueError:
            return "1900-01-01"

    candidates: list[tuple[str, str, str]] = []
    for _url in picked_urls:
        print(f"    [fetch {_url[:60]}]...", end=" ", flush=True)
        _result = call_tool("fetch_url_text", {"url": _url, "max_chars": 40000})

        # "No readable content found" means the page is likely JS-rendered
        # (a SPA/dynamic site) — a plain HTML fetch sees an empty shell.
        # Jina Reader renders in a real headless browser server-side and
        # is free for this volume (no API key needed under ~20 req/min).
        if _result and "No readable content found" in _result:
            print(f"(JS-rendered — trying Jina Reader)...", end=" ", flush=True)
            try:
                _jina_resp = requests.get(
                    f"https://r.jina.ai/{_url}", timeout=30,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; ObsidianCapitalBot/1.0)"}
                )
                if _jina_resp.status_code == 200 and len(_jina_resp.text) > 500:
                    _result = _jina_resp.text[:40000]
                else:
                    print(f"[jina: HTTP {_jina_resp.status_code}, "
                          f"{len(_jina_resp.text)} chars: "
                          f"{_jina_resp.text[:150]!r}]", end=" ", flush=True)
            except requests.RequestException as _jina_err:
                print(f"[jina error: {_jina_err}]", end=" ", flush=True)

        _ok = (
            _result
            and not _result.startswith("ERROR")
            and "404 Not Found" not in _result
            and "403 Forbidden" not in _result
            and "Client error" not in _result
            and "Payment Required" not in _result
            and "Just a moment" not in _result
            and len(_result) > 500
        )
        if _ok:
            if len(_result) < 2000:
                print(f"✗ (too short: {len(_result):,} chars — likely paywall)")
                continue
            _cdate     = parse_call_date(_result)
            _sort_date = _sanitize_sort_date(_cdate)
            flag = " → out of range, low priority" if (_sort_date == "1900-01-01" and _cdate != _today_str) else ""
            print(f"✓ ({len(_result):,} chars, date={_cdate}{flag})")
            candidates.append((_sort_date, _url, _result))
        else:
            if not _result:
                reason = "empty response"
            elif _result.startswith("ERROR"):
                reason = _result[:100]
            elif "404 Not Found" in _result:
                reason = "404 Not Found"
            elif "403 Forbidden" in _result:
                reason = "403 Forbidden"
            elif "Payment Required" in _result:
                reason = "402 Payment Required (paywall)"
            elif "Just a moment" in _result:
                reason = "Cloudflare bot-check page returned instead of content"
            elif "Client error" in _result:
                reason = _result[:100]
            elif len(_result) <= 500:
                _snippet = _result.strip().replace("\n", " ")[:200]
                reason = f"too short ({len(_result)} chars): {_snippet!r}"
            else:
                reason = f"unknown ({len(_result)} chars, no recognized failure marker)"
            print(f"✗ ({reason})")

    if not candidates:
        return "no_data"

    # Most recent valid date first
    candidates.sort(key=lambda c: c[0] if c[0] else "1900-01-01", reverse=True)
    fetch_result = candidates[0][2]
    fetch_url    = candidates[0][1]
    source_url   = candidates[0][1]
    print(f"    → Selected candidate dated {candidates[0][0]} ({fetch_url[:60]})")


    transcript_text = fetch_result
    call_date       = parse_call_date(fetch_result)
    fiscal_quarter  = parse_fiscal_quarter(fetch_result)
    source          = "web_search"

    if not transcript_text:
        return "no_data"

    # ── Step 3: LLM synthesis ──────────────────────────────────────────────
    print(f"    [synthesis] asking Nova...", end=" ", flush=True)

    # Run synthesis — Nova appends CALL_DATE: and FISCAL_QUARTER: lines
    # which synthesize_earnings parses then strips from the returned text.
    # We extract them from the raw subprocess output first via a helper.
    import re as _re
    import subprocess as _sp

    _prompt = f"""You are Nova, Obsidian Capital's intelligence assistant.
You have retrieved the most recent earnings call for {company_name} ({ticker.upper()}).
Auto-parsed call date (UNVERIFIED — may be wrong, check against the content below): {call_date}
Auto-parsed fiscal quarter (UNVERIFIED — may be wrong, check against the content below): {fiscal_quarter}
Source: {source}

EARNINGS CALL CONTENT:
{transcript_text[:35000]}

Synthesize this earnings call into a focused summary for our stock analyst Jupiter.
Write 200-350 words covering:

1. **Management Tone**: Overall sentiment — confident, cautious, defensive, optimistic?
   Note any evasiveness on analyst questions.

2. **Key Financial Highlights**: Revenue, EPS, margins — beat/miss vs guidance,
   and whether guidance was raised, maintained, or lowered.

3. **Forward Guidance**: What management said about the next quarter and full year.
   Specific numbers if given.

4. **Strategic Themes**: Top 1-2 business priorities management emphasized
   (new products, cost cuts, AI investments, geographic expansion, etc.)

5. **Legal / Regulatory Mentions**: Any mention of lawsuits, investigations,
   regulatory headwinds, or government scrutiny. Note if completely absent.

6. **Analyst Reception**: What analysts pushed back on or probed most.
   Any notable tension between management and the analyst community?

7. **Key Risk**: The single most important risk or concern raised on the call.

CRITICAL FORMATTING RULES:
- Begin your response IMMEDIATELY with "**Management Tone**:" — no preamble,
  no "Here is the summary", no "Now I have everything", no meta-commentary.
- Do not narrate your reasoning process. Output only the finished summary.
- Be direct and analytical. This is institutional research, not commentary.

After your summary, on two separate lines, output:
CALL_DATE: YYYY-MM-DD
FISCAL_QUARTER: Q1 2026
Derive these from the actual content above — the auto-parsed values given
earlier may be wrong (e.g. from a misread URL or article ID) and should
not be trusted without checking against what the article itself states.
"""

    try:
        _result = _sp.run(
            ["nova", "-z", _prompt],
            capture_output=True, text=True, timeout=300
        )
        _raw = (_result.stdout or "").strip()
    except Exception as e:
        _raw = f"Nova earnings synthesis error: {e}"

    # Extract structured metadata before stripping.
    # Use strict patterns to avoid matching template text from the prompt.
    _date_match = _re.search(r'CALL_DATE:\s*(\d{4}-\d{2}-\d{2})', _raw)
    _fq_match   = _re.search(
        r'FISCAL_QUARTER:\s*(Q[1-4]\s*\d{4}|\d{4}\s*Q[1-4]|FY\s*\d{2,4})',
        _raw, _re.IGNORECASE)

    if _date_match:
        call_date = _date_match.group(1).strip()
    if _fq_match:
        fiscal_quarter = _fq_match.group(1).strip()

    # Strip metadata lines from the summary text
    summary = _re.sub(r'\n*CALL_DATE:\s*\S+\s*', '', _raw).strip()
    summary = _re.sub(r'\n*FISCAL_QUARTER:\s*.+$', '', summary,
                      flags=_re.MULTILINE).strip()
    if not summary:
        summary = "Nova earnings synthesis returned no content."

    # Nova can produce a well-formed CALL_DATE/FISCAL_QUARTER pair while
    # honestly stating in its own prose that it had nothing real to work
    # from (e.g. a JS-rendered IR page that passed the length check but
    # contained no actual transcript). That combination — confident
    # metadata + a self-admitted-empty summary — would otherwise pass
    # every other check in this function and get saved as "current".
    _failure_signatures = (
        "cannot be assessed",
        "no actual transcript",
        "no transcript content",
        "no content was retrieved",
        "no earnings content",
        "unable to assess",
        "could not be retrieved",
        "no substantive content",
    )
    _summary_lower = summary.lower()
    if any(sig in _summary_lower for sig in _failure_signatures):
        print(f"✗ (Nova reported no usable content in its own summary)")
        return "no_data"

    print(f"✓ ({len(summary):,} chars)")

    # ── Step 4: Save to Nova JSON ──────────────────────────────────────────
    today  = datetime.date.today().isoformat()
    record = {
        "ticker":       ticker,
        "company_name": company_name,
        "earnings": {
            "call_date":             call_date,
            "fiscal_quarter":        fiscal_quarter,
            "source":                source,
            "source_url":            source_url,
            "research_date":         today,
            "nova_status":           "current",
            "nova_earnings_summary": summary,
        }
    }

    print(f"    [save]...", end=" ", flush=True)
    save_result = call_tool("write_nova_record", {
        "ticker":      ticker,
        "record_json": json.dumps(record),
    })
    print(f"✓")
    return "ok"


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'═'*52}")
    print(f"  Nova Earnings Call Research Agent")
    print(f"  Obsidian Capital")
    print(f"  {datetime.date.today().isoformat()}")
    print(f"{'═'*52}\n")

    args        = sys.argv[1:]
    force       = "--force" in args
    stale_only  = "--stale-only" in args
    args        = [a for a in args if not a.startswith("--")]

    explicit_tickers = [t.upper() for t in args]

    # ── Load watchlist ─────────────────────────────────────────────────────
    if explicit_tickers:
        tickers = explicit_tickers
        print(f"  ⚠ CLI override: {len(tickers)} ticker(s) specified")
    else:
        tickers = load_watchlist()
        if not tickers:
            print(f"  ✗ No tickers found in watchlist.json or data directory")
            sys.exit(1)
        print(f"  Watchlist: {len(tickers)} tickers loaded")

    # ── Load existing Nova data ────────────────────────────────────────────
    nova_data = load_nova_json()
    print(f"  Nova supplemental: {len(nova_data)} existing records\n")

    # ── Categorize tickers ─────────────────────────────────────────────────
    to_research      : list[str] = []
    skipped_current  : list[str] = []  # genuinely recent call_date
    skipped_cooldown : list[str] = []  # no data found recently, waiting out cooldown

    for ticker in tickers:
        current = earnings_is_current(nova_data, ticker)
        if current and not force:
            earnings = nova_data.get(ticker.upper(), {}).get("earnings", {})
            in_cooldown = (
                earnings.get("last_search_result") == "no_new_data_found"
                and days_since(earnings.get("last_search_attempted", "")) <= NO_DATA_RETRY_DAYS
            )
            (skipped_cooldown if in_cooldown else skipped_current).append(ticker)
        else:
            if stale_only and current:
                earnings = nova_data.get(ticker.upper(), {}).get("earnings", {})
                in_cooldown = (
                    earnings.get("last_search_result") == "no_new_data_found"
                    and days_since(earnings.get("last_search_attempted", "")) <= NO_DATA_RETRY_DAYS
                )
                (skipped_cooldown if in_cooldown else skipped_current).append(ticker)
            else:
                to_research.append(ticker)

    skipped = skipped_current + skipped_cooldown
    print(f"  To research:  {len(to_research)}")
    print(f"  Skipping:     {len(skipped)} total"
          f" ({len(skipped_current)} current within {STALE_DAYS} days,"
          f" {len(skipped_cooldown)} no-data cooldown within {NO_DATA_RETRY_DAYS} days)")
    if force:
        print(f"  Mode: FORCE REFRESH")
    print()

    if not to_research:
        print("  ✓ All earnings data is current. Nothing to refresh.")
        print(f"{'═'*52}\n")
        return

    # ── Estimate runtime ───────────────────────────────────────────────────
    est_minutes = (len(to_research) * (SLEEP_BETWEEN + 60)) / 60
    print(f"  Estimated runtime: ~{est_minutes:.0f} minutes "
          f"(varies by transcript length)")
    print(f"{'─'*52}\n")

    # ── Research loop ──────────────────────────────────────────────────────
    results_ok       = []
    results_no_data  = []
    results_error    = []

    for i, ticker in enumerate(to_research, 1):
        company_name = get_company_name(ticker)

        print(f"  [{i}/{len(to_research)}] {ticker}  ({company_name})")

        try:
            status = research_earnings(ticker, company_name, force=force)
        except Exception as e:
            print(f"    ✗ Unexpected error: {e}")
            status = "error"

        if status == "ok":
            results_ok.append(ticker)
        elif status == "no_data":
            results_no_data.append(ticker)
            # Record that a search was actually conducted today, even
            # though nothing new was found — without touching the
            # existing call_date/summary from the last successful
            # research (if any), so Jansky/pipeline_health can later
            # distinguish "actively checked recently, nothing new
            # available" from "genuinely neglected," rather than
            # treating a ticker with a real recent search attempt the
            # same as one nobody has looked at in months.
            existing = nova_data.get(ticker.upper(), {})
            existing_earnings = dict(existing.get("earnings", {}))
            existing_earnings["last_search_attempted"] = datetime.date.today().isoformat()
            existing_earnings["last_search_result"]    = "no_new_data_found"
            merged_record = dict(existing)
            merged_record["ticker"]       = ticker
            merged_record["company_name"] = company_name
            merged_record["earnings"]     = existing_earnings
            try:
                call_tool("write_nova_record", {
                    "ticker":      ticker,
                    "record_json": json.dumps(merged_record),
                })
            except Exception as e:
                print(f"    ⚠ Could not record failed-search attempt: {e}")
        else:
            results_error.append(ticker)

        print()

        # Polite delay between tickers
        if i < len(to_research):
            time.sleep(SLEEP_BETWEEN)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'═'*52}")
    print(f"  Nova Earnings Call Research Complete")
    print(f"  ✓ Updated:   {len(results_ok)}")
    print(f"  ⚠ No data:  {len(results_no_data)}  "
          f"({', '.join(results_no_data) if results_no_data else 'none'})")
    print(f"  ✗ Errors:   {len(results_error)}  "
          f"({', '.join(results_error) if results_error else 'none'})")
    print(f"  – Skipped:  {len(skipped)}")
    print(f"\n  Nova data: {NOVA_JSON}")
    print(f"{'═'*52}\n")


if __name__ == "__main__":
    main()
