#!/usr/bin/env python3
"""Show where key legal terms fall in a ticker's stored 10-K legal text.
Usage: python3 diag_legal_offsets.py TICKER [term ...]
Nova's synthesis only sees the first 7,000 chars of this text, so any term
first appearing after ~7000 never reaches it."""
import glob, json, os, re, sys

tk = sys.argv[1].upper()
terms = sys.argv[2:] or ["Aetna", "product liability", "plaintiffs", "qui tam",
                         "Texas", "bellwether", "antitrust", "Complera"]
files = sorted(glob.glob(os.path.join("data", "research_*.json")))
if not files:
    sys.exit("no research files in ./data")
d = json.load(open(files[-1], encoding="utf-8"))
recs = d if isinstance(d, dict) else {r.get("ticker"): r for r in d}
rec = recs.get(tk)
if rec is None:
    for v in recs.values():
        if isinstance(v, dict) and v.get("ticker") == tk:
            rec = v
            break
if rec is None:
    sys.exit("ticker not found")
data = rec.get("data", rec)
text = data.get("legal_proceedings") or ""
if isinstance(text, dict):
    text = json.dumps(text)
print("file:", files[-1])
print("legal_proceedings length:", len(text), " (Nova sees first 7000 + 3000 secondary)")
for t in terms:
    pos = [m.start() for m in re.finditer(re.escape(t), text, re.I)]
    seen = "SEEN" if pos and pos[0] < 7000 else "NOT SEEN"
    print("  %-18s count=%-3d first=%-6s  %s" % (t, len(pos), pos[0] if pos else "-", seen if pos else "absent"))
