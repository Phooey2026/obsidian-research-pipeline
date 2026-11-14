#!/usr/bin/env python3
"""Dump Jansky's latest review as plain text for review.
Usage: python3 show_jansky.py > jansky_report.txt   (then attach the .txt)
Read-only. Prints posture, executive summary, verdict distribution, Python pre-check
flags, the Atlas/Mercury/Nova/synthesis notes, notes for flagged sectors, and trade decisions."""
import json

r = json.load(open("jansky_latest.json", encoding="utf-8"))

def h(t):
    print("\n" + "=" * 8 + " " + t + " " + "=" * 8)

print("keys:", sorted(r.keys()))
print("posture:", r.get("overall_posture"))
print("attention:", sorted(r.get("tickers_needing_attention", [])))
h("EXECUTIVE SUMMARY")
print(r.get("executive_summary", ""))
jup = r.get("agent_reviews", {}).get("jupiter", {})
h("VERDICT DISTRIBUTION")
print(json.dumps(jup.get("verdict_distribution"), indent=1))
h("PYTHON FLAGS (per sector, per ticker)")
for sec, fl in (r.get("python_flags") or {}).items():
    print("[%s]" % sec)
    for tk, items in fl.items():
        for it in (items if isinstance(items, list) else [items]):
            print("  %s: %s" % (tk, str(it)[:600]))
pn = r.get("pass_notes", {})
for k in ("atlas", "mercury", "nova", "synthesis"):
    h("PASS NOTES: " + k)
    print(pn.get(k, ""))
flagged = set((r.get("python_flags") or {}).keys())
for sec in sorted(flagged):
    h("SECTOR NOTES: " + sec)
    print(pn.get(sec, ""))
h("TRADE DECISIONS")
for d in r.get("trade_review", {}).get("decisions", []):
    print(json.dumps(d)[:700])
