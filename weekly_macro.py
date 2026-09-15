#!/usr/bin/env python3
"""
Weekly Macro Research Pipeline
Calls macromcp tools, saves:
  - data/macro_YYYYMMDD.json            raw data from all tools
  - reports/macro_summary_YYYYMMDD.md   human-readable weekly summary + forecast
  - macro_backdrop.yaml                 structured AI context (always latest)
  - macro_backdrop_YYYYMMDD.yaml        dated archive copy

Run via cron alongside weekly_research.py:
  0 6 * * 1 /usr/bin/python3 /home/jay/stock_dashboard/weekly_macro.py
"""

import json
import os
import sys
import datetime
import time
import subprocess
import requests

# ─── Configuration ─────────────────────────────────────────────────────────
MACROMCP_URL = "http://localhost:8643/mcp"

BASE_DIR    = "/home/jay/stock_dashboard"
DATA_DIR    = f"{BASE_DIR}/data"
REPORT_DIR  = f"{BASE_DIR}/reports"

ATLAS_PROFILE = os.path.expanduser("~/.hermes/profiles/atlas")

# Note: Atlas's SOUL.md is NOT used in generate_summary() — see that function
# for the explanation. The SOUL.md is used by the atlas CLI for interactive
# sessions only.


# ─── Atlas Hermes Profile Loader ───────────────────────────────────────────
def load_atlas_config() -> dict:
    """
    Read Atlas's model, base_url, and API key directly from her Hermes
    profile config.yaml and .env.  No model strings are hardcoded here —
    update the model any time with:

        atlas config set model.default openrouter/some/new-model

    Returns a dict with keys: model, base_url, api_key, soul

    Resolution order (mirrors Hermes precedence):
      1. ~/.hermes/profiles/atlas/config.yaml  → model, base_url
      2. ~/.hermes/profiles/atlas/.env         → OPENROUTER_API_KEY /
                                                 OPENAI_API_KEY / LLM_API_KEY
      3. ~/.hermes/.env (default profile)      → same keys as fallback
      4. Environment variables                 → same keys as final fallback
    """
    cfg = {
        "model":    "",
        "base_url": "",
        "api_key":  "",
        "soul":     "",
    }

    # ── 1. Parse config.yaml (minimal YAML — avoid PyYAML dependency) ───────
    config_path = os.path.join(ATLAS_PROFILE, "config.yaml")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                content = f.read()

            # Extract model.default  (handles both flat and nested forms)
            # Flat:   model: openrouter/minimax/minimax-m2-7
            # Nested: model:\n  default: openrouter/minimax/minimax-m2-7
            import re
            # Try nested first
            m = re.search(r'^\s{0,4}default\s*:\s*(.+)$', content, re.MULTILINE)
            if m:
                cfg["model"] = m.group(1).strip().strip('"\'')
            else:
                # Try flat  "model: <value>"  (not inside a sub-key)
                m = re.search(r'^model\s*:\s*(.+)$', content, re.MULTILINE)
                if m:
                    val = m.group(1).strip().strip('"\'')
                    # Skip if this line is itself a section header (no value)
                    if val and not val.startswith('#'):
                        cfg["model"] = val

            # Extract base_url
            m = re.search(r'base_url\s*:\s*(.+)$', content, re.MULTILINE)
            if m:
                val = m.group(1).strip().strip('"\'')
                if val and not val.startswith('#') and val != 'null':
                    cfg["base_url"] = val

        except Exception as e:
            print(f"  ⚠ Could not parse Atlas config.yaml: {e}")

    # ── 2. Parse .env files for API key ─────────────────────────────────────
    def _parse_env_file(path: str) -> dict:
        pairs = {}
        if not os.path.exists(path):
            return pairs
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    pairs[k.strip()] = v.strip().strip('"\'')
        except Exception:
            pass
        return pairs

    atlas_env   = _parse_env_file(os.path.join(ATLAS_PROFILE, ".env"))
    default_env = _parse_env_file(os.path.expanduser("~/.hermes/.env"))

    # Prefer Atlas-profile env, then default Hermes env, then shell env
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        val = (atlas_env.get(key)
               or default_env.get(key)
               or os.environ.get(key, ""))
        if val:
            cfg["api_key"] = val
            break

    # ── 3. Load SOUL.md ──────────────────────────────────────────────────────
    soul_path = os.path.join(ATLAS_PROFILE, "SOUL.md")
    if os.path.exists(soul_path):
        try:
            with open(soul_path, "r") as f:
                cfg["soul"] = f.read().strip()
        except Exception:
            pass

    # ── 4. Resolve base_url if still empty ───────────────────────────────────
    # OpenRouter is the most common case — infer from model string prefix
    if not cfg["base_url"]:
        if cfg["model"].startswith("openrouter/"):
            cfg["base_url"] = "https://openrouter.ai/api/v1/chat/completions"
            # OpenRouter model strings drop the "openrouter/" prefix in the call
            cfg["model"] = cfg["model"][len("openrouter/"):]
        elif cfg["model"]:
            # Generic fallback — assume OpenAI-compatible via env
            cfg["base_url"] = os.environ.get(
                "LLM_URL",
                "http://localhost:11434/v1/chat/completions"
            )

    return cfg

