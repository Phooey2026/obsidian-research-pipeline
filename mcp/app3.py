"""
novamcp - MCP server for Nova legal research and earnings call intelligence
Port 8644 — companion to webmcp (8642) and macromcp (8643)

Search backend: SearXNG (preferred, set SEARCH_PROVIDER=searxng + SEARXNG_URL)
                DuckDuckGo fallback when SearXNG not configured

Tools:
  Legal Research (5):
    search_legal_news          — DDG news search for legal/regulatory issues
    fetch_courtlistener        — CourtListener RECAP federal docket search
    fetch_sec_enforcement      — SEC enforcement actions + litigation releases
    fetch_doj_press            — DOJ press release search
    get_legal_proceedings_deep — Aggressive multi-strategy SEC 10-K extraction

  Earnings Call (2):
    get_earnings_transcript_search  — SearXNG search for transcript/press release URLs
    fetch_url_text                  — Lightweight readability fetch with Google cache fallback

  Nova Data Management (2):
    read_nova_data    — Read nova_supplemental.json
    write_nova_record — Write/update one ticker record in nova_supplemental.json
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from ddgs import DDGS
from markdownify import markdownify as md
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from readability import Document as ReadabilityDocument
from starlette.middleware.cors import CORSMiddleware

# ============================================================================
# Configuration
# ============================================================================

logger = logging.getLogger(__name__)

BASE_DIR         = os.environ.get("NOVA_BASE_DIR",
                                  "/home/jay/stock_dashboard")
NOVA_JSON        = os.path.join(BASE_DIR, "nova_supplemental.json")
DDG_EXCLUDE      = "-site:grokipedia.com"
STALE_DAYS       = 90          # records older than this are flagged stale
COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v4"
SEC_EFTS_BASE    = "https://efts.sec.gov/LATEST/search-index"
SEC_LITRELEASES  = "https://www.sec.gov/litigation/litreleases.shtml"
DOJ_SEARCH_BASE  = "https://www.justice.gov/news"


def _load_dotenv(path: str) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ if missing."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key   = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        logger.warning(f"Failed to load .env: {e}")


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "ddg").strip().lower()
SEARXNG_URL     = os.environ.get("SEARXNG_URL", "").strip()

# ============================================================================
# Shared helpers
# ============================================================================


def _html_to_clean(html: str) -> str:
    """Convert HTML to clean markdown, collapsing excessive whitespace."""
    text = md(html,
              heading_style="ATX",
              strip=["img", "script", "style", "nav", "footer", "header"])
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.strip()


async def _fetch_page_light(url: str, timeout: int = 30) -> tuple[str, str]:
    """Fast HTTP fetch + readability parse. Returns (title, clean_text)."""
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        verify=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; NovaMCP/1.0)"}
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    doc   = ReadabilityDocument(html)
    title = doc.title()
    clean = _html_to_clean(doc.summary())
    if len(clean) < 50:
        clean = _html_to_clean(html)
    return title, clean


async def _ddg_search(query: str, limit: int = 10) -> list[dict]:
    """Search using SearXNG (preferred) or DuckDuckGo fallback.
    SearXNG is used when SEARCH_PROVIDER=searxng and SEARXNG_URL is set.
    Includes a short sleep and one retry to handle rate limiting.
    """
    await asyncio.sleep(2.0)

    full_query = f"{query.strip()} {DDG_EXCLUDE}"

    # ── SearXNG path ──────────────────────────────────────────────────────
    if SEARCH_PROVIDER == "searxng" and SEARXNG_URL:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=20,
                    follow_redirects=True,
                    headers={"User-Agent": "NovaMCP/1.0"}
                ) as client:
                    resp = await client.get(
                        f"{SEARXNG_URL.rstrip('/')}/search",
                        params={"q": full_query, "format": "json"}
                    )
                    resp.raise_for_status()
                    data = resp.json()

                hits = [
                    {
                        "title":       r.get("title", ""),
                        "url":         r.get("url", ""),
                        "description": r.get("content", ""),
                    }
                    for r in data.get("results", [])[:limit]
                ]
                if hits:
                    return hits
                if attempt == 0:
                    await asyncio.sleep(3.0)
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(3.0)
                else:
                    raise
        return []

    # ── DDG fallback path ─────────────────────────────────────────────────
    for attempt in range(2):
        try:
            results = DDGS().text(full_query, max_results=limit)
            hits = [
                {
                    "title":       r.get("title", ""),
                    "url":         r.get("href", ""),
                    "description": r.get("body", ""),
                }
                for r in results
            ]
            if hits:
                return hits
            if attempt == 0:
                await asyncio.sleep(5.0)
        except Exception:
            if attempt == 0:
                await asyncio.sleep(5.0)
            else:
                raise

    return []


def _load_nova_json() -> dict:
    """Load nova_supplemental.json; return empty dict if absent or corrupt."""
    if not os.path.exists(NOVA_JSON):
        return {}
    try:
        with open(NOVA_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read nova_supplemental.json: {e}")
        return {}


def _save_nova_json(data: dict) -> None:
    """Atomically write nova_supplemental.json."""
    tmp = NOVA_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, NOVA_JSON)


def _days_since(date_str: str) -> int:
    """Return calendar days since a YYYY-MM-DD string. Returns 9999 on error."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (datetime.utcnow().date() - d).days
    except Exception:
        return 9999


