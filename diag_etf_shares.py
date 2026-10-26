#!/usr/bin/env python3
"""
diag_etf_shares.py — which free source gives ETF shares-outstanding for flow estimates?

Usage:  python3 diag_etf_shares.py [TICKER ...]     (default: IBIT FBTC BITQ GLD IAU)

Read-only. For each ticker prints what yfinance returns (get_shares_full, and the
share/AUM/NAV fields of .info), then probes the issuer pages for GLD and IAU.
"""
import sys, re, json, urllib.request
from datetime import datetime, timedelta, timezone

TICKERS = [t.upper() for t in sys.argv[1:]] or ["IBIT", "FBTC", "BITQ", "GLD", "IAU"]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def fetch(url, n=400000):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
            return r.status, r.read(n).decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def yf_probe():
    import yfinance as yf
    print(f"yfinance {getattr(yf, '__version__', '?')}\n")
    for t in TICKERS:
        print(f"── {t}")
        tk = yf.Ticker(t)
        for days in (120, 400):
            try:
                s = tk.get_shares_full(start=(datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat())
                n = 0 if s is None else len(s)
                print(f"   get_shares_full(start=-{days}d): {n} rows" + (f"; last={s.index[-1]} -> {float(s.iloc[-1]):,.0f}" if n else ""))
            except Exception as e:
                print(f"   get_shares_full(start=-{days}d): {type(e).__name__}: {str(e)[:100]}")
        try:
            info = tk.info
            keys = ["sharesOutstanding", "impliedSharesOutstanding", "totalAssets", "navPrice",
                    "netAssets", "floatShares", "regularMarketPrice", "previousClose", "quoteType"]
            print("   info: " + ", ".join(f"{k}={info.get(k)}" for k in keys))
        except Exception as e:
            print(f"   info: {type(e).__name__}: {str(e)[:100]}")


def issuer_probe():
    print("\n── Issuer pages")
    tests = [
        ("GLD  SPDR archive csv", "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"),
        ("GLD  SPDR api", "https://api.spdrgoldshares.com/api/v1/historical-archive?exchange=NYSE&lang=en"),
        ("GLD  product page", "https://www.spdrgoldshares.com/usa/"),
        ("IAU  iShares page", "https://www.ishares.com/us/products/239561/ishares-gold-trust-fund"),
        ("IBIT iShares page", "https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf"),
    ]
    for label, url in tests:
        st, body = fetch(url)
        print(f"   {label}: status={st} bytes={len(body) if st else 0}")
        if st:
            m = re.findall(r"(?i)shares?\s+outstanding[^<]{0,60}(?:<[^>]+>\s*){0,6}[\d,\.]+", body)[:2]
            if m:
                print("      shares-outstanding hits:", [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x))[:120] for x in m])
            print("      head:", re.sub(r"\s+", " ", body[:160]))
        else:
            print("      ", body)


if __name__ == "__main__":
    try:
        yf_probe()
    except Exception as e:
        print("yfinance probe failed:", e)
    issuer_probe()
