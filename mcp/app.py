"""
webmcp - MCP server for web scraping and content extraction
"""

import asyncio
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from ddgs import DDGS
from markdownify import markdownify as md
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from playwright.async_api import async_playwright
from readability import Document as ReadabilityDocument
from starlette.middleware.cors import CORSMiddleware

# ============================================================================
# Configuration
# ============================================================================

logger = logging.getLogger(__name__)

TOOL_CALL_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tool_calls.log.json"
)


def _load_dotenv(path: str) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ if missing."""
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        logger.warning(f"Failed to load .env file from {path}: {e}")


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

LLM_URL = os.environ.get("LLM_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "ddg").strip().lower()
SEARXNG_URL = os.environ.get("SEARXNG_URL", "").strip()
# Appended to every DuckDuckGo (ddgs) text search query.
DDG_QUERY_SITE_EXCLUDE = "-site:grokipedia.com"

# if not LLM_URL or not LLM_MODEL:
#     raise ValueError("LLM_URL and LLM_MODEL environment variables are required")

# ============================================================================
# Content Processing
# ============================================================================


def _html_to_clean(html: str) -> str:
    """Convert HTML to clean markdown, collapsing excessive whitespace."""
    text = md(
        html,
        heading_style="ATX",
        strip=["img", "script", "style", "nav", "footer", "header"]
    )
    # Collapse runs of 3+ blank lines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse runs of spaces (but not newlines) on each line
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.strip()


async def _fetch_one(browser: Any, url: str, timeout_ms: int = 0) -> tuple[str, str]:
    """Fetch a single URL using an existing browser instance."""
    page = await browser.new_page()
    await page.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(2000)
        html = await page.content()
    finally:
        await page.close()

    doc = ReadabilityDocument(html)
    title = doc.title()
    clean_text = _html_to_clean(doc.summary())

    if len(clean_text) < 50:
        clean_text = _html_to_clean(html)

    return title, clean_text


async def _fetch_pages(urls: list[str]) -> list[tuple[str, str, str | None]]:
    """Fetch multiple URLs in parallel with a shared browser. Returns [(title, text, error)]."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            async def _fetch_single(url: str) -> tuple[str, str, str | None]:
                try:
                    title, text = await _fetch_one(browser, url)
                    return title, text, None
                except Exception as e:
                    logger.error(f"Failed to fetch {url}: {e}")
                    return "", "", str(e)

            results = await asyncio.gather(*[_fetch_single(u) for u in urls])
        finally:
            await browser.close()

    return results


async def _fetch_page_light(url: str) -> tuple[str, str]:
    """Fast fetch without a browser — good for simple pages."""
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        verify=False
    ) as client:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        html = resp.text

    doc = ReadabilityDocument(html)
    title = doc.title()
    clean_text = _html_to_clean(doc.summary())

    if len(clean_text) < 50:
        clean_text = _html_to_clean(html)

    return title, clean_text


async def _llm_extract(content: str, prompt: str | None, schema: dict | None) -> str:
    """Send content to local LLM for structured extraction."""
    system_msg = (
        "You are a data extraction assistant. "
        "Extract the requested information from the provided web page content. "
        "Be precise and only return the extracted data. Be as detailed as possible "
        "without including extra information. Do not skimp. "
        "NEVER return an empty result. If you cannot find the requested data, "
        "you MUST explain why — e.g. the page didn't contain it, the content was "
        "blocked, the page was a login wall, etc."
    )

    if schema:
        system_msg += f"\n\nReturn the data as JSON matching this schema:\n{json.dumps(schema, indent=2)}"

    user_msg = content
    if prompt:
        user_msg += f"\n\n---\nExtraction request: {prompt}"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{LLM_URL}/v1/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.1,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        resp.raise_for_status()
        result = resp.json()
        return result["choices"][0]["message"]["content"]


async def _search_ddg(query: str, limit: int) -> list[dict]:
    """Search using DuckDuckGo."""
    base = query.strip()
    ddg_query = f"{base} {DDG_QUERY_SITE_EXCLUDE}" if base else DDG_QUERY_SITE_EXCLUDE
    results = DDGS().text(ddg_query, max_results=limit)
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "description": r.get("body", ""),
        }
        for r in results
    ]


async def _search_searxng(query: str, limit: int) -> list[dict]:
    """Search using a SearXNG instance."""
    if not SEARXNG_URL:
        raise ValueError("SEARXNG_URL is required when SEARCH_PROVIDER=searxng")

    base_url = SEARXNG_URL.rstrip("/")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(
            f"{base_url}/search",
            params={"q": query, "format": "json"},
            headers={"User-Agent": "webmcp/1.0"},
        )
        resp.raise_for_status()
        payload = resp.json()

    results = payload.get("results", [])[:limit]
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("content", ""),
        }
        for r in results
    ]

# ============================================================================
# Tool Call Logging
# ============================================================================


class ToolCallLogger:
    """Manages persistent tool call logging with bounded history."""

    MAX_ENTRIES = 10

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self._buffer: list[dict[str, Any]] = []
        self._load_existing()

    def _load_existing(self) -> None:
        """Load existing log on startup."""
        if self.log_path.exists():
            try:
                with open(self.log_path, "r") as f:
                    self._buffer = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load existing log: {e}")
                self._buffer = []

    def _flush(self) -> None:
        """Persist the buffer to disk."""
        try:
            with open(self.log_path, "w") as f:
                json.dump(self._buffer[-self.MAX_ENTRIES:], f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to flush tool log: {e}")

    def log_call(self, tool_name: str, arguments: dict, result: str) -> None:
        """Log a tool call and persist if buffer is full."""
        entry = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "arguments": arguments,
            "result": result,
        }
        self._buffer.append(entry)

        if len(self._buffer) > self.MAX_ENTRIES:
            self._buffer = self._buffer[-self.MAX_ENTRIES:]
            self._flush()


_tool_logger = ToolCallLogger(TOOL_CALL_LOG_PATH)

# ============================================================================
# MCP Server Setup
# ============================================================================

mcp = FastMCP(
    "webmcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


@mcp.tool()
async def get_current_date() -> str:
    """Get the current date. Use this tool to get today's date in ISO format (YYYY-MM-DD)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d (%A)")


@mcp.tool()
async def search_web(query: str, limit: int = 10) -> str:
    """Searches the web for a query using ddg or searxng. Returns titles, URLs, and descriptions."""
    if SEARCH_PROVIDER == "searxng":
        data = await _search_searxng(query, limit)
    elif SEARCH_PROVIDER == "ddg":
        data = await _search_ddg(query, limit)
    else:
        raise ValueError("SEARCH_PROVIDER must be either 'ddg' or 'searxng'")

    _tool_logger.log_call(
        "search_web",
        {"query": query, "limit": limit, "provider": SEARCH_PROVIDER},
        json.dumps(data)
    )
    return json.dumps(data, indent=2)


@mcp.tool()
async def extract(
    urls: list[str],
    prompt: str | None = None,
    schema: dict | None = None,
    use_browser: bool = True,
) -> str:
    """Extract structured data from one or more URLs using a local LLM.

    Fetches each URL, extracts readable content, then sends it to a local LLM
    with your prompt/schema to pull out structured data.

    To find URLs first, call search_web separately, then pass the results here.

    Args:
        urls: URLs to extract from.
        prompt: Tells the extraction LLM what data to pull from the page content.
        schema: JSON schema the output should conform to.
        use_browser: If True (default), use Playwright for JS rendering.
                     False uses lightweight HTTP fetch.
    """
    if not prompt and not schema:
        error_result = {"error": "At least one of prompt or schema is required."}
        _tool_logger.log_call("extract", {"urls": urls}, json.dumps(error_result))
        return json.dumps(error_result, indent=2)

    # Fetch and clean each page
    contents: list[str] = []

    if use_browser:
        results = await _fetch_pages(urls)
        for url, (title, text, err) in zip(urls, results):
            if err:
                contents.append(f"=== {url} ===\nFailed to fetch: {err}")
            else:
                if len(text) > 12000:
                    text = text[:12000] + "\n... [truncated]"
                contents.append(f"=== {url} ===\n{title}\n\n{text}")
    else:
        for url in urls:
            try:
                title, text = await _fetch_page_light(url)
                if len(text) > 12000:
                    text = text[:12000] + "\n... [truncated]"
                contents.append(f"=== {url} ===\n{title}\n\n{text}")
            except Exception as e:
                contents.append(f"=== {url} ===\nFailed to fetch: {e}")

    combined = "\n\n".join(contents)
    result = await _llm_extract(combined, prompt, schema)

    _tool_logger.log_call(
        "extract",
        {
            "urls": urls,
            "prompt": prompt,
            "schema": schema,
            "use_browser": use_browser,
        },
        result
    )

    return result

@mcp.tool()
def get_stock_prices(ticker: str, period: str = "1mo") -> str:
    """Fetch recent stock closing prices from Yahoo Finance.
    ticker: stock symbol e.g. AVGO, NVDA, TSLA
    period: use yfinance periods: 1mo, 3mo, 6mo, 1y, 2y, 5y, ytd, max
    NOTE: do NOT use '30d' — use '1mo', '3mo', '6mo', '1y' instead
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker.upper()).history(period=period)[["Close"]]
        if hist.empty:
            return f"No data found for {ticker}."
        hist.index = hist.index.strftime("%Y-%m-%d")
        lines = [f"{date}: ${price:.2f}" for date, price in hist["Close"].items()]
        summary = (f"{ticker.upper()} — {len(lines)} trading days\n"
                   f"Start: {lines[0]}\n"
                   f"End:   {lines[-1]}\n"
                   f"High:  ${hist['Close'].max():.2f}\n"
                   f"Low:   ${hist['Close'].min():.2f}\n\n")
        return summary + "\n".join(lines)
    except Exception as e:
        return f"Error fetching {ticker}: {str(e)}"

@mcp.tool()
def get_stock_info(ticker: str) -> str:
    """Fetch key fundamentals for a stock: P/E ratio, market cap, dividend yield,
    52-week high/low, sector, and analyst target price.
    ticker: stock symbol e.g. AVGO, NVDA, TSLA
    """
    try:
        import yfinance as yf
        info = yf.Ticker(ticker.upper()).info
        def fmt(val, prefix="", suffix="", billions=False):
            if val is None:
                return "N/A"
            if billions:
                return f"${val/1e9:.2f}B"
            return f"{prefix}{val:,.2f}{suffix}"

        # Dividend yield: yfinance's dividendYield field is a whole
        # percentage number (2.45 = 2.45%), confirmed Sept 2026 via direct
        # cross-check against dividendRate/currentPrice across yield levels
        # from 0.43% to 2.7% — no scaling needed. The previous "< 1" check
        # was built around an earlier yfinance quirk (a specific ticker
        # returning e.g. 28 instead of 0.28) that no longer reproduces;
        # that check was silently discarding every real yield >= 1%, which
        # is most mature/value dividend payers. Kept a generous upper
        # bound as a sanity check for any future genuinely anomalous value.
        raw_dy = info.get('dividendYield')
        if raw_dy and 0 < raw_dy <= 50:
            dy_pct = raw_dy
            div_yield_str = fmt(dy_pct, suffix="%")
        else:
            div_yield_str = 'None'

        # Price/FCF
        price  = info.get('currentPrice') or info.get('regularMarketPrice')
        fcf    = info.get('freeCashflow')
        shares = info.get('sharesOutstanding')
        if fcf and shares and shares > 0 and price:
            fcf_per_share = fcf / shares
            pfcf = price / fcf_per_share if fcf_per_share > 0 else None
            pfcf_str = f"{pfcf:.1f}x" if pfcf else "N/A"
        else:
            pfcf_str = "N/A"

        ps = info.get('priceToSalesTrailing12Months')

        return (
            f"=== {info.get('longName', ticker.upper())} ({ticker.upper()}) ===\n"
            f"Sector:            {info.get('sector', 'N/A')}\n"
            f"Industry:          {info.get('industry', 'N/A')}\n"
            f"Current Price:     ${info.get('currentPrice', info.get('regularMarketPrice', 'N/A'))}\n"
            f"Market Cap:        {fmt(info.get('marketCap'), billions=True)}\n"
            f"P/E Ratio (TTM):   {fmt(info.get('trailingPE'))}\n"
            f"Forward P/E:       {fmt(info.get('forwardPE'))}\n"
            f"PEG Ratio:         {fmt(info.get('pegRatio'))}\n"
            f"Price/Sales:       {f'{ps:.2f}x' if ps else 'N/A'}\n"
            f"Price/FCF:         {pfcf_str}\n"
            f"Free Cash Flow:    {fmt(fcf, billions=True)}\n"
            f"Debt/Equity:       {fmt(info.get('debtToEquity'), suffix='%') if info.get('debtToEquity') else 'N/A'}\n"
            f"EPS (TTM):         {fmt(info.get('trailingEps'), prefix='$')}\n"
            f"52-Week High:      {fmt(info.get('fiftyTwoWeekHigh'), prefix='$')}\n"
            f"52-Week Low:       {fmt(info.get('fiftyTwoWeekLow'), prefix='$')}\n"
            f"Dividend Yield:    {div_yield_str}\n"
            f"Analyst Target:    {fmt(info.get('targetMeanPrice'), prefix='$')}\n"
            f"Recommendation:    {info.get('recommendationKey', 'N/A').upper()}\n"
            f"Revenue (TTM):     {fmt(info.get('totalRevenue'), billions=True)}\n"
            f"Profit Margin:     {fmt(info.get('profitMargins') * 100 if info.get('profitMargins') is not None else None, suffix='%')}\n"
        )
    except Exception as e:
        return f"Error fetching info for {ticker}: {str(e)}"


_XBRL_BS_CACHE = {}


