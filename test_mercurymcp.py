#!/usr/bin/env python3
"""
Quick smoke test for mercurymcp (port 8645).
Initializes an MCP session and lists all registered tools.
Run on Jetson: python3 test_mercurymcp.py
"""

import requests
import json

URL = "http://localhost:8645/mcp"

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

def parse_response(r) -> dict:
    text = r.text.strip()
    for line in text.split('\n'):
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

session = requests.Session()

# 1. Initialize
print("1. Initializing MCP session...")
r = session.post(URL, headers=headers, json={
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke-test", "version": "1.0"}
    }
}, timeout=15)

data = parse_response(r)
sid = r.headers.get("mcp-session-id", "")
if sid:
    headers["mcp-session-id"] = sid

if "result" in data:
    info = data["result"].get("serverInfo", {})
    print(f"   ✓ Connected — server: {info.get('name','?')} v{info.get('version','?')}")
else:
    print(f"   ✗ Init failed: {data}")

# 2. Notify initialized
session.post(URL, headers=headers, json={
    "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}
}, timeout=10)

# 3. List tools
print("\n2. Listing registered tools...")
r = session.post(URL, headers=headers, json={
    "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
}, timeout=15)

data = parse_response(r)
tools = data.get("result", {}).get("tools", [])
print(f"   ✓ {len(tools)} tools registered:\n")
for t in tools:
    print(f"   • {t['name']}")
    desc = t.get('description', '')
    if desc:
        print(f"     {desc[:80].strip()}{'...' if len(desc) > 80 else ''}")

# 4. Quick live call — forex rates (fastest tool, no API key needed)
print("\n3. Live call: get_forex_rates ...")
r = session.post(URL, headers=headers, json={
    "jsonrpc": "2.0", "id": 3,
    "method": "tools/call",
    "params": {"name": "get_forex_rates", "arguments": {}}
}, timeout=60)

data = parse_response(r)
result_text = ""
for item in data.get("result", {}).get("content", []):
    if item.get("type") == "text":
        result_text += item.get("text", "")

if result_text:
    print(f"   ✓ Response ({len(result_text):,} chars):\n")
    print("   " + "\n   ".join(result_text.split('\n')[:20]))
    if result_text.count('\n') > 20:
        print("   ... (truncated)")
else:
    print(f"   ✗ No result: {data}")

print("\n✓ Smoke test complete.")
