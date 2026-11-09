#!/usr/bin/env python3
"""diag_nova_record.py TICKER [term ...]  — show what Nova holds for a ticker.

Prints the record's structure (keys, dates, sizes), then every passage mentioning any of the
terms (default: 'qui tam', 'Texas', 'trial', 'verdict', 'settle'), with 300 chars of context.
Works on nova_supplemental.json in the current directory (list or dict layout)."""
import json, re, sys

if len(sys.argv) < 2:
    sys.exit(__doc__)
tk = sys.argv[1].upper()
terms = sys.argv[2:] or ["qui tam", "Texas", "trial", "verdict", "settle"]
d = json.load(open("nova_supplemental.json"))
if isinstance(d, dict):
    rec = d.get(tk) or next((v for v in d.values() if isinstance(v, dict) and v.get("ticker") == tk), None)
else:
    rec = next((x for x in d if x.get("ticker") == tk), None)
if rec is None:
    sys.exit("no record for " + tk)

def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, path + "." + k if path else k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk(v, "%s[%d]" % (path, i))
    else:
        yield path, o

leaves = list(walk(rec))
print("== %s: %d fields" % (tk, len(leaves)))
for p, v in leaves:
    sv = str(v)
    print("  %-40s %s" % (p, (sv[:90] + "...") if len(sv) > 90 else sv))
print()
for p, v in leaves:
    sv = str(v)
    for t in terms:
        for m in re.finditer(re.escape(t), sv, re.I):
            a, b = max(0, m.start() - 300), min(len(sv), m.end() + 300)
            print("[%s | %s] ...%s..." % (p, t, sv[a:b].replace("\n", " ")))
            break