def _xbrl_cash_debt_fcf(ticker, _facts=None):
    """SEC XBRL fallback for fields Yahoo leaves N/A (Nov 9 2026: VST showed Total Cash,
    Total Debt and Free Cash Flow all N/A, so Jupiter's table had no FCF/debt).
    Returns {'cash': (val, end), 'debt': (val, end, tag), 'fcf': (val, fy_end), 'ocf': val,
    'capex': val} with only the fields that could be found, or {} on any failure.
    Cash and debt are the latest balance-sheet instant from a 10-Q/10-K; FCF is the latest
    fiscal year (10-K) operating cash flow minus capex, so it is labelled FY, not TTM."""
    import time
    import json
    import urllib.request
    from datetime import datetime
    key = ticker.upper()
    hit = _XBRL_BS_CACHE.get(key)
    if hit and time.time() - hit[0] < 6 * 3600 and _facts is None:
        return hit[1]
    out = {}
    try:
        if _facts is None:
            hdr = {'User-Agent': 'Jay jay@example.com'}

            def _get(url):
                req = urllib.request.Request(url, headers=hdr)
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read())
            cands = [e for e in _get('https://www.sec.gov/files/company_tickers.json').values()
                     if e.get('ticker', '').upper() == key]
            if key == 'XOM':
                cands.append({'cik_str': '34088'})
            for e in cands:
                try:
                    d = _get('https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json' % str(e['cik_str']).zfill(10))
                except Exception:
                    continue
                _facts = d.get('facts', {}).get('us-gaap', {})
                if _facts:
                    break
        if not _facts:
            return {}

        def _items(tag):
            return _facts.get(tag, {}).get('units', {}).get('USD', [])

        def _latest_instant(tag):
            best = None
            for x in _items(tag):
                if x.get('form') in ('10-Q', '10-Q/A', '10-K', '10-K/A') and 'end' in x and x.get('val') is not None:
                    if best is None or x['end'] > best['end'] or (x['end'] == best['end'] and x.get('filed', '') > best.get('filed', '')):
                        best = x
            return best

        def _latest_fy(tag):
            best = None
            for x in _items(tag):
                if x.get('form') in ('10-K', '10-K/A') and 'start' in x and 'end' in x and x.get('val') is not None:
                    try:
                        days = (datetime.strptime(x['end'], '%Y-%m-%d') - datetime.strptime(x['start'], '%Y-%m-%d')).days
                    except Exception:
                        continue
                    if 350 <= days <= 380:
                        if best is None or x['end'] > best['end'] or (x['end'] == best['end'] and x.get('filed', '') > best.get('filed', '')):
                            best = x
            return best

        def _fresh(x, days):
            try:
                return (datetime.utcnow() - datetime.strptime(x['end'], '%Y-%m-%d')).days <= days
            except Exception:
                return False

        for tag in ('CashAndCashEquivalentsAtCarryingValue',
                    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'):
            x = _latest_instant(tag)
            if x and _fresh(x, 200):
                out['cash'] = (float(x['val']), x['end'])
                break
        x = _latest_instant('LongTermDebt')
        if x and _fresh(x, 200):
            out['debt'] = (float(x['val']), x['end'], 'LongTermDebt')
        else:
            n = _latest_instant('LongTermDebtNoncurrent')
            if n and _fresh(n, 200):
                c = _latest_instant('LongTermDebtCurrent')
                cv = float(c['val']) if c and c['end'] == n['end'] else 0.0
                out['debt'] = (float(n['val']) + cv, n['end'], 'LongTermDebtNoncurrent+Current')
        ocf = None
        for tag in ('NetCashProvidedByUsedInOperatingActivities',
                    'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'):
            ocf = _latest_fy(tag)
            if ocf:
                break
        capex = None
        for tag in ('PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets',
                    'PaymentsForCapitalImprovements'):
            capex = _latest_fy(tag)
            if capex and ocf and capex['end'] == ocf['end']:
                break
            capex = None
        if ocf and capex and ocf['end'] == capex['end'] and _fresh(ocf, 600):
            out['ocf'] = float(ocf['val'])
            out['capex'] = float(capex['val'])
            out['fcf'] = (float(ocf['val']) - float(capex['val']), ocf['end'])
    except Exception:
        out = {}
    if _facts is None or out:
        _XBRL_BS_CACHE[key] = (time.time(), out)
    return out


@mcp.tool()
def get_fundamentals(ticker: str) -> str:
    """Fetch extended fundamental valuation metrics: PEG ratio, Price/Sales,
    Price/Free-Cash-Flow, Free Cash Flow, Debt/Equity, cash position, and
    revenue/earnings growth rates. Complements get_stock_info.
    ticker: stock symbol e.g. AVGO, NVDA, TSLA
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker.upper())
        info = t.info

        def fmt(val, prefix="", suffix="", decimals=2, billions=False, pct=False):
            if val is None:
                return "N/A"
            if billions:
                return f"${val/1e9:.2f}B"
            if pct:
                return f"{val*100:.1f}%"
            return f"{prefix}{val:,.{decimals}f}{suffix}"

        # Nov 9 2026: when Yahoo leaves Free Cash Flow / Total Cash / Total Debt empty
        # (VST), fall back to SEC XBRL so the data pack still has FCF and debt. The
        # source and as-of date are printed on the line so nothing is passed off as Yahoo.
        fcf = info.get('freeCashflow')
        _xb = {}
        if not fcf or info.get('totalDebt') is None or info.get('totalCash') is None:
            _xb = _xbrl_cash_debt_fcf(ticker)
        fcf_note = ""
        if not fcf and _xb.get('fcf'):
            fcf = _xb['fcf'][0]
            fcf_note = f"  (SEC XBRL FY ending {_xb['fcf'][1]}: operating CF minus capex)"
        fcf_str = (fmt(fcf, billions=True) if fcf else "N/A") + (fcf_note if fcf else "")

        # Price/FCF
        price = info.get('currentPrice') or info.get('regularMarketPrice')
        shares = info.get('sharesOutstanding')
        if fcf and shares and shares > 0 and price:
            fcf_per_share = fcf / shares
            pfcf = price / fcf_per_share if fcf_per_share > 0 else None
            pfcf_str = f"{pfcf:.1f}x" if pfcf else "N/A"
        else:
            pfcf_str = "N/A"

        # Price/Sales
        ps = info.get('priceToSalesTrailing12Months')
        ps_str = f"{ps:.2f}x" if ps else "N/A"

        # Debt metrics
        total_debt = info.get('totalDebt')
        total_cash = info.get('totalCash')
        de_ratio   = info.get('debtToEquity')
        debt_note = cash_note = ""
        if total_debt is None and _xb.get('debt'):
            total_debt = _xb['debt'][0]
            debt_note = f"  (SEC XBRL {_xb['debt'][2]}, as of {_xb['debt'][1]})"
        if total_cash is None and _xb.get('cash'):
            total_cash = _xb['cash'][0]
            cash_note = f"  (SEC XBRL, as of {_xb['cash'][1]})"

        lines = [f"=== {ticker.upper()} Extended Fundamentals ===\n"]

        lines.append("── Valuation Multiples ──")
        lines.append(f"  PEG Ratio:         {fmt(info.get('pegRatio'), decimals=2)}")
        lines.append(f"  Price/Sales:       {ps_str}")
        lines.append(f"  Price/Book:        {fmt(info.get('priceToBook'), decimals=2)}")
        lines.append(f"  Price/FCF:         {pfcf_str}")
        lines.append(f"  EV/Revenue:        {fmt(info.get('enterpriseToRevenue'), decimals=2)}")
        lines.append(f"  EV/EBITDA:         {fmt(info.get('enterpriseToEbitda'), decimals=2)}")

        lines.append("\n── Cash Flow ──")
        lines.append(f"  Free Cash Flow:    {fcf_str}")
        lines.append(f"  Operating CF:      {fmt(info.get('operatingCashflow'), billions=True)}")
        lines.append(f"  FCF Margin:        {fmt(fcf / info['totalRevenue'] if fcf and info.get('totalRevenue') else None, pct=True)}")

        lines.append("\n── Balance Sheet ──")
        lines.append(f"  Total Cash:        {fmt(total_cash, billions=True)}{cash_note if total_cash is not None else ''}")
        lines.append(f"  Total Debt:        {fmt(total_debt, billions=True)}{debt_note if total_debt is not None else ''}")
        lines.append(f"  Debt/Equity:       {fmt(de_ratio, suffix='%') if de_ratio else 'N/A'}")
        lines.append(f"  Current Ratio:     {fmt(info.get('currentRatio'), decimals=2)}")
        lines.append(f"  Quick Ratio:       {fmt(info.get('quickRatio'), decimals=2)}")

        lines.append("\n── Growth Rates ──")
        rev_growth = info.get('revenueGrowth')
        earn_growth = info.get('earningsGrowth')
        lines.append(f"  Revenue Growth YoY:   {fmt(rev_growth, pct=True)}")
        lines.append(f"  Earnings Growth YoY:  {fmt(earn_growth, pct=True)}")
        lines.append(f"  EPS (TTM):            {fmt(info.get('trailingEps'), prefix='$')}")
        lines.append(f"  EPS (Forward):        {fmt(info.get('forwardEps'), prefix='$')}")
        lines.append(f"  Gross Margins:        {fmt(info.get('grossMargins'), pct=True)}")
        lines.append(f"  Operating Margins:    {fmt(info.get('operatingMargins'), pct=True)}")
        lines.append(f"  Profit Margins:       {fmt(info.get('profitMargins'), pct=True)}")

        lines.append("\n── Shares ──")
        lines.append(f"  Shares Outstanding: {fmt(info.get('sharesOutstanding'), billions=True).replace('$','')}")
        lines.append(f"  Float:              {fmt(info.get('floatShares'), billions=True).replace('$','')}")
        lines.append(f"  Buyback (52W):      {fmt(info.get('sharesPercentSharesOut'), pct=True)}")

        lines.append("\nSource: Yahoo Finance")
        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching fundamentals for {ticker}: {str(e)}"


@mcp.tool()
def get_technicals(ticker: str) -> str:
    """Compute technical indicators from price history: RSI(14), 50-day and
    200-day moving averages, MA cross signal (golden/death/neutral), and
    price position relative to MAs. Useful for identifying oversold re-entry points.
    ticker: stock symbol e.g. AVGO, NVDA, TSLA
    """
    try:
        import yfinance as yf

        t = yf.Ticker(ticker.upper())
        hist = t.history(period="1y")[["Close", "High", "Low"]]
        if hist.empty or len(hist) < 20:
            return f"Insufficient price history for {ticker}."

        closes = hist["Close"].values
        highs  = hist["High"].values
        lows   = hist["Low"].values

        # ── RSI(14) ──
        def compute_rsi(prices, period=14):
            deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
            gains  = [d if d > 0 else 0 for d in deltas]
            losses = [-d if d < 0 else 0 for d in deltas]
            if len(gains) < period:
                return None
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period
            for i in range(period, len(gains)):
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                return 100.0
            rs = avg_gain / avg_loss
            return round(100 - (100 / (1 + rs)), 2)

        rsi = compute_rsi(closes)

        # ── Moving Averages ──
        current = closes[-1]
        ma50  = sum(closes[-50:])  / 50  if len(closes) >= 50  else None
        ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None

        # MA cross signal
        # Nov 9 2026: the old label said "DEATH CROSS" whenever ma50 < ma200, even
        # when the cross happened months ago and price had since recovered above
        # both averages (NEE, SO, DUK). A cross is an EVENT; ma50-vs-ma200 is a
        # STATE. Look back over the last 30 sessions for the actual crossover and
        # only use the CROSS wording if one occurred; otherwise describe the state.
        if ma50 and ma200:
            _cross_kind, _cross_date, _cross_ago = None, None, None
            try:
                _n = len(closes)
                _idx = list(hist.index)
                _prev_state = None
                _look = min(30, _n - 200)
                for _k in range(_n - _look - 1, _n):
                    if _k < 199:
                        continue
                    _m50 = sum(closes[_k - 49:_k + 1]) / 50
                    _m200 = sum(closes[_k - 199:_k + 1]) / 200
                    _state = _m50 > _m200
                    if _prev_state is not None and _state != _prev_state:
                        _cross_kind = "GOLDEN" if _state else "DEATH"
                        _cross_date = str(_idx[_k])[:10]
                        _cross_ago = _n - 1 - _k
                    _prev_state = _state
            except Exception:
                _cross_kind = None
            _above_both = current > ma50 and current > ma200
            _below_both = current < ma50 and current < ma200
            _px = (" | price above both MAs" if _above_both
                   else " | price below both MAs" if _below_both
                   else " | price between the MAs")
            if _cross_kind == "GOLDEN":
                ma_signal = (f"GOLDEN CROSS on {_cross_date} ({_cross_ago} sessions ago, bullish)" + _px)
            elif _cross_kind == "DEATH":
                ma_signal = (f"DEATH CROSS on {_cross_date} ({_cross_ago} sessions ago, bearish)" + _px)
            elif ma50 > ma200:
                ma_signal = "50D above 200D (bullish structure, no cross in last 30 sessions)" + _px
            else:
                ma_signal = "50D below 200D (bearish structure, no cross in last 30 sessions)" + _px
        else:
            ma_signal = "Insufficient data"

        # Price vs MAs
        def vs_ma(price, ma):
            if ma is None:
                return "N/A"
            pct = (price - ma) / ma * 100
            direction = "above" if pct > 0 else "below"
            return f"{abs(pct):.1f}% {direction}"

        # RSI interpretation
        if rsi is None:
            rsi_interp = "N/A"
        elif rsi <= 30:
            rsi_interp = "OVERSOLD — potential buy signal"
        elif rsi <= 45:
            rsi_interp = "Weakening — approaching oversold"
        elif rsi <= 55:
            rsi_interp = "Neutral"
        elif rsi <= 70:
            rsi_interp = "Strengthening"
        else:
            rsi_interp = "OVERBOUGHT — caution"

        # 52W high — prefer Yahoo's own fiftyTwoWeekHigh field (same field
        # get_stock_info reports) over recomputing from history(). The
        # High-vs-Close fix below still matters for the fallback path, but
        # it wasn't the whole story: yf.Ticker.history() defaults to
        # auto_adjust=True, which back-adjusts historical prices for every
        # dividend paid since — lowering computed past highs for any
        # dividend-paying name, while info['fiftyTwoWeekHigh'] reports the
        # actual raw traded price. That's a systematic gap sized to
        # dividend yield, which is exactly why Jansky saw this conflict
        # cluster in Utilities, Healthcare, and Packaging (income-oriented
        # sectors) rather than low/no-dividend growth names, even after the
        # earlier Close→High fix. Falls back to the computed value (still
        # uses intraday High, not Close, matching Yahoo's own convention)
        # only if the live field is unavailable.
        try:
            info_52w = t.info.get('fiftyTwoWeekHigh')
        except Exception:
            info_52w = None
        if info_52w:
            high_52w = info_52w
        else:
            high_52w = max(highs[-252:]) if len(highs) >= 252 else max(highs)
        drawdown = (current - high_52w) / high_52w * 100
        # Nov 3 2026: Jansky flagged DPZ's 52-week-low reference as a possible
        # stop-loss level that "may not exist as stated". The technicals output only
        # carried the HIGH, so an analyst quoting a low had to take it from raw
        # price tables (dividend-adjusted, so lower than the real traded low) or
        # from get_stock_info. Report the low here from the same source and
        # convention as the high: Yahoo's own field first, intraday lows as fallback.
        try:
            info_52w_low = t.info.get('fiftyTwoWeekLow')
        except Exception:
            info_52w_low = None
        low_52w = info_52w_low if info_52w_low else (min(lows[-252:]) if len(lows) >= 252 else min(lows))
        above_low = (current - low_52w) / low_52w * 100 if low_52w else None

        lines = [f"=== {ticker.upper()} Technical Indicators ===\n"]
        lines.append(f"Current Price:     ${current:.2f}")
        lines.append(f"52W High:          ${high_52w:.2f}")
        lines.append(f"52W Low:           ${low_52w:.2f}"
                     + (f"  (price is {above_low:+.1f}% vs the low)" if above_low is not None else ""))
        lines.append(f"Drawdown from High: {drawdown:.1f}%\n")

        lines.append("── Momentum ──")
        lines.append(f"  RSI (14):          {rsi if rsi else 'N/A'}")
        lines.append(f"  RSI Signal:        {rsi_interp}\n")

        lines.append("── Moving Averages ──")
        lines.append(f"  50-Day MA:         ${ma50:.2f}" if ma50 else "  50-Day MA:         N/A")
        lines.append(f"  200-Day MA:        ${ma200:.2f}" if ma200 else "  200-Day MA:        N/A")
        lines.append(f"  MA Signal:         {ma_signal}")
        lines.append(f"  vs 50-Day MA:      {vs_ma(current, ma50)}")
        lines.append(f"  vs 200-Day MA:     {vs_ma(current, ma200)}\n")

        # Re-entry signal summary
        signals = []
        if rsi and rsi <= 35:
            signals.append("RSI oversold")
        if ma50 and current < ma50:
            signals.append("below 50D MA")
        if ma200 and current < ma200:
            signals.append("below 200D MA")
        if drawdown <= -20:
            signals.append(f"{drawdown:.0f}% off 52W high")

        if signals:
            lines.append(f"Re-entry Signals:  {', '.join(signals)}")
        else:
            lines.append("Re-entry Signals:  None — not technically oversold")

        lines.append("\nSource: Yahoo Finance price history (computed locally)")
        return "\n".join(lines)

    except Exception as e:
        return f"Error computing technicals for {ticker}: {str(e)}"


@mcp.tool()
def get_institutional(ticker: str, top_n: int = 15) -> str:
    """Fetch institutional and mutual fund ownership data from Yahoo Finance.
    Shows top holders, recent changes (additions/reductions), and ownership concentration.
    Useful for detecting smart money accumulation or distribution.
    ticker: stock symbol e.g. AVGO, NVDA, TSLA
    top_n: number of top holders to show (default 15)
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker.upper())
        info = t.info

        lines = [f"=== {ticker.upper()} Institutional Ownership ===\n"]

        # Summary stats
        inst_pct = info.get('institutionPercent') or info.get('heldPercentInstitutions')
        insider_pct = info.get('insiderPercent') or info.get('heldPercentInsiders')
        lines.append("── Ownership Summary ──")
        # Oct 2026: Yahoo's institutional % adds up overlapping 13F filings
        # (the same shares reported by several filers), so it can exceed
        # 100% — it showed up in six Packaging tickers plus CFG and cost an
        # analyst flag per ticker per week. Label it as an artifact instead
        # of printing it as a fact, and don't derive "% public" from it.
        inst_ok = bool(inst_pct) and 0 < inst_pct <= 1.0
        if inst_pct and not inst_ok:
            lines.append(
                f"  Institutional:  {inst_pct*100:.1f}%  ⚠ EXCEEDS 100% — Yahoo/13F "
                f"double-counting artifact, not a real ownership level. "
                f"Use the top-holder % Out figures below instead.")
        else:
            lines.append(f"  Institutional:  {inst_pct*100:.1f}%" if inst_pct else "  Institutional:  N/A")
        lines.append(f"  Insider:        {insider_pct*100:.1f}%" if insider_pct else "  Insider:        N/A")
        # Oct 26 2026 (REYN): insider 74.1% + institutional 32.1% = 106%. Yahoo's
        # institutional % counts shares from holders who are themselves insiders/
        # parents (REYN's controlling shareholder files a 13F), so the two overlap
        # and must not be added. Use floatShares/sharesOutstanding for the real float.
        _fl, _so = info.get('floatShares'), info.get('sharesOutstanding')
        free_float = (_fl / _so) if (_fl and _so and _so > 0 and _fl <= _so * 1.001) else None
        if inst_ok and insider_pct and inst_pct + insider_pct > 1.0:
            lines.append(
                f"  ⚠ Insider ({insider_pct*100:.1f}%) + institutional ({inst_pct*100:.1f}%) = "
                f"{(inst_pct+insider_pct)*100:.0f}% — the two overlap (controlling holder also "
                f"files as an institution) and are NOT additive. This is a controlled company; "
                f"treat the insider stake as the real concentration figure.")
        if free_float is not None:
            lines.append(f"  Free float:     {free_float*100:.1f}% of shares outstanding "
                         f"(floatShares/sharesOutstanding)")
        elif inst_ok and not (insider_pct and inst_pct + insider_pct > 1.0):
            lines.append(f"  Float:          {max(0.0, (1 - inst_pct - (insider_pct or 0))*100):.1f}% public")

        # Top institutional holders
        try:
            inst = t.institutional_holders
            if inst is not None and not inst.empty:
                lines.append(f"\n── Top {min(top_n, len(inst))} Institutional Holders ──")
                lines.append(f"  {'Holder':<35} {'Shares':>12} {'% Out':>7} {'Value':>12} {'Change':>10}")
                lines.append("  " + "─" * 80)
                for _, row in inst.head(top_n).iterrows():
                    holder  = str(row.get('Holder', 'Unknown'))[:34]
                    shares  = row.get('Shares', 0)
                    pct_out = row.get('pctHeld') or row.get('% Out', 0)
                    value   = row.get('Value', 0)
                    change  = row.get('pctChange') or row.get('% Change')

                    shares_str = f"{shares/1e6:.1f}M" if shares else "N/A"
                    pct_str    = f"{pct_out*100:.2f}%" if pct_out else "N/A"
                    val_str    = f"${value/1e9:.2f}B" if value and value > 1e8 \
                                 else (f"${value/1e6:.0f}M" if value else "N/A")
                    if change is not None:
                        arrow = "▲" if change > 0 else ("▼" if change < 0 else "─")
                        chg_str = f"{arrow}{abs(change)*100:.1f}%"
                    else:
                        chg_str = "N/A"

                    lines.append(f"  {holder:<35} {shares_str:>12} {pct_str:>7} {val_str:>12} {chg_str:>10}")
            else:
                lines.append("\nInstitutional holders: No data available.")
        except Exception as e:
            lines.append(f"\nInstitutional holders: Could not retrieve ({e})")

        # Top mutual fund holders
        try:
            mf = t.mutualfund_holders
            if mf is not None and not mf.empty:
                lines.append(f"\n── Top {min(5, len(mf))} Mutual Fund Holders ──")
                for _, row in mf.head(5).iterrows():
                    holder = str(row.get('Holder', 'Unknown'))[:40]
                    pct_out = row.get('pctHeld') or row.get('% Out', 0)
                    pct_str = f"{pct_out*100:.2f}%" if pct_out else "N/A"
                    lines.append(f"  {holder:<40} {pct_str:>7}")
        except Exception:
            pass

        # Accumulation/distribution signal
        try:
            inst = t.institutional_holders
            if inst is not None and not inst.empty:
                changes = [row.get('pctChange') or row.get('% Change', 0)
                           for _, row in inst.iterrows()
                           if (row.get('pctChange') or row.get('% Change')) is not None]
                if changes:
                    # Nov 3 2026: AEP showed "+35.1% average institutional change" and
                    # it was read as live accumulation. A mean over ~10 holders is moved
                    # by a single new position or restated filing (a +300% line is
                    # enough), so the signal now uses the MEDIAN and says when the mean
                    # is being pulled by outliers; and flags when the 13F "date reported"
                    # values span many months (changes then compare different periods).
                    import statistics as _st
                    mean_change = sum(changes) / len(changes)
                    avg_change = _st.median(changes)
                    net_adding = sum(1 for c in changes if c > 0)
                    net_reducing = sum(1 for c in changes if c < 0)
                    _outl = [c for c in changes if abs(c) > 1.0]
                    lines.append(f"\n── Accumulation Signal ──")
                    lines.append(f"  Median position change: {avg_change*100:+.1f}%   "
                                 f"(mean {mean_change*100:+.1f}%)")
                    if _outl or abs(mean_change - avg_change) > 0.10:
                        lines.append(
                            f"  ⚠ The mean is pulled by {len(_outl)} holder(s) with changes beyond ±100% "
                            f"(new positions or restated filings: "
                            f"{', '.join(f'{c*100:+.0f}%' for c in _outl[:3]) or 'wide spread'}). "
                            f"The signal below uses the MEDIAN; do not cite the mean as accumulation.")
                    try:
                        _dr = [d for d in inst['Date Reported'].tolist() if d is not None]
                        if _dr:
                            _d0, _d1 = min(_dr), max(_dr)
                            _span = (_d1 - _d0).days if hasattr(_d1 - _d0, 'days') else 0
                            if _span > 100:
                                lines.append(
                                    f"  ⚠ 13F 'date reported' values span {_span} days "
                                    f"({str(_d0)[:10]} to {str(_d1)[:10]}): position changes compare "
                                    f"different filing periods; treat the signal as weak.")
                    except Exception:
                        pass
                    lines.append(f"  Adding: {net_adding} / Reducing: {net_reducing} institutions")
                    if avg_change > 0.02:
                        lines.append("  Signal: 🟢 Net accumulation — institutions increasing positions")
                    elif avg_change < -0.02:
                        lines.append("  Signal: 🔴 Net distribution — institutions reducing positions")
                    else:
                        lines.append("  Signal: ⚪ Neutral — mixed institutional activity")
        except Exception:
            pass

        lines.append("\nSource: Yahoo Finance institutional ownership data")
        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching institutional data for {ticker}: {str(e)}"
    
