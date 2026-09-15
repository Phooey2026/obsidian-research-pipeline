"""
macromcp - MCP server for macroeconomic data collection
Provides tools for interest rates, inflation, employment, producer prices,
consumer prices, GDP, and Fed policy signals.

Companion to webmcp (app.py). Run on a different port (8643).
Feeds weekly_macro.py to produce:
  - macro_summary_YYYYMMDD.md   (human-readable weekly economic report)
  - macro_backdrop_YYYYMMDD.yaml (structured AI context file)

Data sources:
  - FRED (Federal Reserve Bank of St. Louis) — free, no key required for
    most series; FRED_API_KEY in .env unlocks higher rate limits
  - BLS Public Data API v2 — free, optional BLS_API_KEY for higher limits
  - US Treasury FiscalData API — no key required
  - Yahoo Finance (yfinance) — yield curve, market-based inflation expectations
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import yfinance as yf
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

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")   # Optional — raises rate limit
BLS_API_KEY  = os.environ.get("BLS_API_KEY", "")    # Optional — raises rate limit

FRED_BASE    = "https://api.stlouisfed.org/fred/series/observations"
BLS_BASE     = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
TREASURY_BASE = "https://api.fiscaldata.treasury.gov/services/api/v1"
SEARXNG_URL  = os.environ.get("SEARXNG_URL", "http://localhost:8080")

# ============================================================================
# Internal helpers
# ============================================================================


async def _fred_series(series_id: str, limit: int = 12,
                        frequency: str = "") -> list[dict]:
    """
    Fetch the most recent `limit` observations for a FRED series.
    Returns list of {"date": "YYYY-MM-DD", "value": float|None}.
    frequency: optional aggregation override — "m", "q", "a"
    """
    params: dict = {
        "series_id": series_id,
        "sort_order": "desc",
        "limit": limit,
        "file_type": "json",
    }
    if FRED_API_KEY:
        params["api_key"] = FRED_API_KEY
    else:
        # FRED allows anonymous access with reduced rate limits
        params["api_key"] = "FRED_ANONYMOUS"  # triggers anonymous path
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

    # Return oldest-first for readability
    return list(reversed(result))


async def _bls_series(series_ids: list[str], years: int = 2) -> dict[str, list[dict]]:
    """
    Fetch BLS series for the past `years` years.
    Returns {series_id: [{"year": str, "period": str, "value": float}]}.
    """
    current_year = datetime.now().year
    start_year = current_year - years

    payload: dict = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(current_year),
    }
    if BLS_API_KEY:
        payload["registrationkey"] = BLS_API_KEY

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(BLS_BASE, json=payload)
        r.raise_for_status()
        data = r.json()

    result: dict = {}
    for series in data.get("Results", {}).get("series", []):
        sid = series["seriesID"]
        rows = []
        for item in series.get("data", []):
            try:
                val = float(item["value"].replace(",", ""))
            except (ValueError, TypeError, KeyError):
                val = None
            rows.append({
                "year":   item.get("year"),
                "period": item.get("period"),
                "label":  item.get("periodName", ""),
                "value":  val,
            })
        # BLS returns newest-first; reverse for chronological order
        result[sid] = list(reversed(rows))

    return result


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
    "macromcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


# ── Interest Rates ────────────────────────────────────────────────────────────

@mcp.tool()
async def get_interest_rates() -> str:
    """
    Fetch current US interest rate data: Fed Funds rate (target range),
    SOFR, prime rate, and recent FOMC meeting history.

    Sources: FRED (DFF, SOFR, DPRIME, FEDFUNDS)
    """
    lines = ["=== INTEREST RATES ===\n"]

    try:
        # Effective Fed Funds Rate (daily)
        ff = await _fred_series("DFF", limit=30)
        val, date = _latest(ff)
        lines.append(f"Fed Funds Rate (Effective):  {val:.2f}%  [{date}]")

        # FOMC target rate upper bound
        ff_upper = await _fred_series("DFEDTARU", limit=6)
        ub, ub_date = _latest(ff_upper)
        ff_lower = await _fred_series("DFEDTARL", limit=6)
        lb, lb_date = _latest(ff_lower)
        if ub is not None and lb is not None:
            lines.append(f"FOMC Target Range:           {lb:.2f}% – {ub:.2f}%  [{ub_date}]")

        # SOFR (Secured Overnight Financing Rate) — 30-day avg
        sofr = await _fred_series("SOFR30DAYAVG", limit=10)
        sv, sd = _latest(sofr)
        if sv is not None:
            lines.append(f"SOFR (30-day avg):           {sv:.2f}%  [{sd}]")

        # Prime Rate
        prime = await _fred_series("DPRIME", limit=6)
        pv, pd_ = _latest(prime)
        lines.append(f"Prime Rate:                  {pv:.2f}%  [{pd_}]")

        # 3-month T-bill
        tbill = await _fred_series("DTB3", limit=10)
        tv, td = _latest(tbill)
        if tv is not None:
            lines.append(f"3-Month T-Bill:              {tv:.2f}%  [{td}]")

        # 6-month history of effective rate for trend
        lines.append("\nFed Funds Rate (last 12 obs):")
        for obs in ff[-12:]:
            if obs["value"] is not None:
                lines.append(f"  {obs['date']}  {obs['value']:.2f}%")

        lines.append("\nSource: FRED (Federal Reserve Bank of St. Louis)")
    except Exception as e:
        lines.append(f"Error fetching interest rate data: {e}")

    return "\n".join(lines)


@mcp.tool()
async def get_yield_curve() -> str:
    """
    Fetch the US Treasury yield curve: 1M, 3M, 6M, 1Y, 2Y, 5Y, 10Y, 20Y, 30Y yields.
    Computes the 2Y/10Y spread (key recession indicator) and 3M/10Y spread.

    Sources: FRED constant maturity treasury series (DGS*)
    """
    lines = ["=== YIELD CURVE ===\n"]

    series_map = {
        "1-Month":  "DGS1MO",
        "3-Month":  "DGS3MO",
        "6-Month":  "DGS6MO",
        "1-Year":   "DGS1",
        "2-Year":   "DGS2",
        "5-Year":   "DGS5",
        "10-Year":  "DGS10",
        "20-Year":  "DGS20",
        "30-Year":  "DGS30",
    }

    yields: dict[str, float] = {}

    try:
        for label, series_id in series_map.items():
            obs = await _fred_series(series_id, limit=5)
            val, date = _latest(obs)
            if val is not None:
                yields[label] = val
                lines.append(f"  {label:<10} {val:.2f}%   [{date}]")
            else:
                lines.append(f"  {label:<10} N/A")

        # Spread calculations
        lines.append("\nKey Spreads (basis points):")
        y2  = yields.get("2-Year")
        y10 = yields.get("10-Year")
        y3m = yields.get("3-Month")

        if y2 and y10:
            spread_2_10 = (y10 - y2) * 100
            inverted = "⚠ INVERTED" if spread_2_10 < 0 else "normal"
            lines.append(f"  2Y/10Y spread:   {spread_2_10:+.1f} bps  ({inverted})")

        if y3m and y10:
            spread_3m_10 = (y10 - y3m) * 100
            inverted_3m = "⚠ INVERTED" if spread_3m_10 < 0 else "normal"
            lines.append(f"  3M/10Y spread:   {spread_3m_10:+.1f} bps  ({inverted_3m})")

        if y2 and y10:
            shape = ("Steep (bullish growth signal)" if spread_2_10 > 100
                     else "Flat (uncertainty)" if -25 < spread_2_10 <= 100
                     else "Inverted (recession risk)" if spread_2_10 <= -25
                     else "Mildly inverted")
            lines.append(f"\nCurve shape: {shape}")

        lines.append("\nSource: FRED constant maturity Treasury yields")
    except Exception as e:
        lines.append(f"Error fetching yield curve: {e}")

    return "\n".join(lines)


# ── Inflation ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_cpi_data() -> str:
    """
    Fetch Consumer Price Index data: headline CPI, core CPI (ex food & energy),
    shelter CPI, and month-over-month / year-over-year percent changes.

    Sources: FRED (CPIAUCSL, CPILFESL, CUSR0000SAH1)
    """
    lines = ["=== CONSUMER PRICE INDEX (CPI) ===\n"]

    try:
        # Headline CPI (All Urban Consumers, All Items)
        cpi = await _fred_series("CPIAUCSL", limit=24, frequency="m")
        val, date = _latest(cpi)
        mom = _pct_change(cpi, 1)
        yoy = _pct_change(cpi, 12)
        lines.append(f"Headline CPI:     {val:.3f}  [{date}]")
        if mom is not None:
            lines.append(f"  MoM change:     {mom:+.2f}%")
        if yoy is not None:
            lines.append(f"  YoY change:     {yoy:+.2f}%")

        # Core CPI (Less Food & Energy)
        core = await _fred_series("CPILFESL", limit=24, frequency="m")
        cval, cdate = _latest(core)
        cmom = _pct_change(core, 1)
        cyoy = _pct_change(core, 12)
        lines.append(f"\nCore CPI:         {cval:.3f}  [{cdate}]")
        if cmom is not None:
            lines.append(f"  MoM change:     {cmom:+.2f}%")
        if cyoy is not None:
            lines.append(f"  YoY change:     {cyoy:+.2f}%")

        # Shelter CPI
        shelter = await _fred_series("CUSR0000SAH1", limit=24, frequency="m")
        sval, sdate = _latest(shelter)
        syoy = _pct_change(shelter, 12)
        lines.append(f"\nShelter CPI:      {sval:.3f}  [{sdate}]")
        if syoy is not None:
            lines.append(f"  YoY change:     {syoy:+.2f}%")

        # 12-month headline CPI history
        lines.append("\nHeadline CPI YoY trend (last 12 months):")
        vals = [o for o in cpi if o["value"] is not None]
        for i in range(max(0, len(vals) - 12), len(vals)):
            if i >= 12:
                obs = vals[i]
                yoy_i = ((obs["value"] - vals[i-12]["value"]) / vals[i-12]["value"]) * 100
                lines.append(f"  {obs['date']}  {yoy_i:+.2f}%")

        lines.append("\nFed target: 2.0% YoY PCE (CPI typically runs ~30-50 bps higher)")
        lines.append("Source: FRED — Bureau of Labor Statistics via FRED")
    except Exception as e:
        lines.append(f"Error fetching CPI data: {e}")

    return "\n".join(lines)


@mcp.tool()
async def get_pce_data() -> str:
    """
    Fetch PCE (Personal Consumption Expenditures) inflation data — the Fed's
    preferred inflation gauge. Returns headline PCE, core PCE, and trend.

    Sources: FRED (PCEPI, PCEPILFE)
    """
    lines = ["=== PCE INFLATION (Fed's Preferred Gauge) ===\n"]

    try:
        # Headline PCE
        pce = await _fred_series("PCEPI", limit=24, frequency="m")
        val, date = _latest(pce)
        mom = _pct_change(pce, 1)
        yoy = _pct_change(pce, 12)
        lines.append(f"Headline PCE:     {val:.3f}  [{date}]")
        if mom is not None:
            lines.append(f"  MoM change:     {mom:+.2f}%")
        if yoy is not None:
            lines.append(f"  YoY change:     {yoy:+.2f}%  (Fed target: 2.0%)")

        # Core PCE (Less Food & Energy)
        core = await _fred_series("PCEPILFE", limit=24, frequency="m")
        cval, cdate = _latest(core)
        cmom = _pct_change(core, 1)
        cyoy = _pct_change(core, 12)
        lines.append(f"\nCore PCE:         {cval:.3f}  [{cdate}]")
        if cmom is not None:
            lines.append(f"  MoM change:     {cmom:+.2f}%")
        if cyoy is not None:
            lines.append(f"  YoY change:     {cyoy:+.2f}%")

        # Trend
        lines.append("\nCore PCE YoY trend (last 12 months):")
        vals = [o for o in core if o["value"] is not None]
        for i in range(max(0, len(vals) - 12), len(vals)):
            if i >= 12:
                obs = vals[i]
                yoy_i = ((obs["value"] - vals[i-12]["value"]) / vals[i-12]["value"]) * 100
                marker = " ← latest" if i == len(vals) - 1 else ""
                lines.append(f"  {obs['date']}  {yoy_i:+.2f}%{marker}")

        lines.append("\nSource: FRED — Bureau of Economic Analysis via FRED")
    except Exception as e:
        lines.append(f"Error fetching PCE data: {e}")

    return "\n".join(lines)


@mcp.tool()
async def get_ppi_data() -> str:
    """
    Fetch Producer Price Index data: final demand PPI, core PPI (ex food &
    energy), and goods vs services breakdown. PPI leads CPI by 1-3 months.

    Sources: FRED (PPIACO, PPIFES, PPIFGS, PPIFID)
    """
    lines = ["=== PRODUCER PRICE INDEX (PPI) ===\n"]

    try:
        # All commodities PPI
        ppi_all = await _fred_series("PPIACO", limit=24, frequency="m")
        val, date = _latest(ppi_all)
        mom = _pct_change(ppi_all, 1)
        yoy = _pct_change(ppi_all, 12)
        lines.append(f"PPI All Commodities:    {val:.3f}  [{date}]")
        if mom is not None:
            lines.append(f"  MoM change:           {mom:+.2f}%")
        if yoy is not None:
            lines.append(f"  YoY change:           {yoy:+.2f}%")

        # Final Demand PPI (headline)
        ppi_fd = await _fred_series("PPIFID", limit=24, frequency="m")
        fval, fdate = _latest(ppi_fd)
        fmom = _pct_change(ppi_fd, 1)
        fyoy = _pct_change(ppi_fd, 12)
        lines.append(f"\nPPI Final Demand:       {fval:.3f}  [{fdate}]")
        if fmom is not None:
            lines.append(f"  MoM change:           {fmom:+.2f}%")
        if fyoy is not None:
            lines.append(f"  YoY change:           {fyoy:+.2f}%")

        # Final Demand ex Food & Energy (core PPI)
        ppi_core = await _fred_series("PPIFES", limit=24, frequency="m")
        cval, cdate = _latest(ppi_core)
        cyoy = _pct_change(ppi_core, 12)
        lines.append(f"\nPPI Core (ex Food/Energy): {cval:.3f}  [{cdate}]")
        if cyoy is not None:
            lines.append(f"  YoY change:           {cyoy:+.2f}%")

        # Final Demand Goods
        ppi_goods = await _fred_series("PPIFGS", limit=24, frequency="m")
        gval, gdate = _latest(ppi_goods)
        gyoy = _pct_change(ppi_goods, 12)
        lines.append(f"\nPPI Final Demand Goods: {gval:.3f}  [{gdate}]")
        if gyoy is not None:
            lines.append(f"  YoY change:           {gyoy:+.2f}%")

        lines.append("\nNote: PPI is a leading indicator of CPI by 1-3 months.")
        lines.append("Source: FRED — Bureau of Labor Statistics via FRED")
    except Exception as e:
        lines.append(f"Error fetching PPI data: {e}")

    return "\n".join(lines)


@mcp.tool()
async def get_inflation_expectations() -> str:
    """
    Fetch market-based and survey-based inflation expectations:
    5-year breakeven, 10-year breakeven, Michigan 1Y and 5Y expectations,
    and the Cleveland Fed inflation expectations model.

    Sources: FRED (T5YIE, T10YIE, MICH, EXPINF1YR, EXPINF5YR)
    """
    lines = ["=== INFLATION EXPECTATIONS ===\n"]

    try:
        # 5-year breakeven (market-based)
        be5 = await _fred_series("T5YIE", limit=10)
        v5, d5 = _latest(be5)
        lines.append(f"5Y Breakeven Inflation:          {v5:.2f}%  [{d5}]")

        # 10-year breakeven
        be10 = await _fred_series("T10YIE", limit=10)
        v10, d10 = _latest(be10)
        lines.append(f"10Y Breakeven Inflation:         {v10:.2f}%  [{d10}]")

        # Michigan 1-year ahead
        mich1 = await _fred_series("MICH", limit=6, frequency="m")
        vm, dm = _latest(mich1)
        lines.append(f"\nMichigan Survey 1Y Expectation: {vm:.1f}%  [{dm}]")

        # Cleveland Fed 1Y
        clev1 = await _fred_series("EXPINF1YR", limit=6, frequency="m")
        vc1, dc1 = _latest(clev1)
        lines.append(f"Cleveland Fed 1Y Expectation:   {vc1:.2f}%  [{dc1}]")

        # Cleveland Fed 5Y
        clev5 = await _fred_series("EXPINF5YR", limit=6, frequency="m")
        vc5, dc5 = _latest(clev5)
        lines.append(f"Cleveland Fed 5Y Expectation:   {vc5:.2f}%  [{dc5}]")

        # TIPS spread as proxy for long-run credibility
        if v5 and v10:
            lines.append(f"\nMarket Inflation Credibility:")
            if v10 > 2.5:
                lines.append("  ⚠ 10Y breakeven above 2.5% — market questions Fed credibility")
            elif v10 < 1.5:
                lines.append("  ⚠ 10Y breakeven below 1.5% — deflation concern")
            else:
                lines.append("  ✓ 10Y breakeven in 1.5-2.5% range — expectations anchored")

        lines.append("\nSource: FRED — TIPS market and survey data")
    except Exception as e:
        lines.append(f"Error fetching inflation expectations: {e}")

    return "\n".join(lines)


# ── Labor Market ─────────────────────────────────────────────────────────────

@mcp.tool()
async def get_jobs_data() -> str:
    """
    Fetch US labor market data: nonfarm payrolls (monthly change),
    unemployment rate, labor force participation rate, job openings (JOLTS),
    and initial jobless claims.

    Sources: FRED (PAYEMS, UNRATE, CIVPART, JTSJOL, ICSA)
    """
    lines = ["=== LABOR MARKET ===\n"]

    try:
        # Nonfarm Payrolls (total, monthly change)
        payrolls = await _fred_series("PAYEMS", limit=14, frequency="m")
        val, date = _latest(payrolls)
        chg = _pct_change(payrolls, 1)
        if val and chg is not None:
            chg_abs = (val - [o["value"] for o in payrolls if o["value"] is not None][-2]) * 1000
            lines.append(f"Nonfarm Payrolls:       {val/1000:.1f}M  [{date}]")
            lines.append(f"  Monthly change:       {chg_abs:+,.0f} jobs")

        # Unemployment Rate
        unemp = await _fred_series("UNRATE", limit=14, frequency="m")
        uval, udate = _latest(unemp)
        lines.append(f"\nUnemployment Rate:      {uval:.1f}%  [{udate}]")

        # Labor Force Participation
        lfpr = await _fred_series("CIVPART", limit=6, frequency="m")
        lval, ldate = _latest(lfpr)
        lines.append(f"Labor Force Participation: {lval:.1f}%  [{ldate}]")

        # U-6 (broader unemployment: underemployed + discouraged)
        u6 = await _fred_series("U6RATE", limit=6, frequency="m")
        u6val, u6date = _latest(u6)
        lines.append(f"U-6 Unemployment:       {u6val:.1f}%  [{u6date}]")

        # JOLTS Job Openings
        jolts = await _fred_series("JTSJOL", limit=6, frequency="m")
        jval, jdate = _latest(jolts)
        if jval:
            lines.append(f"\nJob Openings (JOLTS):   {jval/1000:.2f}M  [{jdate}]")
            # Openings / unemployed ratio (Beveridge curve signal)
            unemp_level = await _fred_series("UNEMPLOY", limit=6, frequency="m")
            ulval, _ = _latest(unemp_level)
            if ulval:
                ratio = jval / ulval
                lines.append(f"  Openings/Unemployed:  {ratio:.2f}x  (>1.0 = tight labor market)")

        # Initial Jobless Claims (weekly)
        claims = await _fred_series("ICSA", limit=8)
        cval, cdate = _latest(claims)
        lines.append(f"\nInitial Jobless Claims: {cval:,.0f}  [{cdate}]  (weekly)")

        # Continuing Claims
        cont = await _fred_series("CCSA", limit=6)
        ccval, ccdate = _latest(cont)
        lines.append(f"Continuing Claims:      {ccval:,.0f}  [{ccdate}]")

        # Unemployment trend
        lines.append("\nUnemployment Rate trend (last 12 months):")
        for obs in unemp[-12:]:
            if obs["value"] is not None:
                lines.append(f"  {obs['date']}  {obs['value']:.1f}%")

        lines.append("\nSource: FRED — BLS via FRED")
    except Exception as e:
        lines.append(f"Error fetching jobs data: {e}")

    return "\n".join(lines)


@mcp.tool()
async def get_wages_data() -> str:
    """
    Fetch US wage growth data: average hourly earnings (all workers,
    production workers), Employment Cost Index, and real wage growth.

    Sources: FRED (CES0500000003, AHETPI, ECIWAG)
    """
    lines = ["=== WAGE GROWTH ===\n"]

    try:
        # Average Hourly Earnings — All Private
        ahe = await _fred_series("CES0500000003", limit=24, frequency="m")
        val, date = _latest(ahe)
        mom = _pct_change(ahe, 1)
        yoy = _pct_change(ahe, 12)
        lines.append(f"Avg Hourly Earnings (All Private):  ${val:.2f}  [{date}]")
        if mom is not None:
            lines.append(f"  MoM change:                         {mom:+.2f}%")
        if yoy is not None:
            lines.append(f"  YoY change:                         {yoy:+.2f}%")

        # Average Hourly Earnings — Production & Nonsupervisory
        ahe_prod = await _fred_series("AHETPI", limit=24, frequency="m")
        pval, pdate = _latest(ahe_prod)
        pyoy = _pct_change(ahe_prod, 12)
        lines.append(f"\nAvg Hourly Earnings (Production):   ${pval:.2f}  [{pdate}]")
        if pyoy is not None:
            lines.append(f"  YoY change:                         {pyoy:+.2f}%")

        # Employment Cost Index (quarterly — most comprehensive)
        eci = await _fred_series("ECIWAG", limit=8, frequency="q")
        eval_, edate = _latest(eci)
        eyoy = _pct_change(eci, 4)
        lines.append(f"\nEmployment Cost Index (Wages):      {eval_:.1f}  [{edate}]  (quarterly)")
        if eyoy is not None:
            lines.append(f"  YoY change:                         {eyoy:+.2f}%")

        lines.append("\nNote: Wage growth above ~3.5% sustains service inflation at Fed target.")
        lines.append("Source: FRED — Bureau of Labor Statistics via FRED")
    except Exception as e:
        lines.append(f"Error fetching wage data: {e}")

    return "\n".join(lines)


# ── GDP & Growth ──────────────────────────────────────────────────────────────

@mcp.tool()
async def get_gdp_data() -> str:
    """
    Fetch US GDP data: real GDP growth (quarterly annualized), GDP Now
    estimate, personal consumption, and gross private investment.

    Sources: FRED (A191RL1Q225SBEA, PCECC96, GPDIA)
    """
    lines = ["=== GDP & ECONOMIC GROWTH ===\n"]

    try:
        # Real GDP growth rate (quarterly, annualized)
        gdp_growth = await _fred_series("A191RL1Q225SBEA", limit=8, frequency="q")
        gval, gdate = _latest(gdp_growth)
        lines.append(f"Real GDP Growth (QoQ annualized):  {gval:+.1f}%  [{gdate}]")

        # Real GDP level
        gdp_level = await _fred_series("GDPC1", limit=8, frequency="q")
        lval, ldate = _latest(gdp_level)
        lyoy = _pct_change(gdp_level, 4)
        lines.append(f"Real GDP Level:                    ${lval/1000:.2f}T  [{ldate}]")
        if lyoy is not None:
            lines.append(f"  YoY change:                        {lyoy:+.2f}%")

        # Nominal GDP
        nom_gdp = await _fred_series("GDP", limit=6, frequency="q")
        nval, ndate = _latest(nom_gdp)
        lines.append(f"Nominal GDP:                       ${nval/1000:.2f}T  [{ndate}]")

        # Personal Consumption Expenditures (real, level — compute QoQ growth)
        # PCECC96: Real PCE in billions chained 2017 dollars, quarterly SAAR
        pce_level = await _fred_series("PCECC96", limit=8)
        pval, pdate = _latest(pce_level)
        pce_vals = [o["value"] for o in pce_level if o["value"] is not None]
        pce_qoq = None
        if len(pce_vals) >= 2:
            # Annualize the quarterly growth rate
            pce_qoq = ((pce_vals[-1] / pce_vals[-2]) ** 4 - 1) * 100
        if pce_qoq is not None:
            lines.append(f"\nReal PCE Growth (QoQ ann.):        {pce_qoq:+.1f}%  [{pdate}]")
        else:
            lines.append(f"\nReal PCE Level:                    ${pval:.0f}B  [{pdate}]")

        # Gross Private Domestic Investment
        gpdi = await _fred_series("GPDIC1", limit=8, frequency="q")
        invval, invdate = _latest(gpdi)
        invyoy = _pct_change(gpdi, 4)
        lines.append(f"Real Gross Private Investment:     ${invval:.0f}B  [{invdate}]")
        if invyoy is not None:
            lines.append(f"  YoY change:                        {invyoy:+.2f}%")

        # GDP trend
        lines.append("\nReal GDP Growth trend (last 8 quarters):")
        for obs in gdp_growth[-8:]:
            if obs["value"] is not None:
                marker = " ⚠ CONTRACTION" if obs["value"] < 0 else ""
                lines.append(f"  {obs['date']}  {obs['value']:+.1f}%{marker}")

        # Two consecutive negatives = technical recession signal
        recent = [o["value"] for o in gdp_growth[-4:] if o["value"] is not None]
        if len(recent) >= 2 and recent[-1] < 0 and recent[-2] < 0:
            lines.append("\n⚠ TECHNICAL RECESSION SIGNAL: Two consecutive negative GDP quarters")

        lines.append("\nSource: FRED — Bureau of Economic Analysis via FRED")
    except Exception as e:
        lines.append(f"Error fetching GDP data: {e}")

    return "\n".join(lines)


# ── Credit & Financial Conditions ─────────────────────────────────────────────

@mcp.tool()
async def get_credit_conditions() -> str:
    """
    Fetch credit market and financial conditions data: investment grade and
    high yield spreads, bank lending standards, financial conditions index,
    and consumer credit growth.

    Sources: FRED (BAMLC0A0CM, BAMLH0A0HYM2, STLFSI4, DRTSCILM)
    """
    lines = ["=== CREDIT & FINANCIAL CONDITIONS ===\n"]

    try:
        # Investment Grade Corporate Bond Spread (OAS)
        ig_spread = await _fred_series("BAMLC0A0CM", limit=10)
        igval, igdate = _latest(ig_spread)
        lines.append(f"IG Corporate Spread (OAS):    {igval:.2f}%  [{igdate}]")

        # High Yield Spread (OAS)
        hy_spread = await _fred_series("BAMLH0A0HYM2", limit=10)
        hyval, hydate = _latest(hy_spread)
        lines.append(f"High Yield Spread (OAS):      {hyval:.2f}%  [{hydate}]")

        # Spread interpretation
        if hyval:
            if hyval > 800:
                lines.append("  ⚠ HY spread > 800 bps — stress/distress territory")
            elif hyval > 600:
                lines.append("  ⚠ HY spread 600-800 bps — elevated risk aversion")
            elif hyval < 350:
                lines.append("  ✓ HY spread < 350 bps — benign credit environment")
            else:
                lines.append("  → HY spread 350-600 bps — normal range")

        # St. Louis Fed Financial Stress Index
        fsi = await _fred_series("STLFSI4", limit=8)
        fval, fdate = _latest(fsi)
        lines.append(f"\nSt. Louis Financial Stress Idx: {fval:+.4f}  [{fdate}]")
        if fval is not None:
            stress_level = ("Elevated stress" if fval > 1.0
                            else "Moderate stress" if fval > 0
                            else "Below-average stress")
            lines.append(f"  Interpretation: {stress_level}  (0 = average, >1 = stressed)")

        # Bank lending standards — C&I loans (Senior Loan Officer Survey)
        slos = await _fred_series("DRTSCILM", limit=8, frequency="q")
        sval, sdate = _latest(slos)
        lines.append(f"\nBank Lending Standards (C&I):   {sval:+.1f}  [{sdate}]  (quarterly)")
        if sval is not None:
            tightening = (">0 = net tightening — credit headwind"
                         if sval > 0 else "<0 = net easing — credit tailwind")
            lines.append(f"  Interpretation: {tightening}")

        # Consumer credit growth
        cons_credit = await _fred_series("TOTALSL", limit=12, frequency="m")
        ccval, ccdate = _latest(cons_credit)
        ccyoy = _pct_change(cons_credit, 12)
        lines.append(f"\nTotal Consumer Credit:         ${ccval/1000:.1f}T  [{ccdate}]")
        if ccyoy is not None:
            lines.append(f"  YoY growth:                    {ccyoy:+.2f}%")

        lines.append("\nSource: FRED — BofA ICE and Federal Reserve via FRED")
    except Exception as e:
        lines.append(f"Error fetching credit condition data: {e}")

    return "\n".join(lines)


# ── Housing ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_housing_data() -> str:
    """
    Fetch US housing market data: existing home sales, new home sales,
    housing starts, building permits, median home price, and mortgage rates.

    Sources: FRED (EXHOSLUSM495S, HSN1F, HOUST, PERMIT, MSPUS, MORTGAGE30US)
    """
    lines = ["=== HOUSING MARKET ===\n"]

    try:
        # 30-year fixed mortgage rate
        mortgage = await _fred_series("MORTGAGE30US", limit=8)
        mval, mdate = _latest(mortgage)
        lines.append(f"30-Year Fixed Mortgage Rate:  {mval:.2f}%  [{mdate}]")

        # 15-year fixed
        mortgage15 = await _fred_series("MORTGAGE15US", limit=8)
        m15val, m15date = _latest(mortgage15)
        lines.append(f"15-Year Fixed Mortgage Rate:  {m15val:.2f}%  [{m15date}]")

        # Existing Home Sales (monthly, SAAR)
        existing = await _fred_series("EXHOSLUSM495S", limit=12, frequency="m")
        eval_, edate = _latest(existing)
        eyoy = _pct_change(existing, 12)
        if eval_:
            lines.append(f"\nExisting Home Sales:          {eval_/1000:.2f}M SAAR  [{edate}]")
            if eyoy is not None:
                lines.append(f"  YoY change:                   {eyoy:+.1f}%")

        # New Home Sales (monthly, SAAR)
        new_sales = await _fred_series("HSN1F", limit=12, frequency="m")
        nval, ndate = _latest(new_sales)
        nyoy = _pct_change(new_sales, 12)
        if nval:
            lines.append(f"New Home Sales:               {nval:.0f}K SAAR  [{ndate}]")
            if nyoy is not None:
                lines.append(f"  YoY change:                   {nyoy:+.1f}%")

        # Housing Starts
        starts = await _fred_series("HOUST", limit=12, frequency="m")
        sval, sdate = _latest(starts)
        if sval:
            lines.append(f"Housing Starts:               {sval:.0f}K SAAR  [{sdate}]")

        # Building Permits
        permits = await _fred_series("PERMIT", limit=6, frequency="m")
        pval, pdate = _latest(permits)
        if pval:
            lines.append(f"Building Permits:             {pval:.0f}K SAAR  [{pdate}]")

        # Median Home Sales Price
        price = await _fred_series("MSPUS", limit=8, frequency="q")
        prval, prdate = _latest(price)
        pryoy = _pct_change(price, 4)
        if prval:
            lines.append(f"\nMedian Home Sales Price:      ${prval:,.0f}  [{prdate}]  (quarterly)")
            if pryoy is not None:
                lines.append(f"  YoY change:                   {pryoy:+.1f}%")

        # Case-Shiller 20-city home price index YoY
        cs = await _fred_series("SPCS20RSA", limit=15, frequency="m")
        csval, csdate = _latest(cs)
        csyoy = _pct_change(cs, 12)
        if csval:
            lines.append(f"Case-Shiller 20-City Index:   {csval:.2f}  [{csdate}]")
            if csyoy is not None:
                lines.append(f"  YoY change:                   {csyoy:+.1f}%")

        lines.append("\nSource: FRED — NAR, Census Bureau, Freddie Mac via FRED")
    except Exception as e:
        lines.append(f"Error fetching housing data: {e}")

    return "\n".join(lines)


# ── Consumer & Business Sentiment ─────────────────────────────────────────────

@mcp.tool()
async def get_sentiment_data() -> str:
    """
    Fetch consumer and business sentiment indicators: University of Michigan
    Consumer Sentiment, Conference Board Consumer Confidence, ISM
    Manufacturing and Services PMI, and small business optimism (NFIB).

    Sources: FRED (UMCSENT, CSCICP03USM665S, NFCI, IPMAN, USSLIND)
    """
    lines = ["=== CONSUMER & BUSINESS SENTIMENT ===\n"]

    try:
        # University of Michigan Consumer Sentiment
        umich = await _fred_series("UMCSENT", limit=12, frequency="m")
        uval, udate = _latest(umich)
        lines.append(f"U. Michigan Consumer Sentiment:    {uval:.1f}  [{udate}]")
        # 12-month trend
        u_trend = [o["value"] for o in umich[-6:] if o["value"] is not None]
        if len(u_trend) >= 2:
            direction = "improving" if u_trend[-1] > u_trend[0] else "deteriorating"
            lines.append(f"  6-month trend:                     {direction}")

        # Conference Board Consumer Confidence
        conf_board = await _fred_series("CSCICP03USM665S", limit=6, frequency="m")
        cval, cdate = _latest(conf_board)
        lines.append(f"Conference Board Consumer Conf.:   {cval:.1f}  [{cdate}]")

        # NFIB Small Business Optimism
        nfib = await _fred_series("NFCI", limit=6)
        nval, ndate = _latest(nfib)
        lines.append(f"\nChicago Fed NFCI (Financial Cond.): {nval:+.4f}  [{ndate}]")
        if nval is not None:
            lines.append(f"  (above 0 = tighter than avg, below 0 = looser)")

        # Industrial Production: Manufacturing (IPMAN) — confirmed FRED series
        # Best freely available manufacturing activity proxy (ISM PMI is ISM-licensed)
        ipman = await _fred_series("IPMAN", limit=13, frequency="m")
        imval, imdate = _latest(ipman)
        imyoy = _pct_change(ipman, 12)
        lines.append(f"\nIndustrial Production (Mfg):       {imval:.1f}  [{imdate}]")
        if imyoy is not None:
            mfg_signal = "Expanding" if imyoy > 0 else "Contracting"
            lines.append(f"  YoY change:                        {imyoy:+.2f}%  ({mfg_signal})")

        # US Leading Index (Conference Board) — USSLIND confirmed on FRED
        leading = await _fred_series("USSLIND", limit=6, frequency="m")
        lval, ldate = _latest(leading)
        lmom = _pct_change(leading, 1)
        lines.append(f"US Leading Index (CB):             {lval:.1f}  [{ldate}]")
        if lmom is not None:
            lines.append(f"  MoM change:                        {lmom:+.2f}%")

        lines.append("\nSource: FRED — Univ. of Michigan, Conference Board, Fed Reserve")
    except Exception as e:
        lines.append(f"Error fetching sentiment data: {e}")

    return "\n".join(lines)


# ── Fed Policy & Money Supply ─────────────────────────────────────────────────

@mcp.tool()
async def get_fed_policy_data() -> str:
    """
    Fetch Federal Reserve policy data: balance sheet size (QE/QT tracker),
    M2 money supply, reserve balances, and the Fed's IORB rate.

    Sources: FRED (WALCL, M2SL, WRESBAL, IORB)
    """
    lines = ["=== FED POLICY & MONEY SUPPLY ===\n"]

    try:
        # Fed Balance Sheet (total assets — weekly)
        balance_sheet = await _fred_series("WALCL", limit=12)
        bval, bdate = _latest(balance_sheet)
        bchg = _pct_change(balance_sheet, 12)
        if bval:
            lines.append(f"Fed Balance Sheet:         ${bval/1e6:.2f}T  [{bdate}]")
            if bchg is not None:
                direction = "expanding (QE)" if bchg > 0 else "contracting (QT)"
                lines.append(f"  52-week change:          {bchg:+.1f}%  ({direction})")

        # M2 Money Supply
        m2 = await _fred_series("M2SL", limit=15, frequency="m")
        m2val, m2date = _latest(m2)
        m2yoy = _pct_change(m2, 12)
        if m2val:
            lines.append(f"\nM2 Money Supply:           ${m2val/1000:.2f}T  [{m2date}]")
            if m2yoy is not None:
                lines.append(f"  YoY growth:              {m2yoy:+.2f}%")
                if m2yoy > 8:
                    lines.append("  ⚠ Rapid M2 growth — historical inflation leading indicator")
                elif m2yoy < -2:
                    lines.append("  ⚠ M2 contracting — unusual, watch for deflationary pressure")

        # Reserve Balances at Federal Reserve Banks
        reserves = await _fred_series("WRESBAL", limit=8)
        rval, rdate = _latest(reserves)
        if rval:
            lines.append(f"\nBank Reserve Balances:     ${rval/1e6:.2f}T  [{rdate}]")

        # Interest on Reserve Balances (IORB)
        iorb = await _fred_series("IORB", limit=6)
        ival, idate = _latest(iorb)
        if ival:
            lines.append(f"Interest on Reserves (IORB): {ival:.2f}%  [{idate}]")

        # Velocity of M2
        m2_vel = await _fred_series("M2V", limit=8, frequency="q")
        vval, vdate = _latest(m2_vel)
        if vval:
            lines.append(f"\nM2 Velocity:               {vval:.2f}  [{vdate}]  (quarterly)")
            lines.append(f"  (rising = money circulating faster — inflationary signal)")

        lines.append("\nSource: FRED — Federal Reserve H.4.1 release and monetary aggregates")
    except Exception as e:
        lines.append(f"Error fetching Fed policy data: {e}")

    return "\n".join(lines)


@mcp.tool()
async def get_fed_communications() -> str:
    """
    Search for recent Federal Reserve communications — FOMC statements,
    speeches by Fed officials (Chair, Governors, regional Presidents),
    and major Fed-adjacent events (e.g. Jackson Hole Economic Symposium)
    — via SearXNG (local instance).

    Every other Atlas tool pulls numeric time-series data (FRED/BLS/
    Treasury); none of them can surface a speech, statement, or event
    itself. This tool exists to close that gap — qualitative Fed
    communications often move markets well before they show up in any
    numeric series Atlas otherwise tracks.

    Source: SearXNG (local, port 8080)
    """
    lines = ["=== FEDERAL RESERVE COMMUNICATIONS ===\n"]

    queries = [
        ("FOMC Statement / Meeting",   "FOMC statement Federal Reserve meeting"),
        ("Fed Chair Remarks",          "Jerome Powell Federal Reserve speech remarks"),
        ("Fed Officials / Governors",  "Federal Reserve governor president speech this week"),
        ("Major Fed Events",           "Federal Reserve Jackson Hole symposium economic policy"),
    ]

    async with httpx.AsyncClient(timeout=20) as client:
        for section, query in queries:
            lines.append(f"── {section} ──")
            try:
                r = await client.get(
                    f"{SEARXNG_URL}/search",
                    params={
                        "q":          query,
                        "format":     "json",
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


# ── Summary snapshot ──────────────────────────────────────────────────────────

@mcp.tool()
async def get_macro_snapshot() -> str:
    """
    Fetch a concise macro snapshot: the 10 most important current readings
    across rates, inflation, growth, and labor in a single call.
    Ideal for quick context injection before stock analysis.

    Sources: FRED (multiple series)
    """
    lines = ["=== MACRO SNAPSHOT ===",
             f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"]

    async def _quick(series_id: str, limit: int = 6,
                     freq: str = "") -> tuple[Optional[float], str]:
        try:
            obs = await _fred_series(series_id, limit=limit, frequency=freq)
            return _latest(obs)
        except Exception:
            return None, "N/A"

    series = [
        ("Fed Funds Rate",         "DFF",            6,  ""),
        ("10Y Treasury",           "DGS10",          6,  ""),
        ("2Y Treasury",            "DGS2",           6,  ""),
        ("Core PCE YoY",           "PCEPILFE",       24, "m"),
        ("Core CPI YoY",           "CPILFESL",       24, "m"),
        ("Unemployment Rate",      "UNRATE",         6,  "m"),
        ("Nonfarm Payrolls (M)",   "PAYEMS",         6,  "m"),
        ("Real GDP Growth (QoQ)",  "A191RL1Q225SBEA",6, "q"),
        ("Industrial Production Mfg",  "IPMAN",          13, "m"),
        ("HY Credit Spread",       "BAMLH0A0HYM2",  6,  ""),
    ]

    for label, sid, lim, freq in series:
        val, date = await _quick(sid, lim, freq)
        if val is not None:
            # Special formatting for specific series
            if "PCE" in label or "CPI" in label:
                # Compute YoY for index-level series
                try:
                    obs = await _fred_series(sid, limit=24, frequency="m")
                    vals = [o["value"] for o in obs if o["value"] is not None]
                    if len(vals) >= 13:
                        yoy = ((vals[-1] - vals[-13]) / vals[-13]) * 100
                        lines.append(f"  {label:<28} {yoy:+.2f}%  [{date}]")
                        continue
                except Exception:
                    pass
            elif "Payrolls" in label:
                lines.append(f"  {label:<28} {val/1000:.1f}M  [{date}]")
                continue

            lines.append(f"  {label:<28} {val:.2f}  [{date}]")
        else:
            lines.append(f"  {label:<28} N/A")

    lines.append("\nSource: FRED")
    return "\n".join(lines)


# ── Backdrop YAML builder ─────────────────────────────────────────────────────

@mcp.tool()
async def build_macro_backdrop() -> str:
    """
    Collect all key macro data series and return a structured YAML-format
    economic backdrop file. Designed to be saved as macro_backdrop_YYYYMMDD.yaml
    and injected into AI stock research prompts.

    Includes: monetary_policy, inflation, labor_market, growth, credit,
    housing, and sentiment themes — each with stance, evidence_strength,
    evidence_consensus, data_freshness, key_metrics, and risk_flags.

    Sources: FRED (multiple series)
    """

    async def _q(series_id: str, limit: int = 6, freq: str = ""):
        try:
            obs = await _fred_series(series_id, limit=limit, frequency=freq)
            val, date = _latest(obs)
            return val, date, obs
        except Exception:
            return None, "N/A", []

    # ── Gather all data ──────────────────────────────────────────────────────
    ff_val,    ff_date,   _    = await _q("DFF", 10)
    ub_val,    ub_date,   _    = await _q("DFEDTARU", 6)
    lb_val,    lb_date,   _    = await _q("DFEDTARL", 6)
    dgs10,     d10_date,  _    = await _q("DGS10", 6)
    dgs2,      d2_date,   _    = await _q("DGS2", 6)
    dgs3m,     d3m_date,  _    = await _q("DGS3MO", 6)

    pce_obs                    = (await _q("PCEPILFE", 24, "m"))[2]
    pce_val,   pce_date,  _    = await _q("PCEPILFE", 24, "m")
    cpi_obs                    = (await _q("CPILFESL", 24, "m"))[2]
    cpi_val,   cpi_date,  _    = await _q("CPILFESL", 24, "m")
    be5_val,   be5_date,  _    = await _q("T5YIE", 6)
    be10_val,  be10_date, _    = await _q("T10YIE", 6)

    unemp_val, unemp_date,_    = await _q("UNRATE", 6, "m")
    payrolls,  pay_date,  pay_obs = await _q("PAYEMS", 14, "m")
    claims_val,claims_date,_   = await _q("ICSA", 6)

    gdp_val,   gdp_date,  gdp_obs = await _q("A191RL1Q225SBEA", 8, "q")
    m2_obs                     = (await _q("M2SL", 15, "m"))[2]
    m2_val,    m2_date,   _    = await _q("M2SL", 15, "m")

    hy_val,    hy_date,   _    = await _q("BAMLH0A0HYM2", 6)
    ig_val,    ig_date,   _    = await _q("BAMLC0A0CM", 6)
    fsi_val,   fsi_date,  _    = await _q("STLFSI4", 6)

    umich_val, umich_date,_    = await _q("UMCSENT", 6, "m")
    ism_val,   ism_date,  ism_obs = await _q("IPMAN", 13, "m")

    mortgage_val, mort_date, _ = await _q("MORTGAGE30US", 6)

    # ── Derived calculations ─────────────────────────────────────────────────
    spread_2_10 = ((dgs10 - dgs2) * 100) if dgs10 and dgs2 else None
    spread_3m_10 = ((dgs10 - dgs3m) * 100) if dgs10 and dgs3m else None

    pce_yoy = None
    pce_vals = [o["value"] for o in pce_obs if o["value"] is not None]
    if len(pce_vals) >= 13:
        pce_yoy = ((pce_vals[-1] - pce_vals[-13]) / pce_vals[-13]) * 100

    cpi_yoy = None
    cpi_vals = [o["value"] for o in cpi_obs if o["value"] is not None]
    if len(cpi_vals) >= 13:
        cpi_yoy = ((cpi_vals[-1] - cpi_vals[-13]) / cpi_vals[-13]) * 100

    m2_yoy = None
    m2_vals = [o["value"] for o in m2_obs if o["value"] is not None]
    if len(m2_vals) >= 13:
        m2_yoy = ((m2_vals[-1] - m2_vals[-13]) / m2_vals[-13]) * 100

    # PPI metrics for YAML output (Jansky recommendation)
    ppi_yoy = None
    ppi_mom = None
    try:
        _ppi_obs = await _fred_series("PPIACO", limit=14, frequency="m")
        _ppi_vals = [o["value"] for o in _ppi_obs if o["value"] is not None]
        if len(_ppi_vals) >= 13:
            ppi_yoy = ((_ppi_vals[-1] - _ppi_vals[-13]) / _ppi_vals[-13]) * 100
        if len(_ppi_vals) >= 2:
            ppi_mom = ((_ppi_vals[-1] - _ppi_vals[-2]) / _ppi_vals[-2]) * 100
    except Exception:
        pass

    pay_chg = None
    pay_vals = [o["value"] for o in pay_obs if o["value"] is not None]
    if len(pay_vals) >= 2:
        pay_chg = (pay_vals[-1] - pay_vals[-2]) * 1000  # thousands → abs jobs

    gdp_recent = [o["value"] for o in gdp_obs if o["value"] is not None][-2:] if gdp_obs else []
    in_recession = len(gdp_recent) == 2 and all(v < 0 for v in gdp_recent)

    # ── Classify monetary stance ─────────────────────────────────────────────
    if ff_val is not None and pce_yoy is not None:
        real_rate = ff_val - pce_yoy
        if real_rate > 1.5:
            monetary_stance = "Restrictive"
        elif real_rate > 0:
            monetary_stance = "Mildly Restrictive"
        elif real_rate > -1:
            monetary_stance = "Neutral"
        else:
            monetary_stance = "Accommodative"
    else:
        monetary_stance = "Unknown"

    # ── Classify inflation stance ─────────────────────────────────────────────
    if pce_yoy is not None:
        if pce_yoy > 4.0:
            inflation_stance = "Significantly Above Target"
        elif pce_yoy > 2.5:
            inflation_stance = "Above Target"
        elif pce_yoy > 1.5:
            inflation_stance = "Near Target"
        else:
            inflation_stance = "At or Below Target"
    else:
        inflation_stance = "Unknown"

    # ── Classify labor market ─────────────────────────────────────────────────
    if unemp_val is not None:
        if unemp_val < 4.0:
            labor_stance = "Tight"
        elif unemp_val < 5.0:
            labor_stance = "Balanced"
        else:
            labor_stance = "Slack"
    else:
        labor_stance = "Unknown"

    # ── Classify credit conditions ────────────────────────────────────────────
    if hy_val is not None:
        if hy_val > 700:
            credit_stance = "Stressed"
        elif hy_val > 450:
            credit_stance = "Cautious"
        else:
            credit_stance = "Benign"
    else:
        credit_stance = "Unknown"

    # ── Classify growth ───────────────────────────────────────────────────────
    if gdp_val is not None:
        if in_recession:
            growth_stance = "Contraction"
        elif gdp_val < 0:
            growth_stance = "Slowing"
        elif gdp_val < 2:
            growth_stance = "Below Trend"
        elif gdp_val < 3.5:
            growth_stance = "Trend Growth"
        else:
            growth_stance = "Above Trend"
    else:
        growth_stance = "Unknown"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Build YAML ────────────────────────────────────────────────────────────
    lines = [
        f"# Macro Economic Backdrop",
        f"# Generated: {now}",
        f"# Source: macromcp / FRED",
        f"",
        f"generated_date: \"{now}\"",
        f"",
        f"# ---",
        f"# MONETARY POLICY",
        f"# ---",
        f"monetary_policy:",
        f"  theme: Interest Rates",
        f"  stance: {monetary_stance}",
        f"  evidence_strength: {'Strong' if ff_val and pce_yoy else 'Moderate'}",
        f"  evidence_consensus: {'Strong' if monetary_stance in ['Restrictive','Accommodative'] else 'Moderate'}",
        f"  data_freshness: High",
        f"  key_metrics:",
        f"    fed_funds_rate: {f'{(ff_val):.2f}' if ff_val else 'N/A'}",
        f"    fomc_target_range: \"{f'{(lb_val):.2f}' if lb_val else 'N/A'}–{f'{(ub_val):.2f}' if ub_val else 'N/A'}%\"",
        f"    real_fed_funds_rate: {f'{((ff_val - pce_yoy)):.2f}' if ff_val and pce_yoy else 'N/A'}",
        f"    yield_10y: {f'{(dgs10):.2f}' if dgs10 else 'N/A'}",
        f"    yield_2y: {f'{(dgs2):.2f}' if dgs2 else 'N/A'}",
        f"    spread_2y_10y_bps: {f'{(spread_2_10):.1f}' if spread_2_10 else 'N/A'}",
        f"    spread_3m_10y_bps: {f'{(spread_3m_10):.1f}' if spread_3m_10 else 'N/A'}",
        f"    yield_curve_inverted: {'true' if spread_2_10 and spread_2_10 < 0 else 'false'}",
        f"  risk_flags:",
    ]

    # oil_shock_risk (Sept 2026) — PPI running meaningfully hotter than core
    # PCE signals supply-side/cost-push pressure building in the pipeline
    # ahead of consumer prices (e.g. an energy/input-cost shock), distinct
    # from ordinary demand-pull inflation. Computed once, used in both
    # monetary_policy and inflation risk_flags below.
    _oil_shock_gap  = (ppi_yoy - pce_yoy) if (ppi_yoy is not None and pce_yoy is not None) else None
    _oil_shock_flag = _oil_shock_gap is not None and _oil_shock_gap > 3
    _oil_shock_msg  = (
        f"    - \"oil_shock_risk: PPI running {_oil_shock_gap:.1f}pts above "
        f"core PCE ({ppi_yoy:.1f}% vs {pce_yoy:.1f}%) — supply-side "
        f"cost-push pressure not yet fully passed through to consumer prices\""
    ) if _oil_shock_flag else ""

    if spread_2_10 and spread_2_10 < -50:
        lines.append(f"    - \"Deeply inverted yield curve ({spread_2_10:.0f} bps) — elevated recession risk\"")
    if ff_val and ff_val > 5.0:
        lines.append(f"    - \"Fed Funds at {ff_val:.2f}% — most restrictive since pre-2008\"")
    if _oil_shock_flag:
        lines.append(_oil_shock_msg)
    if not (spread_2_10 and spread_2_10 < 0) and not (ff_val and ff_val > 5.0) and not _oil_shock_flag:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ---",
        f"# INFLATION",
        f"# ---",
        f"inflation:",
        f"  theme: Inflation",
        f"  stance: {inflation_stance}",
        f"  evidence_strength: {'Strong' if pce_yoy and cpi_yoy else 'Moderate'}",
        f"  evidence_consensus: {'Strong' if pce_yoy and cpi_yoy and abs(pce_yoy - cpi_yoy) < 1 else 'Moderate'}",
        f"  data_freshness: {'High' if pce_date != 'N/A' else 'Low'}",
        f"  key_metrics:",
        f"    core_pce_yoy: {f'{(pce_yoy):.2f}' if pce_yoy else 'N/A'}",
        f"    core_cpi_yoy: {f'{(cpi_yoy):.2f}' if cpi_yoy else 'N/A'}",
        f"    breakeven_5y: {f'{(be5_val):.2f}' if be5_val else 'N/A'}",
        f"    breakeven_10y: {f'{(be10_val):.2f}' if be10_val else 'N/A'}",
        f"    fed_target: 2.00",
        f"    overshoot_bps: {f'{((pce_yoy - 2.0) * 100):.0f}' if pce_yoy else 'N/A'}",
        f"    ppi_all_commodities_yoy: {f'{ppi_yoy:.2f}' if ppi_yoy else 'N/A'}",
        f"    ppi_all_commodities_mom: {f'{ppi_mom:.2f}' if ppi_mom else 'N/A'}",
        f"  risk_flags:",
    ]

    if pce_yoy and pce_yoy > 3.5:
        lines.append(f"    - \"Core PCE at {pce_yoy:.1f}% — well above 2% target, policy credibility at risk\"")
    if be10_val and be10_val > 2.6:
        lines.append(f"    - \"10Y breakeven {be10_val:.2f}% — market pricing persistent inflation\"")
    if _oil_shock_flag:
        lines.append(_oil_shock_msg)
    if not (pce_yoy and pce_yoy > 3.5) and not _oil_shock_flag:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ---",
        f"# LABOR MARKET",
        f"# ---",
        f"labor_market:",
        f"  theme: Employment",
        f"  stance: {labor_stance}",
        f"  evidence_strength: Strong",
        f"  evidence_consensus: Strong",
        f"  data_freshness: High",
        f"  key_metrics:",
        f"    unemployment_rate: {f'{(unemp_val):.1f}' if unemp_val else 'N/A'}",
        f"    monthly_payroll_change: {f'{(pay_chg):.0f}' if pay_chg else 'N/A'}",
        f"    initial_jobless_claims: {f'{(claims_val):.0f}' if claims_val else 'N/A'}",
        f"  risk_flags:",
    ]

    if claims_val and claims_val > 260000:
        lines.append(f"    - \"Jobless claims elevated at {claims_val:,.0f} — labor market softening\"")
    if unemp_val and unemp_val > 5.0:
        lines.append(f"    - \"Unemployment above 5% — slack in labor market\"")
    if not ((claims_val and claims_val > 260000) or (unemp_val and unemp_val > 5.0)):
        lines.append(f"    - none_identified")

    # IPMAN YoY — moved here (Sept 2026) so it's available for growth's
    # key_metrics, not just the risk-flag check below. This metric measures
    # manufacturing output and belongs under growth, not sentiment, where
    # it was previously also duplicated.
    ipman_vals = [o["value"] for o in ism_obs if o["value"] is not None]
    ipman_yoy  = ((ipman_vals[-1] - ipman_vals[-13]) / ipman_vals[-13] * 100) if len(ipman_vals) >= 13 else None

    lines += [
        f"",
        f"# ---",
        f"# ECONOMIC GROWTH",
        f"# ---",
        f"growth:",
        f"  theme: GDP Growth",
        f"  stance: {growth_stance}",
        f"  evidence_strength: {'Strong' if gdp_val is not None else 'Low'}",
        f"  evidence_consensus: Moderate",
        f"  data_freshness: {'Moderate' if gdp_val is not None else 'Low'}",
        f"  key_metrics:",
        f"    real_gdp_growth_qoq_ann: {f'{(gdp_val):.1f}' if gdp_val is not None else 'N/A'}",
        f"    in_technical_recession: {'true' if in_recession else 'false'}",
        f"    industrial_production_mfg: {f'{(ism_val):.1f}' if ism_val else 'N/A'}",
        f"    industrial_production_mfg_yoy: {f'{(ipman_yoy):.2f}' if ipman_yoy is not None else 'N/A'}",
        f"    m2_yoy_growth: {f'{(m2_yoy):.2f}' if m2_yoy else 'N/A'}",
        f"  risk_flags:",
    ]

    if in_recession:
        lines.append(f"    - \"TECHNICAL RECESSION — two consecutive negative GDP quarters\"")
    if ipman_yoy is not None and ipman_yoy < -2:
        lines.append(f"    - \"Industrial production (mfg) down {ipman_yoy:.1f}% YoY — manufacturing contraction\"")
    if not in_recession and not (ipman_yoy is not None and ipman_yoy < -2):
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ---",
        f"# CREDIT & FINANCIAL CONDITIONS",
        f"# ---",
        f"credit:",
        f"  theme: Credit Conditions",
        f"  stance: {credit_stance}",
        f"  evidence_strength: {'Strong' if hy_val and ig_val else 'Moderate'}",
        f"  evidence_consensus: Moderate",
        f"  data_freshness: High",
        f"  key_metrics:",
        f"    hy_spread_bps: {f'{(hy_val * 100):.0f}' if hy_val else 'N/A'}",
        f"    ig_spread_bps: {f'{(ig_val * 100):.0f}' if ig_val else 'N/A'}",
        f"    stl_financial_stress_idx: {f'{(fsi_val):.4f}' if fsi_val else 'N/A'}",
        f"  risk_flags:",
    ]

    if hy_val and hy_val * 100 > 600:
        lines.append(f"    - \"HY spreads at {hy_val * 100:.0f} bps — elevated credit stress\"")
    if fsi_val and fsi_val > 1.0:
        lines.append(f"    - \"St. Louis FSI at {fsi_val:.3f} — financial stress elevated\"")
    if not ((hy_val and hy_val * 100 > 600) or (fsi_val and fsi_val > 1.0)):
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ---",
        f"# HOUSING",
        f"# ---",
        f"housing:",
        f"  theme: Housing Market",
        f"  stance: {'Stressed' if mortgage_val and mortgage_val > 7 else 'Constrained' if mortgage_val and mortgage_val > 6 else 'Stable'}",
        f"  evidence_strength: Strong",
        f"  evidence_consensus: Moderate",
        f"  data_freshness: Moderate",
        f"  key_metrics:",
        f"    mortgage_rate_30y: {f'{(mortgage_val):.2f}' if mortgage_val else 'N/A'}",
        f"  risk_flags:",
    ]

    if mortgage_val and mortgage_val > 7:
        lines.append(f"    - \"30Y mortgage at {mortgage_val:.2f}% — housing affordability severely constrained\"")
    else:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ---",
        f"# CONSUMER SENTIMENT",
        f"# ---",
        f"sentiment:",
        f"  theme: Consumer Confidence",
        f"  stance: {'Pessimistic' if umich_val and umich_val < 65 else 'Cautious' if umich_val and umich_val < 80 else 'Confident'}",
        f"  evidence_strength: Moderate",
        f"  evidence_consensus: Moderate",
        f"  data_freshness: Moderate",
        f"  key_metrics:",
        f"    umich_consumer_sentiment: {f'{(umich_val):.1f}' if umich_val else 'N/A'}",
        f"  risk_flags:",
    ]

    if umich_val and umich_val < 65:
        lines.append(f"    - \"Consumer sentiment at {umich_val:.1f} — historically associated with recession\"")
    else:
        lines.append(f"    - none_identified")

    lines += [
        f"",
        f"# ---",
        f"# AI USAGE GUIDANCE",
        f"# ---",
        f"ai_guidance:",
        f"  when_analyzing_growth_stocks: >",
        f"    Weight monetary_policy.stance and credit.stance heavily.",
        f"    Restrictive rates compress multiples. Benign credit = risk-on.",
        f"  when_analyzing_value_stocks: >",
        f"    Focus on growth.stance and labor_market. Slowing growth",
        f"    rewards defensive value; tight labor supports consumer spending.",
        f"  when_analyzing_bonds_or_rates_sensitive: >",
        f"    Use yield_curve inversion status and monetary_policy real rate.",
        f"    Deeply inverted curve historically precedes credit events 12-18 months out.",
        f"  sector_tilts:",
        f"    - Restrictive monetary + Above Target inflation → short duration, energy, commodities",
        f"    - Pivot signal (rates falling) → long duration, growth, REITs",
        f"    - Labor slack emerging → consumer discretionary caution",
        f"    - HY spreads widening → reduce cyclical exposure",
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
    uvicorn.run(app, host="0.0.0.0", port=8643)
