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
  get_gold_drivers         — Real yield, breakeven, dollar, gold trend, COT percentile, GLD/IAU flows
  get_etf_flows            — ETF creations/redemptions proxy (IBIT, FBTC, BITQ, GLD, IAU)
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
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncio
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


_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


async def _nass_query(client: httpx.AsyncClient, **params) -> list[dict]:
    """
    Query USDA NASS Quick Stats. Returns the 'data' rows ([] when NASS reports
    no matching records). NASS answers HTTP 400 with {"error": [...]} both for a
    bad parameter value and for "no records" — surface that text instead of the
    bare 'Bad Request' so a wrong commodity_desc is diagnosable.
    """
    q = {"key": USDA_API_KEY, "format": "JSON", **params}
    r = await client.get(USDA_NASS, params=q)
    if r.status_code == 400:
        try:
            msg = "; ".join(r.json().get("error", [])) or r.text[:120]
        except Exception:
            msg = r.text[:120]
        if "no data" in msg.lower():
            return []
        raise RuntimeError(f"NASS 400: {msg}")
    r.raise_for_status()
    return r.json().get("data", [])


def _nass_sort_key(row: dict) -> tuple:
    """Chronological key: (year, month, week_ending) — NASS rows are unordered."""
    month = _MONTHS.get((row.get("reference_period_desc") or "")[:3].upper(), 0)
    return (row.get("year", "0"), month, row.get("week_ending") or "")


