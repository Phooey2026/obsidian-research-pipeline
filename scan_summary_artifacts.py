#!/usr/bin/env python3
"""Find leaked reasoning and stray foreign-language fragments in Jupiter's summaries.
Read-only. Usage: python3 scan_summary_artifacts.py [TICKER ...]
Prints each hit with ~60 chars of context, then a count of affected tickers."""
import glob, json, os, re, sys

files = sorted(glob.glob(os.path.join("data", "research_*.json")))
d = json.load(open(files[-1], encoding="utf-8"))
recs = d if isinstance(d, dict) else {r.get("ticker"): r for r in d}
only = set(a.upper() for a in sys.argv[1:])

# reasoning that should never reach a published summary
LEAK = re.compile(r"(\bwait\b\s*[,—–-]|\bhmm\b|\blet me\b|\bactually,|\bon second thought\b|"
                  r"\bI need to\b|\bI should\b|\blet's recompute\b|\brecalculat)", re.I)
# letters outside plain English/Latin-1 punctuation that are not in typical company or person names
ODD = re.compile(r"[À-ɏͰ-῿　-鿿]")
ITAL = re.compile(r"\b(Nessun[oa]?|Nessuna|Mildly Restrictive|Smuri)\b")
hits = {}
for tk, rec in sorted(recs.items()):
    if only and tk not in only:
        continue
    if not isinstance(rec, dict):
        continue
    sm = rec.get("summary") or ""
    if not isinstance(sm, str):
        continue
    for kind, rx in (("LEAK", LEAK), ("NONLATIN", ODD), ("FOREIGN", ITAL)):
        for m in rx.finditer(sm):
            a = max(0, m.start() - 60)
            hits.setdefault(tk, []).append("%-8s ...%s" % (kind, sm[a:m.end() + 60].replace("\n", " ")))
for tk, hs in hits.items():
    print("== " + tk)
    for h in hs[:6]:
        print("   " + h)
print("\n%d tickers with hits (of %d checked)" % (len(hits), len(only) or len(recs)))