# ============================================================================
# MCP Server
# ============================================================================

mcp = FastMCP(
    "novamcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


# ────────────────────────────────────────────────────────────────────────────
# LEGAL RESEARCH TOOLS
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_legal_news(ticker: str, company_name: str,
                            max_results: int = 10) -> str:
    """Search DuckDuckGo for recent legal, regulatory, and litigation news
    about a company. Returns up to max_results structured results with title,
    source URL, and description snippet.

    ticker:       stock symbol e.g. GOOG, META, MSFT
    company_name: full company name e.g. 'Alphabet Inc'
    max_results:  number of results (default 10, max 10)
    """
    try:
        max_results = min(max_results, 10)

        # For short/ambiguous tickers (1-2 chars), the ticker itself is useless
        # as a search term. Use the first word of the company name instead.
        # e.g. ticker="B", company_name="BARRICK MINING CORP" → search_id="Barrick"
        search_id = ticker.upper()
        if len(ticker) <= 2:
            first_word = company_name.split()[0].title()
            if len(first_word) > 2:
                search_id = first_word

        queries = [
            f"{company_name} lawsuit litigation 2024 2025 2026",
            f"{search_id} SEC investigation regulatory fine 2025 2026",
            f"{company_name} DOJ FTC antitrust enforcement 2025 2026",
        ]
        seen_urls: set[str] = set()
        all_results: list[dict] = []

        for q in queries:
            if len(all_results) >= max_results:
                break
            hits = await _ddg_search(q, limit=5)
            for h in hits:
                if h["url"] not in seen_urls and len(all_results) < max_results:
                    seen_urls.add(h["url"])
                    all_results.append(h)

        if not all_results:
            return (f"No legal news found for {ticker.upper()} "
                    f"({company_name}) via DDG search.")

        lines = [f"=== Legal News: {company_name} ({ticker.upper()}) ===",
                 f"Results: {len(all_results)}\n"]
        for i, r in enumerate(all_results, 1):
            lines.append(f"[{i}] {r['title']}")
            lines.append(f"    URL: {r['url']}")
            lines.append(f"    {r['description']}\n")
        return "\n".join(lines)

    except Exception as e:
        return f"Error in search_legal_news for {ticker}: {e}"


@mcp.tool()
async def fetch_courtlistener(company_name: str, max_results: int = 10) -> str:
    """Search CourtListener (RECAP Project) for federal court cases involving
    a company. Returns case name, court, filed date, docket number, and URL.
    Requires COURTLISTENER_API_KEY in .env for authenticated access.

    company_name: full or partial company name e.g. 'Alphabet' or 'Meta Platforms'
    max_results:  number of docket results to return (default 10)
    """
    try:
        max_results = min(max_results, 20)

        # Strip common legal suffixes that cause 400 errors on the dockets
        # endpoint — CourtListener search works better with the core name.
        # e.g. "Duke Energy Corporation" → "Duke Energy"
        #      "Advanced Micro Devices, Inc." → "Advanced Micro Devices"
        _suffix_re = re.compile(
            r',?\s+(Inc\.?|Corp\.?|Corporation|Ltd\.?|LLC|L\.L\.C\.|'
            r'Limited|PLC|plc|Co\.?|Company|Group|Holdings?|Incorporated)'
            r'\s*$',
            re.IGNORECASE
        )
        search_name = _suffix_re.sub("", company_name).strip().rstrip(",")

        # Build auth header — degrades gracefully if key not present
        cl_token  = os.environ.get("COURTLISTENER_API_KEY", "")
        cl_headers = {
            "User-Agent": "NovaMCP/1.0 (Obsidian Capital research)"
        }
        if cl_token:
            cl_headers["Authorization"] = f"Token {cl_token}"

        async with httpx.AsyncClient(
            timeout=30,
            headers=cl_headers
        ) as client:
            # Primary: use the /search/ endpoint with type=d (dockets).
            # This endpoint accepts natural language queries and is more
            # forgiving than /dockets/ with company name strings.
            resp = await client.get(
                "https://www.courtlistener.com/api/rest/v4/search/",
                params={
                    "q":         search_name,
                    "type":      "d",
                    "order_by":  "score desc",
                    "page_size": max_results,
                }
            )

            # Fall back to /dockets/ if search endpoint returns an error
            if resp.status_code >= 400:
                resp = await client.get(
                    f"{COURTLISTENER_BASE}/dockets/",
                    params={
                        "case_name__icontains": search_name,
                        "order_by":             "date_filed desc",
                        "page_size":            max_results,
                    }
                )

            resp.raise_for_status()
            data = resp.json()

        # /search/ wraps results under "results"; /dockets/ does the same
        results = data.get("results", [])
        if not results:
            return (f"No federal court dockets found for '{company_name}' "
                    f"(searched as '{search_name}') in CourtListener/RECAP.")

        total = data.get("count", len(results))
        lines = [
            f"=== CourtListener Dockets: {company_name} ===",
            f"Searched as: '{search_name}'",
            f"Found {total} total (showing {len(results)})\n"
        ]

        for r in results:
            # V4 search uses camelCase; V4 dockets endpoint uses snake_case
            case_name  = (r.get("caseName") or r.get("case_name")
                          or r.get("case_name_short", "N/A"))
            court      = r.get("court", "N/A")
            date_filed = r.get("dateFiled") or r.get("date_filed", "N/A")
            docket_num = r.get("docketNumber") or r.get("docket_number", "N/A")
            nature     = r.get("suitNature") or r.get("nature_of_suit", "")
            cause      = r.get("cause", "")
            # V4 search returns docket_absolute_url; build full URL from it
            abs_url    = r.get("docket_absolute_url", "")
            cl_id      = r.get("docket_id") or r.get("id", "")
            if abs_url:
                url = f"https://www.courtlistener.com{abs_url}"
            elif cl_id:
                url = f"https://www.courtlistener.com/docket/{cl_id}/"
            else:
                url = "N/A"

            lines.append(f"Case:    {case_name}")
            lines.append(f"Court:   {court}")
            lines.append(f"Filed:   {date_filed}  |  Docket: {docket_num}")
            if nature:
                lines.append(f"Nature:  {nature}")
            if cause:
                lines.append(f"Cause:   {cause}")
            lines.append(f"URL:     {url}\n")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching CourtListener data for '{company_name}': {e}"


@mcp.tool()
async def fetch_sec_enforcement(ticker: str, company_name: str) -> str:
    """Search SEC EDGAR full-text search and SEC Litigation Releases for
    enforcement actions, admin proceedings, and regulatory actions involving
    a company.

    ticker:       stock symbol e.g. GOOG
    company_name: full company name e.g. 'Alphabet Inc'
    """
    try:
        lines = [f"=== SEC Enforcement: {company_name} ({ticker.upper()}) ===\n"]

        # ── Part 1: EDGAR full-text search for enforcement docs ──────────────
        search_term = company_name.split()[0]   # use first word for broader match
        params = {
            "q":         f'"{search_term}"',
            "dateRange": "custom",
            "startdt":   "2020-01-01",
            "forms":     "AP,AAE,34-",           # admin proceedings + orders
            "hits.hits.total.value": 1,
        }
        efts_url = "https://efts.sec.gov/LATEST/search-index"
        # Use the public EDGAR full-text search endpoint
        edgar_search_url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{company_name.replace(' ', '+')}%22"
            f"&dateRange=custom&startdt=2020-01-01"
            f"&forms=AP"
        )

        # Simpler: use the EDGAR EFTS REST API
        async with httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": "Jay jay@example.com"}
        ) as client:
            # EDGAR full-text search
            efts_resp = await client.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={
                    "q":       f'"{company_name}"',
                    "startdt": "2020-01-01",
                    "forms":   "AP",
                }
            )

            edgar_hits = []
            if efts_resp.status_code == 200:
                efts_data = efts_resp.json()
                hits = (efts_data.get("hits", {})
                                 .get("hits", []))[:5]
                for h in hits:
                    src = h.get("_source", {})
                    edgar_hits.append({
                        "title":    src.get("display_names", ["N/A"])[0]
                                    if src.get("display_names") else "N/A",
                        "filed":    src.get("file_date", "N/A"),
                        "form":     src.get("form_type", "N/A"),
                        "url":      ("https://www.sec.gov/Archives/edgar/data/"
                                     + src.get("entity_id", "")
                                     + "/" + src.get("file_num", "")),
                    })

            # SEC Litigation Releases — search the HTML index
            lit_resp = await client.get(
                "https://www.sec.gov/litigation/litreleases/litreleases-index.json",
                timeout=20
            )
            lit_hits = []
            if lit_resp.status_code == 200:
                lit_data = lit_resp.json()
                search_lower = company_name.lower()
                ticker_lower = ticker.lower()
                # Scan entries for company name or ticker mentions
                entries = lit_data if isinstance(lit_data, list) else \
                          lit_data.get("results", [])
                for entry in entries[:500]:      # check most recent 500
                    title = (entry.get("title") or
                             entry.get("name") or "").lower()
                    if (search_lower.split()[0] in title
                            or ticker_lower in title):
                        lit_hits.append({
                            "title": entry.get("title") or entry.get("name"),
                            "date":  entry.get("date") or entry.get("filed"),
                            "url":   entry.get("url") or entry.get("href", ""),
                        })
                        if len(lit_hits) >= 5:
                            break

        # ── Format output ────────────────────────────────────────────────────
        if edgar_hits:
            lines.append("── EDGAR Admin Proceedings ──")
            for h in edgar_hits:
                lines.append(f"  Form:  {h['form']}  |  Filed: {h['filed']}")
                lines.append(f"  Title: {h['title']}")
                lines.append(f"  URL:   {h['url']}\n")
        else:
            lines.append("── EDGAR Admin Proceedings: None found\n")

        if lit_hits:
            lines.append("── SEC Litigation Releases ──")
            for h in lit_hits:
                lines.append(f"  Date:  {h['date']}")
                lines.append(f"  Title: {h['title']}")
                if h["url"]:
                    url = (h["url"] if h["url"].startswith("http")
                           else "https://www.sec.gov" + h["url"])
                    lines.append(f"  URL:   {url}")
                lines.append("")
        else:
            lines.append("── SEC Litigation Releases: None found\n")

        # ── Part 2: DDG fallback for SEC enforcement news ────────────────────
        ddg_hits = await _ddg_search(
            f'"{company_name}" SEC enforcement fine penalty 2023 2024 2025',
            limit=5
        )
        if ddg_hits:
            lines.append("── SEC Enforcement News (DDG) ──")
            for h in ddg_hits:
                lines.append(f"  {h['title']}")
                lines.append(f"  {h['url']}")
                lines.append(f"  {h['description']}\n")

        lines.append(
            f"\nSEC EDGAR direct search: "
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{company_name.replace(' ', '+')}%22"
        )
        return "\n".join(lines)

    except Exception as e:
        return f"Error in fetch_sec_enforcement for {ticker}: {e}"