# ─── MCP Tool Caller (mirrors weekly_research.py pattern) ──────────────────

_RETRYABLE_ERRORS = ("HTTP 500", "HTTP 429", "HTTP 503", "rate limit",
                     "temporarily unavailable", "server error")


class _RetryableError(Exception):
    def __init__(self, snippet: str, wait: float):
        self.snippet = snippet
        self.wait    = wait
        super().__init__(snippet)


def call_tool(tool_name: str, arguments: dict,
              retries: int = 3, backoff: float = 5.0) -> str:
    """Call a macromcp tool via StreamableHTTP MCP protocol."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }

    last_result = "No data returned"

    for attempt in range(retries + 1):
        session = requests.Session()
        try:
            init_r = session.post(MACROMCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "weekly_macro", "version": "1.0"}
                }
            }, timeout=30)

            sid = init_r.headers.get("mcp-session-id", "")
            if sid:
                headers["mcp-session-id"] = sid

            tool_r = session.post(MACROMCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}
            }, timeout=120)

            # Handle SSE (text/event-stream) response format —
            # MCP servers return data: {...} lines, not raw JSON
            # NOTE: tool_r.text is deliberately NOT used. requests falls back to
            # ISO-8859-1 when a text/* content-type (e.g. text/event-stream) has
            # no explicit charset= parameter, which mojibakes any UTF-8 multi-byte
            # character (─, —, ↑, ⚠, etc.) into 2-3 wrong characters each.
            # Decoding tool_r.content explicitly avoids that guesswork.
            text = tool_r.content.decode('utf-8', errors='replace').strip()
            result_text = ""
            for line in text.split('\n'):
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                        for block in (data.get("result", {})
                                          .get("content", [])):
                            if block.get("type") == "text":
                                result_text += block.get("text", "")
                    except Exception:
                        continue
            # Fallback: try parsing as plain JSON if SSE parsing got nothing
            if not result_text:
                try:
                    data = json.loads(tool_r.content.decode('utf-8', errors='replace'))
                    for block in data.get("result", {}).get("content", []):
                        if block.get("type") == "text":
                            result_text += block.get("text", "")
                except Exception:
                    result_text = text

            last_result = result_text or "No text content returned"

            # Check for retryable errors embedded in result
            for err_phrase in _RETRYABLE_ERRORS:
                if err_phrase.lower() in last_result.lower():
                    wait = backoff * (2 ** attempt)
                    raise _RetryableError(last_result[:120], wait)

            return last_result

        except _RetryableError as e:
            if attempt < retries:
                print(f"      ⚠ Retryable error, waiting {e.wait:.0f}s... ({e.snippet[:60]})")
                time.sleep(e.wait)
                continue
            return f"Error (retries exhausted): {e.snippet}"

        except requests.exceptions.Timeout:
            if attempt < retries:
                wait = backoff * (2 ** attempt)
                print(f"      ⚠ Timeout, retrying in {wait:.0f}s...")
                time.sleep(wait)
                continue
            return f"Error: Request timed out after {retries + 1} attempts"

        except Exception as e:
            if attempt < retries:
                wait = backoff * (2 ** attempt)
                print(f"      ⚠ Error: {e}, retrying in {wait:.0f}s...")
                time.sleep(wait)
                continue
            return f"Error: {str(e)}"

        finally:
            session.close()

    return last_result


# ─── LLM Summary Generator ─────────────────────────────────────────────────

SUMMARY_USER = """Here is the raw macroeconomic data collected this week.
Produce the weekly briefing exactly as specified in your instructions.