@mcp.tool()
def get_earnings(ticker: str) -> str:    
    """Fetch earnings history, next earnings date, and annual revenue trend for a stock.
    ticker: stock symbol e.g. AVGO, NVDA, TSLA
    """
    try:
        import yfinance as yf
        from datetime import datetime, timezone

        t = yf.Ticker(ticker.upper())
        info = t.info

        # Next earnings date
        # Oct 2026: yfinance's earningsTimestamp is often the LAST report
        # date rather than the next one (XEL showed 2026-07-30 in October;
        # AMGN, ELV, V and DVN had the same problem), and nothing checked it
        # against today. A date in the past is never reported as "next":
        # fall back to the estimate window, then the earnings calendar, then
        # an explicit "not yet published".
        def _fmt_ts(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')

        cutoff_ts = datetime.now(timezone.utc).timestamp() - 86400  # today still counts
        ts_main = info.get('earningsTimestamp')
        ts_low = info.get('earningsTimestampStart')
        ts_high = info.get('earningsTimestampEnd')

        next_str = None
        if ts_main and ts_main >= cutoff_ts:
            next_str = _fmt_ts(ts_main)
        elif ts_low and ts_high and ts_high >= cutoff_ts:
            low_str, high_str = _fmt_ts(ts_low), _fmt_ts(ts_high)
            next_str = (f"{low_str} (estimate)" if low_str == high_str
                        else f"{low_str} to {high_str} (estimate)")
        else:
            try:
                ed = t.get_earnings_dates(limit=12)
                if ed is not None and not ed.empty:
                    now_utc = datetime.now(timezone.utc)
                    upcoming = []
                    for d in ed.index:
                        py = d.to_pydatetime()
                        if py.tzinfo is None:
                            py = py.replace(tzinfo=timezone.utc)
                        if py.timestamp() >= cutoff_ts:
                            upcoming.append(py)
                    if upcoming:
                        next_str = (min(upcoming).strftime('%Y-%m-%d')
                                    + " (from earnings calendar)")
            except Exception:
                pass
        if next_str is None:
            if ts_main:
                next_str = (f"N/A — next date not yet published "
                            f"(Yahoo's last listed date was {_fmt_ts(ts_main)}, now past)")
            else:
                next_str = "N/A"

        lines = [f"=== {ticker.upper()} Earnings Report ===\n"]
        lines.append(f"Next Earnings Date: {next_str}\n")

       # Quarterly EPS history
        # Sept 2026: yfinance's Ticker.earnings_history has been going
        # empty/None for a growing share of tickers (a Yahoo-side data
        # change, not something this code controls) — matches Jansky's
        # report of missing beat/miss coverage across whole sectors
        # (all 8 Semis, all 8 Healthcare) rather than scattered individual
        # tickers, which looks like a systemic source change rather than
        # per-ticker noise. Added Ticker.get_earnings_dates() as a second
        # attempt when the first returns nothing — it's yfinance's newer,
        # still-maintained API for the same reported-vs-estimated EPS data,
        # under different column names. NOT yet confirmed live (this
        # sandbox has no network path to Yahoo Finance) — please verify
        # against a couple of the previously-missing Semis/Healthcare
        # tickers on the next run.
        try:
            hist = t.earnings_history
            # Oct 2026: also fall back when earnings_history is merely
            # sparse (MU came back with 3 odd rows, not empty), and when
            # using get_earnings_dates() keep only REPORTED quarters, oldest
            # to newest — it returns newest-first with future (NaN) rows
            # first, so the old .tail(8) kept the OLDEST rows on the page.
            if hist is None or hist.empty or len(hist) < 4:
                try:
                    alt = t.get_earnings_dates(limit=12)
                    if alt is not None and not alt.empty:
                        alt = alt.rename(columns={
                            'Reported EPS': 'epsActual',
                            'EPS Estimate': 'epsEstimate',
                            'Surprise(%)':  'surprisePct',
                        })
                        if 'epsActual' in alt.columns:
                            alt = alt[alt['epsActual'].notna()].sort_index()
                        if hist is None or hist.empty or len(alt) > len(hist):
                            hist = alt
                except Exception:
                    if hist is not None and hist.empty:
                        hist = None
            if hist is not None and not hist.empty:
                hist = hist.tail(8)
                lines.append("\nQuarterly EPS (last 8 quarters):")
                lines.append(f"  {'Quarter':<12} {'Actual':>8} {'Estimate':>10} {'Surprise':>10} {'Beat?':>6}")
                lines.append("  " + "-" * 50)
                lines.append("  (Yahoo 'Actual' is normally the company's ADJUSTED/non-GAAP EPS — it will "
                             "differ from the GAAP diluted EPS in the SEC EDGAR table; neither is wrong.)")
                def _clean(v):
                    return None if (v is None or (isinstance(v, float) and v != v)) else v
                for idx, row in hist.iterrows():
                    q = row.get('quarter', idx)
                    quarter = q.strftime('%Y-%m-%d') if hasattr(q, 'strftime') else str(q)
                    actual = _clean(row.get('epsActual'))
                    estimate = _clean(row.get('epsEstimate'))
                    # get_earnings_dates() gives surprisePct (%), not the
                    # raw EPS-dollar diff epsDifference gives — compute the
                    # dollar surprise from actual/estimate when only the
                    # percent form is available, so downstream formatting
                    # (and the beat/miss sign) stays consistent either way.
                    surprise = _clean(row.get('epsDifference'))
                    if surprise is None and actual is not None and estimate is not None:
                        surprise = actual - estimate
                    beat = "✓" if surprise and surprise > 0 else "✗"
                    act_str = f"${actual:>7.2f}" if actual is not None else "    N/A"
                    est_str = f"${estimate:>9.2f}" if estimate is not None else "       N/A"
                    sur_str = f"{surprise:>+9.2f}" if surprise is not None else "       N/A"
                    lines.append(f"  {quarter:<12} {act_str} {est_str} {sur_str} {beat:>6}")
            else:
                lines.append("\nQuarterly EPS: No data available.")
        except Exception as e:
            lines.append(f"\nQuarterly EPS: Could not retrieve ({str(e)})")

      # Annual revenue trend
        try:
            financials = t.financials
            if financials is not None and not financials.empty:
                if 'Total Revenue' in financials.index:
                    revenue = financials.loc['Total Revenue'].sort_index()
                    lines.append("\nAnnual Revenue Trend:")
                    for date, val in revenue.items():
                        year = date.strftime('%Y') if hasattr(
                            date, 'strftime') else str(date)[:4]
                        if val and not (isinstance(val, float) 
                                       and __import__('math').isnan(val)):
                            lines.append(f"  {year}: ${val/1e9:.2f}B")
        except Exception:
            lines.append("\nAnnual Revenue: Could not retrieve.")

        # Annual EPS trend
        try:
            earnings = t.earnings
            if earnings is not None and not earnings.empty:
                lines.append("\nAnnual EPS Trend:")
                for idx, row in earnings.iterrows():
                    year = str(idx)[:4]
                    eps = row.get('Earnings', row.get('epsActual', None))
                    if eps:
                        lines.append(f"  {year}: ${eps:.2f}")
        except Exception:
            pass

        # Key forward metrics
        lines.append("\nForward Metrics:")
        lines.append(f"  EPS (TTM):        ${info.get('trailingEps', 'N/A')}")
        lines.append(f"  EPS (Forward):    ${info.get('forwardEps', 'N/A')}")
        lines.append(f"  Revenue (TTM):    ${info.get('totalRevenue', 0)/1e9:.2f}B"
                    if info.get('totalRevenue') else "  Revenue (TTM):    N/A")
        # Oct 2026: Yahoo returns growth and margin as raw RATIOS (0.608 means
        # 60.8%). Printed bare, the analyst read "Revenue Growth: 3.793" as
        # 3.8% when it is 379%. Always show percentages, label the basis, and
        # flag values that are almost certainly Yahoo data artifacts.
        def _pct_str(v, flag_over=None, basis=""):
            if not isinstance(v, (int, float)) or v != v:
                return "N/A"
            out = f"{v * 100:,.1f}%" + (f"  ({basis})" if basis else "")
            if flag_over is not None and abs(v) > flag_over:
                out += "  ⚠ extreme — likely a tiny-base or one-off effect, verify before use"
            return out

        lines.append(f"  Revenue Growth:   {_pct_str(info.get('revenueGrowth'), 2.0, 'latest quarter vs same quarter last year')}")
        lines.append(f"  Earnings Growth:  {_pct_str(info.get('earningsGrowth'), 2.0, 'latest quarter vs same quarter last year')}")
        _rg = info.get('revenueGrowth')
        if isinstance(_rg, (int, float)) and _rg == _rg and (_rg <= -0.4 or _rg >= 0.6):
            lines.append("  ⚠ Revenue growth this large usually means a spin-off, divestiture or acquisition "
                         "changed the company's scope, not organic change — confirm against the company's "
                         "continuing-operations figures before using it.")
        lines.append(f"  Profit Margin:    {_pct_str(info.get('profitMargins'), None, 'net income / revenue, TTM')}")
        lines.append(f"  P/E (TTM):        {info.get('trailingPE', 'N/A')}")
        lines.append(f"  P/E (Forward):    {info.get('forwardPE', 'N/A')}")
        _peg = info.get('pegRatio')
        _peg_note = ""
        if isinstance(_peg, (int, float)) and (_peg <= 0 or _peg > 10):
            _peg_note = "  ⚠ implausible PEG (Yahoo data artifact) — do not use"
        lines.append(f"  PEG Ratio:        {_peg if _peg is not None else 'N/A'}{_peg_note}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching earnings for {ticker}: {str(e)}"


# ── SEC filing R-page fallback (Nov 3 2026) ──────────────────────────────────
# SEC's companyfacts / companyconcept / frames APIs are one dataset, and for some
# filers it is missing whole 10-Qs (diag_lag.py, Nov 3: NEE has no Q2/Q3 2026, C no
# Q1/Q2, DUK no Q2, while the filings index lists them). The numbers are still in
# the filing's own XBRL "R" pages, so read the income statement from there.

def _sec_raw(url: str, timeout: int = 30) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'Jay jay@example.com'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _r_cells(row_html: str) -> list:
    import html as _h
    out = []
    for c in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row_html, re.S | re.I):
        t = _h.unescape(re.sub(r'<[^>]+>', ' ', c)).replace('\xa0', ' ')
        out.append(re.sub(r'\s+', ' ', t).strip())
    return out


def _r_num(s: str):
    s = s.replace('$', '').replace(',', '').strip()
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()').strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


_R_REV = [r'^total (operating )?(net )?revenues?(,? net( of interest expense)?)?$',
          r'^total revenues?\b', r'^(net )?revenues?(,? net( of interest expense)?)?$',
          r'^operating revenues$', r'^total operating revenues?\b', r'^net sales$',
          r'^total net sales\b', r'^sales$', r'^revenues?, net\b']
_R_NI = [r'^net (income|earnings|profit)( \(loss\))? (attributable to|available to)\b(?!.*non[- ]?controlling)',
         r'^net (income|earnings|profit)( \(loss\))?$',
         r"^.{0,40}\bnet (income|earnings)( \(loss\))?$"]


def _rpage_quarter(html_text: str):
    """Current-quarter revenue / net income / diluted EPS from an EDGAR R-page
    income statement, or None. Only 3-month columns are read."""
    rows = re.findall(r'<tr\b.*?</tr>', html_text, re.S | re.I)
    if len(rows) < 4:
        return None
    head = _r_cells(rows[0]) + _r_cells(rows[1])
    top = ' '.join(head).lower()
    if not re.search(r'3 months ended|13 weeks ended|three months ended', top):
        return None
    scale = 1e6 if 'in millions' in top else 1e3 if 'in thousands' in top else 1e9 if 'in billions' in top else 1.0
    dm = re.search(r'([A-Z][a-z]{2})[a-z]*\.? (\d{1,2}), (\d{4})', ' '.join(_r_cells(rows[1])) or ' '.join(head))
    if not dm:
        return None
    try:
        end = datetime.strptime(f"{dm.group(1)} {dm.group(2)} {dm.group(3)}", "%b %d %Y").strftime('%Y-%m-%d')
    except ValueError:
        return None
    items = []
    section = ''
    for r in rows[2:]:
        c = _r_cells(r)
        if len(c) < 2:
            continue
        label = re.sub(r'\s*\[\d+\]\s*$', '', c[0]).strip().lower().rstrip(':')
        # Some filers (NEE, C) put an empty cell between label and values:
        # take the first numeric cell, not blindly c[1].
        _cell = next((x for x in c[1:] if _r_num(x) is not None), None)
        v = _r_num(_cell) if _cell is not None else None
        if label and v is None:
            section = label          # header row such as "Diluted earnings per share:"
        elif label:
            items.append((label, v, section, '.' in _cell))

    def pick(patterns):
        for pat in patterns:
            for label, v, _sec, _dec in items:
                if re.search(pat, label):
                    return v
        return None

    rev, ni = pick(_R_REV), pick(_R_NI)
    _bad = r'in shares|weighted|shares outstanding|number of|average shares'
    # EPS is always printed with cents; a whole-number cell is a share count or $ amount.
    eps = next((v for label, v, _s, dec in items
                if ('diluted' in label or 'assuming dilution' in label)
                and dec and not re.search(_bad, label) and abs(v) < 100), None)
    if eps is None:   # "Diluted earnings per share:" header, then "Net income" beneath it
        eps = next((v for label, v, sec, dec in items
                    if re.search(r'diluted|assuming dilution', sec)
                    and not re.search(_bad, sec) and dec
                    and re.search(r'^net (income|earnings)|per share', label)
                    and not re.search(_bad, label) and abs(v) < 100), None)
    if ni is None and eps is None:
        return None
    return {'end': end,
            'rev': rev * scale if rev is not None else None,
            'ni': ni * scale if ni is not None else None,
            'eps': eps}


def _filing_quarter(cik_num: str, accn: str):
    """Fetch FilingSummary.xml, locate the income statement R page, parse it."""
    import html as _h
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{accn.replace('-', '')}/"
    fs = _sec_raw(base + 'FilingSummary.xml').decode('utf-8', 'replace')
    reports = []
    for blk in re.findall(r'<Report\b.*?</Report>', fs, re.S):
        fn = re.search(r'<HtmlFileName>(.*?)</HtmlFileName>', blk, re.S)
        sn = re.search(r'<ShortName>(.*?)</ShortName>', blk, re.S)
        if fn and sn:
            reports.append((_h.unescape(sn.group(1)).strip(), fn.group(1).strip()))
    for sn, fn in reports[:12]:
        if (re.search(r'(?i)income|operations|earnings', sn)
                and not re.search(r'(?i)comprehensive|parenthetical|equity|cash|balance|cover|tax|segment|detail|polic|note', sn)):
            q = _rpage_quarter(_sec_raw(base + fn).decode('utf-8', 'replace'))
            if q:
                q['page'] = fn
                return q
    return None


@mcp.tool()
def get_sec_earnings(ticker: str, quarters: int = 8) -> str:
    """Fetch quarterly EPS and revenue directly from SEC EDGAR filings.
    Returns up to 8 quarters of historical data — more reliable than Yahoo Finance.
    ticker: stock symbol e.g. AAPL, MSFT, TSLA
    quarters: number of quarters to return (default 8, max 12)
    """
    try:
        import urllib.request
        import json
        import math
        from datetime import datetime

        headers = {'User-Agent': 'Jay jay@example.com'}

        def sec_get(url):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())

        # Step 1: Resolve ticker to CIK
        tickers_url = 'https://www.sec.gov/files/company_tickers.json'
        tickers_data = sec_get(tickers_url)
        ticker_upper = ticker.upper()
        matches = [e for e in tickers_data.values()
                   if e.get('ticker', '').upper() == ticker_upper]
        if not matches:
            return f"Could not find SEC CIK for ticker {ticker_upper}. "\
                   f"Company may not file with SEC or ticker is incorrect."

        # SEC's ticker file lists XOM only under the new holding company
        # (ExxonMobil Holdings Corp, CIK 2115436), which has no 10-K/XBRL
        # history yet. Fall back to the legacy operating registrant that
        # filed the annual reports (Exxon Mobil Corp, CIK 34088).
        _LEGACY_CIK = {"XOM": ("34088", "Exxon Mobil Corp (legacy registrant)")}
        if ticker_upper in _LEGACY_CIK:
            _lc = _LEGACY_CIK[ticker_upper]
            if not any(str(m['cik_str']).zfill(10) == _lc[0].zfill(10) for m in matches):
                matches = matches + [{'cik_str': _lc[0], 'title': _lc[1]}]

        # Oct 20 2026: a ticker can map to several registrants (e.g. XOM ->
        # a holding-company CIK with no financial facts). Use the first
        # registrant that actually has US-GAAP facts.
        cik = None
        company_name = None
        facts = {}
        for entry in matches:
            _cik = str(entry['cik_str']).zfill(10)
            try:
                _data = sec_get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{_cik}.json')
            except Exception:
                continue
            _facts = _data.get('facts', {}).get('us-gaap', {})
            if _facts:
                cik, company_name, facts = _cik, entry.get('title', ticker_upper), _facts
                break
            if cik is None:
                cik, company_name = _cik, entry.get('title', ticker_upper)

        def _foreign_note():
            """Nov 9 2026: AEM and VALE (40-F/20-F + 6-K filers) printed 'No quarterly EPS
            data found', which reads like a failure. Foreign private issuers have no 10-Q,
            so SEC XBRL has no quarterly series; say so and name the tool that has the
            latest results. Returns '' when the filer is a normal 10-Q filer or on any error."""
            try:
                _recent = sec_get(f'https://data.sec.gov/submissions/CIK{cik}.json'
                                  ).get('filings', {}).get('recent', {}).get('form', [])[:250]
                if (any(_f in ('20-F', '40-F', '20-F/A', '40-F/A', '6-K') for _f in _recent)
                        and '10-Q' not in _recent):
                    return (f"NOTE: {company_name or ticker_upper} is a foreign private issuer "
                            f"(files 20-F/40-F annual reports and 6-K interim releases, no 10-Q), so "
                            f"SEC XBRL has no quarterly EPS series for it. This is expected for foreign filers. "
                            f"Use get_sec_earnings_release (novamcp) for the latest quarterly results "
                            f"and Nova's earnings-call record for the call.")
            except Exception:
                pass
            return ""

        if not facts:
            _fn = _foreign_note()
            return (f"No US-GAAP financial data found for {ticker_upper} "
                    f"(CIK: {cik}). Company may use IFRS reporting."
                    + (f"\n{_fn}" if _fn else ""))

        def _unit_items(concept, unit):
            """Facts for a concept in `unit`. Per-share concepts are not always filed
            under the literal 'USD/shares' key (e.g. partnership per-unit tags), so any
            'USD/…' unit is accepted for them."""
            units = facts.get(concept, {}).get('units', {})
            if unit in units:
                return units[unit]
            if unit.startswith('USD/'):
                for k, v in units.items():
                    if k.startswith('USD/'):
                        return v
            return []

        # Step 3: Extract quarterly data helper
        def get_quarterly(concept, unit):
            items = _unit_items(concept, unit)
            seen = {}
            for x in items:
                if x.get('form') in ('10-Q', '10-Q/A'):
                    try:
                        start = datetime.strptime(x['start'], '%Y-%m-%d')
                        end = datetime.strptime(x['end'], '%Y-%m-%d')
                        days = (end - start).days
                        if 75 <= days <= 110:
                            end_str = x['end']
                            if end_str not in seen or \
                               x['accn'] > seen[end_str]['accn']:
                                seen[end_str] = x
                    except Exception:
                        pass
            return sorted(seen.values(),
                         key=lambda x: x['end'], reverse=True)

        # Step 4: Get annual data helper
        # Fixed (Sept 2026): previously took a single concept tag with no
        # duration check, so it silently accepted whatever the SEC returned
        # under that tag — including years-old data. Two real bugs this
        # caused, confirmed on BALL/DUK: many companies stopped populating
        # the plain 'Revenues' tag after adopting ASC 606 (~2018) in favor
        # of 'RevenueFromContractWithCustomerExcludingAssessedTax'; when the
        # newer tag had no data for a ticker (a coverage gap, not the
        # ticker's fault), the old sequential fallback chain landed on
        # 'Revenues' and returned only its last populated years — 2012-2017
        # for BALL/DUK — with nothing to signal it was stale. Fixed by (1)
        # merging all known revenue tags together instead of an early-return
        # fallback chain, so a gap in one tag doesn't hide real data sitting
        # under another, and (2) applying the same 350-380 day duration
        # guard get_quarterly() already uses, so a tag that mixes in
        # five-year-selected-data-table entries or partial-year figures
        # can't contaminate the "annual" series.
        def get_annual(concepts, unit):
            if isinstance(concepts, str):
                concepts = [concepts]
            seen = {}
            for concept in concepts:
                items = _unit_items(concept, unit)
                for x in items:
                    if x.get('form') not in ('10-K', '10-K/A'):
                        continue
                    try:
                        start = datetime.strptime(x['start'], '%Y-%m-%d')
                        end = datetime.strptime(x['end'], '%Y-%m-%d')
                        days = (end - start).days
                        if not (350 <= days <= 380):
                            continue  # not a genuine full fiscal year
                        end_str = x['end'][:4]  # year
                        if end_str not in seen or \
                           x['accn'] > seen[end_str]['accn']:
                            seen[end_str] = x
                    except Exception:
                        pass
            return sorted(seen.values(),
                         key=lambda x: x['end'], reverse=True)

        def best_quarterly(concepts, unit):
            """Quarterly series from whichever candidate tag has the NEWEST data.

            The old code took the first tag that returned ANYTHING, so a filer that
            used a tag years ago and a different one now got its ancient series
            (EPD showed 2010/2011 quarters). First-listed wins ties, so preferred
            tags (diluted before basic) still win when equally current."""
            return best_quarterly_named(concepts, unit)[1]

        def best_quarterly_named(concepts, unit):
            """Like best_quarterly but also returns WHICH concept won."""
            best, name = [], None
            for c in concepts:
                q = get_quarterly(c, unit)
                if q and (not best or q[0]['end'] > best[0]['end']):
                    best, name = q, c
            return name, best

        def best_annual(concepts, unit):
            """Annual series from the candidate tag with the NEWEST 10-K data."""
            best = []
            for c in concepts:
                a = get_annual(c, unit)
                if a and (not best or a[0]['end'] > best[0]['end']):
                    best = a
            return best

        # Oct 26 2026: a filer can report TOTAL revenue under 'Revenues' and a
        # narrower slice under the ASC 606 tag (VST: contract revenue excludes
        # derivative/hedge results). The slice can be larger OR smaller than the
        # total (VST Q1'26 total 5.64B > 5.00B, Q2'26 total 4.02B < 4.40B), so a
        # max-merge is wrong. Choose per PERIOD: 'Revenues' (undimensioned total)
        # wins when it exists for that period, else fall through the other tags.
        # NEE (Oct 26): files its consolidated total under
        # RegulatedAndUnregulatedOperatingRevenue ($27.41B FY25) while the
        # IncludingAssessedTax tag holds a rounded contract-only slice ($6.1B
        # for a quarter whose total is $6.70B), so the utility total ranks high.
        REV_TAGS = ['Revenues',
                    'RegulatedAndUnregulatedOperatingRevenue',
                    'RevenueFromContractWithCustomerExcludingAssessedTax',
                    'SalesRevenueNet',
                    'RevenueFromContractWithCustomerIncludingAssessedTax',
                    'ElectricUtilityRevenue']

        def _merge_max(series_list):   # name kept; series_list is in priority order
            best = {}
            for ser in series_list:
                for x in ser:
                    best.setdefault(x['end'], x)
            return sorted(best.values(), key=lambda x: x['end'], reverse=True)

        quarters = min(quarters, 12)
        lines = [f"=== {company_name} ({ticker_upper}) — SEC EDGAR ===\n"]

        # Step 5: Quarterly EPS — newest-data tag wins (see best_quarterly)
        # Oct 24 2026: EPD's SEC facts show per-unit EPS under a tag the fixed list
        # below never had (its diluted/basic tags stop in 2011, its LP-unit tag in
        # 2013, while revenue and net income are current), so the table was frozen
        # at 2011. Besides the fixed preference list, also consider any us-gaap
        # per-share / per-unit EPS-style concept the filer actually uses. A
        # discovered concept only wins if it has strictly NEWER quarterly data than
        # every listed tag (best_quarterly keeps first-listed on ties).
        _eps_fixed = ['EarningsPerShareDiluted', 'EarningsPerShareBasic',
                      'IncomeLossFromContinuingOperationsPerDilutedShare',
                      'IncomeLossFromContinuingOperationsPerBasicShare',
                      'NetIncomeLossPerOutstandingLimitedPartnershipUnit',
                      'IncomeLossFromContinuingOperationsPerOutstandingLimitedPartnershipUnit']
        _eps_name_ok = re.compile(
            r'(EarningsPerShare|IncomeLoss\w*Per(Basic|Diluted|Outstanding|Share|Unit)|'
            r'NetIncomeLoss\w*Per(Share|Unit|Outstanding|Basic|Diluted))')
        _eps_name_bad = re.compile(
            r'(Discontinued|Extraordinary|CumulativeEffect|Dividend|Distribution|Declared|'
            r'ProForma|Antidilutive|Redeemable|Preferred|Parent)')
        _eps_discovered = sorted(
            (n for n in facts
             if n not in _eps_fixed and _eps_name_ok.search(n) and not _eps_name_bad.search(n)),
            key=lambda n: (('Diluted' not in n), n))
        _eps_concept, eps_q = best_quarterly_named(_eps_fixed + _eps_discovered, 'USD/shares')

        # Step 6: Quarterly Revenue — newest-data tag wins. Utility tags added:
        # utilities (XEL etc.) often report under RegulatedAndUnregulatedOperatingRevenue.
        rev_q = _merge_max([get_quarterly(c, 'USD') for c in REV_TAGS])

        # Bank/thrift revenue override (Sept 2026 fix — confirmed on HBAN,
        # MTB, CFG). ASC 606 "contract with customer" revenue explicitly
        # EXCLUDES interest income (a different accounting standard governs
        # it), so for an interest-income-driven filer, every tag tried
        # above can only ever capture noninterest fee income — a real but
        # structurally partial slice, not total revenue (confirmed on HBAN:
        # RevenueFromContractWithCustomerExcludingAssessedTax = $1.56B fee
        # income vs. real total revenue of ~$8B+). The standard analyst
        # definition of bank revenue is net interest income + noninterest
        # income (confirmed against CFG, where NII + NoninterestIncome =
        # $8.24B matches CFG's own current Revenues tag of $8.25B almost
        # exactly). Detected generically, no ticker/sector hardcoding:
        # NoninterestIncome is a bank/thrift-specific tag that an ordinary
        # commercial filer never populates, so requiring BOTH tags present
        # can't misfire on a non-bank. Overrides per-period only where both
        # exist; periods without bank-style data keep whatever the ASC-606
        # chain above found.
        def _bank_revenue(quarterly: bool) -> list[dict]:
            fn = get_quarterly if quarterly else get_annual
            nii = fn('InterestIncomeExpenseNet', 'USD')
            noninterest = fn('NoninterestIncome', 'USD')
            if not (nii and noninterest):
                return []
            nii_by = {x['end']: x['val'] for x in nii}
            ni_by = {x['end']: x['val'] for x in noninterest}
            return [{'end': end, 'val': nii_by[end] + ni_by[end]}
                    for end in sorted(set(nii_by) & set(ni_by), reverse=True)]

        bank_rev_q = _bank_revenue(quarterly=True)
        if bank_rev_q:
            bank_ends = {r['end'] for r in bank_rev_q}
            rev_q = bank_rev_q + [r for r in rev_q if r['end'] not in bank_ends]

        # Step 7: Quarterly Net Income
        ni_q = get_quarterly('NetIncomeLoss', 'USD')

        # Build quarter lookup dicts
        rev_by_date = {r['end']: r['val'] for r in rev_q}
        ni_by_date = {n['end']: n['val'] for n in ni_q}

        # Step 7.5: Derive missing Q4 entries (Sept 2026 fix — this is what
        # Jansky flagged on IBM as a "fiscal-vs-calendar quarter
        # re-sequencing bug"). Root cause: 10-Q filings only ever cover
        # fiscal Q1-Q3 — Q4 is never filed as its own 10-Q, it's folded
        # into the 10-K's annual figures — so this table previously had NO
        # Q4 row at all for any year. The displayed quarters were actually
        # in correct order, but the visual effect (e.g. 2026-03-31 jumping
        # straight to 2025-09-30, skipping 2025-12-31) looks exactly like a
        # re-sequencing bug even though nothing was technically
        # out-of-order. Fixed by deriving each year's Q4 as
        # annual (10-K) minus the sum of its filed Q1+Q2+Q3 — the standard
        # technique analysts use for this — for Revenue, Net Income, and
        # diluted EPS, only when all three quarters and an annual figure
        # are actually present. EPS derivation is an approximation (share
        # count can shift quarter to quarter), so derived rows are marked
        # "(derived)" rather than presented as directly reported.
        rev_annual_for_q4 = _merge_max([get_annual(c, 'USD') for c in REV_TAGS])
        bank_rev_annual = _bank_revenue(quarterly=False)
        if bank_rev_annual:
            bank_ends_a = {r['end'] for r in bank_rev_annual}
            rev_annual_for_q4 = bank_rev_annual + [
                r for r in rev_annual_for_q4 if r['end'] not in bank_ends_a]
        ni_annual_for_q4  = get_annual('NetIncomeLoss', 'USD')
        # Annual EPS: prefer the SAME tag the quarterly series came from (so a derived
        # Q4 = FY - Q1..Q3 never mixes basic and diluted), else whichever EPS tag has
        # the newest 10-K data. EPD's annual table used to print 2011 and its Q4 rows
        # were never derived because the fixed tag had no recent annual facts.
        def _eps_annual_series():
            a = get_annual(_eps_concept, 'USD/shares') if _eps_concept else []
            return a or best_annual(_eps_fixed + _eps_discovered, 'USD/shares')
        eps_annual_for_q4 = _eps_annual_series()

        # Oct 26 2026: derive Q4 for ANY fiscal-year end, not just Dec 31. The old
        # code only matched Mar/Jun/Sep quarters of a calendar year, so ADSK (Jan),
        # WMT (Jan), MU (Aug), FDX (May)... never got a Q4 row. For each 10-K period
        # end E, the three quarter ends 45-300 days before E are that year's Q1-Q3.
        def _fy_q4(quarterly_items, annual_items):
            out = {}
            for a_ in annual_items:
                try:
                    e_dt = datetime.strptime(a_['end'], '%Y-%m-%d')
                except Exception:
                    continue
                qs = {}
                for it in quarterly_items:
                    try:
                        gap = (e_dt - datetime.strptime(it['end'], '%Y-%m-%d')).days
                    except Exception:
                        continue
                    if 45 <= gap <= 300:
                        qs[it['end']] = it['val']
                if len(qs) == 3:
                    out[a_['end']] = (a_['val'] - sum(qs.values()), list(qs.values()), a_['val'])
            return out

        d_rev_all = _fy_q4(rev_q, rev_annual_for_q4)
        d_ni_all  = _fy_q4(ni_q, ni_annual_for_q4)
        d_eps_all = _fy_q4(eps_q, eps_annual_for_q4)
        derived_q4 = []
        q4_suspect = set()
        for q4_end in sorted(set(d_rev_all) | set(d_ni_all) | set(d_eps_all)):
            if q4_end in rev_by_date or any(q['end'] == q4_end for q in eps_q):
                continue  # already have a real, filed Q4 entry — don't overwrite it
            if q4_end in d_rev_all:
                rev_by_date[q4_end] = d_rev_all[q4_end][0]
                _v, _q3, _fy = d_rev_all[q4_end]
                if _q3 and _v > 1.45 * (sum(_q3) / 3):
                    q4_suspect.add(q4_end)       # Q4 >> avg of Q1-Q3: scope change
            if q4_end in d_ni_all:
                ni_by_date[q4_end] = d_ni_all[q4_end][0]
            if q4_end in d_eps_all:
                _v, _q3, _fy = d_eps_all[q4_end]
                derived_q4.append({'end': q4_end, 'val': _v, 'derived': True})
                if _v < 0 < _fy and all(x > 0 for x in _q3):
                    q4_suspect.add(q4_end)       # annual positive, Q1-Q3 positive, Q4 negative

        eps_q = sorted(eps_q + derived_q4, key=lambda x: x['end'], reverse=True)

        # Step 8: Combined quarterly table
        lines.append(f"Quarterly Results (last {quarters} quarters from SEC filings):")
        lines.append(
            f"  {'Quarter End':<13} {'Revenue':>10} "
            f"{'Net Income':>11} {'Diluted EPS':>12}")
        lines.append("  " + "-" * 50)

        displayed = 0
        _rows = []          # (end, rev, ni) for the scope-change checks below
        for q in eps_q[:quarters]:
            end = q['end']
            eps_val = q['val']
            rev_val = rev_by_date.get(end)
            ni_val = ni_by_date.get(end)

            rev_str = f"${rev_val/1e9:.2f}B" \
                if rev_val and not math.isnan(float(rev_val)) else "N/A"
            ni_str = f"${ni_val/1e9:.2f}B" \
                if ni_val and not math.isnan(float(ni_val)) else "N/A"
            eps_str = f"${eps_val:.2f}" if eps_val is not None else "N/A"
            tag = "  (derived Q4 = FY - Q1-Q3)" if q.get('derived') else ""
            if q.get('derived') and end in q4_suspect:
                tag += "  ⚠ unreliable — mixed scope/restated quarters"
            _rows.append((end, rev_val, ni_val, bool(q.get('derived'))))

            lines.append(
                f"  {end:<13} {rev_str:>10} {ni_str:>11} {eps_str:>12}{tag}")
            displayed += 1

        # Oct 26 2026 (HON): after a spin-off the series mixes perimeters — Q2 revenue
        # $9.72B (with Aerospace) next to Q3 $4.99B (without) — and Yahoo-style
        # growth rates computed across it are meaningless. Say so right under the table.
        try:
            _rv = [(e, r) for e, r, _n, _d in _rows if r and r > 0]
            for (e1, r1), (e2, r2) in zip(_rv, _rv[1:]):          # newest first
                _ratio = r1 / r2
                if _ratio < 0.6 or _ratio > 1.7:
                    lines.append(
                        f"  ⚠ SCOPE CHANGE? revenue moved from ${r2/1e9:.2f}B ({e2}) to "
                        f"${r1/1e9:.2f}B ({e1}), {(_ratio-1)*100:+.0f}% in one quarter — typical of a "
                        f"spin-off, divestiture, big acquisition or discontinued-operations "
                        f"restatement. Quarters in this table are NOT like-for-like: do not compute "
                        f"growth or margins across it; use the company's own continuing-operations "
                        f"figures (Nova) instead.")
                    break
            _mg = sorted(n / r for e, r, n, d in _rows if r and n and r > 0 and not d)
            if len(_mg) >= 4:
                _med = _mg[len(_mg) // 2]
                for e, r, n, d in _rows:
                    if r and n and r > 0 and not d and n / r > 0.45 and n / r > 2 * max(_med, 0.02):
                        lines.append(
                            f"  ⚠ {e}: net income is {n/r*100:.0f}% of revenue vs a typical "
                            f"{_med*100:.0f}% — almost certainly a one-time gain (e.g. sale or spin-off "
                            f"recorded in discontinued operations). Do not treat that quarter's EPS "
                            f"as earnings power.")
                        break
        except Exception:
            pass

        if displayed == 0:
            lines.append("  No quarterly EPS data found in SEC filings.")
            _fn = _foreign_note()
            if _fn:
                lines.append("  " + _fn)
        elif eps_q:
            _newest_q = max(q['end'] for q in eps_q)
            try:
                _qage = (datetime.now() - datetime.strptime(_newest_q, '%Y-%m-%d')).days
            except Exception:
                _qage = 0
            if _qage > 100:
                # Oct 24 2026: tell "SEC's XBRL API hasn't published this filer's
                # newest 10-Q(s)" apart from "tag not covered". NEE and C both have
                # 10-Qs filed in 2026 that are simply absent from companyfacts, so no
                # tag change can recover them; the filings index shows what exists.
                # Nov 3 2026: check from 100 days (REYN's Q2 was missing at 124 days),
                # and read the missing quarters from the filings' own R pages.
                _lag_txt = ""
                _missing = []
                try:
                    _subs = sec_get(f'https://data.sec.gov/submissions/CIK{cik}.json')
                    _rec = _subs.get('filings', {}).get('recent', {})
                    _filed_ends = [q['end'] for q in eps_q if not q.get('derived')]
                    for _i, _f in enumerate(_rec.get('form', [])):
                        if _f == '10-Q' and _filed_ends and _rec['reportDate'][_i] > max(_filed_ends):
                            _accs = _rec.get('accessionNumber') or []
                            _missing.append((_rec['reportDate'][_i], _rec['filingDate'][_i],
                                             _accs[_i] if _i < len(_accs) else ''))
                    if _missing:
                        _lp, _lf, _ = _missing[0]
                        _lag_txt = (f"SEC's XBRL API has not published this filer's latest 10-Q "
                                    f"(period {_lp}, filed {_lf}); the table above lags the "
                                    f"company's real latest quarter")
                except Exception:
                    pass
                if _qage > 200:
                    lines.append(
                        f"  ⚠ STALE: newest quarterly EPS on file is {_newest_q} ({_qage} days old) — "
                        + (_lag_txt if _lag_txt else
                           "this filer likely reports under XBRL tags not covered here")
                        + "; do NOT treat this table as current.")
                elif _lag_txt:
                    lines.append(f"  ⚠ {_lag_txt}.")
                if _missing:
                    _got = []
                    for _lp, _lf, _ac in sorted(_missing, reverse=True)[:3]:
                        try:
                            _q = _filing_quarter(cik.lstrip('0'), _ac) if _ac else None
                        except Exception:
                            _q = None
                        if _q:
                            _got.append((_q, _lf))
                    if _got:
                        lines.append("")
                        lines.append("  Quarters read from the filings themselves (SEC's API has not "
                                     "published them; income-statement R page, 3 months ended):")
                        lines.append(f"  {'Quarter End':<13} {'Revenue':>10} {'Net Income':>11} {'Diluted EPS':>12}")
                        lines.append("  " + "-" * 50)
                        for _q, _lf in _got:
                            _rs = f"${_q['rev']/1e9:.2f}B" if _q['rev'] is not None else "N/A"
                            _ns = f"${_q['ni']/1e9:.2f}B" if _q['ni'] is not None else "N/A"
                            _es = f"${_q['eps']:.2f}" if _q['eps'] is not None else "N/A"
                            lines.append(f"  {_q['end']:<13} {_rs:>10} {_ns:>11} {_es:>12}"
                                         f"  (10-Q filed {_lf}, {_q['page']})")
                        lines.append("  These figures are GAAP as reported in the filing; the table above "
                                     "does not include them.")
                    elif _qage > 200:
                        lines.append("  (the missing filings' income statements could not be read either; "
                                     "use Nova/Yahoo for the latest quarter)")

        # Step 9: Annual revenue trend — merged across all known revenue
        # tags (see get_annual() fix note above), not a sequential fallback
        rev_annual = _merge_max([get_annual(c, 'USD') for c in REV_TAGS])
        # Bank/thrift override — see the _bank_revenue() note above.
        if bank_rev_annual:
            bank_ends_disp = {r['end'] for r in bank_rev_annual}
            rev_annual = sorted(
                bank_rev_annual + [r for r in rev_annual if r['end'] not in bank_ends_disp],
                key=lambda x: x['end'], reverse=True)
            lines_note_bank = True
        else:
            lines_note_bank = False

        if rev_annual:
            lines.append(f"\nAnnual Revenue (from 10-K filings):")
            if lines_note_bank:
                lines.append(
                    "  (bank/thrift filer — revenue = net interest income + "
                    "noninterest income, not ASC 606 contract revenue)")
            newest_year = int(rev_annual[0]['end'][:4])
            if datetime.now().year - newest_year > 2:
                lines.append(
                    f"  ⚠ Most recent annual revenue on file is FY{newest_year} "
                    f"({datetime.now().year - newest_year}yr+ old) — company may "
                    f"report under a revenue XBRL tag not covered here; treat with caution."
                )
            for r in rev_annual[:5]:
                year = r['end'][:4]
                val = r['val']
                lines.append(f"  {year}: ${val/1e9:.2f}B")

        # Step 10: Annual EPS trend
        eps_annual = eps_annual_for_q4
        if eps_annual:
            lines.append(f"\nAnnual Diluted EPS (from 10-K filings):")
            if datetime.now().year - int(eps_annual[0]['end'][:4]) > 2:
                lines.append(
                    f"  ⚠ Most recent annual EPS on file is FY{eps_annual[0]['end'][:4]} — "
                    f"this filer likely reports EPS under a tag not covered here; treat with caution.")
            for e in eps_annual[:5]:
                year = e['end'][:4]
                lines.append(f"  {year}: ${e['val']:.2f}")

        lines.append(f"\nSource: SEC EDGAR XBRL (CIK: {cik})")
        lines.append(
            f"Data as of most recent 10-Q/10-K filing. "
            f"No estimates — actual reported figures only.")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching SEC data for {ticker}: {str(e)}"

@mcp.tool()
def compare_stocks(tickers: str, period: str = "30d") -> str:
    """Compare performance of multiple stocks over a time period.
    tickers: comma-separated symbols e.g. 'AVGO,NVDA,TSM'
    period: use yfinance periods: 1mo, 3mo, 6mo, 1y, 2y, 5y, ytd, max
    NOTE: do NOT use '30d', '90d' — use '1mo', '3mo', '6mo', '1y' instead
    """
    
    try:
        import yfinance as yf
        symbols = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        if not symbols:
            return "No valid tickers provided."

        results = []
        for symbol in symbols:
            try:
                hist = yf.Ticker(symbol).history(period=period)[["Close"]]
                if hist.empty:
                    results.append(f"{symbol}: No data found.")
                    continue
                start = hist["Close"].iloc[0]
                end = hist["Close"].iloc[-1]
                high = hist["Close"].max()
                low = hist["Close"].min()
                change = end - start
                pct = (change / start) * 100
                results.append(
                    f"{symbol:6s}  Start: ${start:>8.2f}  "
                    f"End: ${end:>8.2f}  "
                    f"Change: {pct:>+7.2f}%  "
                    f"High: ${high:>8.2f}  Low: ${low:>8.2f}"
                )
            except Exception as e:
                results.append(f"{symbol}: Error — {str(e)}")

        header = f"=== Stock Comparison ({period}) ===\n"
        return header + "\n".join(results)
    except Exception as e:
        return f"Error comparing stocks: {str(e)}"

_NEWS_NAME_NOISE = re.compile(
    r"\b(inc|corp|corporation|co|company|ltd|plc|holdings|group|the|llc|sa|nv|ag)\b\.?,?",
    re.IGNORECASE)


_NEWS_GENERIC_WORDS = {
    "gaming", "technology", "technologies", "financial", "energy", "systems",
    "international", "resources", "services", "brands", "pharmaceuticals",
    "healthcare", "global", "industries", "partners", "capital", "bancorp",
    "bank", "american", "united", "national", "first", "general",
}


def _news_name_words(name: str) -> list:
    """Up to two distinguishing words from a company name ('Boyd Gaming
    Corporation' -> ['Boyd']; 'Walt Disney Company' -> ['Walt', 'Disney'])."""
    words = [w for w in re.sub(r"[^\w\s&-]", " ", _NEWS_NAME_NOISE.sub(" ", name or "")).split()
             if len(w) >= 3 and w.lower() not in _NEWS_GENERIC_WORDS]
    return words[:2]


def _news_score(text: str, ticker: str, name: str) -> int:
    """How surely an article concerns THIS company: 2 = strong, 1 = weak,
    0 = unrelated. Strong needs a distinguishing word from the company name
    or an exchange-qualified ticker ("NYSE: BYD"). A bare ticker match is
    only weak — short or common tickers (BYD, B, V, C) collide with unrelated
    things constantly, and BYD returned the Chinese automaker."""
    text = text or ""
    for w in _news_name_words(name):
        if re.search(rf"\b{re.escape(w)}", text, re.IGNORECASE):
            return 2
    if re.search(rf"\b(?:NYSE|NASDAQ|NYSEARCA|NYSE American)\s*:\s*{re.escape(ticker)}\b", text):
        return 2
    if len(ticker) >= 3 and re.search(rf"\b{re.escape(ticker)}\b", text):
        # With no company name known there is nothing better to check against.
        return 2 if not _news_name_words(name) else 1
    return 0


def _news_searxng(query: str, limit: int) -> list:
    """News via the local SearXNG instance (already carries the paid Brave
    engine). Tries the news category first, then general results."""
    base = (SEARXNG_URL or "http://localhost:8080").rstrip("/")
    results = []
    for params in ({"q": query, "format": "json", "categories": "news", "time_range": "month"},
                   {"q": query, "format": "json"}):
        r = httpx.get(f"{base}/search", params=params, timeout=20,
                      headers={"User-Agent": "webmcp/1.0"})
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            break
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "summary": x.get("content", "")} for x in results]


