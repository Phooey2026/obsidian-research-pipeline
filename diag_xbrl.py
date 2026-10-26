#!/usr/bin/env python3
"""
diag_xbrl.py — why does get_sec_earnings show stale quarters for a ticker?

Usage:  python3 diag_xbrl.py NEE [EPD ...] [+ConceptName ...] [--scan]
        +Name  also show that us-gaap concept;  --scan lists every us-gaap concept
        whose name contains "Revenue" with its newest period and value

For each ticker it prints:
  1. the newest 10-Q / 10-K filings SEC lists for the company (period + filed date)
  2. for each EPS / net-income / revenue concept the extractor tries: how many
     facts exist, and the newest few (period start/end, days, form, filed, value)
so we can see whether the newest quarter is missing from SEC's data, filed under
a tag the extractor doesn't try, or being dropped by its duration/form filters.
Read-only; talks only to sec.gov.
"""
import sys, json, urllib.request
from datetime import datetime

UA = {"User-Agent": "Jay jay@example.com"}
CONCEPTS = [
    "EarningsPerShareDiluted", "EarningsPerShareBasic",
    "IncomeLossFromContinuingOperationsPerDilutedShare",
    "IncomeLossFromContinuingOperationsPerBasicShare",
    "NetIncomeLossPerOutstandingLimitedPartnershipUnit",
    "IncomeLossFromContinuingOperationsPerOutstandingLimitedPartnershipUnit",
    "NetIncomeLoss", "ProfitLoss",
    "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
]


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.loads(r.read())


def days(a, b):
    try:
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    except Exception:
        return None


def main(tickers, extra=(), scan=False):
    tk = get("https://www.sec.gov/files/company_tickers.json")
    for t in tickers:
        t = t.upper()
        matches = [e for e in tk.values() if e.get("ticker", "").upper() == t]
        print("=" * 78)
        print(f"{t}: registrants in SEC ticker file: "
              + "; ".join(f"{m['title']} (CIK {m['cik_str']})" for m in matches) or "none")
        for m in matches:
            cik = str(m["cik_str"]).zfill(10)
            try:
                subs = get(f"https://data.sec.gov/submissions/CIK{cik}.json")
                rec = subs.get("filings", {}).get("recent", {})
                rows = [(rec["form"][i], rec["reportDate"][i], rec["filingDate"][i])
                        for i in range(len(rec.get("form", [])))
                        if rec["form"][i] in ("10-Q", "10-K", "10-Q/A", "10-K/A")][:6]
                print(f"\n  newest periodic filings for CIK {cik}:")
                for r in rows:
                    print(f"    {r[0]:<7} period {r[1]}  filed {r[2]}")
                facts = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
                gaap = facts.get("facts", {}).get("us-gaap", {})
            except Exception as e:
                print(f"  lookup problem: {type(e).__name__}: {e}")
                continue
            print("\n  concepts (newest facts first; Q = 75-110 day duration, A = 350-380):")
            if scan:
                print("\n  every us-gaap concept with 'Revenue' in its name (newest period first):")
                rows_ = []
                for c, v in gaap.items():
                    if "Revenue" not in c:
                        continue
                    its = [x for u in v.get("units", {}).values() for x in u]
                    if its:
                        n = max(its, key=lambda x: (x.get("end", ""), x.get("filed", "")))
                        rows_.append((n.get("end", ""), c, len(its), n.get("val"), days(n.get("start", ""), n.get("end", ""))))
                for r in sorted(rows_, reverse=True)[:25]:
                    print(f"   {r[0]}  {r[1]}  ({r[2]} facts)  newest val={r[3]}  [{r[4]}d]")
            for c in list(CONCEPTS) + list(extra):
                units = gaap.get(c, {}).get("units", {})
                items = [x for u in units.values() for x in u]
                if not items:
                    continue
                items.sort(key=lambda x: (x.get("end", ""), x.get("filed", "")), reverse=True)
                print(f"   {c}: {len(items)} facts")
                for x in items[:4]:
                    d = days(x.get("start", ""), x.get("end", ""))
                    kind = "Q" if d and 75 <= d <= 110 else "A" if d and 350 <= d <= 380 else f"{d}d"
                    print(f"      {x.get('start','?')} → {x.get('end','?')} [{kind}] "
                          f"form={x.get('form')} filed={x.get('filed')} val={x.get('val')}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    _a = sys.argv[1:]
    main([a for a in _a if not a.startswith(("+", "--"))],
         [a[1:] for a in _a if a.startswith("+")], "--scan" in _a)
