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
            f"Profit Margin:     {fmt(info.get('profitMargins', 0) * 100, suffix='%')}\n"
        )
    except Exception as e:
        return f"Error fetching info for {ticker}: {str(e)}"


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

        # Free Cash Flow
        fcf = info.get('freeCashflow')
        fcf_str = fmt(fcf, billions=True) if fcf else "N/A"

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
        lines.append(f"  Total Cash:        {fmt(total_cash, billions=True)}")
        lines.append(f"  Total Debt:        {fmt(total_debt, billions=True)}")
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

        hist = yf.Ticker(ticker.upper()).history(period="1y")[["Close", "High"]]
        if hist.empty or len(hist) < 20:
            return f"Insufficient price history for {ticker}."

        closes = hist["Close"].values
        highs  = hist["High"].values

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
        if ma50 and ma200:
            if ma50 > ma200:
                ma_signal = "GOLDEN CROSS (bullish)"
            else:
                ma_signal = "DEATH CROSS (bearish)"
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

        # 52W drawdown from high — uses intraday High (not Close), matching
        # Yahoo's own fiftyTwoWeekHigh convention (highest price ever
        # touched, not highest closing price). Using Close-only here
        # previously produced a systematic $1-10 gap against stock_info's
        # figure, since a closing-price high can never exceed the true
        # intraday high.
        high_52w = max(highs[-252:]) if len(highs) >= 252 else max(highs)
        drawdown = (current - high_52w) / high_52w * 100

        lines = [f"=== {ticker.upper()} Technical Indicators ===\n"]
        lines.append(f"Current Price:     ${current:.2f}")
        lines.append(f"52W High:          ${high_52w:.2f}")
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
        lines.append(f"  Institutional:  {inst_pct*100:.1f}%" if inst_pct else "  Institutional:  N/A")
        lines.append(f"  Insider:        {insider_pct*100:.1f}%" if insider_pct else "  Insider:        N/A")
        lines.append(f"  Float:          {((1 - (inst_pct or 0) - (insider_pct or 0))*100):.1f}% public" if inst_pct else "")

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
                    avg_change = sum(changes) / len(changes)
                    net_adding = sum(1 for c in changes if c > 0)
                    net_reducing = sum(1 for c in changes if c < 0)
                    lines.append(f"\n── Accumulation Signal ──")
                    lines.append(f"  Avg position change: {avg_change*100:+.1f}%")
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
        next_date = info.get('earningsTimestamp')
        if next_date:
            next_str = datetime.fromtimestamp(
                next_date, tz=timezone.utc).strftime('%Y-%m-%d')
        else:
            next_date_low = info.get('earningsTimestampStart')
            next_date_high = info.get('earningsTimestampEnd')
            if next_date_low and next_date_high:
                low_str = datetime.fromtimestamp(
                    next_date_low, tz=timezone.utc).strftime('%Y-%m-%d')
                high_str = datetime.fromtimestamp(
                    next_date_high, tz=timezone.utc).strftime('%Y-%m-%d')
                next_str = f"{low_str} to {high_str} (estimate)"
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
            if hist is None or hist.empty:
                try:
                    hist = t.get_earnings_dates(limit=8)
                    if hist is not None and not hist.empty:
                        hist = hist.rename(columns={
                            'Reported EPS': 'epsActual',
                            'EPS Estimate': 'epsEstimate',
                            'Surprise(%)':  'surprisePct',
                        })
                except Exception:
                    hist = None
            if hist is not None and not hist.empty:
                hist = hist.tail(8)
                lines.append("\nQuarterly EPS (last 8 quarters):")
                lines.append(f"  {'Quarter':<12} {'Actual':>8} {'Estimate':>10} {'Surprise':>10} {'Beat?':>6}")
                lines.append("  " + "-" * 50)
                for idx, row in hist.iterrows():
                    quarter = str(row.get('quarter', idx))
                    actual = row.get('epsActual')
                    estimate = row.get('epsEstimate')
                    # get_earnings_dates() gives surprisePct (%), not the
                    # raw EPS-dollar diff epsDifference gives — compute the
                    # dollar surprise from actual/estimate when only the
                    # percent form is available, so downstream formatting
                    # (and the beat/miss sign) stays consistent either way.
                    surprise = row.get('epsDifference')
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
        lines.append(f"  Revenue Growth:   {info.get('revenueGrowth', 'N/A')}")
        lines.append(f"  Earnings Growth:  {info.get('earningsGrowth', 'N/A')}")
        lines.append(f"  Profit Margin:    {info.get('profitMargins', 'N/A')}")
        lines.append(f"  P/E (TTM):        {info.get('trailingPE', 'N/A')}")
        lines.append(f"  P/E (Forward):    {info.get('forwardPE', 'N/A')}")
        lines.append(f"  PEG Ratio:        {info.get('pegRatio', 'N/A')}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching earnings for {ticker}: {str(e)}"

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
        cik = None
        company_name = None
        for entry in tickers_data.values():
            if entry.get('ticker', '').upper() == ticker_upper:
                cik = str(entry['cik_str']).zfill(10)
                company_name = entry.get('title', ticker_upper)
                break

        if not cik:
            return f"Could not find SEC CIK for ticker {ticker_upper}. "\
                   f"Company may not file with SEC or ticker is incorrect."

        # Step 2: Fetch company facts
        facts_url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
        data = sec_get(facts_url)
        facts = data.get('facts', {}).get('us-gaap', {})

        if not facts:
            return f"No US-GAAP financial data found for {ticker_upper} "\
                   f"(CIK: {cik}). Company may use IFRS reporting."

        # Step 3: Extract quarterly data helper
        def get_quarterly(concept, unit):
            items = facts.get(concept, {}).get('units', {}).get(unit, [])
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
                items = facts.get(concept, {}).get('units', {}).get(unit, [])
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

        quarters = min(quarters, 12)
        lines = [f"=== {company_name} ({ticker_upper}) — SEC EDGAR ===\n"]

        # Step 5: Quarterly EPS
        eps_q = get_quarterly('EarningsPerShareDiluted', 'USD/shares')
        if not eps_q:
            eps_q = get_quarterly('EarningsPerShareBasic', 'USD/shares')

        # Step 6: Quarterly Revenue — try multiple GAAP concepts
        rev_q = get_quarterly(
            'RevenueFromContractWithCustomerExcludingAssessedTax', 'USD')
        if not rev_q:
            rev_q = get_quarterly('Revenues', 'USD')
        if not rev_q:
            rev_q = get_quarterly('SalesRevenueNet', 'USD')
        if not rev_q:
            rev_q = get_quarterly('RevenueFromContractWithCustomerIncludingAssessedTax', 'USD')

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
        rev_annual_for_q4 = get_annual(
            ['RevenueFromContractWithCustomerExcludingAssessedTax',
             'Revenues', 'SalesRevenueNet',
             'RevenueFromContractWithCustomerIncludingAssessedTax'],
            'USD')
        ni_annual_for_q4  = get_annual('NetIncomeLoss', 'USD')
        eps_annual_for_q4 = get_annual('EarningsPerShareDiluted', 'USD/shares')

        def _q4_derive(quarterly_items, annual_items, year):
            q123 = [it['val'] for it in quarterly_items
                    if it['end'][:4] == year and it['end'][5:7] in ('03', '06', '09')]
            if len(q123) != 3:
                return None
            annual_val = next((a['val'] for a in annual_items if a['end'][:4] == year), None)
            if annual_val is None:
                return None
            return annual_val - sum(q123)

        years_seen = sorted({q['end'][:4] for q in eps_q} | {r['end'][:4] for r in rev_q})
        derived_q4 = []
        for year in years_seen:
            q4_end = f"{year}-12-31"
            if q4_end in rev_by_date or any(q['end'] == q4_end for q in eps_q):
                continue  # already have a real, filed Q4 entry — don't overwrite it

            d_rev = _q4_derive(rev_q, rev_annual_for_q4, year)
            d_ni  = _q4_derive(ni_q, ni_annual_for_q4, year)
            d_eps = _q4_derive(eps_q, eps_annual_for_q4, year)

            if d_rev is not None:
                rev_by_date[q4_end] = d_rev
            if d_ni is not None:
                ni_by_date[q4_end] = d_ni
            if d_eps is not None:
                derived_q4.append({'end': q4_end, 'val': d_eps, 'derived': True})

        eps_q = sorted(eps_q + derived_q4, key=lambda x: x['end'], reverse=True)

        # Step 8: Combined quarterly table
        lines.append(f"Quarterly Results (last {quarters} quarters from SEC filings):")
        lines.append(
            f"  {'Quarter End':<13} {'Revenue':>10} "
            f"{'Net Income':>11} {'Diluted EPS':>12}")
        lines.append("  " + "-" * 50)

        displayed = 0
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

            lines.append(
                f"  {end:<13} {rev_str:>10} {ni_str:>11} {eps_str:>12}{tag}")
            displayed += 1

        if displayed == 0:
            lines.append("  No quarterly EPS data found in SEC filings.")

        # Step 9: Annual revenue trend — merged across all known revenue
        # tags (see get_annual() fix note above), not a sequential fallback
        rev_annual = get_annual(
            ['RevenueFromContractWithCustomerExcludingAssessedTax',
             'Revenues',
             'SalesRevenueNet',
             'RevenueFromContractWithCustomerIncludingAssessedTax'],
            'USD')

        if rev_annual:
            lines.append(f"\nAnnual Revenue (from 10-K filings):")
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
        eps_annual = get_annual('EarningsPerShareDiluted', 'USD/shares')
        if eps_annual:
            lines.append(f"\nAnnual Diluted EPS (from 10-K filings):")
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

@mcp.tool()
def get_stock_news(ticker: str, max_results: int = 5) -> str:
    """Fetch recent news articles for a stock ticker from Yahoo Finance.
    ticker: stock symbol e.g. AVGO, NVDA, TSLA
    max_results: number of news items to return (default 8)
    """
    try:
        import re
        import yfinance as yf
        from datetime import datetime, timezone

        t = yf.Ticker(ticker.upper())
        news = t.news

        if not news:
            from ddgs import DDGS
            results = []
            query = (f"{ticker} stock news site:cnbc.com OR site:finance.yahoo.com "
                     f"OR site:marketwatch.com OR site:reuters.com OR site:bloomberg.com")
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append(
                        f"Title: {r['title']}\n"
                        f"Source: {r['href']}\n"
                        f"Summary: {r['body']}\n"
                    )
            return (f"=== {ticker.upper()} News (web search fallback) ===\n\n" +
                    "\n---\n".join(results) if results else "No news found.")

        lines = [f"=== {ticker.upper()} News (Yahoo Finance) ===\n"]
        count = 0
        for item in news:
            if count >= max_results:
                break
            if not item:
                continue
            content = item.get('content', item)
            if not content or not isinstance(content, dict):
                continue
            title = content.get('title', 'No title')
            publisher = content.get('provider', {}).get('displayName', 'Unknown')
            pub_date = content.get('pubDate', 'Unknown date')
            canonical = content.get('canonicalUrl') or {}
            clickthrough = content.get('clickThroughUrl') or {}
            url = canonical.get('url', '') or clickthrough.get('url', '')
            summary = content.get('summary', content.get('description', ''))
            summary = re.sub(r'<[^>]+>', '', summary)[:200]
            lines.append(
                f"{count+1}. [{publisher}] {title}\n"
                f"   Published: {pub_date}\n"
                f"   URL: {url}\n"
                f"   Summary: {summary}\n"
            )
            count += 1
        return "\n".join(lines) if len(lines) > 1 else f"No readable news found for {ticker.upper()}."

    except Exception as e:
        return f"Error fetching news for {ticker}: {str(e)}"

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

        # Calculate month-over-month change
        mom_change = ""
        if shares_short and prior_month and prior_month > 0:
            chg = ((shares_short - prior_month) / prior_month) * 100
            arrow = "↑" if chg > 0 else "↓"
            mom_change = f"{arrow} {abs(chg):.1f}% vs prior month"

        lines = [f"=== {ticker.upper()} Short Interest ===\n"]
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
        

@mcp.tool()
def get_legal_proceedings(ticker: str) -> str:
    """Fetch legal proceedings and litigation disclosures from SEC 10-K filing.
    Returns material lawsuits, regulatory actions, DOJ/FTC/EC cases, class
    actions, and other legal risks companies are required to disclose.
    Searches both Item 3 and Note 10/Legal Matters in financial statements.
    Note: extraction may fail for combined filings, foreign filers (20-F),
    or companies with minimal legal disclosure. Review the SEC link directly.
    ticker: stock symbol e.g. GOOG, META, MSFT, AAPL, CRM, TSLA
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

        tenk_accn  = None
        tenk_date  = None
        form_type  = None
        for i, form in enumerate(forms):
            if form in ('10-K', '20-F'):    # base filings only, skip /A
                tenk_accn = accns[i]
                tenk_date = dates[i]
                form_type = form
                break

        if not tenk_accn:
            return f"No 10-K filing found for {ticker.upper()}"

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
            if re.search(r'x?ex\d|exhibit|exh\d', name_lower):
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

        def extract_from(start_pos, chunk_size=25000, min_len=100):
            chunk = clean[start_pos:start_pos + chunk_size]
            end_pos = len(chunk)
            for marker in end_markers:
                # Use 2000 char minimum to avoid false early triggers
                p = chunk.find(marker, 2000)
                if p != -1 and p < end_pos:
                    end_pos = p
            result = chunk[:end_pos].strip()
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
        for search_from_pct in [0.50, 0.35, 0.60, 0.75]:
            threshold = int(doc_len * search_from_pct)
            for phrase in anchor_phrases:
                pos = clean.lower().find(phrase.lower(), threshold)
                if pos != -1:
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
        if not legal_text:
            note_cc_re = re.compile(
                r'Note\s+\d+[\.\s:\u2013\u2014,\-\"\u201c\u201d]+'
                r'Commitments\s+and\s+Contingencies',
                re.IGNORECASE)
            for threshold_pct in [0.25, 0.10]:
                threshold = int(doc_len * threshold_pct)
                m = note_cc_re.search(clean, threshold)
                if m:
                    candidate = extract_from(m.start(), 25000)
                    if len(candidate) > 500:
                        legal_text = candidate
                        break

        # Strategy B: Find 'Legal Matters' in final 30% of document
        if not legal_text:
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

        # Strategy D: Last resort — broad litigation keywords.
        # Also catches genuinely minimal disclosures (e.g. AMD) by using
        # a lower min_len=100 threshold — returning the short boilerplate
        # rather than a NOT_EXTRACTED failure message.
        if not legal_text:
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

        if not legal_text:
            return (
                f"=== {company_name} ({ticker.upper()}) ===\n"
                f"10-K filed: {tenk_date}\n\n"
                f"Legal proceedings section could not be extracted.\n"
                f"This may indicate minimal disclosed legal exposure\n"
                f"or an unusual filing format.\n\n"
                f"Review directly:\n{doc_url}"
            )

        # Step 5: Final cleanup and truncation
        legal_text = re.sub(r'\s+', ' ', legal_text).strip()
        if len(legal_text) > 25000:
            legal_text = (legal_text[:25000]
                          + "\n\n[Truncated — full text in SEC filing]")

        lines = [
            f"=== {company_name} ({ticker.upper()}) ===",
            f"Legal Proceedings — 10-K filed {tenk_date}",
            f"Source: {doc_filename}\n",
            legal_text,
            f"\nFull filing: {doc_url}",
            f"SEC EDGAR: https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik}&type=10-K"
        ]

        return "\n".join(lines)

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