@mcp.tool()
def get_stock_news(ticker: str, max_results: int = 5) -> str:
    """Fetch recent news articles for a stock ticker.
    Tries Yahoo Finance first, then SearXNG, then DuckDuckGo. Articles are
    filtered to the actual company (not same-ticker lookalikes), and if every
    source fails the result says so explicitly instead of looking like "no news".
    ticker: stock symbol e.g. AVGO, NVDA, TSLA
    max_results: number of news items to return (default 5)
    """
    sym = ticker.upper()
    errors = []
    try:
        import yfinance as yf

        t = yf.Ticker(sym)
        try:
            name = (t.info or {}).get("shortName") or (t.info or {}).get("longName") or ""
        except Exception:
            name = ""

        # ── 1. Yahoo Finance ────────────────────────────────────────────
        items = []
        try:
            for item in (t.news or []):
                if not item:
                    continue
                content = item.get('content', item)
                if not content or not isinstance(content, dict):
                    continue
                canonical = content.get('canonicalUrl') or {}
                clickthrough = content.get('clickThroughUrl') or {}
                items.append({
                    "title": content.get('title', 'No title'),
                    "publisher": (content.get('provider') or {}).get('displayName', 'Unknown'),
                    "date": content.get('pubDate', 'Unknown date'),
                    "url": canonical.get('url', '') or clickthrough.get('url', ''),
                    "summary": re.sub(r'<[^>]+>', '',
                                      content.get('summary', content.get('description', '')) or '')[:200],
                })
        except Exception as e:
            errors.append(f"Yahoo error: {str(e)[:80]}")

        # Articles that only match on the bare ticker are kept aside as a
        # low-confidence last resort, never preferred over a name match.
        weak_pool = []   # (source label, formatted block)

        def _yahoo_block(n, i):
            return (f"{n}. [{i['publisher']}] {i['title']}\n"
                    f"   Published: {i['date']}\n   URL: {i['url']}\n"
                    f"   Summary: {i['summary']}\n")

        scored = [(_news_score(f"{i['title']} {i['summary']}", sym, name), i) for i in items]
        strong = [i for s, i in scored if s == 2]
        weak = [i for s, i in scored if s == 1]
        if strong:
            lines = [f"=== {sym} News (Yahoo Finance) ===\n"]
            lines += [_yahoo_block(n, i) for n, i in enumerate(strong[:max_results], 1)]
            return "\n".join(lines)
        weak_pool += [("Yahoo Finance", _yahoo_block(0, i)) for i in weak]
        if items:
            errors.append(f"Yahoo returned {len(items)} items, none clearly about {name or sym}")
        elif not any(e.startswith("Yahoo") for e in errors):
            errors.append("Yahoo returned no items")

        # ── 2. SearXNG ──────────────────────────────────────────────────
        query = f'"{name}" {sym} stock news' if name else f"{sym} stock news"
        def _web_block(n, title, url, summary):
            return f"{n}. {title}\n   URL: {url}\n   Summary: {summary[:200]}\n"

        try:
            res = _news_searxng(query, max_results * 3)
            sc = [(_news_score(f"{x['title']} {x['summary']}", sym, name), x) for x in res]
            found = [x for s, x in sc if s == 2][:max_results]
            if found:
                lines = [f"=== {sym} News (SearXNG web search — Yahoo unavailable) ===\n"]
                lines += [_web_block(n, x['title'], x['url'], x['summary'])
                          for n, x in enumerate(found, 1)]
                return "\n".join(lines)
            weak_pool += [("SearXNG", _web_block(0, x['title'], x['url'], x['summary']))
                          for s, x in sc if s == 1]
            errors.append("SearXNG returned nothing clearly about the company")
        except Exception as e:
            errors.append(f"SearXNG error: {str(e)[:80]}")

        # ── 3. DuckDuckGo (last resort) ─────────────────────────────────
        try:
            from ddgs import DDGS
            ddg_query = ((f'"{name}" ' if name else f"{sym} ")
                         + "stock news site:cnbc.com OR site:finance.yahoo.com "
                           "OR site:marketwatch.com OR site:reuters.com OR site:bloomberg.com")
            with DDGS() as ddgs:
                raw = list(ddgs.text(ddg_query, max_results=max_results * 2))
            sc = [(_news_score(f"{r.get('title', '')} {r.get('body', '')}", sym, name), r) for r in raw]
            found = [r for s, r in sc if s == 2][:max_results]
            if found:
                lines = [f"=== {sym} News (DuckDuckGo fallback — Yahoo and SearXNG unavailable) ===\n"]
                lines += [_web_block(n, r.get('title', ''), r.get('href', ''), r.get('body', ''))
                          for n, r in enumerate(found, 1)]
                return "\n".join(lines)
            weak_pool += [("DuckDuckGo", _web_block(0, r.get('title', ''), r.get('href', ''), r.get('body', '')))
                          for s, r in sc if s == 1]
            errors.append("DuckDuckGo returned nothing clearly about the company")
        except Exception as e:
            errors.append(f"DuckDuckGo error: {str(e)[:80]}")

        # ── Last resort: ticker-only matches, clearly labeled ───────────
        if weak_pool:
            lines = [f"=== {sym} News (LOW CONFIDENCE — matched on the ticker symbol only; "
                     f"may be about a different company) ===\n"]
            for n, (src, block) in enumerate(weak_pool[:max_results], 1):
                lines.append(f"[{src}] " + block.replace("0. ", f"{n}. ", 1))
            return "\n".join(lines)

        return (f"=== {sym} News UNAVAILABLE ===\n"
                f"All news sources failed this run: {'; '.join(errors)}.\n"
                f"Treat news/catalyst coverage for {sym} as MISSING this week — "
                f"this is a feed outage, not evidence that there is no news.")

    except Exception as e:
        return (f"=== {sym} News UNAVAILABLE ===\nError fetching news: {str(e)}. "
                f"Treat news coverage as MISSING this week.")

