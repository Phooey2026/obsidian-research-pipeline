#!/usr/bin/env python3
"""
searxng_preflight.py — make sure SearXNG actually returns results before a pipeline run.

Oct 26 2026: SearXNG has needed a `docker restart searxng` before searches work
(0 results until restarted). Every web-search consumer (Nova earnings/legal, Mercury's
BDI and news, Jupiter's web fallbacks) degrades silently when that happens, so this
probes first and restarts the container if the probe comes back empty.

Usage:   python3 searxng_preflight.py            # probe, restart if empty, re-probe
         python3 searxng_preflight.py --check    # probe only, never restart (exit 1 if empty)
Chain it:  python3 searxng_preflight.py && python3 weekly_research.py

Exit code 0 = SearXNG is returning results, 1 = still empty after restarts.
"""
import json, os, subprocess, sys, time, urllib.parse, urllib.request

URL = os.environ.get("SEARXNG_URL", "http://localhost:8080")
CONTAINER = os.environ.get("SEARXNG_CONTAINER", "searxng")
QUERIES = ["Apple earnings press release", "Baltic Dry Index"]   # either one returning results = healthy
MAX_RESTARTS = 2


def probe() -> int:
    """Return the best result count across the probe queries; print why when it is low.

    SearXNG's JSON carries `unresponsive_engines` ([engine, reason] pairs, e.g.
    ['duckduckgo', 'CAPTCHA']) — logging them is how we find out WHICH engines are being
    blocked, which is the actual root cause behind needing a restart.
    """
    best = 0
    for q in QUERIES:
        try:
            u = f"{URL}/search?" + urllib.parse.urlencode({"q": q, "format": "json"})
            with urllib.request.urlopen(u, timeout=25) as r:
                d = json.loads(r.read())
            n = len(d.get("results", []))
            best = max(best, n)
            bad = d.get("unresponsive_engines", [])
            if bad or n == 0:
                print(f"  '{q}': {n} results; unresponsive engines: "
                      + (", ".join(f"{e[0]} ({e[1]})" for e in bad) if bad else "none reported"))
            engines = sorted({e for x in d.get("results", []) for e in x.get("engines", [])})
            if engines:
                print(f"  '{q}': answered by {', '.join(engines)}")
        except Exception as e:
            print(f"  probe '{q}': {type(e).__name__}: {str(e)[:80]}")
    return best


def main() -> int:
    check_only = "--check" in sys.argv
    n = probe()
    print(f"SearXNG probe: {n} results")
    if n > 0:
        return 0
    if check_only:
        print("SearXNG is returning nothing (not restarting: --check).")
        return 1
    for i in range(1, MAX_RESTARTS + 1):
        print(f"Restarting container '{CONTAINER}' (attempt {i}/{MAX_RESTARTS})...")
        try:
            subprocess.run(["docker", "restart", CONTAINER], check=True, timeout=120,
                           capture_output=True)
        except Exception as e:
            print(f"  docker restart failed: {type(e).__name__}: {str(e)[:100]}")
            return 1
        time.sleep(20)
        n = probe()
        print(f"SearXNG probe after restart: {n} results")
        if n > 0:
            return 0
    print("SearXNG still returns nothing — web-search features will be degraded.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
