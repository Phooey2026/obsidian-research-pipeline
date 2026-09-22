"""
mercurymcp - MCP server for Currencies, Cryptocurrencies & Commodities data
Covers the Three C's (CCC) for Mercury, Obsidian Capital's CCC analyst.

Companion to webmcp (app.py) and macromcp (app2.py). Run on port 8645.
Feeds weekly_mercury.py to produce:
  - mercury_summary_YYYYMMDD.md   (human-readable weekly CCC report)
  - mercury_backdrop_YYYYMMDD.yaml (structured AI context file)

Tools (16 total):
  get_forex_rates          — 10 currency pairs + DXY
  get_central_bank_rates   — 8 central bank policy rates
  get_money_supply         — M2/M3/M4 for USD/EUR/JPY/GBP
  get_crypto_prices        — BTC, ETH via CoinGecko
  get_crypto_news          — SearXNG crypto news
  get_etf_data             — 17 ETFs: commodities, real estate, fixed income
  get_energy_prices        — WTI, Brent, NatGas, Gasoline, HeatingOil
  get_metals_prices        — Gold, Silver, Copper, Platinum, Palladium, etc.
  get_agricultural_prices  — Corn, Soybeans, Wheat, Cotton, Sugar, Coffee, etc.
  get_livestock_prices     — Live Cattle, Lean Hogs, Feeder Cattle
  get_wasde_summary        — USDA WASDE monthly report
  get_crop_progress        — USDA NASS crop progress
  get_noaa_drought         — NOAA drought monitor
  get_cot_report           — CFTC Commitment of Traders
  get_baltic_dry           — Baltic Dry Index via SearXNG
  build_mercury_backdrop   — Structured YAML for cross-agent injection

Data sources:
  - FRED (Federal Reserve Bank of St. Louis) — free, FRED_API_KEY optional
  - Frankfurter API (ECB) — free, no key required
  - CoinGecko API — free tier, no key required (10K calls/month)
  - yfinance — ETF price/return data
  - EIA (Energy Information Administration) — free, EIA_API_KEY required
    (register free at https://www.eia.gov/opendata/register.php)
  - USDA NASS — free, USDA_API_KEY required
    (register free at https://quickstats.nass.usda.gov/api)
  - USDA FAS — free, no key for WASDE JSON endpoint
  - NOAA CPC — free, no key required
  - CFTC — free, no key required (Socrata open data)
  - SearXNG — local instance on port 8080
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware

# ============================================================================
# Configuration
# ============================================================================

logger = logging.getLogger(__name__)


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

FRED_API_KEY  = os.environ.get("FRED_API_KEY", "")
EIA_API_KEY   = os.environ.get("EIA_API_KEY", "")    # register free at eia.gov
USDA_API_KEY  = os.environ.get("USDA_API_KEY", "")   # register free at nass.usda.gov
SEARXNG_URL   = os.environ.get("SEARXNG_URL", "http://localhost:8080")

FRED_BASE      = "https://api.stlouisfed.org/fred/series/observations"
FRANKFURTER    = "https://api.frankfurter.dev/v1"
COINGECKO      = "https://api.coingecko.com/api/v3"
EIA_BASE       = "https://api.eia.gov/v2"
USDA_NASS      = "https://quickstats.nass.usda.gov/api/api_GET/"
USDA_WASDE     = "https://apps.fas.usda.gov/psdonline/app/index.html#/app/downloads"
USDA_WASDE_API = "https://apps.fas.usda.gov/psdonline/api/psd/file"
CFTC_BASE      = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"
NOAA_DROUGHT   = "https://droughtmonitor.unl.edu/DmData/GISData.aspx"

# ============================================================================
# Internal helpers
# ============================================================================


async def _fred_series(series_id: str, limit: int = 12,
                        frequency: str = "") -> list[dict]:
    """Fetch the most recent `limit` observations for a FRED series."""
    params: dict = {
        "series_id":  series_id,
        "sort_order": "desc",
        "limit":      limit,
        "file_type":  "json",
        "api_key":    FRED_API_KEY if FRED_API_KEY else "FRED_ANONYMOUS",
    }
    if frequency:
        params["frequency"] = frequency

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(FRED_BASE, params=params)
        r.raise_for_status()
        data = r.json()

    observations = data.get("observations", [])
    result = []
    for obs in observations:
        val_str = obs.get("value", ".")
        try:
            val = float(val_str)
        except (ValueError, TypeError):
            val = None
        result.append({"date": obs["date"], "value": val})
    return list(reversed(result))


def _latest(obs_list: list[dict]) -> tuple[Optional[float], str]:
    """Return (most_recent_value, date_str) from a FRED observation list."""
    for obs in reversed(obs_list):
        if obs["value"] is not None:
            return obs["value"], obs["date"]
    return None, "N/A"


def _pct_change(obs_list: list[dict], periods: int = 1) -> Optional[float]:
    """Compute pct change between the last and `periods`-ago observation."""
    vals = [o["value"] for o in obs_list if o["value"] is not None]
    if len(vals) < periods + 1:
        return None
    return ((vals[-1] - vals[-(periods + 1)]) / vals[-(periods + 1)]) * 100


# ============================================================================
# MCP Server
# ============================================================================

mcp = FastMCP(
    "mercurymcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


# ── Forex & Currencies ────────────────────────────────────────────────────────

@mcp.tool()
async def get_forex_rates() -> str:
    """
    Fetch current exchange rates for the top 10 currency pairs tracked by
    Mercury, plus the DXY Dollar Index from FRED.

    Pairs: EUR/USD, USD/JPY, GBP/USD, USD/CHF, AUD/USD, USD/CAD,
           NZD/USD, USD/CNY, USD/MXN, USD/BRL

    Sources: Frankfurter API (ECB reference rates), FRED (DXY)
    """
    lines = ["=== FOREX RATES ===\n"]

    # ── ECB rates via Frankfurter (base USD, latest) ──────────────────────
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(f"{FRANKFURTER}/latest", params={"from": "USD"})
            r.raise_for_status()
            data = r.json()

        date_str = data.get("date", "N/A")
        rates    = data.get("rates", {})

        # Frankfurter returns USD → X; we want X/USD or USD/X conventions
        pairs = [
            ("EUR/USD", "EUR", True),    # True = invert (EUR is base)
            ("USD/JPY", "JPY", False),
            ("GBP/USD", "GBP", True),
            ("USD/CHF", "CHF", False),
            ("AUD/USD", "AUD", True),
            ("USD/CAD", "CAD", False),
            ("NZD/USD", "NZD", True),
            ("USD/CNY", "CNY", False),
            ("USD/MXN", "MXN", False),
            ("USD/BRL", "BRL", False),
        ]

        lines.append(f"ECB Reference Rates  [{date_str}]")
        lines.append(f"{'Pair':<12} {'Rate':>10}")
        lines.append("-" * 25)

        for label, code, invert in pairs:
            raw = rates.get(code)
            if raw is None:
                lines.append(f"{label:<12} {'N/A':>10}")
                continue
            rate = (1 / raw) if invert else raw
            lines.append(f"{label:<12} {rate:>10.4f}")

    except Exception as e:
        lines.append(f"Frankfurter API error: {e}")

    # ── DXY (Broad Dollar Index) from FRED ───────────────────────────────
    try:
        dxy = await _fred_series("DTWEXBGS", limit=30)
        dxy_val, dxy_date = _latest(dxy)

        lines.append(f"\nDXY Broad Dollar Index:  {dxy_val:.3f}  [{dxy_date}]")

        # 4-week and 13-week trend
        vals = [o["value"] for o in dxy if o["value"] is not None]
        if len(vals) >= 5:
            chg_4w = ((vals[-1] - vals[-5]) / vals[-5]) * 100
            direction = "↑ strengthening" if chg_4w > 0 else "↓ weakening"
            lines.append(f"  4-week change:         {chg_4w:+.2f}%  ({direction})")
        if len(vals) >= 14:
            chg_13w = ((vals[-1] - vals[-14]) / vals[-14]) * 100
            lines.append(f"  13-week change:        {chg_13w:+.2f}%")

        lines.append("\nDXY recent history (last 10 obs):")
        for obs in dxy[-10:]:
            if obs["value"] is not None:
                lines.append(f"  {obs['date']}  {obs['value']:.3f}")

        lines.append("\nNote: Rising DXY = USD strengthening = headwind for commodities & EM FX.")
        lines.append("Source: Frankfurter/ECB, FRED DTWEXBGS")

    except Exception as e:
        lines.append(f"\nDXY fetch error: {e}")

    return "\n".join(lines)


@mcp.tool()
async def get_central_bank_rates() -> str:
    """
    Fetch policy interest rates for the major central banks:
    Federal Reserve (USD), European Central Bank (EUR), Bank of Japan (JPY),
    Bank of England (GBP), Bank of Canada (CAD), Reserve Bank of Australia (AUD),
    Swiss National Bank (CHF).

    Policy divergence between central banks is the primary driver of
    medium-term currency trends.

    Source: FRED (multiple series)
    """
    lines = ["=== CENTRAL BANK POLICY RATES ===\n"]

    cb_series = [
        ("Federal Reserve (USD)",          "FEDFUNDS",    "m"),
        ("European Central Bank (EUR)",    "ECBDFR",      "m"),
        ("Bank of Japan (JPY)",            "IRSTCB01JPM156N", "m"),
        ("Bank of England (GBP)",          "IR3TIB01GBM156N", "m"),  # IR3TIB01 series — BIS-sourced, lags ~1 quarter; BoJ/BoC may show stale values
        ("Bank of Canada (CAD)",           "IRSTCB01CAM156N", "m"),
        ("Reserve Bank of Australia (AUD)","IR3TIB01AUM156N", "m"),
        ("Swiss National Bank (CHF)",      "IR3TIB01CHM156N", "m"),
        ("People's Bank of China (CNY)",   "IRSTCB01CNM156N", "m"),
    ]

    rates = {}
    for label, series_id, freq in cb_series:
        try:
            obs = await _fred_series(series_id, limit=6, frequency=freq)
            val, date = _latest(obs)
            rates[label] = (val, date)
            if val is not None:
                lines.append(f"  {label:<40} {val:.2f}%  [{date}]")
            else:
                lines.append(f"  {label:<40} N/A")
        except Exception as e:
            lines.append(f"  {label:<40} Error: {e}")

    # Policy divergence commentary
    lines.append("\nPolicy Divergence Analysis:")
    fed   = rates.get("Federal Reserve (USD)",    (None, ""))[0]
    ecb   = rates.get("European Central Bank (EUR)", (None, ""))[0]
    boj   = rates.get("Bank of Japan (JPY)",      (None, ""))[0]
    boe   = rates.get("Bank of England (GBP)",    (None, ""))[0]

    if fed is not None and ecb is not None:
        spread = fed - ecb
        direction = "USD bullish vs EUR" if spread > 0 else "EUR bullish vs USD"
        lines.append(f"  Fed vs ECB spread:      {spread:+.2f}%  → {direction}")
    if fed is not None and boj is not None:
        spread = fed - boj
        lines.append(f"  Fed vs BoJ spread:      {spread:+.2f}%  → carry trade favors USD/JPY longs")
    if fed is not None and boe is not None:
        spread = fed - boe
        direction = "USD bullish vs GBP" if spread > 0 else "GBP bullish vs USD"
        lines.append(f"  Fed vs BoE spread:      {spread:+.2f}%  → {direction}")

    lines.append("\nSource: FRED — central bank policy rate series")
    return "\n".join(lines)


@mcp.tool()
async def get_money_supply() -> str:
    """
    Fetch M2 money supply data for USD, EUR, JPY, GBP and the UK.
    Rapid M2 expansion is historically associated with currency debasement
    and commodity price inflation. Contracting M2 is disinflationary.

    Source: FRED (M2SL, MABMM301EZM189S, MABMM301JPM189S, MABMM301GBM189S)
    """
    lines = ["=== MONEY SUPPLY (M2) ===\n"]

    m2_series = [
        ("US M2 (USD)",       "M2SL",              "m", "T", 1000),
        ("Eurozone M3 (EUR)", "MABMM301EZM189S",   "m", "T", 1e9),
        ("Japan M2 (JPY)",    "MABMM301JPM189S",   "m", "T", 1e9),
        ("UK M4 (GBP)",       "MABMM301GBM189S",   "m", "T", 1e9),
    ]

    for label, series_id, freq, unit, divisor in m2_series:
        try:
            obs = await _fred_series(series_id, limit=15, frequency=freq)
            val, date = _latest(obs)
            yoy = _pct_change(obs, 12)
            mom = _pct_change(obs, 1)
            if val is not None:
                display = val / divisor if divisor > 1 else val
                lines.append(f"{label}")
                lines.append(f"  Level:        {display:.2f}{unit}  [{date}]")
                if mom is not None:
                    lines.append(f"  MoM change:   {mom:+.2f}%")
                if yoy is not None:
                    lines.append(f"  YoY change:   {yoy:+.2f}%")
                    if yoy > 8:
                        lines.append(f"  ⚠ Rapid expansion — potential currency debasement signal")
                    elif yoy < -2:
                        lines.append(f"  ⚠ Contracting M2 — disinflationary / deflationary signal")
                lines.append("")
        except Exception as e:
            lines.append(f"{label}: Error — {e}\n")

    lines.append("Source: FRED — Federal Reserve and international monetary aggregates")
    return "\n".join(lines)


# ── Cryptocurrency ─────────────────────────────────────────────────────────────

@mcp.tool()
async def get_crypto_prices() -> str:
    """
    Fetch current prices, market caps, 24h/7d/30d changes, and volume for
    BTC and ETH via CoinGecko free API.

    Also fetches price data for crypto ETFs (BITQ, FBTC, IBIT) via yfinance
    fallback — these are equity-listed instruments and trade like stocks.

    Source: CoinGecko (free, no key), yfinance for ETFs
    """
    lines = ["=== CRYPTOCURRENCY PRICES ===\n"]

    # ── BTC and ETH via CoinGecko ─────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{COINGECKO}/coins/markets",
                params={
                    "vs_currency":           "usd",
                    "ids":                   "bitcoin,ethereum",
                    "order":                 "market_cap_desc",
                    "per_page":              "10",
                    "page":                  "1",
                    "sparkline":             "false",
                    "price_change_percentage": "24h,7d,30d",
                },
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            coins = r.json()

        for coin in coins:
            name     = coin.get("name", "N/A")
            symbol   = coin.get("symbol", "").upper()
            price    = coin.get("current_price")
            mcap     = coin.get("market_cap")
            vol_24h  = coin.get("total_volume")
            chg_24h  = coin.get("price_change_percentage_24h")
            chg_7d   = coin.get("price_change_percentage_7d_in_currency")
            chg_30d  = coin.get("price_change_percentage_30d_in_currency")
            high_24h = coin.get("high_24h")
            low_24h  = coin.get("low_24h")
            ath      = coin.get("ath")
            ath_chg  = coin.get("ath_change_percentage")

            lines.append(f"── {name} ({symbol}) ──")
            if price:
                lines.append(f"  Price:              ${price:>12,.2f}")
            if high_24h and low_24h:
                lines.append(f"  24h Range:          ${low_24h:,.2f} – ${high_24h:,.2f}")
            if chg_24h is not None:
                arrow = "↑" if chg_24h >= 0 else "↓"
                lines.append(f"  24h Change:         {arrow} {chg_24h:+.2f}%")
            if chg_7d is not None:
                arrow = "↑" if chg_7d >= 0 else "↓"
                lines.append(f"  7d Change:          {arrow} {chg_7d:+.2f}%")
            if chg_30d is not None:
                arrow = "↑" if chg_30d >= 0 else "↓"
                lines.append(f"  30d Change:         {arrow} {chg_30d:+.2f}%")
            if mcap:
                lines.append(f"  Market Cap:         ${mcap/1e9:.2f}B")
            if vol_24h:
                lines.append(f"  24h Volume:         ${vol_24h/1e9:.2f}B")
            if ath and ath_chg is not None:
                lines.append(f"  ATH:                ${ath:,.2f}  ({ath_chg:+.1f}% from ATH)")
            lines.append("")

    except Exception as e:
        lines.append(f"CoinGecko error: {e}\n")

    lines.append("Source: CoinGecko (BTC/ETH)")
    lines.append("Note: Crypto ETFs (BITQ, FBTC, IBIT) are now covered by get_etf_data tool.")
    return "\n".join(lines)


@mcp.tool()
async def get_crypto_news() -> str:
    """
    Search for recent cryptocurrency news and market intelligence via
    SearXNG (local instance). Targets The Block, Decrypt, CoinDesk,
    CoinTelegraph, and general crypto market news.

    Source: SearXNG (local, port 8080)
    """
    lines = ["=== CRYPTOCURRENCY NEWS & INTELLIGENCE ===\n"]

    queries = [
        ("Bitcoin market outlook",          "bitcoin BTC market trend week"),
        ("Ethereum developments",           "ethereum ETH price market"),
        ("Crypto regulatory news",          "cryptocurrency regulation SEC ETF"),
        ("Crypto market sentiment",         "crypto market sentiment institutional"),
    ]

    async with httpx.AsyncClient(timeout=20) as client:
        for section, query in queries:
            lines.append(f"── {section} ──")
            try:
                r = await client.get(
                    f"{SEARXNG_URL}/search",
                    params={
                        "q":       query,
                        "format":  "json",
                        "time_range": "week",
                    },
                )
                r.raise_for_status()
                results = r.json().get("results", [])[:4]
                if not results:
                    lines.append("  No results found.")
                else:
                    for item in results:
                        title   = item.get("title", "No title")
                        url     = item.get("url", "")
                        content = item.get("content", "")[:180]
                        lines.append(f"  • {title}")
                        if content:
                            lines.append(f"    {content}")
                        if url:
                            lines.append(f"    {url}")
                lines.append("")
            except Exception as e:
                lines.append(f"  SearXNG error: {e}\n")

    lines.append("Source: SearXNG (local) — web search aggregator")
    return "\n".join(lines)


# ── ETF Data ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_etf_data(tickers: list[str] = None) -> str:
    """
    Fetch price, performance, AUM, expense ratio, and 52-week range data
    for Mercury's ETF watchlist via yfinance.

    Covers the full ETF universe tracked by Mercury:
      Held positions:  IAU (gold), BITQ (crypto), VDE (energy)
      Spot Bitcoin:    FBTC, IBIT
      Commodities:     GLD, SLV, PDBC, DBA, USO, UNG, CPER
      Real Estate:     VNQ, IYR, XLRE
      Fixed Income:    TLT, HYG

    If tickers is None or empty, fetches all 17 ETFs.
    Returns NAV/price, 1d/1m/3m/1y returns, AUM, expense ratio,
    52-week high/low, volume, and premium/discount to NAV where available.

    Source: yfinance
    """
    DEFAULT_ETFS = [
        "IAU", "BITQ", "VDE",
        "FBTC", "IBIT",
        "GLD", "SLV", "PDBC", "DBA", "USO", "UNG", "CPER",
        "VNQ", "IYR", "XLRE",
        "TLT", "HYG",
    ]

    ETF_DESCRIPTIONS = {
        "IAU":  "iShares Gold Trust (HELD)",
        "BITQ": "Bitwise Crypto Industry ETF (HELD)",
        "VDE":  "Vanguard Energy ETF (HELD)",
        "FBTC": "Fidelity Wise Origin Bitcoin Fund (spot BTC)",
        "IBIT": "iShares Bitcoin Trust (spot BTC)",
        "GLD":  "SPDR Gold Shares",
        "SLV":  "iShares Silver Trust",
        "PDBC": "Invesco Diversified Commodity Strategy (no K-1)",
        "DBA":  "Invesco Agriculture Fund",
        "USO":  "US Oil Fund (WTI proxy)",
        "UNG":  "US Natural Gas Fund",
        "CPER": "US Copper Index Fund",
        "VNQ":  "Vanguard Real Estate ETF (broad REIT)",
        "IYR":  "iShares US Real Estate ETF",
        "XLRE": "Real Estate Select Sector SPDR",
        "TLT":  "iShares 20+ Year Treasury Bond ETF",
        "HYG":  "iShares High Yield Corporate Bond ETF",
    }

    symbols = tickers if tickers else DEFAULT_ETFS
    lines = ["=== ETF DATA (Mercury Watchlist) ===\n"]

    try:
        import yfinance as yf
    except ImportError:
        return "ERROR: yfinance not installed — ETF data unavailable"

    for symbol in symbols:
        desc = ETF_DESCRIPTIONS.get(symbol, symbol)
        lines.append(f"── {symbol} — {desc} ──")
        try:
            t    = yf.Ticker(symbol)
            info = t.info

            # Price / NAV
            price    = info.get("navPrice") or info.get("currentPrice") or info.get("regularMarketPrice")
            prev     = info.get("previousClose")
            high52   = info.get("fiftyTwoWeekHigh")
            low52    = info.get("fiftyTwoWeekLow")
            volume   = info.get("volume") or info.get("averageVolume")
            aum      = info.get("totalAssets")
            expense  = info.get("annualReportExpenseRatio") or info.get("expenseRatio")
            category = info.get("category") or info.get("fundFamily", "")

            # Period returns via history
            hist_1m  = t.history(period="1mo")
            hist_3m  = t.history(period="3mo")
            hist_1y  = t.history(period="1y")

            def period_return(hist):
                if hist is not None and not hist.empty and len(hist) > 1:
                    return ((hist["Close"].iloc[-1] - hist["Close"].iloc[0])
                            / hist["Close"].iloc[0]) * 100
                return None

            # 1-day change — derived from the same daily history series used
            # for 1m/3m/1y returns, not from .info's price/previousClose
            # fields. Those two fields aren't guaranteed to share a price
            # basis (price falls back through navPrice/currentPrice/
            # regularMarketPrice; previousClose is always regular-market) —
            # for spot BTC ETFs, where NAV and market price commonly diverge
            # intraday, that mismatch produced spurious large "1-day changes"
            # that didn't reflect any real move in the underlying asset.
            chg_1d = None
            if hist_1m is not None and not hist_1m.empty:
                _closes_1d = hist_1m["Close"].dropna()
                if len(_closes_1d) > 1:
                    chg_1d = ((_closes_1d.iloc[-1] - _closes_1d.iloc[-2])
                              / _closes_1d.iloc[-2]) * 100

            chg_1m = period_return(hist_1m)
            chg_3m = period_return(hist_3m)
            chg_1y = period_return(hist_1y)

            # 52-week position
            pct_from_52w_high = ((price - high52) / high52 * 100) if price and high52 else None

            if price:
                lines.append(f"  Price/NAV:      ${price:.2f}")
            if chg_1d is not None:
                lines.append(f"  1d Change:      {chg_1d:+.2f}%")
            if chg_1m is not None:
                lines.append(f"  1m Return:      {chg_1m:+.2f}%")
            if chg_3m is not None:
                lines.append(f"  3m Return:      {chg_3m:+.2f}%")
            if chg_1y is not None:
                lines.append(f"  1y Return:      {chg_1y:+.2f}%")
            if high52 and low52:
                lines.append(f"  52w Range:      ${low52:.2f} – ${high52:.2f}")
            if pct_from_52w_high is not None:
                lines.append(f"  vs 52w High:    {pct_from_52w_high:+.2f}%")
            if aum:
                lines.append(f"  AUM:            ${aum/1e9:.2f}B")
            if expense:
                lines.append(f"  Expense Ratio:  {expense*100:.2f}%")
            if volume:
                lines.append(f"  Volume:         {volume:,.0f}")
            if category:
                lines.append(f"  Category:       {category}")

        except Exception as e:
            lines.append(f"  Error fetching {symbol}: {e}")

        lines.append("")

    lines.append("Source: yfinance")
    lines.append("Note: IAU, BITQ, VDE are current portfolio holdings.")
    lines.append("Note: BITQ/FBTC/IBIT crypto ETF data previously in get_crypto_prices — now consolidated here.")
    return "\n".join(lines)


# ── Energy ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_energy_prices() -> str:
    """
    Fetch energy commodity prices and supply data:
    WTI Crude, Brent Crude, Natural Gas (Henry Hub), Gasoline (RBOB),
    Heating Oil, and US crude inventory levels.

    Source: EIA open data API (free, requires EIA_API_KEY in .env)
    Fallback: FRED series for key prices if EIA key not available.
    """
    lines = ["=== ENERGY PRICES ===\n"]

    # ── EIA API v2 route-based calls ────────────────────────────────────
    if EIA_API_KEY:
        # EIA v2 uses category routes, not /seriesid/
        # Each route has its own facet filter for the series
        eia_routes = [
            ("WTI Crude Oil ($/bbl)",          f"{EIA_BASE}/petroleum/pri/spt/data/",           "RWTC"),
            ("Brent Crude Oil ($/bbl)",         f"{EIA_BASE}/petroleum/pri/spt/data/",           "RBRTE"),
            ("Henry Hub Nat Gas ($/MMBtu)",     f"{EIA_BASE}/natural-gas/pri/fut/data/",          "RNGWHHD"),
            ("RBOB Gasoline ($/gal)",           f"{EIA_BASE}/petroleum/pri/gnd/dcus/nus/w/data/","EMM_EPMRU_PTE_NUS_DPG"),
            ("Heating Oil No.2 ($/gal)",        f"{EIA_BASE}/petroleum/pri/gnd/dcus/nus/w/data/","EMM_EPD2F_PTE_NUS_DPG"),
        ]
        async with httpx.AsyncClient(timeout=30) as client:
            for label, route, series_id in eia_routes:
                try:
                    r = await client.get(
                        route,
                        params={
                            "api_key":              EIA_API_KEY,
                            "data[]":               "value",
                            "facets[series][]":     series_id,
                            "sort[0][column]":      "period",
                            "sort[0][direction]":   "desc",
                            "length":               "8",
                        },
                    )
                    r.raise_for_status()
                    series_data = r.json().get("response", {}).get("data", [])
                    if series_data:
                        latest = series_data[0]
                        val    = latest.get("value")
                        period = latest.get("period", "N/A")
                        lines.append(f"{label:<45} {val}  [{period}]")
                        vals = [float(d["value"]) for d in series_data
                                if d.get("value") not in (None, "")]
                        if len(vals) >= 2:
                            chg_4w = ((vals[0] - vals[min(4, len(vals)-1)])
                                      / vals[min(4, len(vals)-1)]) * 100
                            lines.append(f"  {'4-week change:':<43} {chg_4w:+.2f}%")
                    else:
                        lines.append(f"{label:<45} N/A (no data returned)")
                except Exception as e:
                    lines.append(f"{label:<45} EIA error: {e}")

        # ── RBOB/Heating Oil FRED fallback if EIA failed ─────────────────
        fred_fallback = [
            ("RBOB Gasoline ($/gal)",    "GASREGCOVW",  8, ""),
            ("Heating Oil No.2 ($/gal)", "DHOILNYH",    8, ""),
        ]
        for label, series_id, limit, freq in fred_fallback:
            # Fetch if the label doesn't appear with a dollar value
            # (EIA error lines contain the label but no price)
            label_has_price = any(
                label in ln and "$" in ln
                for ln in lines
            )
            if not label_has_price:
                try:
                    obs = await _fred_series(series_id, limit=limit, frequency=freq)
                    val, date = _latest(obs)
                    if val is not None:
                        lines.append(f"{label:<45} ${val:.3f}  [{date}] (FRED fallback)")
                except Exception:
                    pass

        # ── US Crude Inventory (EIA weekly) ──────────────────────────────
        # Facets are required: without them, consecutive rows in the
        # response can be entirely different series (e.g. "Ending Stocks
        # Excluding SPR" vs. "Stocks in Transit from Alaska" vs. "SPR
        # stocks") rather than the same measurement two weeks apart —
        # confirmed via direct API testing, which produced an impossible
        # "weekly change" by diffing two unrelated series. duoarea=NUS +
        # product=EPC0 narrows to "US crude oil" but several distinct
        # process series still share that scope; process=SAX pins to
        # exactly WCESTUS1 (Ending Stocks Excluding SPR) — the standard
        # weekly commercial crude inventory figure.
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(
                    f"{EIA_BASE}/petroleum/stoc/wstk/data/",
                    params={
                        "api_key": EIA_API_KEY,
                        "data[]":  "value",
                        "facets[duoarea][]": "NUS",
                        "facets[product][]": "EPC0",
                        "facets[process][]": "SAX",
                        "sort[0][column]": "period",
                        "sort[0][direction]": "desc",
                        "length": "5",
                    },
                )
                r.raise_for_status()
                inv_data = r.json().get("response", {}).get("data", [])
                if inv_data:
                    inv_latest = inv_data[0]
                    inv_val    = inv_latest.get("value")
                    inv_period = inv_latest.get("period", "N/A")
                    lines.append(f"\nUS Crude Inventories (Mbbl, ex-SPR):      {inv_val}  [{inv_period}]")
                    if len(inv_data) >= 2:
                        prev_inv = float(inv_data[1].get("value", 0) or 0)
                        curr_inv = float(inv_val or 0)
                        draw = curr_inv - prev_inv
                        signal = "↓ draw (bullish)" if draw < 0 else "↑ build (bearish)"
                        lines.append(f"  Weekly change (Mbbl):                   {draw:+.1f}  {signal}")
        except Exception as e:
            lines.append(f"\nCrude inventory error: {e}")

    else:
        # ── FRED fallback (no EIA key) ────────────────────────────────────
        lines.append("EIA_API_KEY not set — using FRED fallback (weekly data)\n")
        fred_energy = [
            ("WTI Crude Oil ($/bbl)",          "DCOILWTICO", 12, ""),
            ("Brent Crude Oil ($/bbl)",         "DCOILBRENTEU", 12, ""),
            ("Henry Hub Natural Gas ($/MMBtu)", "DHHNGSP",    12, ""),
            ("Kerosene-Jet Fuel ($/gal)",       "DJFUELUSGULF", 12, ""),
        ]
        for label, series_id, limit, freq in fred_energy:
            try:
                obs = await _fred_series(series_id, limit=limit, frequency=freq)
                val, date = _latest(obs)
                chg_4w = _pct_change(obs, min(4, len([o for o in obs if o["value"] is not None]) - 1))
                if val is not None:
                    lines.append(f"{label:<45} ${val:.2f}  [{date}]")
                    if chg_4w is not None:
                        lines.append(f"  4-week change:                          {chg_4w:+.2f}%")
                else:
                    lines.append(f"{label:<45} N/A")
            except Exception as e:
                lines.append(f"{label}: FRED error — {e}")

    lines.append("\nSource: EIA open data API (or FRED fallback)")
    lines.append("")

    # ── Energy Sector ETF (VDE) via yfinance ───────────────────────────────
    try:
        import yfinance as yf
        vde = yf.Ticker("VDE")
        vde_info = vde.info
        vde_hist = vde.history(period="1mo")
        vde_price = vde_info.get("currentPrice") or vde_info.get("regularMarketPrice")
        vde_prev = vde_info.get("previousClose")
        if vde_price:
            vde_chg_1d = ((vde_price - vde_prev) / vde_prev * 100) if vde_prev else None
            vde_30d = None
            if not vde_hist.empty and len(vde_hist) > 1:
                vde_30d = ((vde_hist["Close"].iloc[-1] - vde_hist["Close"].iloc[0])
                           / vde_hist["Close"].iloc[0]) * 100
            lines.append("── Energy Sector ETF (VDE) ──")
            lines.append(f"  Price:  ${vde_price:.2f}")
            if vde_chg_1d is not None:
                lines.append(f"  1d:     {vde_chg_1d:+.2f}%")
            if vde_30d is not None:
                lines.append(f"  30d:    {vde_30d:+.2f}%")
            high52 = vde_info.get("fiftyTwoWeekHigh")
            low52  = vde_info.get("fiftyTwoWeekLow")
            if high52 and low52:
                lines.append(f"  52w:    ${low52:.2f}–${high52:.2f}")
    except Exception as e:
        lines.append(f"VDE data error: {e}")

    return "\n".join(lines)


# ── Metals ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_metals_prices() -> str:
    """
    Fetch precious and industrial metals prices:
    Gold, Silver, Copper, Platinum, Palladium, Aluminum, Iron Ore.

    Gold is the primary safe-haven signal. Copper is a global growth proxy.
    The Gold/Silver ratio signals risk appetite (rising = risk-off).
    The Gold/Copper ratio is an alternative recession indicator.

    Source: FRED (gold, silver, copper via London fix and spot series)
    """
    lines = ["=== METALS PRICES ===\n"]

    prices: dict[str, Optional[float]] = {}

    # ── Precious metals via yfinance (LBMA series removed from FRED) ──────
    try:
        import yfinance as yf
        precious = [
            ("Gold (USD/troy oz)",      "GC=F"),
            ("Silver (USD/troy oz)",    "SI=F"),
            ("Platinum (USD/troy oz)",  "PL=F"),
            ("Palladium (USD/troy oz)", "PA=F"),
        ]
        for label, ticker in precious:
            try:
                t    = yf.Ticker(ticker)
                hist = t.history(period="13mo")
                if not hist.empty:
                    closes = hist["Close"].dropna()
                    val    = float(closes.iloc[-1])
                    date   = str(closes.index[-1].date())
                    prices[label] = val
                    lines.append(f"{label:<28} ${val:>10,.2f}  [{date}]")
                    if len(closes) >= 22:
                        mom = ((closes.iloc[-1] - closes.iloc[-22]) / closes.iloc[-22]) * 100
                        lines.append(f"  MoM:  {mom:+.2f}%")
                    if len(closes) >= 252:
                        yoy = ((closes.iloc[-1] - closes.iloc[-252]) / closes.iloc[-252]) * 100
                        lines.append(f"  YoY:  {yoy:+.2f}%")
                    lines.append("")
                else:
                    lines.append(f"{label:<28} N/A (no yfinance data)\n")
            except Exception as e:
                lines.append(f"{label}: yfinance error — {e}\n")
    except ImportError:
        lines.append("yfinance not installed — precious metals unavailable\n")

    # ── Industrial metals via FRED (World Bank Pink Sheet) ────────────────
    industrial_series = [
        ("Copper (USD/mt)",        "PCOPPUSDM",   14, "m"),
        ("Aluminum (USD/mt)",      "PALUMUSDM",   14, "m"),
        ("Iron Ore (USD/dry t)",   "PIORECRUSDM", 14, "m"),
    ]
    for label, series_id, limit, freq in industrial_series:
        try:
            obs  = await _fred_series(series_id, limit=limit, frequency=freq)
            val, date = _latest(obs)
            prices[label] = val
            mom  = _pct_change(obs, 1)
            yoy  = _pct_change(obs, min(12, len([o for o in obs if o["value"] is not None]) - 1))
            if val is not None:
                lines.append(f"{label:<28} ${val:>10,.2f}  [{date}]")
                if mom is not None:
                    lines.append(f"  MoM:  {mom:+.2f}%")
                if yoy is not None:
                    lines.append(f"  YoY:  {yoy:+.2f}%")
                lines.append("")
            else:
                lines.append(f"{label:<28} N/A\n")
        except Exception as e:
            lines.append(f"{label}: FRED error — {e}\n")

    # ── Key ratios ────────────────────────────────────────────────────────
    gold_price = next((v for k, v in prices.items() if "Gold" in k), None)
    silv_price = next((v for k, v in prices.items() if "Silver" in k), None)
    copp_price = next((v for k, v in prices.items() if "Copper" in k), None)

    lines.append("── Key Ratios ──")
    if gold_price and silv_price:
        gs_ratio = gold_price / silv_price
        lines.append(f"Gold/Silver Ratio:    {gs_ratio:.1f}x")
        if gs_ratio > 90:
            lines.append("  ⚠ Elevated ratio (>90) — risk-off / silver underperforming")
        elif gs_ratio < 50:
            lines.append("  ✓ Low ratio (<50) — risk-on / silver outperforming")

    if gold_price and copp_price:
        # Copper in USD/lb → convert gold to per-lb equivalent for ratio
        # Standard: gold oz / copper lb — rising = risk-off
        gc_ratio = gold_price / (copp_price * 100)  # normalize
        lines.append(f"Gold/Copper Ratio:    {gc_ratio:.2f}  (rising = risk-off)")

    # ── Gold ETF (IAU) via yfinance ────────────────────────────────────────
    try:
        import yfinance as yf
        iau = yf.Ticker("IAU")
        iau_info = iau.info
        iau_hist = iau.history(period="1mo")
        iau_price = iau_info.get("currentPrice") or iau_info.get("regularMarketPrice")
        iau_prev = iau_info.get("previousClose")
        if iau_price:
            iau_chg_1d = ((iau_price - iau_prev) / iau_prev * 100) if iau_prev else None
            iau_30d = None
            if not iau_hist.empty and len(iau_hist) > 1:
                iau_30d = ((iau_hist["Close"].iloc[-1] - iau_hist["Close"].iloc[0])
                           / iau_hist["Close"].iloc[0]) * 100
            lines.append(f"\n── Gold ETF (IAU) ──")
            lines.append(f"  Price:  ${iau_price:.2f}")
            if iau_chg_1d is not None:
                lines.append(f"  1d:     {iau_chg_1d:+.2f}%")
            if iau_30d is not None:
                lines.append(f"  30d:    {iau_30d:+.2f}%")
            high52 = iau_info.get("fiftyTwoWeekHigh")
            low52  = iau_info.get("fiftyTwoWeekLow")
            if high52 and low52:
                lines.append(f"  52w:    ${low52:.2f}–${high52:.2f}")
    except Exception as e:
        lines.append(f"IAU data error: {e}")

    lines.append("\nNote: Gold = safe-haven. Copper = global growth proxy (Dr. Copper).")
    lines.append("Source: FRED — London Bullion Market and World Bank commodity series")
    return "\n".join(lines)


# ── Agriculture ───────────────────────────────────────────────────────────────

@mcp.tool()
async def get_agricultural_prices() -> str:
    """
    Fetch agricultural commodity prices: Corn, Soybeans, Wheat, Cotton,
    Sugar, Coffee, Cocoa, Orange Juice — via yfinance daily futures prices
    (primary) with FRED World Bank monthly series as fallback. Rubber and
    Lumber remain FRED-only. USDA NASS quick stats where available.

    Note: FRED's World Bank Pink Sheet series are monthly with a real
    publication lag (often 4-6+ weeks) — genuinely stale by design, not a
    fetch failure. yfinance futures give daily-updated prices for the
    actively-traded commodities instead.

    Source: yfinance (CBOT/ICE futures), FRED (fallback), USDA NASS (if key available)
    """
    lines = ["=== AGRICULTURAL COMMODITY PRICES ===\n"]

    # (label, yfinance ticker, unit divisor, unit label, FRED fallback series, limit, freq)
    # Grains (CBOT) quote in cents/bushel — divide by 100 for $/bu.
    # Softs (ICE) quote in cents/lb — kept as cents/lb, the standard market
    # convention (e.g. "89.14¢/lb" cotton, not "$0.89/lb").
    # Cocoa (ICE) quotes directly in $/metric ton — no conversion.
    futures_series = [
        ("Corn",           "ZC=F", 100, "$/bu",  "PMAIZMTUSDM",   14, "m"),
        ("Soybeans",       "ZS=F", 100, "$/bu",  "PSOYBUSDM",     14, "m"),
        ("Wheat",          "ZW=F", 100, "$/bu",  "PWHEAMTUSDM",   14, "m"),
        ("Cotton",         "CT=F", 1,   "¢/lb",  "PCOTTINDUSDM",  14, "m"),
        ("Sugar",          "SB=F", 1,   "¢/lb",  "PSUGAISAUSDM",  14, "m"),
        ("Coffee Arabica", "KC=F", 1,   "¢/lb",  "PCOFFOTMUSDM",  14, "m"),
        ("Cocoa",          "CC=F", 1,   "$/mt",  "PCOCOUSDM",     14, "m"),
        ("Orange Juice",   "OJ=F", 1,   "¢/lb",  "APU0000713111", 14, "m"),
    ]

    for label, ticker, divisor, unit, fred_id, limit, freq in futures_series:
        _got_yfinance = False
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="3mo")
            if not hist.empty:
                closes = hist["Close"].dropna()
                if len(closes) >= 1:
                    latest_val  = float(closes.iloc[-1]) / divisor
                    latest_date = str(closes.index[-1].date())
                    lines.append(f"{label} ({unit}):".ljust(28) + f"{latest_val:>9.2f}  [{latest_date}]")
                    if len(closes) >= 2:
                        chg_1d = ((closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2]) * 100
                        lines.append(f"  1-day:  {chg_1d:+.2f}%")
                    if len(closes) >= 22:
                        chg_1m = ((closes.iloc[-1] - closes.iloc[-22]) / closes.iloc[-22]) * 100
                        lines.append(f"  ~1mo:   {chg_1m:+.2f}%")
                    lines.append("")
                    _got_yfinance = True
        except Exception:
            pass

        if not _got_yfinance:
            # Fall back to the original FRED World Bank series
            try:
                obs  = await _fred_series(fred_id, limit=limit, frequency=freq)
                val, date = _latest(obs)
                mom  = _pct_change(obs, 1)
                yoy  = _pct_change(obs, min(12, len([o for o in obs if o["value"] is not None]) - 1))
                if val is not None:
                    lines.append(f"{label} (USD/mt, FRED fallback — monthly, may lag):".ljust(28)
                                 + f"${val:>9.2f}  [{date}]")
                    if mom is not None:
                        lines.append(f"  MoM:  {mom:+.2f}%")
                    if yoy is not None:
                        lines.append(f"  YoY:  {yoy:+.2f}%")
                    lines.append("")
                else:
                    lines.append(f"{label:<28} N/A  [{date}]\n")
            except Exception as e:
                lines.append(f"{label}: Error — {e}\n")

    # Rubber and Lumber — no reliable liquid yfinance futures ticker; FRED only
    other_series = [
        ("Rubber (USD/kg)",        "PRUBBUSDM",     14, "m"),
        ("Lumber (USD/1000 bft)",  "WPU081",        14, "m"),
    ]
    for label, series_id, limit, freq in other_series:
        try:
            obs  = await _fred_series(series_id, limit=limit, frequency=freq)
            val, date = _latest(obs)
            mom  = _pct_change(obs, 1)
            yoy  = _pct_change(obs, min(12, len([o for o in obs if o["value"] is not None]) - 1))

            if val is not None:
                lines.append(f"{label:<28} ${val:>9.2f}  [{date}]")
                if mom is not None:
                    lines.append(f"  MoM:  {mom:+.2f}%")
                if yoy is not None:
                    lines.append(f"  YoY:  {yoy:+.2f}%")
                lines.append("")
            else:
                lines.append(f"{label:<28} N/A  [{date}]\n")
        except Exception as e:
            lines.append(f"{label}: Error — {e}\n")

    # ── USDA NASS supplemental (if key available) ─────────────────────────
    if USDA_API_KEY:
        lines.append("── USDA NASS Price Received (latest season) ──")
        nass_queries = [
            ("Corn",     "CORN",     "PRICE RECEIVED"),
            ("Soybeans", "SOYBEANS", "PRICE RECEIVED"),
            ("Wheat",    "WHEAT",    "PRICE RECEIVED"),
        ]
        async with httpx.AsyncClient(timeout=20) as client:
            for crop, commodity, stat in nass_queries:
                try:
                    r = await client.get(
                        USDA_NASS,
                        params={
                            "key":           USDA_API_KEY,
                            "commodity_desc": commodity,
                            "statisticcat_desc": stat,
                            "agg_level_desc": "NATIONAL",
                            "year__GE":      str(datetime.now().year - 1),
                            "format":        "JSON",
                        },
                    )
                    r.raise_for_status()
                    items = r.json().get("data", [])
                    if items:
                        latest_item = sorted(items,
                                             key=lambda x: x.get("year", "0"),
                                             reverse=True)[0]
                        val    = latest_item.get("Value", "N/A")
                        unit   = latest_item.get("unit_desc", "")
                        year   = latest_item.get("year", "")
                        lines.append(f"  {crop:<12} ${val} {unit}  [{year}]")
                except Exception as e:
                    lines.append(f"  {crop}: NASS error — {e}")
        lines.append("")

    lines.append("Source: FRED (World Bank commodity prices), USDA NASS (if key set)")
    return "\n".join(lines)


@mcp.tool()
async def get_livestock_prices() -> str:
    """
    Fetch livestock commodity prices: Live Cattle, Lean Hogs, Feeder Cattle
    — via yfinance daily CME futures prices (primary). Also includes BLS
    retail average prices for beef, pork, chicken, eggs, and milk as
    supplementary consumer-price context (monthly, may lag).

    Source: yfinance (CME futures), FRED (BLS retail series), USDA NASS (if key available)
    """
    lines = ["=== LIVESTOCK PRICES ===\n"]

    # CME livestock futures — cents/lb, kept as-is (standard market
    # convention, e.g. "211.73 ¢/lb" not "$2.1173/lb")
    futures_series = [
        ("Live Cattle",   "LE=F"),
        ("Lean Hogs",     "HE=F"),
        ("Feeder Cattle", "GF=F"),
    ]
    lines.append("── CME Futures (daily) ──")
    for label, ticker in futures_series:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="3mo")
            if not hist.empty:
                closes = hist["Close"].dropna()
                if len(closes) >= 1:
                    latest_val  = float(closes.iloc[-1])
                    latest_date = str(closes.index[-1].date())
                    lines.append(f"{label} (¢/lb):".ljust(28) + f"{latest_val:>9.2f}  [{latest_date}]")
                    if len(closes) >= 2:
                        chg_1d = ((closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2]) * 100
                        lines.append(f"  1-day:  {chg_1d:+.2f}%")
                    if len(closes) >= 22:
                        chg_1m = ((closes.iloc[-1] - closes.iloc[-22]) / closes.iloc[-22]) * 100
                        lines.append(f"  ~1mo:   {chg_1m:+.2f}%")
                    lines.append("")
                else:
                    lines.append(f"{label} ({ticker}): no data\n")
            else:
                lines.append(f"{label} ({ticker}): no data\n")
        except Exception as e:
            lines.append(f"{label}: Error — {e}\n")

    lines.append("── Retail Prices (BLS, monthly — supplementary context) ──")

    # FRED livestock / meat-related series
    livestock_series = [
        ("Beef & Veal (USD/lb)",   "APU0000703112",  14, "m"),
        ("Pork (USD/lb)",          "APU0000FD3101",  14, "m"),
        ("Chicken (USD/lb)",       "APU0000706111",  14, "m"),
        ("Eggs, Grade A (USD/doz)","APU0000708111",  14, "m"),
        ("Milk, whole (USD/gal)",  "APU0000709112",  14, "m"),
    ]

    for label, series_id, limit, freq in livestock_series:
        try:
            obs  = await _fred_series(series_id, limit=limit, frequency=freq)
            val, date = _latest(obs)
            yoy  = _pct_change(obs, min(12, len([o for o in obs if o["value"] is not None]) - 1))
            if val is not None:
                lines.append(f"{label:<30} ${val:.3f}  [{date}]")
                if yoy is not None:
                    lines.append(f"  YoY:  {yoy:+.2f}%")
                lines.append("")
            else:
                lines.append(f"{label:<30} N/A\n")
        except Exception as e:
            lines.append(f"{label}: Error — {e}\n")

    # ── USDA NASS cattle and hog prices (if key available) ────────────────
    if USDA_API_KEY:
        lines.append("── USDA NASS Livestock Prices Received ──")
        nass_livestock = [
            ("Live Cattle", "CATTLE, BEEF"),
            ("Hogs",        "HOGS"),
        ]
        async with httpx.AsyncClient(timeout=20) as client:
            for label, commodity in nass_livestock:
                try:
                    r = await client.get(
                        USDA_NASS,
                        params={
                            "key":               USDA_API_KEY,
                            "commodity_desc":    commodity,
                            "statisticcat_desc": "PRICE RECEIVED",
                            "agg_level_desc":    "NATIONAL",
                            "year__GE":          str(datetime.now().year - 1),
                            "format":            "JSON",
                        },
                    )
                    r.raise_for_status()
                    items = r.json().get("data", [])
                    if items:
                        latest_item = sorted(items,
                                             key=lambda x: x.get("year", "0"),
                                             reverse=True)[0]
                        val  = latest_item.get("Value", "N/A")
                        unit = latest_item.get("unit_desc", "")
                        yr   = latest_item.get("year", "")
                        lines.append(f"  {label:<14} ${val} {unit}  [{yr}]")
                except Exception as e:
                    lines.append(f"  {label}: NASS error — {e}")
        lines.append("")

    lines.append("Source: FRED (BLS retail meat prices), USDA NASS (if key set)")
    return "\n".join(lines)


# ── Agricultural Intelligence ─────────────────────────────────────────────────

@mcp.tool()
async def get_wasde_summary() -> str:
    """
    Fetch the latest USDA World Agricultural Supply and Demand Estimates
    (WASDE) report summary. Published monthly, this is the authoritative
    global crop supply/demand report that commodity traders watch closely.

    Falls back to SearXNG search for WASDE highlights if direct API
    is unavailable.

    Source: USDA FAS PSD API, SearXNG fallback
    """
    lines = ["=== USDA WASDE REPORT SUMMARY ===\n"]
    lines.append("The WASDE (World Agricultural Supply and Demand Estimates)")
    lines.append("is published monthly by USDA. It is the primary document")
    lines.append("that moves grain, oilseed, and soft commodity markets.\n")

    # ── SearXNG search for latest WASDE highlights ────────────────────────
    # Direct WASDE API access requires parsing large binary files;
    # SearXNG gives us actionable headline summary faster
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{SEARXNG_URL}/search",
                params={
                    "q":          "USDA WASDE report latest corn soybeans wheat supply demand",
                    "format":     "json",
                    "time_range": "month",
                },
            )
            r.raise_for_status()
            results = r.json().get("results", [])[:5]

        lines.append("── Latest WASDE Coverage (via web search) ──")
        if results:
            for item in results:
                title   = item.get("title", "No title")
                content = item.get("content", "")[:220]
                url     = item.get("url", "")
                lines.append(f"• {title}")
                if content:
                    lines.append(f"  {content}")
                lines.append(f"  {url}")
                lines.append("")
        else:
            lines.append("No recent WASDE coverage found in search results.")

    except Exception as e:
        lines.append(f"SearXNG search error: {e}")

    lines.append("Source: SearXNG web search for USDA WASDE report coverage")
    lines.append("Note: For raw USDA PSD data, visit apps.fas.usda.gov/psdonline")
    return "\n".join(lines)


@mcp.tool()
async def get_crop_progress() -> str:
    """
    Fetch USDA weekly crop progress and condition reports for corn, soybeans,
    and winter wheat. These reports move ag commodity prices when conditions
    deviate from expectations. Seasonal: published weekly Apr-Nov.

    Source: USDA NASS (if USDA_API_KEY set), SearXNG fallback
    """
    lines = ["=== USDA CROP PROGRESS & CONDITIONS ===\n"]

    current_month = datetime.now().month
    in_season = 4 <= current_month <= 11
    if not in_season:
        lines.append("Note: Crop progress reports are seasonal (April–November).")
        lines.append(f"Current month ({current_month}) is outside primary reporting season.")
        lines.append("Winter wheat progress may still be available.\n")

    # ── USDA NASS crop progress (if key available) ────────────────────────
    if USDA_API_KEY:
        crops_progress = [
            ("Corn",         "CORN",         "PROGRESS"),
            ("Corn",         "CORN",         "CONDITION"),
            ("Soybeans",     "SOYBEANS",     "CONDITION"),
            ("Winter Wheat", "WHEAT",        "CONDITION"),
        ]
        async with httpx.AsyncClient(timeout=20) as client:
            for label, commodity, stat in crops_progress:
                try:
                    r = await client.get(
                        USDA_NASS,
                        params={
                            "key":               USDA_API_KEY,
                            "commodity_desc":    commodity,
                            "statisticcat_desc": stat,
                            "agg_level_desc":    "NATIONAL",
                            "year__GE":          str(datetime.now().year - 1),
                            "format":            "JSON",
                            "numeric_desc":      "GE 0",
                        },
                    )
                    r.raise_for_status()
                    items = r.json().get("data", [])
                    if items:
                        latest = sorted(items,
                                        key=lambda x: (x.get("year","0"),
                                                       x.get("week_ending","0")),
                                        reverse=True)[0]
                        val  = latest.get("Value", "N/A")
                        week = latest.get("week_ending", "N/A")
                        unit = latest.get("unit_desc", "%")
                        lines.append(f"{label} — {stat.split(',')[0].strip()}:")
                        lines.append(f"  {val} {unit}  [week ending {week}]")
                        lines.append("")
                except Exception as e:
                    lines.append(f"{label} {stat}: NASS error — {e}")

    # ── SearXNG fallback / supplement ────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{SEARXNG_URL}/search",
                params={
                    "q":          "USDA crop progress report corn soybeans wheat conditions weekly",
                    "format":     "json",
                    "time_range": "week",
                },
            )
            r.raise_for_status()
            results = r.json().get("results", [])[:4]

        lines.append("── Crop Progress Coverage (web) ──")
        for item in results:
            title   = item.get("title", "")
            content = item.get("content", "")[:200]
            url     = item.get("url", "")
            lines.append(f"• {title}")
            if content:
                lines.append(f"  {content}")
            lines.append(f"  {url}")
            lines.append("")

    except Exception as e:
        lines.append(f"SearXNG error: {e}")

    lines.append("Source: USDA NASS Crop Progress (if key set), SearXNG supplement")
    return "\n".join(lines)


@mcp.tool()
async def get_noaa_drought() -> str:
    """
    Fetch current US drought conditions from NOAA / US Drought Monitor.
    Drought in key agricultural regions (Corn Belt, Great Plains, California)
    is a primary supply shock for corn, soybeans, wheat, cattle, and produce.

    Source: US Drought Monitor (droughtmonitor.unl.edu), SearXNG
    """
    lines = ["=== NOAA / US DROUGHT MONITOR ===\n"]
    lines.append("Drought conditions directly impact agricultural commodity supply.")
    lines.append("Key regions: Corn Belt (IA/IL/IN), Great Plains (KS/NE/SD),")
    lines.append("California (almonds, oranges, rice), Southeast (cotton, peanuts).\n")

    # ── US Drought Monitor data (JSON API) ────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # National summary — percent area in drought categories
            r = await client.get(
                "https://droughtmonitor.unl.edu/DmData/DataTables.aspx",
                params={
                    "mode":  "table",
                    "aoi":   "conus",
                    "date":  datetime.now().strftime("%Y%m%d"),
                },
            )
            # DM serves HTML tables; fall through to SearXNG
            lines.append("── US Drought Monitor (current week) ──")
            lines.append("Visit: https://droughtmonitor.unl.edu for current map")
            lines.append("")
    except Exception:
        pass

    # ── SearXNG for current drought conditions and ag impact ─────────────
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{SEARXNG_URL}/search",
                params={
                    "q":          "US drought monitor corn belt agriculture crop conditions this week",
                    "format":     "json",
                    "time_range": "week",
                },
            )
            r.raise_for_status()
            results = r.json().get("results", [])[:4]

        lines.append("── Drought & Agricultural Weather Coverage ──")
        if results:
            for item in results:
                title   = item.get("title", "")
                content = item.get("content", "")[:200]
                url     = item.get("url", "")
                lines.append(f"• {title}")
                if content:
                    lines.append(f"  {content}")
                lines.append(f"  {url}")
                lines.append("")
        else:
            lines.append("No current drought coverage found.")

    except Exception as e:
        lines.append(f"SearXNG error: {e}")

    lines.append("Source: US Drought Monitor (droughtmonitor.unl.edu), SearXNG")
    return "\n".join(lines)


# ── Market Structure & Positioning ───────────────────────────────────────────

@mcp.tool()
async def get_cot_report() -> str:
    """
    Fetch CFTC Commitment of Traders (COT) report data for key commodity
    futures contracts. Shows net positioning of commercial hedgers vs
    large speculators. Extreme speculator positioning is a contrarian signal.

    Covers: Gold, Silver, Crude Oil (WTI), Natural Gas, Corn, Soybeans,
    Wheat, Copper.

    Source: CFTC public data (Socrata open data API, no key required)
    """
    lines = ["=== CFTC COMMITMENT OF TRADERS (COT) ===\n"]
    lines.append("Large Speculator net position: positive = net long, negative = net short.")
    lines.append("Extreme positioning = contrarian signal (crowded longs/shorts often reverse).\n")

    # CFTC Socrata endpoint — legacy COT report
    # market_and_exchange_names field identifies the contract
    contracts = [
        "GOLD - COMMODITY EXCHANGE INC.",
        "SILVER - COMMODITY EXCHANGE INC.",
        "WTI FINANCIAL CRUDE OIL - NEW YORK MERCANTILE EXCHANGE",
        "NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE",
        "CORN - CHICAGO BOARD OF TRADE",
        "SOYBEANS - CHICAGO BOARD OF TRADE",
        "WHEAT-SRW - CHICAGO BOARD OF TRADE",
        "COPPER- #1 - COMMODITY EXCHANGE INC.",
    ]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Get most recent report date first
            r = await client.get(
                CFTC_BASE,
                params={
                    "$order":  "report_date_as_yyyy_mm_dd DESC",
                    "$limit":  "1",
                    "$select": "report_date_as_yyyy_mm_dd",
                },
            )
            r.raise_for_status()
            latest_rows = r.json()
            if not latest_rows:
                lines.append("No COT data returned from CFTC API.")
                return "\n".join(lines)

            latest_date = latest_rows[0].get("report_date_as_yyyy_mm_dd", "")
            lines.append(f"Latest COT report date: {latest_date}\n")

            for contract in contracts:
                try:
                    r2 = await client.get(
                        CFTC_BASE,
                        params={
                            "$where":  f"market_and_exchange_names='{contract}' "
                                       f"AND report_date_as_yyyy_mm_dd='{latest_date}'",
                            "$limit":  "1",
                            "$select": ("market_and_exchange_names,"
                                        "noncomm_positions_long_all,"
                                        "noncomm_positions_short_all,"
                                        "report_date_as_yyyy_mm_dd"),
                        },
                    )
                    r2.raise_for_status()
                    rows = r2.json()
                    if rows:
                        row      = rows[0]
                        name     = row.get("market_and_exchange_names", contract)
                        # Shorten for display
                        short_name = name.split(" - ")[0].title()
                        long_pos = int(row.get("noncomm_positions_long_all", 0) or 0)
                        short_pos= int(row.get("noncomm_positions_short_all", 0) or 0)
                        net      = long_pos - short_pos
                        direction = "NET LONG ↑" if net > 0 else "NET SHORT ↓"
                        lines.append(f"{short_name:<30} Net: {net:>+10,}  ({direction})")
                        lines.append(f"  Long: {long_pos:>10,}   Short: {short_pos:>10,}")

                        # Flag extreme positioning
                        abs_net = abs(net)
                        if abs_net > 200000:
                            lines.append(f"  ⚠ Extreme positioning — contrarian signal possible")
                        lines.append("")
                    else:
                        lines.append(f"{contract.split(' - ')[0].title():<30} No data for {latest_date}\n")
                except Exception as e:
                    lines.append(f"{contract.split(' - ')[0].title()}: Error — {e}\n")

    except Exception as e:
        lines.append(f"CFTC API error: {e}")

    lines.append("Source: CFTC Commitment of Traders — publicreporting.cftc.gov (Socrata)")
    return "\n".join(lines)


@mcp.tool()
async def get_baltic_dry() -> str:
    """
    Fetch the Baltic Dry Index (BDI) — a leading indicator of global raw
    material demand. Rising BDI signals growing demand for bulk commodities
    (iron ore, coal, grain). Falling BDI signals slowing global trade.

    Also fetches FRED shipping-related series as context.

    Source: FRED (DBDA series)
    """
    lines = ["=== BALTIC DRY INDEX (BDI) ===\n"]
    lines.append("The BDI measures shipping rates for dry bulk commodities.")
    lines.append("Rising BDI = growing demand (bullish industrials/energy/metals).")
    lines.append("Falling BDI = slowing trade (bearish signal for commodities).\n")

    # BDI via SearXNG web search — Baltic Exchange data is paywalled;
    # SearXNG pulls current readings from shipping news and financial sites
    bdi_found = False
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{SEARXNG_URL}/search",
                params={
                    "q":          "Baltic Dry Index BDI points",
                    "format":     "json",
                    "time_range": "week",
                },
            )
            r.raise_for_status()
            results = r.json().get("results", [])[:10]

        shipping_results = [
            item for item in results
            if any(kw in (item.get("title","") + item.get("content","")).lower()
                   for kw in ["baltic", "bdi", "dry bulk", "shipping rate"])
        ]

        if shipping_results:
            # ── Extract numeric BDI value from snippets ──────────────────
            # Handles: "2,670 points", "reached 2670", "BDI... 2,729",
            # "Baltic Dry rose to 3,399 Index Points", "climbed/fell to X".
            # Widened Sept 2026 after confirming the original patterns
            # missed a real current-reading snippet ("Baltic Dry rose to
            # 3,399 Index Points on <date>") entirely — "Baltic Dry" alone
            # (no "Index") didn't match bdi_pattern's keyword list, and the
            # word "Index" between the number and "Points" broke
            # points_pattern. Meanwhile a Wikipedia snippet describing the
            # index's 1985 base value of 1,000 satisfied the old patterns
            # just fine, so it — the wrong, historical value — is what won.
            import re as _re
            bdi_pattern = _re.compile(
                r'(?:BDI|Baltic Dry Index|Baltic Dry|reached|reaching|' +
                r'stood at|to reach|rose to|climbed to|fell to|dropped to)' +
                r'[^\d]{0,20}(\d{1,2},\d{3}|\d{4})\b',
                _re.IGNORECASE
            )
            points_pattern = _re.compile(
                r'(\d{1,2},\d{3}|\d{4})\s*(?:index\s+)?points',
                _re.IGNORECASE
            )
            bdi_value = None
            bdi_source_date = ""
            _min_bdi_year = datetime.now(timezone.utc).year - 2
            for item in shipping_results:
                text = item.get("title","") + " " + item.get("content","")
                for pat in (bdi_pattern, points_pattern):
                    m = pat.search(text)
                    if m:
                        candidate = int(m.group(1).replace(",", ""))
                        if 400 <= candidate <= 8000:
                            date_m = _re.search(
                                r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)' +
                                r'\w*\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}',
                                text, _re.IGNORECASE
                            )
                            candidate_date = date_m.group(0) if date_m else ""
                            # Reject a candidate tied to an old date — e.g. a
                            # Wikipedia snippet describing the BDI's 1985
                            # base value ("...set at 1,000 on January 4,
                            # 1985") satisfies the numeric range check just
                            # as well as a genuine current reading, and was
                            # confirmed in practice (Sept 2026) to win the
                            # match ahead of a result with the real current
                            # value. Undated candidates are still accepted —
                            # only a clearly old explicit date disqualifies.
                            year_m = _re.search(r'\b(19|20)\d{2}\b', candidate_date)
                            if year_m and int(year_m.group(0)) < _min_bdi_year:
                                continue
                            bdi_value = candidate
                            bdi_source_date = candidate_date
                            break
                if bdi_value:
                    break

            if bdi_value:
                lines.append(f"Baltic Dry Index (BDI):  {bdi_value:,}  [{bdi_source_date}]")
                lines.append("")

            lines.append("── BDI Current Intelligence ──")
            for item in shipping_results[:3]:
                title   = item.get("title", "")
                content = item.get("content", "")[:200]
                lines.append(f"• {title}")
                if content:
                    lines.append(f"  {content}")
                lines.append("")
            bdi_found = True
        else:
            lines.append("SearXNG: no BDI-specific results this week.")

    except Exception as e:
        lines.append(f"BDI web search error: {e}")

    # FRED DBDA fallback
    if not bdi_found:
        try:
            obs = await _fred_series("DBDA", limit=20)
            val, date = _latest(obs)
            if val is not None:
                lines.append(f"Baltic Dry Index (FRED):  {val:,.0f}  [{date}]")
                vals = [o["value"] for o in obs if o["value"] is not None]
                if len(vals) >= 5:
                    chg_4w = ((vals[-1] - vals[-5]) / vals[-5]) * 100
                    lines.append(f"  4-week change: {chg_4w:+.1f}%")
                bdi_found = True
        except Exception:
            pass

    if not bdi_found:
        lines.append("BDI data unavailable this week.")

    lines.append("\nSource: SearXNG (Baltic Dry Index news), FRED DBDA fallback")
    return "\n".join(lines)


# ── Mercury Backdrop YAML Builder ─────────────────────────────────────────────

def _extract_metric_and_change(text: str, label_prefix: str) -> tuple:
    """
    Parse (value, %-change, unit) from a labeled line in the formatted text
    already returned by get_energy_prices() / get_agricultural_prices() /
    get_baltic_dry(). Reusing their output here — instead of an independent
    FRED-only fetch — is the fix for the yaml/summary desync flagged by
    Jansky (Sept 2026 weekly review): two different sources for the same
    figure (e.g. FRED's DCOILWTICO vs EIA's spot WTI) meant the backdrop
    yaml and Mercury's prose summary could report different numbers for
    the same metric, in different units, from different fetch times. One
    fetch, parsed twice, guarantees they agree.
    """
    import re
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith(label_prefix):
            if "error" in line.lower() or "no data" in line.lower():
                return None, None, ""
            unit_m = re.search(r'\(([^)]+)\)', line)
            unit = unit_m.group(1).split(",")[0].strip() if unit_m else ""
            rest = line.split(":", 1)[-1] if ":" in line else line
            val_m = re.search(r'-?[\d,]+\.?\d*', rest)
            if not val_m:
                return None, None, ""
            val = float(val_m.group(0).replace(",", ""))
            chg = None
            for j in range(i + 1, min(i + 3, len(lines))):
                if "4-week change" in lines[j] or "~1mo" in lines[j]:
                    chg_m = re.search(r'([-+]?\d+\.?\d*)%', lines[j])
                    if chg_m:
                        chg = float(chg_m.group(1))
                    break
                if lines[j].strip() == "":
                    break
            return val, chg, unit
    return None, None, ""


@mcp.tool()
async def build_mercury_backdrop() -> str:
    """
    Aggregate key CCC data and return a structured YAML-format backdrop file.
    Designed to be saved as mercury_backdrop_YYYYMMDD.yaml and optionally
    injected into other agents' prompts for cross-market awareness.

    Covers: DXY trend, crypto momentum, energy trend, metals safe-haven
    signal, agricultural supply stress, BDI demand signal, COT positioning.

    Source: FRED, Frankfurter, CoinGecko, yfinance (multiple series). WTI,
    Nat Gas, Corn, and BDI are parsed from get_energy_prices() /
    get_agricultural_prices() / get_baltic_dry()'s own output rather than
    fetched independently here — see _extract_metric_and_change().
    """
    async def _q(series_id: str, limit: int = 14,
                  freq: str = "") -> tuple[Optional[float], str, list]:
        try:
            obs      = await _fred_series(series_id, limit=limit, frequency=freq)
            val, date = _latest(obs)
            return val, date, obs
        except Exception:
            return None, "N/A", []

    # ── Gather data ───────────────────────────────────────────────────────
    dxy_val, dxy_date, dxy_obs     = await _q("DTWEXBGS", 30)
    # Gold via yfinance (FRED LBMA series removed)
    gold_val = None
    gold_date = "N/A"
    gold_obs = []
    gold_4w = None
    try:
        import yfinance as yf
        _gc = yf.Ticker("GC=F").history(period="3mo")
        if not _gc.empty:
            _gc_closes = _gc["Close"].dropna()
            gold_val  = float(_gc_closes.iloc[-1])
            gold_date = str(_gc_closes.index[-1].date())
            if len(_gc_closes) >= 20:
                gold_4w = ((_gc_closes.iloc[-1] - _gc_closes.iloc[-20])
                           / _gc_closes.iloc[-20]) * 100
    except Exception:
        pass
    silver_val = None
    try:
        import yfinance as yf
        _si = yf.Ticker("SI=F").history(period="1mo")
        if not _si.empty:
            silver_val = float(_si["Close"].dropna().iloc[-1])
    except Exception:
        pass
    copper_val, copper_date, _     = await _q("PCOPPUSDM", 14, "m")

    # ── WTI, Nat Gas, Corn, BDI — reuse the SAME fetch the prose summary
    # uses (get_energy_prices / get_agricultural_prices / get_baltic_dry)
    # instead of an independent FRED-only pull. Two different sources for
    # the same figure is exactly what caused the yaml/summary desync
    # Jansky flagged (WTI $107.02 yaml vs $99.08 summary, natgas $2.97 vs
    # $2.79, corn in different units, BDI "Unknown" vs 3,370). One fetch,
    # parsed twice, guarantees the numbers agree. ──────────────────────────
    energy_text = await get_energy_prices()
    wti_val, wti_4w, _            = _extract_metric_and_change(energy_text, "WTI Crude Oil (")
    natgas_val, _, _              = _extract_metric_and_change(energy_text, "Henry Hub Nat Gas (")

    ag_text = await get_agricultural_prices()
    corn_val, corn_yoy, corn_unit = _extract_metric_and_change(ag_text, "Corn (")
    # NOTE: primary source is now yfinance daily futures ($/bu, ~1mo trend
    # window), not FRED's monthly $/mt YoY series — "corn_yoy" here is
    # whichever trend window the matched line reported (1mo if yfinance
    # succeeded, true YoY if it fell back to FRED). The ag_stance
    # thresholds below were tuned for YoY moves; revisit if it starts
    # flipping too often on ordinary 1-month noise.
    soy_val, soy_date, _          = await _q("PSOYBUSDM", 14, "m")
    wheat_val, wheat_date, _      = await _q("PWHEAMTUSDM", 14, "m")

    bdi_text = await get_baltic_dry()
    bdi_val, bdi_4w, _            = _extract_metric_and_change(bdi_text, "Baltic Dry Index (")

    # ── DXY trend ─────────────────────────────────────────────────────────
    dxy_vals = [o["value"] for o in dxy_obs if o["value"] is not None]
    dxy_4w   = ((dxy_vals[-1] - dxy_vals[-5]) / dxy_vals[-5] * 100) \
               if len(dxy_vals) >= 5 else None
    if dxy_4w is not None:
        if dxy_4w > 2:
            dxy_stance = "Strengthening"
        elif dxy_4w < -2:
            dxy_stance = "Weakening"
        else:
            dxy_stance = "Neutral"
    else:
        dxy_stance = "Unknown"

    # ── Gold safe-haven signal (gold_4w computed above via yfinance) ────────
    if gold_4w is not None:
        if gold_4w > 3:
            gold_stance = "Strong Safe-Haven Bid"
        elif gold_4w > 0:
            gold_stance = "Mild Safe-Haven Bid"
        elif gold_4w < -3:
            gold_stance = "Risk-On (Gold Selling)"
        else:
            gold_stance = "Neutral"
    else:
        gold_stance = "Unknown"

    gs_ratio = (gold_val / silver_val) if gold_val and silver_val else None

    # ── Energy trend ─────────────────────────────────────────────────────
    if wti_4w is not None:
        if wti_4w > 5:
            energy_stance = "Rising (Bullish)"
        elif wti_4w < -5:
            energy_stance = "Falling (Bearish)"
        else:
            energy_stance = "Stable"
    else:
        energy_stance = "Unknown"

    # ── Grain supply stress ───────────────────────────────────────────────
    if corn_yoy is not None:
        if corn_yoy > 15:
            ag_stance = "Supply Stressed (Prices Elevated)"
        elif corn_yoy < -15:
            ag_stance = "Supply Abundant (Prices Depressed)"
        else:
            ag_stance = "Stable"
    else:
        ag_stance = "Unknown"

    # ── BDI demand signal ─────────────────────────────────────────────────
    if bdi_4w is not None:
        if bdi_4w > 10:
            bdi_stance = "Accelerating Demand"
        elif bdi_4w < -10:
            bdi_stance = "Slowing Demand"
        else:
            bdi_stance = "Stable"
    else:
        bdi_stance = "Unknown"

    # ── CoinGecko BTC for crypto stance ──────────────────────────────────
    # Uses /coins/markets (same endpoint as get_crypto_prices()) rather than
    # /simple/price — the latter's usd_7d_change field is unreliably
    # populated on CoinGecko's free tier even when price comes through fine.
    crypto_stance = "Unknown"
    btc_price = None
    btc_7d = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{COINGECKO}/coins/markets",
                params={
                    "vs_currency":              "usd",
                    "ids":                      "bitcoin",
                    "order":                    "market_cap_desc",
                    "per_page":                 "1",
                    "page":                     "1",
                    "sparkline":                "false",
                    "price_change_percentage":  "7d",
                },
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            _coins = r.json()
            if _coins:
                btc_price = _coins[0].get("current_price")
                btc_7d    = _coins[0].get("price_change_percentage_7d_in_currency")
            if btc_7d is not None:
                if btc_7d > 5:
                    crypto_stance = "Risk-On (Rising)"
                elif btc_7d < -5:
                    crypto_stance = "Risk-Off (Falling)"
                else:
                    crypto_stance = "Neutral"
    except Exception:
        btc_price = None
        btc_7d    = None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Build YAML ────────────────────────────────────────────────────────
    lines = [
        f"# Mercury CCC Backdrop",
        f"# Generated: {now}",
        f"# Source: mercurymcp / FRED, Frankfurter, CoinGecko",
        f"",
        f"generated_date: \"{now}\"",
        f"",
        f"# ─────────────────────────────────────────────────────────────────",
        f"# US DOLLAR (DXY)",
        f"# ─────────────────────────────────────────────────────────────────",
        f"dollar_index:",
        f"  stance: {dxy_stance}",
        f"  key_metrics:",
        f"    dxy_level: {f'{dxy_val:.3f}' if dxy_val else 'N/A'}",
        f"    dxy_4week_change_pct: {f'{dxy_4w:.2f}' if dxy_4w else 'N/A'}",
        f"  risk_flags:",
    ]
    if dxy_4w and dxy_4w > 3:
        lines.append(f"    - \"DXY rising {dxy_4w:.1f}% in 4 weeks — headwind for commodities and EM currencies\"")
    elif dxy_4w and dxy_4w < -3:
        lines.append(f"    - \"DXY falling {abs(dxy_4w):.1f}% in 4 weeks — tailwind for commodities and EM currencies\"")
    else:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ─────────────────────────────────────────────────────────────────",
        f"# CRYPTO",
        f"# ─────────────────────────────────────────────────────────────────",
        f"crypto:",
        f"  stance: {crypto_stance}",
        f"  key_metrics:",
        f"    btc_price_usd: {f'{btc_price:,.0f}' if btc_price else 'N/A'}",
        f"    btc_7d_change_pct: {f'{btc_7d:.2f}' if btc_7d is not None else 'N/A'}",
        f"  risk_flags:",
    ]
    if btc_7d and btc_7d < -15:
        lines.append(f"    - \"BTC down {abs(btc_7d):.1f}% in 7 days — crypto risk-off, monitor equity correlation\"")
    else:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ─────────────────────────────────────────────────────────────────",
        f"# ENERGY",
        f"# ─────────────────────────────────────────────────────────────────",
        f"energy:",
        f"  stance: {energy_stance}",
        f"  key_metrics:",
        f"    wti_crude_usd: {f'{wti_val:.2f}' if wti_val else 'N/A'}",
        f"    wti_4week_change_pct: {f'{wti_4w:.2f}' if wti_4w else 'N/A'}",
        f"    natgas_henry_hub: {f'{natgas_val:.3f}' if natgas_val else 'N/A'}",
        f"  risk_flags:",
    ]
    if wti_4w and wti_4w > 10:
        lines.append(f"    - \"WTI up {wti_4w:.1f}% in 4 weeks — energy cost inflation risk for equities\"")
    elif wti_4w and wti_4w < -10:
        lines.append(f"    - \"WTI down {abs(wti_4w):.1f}% in 4 weeks — demand concern signal\"")
    else:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ─────────────────────────────────────────────────────────────────",
        f"# METALS",
        f"# ─────────────────────────────────────────────────────────────────",
        f"metals:",
        f"  gold_stance: {gold_stance}",
        f"  key_metrics:",
        f"    gold_usd_oz: {f'{gold_val:.2f}' if gold_val else 'N/A'}",
        f"    gold_4week_change_pct: {f'{gold_4w:.2f}' if gold_4w else 'N/A'}",
        f"    silver_usd_oz: {f'{silver_val:.3f}' if silver_val else 'N/A'}",
        f"    copper_usd_lb: {f'{copper_val:.4f}' if copper_val else 'N/A'}",
        f"    gold_silver_ratio: {f'{gs_ratio:.1f}' if gs_ratio else 'N/A'}",
        f"  risk_flags:",
    ]
    if gold_4w and gold_4w > 5:
        lines.append(f"    - \"Gold up {gold_4w:.1f}% in 4 weeks — strong safe-haven demand, risk-off signal\"")
    if gs_ratio and gs_ratio > 90:
        lines.append(f"    - \"Gold/Silver ratio at {gs_ratio:.0f} — elevated risk aversion\"")
    if not ((gold_4w and gold_4w > 5) or (gs_ratio and gs_ratio > 90)):
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ─────────────────────────────────────────────────────────────────",
        f"# AGRICULTURE",
        f"# ─────────────────────────────────────────────────────────────────",
        f"agriculture:",
        f"  stance: {ag_stance}",
        f"  key_metrics:",
        f"    corn_price: {f'{corn_val:.2f} {corn_unit}' if corn_val else 'N/A'}",
        f"    corn_trend_pct: {f'{corn_yoy:.2f}' if corn_yoy is not None else 'N/A'}",
        f"    corn_trend_window: {'1mo (yfinance)' if corn_unit == '$/bu' else 'YoY (FRED fallback)'}",
        f"    soybeans_usd_mt: {f'{soy_val:.2f}' if soy_val else 'N/A'}",
        f"    wheat_usd_mt: {f'{wheat_val:.2f}' if wheat_val else 'N/A'}",
        f"  risk_flags:",
    ]
    if corn_yoy and corn_yoy > 20:
        lines.append(f"    - \"Corn prices up {corn_yoy:.0f}% YoY — food inflation risk, watch livestock cost passthrough\"")
    elif corn_yoy and corn_yoy < -20:
        lines.append(f"    - \"Corn prices down {abs(corn_yoy):.0f}% YoY — ag sector margin pressure\"")
    else:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ─────────────────────────────────────────────────────────────────",
        f"# GLOBAL TRADE DEMAND (BALTIC DRY)",
        f"# ─────────────────────────────────────────────────────────────────",
        f"global_trade:",
        f"  bdi_stance: {bdi_stance}",
        f"  key_metrics:",
        f"    bdi_level: {f'{bdi_val:,.0f}' if bdi_val else 'N/A'}",
        f"    bdi_4week_change_pct: {f'{bdi_4w:.1f}' if bdi_4w else 'N/A'}",
        f"  risk_flags:",
    ]
    if bdi_4w and bdi_4w < -20:
        lines.append(f"    - \"BDI falling {abs(bdi_4w):.0f}% in 4 weeks — demand warning for industrials and metals\"")
    else:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ─────────────────────────────────────────────────────────────────",
        f"# AI USAGE GUIDANCE",
        f"# ─────────────────────────────────────────────────────────────────",
        f"ai_guidance:",
        f"  for_mercury: >",
        f"    Use dollar_index.stance as the primary filter. Strengthening DXY",
        f"    is a headwind for commodities and EM currencies. Rising gold with",
        f"    rising DXY = true risk-off (unusual). BDI leads industrial metals.",
        f"  for_jupiter: >",
        f"    Rising energy prices (stance: Rising) compress margins for",
        f"    consumer discretionary, airlines, and logistics. Gold safe-haven",
        f"    bid signals risk-off that may weigh on growth multiples.",
        f"    Ag supply stress raises food cost inputs for restaurants and food",
        f"    manufacturers. Rising BDI is a green light for industrials.",
        f"  cross_market_signals:",
        f"    - \"DXY {dxy_stance} + Gold {gold_stance} + Crypto {crypto_stance}\"",
        f"    - \"Energy: {energy_stance} | Agriculture: {ag_stance} | Trade: {bdi_stance}\"",
    ]

    return "\n".join(lines)


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
    uvicorn.run(app, host="0.0.0.0", port=8645)