@mcp.tool()
def get_short_interest(ticker: str) -> str:
    """Fetch current short interest data for a stock.
    Shows short ratio, short % of float, shares short vs prior month.
    ticker: stock symbol e.g. AAPL, NVDA, TSLA
    """
    try:
        import yfinance as yf
        import math

        t = yf.Ticker(ticker.upper())
        info = t.info

        def fmt(val, suffix='', divisor=1, prefix=''):
            if val is None or (isinstance(val, float) and math.isnan(val)):
                return 'N/A'
            return f"{prefix}{val/divisor:,.0f}{suffix}"

        shares_short = info.get('sharesShort')
        prior_month = info.get('sharesShortPriorMonth')
        float_shares = info.get('floatShares')
        outstanding = info.get('sharesOutstanding')
        short_ratio = info.get('shortRatio')
        short_pct = info.get('shortPercentOfFloat')

        # Oct 2026: plausibility check. Yahoo periodically returns garbage
        # here (BAC showed 766 shares short; WFC/CFG/ALL were flagged too),
        # and it was reaching the analyst as a real figure — each report then
        # spent a caveat quarantining it by hand. Reject physically
        # implausible values up front and say so explicitly.
        bad_reason = None
        try:
            if shares_short is not None and float_shares:
                ratio = shares_short / float_shares
                if ratio < 1e-5:
                    bad_reason = (f"shares short ({shares_short:,.0f}) is only "
                                  f"{ratio*100:.5f}% of the float ({float_shares:,.0f})")
                elif ratio > 1.0:
                    bad_reason = (f"shares short ({shares_short:,.0f}) exceeds the "
                                  f"float ({float_shares:,.0f})")
            if not bad_reason and short_pct is not None and short_pct > 1.0:
                bad_reason = f"short % of float is {short_pct*100:,.0f}% (over 100%)"
        except Exception:
            pass

        # Calculate month-over-month change
        mom_change = ""
        if not bad_reason and shares_short and prior_month and prior_month > 0:
            chg = ((shares_short - prior_month) / prior_month) * 100
            # Nov 3 2026: Jansky's 10/31 review found USB/CFG/ALL/DLR/BA reporting
            # month-over-month collapses of -99% or worse (ALL's slipped through
            # unflagged). The level passed the float check, so the PRIOR-month
            # field was the garbage one. A real short book does not shrink 90%+
            # (or grow 10x) in a month, so quarantine the whole record.
            prior_ratio = (prior_month / float_shares) if float_shares else None
            if chg <= -90 or chg >= 900:
                bad_reason = (f"month-over-month change of {chg:+,.0f}% "
                              f"({prior_month:,.0f} -> {shares_short:,.0f} shares) is not "
                              f"physically plausible for a short position")
            elif prior_ratio is not None and (prior_ratio < 1e-5 or prior_ratio > 1.0):
                bad_reason = (f"prior-month short figure ({prior_month:,.0f}) is "
                              f"{prior_ratio*100:.5f}% of the float")
        if not bad_reason and shares_short and prior_month and prior_month > 0:
            arrow = "↑" if chg > 0 else "↓"
            mom_change = f"{arrow} {abs(chg):.1f}% vs prior month"

        lines = [f"=== {ticker.upper()} Short Interest ===\n"]
        if bad_reason:
            lines.append(
                f"⚠ DATA QUALITY: Yahoo's short-interest figures failed a "
                f"plausibility check ({bad_reason}). Treat short interest as "
                f"UNAVAILABLE this week — do not cite or interpret these numbers.\n")
            lines.append("Shares Short:          N/A (implausible source value)")
            lines.append("Prior Month Short:     N/A")
            lines.append("Short % of Float:      N/A")
            lines.append("Short Ratio (days):    N/A")
        else:
            lines.append(
                f"Shares Short:          "
                f"{fmt(shares_short, ' shares')} {mom_change}")
            lines.append(
                f"Prior Month Short:     {fmt(prior_month, ' shares')}")
            lines.append(
                f"Short % of Float:      "
                f"{f'{short_pct*100:.2f}%' if short_pct else 'N/A'}")
            lines.append(
                f"Short Ratio (days):    "
                f"{f'{short_ratio:.2f}' if short_ratio else 'N/A'}")
        lines.append(
            f"Float Shares:          {fmt(float_shares, ' shares')}")
        lines.append(
            f"Shares Outstanding:    {fmt(outstanding, ' shares')}")

        # Interpretation
        lines.append("\nInterpretation:")
        if bad_reason:
            lines.append("  Not available — source data failed a plausibility check.")
            short_pct = None
            short_ratio = None
        if short_pct:
            pct = short_pct * 100
            if pct < 2:
                sentiment = "Very low short interest — minimal bearish pressure."
            elif pct < 5:
                sentiment = "Low-moderate short interest — modest bearish sentiment."
            elif pct < 10:
                sentiment = "Moderate short interest — notable bearish conviction."
            elif pct < 20:
                sentiment = "High short interest — significant bearish sentiment. "\
                           "Squeeze potential if news turns positive."
            else:
                sentiment = "Very high short interest — heavy bearish conviction. "\
                           "Major short squeeze risk."
            lines.append(f"  {sentiment}")

        if short_ratio:
            if short_ratio > 10:
                lines.append(
                    f"  Days-to-cover of {short_ratio:.1f} is elevated — "
                    f"shorts could take weeks to unwind.")
            elif short_ratio > 5:
                lines.append(
                    f"  Days-to-cover of {short_ratio:.1f} is moderate.")
            else:
                lines.append(
                    f"  Days-to-cover of {short_ratio:.1f} is low — "
                    f"shorts can exit quickly.")

        lines.append("\nSource: Yahoo Finance (FINRA-reported, updated twice monthly)")
        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching short interest for {ticker}: {str(e)}"


