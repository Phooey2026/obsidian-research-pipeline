#!/usr/bin/env python3
"""Verdict mix of the latest Jupiter run, with Nova legal risk alongside. Read-only.
Usage: python3 verdict_mix.py [--why]   (run from ~/stock_dashboard after weekly_research.py)
--why also prints the 600 characters after "Overall Verdict" for every High/Critical-legal
name that is not ACCUMULATE, so you can see whether legal risk is the stated reason.
Shows: ACCUMULATE/WATCH/AVOID/none counts, every ACCUMULATE name with its legal risk,
and every High/Critical-legal name with its verdict (is the legal rule blocking ACCUMULATE?)."""
import glob, json, os, re, sys

VRE = re.compile(r'Overall Verdict\**:?\s*\**\s*(ACCUMULATE|WATCH|AVOID)', re.I)
files = sorted(glob.glob(os.path.join("data", "research_*.json")))
if not files:
    raise SystemExit("no research files in ./data")
d = json.load(open(files[-1], encoding="utf-8"))
recs = d if isinstance(d, dict) else {r.get("ticker"): r for r in d}
nova = json.load(open("nova_supplemental.json", encoding="utf-8"))
print("research file:", files[-1])

rows = []
why = {}
for tk, rec in sorted(recs.items()):
    if not isinstance(rec, dict):
        continue
    s = rec.get("summary") or (rec.get("data") or {}).get("summary") or ""
    m = VRE.search(s)
    v = m.group(1).upper() if m else "NONE"
    risk = (nova.get(tk, {}).get("legal") or {}).get("risk_level", "-")
    rows.append((tk, v, risk))
    if m:
        why[tk] = s[m.end():m.end() + 600].strip().replace("\n", " ")

cnt = {}
for r in rows:
    cnt[r[1]] = cnt.get(r[1], 0) + 1
print("verdicts:", ", ".join("%s=%d" % kv for kv in sorted(cnt.items())), " (n=%d)" % len(rows))
print("\nACCUMULATE names (legal risk):")
for r in rows:
    if r[1] == "ACCUMULATE":
        print("  %-6s legal=%s" % (r[0], r[2]))
hi = [r for r in rows if r[2] in ("High", "Critical")]
print("\nHigh/Critical legal names (%d) and their verdicts:" % len(hi))
for r in hi:
    print("  %-6s %-10s legal=%s" % r)
if "--why" in sys.argv:
    print("\nStated reasons (High/Critical legal, not ACCUMULATE):")
    for r in hi:
        if r[1] not in ("ACCUMULATE", "NONE"):
            print("\n%s  %s  legal=%s\n  %s" % (r[0], r[1], r[2], why.get(r[0], "")))
none = [r[0] for r in rows if r[1] == "NONE"]
if none:
    print("\nNo parsable 'Overall Verdict' (%d): %s" % (len(none), " ".join(none)))