--- RAW MACRO DATA ---
{raw_data}
--- END RAW DATA ---"""


def generate_summary(all_data: dict, run_date: str) -> str:
    """
    Send all macro data to Atlas via hermes CLI (atlas -z) and get the
    weekly briefing. Model and identity are configured in Atlas's Hermes
    profile — change the model any time with:

        atlas config set model.default openrouter/some-model

    Note: SOUL.md is intentionally NOT prepended here. When Atlas sees her
    full identity (which describes writing two output files), she produces a
    completion summary instead of the actual briefing prose. The prompt below
    is self-contained and sufficient for a clean oneshot response.
    """
    raw_text = "\n\n".join([
        f"=== {tool_name.upper().replace('_', ' ')} ===\n{content}"
        for tool_name, content in all_data.items()
        if isinstance(content, str) and content and not content.startswith("Error")
    ])

    prompt = f"""You are Atlas, macroeconomic research specialist at Obsidian Capital.
Your task: produce the weekly macroeconomic intelligence briefing
for {run_date}. Structure your report EXACTLY as follows:

## MACRO WEEKLY BRIEFING — {run_date}

### 1. RATE & MONETARY POLICY
[2-3 sentences on Fed stance, real rates, FOMC implications. If the raw
data includes a FEDERAL RESERVE COMMUNICATIONS section (FOMC statements,
speeches, or events like Jackson Hole), incorporate the qualitative
signal from those — they often move markets before showing up in any
numeric series below. If no communications were found, rely on the
numeric data alone as before.]

### 2. INFLATION PICTURE
[2-3 sentences on PCE, CPI, PPI trends, breakevens]

### 3. LABOR MARKET
[2-3 sentences on payrolls, unemployment, claims, wage pressure]

### 4. GROWTH & GDP
[2-3 sentences on GDP, ISM, leading indicators]

### 5. CREDIT & FINANCIAL CONDITIONS
[2-3 sentences on spreads, financial stress, credit availability]

### 6. HOUSING
[1-2 sentences on mortgage rates, activity]

### 7. CONSUMER SENTIMENT
[1-2 sentences on consumer confidence and behavior signals]

### 8. MARKET IMPLICATIONS
[3-4 bullets: sector/asset class tilts this week given the macro backdrop]
- 
- 
- 

### 9. WEEKLY FORECAST
[3-5 sentences: your forward-looking macro call for the next 4-8 weeks.
Include: rate expectations, inflation trajectory, recession probability,
and the single most important data release to watch next week.]

Be precise, data-driven, and actionable. Every claim must be anchored
to a specific data point from the raw data provided below.