@mcp.tool()
def get_insider_trades(ticker: str, days: int = 90) -> str:
    """Fetch recent insider trades from SEC EDGAR Form 4 filings.
    Shows who bought/sold, how many shares, at what price, and their role.
    Distinguishes open market trades from automatic RSU/option transactions.
    ticker: stock symbol e.g. AAPL, NVDA, TSLA
    days: how many days back to search (default 90)
    """
    try:
        import urllib.request
        import json
        import re
        import xml.etree.ElementTree as ET
        from datetime import datetime, timedelta

        headers = {'User-Agent': 'Jay jay@example.com'}

        def sec_get(url):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()

        # Step 1: Resolve ticker to CIK
        tickers_data = json.loads(sec_get(
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

        # Step 2: Search for Form 4 filings using CIK
        start_date = (datetime.now() -
                      timedelta(days=days)).strftime('%Y-%m-%d')
        end_date = datetime.now().strftime('%Y-%m-%d')

        search_url = (
            f'https://efts.sec.gov/LATEST/search-index?'
            f'q=%22{cik}%22'
            f'&dateRange=custom&startdt={start_date}&enddt={end_date}'
            f'&forms=4&hits.hits.total=true')

        data = json.loads(sec_get(search_url))
        hits = data.get('hits', {}).get('hits', [])

        if not hits:
            return (f"No Form 4 filings found for {ticker.upper()} "
                    f"in the last {days} days.")

        lines = [f"=== {company_name} ({ticker.upper()}) ==="]
        lines.append(f"Insider Trades — SEC Form 4 (last {days} days)\n")

        # Transaction code descriptions
        codes = {
            'P': 'Open Market Purchase 🟢',
            'S': 'Open Market Sale 🔴',
            'M': 'Option/RSU Exercise',
            'A': 'Grant/Award',
            'F': 'Tax Withholding Sale (auto)',
            'G': 'Gift',
            'C': 'Conversion',
            'D': 'Disposition to Company ⚠️',
            'I': 'Discretionary Transaction',
            'J': 'Other Acquisition',
        }

        significant = []
        routine = []

        # Step 3: Fetch and parse each Form 4 XML
        for hit in hits[:15]:
            src = hit.get('_source', {})
            adsh = src.get('adsh', '')
            file_date = src.get('file_date', 'N/A')
            display_names = src.get('display_names', [])

            if not adsh:
                continue

            # Find insider name and CIK using regex
            insider_name = 'Unknown'
            insider_cik_num = None

            for name in display_names:
                if (company_name[:5].lower() in name.lower() or
                        ticker.upper() in name):
                    continue
                cik_match = re.search(r'CIK\s+(\d+)', name)
                if cik_match:
                    insider_cik_num = cik_match.group(1).lstrip('0')
                    insider_name = name.split('(')[0].strip()
                    break

            if not insider_cik_num:
                continue

            # Fetch Form 4 XML — look up index first as filename varies
            adsh_fmt = adsh.replace('-', '')
            xml_data = None

            for try_cik in [insider_cik_num, cik.lstrip('0')]:
                try:
                    index_url = (
                        f'https://www.sec.gov/Archives/edgar/data/'
                        f'{try_cik}/{adsh_fmt}/index.json')
                    index_data = json.loads(sec_get(index_url))
                    items = (index_data.get('directory', {})
                                       .get('item', []))
                    xml_filename = None
                    for item in items:
                        name = item.get('name', '')
                        if (name.endswith('.xml') and
                                'index' not in name.lower()):
                            xml_filename = name
                            break
                    if xml_filename:
                        xml_url = (
                            f'https://www.sec.gov/Archives/edgar/data/'
                            f'{try_cik}/{adsh_fmt}/{xml_filename}')
                        xml_data = sec_get(xml_url)
                        break
                except Exception:
                    continue

            if not xml_data:
                continue

            try:
                root = ET.fromstring(xml_data)

                # Get role and title
                owner = root.find('.//reportingOwner')
                title = ''
                role = 'Other'
                if owner is not None:
                    title = owner.findtext('.//officerTitle', '')
                    is_dir = owner.findtext('.//isDirector', '0')
                    is_off = owner.findtext('.//isOfficer', '0')
                    is_10pct = owner.findtext('.//isTenPercentOwner', '0')
                    if is_10pct == '1':
                        role = '10%+ Owner'
                    if is_dir == '1':
                        role = 'Director'
                    if is_off == '1':
                        role = 'Officer'

                role_str = f"{role}{' — ' + title if title else ''}"

                # Parse non-derivative transactions (actual stock)
                for txn in root.findall('.//nonDerivativeTransaction'):
                    date = txn.findtext(
                        './/transactionDate/value', 'N/A')
                    code = txn.findtext(
                        './/transactionCode', 'N/A')
                    shares = txn.findtext(
                        './/transactionShares/value', '0')
                    price = txn.findtext(
                        './/transactionPricePerShare/value', '0')
                    direction = txn.findtext(
                        './/transactionAcquiredDisposedCode/value', 'N/A')
                    owned_after = txn.findtext(
                        './/sharesOwnedFollowingTransaction/value', 'N/A')

                    try:
                        total = float(shares or 0) * float(price or 0)
                        total_str = (f"${total:,.0f}"
                                     if total > 0 else "N/A")
                    except Exception:
                        total_str = "N/A"

                    action = 'BUY' if direction == 'A' else 'SELL'
                    code_desc = codes.get(code, code)

                    try:
                        shares_fmt = f"{float(shares):,.0f}"
                        price_fmt = (f"${float(price):.2f}"
                                     if float(price or 0) > 0 else "N/A")
                    except Exception:
                        shares_fmt = shares
                        price_fmt = price

                    try:
                        owned_fmt = f"{float(owned_after):,.0f} shares"
                    except Exception:
                        owned_fmt = "N/A"

                    entry_str = (
                        f"  {file_date} | {insider_name} ({role_str})\n"
                        f"    {action} {shares_fmt} shares "
                        f"@ {price_fmt} = {total_str} | {code_desc}\n"
                        f"    Owned after: {owned_fmt}")

                    if code in ('P', 'S', 'D'):
                        significant.append(entry_str)
                    else:
                        routine.append(entry_str)

                # Parse derivative transactions (options/RSUs)
                for txn in root.findall('.//derivativeTransaction'):
                    date = txn.findtext(
                        './/transactionDate/value', 'N/A')
                    code = txn.findtext(
                        './/transactionCode', 'N/A')
                    shares = txn.findtext(
                        './/transactionShares/value', '0')
                    security = txn.findtext(
                        './/securityTitle/value', 'N/A')
                    code_desc = codes.get(code, code)

                    try:
                        shares_fmt = f"{float(shares):,.0f}"
                    except Exception:
                        shares_fmt = shares

                    entry_str = (
                        f"  {file_date} | {insider_name} ({role_str})\n"
                        f"    {security} | {shares_fmt} units "
                        f"| {code_desc}")

                    if code in ('P', 'S', 'D'):
                        significant.append(entry_str)
                    else:
                        routine.append(entry_str)

            except Exception:
                continue

        # Output results
        if significant:
            lines.append(
                "🔵 OPEN MARKET TRADES (Discretionary — most significant):")
            lines.extend(significant[:10])
        else:
            lines.append(
                "🔵 OPEN MARKET TRADES: None found in this period.")

        lines.append("")
        if routine:
            lines.append(
                f"⚪ ROUTINE TRANSACTIONS (RSU/Option/Tax — "
                f"{len(routine)} total, showing first 10):")
            lines.extend(routine[:10])
        else:
            lines.append("⚪ ROUTINE TRANSACTIONS: None found.")

        lines.append(
            f"\nSource: SEC EDGAR Form 4 filings "
            f"({start_date} to {end_date})")
        lines.append(
            "Note: 'F' = automatic tax withholding sale (not bearish). "
            "'P'/'S' = discretionary open market trades (most significant). "
            "'D' = disposition to company (non-cash transfer).")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching insider trades for {ticker}: {str(e)}"
            
@mcp.tool()
def get_analyst_ratings(ticker: str, recent_days: int = 60) -> str:
    """Fetch current analyst consensus, price targets, and recent rating changes.
    Shows buy/hold/sell breakdown, mean price target, and recent upgrades/downgrades.
    ticker: stock symbol e.g. AAPL, NVDA, TSLA
    recent_days: how many days back to show rating changes (default 60)
    """
    try:
        import yfinance as yf
        import math
        from datetime import datetime, timedelta, timezone

        t = yf.Ticker(ticker.upper())
        info = t.info

        lines = [f"=== {ticker.upper()} Analyst Ratings ===\n"]

        # --- Price Target Consensus ---
        target_mean = info.get('targetMeanPrice')
        target_high = info.get('targetHighPrice')
        target_low = info.get('targetLowPrice')
        target_median = info.get('targetMedianPrice')
        current_price = info.get('currentPrice',
                                  info.get('regularMarketPrice'))
        rec_key = info.get('recommendationKey', 'N/A').upper()
        rec_mean = info.get('recommendationMean')
        num_analysts = info.get('numberOfAnalystOpinions')

        lines.append("Price Target Consensus:")
        lines.append(
            f"  Current Price:   "
            f"${current_price:.2f}" if current_price else
            f"  Current Price:   N/A")

        if target_mean and current_price:
            upside = ((target_mean - current_price) / current_price) * 100
            upside_str = f"({upside:+.1f}% upside)" if upside > 0 \
                else f"({upside:.1f}% downside)"
        else:
            upside_str = ""

        lines.append(
            f"  Mean Target:     "
            f"${target_mean:.2f} {upside_str}" if target_mean else
            "  Mean Target:     N/A")
        lines.append(
            f"  Median Target:   "
            f"${target_median:.2f}" if target_median else
            "  Median Target:   N/A")
        lines.append(
            f"  High Target:     "
            f"${target_high:.2f}" if target_high else
            "  High Target:     N/A")
        lines.append(
            f"  Low Target:      "
            f"${target_low:.2f}" if target_low else
            "  Low Target:      N/A")
        lines.append(
            f"  Analysts:        "
            f"{num_analysts}" if num_analysts else
            "  Analysts:        N/A")

        # --- Consensus Rating ---
        lines.append("\nConsensus Rating:")
        rating_map = {
            'strongbuy': 'STRONG BUY 🟢🟢',
            'buy': 'BUY 🟢',
            'hold': 'HOLD 🟡',
            'underperform': 'UNDERPERFORM 🔴',
            'sell': 'SELL 🔴🔴'
        }
        rec_display = rating_map.get(
            info.get('recommendationKey', '').lower(),
            rec_key)
        lines.append(f"  Consensus:       {rec_display}")

        # Recommendation mean: 1=Strong Buy, 3=Hold, 5=Strong Sell
        if rec_mean:
            lines.append(
                f"  Rating Score:    {rec_mean:.2f} "
                f"(1.0=Strong Buy → 5.0=Strong Sell)")

        # --- Buy/Hold/Sell Breakdown ---
        try:
            recs = t.recommendations_summary
            if recs is not None and not recs.empty:
                latest = recs.iloc[0]
                strong_buy = latest.get('strongBuy', 0)
                buy = latest.get('buy', 0)
                hold = latest.get('hold', 0)
                sell = latest.get('sell', 0)
                strong_sell = latest.get('strongSell', 0)
                total = strong_buy + buy + hold + sell + strong_sell

                lines.append("\nAnalyst Breakdown:")
                lines.append(
                    f"  Strong Buy:  {strong_buy:>3} "
                    f"({'█' * min(strong_buy, 20)})")
                lines.append(
                    f"  Buy:         {buy:>3} "
                    f"({'█' * min(buy, 20)})")
                lines.append(
                    f"  Hold:        {hold:>3} "
                    f"({'█' * min(hold, 20)})")
                lines.append(
                    f"  Sell:        {sell:>3} "
                    f"({'█' * min(sell, 20)})")
                lines.append(
                    f"  Strong Sell: {strong_sell:>3} "
                    f"({'█' * min(strong_sell, 20)})")
                lines.append(f"  Total:       {total:>3} analysts")
                # Sept 2026: this total (from recommendations_summary, the
                # buy/hold/sell panel) and num_analysts above (from
                # numberOfAnalystOpinions, the price-target panel) are two
                # separate Yahoo analyst panels that legitimately don't
                # have to agree — flagged by Jansky as a mismatch in 5+
                # tickers, but not itself a data bug. Making the
                # discrepancy explicit here instead of presenting two
                # unreconciled numbers with no explanation.
                if num_analysts and total and num_analysts != total:
                    lines.append(
                        f"  (Note: differs from the {num_analysts} analysts "
                        f"in the price-target panel above — Yahoo tracks "
                        f"rating and price-target coverage as separate "
                        f"analyst panels; this is expected, not a data error.)"
                    )
        except Exception:
            pass

        # --- Recent Upgrades/Downgrades ---
        try:
            upgrades = t.upgrades_downgrades
            if upgrades is not None and not upgrades.empty:
                cutoff = datetime.now(timezone.utc) - \
                         timedelta(days=recent_days)

                # Filter to recent period
                if upgrades.index.tz is None:
                    upgrades.index = upgrades.index.tz_localize('UTC')
                recent = upgrades[upgrades.index >= cutoff].copy()

                if not recent.empty:
                    lines.append(
                        f"\nRecent Rating Changes "
                        f"(last {recent_days} days):")

                    # Separate upgrades from downgrades
                    upgrade_list = []
                    downgrade_list = []
                    initiated_list = []

                    for date, row in recent.iterrows():
                        firm = row.get('Firm', 'Unknown')
                        to_grade = row.get('ToGrade', 'N/A')
                        from_grade = row.get('FromGrade', '')
                        action = row.get('Action', 'N/A')
                        date_str = date.strftime('%Y-%m-%d')

                        if from_grade:
                            change_str = (
                                f"  {date_str} | {firm:<30} "
                                f"{from_grade} → {to_grade}")
                        else:
                            change_str = (
                                f"  {date_str} | {firm:<30} "
                                f"→ {to_grade}")

                        action_lower = action.lower()
                        if 'up' in action_lower:
                            upgrade_list.append(
                                f"🟢 {change_str}")
                        elif 'down' in action_lower:
                            downgrade_list.append(
                                f"🔴 {change_str}")
                        else:
                            initiated_list.append(
                                f"🔵 {change_str}")

                    if upgrade_list:
                        lines.append("  Upgrades:")
                        lines.extend(upgrade_list[:8])
                    if downgrade_list:
                        lines.append("  Downgrades:")
                        lines.extend(downgrade_list[:8])
                    if initiated_list:
                        lines.append(
                            f"  Initiated/Reiterated "
                            f"({len(initiated_list)} total, "
                            f"showing 5):")
                        lines.extend(initiated_list[:5])

                    # Summary signal
                    n_up = len(upgrade_list)
                    n_down = len(downgrade_list)
                    lines.append(f"\n  Signal Summary:")
                    if n_up > n_down * 2:
                        lines.append(
                            f"  🟢 Strong upgrade momentum — "
                            f"{n_up} upgrades vs {n_down} downgrades")
                    elif n_up > n_down:
                        lines.append(
                            f"  🟡 Mild upgrade bias — "
                            f"{n_up} upgrades vs {n_down} downgrades")
                    elif n_down > n_up * 2:
                        lines.append(
                            f"  🔴 Strong downgrade pressure — "
                            f"{n_down} downgrades vs {n_up} upgrades")
                    elif n_down > n_up:
                        lines.append(
                            f"  🟡 Mild downgrade bias — "
                            f"{n_down} downgrades vs {n_up} upgrades")
                    else:
                        lines.append(
                            f"  ⚪ Neutral — "
                            f"{n_up} upgrades, {n_down} downgrades")
                else:
                    lines.append(
                        f"\nNo rating changes in the last "
                        f"{recent_days} days.")
        except Exception as e:
            lines.append(f"\nRating changes: Could not retrieve ({e})")

        lines.append("\nSource: Yahoo Finance analyst consensus data")
        lines.append(
            "Note: Price targets and ratings reflect Wall Street "
            "consensus, not guaranteed outcomes.")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching analyst ratings for {ticker}: {str(e)}"
        

# Successor issuers whose 10-K was filed under a predecessor CIK (holding-company reorganizations).
_PREDECESSOR_CIKS = {
    'XOM': ['0000034088'],      # ExxonMobil Holdings Corp <- Exxon Mobil Corp
}


@mcp.tool()
def get_legal_proceedings(ticker: str, debug: bool = False) -> str:
    """Fetch legal proceedings and litigation disclosures from SEC 10-K filing.
    Returns material lawsuits, regulatory actions, DOJ/FTC/EC cases, class
    actions, and other legal risks companies are required to disclose.
    Searches both Item 3 and Note 10/Legal Matters in financial statements.
    Note: extraction may fail for combined filings, foreign filers (20-F),
    or companies with minimal legal disclosure. Review the SEC link directly.
    ticker: stock symbol e.g. GOOG, META, MSFT, AAPL, CRM, TSLA
    debug: True appends a trace of every anchor tried (diagnostics only)
    """
    try:
        import urllib.request
        import json
        import re

        headers = {'User-Agent': 'Jay jay@example.com'}

        def sec_get(url):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()

        # Step 1: Resolve ticker to CIK
        tickers_data = json.loads(sec_get(
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

        # Step 2: Find most recent 10-K or 20-F.
        # Skip 10-K/A and 20-F/A amendments — partial re-filings that rarely
        # contain the full legal proceedings section.
        subs = json.loads(sec_get(
            f'https://data.sec.gov/submissions/CIK{cik}.json'))
        filings = subs.get('filings', {}).get('recent', {})
        forms = filings.get('form', [])
        accns = filings.get('accessionNumber', [])
        dates = filings.get('filingDate', [])

        # Nov 3 2026: XOM files so many documents that `recent` only reaches back a few
        # months; its February 10-K had rolled into the older pages listed under
        # filings.files ("No 10-K filing found for XOM"). Read those when needed.
        if not any(f in ('10-K', '20-F') for f in forms):
            for fe in (subs.get('filings', {}).get('files') or [])[:6]:
                try:
                    older = json.loads(sec_get(
                        'https://data.sec.gov/submissions/' + fe['name']))
                except Exception:
                    continue
                if any(f in ('10-K', '20-F') for f in older.get('form', [])):
                    forms = list(older.get('form', []))
                    accns = list(older.get('accessionNumber', []))
                    dates = list(older.get('filingDate', []))
                    break

        tenk_accn  = None
        tenk_date  = None
        form_type  = None
        for i, form in enumerate(forms):
            if form in ('10-K', '20-F'):    # base filings only, skip /A
                tenk_accn = accns[i]
                tenk_date = dates[i]
                form_type = form
                break

        # Nov 3 2026: XOM now resolves to ExxonMobil Holdings Corp (CIK 2115436), a new
        # holding company that has filed 10-Qs but no 10-K; the FY2025 10-K was filed
        # by the predecessor, Exxon Mobil Corp (CIK 34088). For successor issuers read
        # the predecessor's filings. Add entries here as holding-company
        # reorganizations happen (an 8-K12B in the successor's filings is the tell).
        if not tenk_accn:
            for _pc in _PREDECESSOR_CIKS.get(ticker.upper(), []):
                try:
                    _psubs = json.loads(sec_get(
                        f'https://data.sec.gov/submissions/CIK{_pc}.json'))
                except Exception:
                    continue
                _pr = _psubs.get('filings', {}).get('recent', {})
                _pf, _pa, _pd = (_pr.get('form', []), _pr.get('accessionNumber', []),
                                 _pr.get('filingDate', []))
                for i, form in enumerate(_pf):
                    if form in ('10-K', '20-F'):
                        tenk_accn, tenk_date, form_type = _pa[i], _pd[i], form
                        cik = _pc
                        company_name = (_psubs.get('name') or company_name) + " (predecessor filer)"
                        break
                if tenk_accn:
                    break

        if not tenk_accn:
            return (f"No 10-K filing found for {ticker.upper()} "
                    f"(CIK {cik}, {company_name}; {len(forms)} filings listed, "
                    f"{len(subs.get('filings', {}).get('files') or [])} older pages, "
                    f"forms seen: {sorted(set(forms))[:12]})")

        # Step 3: Get filing index — find main HTM document.
        # Exclude XBRL inline viewer files (R1.htm … R999.htm) which are
        # auto-generated, can be large, and contain no narrative legal text.
        accn_fmt = tenk_accn.replace('-', '')
        cik_num  = cik.lstrip('0')
        index_url = (f'https://www.sec.gov/Archives/edgar/data/'
                     f'{cik_num}/{accn_fmt}/index.json')
        index_data = json.loads(sec_get(index_url))
        items = index_data.get('directory', {}).get('item', [])

        doc_filename = None
        best_size = 0
        preferred = None
        for item in items:
            name = item.get('name', '')
            name_lower = name.lower()
            try:
                size = int(item.get('size', 0) or 0)
            except (ValueError, TypeError):
                size = 0
            if not name_lower.endswith(('.htm', '.html')):
                continue
            if 'index' in name_lower:
                continue
            # Skip exhibit files — xex21, ex3d1, exh1025, exhibit103, etc.
            # Nov 3 2026: DUK names its exhibits like duk-20251231x10kxexx21.htm
            # ("exx21": two x's). The old pattern missed it, and because the name
            # contains "10k" it was then PREFERRED over the real 10-K, so legal-
            # proceedings extraction read a 7.7K-character subsidiaries list.
            if re.search(r'ex+\d|exhibit|exh\d', name_lower):
                continue
            # Skip XBRL inline viewer files (R1.htm … R999.htm)
            if re.match(r'^r\d+\.html?$', name_lower):
                continue
            if size > best_size:
                best_size = size
                doc_filename = name
            # Prefer files explicitly named as 10-K or 20-F
            if ('10k' in name_lower or '20f' in name_lower) and \
               (preferred is None or size > best_size):
                preferred = name
        if preferred:
            doc_filename = preferred
        if not doc_filename:
            return f"Could not find 10-K document for {ticker.upper()}"

        # Step 4: Fetch and clean the document
        doc_url = (f'https://www.sec.gov/Archives/edgar/data/'
                   f'{cik_num}/{accn_fmt}/{doc_filename}')
        raw = sec_get(doc_url).decode('utf-8', errors='ignore')

        # Clean HTML
        clean = re.sub(r'<[^>]+>', ' ', raw)
        clean = re.sub(r'&nbsp;',  ' ', clean)
        clean = re.sub(r'&amp;',   '&', clean)
        clean = re.sub(r'&#160;',  ' ', clean)
        clean = re.sub(r'&#8226;', '•', clean)
        clean = re.sub(r'&#8211;', '-', clean)
        clean = re.sub(r'&#8217;', "'", clean)
        clean = re.sub(r'&#\d+;',  ' ', clean)
        clean = re.sub(r'\s+',     ' ', clean)

        doc_len = len(clean)
        legal_text = None

        # End markers — signals end of legal section.
        # NOTE 1: 'Stockholders' removed — appears in TSLA legal text itself.
        # NOTE 2: 'Note 12' and 'Note 14' removed — they would prematurely
        #         truncate extractions that START at those note headings
        #         (AMD=Note 12, ELV=Note 14). Use Note 15/17/18 as stops.
        end_markers = [
            'Non-Income Taxes', 'Note 11', 'NOTE 11',
            'Note 15', 'NOTE 15', 'Note 17', 'NOTE 17', 'Note 18', 'NOTE 18',
            'Income Taxes', 'INCOME TAXES',
            'Subsequent Events', 'SUBSEQUENT EVENTS',
            'Item 4', 'ITEM 4',
            'STOCK-BASED COMPENSATION', 'Stock-Based Compensation',
            'NOTE T.', 'NOTE S.', 'NOTE R.',
            'Other Stock Transactions',
            'Incentive Awards Stock-based',
        ]

        _ext = {"capped": False, "s": "", "trace": []}

        def _tr(start_pos, outcome, n=0):
            _ext["trace"].append(
                f"  [{_ext['s']}] pos {start_pos} ({100 * start_pos // max(doc_len, 1)}%) "
                f"len {n} -> {outcome} | {clean[start_pos:start_pos + 120]!r}")

        def extract_from(start_pos, chunk_size=25000, min_len=100, item3=False, stop_re=None):
            chunk = clean[start_pos:start_pos + chunk_size]
            end_pos = len(chunk)
            if stop_re:
                # end at the next note heading (C1: the note an Item 3 stub points to)
                _ms = re.search(stop_re, chunk[200:])
                if _ms:
                    end_pos = 200 + _ms.start()
            if item3:
                # A window that opens on the "Item 3" heading ends at "Item 4" however
                # short the section is (CBRE's is ~1,000 characters; the generic
                # end-marker search ignores the first 2,000 and ran on into Part II).
                _m4 = re.search(r'(?i)\bitem\s+4\b', chunk[40:])
                if _m4:
                    end_pos = 40 + _m4.start()
            for marker in end_markers:
                # Use 2000 char minimum to avoid false early triggers
                p = chunk.find(marker, 2000)
                if p != -1 and p < end_pos:
                    end_pos = p
            # Nov 3 2026: when no end marker was found inside the window, the cut is
            # the window's, not the section's. FSLR's extract stopped mid-action with
            # no sign anything was missing (the [Truncated] note below could never
            # fire, because chunk_size already limits the text to 25,000 characters).
            _ext["capped"] = (end_pos == len(chunk)) and (start_pos + chunk_size < len(clean))
            result = chunk[:end_pos].strip()
            # Nov 3 2026: FSLR's extraction began at a risk-factor cross-reference
            # ("Note 14. Commitments and Contingencies - Legal Proceedings to our
            # consolidated financial statements...") and spent its window on tax,
            # privacy, cyber and properties text. Item 1B/1C headings can never
            # sit inside a financial-statement note or Item 3, so a window that
            # contains them started too early: reject it and let the caller try
            # the next anchor.
            if re.search(r'(?i)unresolved\s+staff\s+comments|\bitem\s+1c\b', result):
                _tr(start_pos, 'rejected: contains Item 1B/1C', len(result))
                return ''
            # Second FSLR failure: the start was the pointer sentence that closes
            # Item 3 ("...Legal Proceedings to our consolidated financial statements
            # for information regarding legal proceedings. Item 4. Mine Safety
            # Disclosures None. PART II Item 5..."). Item 4 within the first
            # paragraph means the text opens at the END of the section; the
            # 2,000-char end-marker holdoff then let it run on into Items 5-7.
            if not item3 and re.search(r'(?i)item\s+4\.?\s*mine\s+safety', result[:1500]):
                _tr(start_pos, 'rejected: Item 4 within 1500 chars', len(result))
                return ''
            _tr(start_pos, 'ok' if len(result) >= min_len else 'too short', len(result))
            return result if len(result) >= min_len else ''

        # Strategy A: Unique anchor phrases that only appear in the
        # actual legal notes section — ordered by specificity.
        # Covers: GOOG (Legal Matters), TSLA (Commitments and Contingencies),
        # AAPL/NVDA/AVGO (Antitrust/Privacy Matters),
        # DUK (COMMITMENTS AND CONTINGENCIES ENVIRONMENTAL/LITIGATION)
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
            'Note 16. Contingencies',           # LLY
            'NOTE 16. CONTINGENCIES',           # LLY caps variant
            'Note 16: Contingencies',           # LLY colon variant
            # DUK-style: ALL-CAPS note heading with no note-number prefix,
            # immediately followed by section name (ENVIRONMENTAL or LITIGATION)
            'COMMITMENTS AND CONTINGENCIES ENVIRONMENTAL',
            'COMMITMENTS AND CONTINGENCIES LITIGATION',
        ]

        # Search from 50%, 35%, 60%, then 75% of document
        _ext["s"] = "A"
        for search_from_pct in [0.50, 0.35, 0.60, 0.75]:
            threshold = int(doc_len * search_from_pct)
            for phrase in anchor_phrases:
                pos = clean.lower().find(phrase.lower(), threshold)
                if pos != -1:
                    # Nov 7 2026: DE's note ends with a running list "...antitrust matters
                    # (including class action litigation) product liability...". The
                    # capitalised anchor 'Antitrust Matters' matched that lowercase mid-list
                    # text and won over DE's real Item 3 (FTC action). An anchor written with a
                    # capital must match a capital.
                    if phrase[0].isupper() and not clean[pos].isupper():
                        continue
                    candidate = extract_from(pos, 25000)
                    if len(candidate) > 500:
                        legal_text = candidate
                        break
            if legal_text:
                break

        # Strategy A2: Many companies cross-reference Item 3 to a financial
        # statement note headed "Note N – Commitments and Contingencies".
        # Separator varies across companies: en-dash (–), em-dash (—),
        # period, comma, hyphen, or comma+quote. The character class covers
        # all observed variants.
        # Strategy C0: an explicit "Item 3. Legal Proceedings" heading followed by its
        # OWN text (>1,500 chars, not a pointer to a note). Nov 3 2026: FSLR's notes
        # say "14. Commitments and Contingencies" (no "Note"), so the note-heading
        # strategy below only ever found prose pointers elsewhere in the filing.
        # Item 3 pointers (DUK, CBRE, JPM...) fail this test and fall through as before.
        if not legal_text:
            _ext["s"] = "C0"
            item3_re = re.compile(r'(?i)ITEM\s+3[\.\s]+LEGAL\s+PROCEEDINGS')
            _stubs = []
            for m in list(item3_re.finditer(clean, int(doc_len * 0.30)))[:8]:
                after = clean[m.end():m.end() + 60]
                if re.match(r'\W{0,3}(for|of|in|and|to|under|above|below|included|contained|set|see|refer)\b', after):
                    continue        # prose reference, not the heading
                # Nov 7 2026: GE's 10-K ends with a "Form 10-K cross-reference index"
                # whose row reads "Item 3. Legal Proceedings 70-71 Item 4. Mine Safety..."
                # (page numbers, no text). It was taken for the real section and ran
                # through the signatures. Skip index rows.
                if re.search(r'(?i)(?:\bsee|\bin|\bunder|\bto|\balso|\band|\bwith|\bof|\bat)\W{0,3}$',
                             clean[max(0, m.start() - 14):m.start()]):
                    _tr(m.start(), 'skipped: prose reference to Item 3', 0)
                    continue
                if re.match(r'(?i)item\s+3[\.\s]+legal\s+proceedings\s+(?:pages?\s+)?'
                            r'[\d\s,\-\u2013\u2014]{1,25}item\s+4', clean[m.start():m.start() + 120]):
                    _tr(m.start(), 'skipped: cross-reference index row', 0)
                    continue
                cand = extract_from(m.start(), 25000, item3=True)
                head = cand[:300].lower()
                # Short sections (400-1,500 chars) count only if they carry their own
                # text; one that mainly points at a note falls through to the note.
                _ptr = re.search(r'(?i)\b(see|refer to|included in|set forth in|incorporated)\b.{0,100}\bnote\s+\d+', cand)
                if (len(cand) > 400 and 'see note' not in head and 'refer to note' not in head
                        and 'incorporated herein' not in head and 'incorporated by reference' not in head
                        and (len(cand) > 1500 or not _ptr)):
                    legal_text = cand
                    break
                # Nov 7 2026: keep every genuine Item 3 that was not accepted above. Short
                # ones (<= 600) are pointers or one-paragraph statements; a pointer that also
                # carries its own text (EOG: 1,143 chars) is kept up to 2,000.
                if cand and (len(cand) <= 600 or (_ptr and len(cand) <= 2000)):
                    _stubs.append(cand)

        def _xref_title(t):
            # "Commitments and Contingencies to the Consolidated Financial Statements" is a
            # cross-reference inside prose (GXO: inside the guaranteed-securities text),
            # never the heading of the note itself.
            return bool(re.search(r'(?i)\b(to|in|of|within)\s+the\s+(consolidated|notes?)\b', t[:110]))

        def _index_row(t):
            # Financial-statement index: "Note 18 - Commitments and Contingencies 73 33 Report
            # of Independent Registered Public Accounting Firm" (GXO, Nov 7 2026): the title is
            # followed by page numbers, not by the note's text. Only one "Note N" sits in
            # the row, so the several-notes-in-a-row contents test did not catch it.
            return bool(re.search(r'(?i)(?:commitments|contingenc|litigation|legal)[^.\d]{0,45}\s\d{1,3}\s+\d{1,3}\b', t[:110]))

        def _focus(cand, stub):
            # The stub may name a lettered subsection ("Note 14 j)": CB) or quote a
            # subsection title ("Legal and Regulatory Matters" in Note 22: CI). Start the
            # text there when it exists inside the note, so the window is not spent on
            # derivatives or guarantees first.
            best = None
            ml = re.search(r'(?i)\bNote\s+\d{1,2}\s+\(?([a-z])\)', stub)
            if ml:
                mm = re.search(r'(?<![\w])%s\)\s+[A-Z]' % ml.group(1), cand[200:])
                if mm:
                    best = 200 + mm.start()
            if best is None:
                q = re.search(r'["\u201c]([^"\u201d]{8,60})["\u201d]', stub)
                if q:
                    i = cand.lower().find(q.group(1).lower(), 100)
                    if i != -1:
                        best = i
            if best is not None and len(cand) - best >= 500:
                return cand[best:]
            return cand

        # Strategy C1: Item 3 is only a stub that points at a numbered note (XOM: "Refer
        # to the relevant portions of Note 7 ... for additional information on legal
        # proceedings"). Read the number, find that note's own heading later in the
        # filing, and extract just that note (to the next note heading).
        if not legal_text and _stubs:
            _ext["s"] = "C1"
            for stub in _stubs:
                for num in list(dict.fromkeys(re.findall(r'(?i)\bNote\s+(\d{1,2})\b', stub)))[:3]:
                    head_re = re.compile(r'\bNote\s+%s\s*[\.\:\-\u2013\u2014]\s+(?=[A-Z])' % num)
                    stop = r'\bNote\s+%d\s*[\.\:\-\u2013\u2014]\s+[A-Z]' % (int(num) + 1)
                    for m in list(head_re.finditer(clean, int(doc_len * 0.30)))[:10]:
                        title = clean[m.end():m.end() + 80]
                        if not re.search(r'(?i)litigation|legal|contingenc|commitment|proceeding', title):
                            continue
                        if _xref_title(clean[m.end():m.end() + 120]) or _index_row(clean[m.end():m.end() + 120]):
                            continue
                        # table-of-contents / index entries list several notes in a row
                        if len(re.findall(r'\bNote\s+\d+', clean[m.start():m.start() + 600])) >= 3:
                            continue
                        cand = extract_from(m.start(), 25000, stop_re=stop)
                        if len(cand) > 500:
                            legal_text = _focus(cand, stub)
                            break
                    if legal_text:
                        break
                # Nov 7 2026: many filers number their notes without the word "Note"
                # (EOG: "8. Commitments and Contingencies", FSLR "14. ..."), so the pass
                # above never finds them. Same idea, bare "N. Title" headings; skip
                # "Item N." and contents rows (the next heading follows within 250 chars).
                if not legal_text:
                    for num in list(dict.fromkeys(re.findall(r'(?i)\bNote\s+(\d{1,2})\b', stub)))[:3]:
                        n_i = int(num)
                        bare_re = re.compile(r'(?<![\w.$,])(?:%d\.|\(%d\))\s+(?=[A-Z])' % (n_i, n_i))
                        stop = r'(?<![\w.$,])(?:%d\.|\(%d\))\s+[A-Z]' % (n_i + 1, n_i + 1)
                        for m in list(bare_re.finditer(clean, int(doc_len * 0.30)))[:40]:
                            if clean[max(0, m.start() - 6):m.start()].lower().endswith(
                                    ('item ', 'note ', 'otes ', ' and ', ' see ')):
                                continue        # "Item 8." / "See Note 14. Commitments..." prose
                            # Nov 7 2026: the title must START with a heading word. PNW's
                            # "14. Purchase obligations may include commitments..." passed the
                            # looser keyword test and returned a commitments footnote.
                            if not re.match(r'(?i)\W{0,3}(commitments?|contingenc|contingent|litigation|legal)',
                                            clean[m.end():m.end() + 80]):
                                continue
                            if _xref_title(clean[m.end():m.end() + 120]) or _index_row(clean[m.end():m.end() + 120]):
                                continue
                            if re.search(stop, clean[m.end():m.end() + 250]):
                                continue
                            cand = extract_from(m.start(), 25000, stop_re=stop)
                            if len(cand) > 500:
                                # a stub that carries its own text (EOG's environmental-
                                # proceedings statement) is kept in front of the note
                                cand = _focus(cand, stub)
                                legal_text = (stub + " " + cand) if len(stub) > 600 else cand
                                break
                        if legal_text:
                            break
                if legal_text:
                    break

        # Strategy C1b (Nov 7 2026): Item 3 points at a NAMED section of the MD&A instead of a
        # note (LSTR: "See Item 7, Management's Discussion and Analysis ... Legal Proceedings").
        # There is no note number to follow. The section is a heading that opens a sentence,
        # unlike the cross-references that name it in running text ("...Operations Legal
        # Proceedings." / "... Operations - Legal Proceedings ."), and unlike "Item 3. Legal
        # Proceedings" itself. It ends at the next MD&A heading.
        if not legal_text and _stubs:
            _ext["s"] = "C1b"
            _mda_stop = (r'(?i)\b(Item\s+7A|Quantitative and Qualitative Disclosures|Off[- ]Balance|'
                         r'Critical Accounting|Recent Accounting|Contractual Obligations|'
                         r'Seasonality|Item\s+8\b)')
            for stub in _stubs:
                if not re.search(r'(?i)\bitem\s+7\b', stub):
                    continue
                for m in list(re.finditer(r'(?:Legal Proceedings|Litigation|Legal Matters)\s+(?=[A-Z])',
                                          clean[int(doc_len * 0.30):]))[:40]:
                    pos = int(doc_len * 0.30) + m.start()
                    before = clean[max(0, pos - 12):pos]
                    if not re.search(r'[\.\)]\s$', before) or re.search(r'(?i)item\s+\d+[a-c]?\.\s$', before):
                        continue
                    cand = extract_from(pos, 25000, stop_re=_mda_stop)
                    if len(cand) > 500:
                        legal_text = cand
                        break
                if legal_text:
                    break

        # Strategy C0b (Nov 7 2026): a short Item 3 that is a complete statement and not a
        # pointer. DLR's is 282 characters ("...we were not a party to any material legal
        # proceedings...") and JBHT's 388; the 400-character floor above dropped both, the
        # wider strategies then rejected their windows (Item 4 right after), and DLR fell
        # through to a risk-factor paragraph (strategy D) while JBHT returned nothing. The
        # section really is that short, so return it as is.
        if not legal_text and _stubs:
            _ext["s"] = "C0b"
            for stub in _stubs:
                if (len(stub) >= 100 and len(stub) <= 600 and
                        not re.search(r'(?i)\bnotes?\s+\d|incorporated|\b(see|refer)\b', stub)):
                    legal_text = stub
                    _ext["capped"] = False
                    _tr(0, 'accepted: short complete Item 3', len(stub))
                    break

        if not legal_text:
            _ext["s"] = "A2"
            note_cc_re = re.compile(
                r'Note\s+\d+[\.\s:\u2013\u2014,\-\"\u201c\u201d]+'
                r'Commitments\s+and\s+Contingencies',
                re.IGNORECASE)
            for threshold_pct in [0.25, 0.10]:
                threshold = int(doc_len * threshold_pct)
                for m in list(note_cc_re.finditer(clean, threshold))[:8]:
                    candidate = extract_from(m.start(), 25000)
                    if len(candidate) > 500:
                        legal_text = candidate
                        break
                if legal_text:
                    break

        # Strategy B: Find 'Legal Matters' in final 30% of document
        if not legal_text:
            _ext["s"] = "B"
            threshold = int(doc_len * 0.70)
            positions = [
                m.start() for m in
                re.finditer(r'(?i)\blegal\s+matters\b', clean)
                if m.start() > threshold
            ]
            for pos in positions:
                candidate = extract_from(pos, 25000)
                if len(candidate) > 500:
                    legal_text = candidate
                    break

        # Strategy B2: Insurance/financial filers (e.g. Chubb/CB) embed legal
        # proceedings as a lettered subsection within a larger Commitments
        # note: "h) Legal proceedings". Find the subsection directly.
        if not legal_text:
            _ext["s"] = "B2"
            subsec_re = re.compile(r'\b[a-z]\)\s*[Ll]egal\s+proceedings\b')
            threshold = int(doc_len * 0.30)
            for m in subsec_re.finditer(clean):
                if m.start() > threshold:
                    candidate = extract_from(m.start(), 25000)
                    if len(candidate) > 500:
                        legal_text = candidate
                        break

        # Strategy C: Find Item 3 Legal Proceedings — skip cross-references
        if not legal_text:
            _ext["s"] = "C"
            threshold = int(doc_len * 0.30)
            positions = [
                m.start() for m in
                re.finditer(
                    r'(?i)ITEM\s+3[\.\s]+LEGAL\s+PROCEEDINGS',
                    clean)
                if m.start() > threshold
            ]
            for pos in positions:
                candidate = extract_from(pos, 25000)
                if (len(candidate) > 500
                        and 'see note' not in candidate[:200].lower()
                        and 'incorporated herein' not in
                        candidate[:200].lower()):
                    legal_text = candidate
                    break

        # Strategy C2: 20-F filers (foreign private issuers like RIO) do not
        # use Item 3. Legal disclosures appear under Item 8 as "Legal and
        # Arbitration Proceedings". Search from 10% onward.
        if not legal_text and form_type == '20-F':
            _ext["s"] = "C2"
            for pattern in [
                r'(?i)legal\s+and\s+arbitration\s+proceedings',
                r'(?i)ITEM\s+8[\.\s]+.*?legal',
                r'(?i)legal\s+proceedings\b',
            ]:
                positions = [
                    m.start() for m in
                    re.finditer(pattern, clean)
                    if m.start() > int(doc_len * 0.10)
                ]
                for pos in positions:
                    candidate = extract_from(pos, 25000)
                    if (len(candidate) > 500
                            and 'see note' not in candidate[:200].lower()
                            and 'incorporated herein' not in
                            candidate[:200].lower()):
                        legal_text = candidate
                        break
                if legal_text:
                    break

        # Strategy C0c (Nov 7 2026): an Item 3 that carries its own text but also points at a
        # note which could not be located (EOG: 1,143 characters, "see ... Note 8", then its
        # environmental-proceedings paragraph). Strategy D picked a "Properties" paragraph
        # for EOG, so use the real Item 3 instead and say the note is missing. Tiny
        # pointer-only stubs (<= 600) still go to D as before.
        if not legal_text and _stubs:
            _long = [x for x in _stubs if len(x) > 600]
            if _long:
                _ext["s"] = "C0c"
                legal_text = (max(_long, key=len) +
                              " [Item 3 refers to a financial-statement note whose text could "
                              "not be located; the note's matters are not included here.]")
                _ext["capped"] = False
                _tr(0, 'accepted: Item 3 with own text, note not located', len(legal_text))

        # Strategy D: Last resort — broad litigation keywords.
        # Also catches genuinely minimal disclosures (e.g. AMD) by using
        # a lower min_len=100 threshold — returning the short boilerplate
        # rather than a NOT_EXTRACTED failure message.
        if not legal_text:
            _ext["s"] = "D"
            for keyword in ['pending legal proceedings',
                            'material litigation',
                            'class action',
                            'defendant or plaintiff',
                            'ordinary course of business']:
                pos = clean.lower().find(keyword, doc_len // 4)
                if pos != -1:
                    start = max(0, pos - 200)
                    candidate = extract_from(start, 5000, min_len=100)
                    if candidate:
                        legal_text = candidate
                        break

        if debug:
            for _m in list(re.finditer(r'(?i)ITEM\s+3[\.\s]+LEGAL\s+PROCEEDINGS', clean))[:10]:
                _ext["trace"].append(
                    f"  [item3 heading] pos {_m.start()} ({100 * _m.start() // max(doc_len, 1)}%) "
                    f"| {clean[_m.start():_m.start() + 160]!r}")
        _trace = ("\n\nDEBUG TRACE (doc %d chars, source %s):\n%s" % (
            doc_len, doc_filename, "\n".join(_ext["trace"]) or "  (no anchors tried)")) if debug else ""
        if not legal_text:
            return (
                f"=== {company_name} ({ticker.upper()}) ===\n"
                f"10-K filed: {tenk_date}\n\n"
                f"Legal proceedings section could not be extracted.\n"
                f"This may indicate minimal disclosed legal exposure\n"
                f"or an unusual filing format.\n\n"
                f"Review directly:\n{doc_url}" + _trace
            )

        # Step 5: Final cleanup and truncation
        legal_text = re.sub(r'\s+', ' ', legal_text).strip()
        if len(legal_text) > 25000:
            legal_text = (legal_text[:25000]
                          + "\n\n[Truncated — full text in SEC filing]")
        elif _ext["capped"]:
            _cut = legal_text.rfind('. ')
            if _cut > len(legal_text) * 0.8:
                legal_text = legal_text[:_cut + 1]
            legal_text += ("\n\n[Truncated — the extraction window ended before the legal section did; "
                           "the last matter above may be incomplete and further matters may follow. "
                           "State this limit in the analysis; full text in SEC filing]")

        lines = [
            f"=== {company_name} ({ticker.upper()}) ===",
            f"Legal Proceedings — 10-K filed {tenk_date}",
            f"Source: {doc_filename}\n",
            legal_text,
            f"\nFull filing: {doc_url}",
            f"SEC EDGAR: https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik}&type=10-K"
        ]

        return "\n".join(lines) + _trace

    except Exception as e:
        return f"Error fetching legal proceedings for {ticker}: {str(e)}"


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
    uvicorn.run(app, host="0.0.0.0", port=8642)
