#!/usr/bin/env python3
"""
check_mcp_tool.py — Obsidian Capital

Call one or more tools on a LIVE MCP server and print what the agents would
see, flagging red-flag text (STALE, UNAVAILABLE, Error, ...). Lets you test
data feeds any day without running weekly_mercury.py / weekly_research.py.

Servers: web (8642) | macro (8643) | nova (8644) | mercury (8645)

Usage:
    python3 check_mcp_tool.py mercury                      # the Mercury feeds changed in Oct 2026
    python3 check_mcp_tool.py mercury get_noaa_drought get_livestock_prices
    python3 check_mcp_tool.py web get_stock_news '{"ticker":"MU"}'
    python3 check_mcp_tool.py web get_earnings '{"ticker":"MU"}'
    python3 check_mcp_tool.py web get_short_interest '{"ticker":"MU"}'
    python3 check_mcp_tool.py web get_institutional '{"ticker":"MU"}'
    python3 check_mcp_tool.py mercury --list               # list a server's tools
"""

import sys
import json
import requests

PORTS = {"web": 8642, "macro": 8643, "nova": 8644, "mercury": 8645}

# Default checks when only the server name is given
DEFAULTS = {
    "mercury": ["get_central_bank_rates", "get_forex_rates", "get_livestock_prices",
                "get_crop_progress", "get_noaa_drought", "get_wasde_summary"],
    "web":     [("get_stock_news", {"ticker": "MU"}),
                ("get_earnings", {"ticker": "MU"}),
                ("get_short_interest", {"ticker": "MU"}),
                ("get_institutional", {"ticker": "MU"})],
}

RED_FLAGS = ["stale", "unavailable", "error", "low confidence",
             "missing", "no recent", "returned no", "failed", "traceback", "may not be reflected"]


def parse_response(r) -> dict:
    # SSE responses carry no charset, so requests guesses ISO-8859-1 and turns
    # ✓ — ▲ into mojibake. MCP is always UTF-8.
    r.encoding = "utf-8"
    for line in r.text.strip().split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except Exception:
                continue
    try:
        return r.json()
    except Exception:
        return {}


class Client:
    def __init__(self, port: int):
        self.url = f"http://localhost:{port}/mcp"
        self.s = requests.Session()
        self.h = {"Content-Type": "application/json",
                  "Accept": "application/json, text/event-stream"}
        r = self.s.post(self.url, headers=self.h, timeout=15, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "check-tool", "version": "1.0"}}})
        sid = r.headers.get("mcp-session-id", "")
        if sid:
            self.h["mcp-session-id"] = sid
        self.s.post(self.url, headers=self.h, timeout=10, json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def list_tools(self):
        r = self.s.post(self.url, headers=self.h, timeout=15, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        return parse_response(r).get("result", {}).get("tools", [])

    def call(self, name: str, args: dict) -> str:
        r = self.s.post(self.url, headers=self.h, timeout=180, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": args}})
        data = parse_response(r)
        if "error" in data:
            return f"[MCP ERROR] {data['error']}"
        content = data.get("result", {}).get("content", [])
        return "\n".join(c.get("text", "") for c in content)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in PORTS:
        print(__doc__)
        return 1
    server = sys.argv[1]
    rest = sys.argv[2:]
    try:
        client = Client(PORTS[server])
    except Exception as e:
        print(f"✗ Could not reach {server} on port {PORTS[server]}: {e}")
        return 1

    if rest and rest[0] == "--list":
        for t in client.list_tools():
            print(f"• {t['name']}")
        return 0

    # Build the list of (tool, args)
    jobs = []
    if not rest:
        for d in DEFAULTS.get(server, []):
            jobs.append((d, {}) if isinstance(d, str) else d)
        if not jobs:
            print(f"No default checks for '{server}'. Name a tool (use --list).")
            return 1
    else:
        i = 0
        while i < len(rest):
            name, args = rest[i], {}
            if i + 1 < len(rest) and rest[i + 1].lstrip().startswith("{"):
                args = json.loads(rest[i + 1])
                i += 1
            jobs.append((name, args))
            i += 1

    flagged = []
    for name, args in jobs:
        print("═" * 70)
        print(f"  {server}.{name}  {json.dumps(args) if args else ''}")
        print("═" * 70)
        out = client.call(name, args)
        print(out)
        hits = sorted({f for f in RED_FLAGS if f in out.lower()})
        if hits:
            flagged.append((name, hits))
        print()

    print("═" * 70)
    if flagged:
        print("Red-flag words found (review the output above):")
        for name, hits in flagged:
            print(f"  • {name}: {', '.join(hits)}")
    else:
        print("No red-flag words found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