--- RAW MACRO DATA ---
{raw_text[:12000]}
--- END RAW DATA ---"""

    try:
        result = subprocess.run(
            ["atlas", "-z", prompt],
            capture_output=True, text=True, timeout=300
        )
        output = (result.stdout or "").strip()
        if output:
            return output
        err = (result.stderr or "").strip()
        return (f"Atlas returned no output (exit {result.returncode})"
                + (f": {err[:200]}" if err else ""))
    except FileNotFoundError:
        return "Atlas unavailable (atlas command not found in PATH)"
    except subprocess.TimeoutExpired:
        return "Atlas timed out after 300s"
    except Exception as e:
        return f"Error generating summary: {e}"


# ─── Main Pipeline ─────────────────────────────────────────────────────────

# Tools to call, in order of importance. Each entry: (tool_name, display_label)
MACRO_TOOLS = [
    ("get_macro_snapshot",       "Macro Snapshot"),
    ("get_interest_rates",       "Interest Rates"),
    ("get_yield_curve",          "Yield Curve"),
    ("get_cpi_data",             "CPI Data"),
    ("get_pce_data",             "PCE Data"),
    ("get_ppi_data",             "PPI Data"),
    ("get_inflation_expectations","Inflation Expectations"),
    ("get_jobs_data",            "Jobs / Labor"),
    ("get_wages_data",           "Wages"),
    ("get_gdp_data",             "GDP & Growth"),
    ("get_credit_conditions",    "Credit Conditions"),
    ("get_housing_data",         "Housing"),
    ("get_sentiment_data",       "Sentiment"),
    ("get_fed_policy_data",      "Fed Policy / M2"),
    ("get_fed_communications",   "Fed Communications"),
    ("build_macro_backdrop",     "Build YAML Backdrop"),  # always last
]


def main():
    os.makedirs(DATA_DIR,   exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    run_date = datetime.datetime.now().strftime("%Y-%m-%d")
    stamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M")

    print(f"\n{'═'*54}")
    print(f"  Weekly Macro Research Pipeline — Obsidian Capital")
    print(f"  {run_date}")
    print(f"{'═'*54}\n")

    all_data: dict[str, str] = {}

    for tool_name, label in MACRO_TOOLS:
        print(f"  [{label}]...", end=" ", flush=True)
        t0 = time.time()
        result = call_tool(tool_name, {})
        elapsed = time.time() - t0

        if result.startswith("Error"):
            print(f"✗ ({elapsed:.1f}s) — {result[:80]}")
        else:
            print(f"✓ ({elapsed:.1f}s, {len(result)} chars)")

        all_data[tool_name] = result

    # ── Save raw JSON ───────────────────────────────────────────────────────
    json_path = f"{DATA_DIR}/macro_{stamp}.json"
    with open(json_path, "w") as f:
        json.dump(all_data, f, indent=2)
    print(f"\n✓ Raw data saved: {json_path}")

    # ── Save YAML backdrop ──────────────────────────────────────────────────
    yaml_content = all_data.get("build_macro_backdrop", "")
    if yaml_content and not yaml_content.startswith("Error"):
        # Always-latest copy (for injection into weekly_research.py prompts)
        latest_yaml = f"{BASE_DIR}/macro_backdrop.yaml"
        dated_yaml  = f"{BASE_DIR}/macro_backdrop_{run_date}.yaml"

        with open(latest_yaml, "w") as f:
            f.write(yaml_content)
        with open(dated_yaml, "w") as f:
            f.write(yaml_content)

        print(f"✓ Macro backdrop: {latest_yaml}")
        print(f"✓ Dated archive:  {dated_yaml}")
    else:
        print(f"⚠ YAML backdrop not saved — tool returned error")

    # ── Generate LLM summary ────────────────────────────────────────────────
    atlas_cfg = load_atlas_config()
    atlas_model_label = atlas_cfg.get("model", "unknown model")
    print(f"\n  [Atlas Summary] asking {atlas_model_label}...", end=" ", flush=True)
    t0 = time.time()
    summary = generate_summary(all_data, run_date)
    elapsed = time.time() - t0
    print(f"✓ ({elapsed:.1f}s, {len(summary)} chars)")

    # ── Save summary markdown ────────────────────────────────────────────────
    md_path     = f"{REPORT_DIR}/macro_summary_{stamp}.md"
    latest_md   = f"{BASE_DIR}/macro_summary.md"

    for path in (md_path, latest_md):
        with open(path, "w") as f:
            f.write(summary)

    print(f"✓ Summary saved:  {md_path}")
    print(f"✓ Latest summary: {latest_md}")

    # ── Print summary to console ─────────────────────────────────────────────
    print(f"\n{'─'*54}")
    print(summary[:3000])   # truncate for console readability
    if len(summary) > 3000:
        print(f"\n  ... (truncated — see {latest_md})")
    print(f"{'═'*54}\n")


if __name__ == "__main__":
    main()
