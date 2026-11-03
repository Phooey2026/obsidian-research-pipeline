#!/usr/bin/env python3
"""
diag_rpage.py — why did the R-page fallback return nothing for a filing?

Usage:  python3 diag_rpage.py CIK ACCESSION        e.g.  python3 diag_rpage.py 753308 0000753308-26-000081
Prints the filing's report list, the income-statement page it would pick, the first rows
of that page as the parser sees them, and what _rpage_quarter() returns.
Read-only; imports the parser from mcp/app.py.
"""
import sys, os, re, traceback
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp"))
import app

cik, accn = sys.argv[1], sys.argv[2]
base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/"
fs = app._sec_raw(base + "FilingSummary.xml").decode("utf-8", "replace")
reports = []
for blk in re.findall(r"<Report\b.*?</Report>", fs, re.S):
    f = re.search(r"<HtmlFileName>(.*?)</HtmlFileName>", blk, re.S)
    n = re.search(r"<ShortName>(.*?)</ShortName>", blk, re.S)
    if f and n:
        reports.append((n.group(1), f.group(1)))
print("reports:")
for sn, fn in reports[:10]:
    print(f"  {fn:<8} {sn[:90]}")
for sn, fn in reports[:12]:
    if re.search(r"(?i)income|operations|earnings", sn) and not re.search(r"(?i)comprehensive|parenthetical|equity|cash|balance|cover|tax|segment|detail|polic|note", sn):
        print(f"\nchosen: {fn}  ({sn})")
        try:
            html = app._sec_raw(base + fn).decode("utf-8", "replace")
            rows = re.findall(r"<tr\b.*?</tr>", html, re.S | re.I)
            print(f"rows found: {len(rows)}")
            for r in rows[:14]:
                print("   ", app._r_cells(r)[:6])
            print("\nparsed:", app._rpage_quarter(html))
        except Exception:
            traceback.print_exc()
        break
else:
    print("\nno report matched the income-statement filter")
