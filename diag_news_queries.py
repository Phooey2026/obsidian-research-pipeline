#!/usr/bin/env python3
"""Try candidate legal-news queries against the same search backend novamcp uses.
Usage: python3 diag_news_queries.py "Gilead Sciences, Inc." [extra query ...]
Reads SEARCH_PROVIDER / SEARXNG_URL from the environment or ./.env."""
import os, sys, time, json

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

PROVIDER = _env("SEARCH_PROVIDER", "ddg").lower()
SEARX = _env("SEARXNG_URL")
EXCL = "-site:grokipedia.com"

def search(q, limit=6):
    q = q + " " + EXCL
    if PROVIDER == "searxng" and SEARX:
        import requests
        r = requests.get(SEARX.rstrip("/") + "/search", params={"q": q, "format": "json"}, timeout=20)
        r.encoding = "utf-8"
        return [(x.get("title", ""), x.get("url", ""), x.get("content", "")) for x in r.json().get("results", [])[:limit]]
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    return [(x.get("title", ""), x.get("href", ""), x.get("body", "")) for x in DDGS().text(q, max_results=limit)]

co = sys.argv[1]
qs = [
    co + " lawsuit litigation 2024 2025 2026",
    co + " settlement agrees to pay October 2026",
    co + " trial verdict ruling settles lawsuit 2026",
    co + " Texas attorney general settlement",
] + sys.argv[2:]
print("provider:", PROVIDER)
for q in qs:
    print("\n### " + q)
    try:
        for t, u, c in search(q):
            flag = " <-- settle" if "settle" in (t + c).lower() else ""
            print("  - %s\n    %s%s" % (t[:110], u[:110], flag))
    except Exception as e:
        print("  ERROR", e)
    time.sleep(3)