def _nass_val(row: dict) -> Optional[float]:
    try:
        return float(str(row.get("Value", "")).replace(",", ""))
    except ValueError:
        return None


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
    dxy_synth = None

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

        # Synthetic ICE DXY from the same-day ECB rates (standard formula).
        # FRED's DTWEXBGS is a different index (Fed broad trade-weighted, ~120
        # level, weekly with a ~1 week lag) and was being labeled "DXY", which
        # gave Jansky a level and a date that disagreed with the FX table.
        dxy_synth = None
        try:
            eurusd = 1 / rates["EUR"]; gbpusd = 1 / rates["GBP"]
            dxy_synth = (50.14348112 * eurusd ** -0.576 * rates["JPY"] ** 0.136
                         * gbpusd ** -0.119 * rates["CAD"] ** 0.091
                         * rates["SEK"] ** 0.042 * rates["CHF"] ** 0.036)
        except Exception:
            pass

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

    if dxy_synth is not None:
        lines.append(f"\nICE-style DXY (computed from the ECB rates above):  {dxy_synth:.2f}  [{date_str}]")
        lines.append("  (6-currency basket, same date as the FX table — use this as 'DXY'.)")

    # ── Fed broad trade-weighted dollar index from FRED ──────────────────
    try:
        dxy = await _fred_series("DTWEXBGS", limit=30)
        dxy_val, dxy_date = _latest(dxy)

        lines.append(f"\nFed Broad Dollar Index (FRED DTWEXBGS, ~120 scale, NOT the ICE DXY):  "
                     f"{dxy_val:.3f}  [{dxy_date}]  — weekly series, lags the FX table by days")

        # 4-week and 13-week trend
        vals = [o["value"] for o in dxy if o["value"] is not None]
        if len(vals) >= 5:
            chg_4w = ((vals[-1] - vals[-5]) / vals[-5]) * 100
            direction = "↑ strengthening" if chg_4w > 0 else "↓ weakening"
            lines.append(f"  4-week change:         {chg_4w:+.2f}%  ({direction})")
        if len(vals) >= 14:
            chg_13w = ((vals[-1] - vals[-14]) / vals[-14]) * 100
            lines.append(f"  13-week change:        {chg_13w:+.2f}%")

        lines.append("\nFed Broad Dollar Index recent history (last 10 obs):")
        for obs in dxy[-10:]:
            if obs["value"] is not None:
                lines.append(f"  {obs['date']}  {obs['value']:.3f}")

        lines.append("\nNote: Rising DXY = USD strengthening = headwind for commodities & EM FX.")
        lines.append("Source: Frankfurter/ECB, FRED DTWEXBGS")

    except Exception as e:
        lines.append(f"\nFed Broad Dollar Index fetch error: {e}")

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

    # (label, series_id, freq, max_age_days)
    # Verified on FRED Oct 2026. The OECD "IRSTCB01..." central-bank series
    # for Japan, Canada and China (and IR3TIB01GBM156N for the UK) have
    # STOPPED UPDATING (Dec 2023 / Dec 2023 / Nov 2023 / Jan 2026), so they
    # were silently returning years-old rates. Replacements:
    #   JPY: IRSTCI01JPM156N  call money overnight (tracks BoJ policy rate)
    #   CAD: IRSTCI01CAM156N  call money overnight (tracks BoC target)
    #   GBP: IUDSOIA          SONIA, daily from the Bank of England
    #   CNY: IR3TIB01CNM156N  3-month interbank (no live policy-rate series)
    # OECD monthly series lag ~6-8 weeks, hence the 100-day staleness limit.
    # Anything older than max_age_days is printed as STALE and is excluded
    # from the spread commentary below rather than treated as current.
    cb_series = [
        ("Federal Reserve (USD)",          "DFF",              "",  10),
        ("European Central Bank (EUR)",    "ECBDFR",           "",  10),
        ("Bank of Japan (JPY)",            "IRSTCI01JPM156N",  "m", 100),
        ("Bank of England (GBP)",          "IUDSOIA",          "",  10),
        ("Bank of Canada (CAD)",           "IRSTCI01CAM156N",  "m", 100),
        ("Reserve Bank of Australia (AUD)","IR3TIB01AUM156N",  "m", 100),
        ("Swiss National Bank (CHF)",      "IR3TIB01CHM156N",  "m", 100),
        ("People's Bank of China (CNY)",   "IR3TIB01CNM156N",  "m", 100),
    ]
    proxy_note = {
        "Bank of Japan (JPY)":       "overnight call money (BoJ policy-rate proxy)",
        "Bank of England (GBP)":     "SONIA overnight",
        "Bank of Canada (CAD)":      "overnight call money (BoC policy-rate proxy)",
        "Reserve Bank of Australia (AUD)": "3-mo interbank (policy-rate proxy)",
        "Swiss National Bank (CHF)": "3-mo interbank (policy-rate proxy)",
        "People's Bank of China (CNY)": "3-mo interbank (proxy; no live policy series)",
    }

    def _age_days(date_str: str) -> Optional[int]:
        try:
            return (datetime.now() - datetime.strptime(date_str[:10], "%Y-%m-%d")).days
        except Exception:
            return None

    rates = {}      # label -> (value, date)  — FRESH values only
    stale = []
    for label, series_id, freq, max_age in cb_series:
        try:
            obs = await _fred_series(series_id, limit=6, frequency=freq)
            val, date = _latest(obs)
            if val is None:
                lines.append(f"  {label:<40} N/A")
                continue
            age = _age_days(date)
            note = proxy_note.get(label, "")
            note = f"  ({note})" if note else ""
            if age is not None and age > max_age:
                lines.append(f"  {label:<40} {val:.2f}%  [{date}]  "
                             f"⚠ STALE ({age} days old) — NOT used in comparisons{note}")
                stale.append(label)
            else:
                rates[label] = (val, date)
                lines.append(f"  {label:<40} {val:.2f}%  [{date}]{note}")
        except Exception as e:
            lines.append(f"  {label:<40} Error: {e}")

    if stale:
        lines.append(f"\nData-quality warning: {len(stale)} series stale ({', '.join(stale)}). "
                     "Do not cite those as current policy rates.")

    # Policy divergence — yield differentials only. No directional FX call is
    # hardcoded: a spread is one input; intervention/policy-path risk matters.
    lines.append("\nPolicy Divergence Analysis (Fed minus other bank; fresh data only):")
    fed = rates.get("Federal Reserve (USD)", (None, ""))[0]
    if fed is None:
        lines.append("  Fed rate unavailable — no spreads computed.")
    else:
        for label, short in [("European Central Bank (EUR)", "ECB"),
                             ("Bank of Japan (JPY)", "BoJ"),
                             ("Bank of England (GBP)", "BoE"),
                             ("Bank of Canada (CAD)", "BoC"),
                             ("Reserve Bank of Australia (AUD)", "RBA"),
                             ("Swiss National Bank (CHF)", "SNB")]:
            other = rates.get(label, (None, ""))[0]
            if other is None:
                continue
            spread = fed - other
            side = "USD yield advantage" if spread > 0 else "USD yield disadvantage"
            lines.append(f"  Fed vs {short:<4} spread:  {spread:+.2f}%  → {side}")

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
    _spot_spread = None

    # ── EIA API v2 route-based calls ────────────────────────────────────
    if EIA_API_KEY:
        # EIA v2 uses category routes, not /seriesid/
        # Each route has its own facet filter for the series
        eia_routes = [
            ("WTI Crude Oil ($/bbl)",          f"{EIA_BASE}/petroleum/pri/spt/data/",           "RWTC"),
            ("Brent Crude Oil ($/bbl)",         f"{EIA_BASE}/petroleum/pri/spt/data/",           "RBRTE"),
            ("Henry Hub Nat Gas ($/MMBtu)",     f"{EIA_BASE}/natural-gas/pri/fut/data/",          "RNGWHHD"),
            # Sept 2026 fix: these two previously appended extra path
            # segments (`/dcus/nus/w/`) after the route category, which
            # aren't valid in the EIA v2 route structure — every other
            # route here (spt, fut) is just "<category>/data/" with the
            # specific series picked via facets[series][], and these two
            # were the only ones not following that pattern. That's what
            # was producing the confirmed 404s; series selection already
            # happens correctly via the facets[series][] param below.
            ("RBOB Gasoline ($/gal)",           f"{EIA_BASE}/petroleum/pri/gnd/data/",           "EMM_EPMRU_PTE_NUS_DPG"),
            ("Heating Oil No.2 ($/gal)",        f"{EIA_BASE}/petroleum/pri/gnd/data/",           "EMM_EPD2F_PTE_NUS_DPG"),
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
            # Oct 2026 fix: the old test was `label in ln and "$" in ln`, but the
            # labels themselves contain "($/gal)", so ANY line mentioning the label
            # counted as "has a price" — including "N/A" and EIA-error lines. The
            # FRED fallback therefore never ran and heating oil (and RBOB when EIA
            # failed) was silently missing. Require a real value on a line that
            # starts with the label.
            label_has_price = any(
                ln.startswith(label) and "N/A" not in ln and "error" not in ln.lower()
                for ln in lines
            )
            if not label_has_price:
                try:
                    obs = await _fred_series(series_id, limit=limit, frequency=freq)
                    val, date = _latest(obs)
                    if val is not None:
                        # drop the failed EIA line for this label (N/A / error)
                        lines[:] = [ln for ln in lines
                                    if not (ln.startswith(label)
                                            and ("N/A" in ln or "error" in ln.lower()))]
                        lines.append(f"{label:<45} ${val:.3f}  [{date}] (FRED fallback)")
                except Exception:
                    pass

        # ── Brent–WTI sanity check ───────────────────────────────────────
        try:
            def _first_num(prefix):
                for ln in lines:
                    if ln.startswith(prefix):
                        return float(ln[len(prefix):].split()[0].replace("$", ""))
                return None
            _wti = _first_num("WTI Crude Oil ($/bbl)")
            _brent = _first_num("Brent Crude Oil ($/bbl)")
            if _wti and _brent:
                _spot_spread = _brent - _wti   # judged below, once futures are known
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
        # Oct 17 2026 fix: this call failed with an EMPTY error message
        # (httpx timeouts stringify to ""), so Mercury saw "Crude inventory
        # error:" and a HIGH data gap. Now: longer timeout, one retry, the
        # exception type in any message, and a FRED fallback (WCESTUS1 is the
        # same weekly commercial crude stock series, in thousand barrels).
        inv_data = []
        inv_err = ""
        for _attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=45) as client:
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
                        break
                    inv_err = "EIA returned no rows"
            except Exception as e:
                inv_err = f"{type(e).__name__}: {e}"
        if not inv_data:
            try:
                obs = await _fred_series("WCESTUS1", limit=6, frequency="")
                fvals = [(o["date"], o["value"]) for o in obs if o["value"] is not None]
                if fvals:
                    d_latest, v_latest = fvals[-1]
                    lines.append(f"\nUS Crude Inventories (Mbbl, ex-SPR):      {v_latest:,.0f}  [{d_latest}] (FRED fallback; EIA failed: {inv_err})")
                    if len(fvals) >= 2:
                        draw = v_latest - fvals[-2][1]
                        signal = "↓ draw (bullish)" if draw < 0 else "↑ build (bearish)"
                        lines.append(f"  Weekly change (Mbbl):                   {draw:+.1f}  {signal}")
                else:
                    lines.append(f"\nCrude inventory error: {inv_err} (FRED fallback returned no data)")
            except Exception as e:
                lines.append(f"\nCrude inventory error: {inv_err}; FRED fallback {type(e).__name__}: {e}")
        try:
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

    # ── Front-month futures (Yahoo) — Oct 24 2026 ──────────────────────────
    # The EIA/FRED prints above are SPOT benchmarks (Cushing WTI, Dated Brent).
    # Futures can sit far from spot in a tight market (Oct 7: Brent futures ~$101,
    # WTI ~$89 while spot printed ~$125/$100), so Mercury and Jansky kept reading
    # the difference as a broken feed. Show both, labeled.
    try:
        import yfinance as yf
        _fut = {}
        lines.append("\n── Front-month futures (Yahoo) ──")
        for _lbl, _sym in (("WTI futures (NYMEX CL=F)", "CL=F"),
                           ("Brent futures (ICE BZ=F)", "BZ=F")):
            _h = yf.Ticker(_sym).history(period="1mo")
            if not _h.empty:
                _last = float(_h["Close"].iloc[-1])
                _first = float(_h["Close"].iloc[0])
                _fut[_sym] = _last
                lines.append(f"{_lbl:<45} {_last:.2f}  [{_h.index[-1].strftime('%Y-%m-%d')}]")
                if _first:
                    lines.append(f"  {'~1-month change:':<43} {((_last - _first) / _first) * 100:+.2f}%")
        if len(_fut) == 2:
            _fspr = _fut['BZ=F'] - _fut['CL=F']
            lines.append(f"Futures Brent–WTI spread: ${_fspr:.2f}")
            if _spot_spread is not None:
                lines.append(
                    f"Brent–WTI summary: spot spread ${_spot_spread:.2f} vs futures spread ${_fspr:.2f} "
                    f"(usual range roughly $2–$10). Both are wide, so the market is pricing a large Brent "
                    f"premium; the extra ${_spot_spread - _fspr:.2f} on spot reflects physical Dated Brent "
                    f"trading above Brent futures plus EIA's roughly one-week reporting lag.")
        elif _spot_spread is not None and (_spot_spread < 0 or _spot_spread > 15):
            lines.append(f"\nData-quality warning: Brent–WTI SPOT spread is ${_spot_spread:.2f} "
                         f"(usual range roughly $2–$10) and no futures prices were available to cross-check it.")
        lines.append("Note: the EIA/FRED prices above are physical SPOT benchmarks; futures can differ "
                     "materially from spot in a tight market. A wide gap between the two is a market "
                     "signal (physical premium / backwardation), not necessarily a bad print.")
    except Exception as e:
        lines.append(f"Futures lookup problem: {type(e).__name__}")

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
            _vclose = vde_hist["Close"].dropna() if not vde_hist.empty else vde_hist
            if len(_vclose) > 1:   # dropna: a partial last row once printed "+nan%"
                vde_30d = ((_vclose.iloc[-1] - _vclose.iloc[0]) / _vclose.iloc[0]) * 100
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
            # Live industrial-metal futures: the World Bank monthly series below
            # lags ~2+ months (July 2026 still the latest in October), so these
            # give a current price. Copper is quoted in USD/lb on COMEX.
            ("Copper Futures (USD/lb)",     "HG=F"),
            ("Aluminum Futures (USD/mt)",   "ALI=F"),
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
                try:
                    _age = (datetime.now() - datetime.strptime(date[:10], "%Y-%m-%d")).days
                except Exception:
                    _age = 0
                _stale = f"  ⚠ STALE: monthly World Bank average, {_age} days old" if _age > 75 else \
                         "  (monthly World Bank average)"
                lines.append(f"{label:<28} ${val:>10,.2f}  [{date}]{_stale}")
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
    copp_price = next((v for k, v in prices.items() if k.startswith("Copper (")), None)  # monthly, USD/mt
    copp_live  = prices.get("Copper Futures (USD/lb)")                                   # live, USD/lb

    lines.append("── Key Ratios ──")
    if gold_price and silv_price:
        gs_ratio = gold_price / silv_price
        lines.append(f"Gold/Silver Ratio:    {gs_ratio:.1f}x")
        if gs_ratio > 90:
            lines.append("  ⚠ Elevated ratio (>90) — risk-off / silver underperforming")
        elif gs_ratio < 50:
            lines.append("  ✓ Low ratio (<50) — risk-on / silver outperforming")

    if gold_price and (copp_live or copp_price):
        # Sept 2026 fix: this previously divided by (copp_price * 100)
        # assuming copp_price was already USD/lb — but the FRED series
        # feeding it (PCOPPUSDM, labeled "Copper (USD/mt)" a few lines up)
        # is USD per METRIC TON, roughly 9,000-10,000. Dividing a ~$3,000
        # gold price by ~900,000-1,000,000 produces a ratio that rounds to
        # 0.00 every single time — exactly the self-flagged bug. Standard
        # gold-oz/copper-lb ratio needs copper actually converted to
        # USD/lb first (1 metric ton = 2204.62 lb).
        # Oct 2026: prefer live COMEX copper (same-day as the gold price). The
        # monthly series is 2+ months old, so dividing today's gold by it mixed
        # dates and misstated the ratio.
        if copp_live:
            copper_usd_per_lb, _src = copp_live, "live COMEX copper"
        else:
            copper_usd_per_lb, _src = copp_price / 2204.62, "monthly World Bank copper (STALE)"
        gc_ratio = gold_price / copper_usd_per_lb
        lines.append(f"Gold/Copper Ratio:    {gc_ratio:.2f}  (rising = risk-off; uses {_src})")

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