@mcp.tool()
async def fetch_doj_press(company_name: str, ticker: str,
                          max_results: int = 5) -> str:
    """Search DOJ press releases and news for mentions of a company.
    Covers criminal indictments, civil enforcement, antitrust actions,
    FCPA violations, fraud, and other DOJ activity.

    company_name: full company name e.g. 'Alphabet Inc'
    ticker:       stock symbol e.g. GOOG
    max_results:  number of DDG results (default 5)
    """
    try:
        lines = [f"=== DOJ Activity: {company_name} ({ticker.upper()}) ===\n"]

        # Primary: site-targeted DDG search against justice.gov
        queries = [
            f'site:justice.gov "{company_name}"',
            f'site:justice.gov "{ticker.upper()}" antitrust enforcement',
            f'"{company_name}" DOJ Department of Justice 2023 2024 2025',
        ]

        seen: set[str] = set()
        hits: list[dict] = []
        for q in queries:
            if len(hits) >= max_results:
                break
            results = await _ddg_search(q, limit=4)
            for r in results:
                if r["url"] not in seen and len(hits) < max_results:
                    seen.add(r["url"])
                    hits.append(r)

        if hits:
            lines.append("── DOJ Press Releases / News ──")
            for i, h in enumerate(hits, 1):
                lines.append(f"[{i}] {h['title']}")
                lines.append(f"    URL: {h['url']}")
                lines.append(f"    {h['description']}\n")
        else:
            lines.append("No DOJ press releases found via DDG search.\n")

        lines.append(
            f"Manual search: https://www.justice.gov/search?keys="
            f"{company_name.replace(' ', '+')}"
        )
        return "\n".join(lines)

    except Exception as e:
        return f"Error in fetch_doj_press for {company_name}: {e}"


