#!/usr/bin/env python3
"""Find Nova legal records likely hurt by (1) the old 7,000-char cut-off on the
10-K legal text, or (2) junk news results (homepages / Wikipedia).
Read-only. Usage: python3 scan_nova_legal.py [--min-len 7000]
Prints a ready-to-run re-run command for the affected tickers."""
import glob, json, os, re, sys

MIN = 7000
if "--min-len" in sys.argv:
    MIN = int(sys.argv[sys.argv.index("--min-len") + 1])

files = sorted(glob.glob(os.path.join("data", "research_*.json")))
if not files:
    sys.exit("no research files in ./data")
d = json.load(open(files[-1], encoding="utf-8"))
recs = d if isinstance(d, dict) else {r.get("ticker"): r for r in d}
nova = json.load(open("nova_supplemental.json", encoding="utf-8"))
print("research file:", files[-1])

JUNK = re.compile(r"wikipedia|fandom|merriam|investor|gilead\.com/|/company\b|stock price|quote", re.I)
rows = []
for tk, rec in sorted(recs.items()):
    if not isinstance(rec, dict):
        continue
    data = rec.get("data", rec)
    txt = data.get("legal_proceedings") or ""
    if isinstance(txt, dict):
        txt = json.dumps(txt)
    n = nova.get(tk, {}).get("legal") or {}
    if not n:
        continue
    raw = n.get("web_findings_raw") or ""
    blocks = re.split(r"\n\[\d+\] ", raw)[1:]
    junk = sum(1 for b in blocks if JUNK.search(b[:400]))
    name = (nova.get(tk, {}).get("company_name") or "").split()
    key = name[0].lower() if name else ""
    # blocks that do not even name the company (only judged on the 2000 stored chars)
    off = sum(1 for b in blocks if key and key not in b.lower())
    cut = len(txt) > MIN
    news_bad = bool(blocks) and (junk + off) >= max(2, len(blocks) // 2)
    if cut or news_bad:
        rows.append((tk, len(txt), n.get("research_date", "?"), n.get("risk_level", "?"),
                     junk, off, len(blocks), cut, news_bad))

print("\n%-6s %8s %-11s %-9s %s" % ("TICKER", "TXT_LEN", "LEGAL_DATE", "RISK", "NEWS(junk/offtopic/total)"))
for r in rows:
    tag = []
    if r[7]: tag.append("CUT>%d" % MIN)
    if r[8]: tag.append("NEWS?")
    print("%-6s %8d %-11s %-9s %d/%d/%d  %s" % (r[0], r[1], r[2], r[3], r[4], r[5], r[6], " ".join(tag)))
cut_t = [r[0] for r in rows if r[7]]
print("\n%d tickers with legal text > %d chars; %d with likely-junk news; %d of 128 checked have a Nova legal record"
      % (len(cut_t), MIN, sum(1 for r in rows if r[8]), sum(1 for v in nova.values() if v.get("legal"))))
if rows:
    print("\nRe-run (after novamcp restart, one at a time is gentler on search):")
    print("  python3 nova_legal.py --force " + " ".join(r[0] for r in rows))