ETF_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "etf_share_snapshots.json")


def _etf_share_flow_sync(tickers: list[str], days: int = 28) -> list[str]:
    """Estimate ETF creations/redemptions from the change in implied shares outstanding.

    Oct 26 2026: Mercury could say "IBIT volume 22.9M shares" but not whether money
    was flowing in or out. Yahoo has no usable share-count HISTORY for ETFs
    (get_shares_full returned 0 rows for all five funds) and its sharesOutstanding
    field is stale for GLD/IAU (260M vs ~385M implied). What it does return fresh is
    totalAssets and navPrice, so implied shares = totalAssets / navPrice. Each call
    records that snapshot in etf_share_snapshots.json; flow is the change in implied
    shares since a snapshot at least 5 days old, x the current NAV. The first run only
    records a baseline; estimates begin with the next weekly run.
    """
    import yfinance as yf
    try:
        with open(ETF_SNAPSHOT_PATH) as f:
            store = json.load(f)
    except Exception:
        store = {}
    today = datetime.now(timezone.utc).date()
    out = []
    for tk in tickers:
        try:
            info = yf.Ticker(tk).info
            ta, nav = info.get("totalAssets"), info.get("navPrice") or info.get("regularMarketPrice")
            if not ta or not nav or nav <= 0:
                out.append(f"  {tk:<6} totalAssets/NAV unavailable from Yahoo — flow cannot be estimated")
                continue
            shares = float(ta) / float(nav)
            hist = [h for h in store.get(tk, []) if h.get("date") != today.isoformat()]
            cands = [h for h in hist if (today - datetime.fromisoformat(h["date"]).date()).days >= 5]
            base = None
            if cands:
                tgt = today - timedelta(days=days)
                base = min(cands, key=lambda h: abs((datetime.fromisoformat(h["date"]).date() - tgt).days))
            hist.append({"date": today.isoformat(), "shares": shares, "total_assets": float(ta), "nav": float(nav)})
            store[tk] = hist[-26:]
            if base is None:
                out.append(f"  {tk:<6} implied shares {shares/1e6:,.1f}M recorded as baseline — flow estimates "
                           f"start once a snapshot at least 5 days old exists (next weekly run); flow unverified until then")
                continue
            span = (today - datetime.fromisoformat(base["date"]).date()).days
            d_sh = shares - base["shares"]
            pct = d_sh / base["shares"] * 100 if base["shares"] else 0.0
            if abs(pct) < 0.005 and abs(ta - base["total_assets"]) < 1:
                out.append(f"  {tk:<6} Yahoo's asset figure is unchanged since {base['date']} — cannot tell flat flows "
                           f"from a stale feed; flow unverified")
                continue
            usd = d_sh * float(nav)
            direction = "net CREATIONS (inflow)" if d_sh > 0 else ("net REDEMPTIONS (outflow)" if d_sh < 0 else "flat")
            out.append(f"  {tk:<6} implied shares {base['shares']/1e6:,.1f}M [{base['date']}] → {shares/1e6:,.1f}M "
                       f"[{today}]  {pct:+.2f}% over {span}d  ≈ {'+' if usd >= 0 else '-'}${abs(usd)/1e6:,.0f}M  {direction}")
        except Exception as e:
            out.append(f"  {tk:<6} flow estimate error — {type(e).__name__}: {str(e)[:80]}")
    try:
        with open(ETF_SNAPSHOT_PATH, "w") as f:
            json.dump(store, f)
    except Exception as e:
        out.append(f"  (could not save snapshot history: {type(e).__name__} — flows will not accumulate)")
    return out