@mcp.tool()
def get_legal_proceedings_deep(ticker: str) -> str:
    """Deep extraction of legal proceedings from SEC 10-K, 20-F, or 40-F.
    More aggressive than the standard get_legal_proceedings tool:
    - Tries the current year AND prior year filing if needed
    - Supports 10-K (US domestic), 20-F (foreign private issuer),
      and 40-F (Canadian foreign private issuer e.g. AEM, BHP, RIO)
    - Attempts exhibit parsing when main document cross-references
    - Uses broader position scanning (10%–90% of document)
    - Lower minimum content threshold to catch terse disclosures
    - Tries all HTM files in the filing index, not just the largest

    Use this tool when get_legal_proceedings returns no useful content.
    ticker: stock symbol e.g. GOOG, META, MSFT, AAPL, AEM, RIO
    """
    try:
        import urllib.request
        import json as _json
        import re as _re

        headers = {'User-Agent': 'Jay jay@example.com'}

        def sec_get(url):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()

        # ── Step 1: Resolve CIK ──────────────────────────────────────────────
        tickers_data = _json.loads(sec_get(
            'https://www.sec.gov/files/company_tickers.json'))
        cik = None
        company_name = ticker.upper()
        for entry in tickers_data.values():
            if entry.get('ticker', '').upper() == ticker.upper():
                cik = str(entry['cik_str']).zfill(10)
                company_name = entry.get('title', ticker.upper())
                break
        if not cik:
            return f"Could not find SEC CIK for {ticker.upper()}"

        # ── Step 2: Collect up to 3 recent 10-K/20-F filings to try ─────────
        subs = _json.loads(sec_get(
            f'https://data.sec.gov/submissions/CIK{cik}.json'))
        filings = subs.get('filings', {}).get('recent', {})
        forms   = filings.get('form', [])
        accns   = filings.get('accessionNumber', [])
        dates   = filings.get('filingDate', [])

        candidates = []
        for i, form in enumerate(forms):
            if form in ('10-K', '20-F', '40-F') and len(candidates) < 3:
                candidates.append({
                    'accn':      accns[i],
                    'date':      dates[i],
                    'form_type': form,
                })

        if not candidates:
            return f"No 10-K/20-F/40-F filings found for {ticker.upper()}"

        # ── Extraction helpers ───────────────────────────────────────────────
        end_markers = [
            'Non-Income Taxes', 'Note 11', 'NOTE 11',
            'Note 15', 'NOTE 15', 'Note 17', 'NOTE 17', 'Note 18', 'NOTE 18',
            'Income Taxes', 'INCOME TAXES',
            'Subsequent Events', 'SUBSEQUENT EVENTS',
            'Item 4', 'ITEM 4',
            'STOCK-BASED COMPENSATION', 'Stock-Based Compensation',
            'NOTE T.', 'NOTE S.', 'NOTE R.',
            'Other Stock Transactions',
        ]

        anchor_phrases = [
            'Commitments and Contingencies Legal Proceedings',
            'Legal Proceedings Contingencies',
            'Legal Matters We record a liability when we believe',
            'Antitrust Matters We are subject to formal',
            'Antitrust Matters',
            'Commitments and Contingencies We are subject to',
            'Privacy Matters We are subject to',
            'we record a liability when we believe that it is probable',
            'Legal Proceedings We are subject to various',
            'Legal Proceedings The following',
            'Note 16. Contingencies', 'NOTE 16. CONTINGENCIES',
            'Note 16: Contingencies',
            'COMMITMENTS AND CONTINGENCIES ENVIRONMENTAL',
            'COMMITMENTS AND CONTINGENCIES LITIGATION',
            'Contingencies and Commitments',
            'Legal and Regulatory Matters',
            'Litigation and Regulatory',
            'Wildfire-Related Claims and Litigation',
            'Wildfire Liability',
            'Wildfire Insurance and Contingencies',
            'California Wildfires',
        ]

        def extract_from(clean, start_pos, chunk_size=30000, min_len=80):
            chunk = clean[start_pos:start_pos + chunk_size]
            end_pos = len(chunk)
            for marker in end_markers:
                p = chunk.find(marker, 1500)
                if p != -1 and p < end_pos:
                    end_pos = p
            result = chunk[:end_pos].strip()
            return result if len(result) >= min_len else ''

        def try_extract(clean: str, form_type: str) -> str | None:
            doc_len = len(clean)

            # Strategy A: anchor phrases, scanning 10%–85%
            for pct in [0.50, 0.35, 0.60, 0.75, 0.20, 0.10, 0.85]:
                threshold = int(doc_len * pct)
                for phrase in anchor_phrases:
                    pos = clean.lower().find(phrase.lower(), threshold)
                    if pos != -1:
                        c = extract_from(clean, pos, 30000)
                        if len(c) > 300:
                            return c

            # Strategy A2: Note N Commitments and Contingencies
            note_cc_re = _re.compile(
                r'Note\s+\d+[\.\s:\u2013\u2014,\-\"\u201c\u201d]+'
                r'Commitments\s+and\s+Contingencies',
                _re.IGNORECASE)
            for pct in [0.25, 0.10, 0.05]:
                threshold = int(doc_len * pct)
                m = note_cc_re.search(clean, threshold)
                if m:
                    c = extract_from(clean, m.start(), 30000)
                    if len(c) > 300:
                        return c

            # Strategy B: 'Legal Matters' in final 40%
            threshold = int(doc_len * 0.60)
            for m in _re.finditer(r'(?i)\blegal\s+matters\b', clean):
                if m.start() > threshold:
                    c = extract_from(clean, m.start(), 30000)
                    if len(c) > 300:
                        return c

            # Strategy B2: lettered subsection 'h) Legal proceedings'
            subsec_re = _re.compile(r'\b[a-z]\)\s*[Ll]egal\s+proceedings\b')
            for m in subsec_re.finditer(clean):
                if m.start() > int(doc_len * 0.25):
                    c = extract_from(clean, m.start(), 30000)
                    if len(c) > 300:
                        return c

            # Strategy C: Item 3 Legal Proceedings
            for m in _re.finditer(
                    r'(?i)ITEM\s+3[\.\s]+LEGAL\s+PROCEEDINGS', clean):
                if m.start() > int(doc_len * 0.20):
                    c = extract_from(clean, m.start(), 30000)
                    if (len(c) > 300
                            and 'see note' not in c[:200].lower()
                            and 'incorporated herein' not in c[:200].lower()):
                        return c

            # Strategy C2: 20-F — Legal and Arbitration Proceedings
            if form_type == '20-F':
                for pattern in [
                    r'(?i)legal\s+and\s+arbitration\s+proceedings',
                    r'(?i)legal\s+proceedings\b',
                ]:
                    for m in _re.finditer(pattern, clean):
                        if m.start() > int(doc_len * 0.10):
                            c = extract_from(clean, m.start(), 30000)
                            if (len(c) > 300
                                    and 'see note' not in c[:200].lower()):
                                return c

            # Strategy D: broad keyword sweep, lower threshold
            for keyword in ['pending legal proceedings', 'material litigation',
                            'class action', 'defendant or plaintiff',
                            'ordinary course of business',
                            'regulatory proceedings', 'government investigations',
                            'wildfire']:
                pos = clean.lower().find(keyword, doc_len // 5)
                if pos != -1:
                    start = max(0, pos - 200)
                    c = extract_from(clean, start, 8000, min_len=80)
                    if c:
                        return c

            return None

        def clean_html(raw: str) -> str:
            c = _re.sub(r'<[^>]+>', ' ', raw)
            for ent, rep in [('&nbsp;', ' '), ('&amp;', '&'),
                             ('&#160;', ' '), ('&#8226;', '•'),
                             ('&#8211;', '-'), ('&#8217;', "'"),
                             ('&#8220;', '"'), ('&#8221;', '"')]:
                c = c.replace(ent, rep)
            c = _re.sub(r'&#\d+;', ' ', c)
            c = _re.sub(r'\s+', ' ', c)
            return c

        # ── Step 3: Try each filing year ─────────────────────────────────────
        for filing in candidates:
            accn      = filing['accn']
            date      = filing['date']
            form_type = filing['form_type']
            accn_fmt  = accn.replace('-', '')
            cik_num   = cik.lstrip('0')

            try:
                index_url  = (f'https://www.sec.gov/Archives/edgar/data/'
                              f'{cik_num}/{accn_fmt}/index.json')
                index_data = _json.loads(sec_get(index_url))
                items      = index_data.get('directory', {}).get('item', [])
            except Exception:
                continue

            # Collect candidate HTM files: largest first, prefer named 10-K/20-F
            htm_files = []
            for item in items:
                name       = item.get('name', '')
                name_lower = name.lower()
                try:
                    size = int(item.get('size', 0) or 0)
                except (ValueError, TypeError):
                    size = 0
                if not name_lower.endswith(('.htm', '.html')):
                    continue
                if 'index' in name_lower:
                    continue
                if _re.search(r'x?ex\d|exhibit|exh\d', name_lower):
                    continue
                if _re.match(r'^r\d+\.html?$', name_lower):
                    continue
                priority = 1 if ('10k' in name_lower or '20f' in name_lower) \
                           else 0
                htm_files.append((priority, size, name))

            # Sort: explicit 10k/20f names first, then by size descending
            htm_files.sort(key=lambda x: (-x[0], -x[1]))

            doc_url      = None
            doc_filename = None
            legal_text   = None

            for _, _, fname in htm_files[:5]:    # try up to 5 HTM files
                url = (f'https://www.sec.gov/Archives/edgar/data/'
                       f'{cik_num}/{accn_fmt}/{fname}')
                try:
                    raw   = sec_get(url).decode('utf-8', errors='ignore')
                    clean = clean_html(raw)
                except Exception:
                    continue

                result = try_extract(clean, form_type)
                if result:
                    legal_text   = result
                    doc_url      = url
                    doc_filename = fname
                    break

            if legal_text:
                legal_text = _re.sub(r'\s+', ' ', legal_text).strip()
                if len(legal_text) > 30000:
                    legal_text = (legal_text[:30000]
                                  + "\n\n[Truncated — full text in SEC filing]")
                lines = [
                    f"=== {company_name} ({ticker.upper()}) — DEEP EXTRACTION ===",
                    f"Legal Proceedings — {form_type} filed {date}",
                    f"Source: {doc_filename}\n",
                    legal_text,
                    f"\nFull filing: {doc_url}",
                    f"SEC EDGAR: https://www.sec.gov/cgi-bin/browse-edgar"
                    f"?action=getcompany&CIK={cik}&type=10-K"
                ]
                return "\n".join(lines)

        # All filings exhausted
        return (
            f"=== {company_name} ({ticker.upper()}) — DEEP EXTRACTION ===\n"
            f"Tried {len(candidates)} filing(s) — legal proceedings section "
            f"could not be extracted.\n"
            f"This may indicate an unusual filing format, heavy XBRL embedding, "
            f"or genuinely minimal legal disclosure.\n\n"
            f"Manual review: https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik}&type=10-K"
        )

    except Exception as e:
        return f"Error in get_legal_proceedings_deep for {ticker}: {e}"


# ────────────────────────────────────────────────────────────────────────────
# EARNINGS CALL TOOLS
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_earnings_transcript_search(ticker: str,
                                          company_name: str) -> str:
    """Search for the most recent earnings call press release or transcript.
    Uses quarter-aware queries, wire services, IR pages, and transcript
    aggregators. Designed to handle edge cases like non-calendar fiscal years
    (MU ends August), smaller caps (AMCR), and recently added tickers (TSLA).

    Returns up to 10 candidate URLs for fetch_url_text to retrieve.

    ticker:       stock symbol e.g. MU, TSLA, AMCR
    company_name: full company name e.g. 'Micron Technology'
    """
    try:
        import datetime as _dt

        now   = _dt.datetime.utcnow()
        year  = now.year
        month = now.month

        # Determine the two most likely recent quarters to search for
        # (current and prior) — handles fiscal year mismatches gracefully
        def _recent_quarters(year: int, month: int) -> list[str]:
            """Return last 3 quarter labels most likely to be current.

            Starts from the most recently COMPLETED quarter, not the
            current in-progress one — the current quarter can't have
            earnings results yet, so including it wasted a query slot.
            """
            quarters = []
            for delta in range(1, 4):
                # Walk back by quarter
                m = month - (delta * 3)
                y = year
                while m <= 0:
                    m += 12
                    y -= 1
                q = (m - 1) // 3 + 1
                quarters.append(f"Q{q} {y}")
            return quarters

        recent_qs = _recent_quarters(year, month)  # e.g. ["Q2 2026", "Q1 2026", "Q4 2025"]

        ticker_up = ticker.upper()

        # ── Known IR page patterns for problem tickers ─────────────────────
        # These are directly fetchable without SearXNG
        IR_PAGES = {
            "MU":   "https://investors.micron.com/news-releases/news-release-details",
            "AMCR": "https://www.amcor.com/investors/news",
            "TSLA": "https://ir.tesla.com/press-releases",
            "NVDA": "https://investor.nvidia.com/financial-information/press-releases",
            "AAPL": "https://investor.apple.com/news/press-releases/default.aspx",
        }

        queries = []

        # ── Tier 1: Wire services with quarter-specific terms ──────────────
        for q_label in recent_qs[:2]:
            queries.append(
                f'"{company_name}" {q_label} earnings results '
                f"site:businesswire.com OR site:globenewswire.com OR site:prnewswire.com"
            )

        # ── Tier 2: Wire services without quarter (catches any recent) ──────
        queries.append(
            f'"{company_name}" quarterly earnings results '
            f"site:businesswire.com OR site:globenewswire.com OR site:prnewswire.com"
        )

        # ── Tier 3: Transcript aggregators ────────────────────────────────
        # Motley Fool and Seeking Alpha have reliable free transcripts
        for q_label in recent_qs[:2]:
            queries.append(
                f"{ticker_up} {q_label} earnings call transcript "
                f"site:fool.com OR site:seekingalpha.com"
            )

        # ── Tier 4: Broad fallbacks ────────────────────────────────────────
        queries.append(f"{ticker_up} earnings press release {year}")
        queries.append(f'"{company_name}" quarterly results investor relations {year}')
        queries.append(f"{ticker_up} earnings call {recent_qs[0]}")

        seen: set[str] = set()
        results: list[dict] = []

        for q in queries:
            if len(results) >= 10:
                break
            try:
                hits = await _ddg_search(q, limit=4)
                for h in hits:
                    url = h.get("url", "")
                    if url and url not in seen and len(results) < 10:
                        # Prioritize wire services and transcript sites
                        is_priority = any(
                            s in url for s in (
                                "businesswire.com", "globenewswire.com",
                                "prnewswire.com", "fool.com", "seekingalpha.com",
                                "ir.", "investor.", "investors."
                            )
                        )
                        if is_priority:
                            results.insert(0, h)
                        else:
                            results.append(h)
                        seen.add(url)
            except Exception:
                continue

        # ── Prepend known IR page if ticker has one ────────────────────────
        if ticker_up in IR_PAGES and IR_PAGES[ticker_up] not in seen:
            results.insert(0, {
                "title": f"{company_name} Investor Relations — Press Releases",
                "url":   IR_PAGES[ticker_up],
                "description": "Official IR page — check for most recent quarterly earnings release"
            })

        if not results:
            return (
                f"No earnings transcript URLs found for {ticker_up} ({company_name}).\n"
                f"Searched for quarters: {', '.join(recent_qs)}\n"
                f"Try fetching the IR page directly if known."
            )

        lines = [
            f"=== Earnings Transcript Search: {company_name} ({ticker_up}) ===",
            f"Searched for: {', '.join(recent_qs[:2])}",
            f"Found {len(results)} candidate URL(s).",
            "Fetch the most promising URL(s) with fetch_url_text — "
            "prioritize wire services and official IR pages.\n"
        ]
        for i, r in enumerate(results[:10], 1):
            lines.append(f"[{i}] {r['title']}")
            lines.append(f"    URL: {r['url']}")
            if r.get('description'):
                lines.append(f"    {r['description'][:150]}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"Error in get_earnings_transcript_search for {ticker}: {e}"


@mcp.tool()
async def fetch_url_text(url: str, max_chars: int = 40000) -> str:
    """Fetch a URL and return its readable text content.
    On Cloudflare-blocked pages, automatically retries via Google cache.
    Strips navigation, scripts, and boilerplate.

    url:       the URL to fetch
    max_chars: maximum characters to return (default 40000)
    """
    async def _try_fetch(target_url: str) -> tuple[str, str]:
        """Returns (title, clean_text) or raises."""
        return await _fetch_page_light(target_url)

    def _is_cloudflare_block(text: str) -> bool:
        """Detect Cloudflare challenge/block pages."""
        cf_markers = (
            'Just a moment',
            'Checking your browser',
            'cf-browser-verification',
            'cloudflare',
            'Enable JavaScript and cookies',
            'challenges.cloudflare.com',
        )
        text_lower = text.lower()
        return any(m.lower() in text_lower for m in cf_markers)

    try:
        # ── Primary fetch ──────────────────────────────────────────────
        try:
            title, clean = await _try_fetch(url)
        except Exception as e:
            clean = ""
            title = ""

        # ── Cloudflare detected or empty — try Google cache ────────────
        if not clean or _is_cloudflare_block(clean) or len(clean) < 200:
            cache_url = (f"https://webcache.googleusercontent.com/"
                         f"search?q=cache:{url}&hl=en")
            try:
                title, clean = await _try_fetch(cache_url)
                if clean and not _is_cloudflare_block(clean) and len(clean) >= 200:
                    # Strip Google cache header boilerplate
                    if 'cached version' in clean.lower():
                        idx = clean.lower().find('cached version')
                        clean = clean[idx + 15:].strip()
                else:
                    clean = ""
            except Exception:
                clean = ""

        if not clean:
            return f"No readable content found at: {url}"

        if len(clean) > max_chars:
            clean = clean[:max_chars] + "\n\n[Truncated]"
        return f"=== {title} ===\nURL: {url}\n\n{clean}"

    except Exception as e:
        return f"Error fetching {url}: {e}"


# ────────────────────────────────────────────────────────────────────────────
# NOVA DATA MANAGEMENT TOOLS
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def read_nova_data(ticker: str = "") -> str:
    """Read nova_supplemental.json.
    If ticker is provided, returns only that ticker's record.
    If ticker is empty, returns the full file contents as JSON text.
    Returns a clear message if the file does not exist yet.

    ticker: optional stock symbol to filter (e.g. 'GOOG'); empty = all records
    """
    try:
        data = _load_nova_json()
        if not data:
            return "nova_supplemental.json does not exist or is empty."

        if ticker:
            rec = data.get(ticker.upper())
            if not rec:
                return (f"No Nova record found for {ticker.upper()}. "
                        f"Available tickers: {', '.join(sorted(data.keys()))}")
            return json.dumps({ticker.upper(): rec}, indent=2)

        return json.dumps(data, indent=2)

    except Exception as e:
        return f"Error reading nova_supplemental.json: {e}"


@mcp.tool()
def write_nova_record(ticker: str, record_json: str) -> str:
    """Write or update a single ticker's record in nova_supplemental.json.
    The record_json must be a valid JSON object string containing any subset
    of the Nova record schema. Existing fields not present in record_json
    are preserved (deep merge at the top level only).

    Automatically sets nova_status based on research_date age:
      current  — research_date within last 90 days
      stale    — research_date older than 90 days
      pending  — no research_date present

    ticker:      stock symbol e.g. 'GOOG'
    record_json: JSON string with fields to write, e.g.:
                 '{"legal": {"nova_legal_summary": "...", "risk_level": "High"}}'
    """
    try:
        new_data = json.loads(record_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON in record_json: {e}"

    try:
        ticker = ticker.upper()
        store  = _load_nova_json()

        existing = store.get(ticker, {})

        # Deep-merge at one level: merge sub-dicts (legal, earnings) separately
        for key, value in new_data.items():
            if (key in existing
                    and isinstance(existing[key], dict)
                    and isinstance(value, dict)):
                existing[key] = {**existing[key], **value}
            else:
                existing[key] = value

        # Ensure top-level identifiers
        existing.setdefault("ticker",       ticker)
        existing.setdefault("company_name", ticker)

        # Update nova_status on legal section
        legal = existing.get("legal", {})
        if legal:
            rd = legal.get("research_date", "")
            if rd:
                age = _days_since(rd)
                legal["nova_status"] = "current" if age <= STALE_DAYS \
                                       else "stale"
            else:
                legal["nova_status"] = "pending"
            existing["legal"] = legal

        # Update nova_status on earnings section
        earnings = existing.get("earnings", {})
        if earnings:
            rd = earnings.get("research_date", "")
            if rd:
                age = _days_since(rd)
                earnings["nova_status"] = "current" if age <= STALE_DAYS \
                                          else "stale"
            else:
                earnings["nova_status"] = "pending"
            existing["earnings"] = earnings

        store[ticker] = existing
        _save_nova_json(store)

        return (f"Nova record for {ticker} written successfully. "
                f"nova_supplemental.json now contains "
                f"{len(store)} ticker(s).")

    except Exception as e:
        return f"Error writing Nova record for {ticker}: {e}"


# ============================================================================
# FastAPI App Setup
# ============================================================================

app = mcp.streamable_http_app()

app = CORSMiddleware(
    app,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id"],
)

# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8644)
