#!/usr/bin/env python3
"""
diag_lag.py — where is the 10-Q that SEC's companyfacts is missing?

Usage:  python3 diag_lag.py NEE [C DUK REYN ...]

For each ticker, compares four routes to the newest quarterly data:
  1. submissions     newest 10-Q / 10-K filings (period + filed date)  <- the truth
  2. companyfacts    newest period per key concept (what the tools use today)
  3. companyconcept  same concepts through the per-concept endpoint
  4. frames          is this CIK present in the quarter's all-company frame?
  5. the filing's own XBRL "R" pages (FilingSummary.xml -> income statement rows)
If (3) or (4) or (5) has the quarter that (2) lacks, we have a fix route.
Read-only; talks only to sec.gov.
"""
import sys, json, re, time, urllib.request
from datetime import datetime

UA = {"User-Agent": "Jay jay@example.com"}
CONCEPTS = ["EarningsPerShareDiluted", "NetIncomeLoss", "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax"]


def get(url, raw=False):
    time.sleep(0.15)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        b = r.read()
    return b if raw else json.loads(b)


def newest(facts):
    """newest (end, form, filed) among quarterly-ish facts (60-110 day spans)."""
    best = None
    for unit, arr in facts.items():
        for x in arr:
            try:
                d = (datetime.strptime(x["end"], "%Y-%m-%d") - datetime.strptime(x.get("start", x["end"]), "%Y-%m-%d")).days
            except Exception:
                continue
            if 60 <= d <= 110 and (best is None or x["end"] > best[0]):
                best = (x["end"], x.get("form", "?"), x.get("filed", "?"))
    return best


def strip(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)).strip()


def one(tk):
    print("=" * 78)
    tickers = get("https://www.sec.gov/files/company_tickers.json")
    m = [e for e in tickers.values() if e["ticker"].upper() == tk]
    if not m:
        print(tk, "no CIK"); return
    cik10, cik = str(m[0]["cik_str"]).zfill(10), str(m[0]["cik_str"])
    print(f"{tk}  CIK {cik10}  {m[0]['title']}")

    subs = get(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    rec = subs["filings"]["recent"]
    qs = [(rec["reportDate"][i], rec["filingDate"][i], rec["form"][i], rec["accessionNumber"][i], rec["primaryDocument"][i])
          for i in range(len(rec["form"])) if rec["form"][i] in ("10-Q", "10-K")][:4]
    print("\n[1] submissions — newest periodic filings")
    for q in qs:
        print(f"    {q[2]:<5} period {q[0]}  filed {q[1]}  {q[3]}  {q[4]}")
    top = qs[0] if qs else None

    print("\n[2] companyfacts — newest quarterly period per concept")
    try:
        cf = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json")["facts"].get("us-gaap", {})
        for c in CONCEPTS:
            n = newest(cf.get(c, {}).get("units", {})) if c in cf else None
            print(f"    {c:<55} {n if n else '— none —'}")
    except Exception as e:
        print("    error:", type(e).__name__, str(e)[:100])

    print("\n[3] companyconcept — same concepts, per-concept endpoint")
    for c in CONCEPTS:
        try:
            cc = get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10}/us-gaap/{c}.json")
            n = newest(cc.get("units", {}))
            print(f"    {c:<55} {n if n else '— none —'}")
        except Exception as e:
            print(f"    {c:<55} {type(e).__name__} {str(e)[:50]}")

    if top and top[2] == "10-Q":
        y, mo = int(top[0][:4]), int(top[0][5:7])
        per = f"CY{y}Q{(mo - 1) // 3 + 1}"
        print(f"\n[4] frames — EarningsPerShareDiluted {per}: is CIK {cik} in it?")
        try:
            fr = get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/EarningsPerShareDiluted/USD-per-shares/{per}.json")
            hit = [d for d in fr.get("data", []) if str(d.get("cik")) == cik]
            print(f"    frame has {len(fr.get('data', []))} companies; {tk}: {hit[:1] if hit else 'NOT PRESENT'}")
        except Exception as e:
            print("    error:", type(e).__name__, str(e)[:100])

        print(f"\n[5] the {top[2]} filing's own R pages ({top[3]})")
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{top[3].replace('-', '')}/"
        try:
            fs = get(base + "FilingSummary.xml", raw=True).decode("utf-8", "replace")
            reps = []
            for blk in re.findall(r"<Report\b.*?</Report>", fs, re.S):      # per-report: file name precedes short name
                f = re.search(r"<HtmlFileName>(.*?)</HtmlFileName>", blk, re.S)
                n = re.search(r"<ShortName>(.*?)</ShortName>", blk, re.S)
                if f and n:
                    reps.append((n.group(1), f.group(1)))
            for sn, fn in reps[:8]:
                print(f"    {fn:<10} {sn[:80]}")
            inc = next(((sn, fn) for sn, fn in reps
                        if re.search(r"(?i)(income|operations|earnings)", sn) and not re.search(r"(?i)comprehensive|parenth", sn)), None)
            if inc:
                html = get(base + inc[1], raw=True).decode("utf-8", "replace")
                rows = [strip(r) for r in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)]
                print(f"\n    income statement page {inc[1]} ({inc[0][:60]}) — first rows:")
                for r in [x for x in rows if x][:14]:
                    print("      ", r[:130])
            else:
                print("    no income-statement-like report found in FilingSummary")
        except Exception as e:
            print("    error:", type(e).__name__, str(e)[:100])


if __name__ == "__main__":
    for t in [a.upper() for a in sys.argv[1:]] or ["NEE"]:
        try:
            one(t)
        except Exception as e:
            print(t, "failed:", type(e).__name__, str(e)[:120])
