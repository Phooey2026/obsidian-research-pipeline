#!/usr/bin/env python3
"""
diag_issuer_shares.py — find a source of daily shares-outstanding for IBIT FBTC BITQ GLD IAU.

Usage:  python3 diag_issuer_shares.py            (read-only; ~1-2 minutes)
Paste the whole output back. For each candidate URL it prints the HTTP status, size,
what it looks like (HTML / CSV / XLSX / JSON) and any 'shares outstanding' style hits.
"""
import re, sys, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept": "*/*"}

CANDIDATES = [
    # ── SSGA (GLD) ───────────────────────────────────────────────────────────
    ("GLD  ssga navhist xlsx",  "https://www.ssga.com/library-content/products/fund-data/etfs/us/navhist-us-en-gld.xlsx"),
    ("GLD  ssga product page",  "https://www.ssga.com/us/en/intermediary/etfs/spdr-gold-shares-gld"),
    ("GLD  spdrgoldshares",     "https://www.spdrgoldshares.com/usa/"),
    ("GLD  spdr archive csv",   "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"),
    # ── iShares (IAU, IBIT) ──────────────────────────────────────────────────
    ("IAU  ishares page",       "https://www.ishares.com/us/products/239561/ishares-gold-trust-fund"),
    ("IAU  ishares csv",        "https://www.ishares.com/us/products/239561/ishares-gold-trust-fund/1467271812596.ajax?fileType=csv&fileName=IAU_holdings&dataType=fund"),
    ("IBIT ishares page",       "https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf"),
    ("IBIT ishares csv",        "https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf/1467271812596.ajax?fileType=csv&fileName=IBIT_holdings&dataType=fund"),
    # ── Fidelity (FBTC), Bitwise (BITQ) ──────────────────────────────────────
    ("FBTC fidelity page",      "https://www.fidelity.com/etfs/fidelity-wise-origin-bitcoin-fund"),
    ("FBTC fidelity quote",     "https://digital.fidelity.com/prgw/digital/research/quote/dashboard/summary?symbol=FBTC"),
    ("BITQ bitwise page",       "https://bitwiseinvestments.com/crypto-funds/bitq"),
    # ── Aggregators (single page per fund) ───────────────────────────────────
    ("GLD  stockanalysis",      "https://stockanalysis.com/etf/gld/"),
    ("IAU  stockanalysis",      "https://stockanalysis.com/etf/iau/"),
    ("IBIT stockanalysis",      "https://stockanalysis.com/etf/ibit/"),
    ("FBTC stockanalysis",      "https://stockanalysis.com/etf/fbtc/"),
    ("BITQ stockanalysis",      "https://stockanalysis.com/etf/bitq/"),
]

PATS = [r"(?i)shares?\s+outstanding", r"(?i)shares\s+out\b", r"(?i)total\s+net\s+assets", r"(?i)as\s+of\s+\d"]


def fetch(url, n=1_500_000):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(n)
    except Exception as e:
        return None, "", f"{type(e).__name__}: {str(e)[:140]}".encode()


def squash(t, n=170):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))[:n]


for label, url in CANDIDATES:
    st, ct, body = fetch(url)
    if st is None:
        print(f"{label:<24} FAIL  {body.decode()}")
        continue
    kind = ("XLSX" if body[:2] == b"PK" else "HTML" if b"<html" in body[:2000].lower() or b"<!doctype" in body[:200].lower()
            else "JSON" if body.lstrip()[:1] in (b"{", b"[") else "TEXT/CSV")
    print(f"{label:<24} {st}  {len(body):>9,} bytes  {kind}  ({ct.split(';')[0]})")
    text = body.decode("utf-8", "replace") if kind != "XLSX" else ""
    hits = 0
    for pat in PATS:
        for m in re.finditer(pat, text):
            ctx = squash(text[max(0, m.start() - 30): m.end() + 220])
            if re.search(r"\d[\d,\.]{3,}", ctx):
                print(f"      · {ctx}")
                hits += 1
            if hits >= 3:
                break
        if hits >= 3:
            break
    if kind == "TEXT/CSV":
        print("      head:", squash(text[:300], 300))
    if kind == "XLSX":
        print("      (binary workbook — needs openpyxl to read; sheet names/columns will be checked next)")
