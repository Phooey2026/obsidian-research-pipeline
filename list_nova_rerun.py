#!/usr/bin/env python3
"""Which Nova legal records predate this week's fixes? Read-only.
Usage: python3 list_nova_rerun.py [--cutoff 2026-11-09]
Records dated before the cutoff used the old news search (junk results); records
dated 11-07..11-08 also had the 7,000-char cut on the 10-K legal text. Court
errors saved as data are flagged separately. Prints re-run commands, High/Critical first."""
import json, sys

CUT = "2026-11-09"
if "--cutoff" in sys.argv:
    CUT = sys.argv[sys.argv.index("--cutoff") + 1]
nova = json.load(open("nova_supplemental.json", encoding="utf-8"))
rows = []
for tk, rec in sorted(nova.items()):
    n = rec.get("legal") if isinstance(rec, dict) else None
    if not n:
        continue
    d = str(n.get("research_date", "?"))[:10]
    rows.append((tk, d, n.get("risk_level", "?"), n.get("research_confidence", "?")))
old = [r for r in rows if r[1] < CUT]
print("%d Nova legal records; %d dated before %s" % (len(rows), len(old), CUT))
by_date = {}
for r in rows:
    by_date[r[1]] = by_date.get(r[1], 0) + 1
print("by date:", ", ".join("%s=%d" % kv for kv in sorted(by_date.items())))
rank = {"Critical": 0, "High": 1, "Moderate": 2, "Low": 3}
old.sort(key=lambda r: (rank.get(r[2], 4), r[0]))
hi = [r[0] for r in old if r[2] in ("Critical", "High")]
rest = [r[0] for r in old if r[2] not in ("Critical", "High")]
print("\nHigh/Critical first (%d):" % len(hi))
if hi: print("  python3 nova_legal.py --force " + " ".join(hi))
print("\nThe rest (%d), in batches of 10:" % len(rest))
for i in range(0, len(rest), 10):
    print("  python3 nova_legal.py --force " + " ".join(rest[i:i+10]))
print("\nTICKER DATE       RISK     CONF")
for r in old:
    print("%-6s %-10s %-8s %s" % r)
