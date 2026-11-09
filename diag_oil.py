#!/usr/bin/env python3
"""diag_oil.py — side-by-side FRED daily WTI (DCOILWTICO) and Brent (DCOILBRENTEU), last 40 obs,
plus the 28-day change each way, so a Brent -14.7% / WTI +3.5% split can be traced to real prices.
Reads FRED_API_KEY from the environment or from ./.env"""
import json, os, re, sys, urllib.request
from datetime import datetime

key = os.environ.get("FRED_API_KEY", "")
if not key and os.path.exists(".env"):
    for ln in open(".env"):
        m = re.match(r"\s*(?:export\s+)?FRED_API_KEY\s*=\s*['\"]?([^'\"\s]+)", ln)
        if m:
            key = m.group(1)
if not key:
    sys.exit("no FRED_API_KEY in environment or ./.env")

def series(sid):
    url = ("https://api.stlouisfed.org/fred/series/observations?series_id=%s&sort_order=desc"
           "&limit=40&file_type=json&api_key=%s" % (sid, key))
    obs = json.load(urllib.request.urlopen(url, timeout=30))["observations"]
    return {o["date"]: float(o["value"]) for o in obs if o["value"] not in (".", "")}

w, b = series("DCOILWTICO"), series("DCOILBRENTEU")
dates = sorted(set(w) | set(b))
print("%-11s %9s %9s %8s" % ("date", "WTI", "Brent", "B-W"))
for d in dates:
    wv, bv = w.get(d), b.get(d)
    print("%-11s %9s %9s %8s" % (d, "%.2f" % wv if wv else "-", "%.2f" % bv if bv else "-",
                               "%.2f" % (bv - wv) if wv and bv else "-"))

def chg(s, end):
    ds = sorted(x for x in s if x <= end)
    t0 = datetime.strptime(ds[-1], "%Y-%m-%d")
    base = [x for x in ds if (t0 - datetime.strptime(x, "%Y-%m-%d")).days >= 28]
    return ds[-1], base[-1], (s[ds[-1]] / s[base[-1]] - 1) * 100

end = min(max(w), max(b))
print("\ncommon end date:", end)
for name, s in (("WTI", w), ("Brent", b)):
    e, bd, c = chg(s, end)
    print("%-6s %s vs %s: %+.1f%%" % (name, e, bd, c))