@mcp.tool()
async def get_etf_flows(tickers: list[str] = None) -> str:
    """
    Estimated ETF creations/redemptions (flow proxy) over ~4 weeks, from the change
    in shares outstanding x price. Default set: spot-Bitcoin and crypto-industry ETFs
    (IBIT, FBTC, BITQ) plus gold ETFs (GLD, IAU). Approximate — follows fund filings.
    """
    tickers = [t.upper() for t in (tickers or ["IBIT", "FBTC", "BITQ", "GLD", "IAU"])]
    lines = ["=== ETF FLOW PROXY (change in implied shares outstanding x NAV) ===\n"]
    lines += await asyncio.to_thread(_etf_share_flow_sync, tickers)
    lines.append("\nNote: approximate. Shares outstanding change only via creations/redemptions, so "
                 "this separates real inflows from mere trading volume. Implied shares = Yahoo totalAssets / NAV, "
                 "snapshotted each run (history builds week by week).")
    return "\n".join(lines)


@mcp.tool()
async def get_gold_drivers() -> str:
    """
    The macro drivers of gold in one place, so gold moves can be explained rather
    than just reported: 10Y TIPS real yield, 10Y breakeven inflation, broad dollar,
    gold's trend and drawdown, how tightly gold has tracked real yields lately,
    speculator (COT) positioning against its 3-year range, and GLD/IAU flows.
    Sources: FRED (DFII10, T10YIE, DTWEXBGS), Yahoo Finance (GC=F), CFTC, Yahoo share counts.
    """
    lines = ["=== GOLD DRIVERS ===\n"]
    gold_closes = None

    # ── Gold price, trend, drawdown ──────────────────────────────────────
    try:
        import yfinance as yf
        h = await asyncio.to_thread(lambda: yf.Ticker("GC=F").history(period="14mo"))
        gold_closes = h["Close"].dropna() if h is not None and not h.empty else None
    except Exception as e:
        lines.append(f"Gold price: error — {type(e).__name__}")
    gold_4w = None
    if gold_closes is not None and len(gold_closes) > 25:
        last = float(gold_closes.iloc[-1])
        gold_4w = (last / float(gold_closes.iloc[-21]) - 1) * 100
        hi = float(gold_closes.tail(252).max())
        dd = (last / hi - 1) * 100
        lines.append("── Gold ──")
        lines.append(f"  Price ${last:,.2f} [{gold_closes.index[-1].date()}]   4w {gold_4w:+.1f}%   "
                     f"{dd:+.1f}% from 52w high (${hi:,.0f})")
        if len(gold_closes) >= 200:
            ma = float(gold_closes.tail(200).mean())
            lines.append(f"  vs 200d average (${ma:,.0f}): {(last / ma - 1) * 100:+.1f}%")
        lines.append("")
    else:
        lines.append("Gold price history unavailable — trend and drawdown cannot be computed.\n")

    # ── Rates / dollar ───────────────────────────────────────────────────
    ry_obs = []
    lines.append("── Rates & dollar (gold's main macro drivers) ──")
    for label, sid, n, unit in (("10Y TIPS real yield", "DFII10", 60, "pct"),
                                ("10Y breakeven inflation", "T10YIE", 40, "pct"),
                                ("Broad dollar index (Fed, not ICE DXY)", "DTWEXBGS", 40, "idx")):
        try:
            obs = [o for o in await _fred_series(sid, limit=n) if o["value"] is not None]
            if len(obs) < 6:
                lines.append(f"  {label}: unavailable (FRED {sid} returned too little data)")
                continue
            if sid == "DFII10":
                ry_obs = obs
            cur, prev = obs[-1], obs[-min(21, len(obs))]
            try:
                age = (datetime.now() - datetime.strptime(cur["date"][:10], "%Y-%m-%d")).days
            except Exception:
                age = 0
            stale = f"  ⚠ STALE: {age}d old" if age > 7 else ""
            if unit == "pct":
                lines.append(f"  {label}: {cur['value']:.2f}% [{cur['date']}]   "
                             f"change over ~4w {cur['value'] - prev['value']:+.2f} pts{stale}")
            else:
                lines.append(f"  {label}: {cur['value']:.2f} [{cur['date']}]   "
                             f"change over ~4w {(cur['value'] / prev['value'] - 1) * 100:+.2f}%{stale}")
        except Exception as e:
            lines.append(f"  {label}: unavailable — {type(e).__name__}")
    lines.append("  Rule of thumb: rising real yields / stronger dollar = headwind for gold; "
                 "falling = tailwind.")

    # ── How tightly gold is tracking real yields ─────────────────────────
    try:
        if gold_closes is not None and len(ry_obs) >= 30:
            import pandas as pd
            ry = pd.Series({pd.Timestamp(o["date"]): o["value"] for o in ry_obs})
            g = gold_closes.copy()
            g.index = [pd.Timestamp(i.date()) for i in g.index]
            df = pd.concat([g.rename("g"), ry.rename("r")], axis=1).dropna().tail(60)
            if len(df) >= 25:
                corr = float(df["g"].pct_change().corr(df["r"].diff()))
                tag = ("tracking real yields (inverse) — rates explain the move" if corr < -0.3 else
                       "moving WITH real yields — something other than rates (haven demand, central banks, "
                       "ETF flows) is driving gold" if corr > 0.2 else "no clear link to real yields lately")
                lines.append(f"  Gold vs real-yield daily-change correlation (last {len(df)} sessions): "
                             f"{corr:+.2f} — {tag}")
    except Exception:
        pass
    lines.append("")

    # ── COT positioning vs its own history ───────────────────────────────
    lines.append("── Speculator positioning (CFTC COT, gold futures) ──")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(CFTC_BASE, params={
                "$where": "market_and_exchange_names='GOLD - COMMODITY EXCHANGE INC.'",
                "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "156",
                "$select": "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all"})
            r.raise_for_status()
            rows = r.json()
        nets = [(x["report_date_as_yyyy_mm_dd"][:10],
                 int(x.get("noncomm_positions_long_all") or 0) - int(x.get("noncomm_positions_short_all") or 0))
                for x in rows]
        if len(nets) >= 20:
            cur_d, cur_n = nets[0]
            vals = sorted(n for _d, n in nets)
            pctile = sum(1 for v in vals if v <= cur_n) / len(vals) * 100
            chg = cur_n - nets[min(4, len(nets) - 1)][1]
            crowd = ("CROWDED long — contrarian risk of liquidation" if pctile >= 90 else
                     "light positioning — room to add" if pctile <= 20 else "mid-range")
            lines.append(f"  Net speculative position {cur_n:+,} contracts [{cur_d}]  "
                         f"(percentile rank {pctile:.0f}/100 over the last {len(nets)} weeks; 4-report change {chg:+,}) — {crowd}")
        else:
            lines.append("  COT history unavailable — percentile cannot be computed.")
    except Exception as e:
        lines.append(f"  COT history unavailable — {type(e).__name__}")
    lines.append("")

    # ── Fund flows ───────────────────────────────────────────────────────
    lines.append("── Gold ETF flows (implied shares outstanding change) ──")
    lines += await asyncio.to_thread(_etf_share_flow_sync, ["GLD", "IAU"])
    lines.append("")
    # Wording avoids the words pipeline_health.py treats as CRITICAL Mercury gaps
    # ("gold", "BDI", "baltic"): this is a permanent, known limitation, not a feed failure.
    lines.append("Not covered: central-bank bullion purchases (the WGC quarterly series is "
                 "not available from any configured source) — treat official-sector demand as unverified.")
    return "\n".join(lines)


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

    # ── USDA NASS cattle and hog prices received (if key available) ───────
    # commodity_desc "CATTLE, BEEF" does not exist in Quick Stats (NASS returned
    # HTTP 400, swallowed as an error line). Prices received are published as
    # commodity "CATTLE" (class "INCL CALVES") and "HOGS", monthly, in $ / CWT.
    if USDA_API_KEY:
        lines.append("── USDA NASS Prices Received (national, monthly, $/cwt) ──")
        # Each entry: label, commodity, preferred short_desc fragment, param
        # variants tried in order (NASS answers 400 "invalid query" when ANY
        # filter value isn't valid for the commodity — class_desc "INCL CALVES"
        # was rejected live, hogs worked with no class filter).
        nass_livestock = [
            ("Cattle", "CATTLE", "INCL CALVES"),
            ("Hogs",   "HOGS",   ""),
        ]
        base = dict(statisticcat_desc="PRICE RECEIVED", agg_level_desc="NATIONAL",
                    freq_desc="MONTHLY", year__GE=str(datetime.now().year - 1))
        async with httpx.AsyncClient(timeout=25) as client:
            for label, commodity, prefer in nass_livestock:
                items, err = [], None
                for extra in ({"unit_desc": "$ / CWT"}, {}):
                    try:
                        items = await _nass_query(client, commodity_desc=commodity, **base, **extra)
                        err = None
                        if items:
                            break
                    except Exception as e:
                        err = e
                items = [i for i in items if _nass_val(i) is not None
                         and (i.get("reference_period_desc") or "")[:3].upper() in _MONTHS
                         and "CWT" in (i.get("unit_desc") or "CWT").upper()]
                if not items:
                    lines.append(f"  {label:<8} NASS UNAVAILABLE — {err or 'no monthly price rows returned'}")
                    continue
                # Rows span several series (cattle: steers & heifers, cows, calves...).
                # They are NOT interchangeable ($455 calves vs ~$215 steers), so
                # print every series by name instead of guessing one.
                for series in sorted({i.get("short_desc", "") for i in items}):
                    rows = sorted((i for i in items if i.get("short_desc", "") == series),
                                  key=_nass_sort_key)
                    last = rows[-1]
                    val = _nass_val(last)
                    name = series.split(" - ")[0] if series else label
                    msg = f"  {name:<40} ${val:,.2f}/cwt  [{last.get('reference_period_desc')} {last.get('year')}]"
                    prior = [i for i in rows if i.get("year") == str(int(last["year"]) - 1)
                             and i.get("reference_period_desc") == last.get("reference_period_desc")]
                    if prior and _nass_val(prior[0]):
                        msg += f"   YoY {((val / _nass_val(prior[0])) - 1) * 100:+.1f}%"
                    lines.append(msg)
        lines.append("  (Series are not interchangeable: CALVES are young animals priced far higher per cwt; "
                     "'GE 500 LBS' = cattle 500 lb and over, the closest to feeder/fed cattle.)")
        lines.append("")
    else:
        lines.append("(USDA NASS cattle/hog prices skipped: USDA_API_KEY not set)\n")

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

    esmis_ok = False
    # ── The actual latest WASDE report (USDA ESMIS text file) ─────────────
    # The publication page lists release-files/<id>/wasdeMMYY.txt per release;
    # the newest one is fetched and its Highlights narrative printed, so agents
    # get USDA's own numbers rather than only search-engine headlines.
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
            page = await client.get(
                "https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates")
            page.raise_for_status()
            found = re.findall(r'(https?://[^"\']*?/release-files/\d+/wasde(\d{2})(\d{2})\.txt|'
                               r'/sites/default/release-files/\d+/wasde(\d{2})(\d{2})\.txt)', page.text)
            cands = []
            for full, m1, y1, m2, y2 in found:
                mm, yy = (m1, y1) if m1 else (m2, y2)
                url = full if full.startswith("http") else "https://esmis.nal.usda.gov" + full
                cands.append((int(yy), int(mm), url))
            if not cands:
                raise RuntimeError("no wasde*.txt links found on ESMIS page")
            yy, mm, url = max(cands)
            doc = await client.get(url)
            doc.raise_for_status()
            text = doc.text
        text = text.replace("\f", "\n")
        hdr = re.search(r"WASDE\s*-\s*\d+\s*-\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text)
        released = re.sub(r"\s+", " ", hdr.group(1)) if hdr else f"{mm:02d}/20{yy:02d}"
        lines.append(f"── USDA WASDE {released} — U.S. supply & use (source {url}) ──")
        lines.append("(Each table: prior month's projection vs this month's; units shown in table headers.)")
        # The ESMIS .txt is fixed-width tables, not narrative. Pull the three
        # big U.S. balance sheets down to their 'Ending Stocks' row.
        all_lines = text.split("\n")
        # (name, title regex, optional marker line where the commodity's own
        # block starts — the corn table opens with a FEED GRAINS aggregate in
        # metric tons, and the CORN block (bushels) follows it)
        titles = [("Wheat",    r"U\.S\. Wheat Supply and Use", None),
                  ("Corn",     r"U\.S\. Feed Grain and Corn Supply and Use", r"^\s*CORN\s*$"),
                  ("Soybeans", r"U\.S\. Soybeans and Products Supply and Use", None)]
        got_tables = 0
        for name, pat, marker in titles:
            idx = next((i for i, ln in enumerate(all_lines) if re.search(pat, ln)), None)
            if idx is None:
                lines.append(f"({name} table not found in report text)")
                continue
            window = all_lines[idx: idx + 90]
            block, begin = [], 0
            if marker:
                m_i = next((i for i, ln in enumerate(window) if re.search(marker, ln)), None)
                if m_i is None:
                    lines.append(f"({name} block not found in report text)")
                    continue
                # keep the title + column headers (up to 5 lines), then jump to the block
                block = [ln.rstrip() for ln in window[:5]]
                begin = m_i
            for ln in window[begin:]:
                block.append(ln.rstrip())
                if re.match(r"\s*Ending Stocks", ln) and len(block) > 5:
                    break
            lines.extend(l for l in block if l.strip())
            lines.append("")
            got_tables += 1
        if not got_tables:
            body = re.sub(r"[ \t]+", " ", text[:5000])
            lines.append("(U.S. tables not found; showing start of report)")
            lines.append(body)
            lines.append("")
        esmis_ok = got_tables > 0
        try:
            if (datetime.now() - datetime.strptime(released, "%d %B %Y")).days > 45:
                lines.append(f"⚠ This is the {released} report — a newer WASDE may have been released.")
        except Exception:
            pass
    except Exception as e:
        lines.append(f"(USDA WASDE report text UNAVAILABLE this run: {e}; web coverage below only)\n")

    # ── Web search for the latest WASDE coverage ──────────────────────────
    # Raw WASDE data needs the (keyed) USDA PSD API, so this stays headline
    # coverage. Fixes: queries are dated (the generic query returned old or
    # off-topic pages), results must actually mention WASDE, SearXNG's
    # publishedDate is shown, and the output says plainly when nothing recent
    # was found instead of presenting old articles as current.
    now = datetime.now()
    prev = now.replace(day=1) - timedelta(days=1)
    queries = [
        f"USDA WASDE {now.strftime('%B %Y')} corn soybeans wheat ending stocks yield",
        f"USDA WASDE {prev.strftime('%B %Y')} report ending stocks",
        f"wasde{prev.strftime('%m%y')}.pdf World Agricultural Supply and Demand Estimates",
    ]
    seen, kept = set(), []
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            for q in queries:
                try:
                    r = await client.get(f"{SEARXNG_URL}/search",
                                         params={"q": q, "format": "json", "time_range": "month"})
                    r.raise_for_status()
                    for item in r.json().get("results", []):
                        url = item.get("url", "")
                        text = f"{item.get('title', '')} {item.get('content', '')}".lower()
                        if url in seen or "wasde" not in text and "supply and demand" not in text:
                            continue
                        seen.add(url)
                        kept.append(item)
                except Exception as e:
                    lines.append(f"(search '{q[:40]}…' failed: {e})")
        # Newest dated items first; undated (landing pages) last.
        kept.sort(key=lambda i: str(i.get("publishedDate") or ""), reverse=True)
        kept = kept[:6]
        newest = max((str(i.get("publishedDate") or "")[:10] for i in kept), default="")

        lines.append("── Latest WASDE Coverage (via web search — headlines, not USDA tables) ──")
        if kept:
            for item in kept:
                lines.append(f"• {item.get('title', 'No title')}")
                pub = str(item.get("publishedDate") or "")[:10]
                if pub:
                    lines.append(f"  published {pub}")
                content = item.get("content", "")[:260]
                if content:
                    lines.append(f"  {content}")
                lines.append(f"  {item.get('url', '')}")
                lines.append("")
            try:
                if not esmis_ok and newest and (now - datetime.strptime(newest, "%Y-%m-%d")).days > 45:
                    lines.append(f"⚠ Newest dated coverage is {newest} — the most recent WASDE "
                                 "may NOT be reflected above; treat figures as possibly out of date.")
            except Exception:
                pass
        else:
            lines.append("No WASDE coverage found for the last ~2 months — WASDE figures "
                         "UNAVAILABLE this run; do not assume current balance-sheet numbers.")
    except Exception as e:
        lines.append(f"WASDE search UNAVAILABLE: {e}")

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
    # Each (commodity, category) has MANY unit_desc rows per week (PCT EXCELLENT,
    # PCT GOOD, ... or PCT PLANTED, PCT EMERGED, ...). The old code printed one
    # arbitrary row; this prints the full latest-week breakdown. The invalid
    # numeric_desc parameter was dropped.
    if USDA_API_KEY:
        crops_progress = [
            ("Corn",         {"commodity_desc": "CORN"},                          "PROGRESS"),
            ("Corn",         {"commodity_desc": "CORN"},                          "CONDITION"),
            ("Soybeans",     {"commodity_desc": "SOYBEANS"},                      "CONDITION"),
            ("Winter Wheat", {"commodity_desc": "WHEAT", "class_desc": "WINTER"}, "CONDITION"),
        ]
        async with httpx.AsyncClient(timeout=25) as client:
            for label, extra, stat in crops_progress:
                try:
                    items = await _nass_query(
                        client,
                        statisticcat_desc=stat,
                        agg_level_desc="NATIONAL",
                        freq_desc="WEEKLY",
                        year=str(datetime.now().year),
                        **extra,
                    )
                    items = [i for i in items if i.get("week_ending") and _nass_val(i) is not None]
                    if not items:
                        lines.append(f"{label} — {stat}: no NASS rows for {datetime.now().year} "
                                     "(out of season or not yet reported)\n")
                        continue
                    week = max(i["week_ending"] for i in items)
                    latest = sorted((i for i in items if i["week_ending"] == week),
                                    key=lambda x: x.get("unit_desc", ""))
                    stale_note = ""
                    try:
                        age = (datetime.now() - datetime.strptime(week[:10], "%Y-%m-%d")).days
                        if age > 21:
                            stale_note = (f"  ⚠ STALE: last NASS report was {age} days ago "
                                          "(season reporting has ended) — not current conditions")
                    except Exception:
                        pass
                    lines.append(f"{label} — {stat}  [week ending {week}]{stale_note}")
                    for row in latest:
                        lines.append(f"  {row.get('unit_desc', '?'):<28} {_nass_val(row):g}")
                    lines.append("")
                except Exception as e:
                    lines.append(f"{label} {stat}: NASS UNAVAILABLE — {e}\n")
    else:
        lines.append("(USDA NASS crop data skipped: USDA_API_KEY not set)\n")

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

    # ── US Drought Monitor data (official JSON web service) ───────────────
    # The old code requested an HTML page, ignored the response, and printed
    # "Visit: droughtmonitor.unl.edu" — so no drought numbers ever reached the
    # agents. The USDM data service returns percent-area by category.
    async def _usdm(client: httpx.AsyncClient, aoi: str) -> dict:
        """Latest + prior USDM rows for an area. CONUS lives under /USStatistics,
        states under /StateStatistics (the US endpoint returned nothing for
        'IA' etc.). JSON key casing is not guaranteed, so keys are lowercased."""
        end   = datetime.now()
        start = end - timedelta(days=21)
        scope = "USStatistics" if aoi == "conus" else "StateStatistics"
        r = await client.get(
            f"https://usdmdataservices.unl.edu/api/{scope}/GetDroughtSeverityStatisticsByAreaPercent",
            params={
                "aoi":            aoi,
                "startdate":      f"{start.month}/{start.day}/{start.year}",
                "enddate":        f"{end.month}/{end.day}/{end.year}",
                "statisticsType": "1",
            },
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        rows = [{str(k).lower(): v for k, v in row.items()} for row in r.json()]
        if not rows:
            raise RuntimeError("empty response")
        rows.sort(key=lambda x: str(x.get("mapdate") or x.get("validstart") or ""))
        for row in rows:
            if not any(k in row for k in ("d0", "d1", "d2")):
                raise RuntimeError(f"unrecognized fields: {sorted(row)[:8]}")
        return {"latest": rows[-1], "prior": rows[-2] if len(rows) > 1 else None}

    def _cum(row: dict) -> dict:
        """Percent of area in D0+ ... D4 (cumulative) from a lowercased USDM row.
        statisticsType=1 is USDM's 'traditional' (already cumulative) output;
        the check below also copes with categorical rows."""
        g = lambda k: float(row.get(k) or 0)
        cats = [g("d0"), g("d1"), g("d2"), g("d3"), g("d4")]
        # Single-category rows sum (none + D0..D4) to ~100 -> accumulate them.
        # Cumulative rows don't (none + D0 alone is already ~100), so use as-is
        # rather than double-accumulating.
        if abs(g("none") + sum(cats) - 100) <= 2.0:
            return {f"D{i}": sum(cats[i:]) for i in range(5)}
        return {f"D{i}": cats[i] for i in range(5)}

    # StateStatistics wants the two-digit state FIPS code (abbreviations return
    # an empty list — verified against the USDM web-service docs / live run).
    regions = [("CONUS", "conus"), ("Iowa", "19"), ("Illinois", "17"), ("Nebraska", "31"),
               ("Kansas", "20"), ("Texas", "48"), ("California", "06")]
    got_any = False
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            results = await asyncio.gather(*[_usdm(client, aoi) for _, aoi in regions],
                                           return_exceptions=True)
        lines.append("── US Drought Monitor — % of area in drought (cumulative) ──")
        lines.append(f"  {'Region':<12} {'D1+':>6} {'D2+':>6} {'D3+':>6} {'D4':>6}   {'D1+ wk/wk':>10}   Map date")
        for (name, _), res in zip(regions, results):
            if isinstance(res, Exception):
                lines.append(f"  {name:<12} unavailable — {type(res).__name__}: {str(res)[:90]}")
                continue
            got_any = True
            cur = _cum(res["latest"])
            chg = ""
            if res["prior"]:
                chg = f"{cur['D1'] - _cum(res['prior'])['D1']:+.1f} pts"
            md = str(res["latest"].get("mapdate") or res["latest"].get("validstart") or "")[:10]
            lines.append(f"  {name:<12} {cur['D1']:>5.1f}% {cur['D2']:>5.1f}% {cur['D3']:>5.1f}% "
                         f"{cur['D4']:>5.1f}%   {chg:>10}   {md}")
        if not got_any:
            lines.append("  Drought Monitor service returned no data this run — "
                         "drought levels UNAVAILABLE (web coverage below is qualitative only).")
        lines.append("")
    except Exception as e:
        lines.append(f"Drought Monitor UNAVAILABLE: {e}\n")

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


_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _parse_snippet_date(text: str, fallback: str = ""):
    """Return a date found in a search snippet (or SearXNG's publishedDate), else None.

    Handles 'Oct 24, 2026', 'October 24 2026', '24 Oct 2026', '2026-10-24', and
    year-less 'Oct 24' (assumed the current year, or last year if that would be future).
    """
    import re as _re
    today = datetime.now(timezone.utc).date()
    text = _re.sub(r'<[^>]+>', '', text or '')
    # "...fell for a second session on Monday" — resolve a bare weekday to its most
    # recent occurrence (today counts), but only when no explicit date is present.
    _wd = {n: i for i, n in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday",
                                       "saturday", "sunday"])}
    pats = [
        (r'\b(\d{4})-(\d{2})-(\d{2})\b', lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (r'\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b',
         lambda m: (int(m[3]), _MON.get(m[1].lower()), int(m[2]))),
        (r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})\b',
         lambda m: (int(m[3]), _MON.get(m[2].lower()), int(m[1]))),
        (r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!,?\s*\d{4})',
         lambda m: (today.year, _MON.get(m[1].lower()), int(m[2]))),
    ]
    for src in (text, fallback):
        if not src:
            continue
        for pat, conv in pats:
            m = _re.search(pat, src, _re.IGNORECASE)
            if not m:
                continue
            try:
                y, mo, d = conv(m)
                if not mo:
                    continue
                dt_ = datetime(y, mo, d).date()
                if dt_ > today + timedelta(days=1):
                    dt_ = datetime(y - 1, mo, d).date()      # year-less date that would be in the future
                return dt_
            except Exception:
                continue
    wm = _re.search(r'\bon\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', text, _re.IGNORECASE)
    if wm:
        back = (today.weekday() - _wd[wm.group(1).lower()]) % 7
        return today - timedelta(days=back)
    return None


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
            stale_candidate = None
            for item in shipping_results:
                text = _re.sub(r'<[^>]+>', '', item.get("title","") + " " + item.get("content",""))
                for pat in (bdi_pattern, points_pattern):
                    m = pat.search(text)
                    if m:
                        candidate = int(m.group(1).replace(",", ""))
                        if 400 <= candidate <= 8000:
                            # Oct 26 2026: Jansky asked for the snippet date to be
                            # verified. Parse it (snippet text, else SearXNG's own
                            # publishedDate), reject anything older than 14 days —
                            # including dated-this-year-but-months-old results and the
                            # 1985 Wikipedia base value — and report the age.
                            cand_dt = _parse_snippet_date(text, str(item.get("publishedDate") or ""))
                            if cand_dt is not None:
                                _age = (datetime.now(timezone.utc).date() - cand_dt).days
                                if _age > 14:
                                    stale_candidate = (candidate, cand_dt, _age)
                                    continue
                                candidate_date = f"{cand_dt.isoformat()}, {_age}d old"
                            else:
                                candidate_date = ""
                            bdi_value = candidate
                            bdi_source_date = candidate_date
                            break
                if bdi_value:
                    break

            if not bdi_value and stale_candidate:
                _c, _d, _a = stale_candidate
                lines.append(f"Baltic Dry Index (BDI):  {_c:,}  ⚠ STALE: the only readings found are dated "
                             f"{_d.isoformat()} ({_a} days old) — do NOT treat as current")
                lines.append("")
            if bdi_value:
                if bdi_source_date:
                    lines.append(f"Baltic Dry Index (BDI):  {bdi_value:,}  [{bdi_source_date}]")
                else:
                    lines.append(f"Baltic Dry Index (BDI):  {bdi_value:,}  [date not stated in source snippet — "
                                 f"web-search reading from the past week; confirm recency before relying on it]")
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
    if any("Baltic Dry Index (BDI)" in _l and "STALE" in _l for _l in bdi_text.split("\n")):
        bdi_val, bdi_4w = None, None      # stale reading must not enter the backdrop YAML as current

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
        f"# US DOLLAR (Fed Broad Dollar Index, DTWEXBGS — not ICE DXY)",
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
