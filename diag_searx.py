#!/usr/bin/env python3
"""Probe the SearXNG instance: which engines answer, and do query words matter?
Usage: python3 diag_searx.py            (uses SEARXNG_URL from env or ./.env)"""
import os, time

def _env(k, d=""):
    if os.environ.get(k):
        return os.environ[k].strip()
    try:
        for line in open(".env", encoding="utf-8"):
            if line.startswith(k + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return d

import requests
URL = _env("SEARXNG_URL").rstrip("/") + "/search"
print("SearXNG:", URL)

def run(label, q, **extra):
    params = {"q": q, "format": "json"}
    params.update(extra)
    try:
        r = requests.get(URL, params=params, timeout=25)
        r.encoding = "utf-8"
        d = r.json()
    except Exception as e:
        print("\n### %s\n  ERROR %s" % (label, e))
        return
    res = d.get("results", [])
    print("\n### %s  | q=%r %s" % (label, q, extra or ""))
    print("  results=%d  unresponsive=%s" % (len(res), d.get("unresponsive_engines")))
    for x in res[:6]:
        print("  - [%s] %s\n      %s" % (x.get("engine"), (x.get("title") or "")[:100], (x.get("url") or "")[:100]))
    time.sleep(3)

run("baseline (what novamcp sends)", "Gilead Sciences, Inc. settlement agrees to pay October 2026 -site:grokipedia.com")
run("no exclusion operator", "Gilead Sciences, Inc. settlement agrees to pay October 2026")
run("short, no suffix", "Gilead Texas Medicaid settlement $45 million")
run("news category", "Gilead Texas Medicaid settlement $45 million", categories="news")
run("news + month", "Gilead settlement", categories="news", time_range="month")
run("quoted phrase", '"Gilead" "Texas" settlement Medicaid kickback')
run("control: unrelated", "Texas Medicaid kickback settlement Paxton")
