#!/usr/bin/env python3
"""
repair_summaries.py — Surgical summary repair for weekly_research output.

Identifies incomplete summaries in an existing research JSON and re-runs
ONLY the generate_summary step for specified tickers (or auto-detected
broken ones), patching the results in-place without disturbing other records.

Usage:
    # Auto-detect and repair broken summaries in the latest research file
    python3 repair_summaries.py

    # Repair specific tickers
    python3 repair_summaries.py TGT UPS AMT

    # Dry run — show what would be repaired without making changes
    python3 repair_summaries.py --dry-run

    # Repair specific file
    python3 repair_summaries.py --file data/research_20260613_1400.json TGT UPS AMT
"""

import os
import sys
import json
import glob
import argparse
import datetime

# ── Path setup ────────────────────────────────────────────────────────────────
BASE_DIR = os.environ.get(
    "STOCK_BASE_DIR",
    os.path.dirname(os.path.abspath(__file__))
)
DATA_DIR   = os.path.join(BASE_DIR, "data")
MACRO_FILE = os.path.join(BASE_DIR, "macro_backdrop.yaml")
NOVA_FILE  = os.path.join(BASE_DIR, "nova_supplemental.json")
CONFIG_FILE= os.path.join(BASE_DIR, "obsidian_config.json")
sys.path.insert(0, BASE_DIR)

# ── Load config for pipeline-wide constants ───────────────────────────────────
def _load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

_config = _load_config()

# ── Broken summary detection ──────────────────────────────────────────────────
# Min chars read from obsidian_config.json; fallback to 2000
MIN_SUMMARY_CHARS = _config.get("summary_quality", {}).get("min_summary_chars", 2000)

BROKEN_SIGNATURES = [
    "⚠️ No reply",
    "⚠ No reply",
    "model returned empty content",
    "No reply from model",
    "the full analysis is above",
    "Want me to run another ticker",
    "Shall I proceed",
    "Write this to a file",
    "Error generating summary",
    "Report complete and saved to",   # GE file-saving behavior
    "Let me produce the full",        # self-talk leak
]

# Verdict words that should appear in a complete summary
VERDICT_WORDS = ["ACCUMULATE", "WATCH", "AVOID"]


def is_broken(summary: str, min_chars: int = None) -> tuple:
    """Return (broken, reason) for a summary string."""
    threshold = min_chars if min_chars is not None else MIN_SUMMARY_CHARS
    if not summary or len(summary.strip()) == 0:
        return True, "empty"
    for sig in BROKEN_SIGNATURES:
        if sig.lower() in summary.lower():
            return True, f"error signature: '{sig}'"
    if len(summary) < threshold:
        return True, f"too short ({len(summary)} chars < {threshold})"
    # Check for missing verdict — complete summaries always contain one
    if not any(v in summary for v in VERDICT_WORDS):
        return True, "no verdict (ACCUMULATE/WATCH/AVOID missing — likely truncated)"
    return False, ""


def find_latest_research_file():
    pattern = os.path.join(DATA_DIR, "research_*.json")
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def load_macro_backdrop():
    if os.path.exists(MACRO_FILE):
        with open(MACRO_FILE, encoding="utf-8") as f:
            content = f.read()
        print(f"  ✓ Atlas macro backdrop loaded ({len(content):,} chars)")
        return content
    print("  ⚠ No macro backdrop found")
    return ""


def load_nova_supplemental():
    if os.path.exists(NOVA_FILE):
        with open(NOVA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        print(f"  ✓ Nova supplemental loaded ({len(data)} ticker records)")
        return data
    print("  ⚠ No Nova supplemental found")
    return {}


def main():
    parser = argparse.ArgumentParser(
        description="Surgically repair broken summaries in a research JSON file."
    )
    parser.add_argument(
        "tickers", nargs="*",
        help="Tickers to repair. If omitted, auto-detects broken summaries."
    )
    parser.add_argument(
        "--file", "-f", default=None,
        help="Research JSON file to repair. Defaults to the latest."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be repaired without making changes."
    )
    parser.add_argument(
        "--min-chars", type=int, default=MIN_SUMMARY_CHARS,
        help=f"Minimum chars for a healthy summary (default: {MIN_SUMMARY_CHARS})"
    )
    parser.add_argument(
        "--refetch", action="store_true",
        help=(
            "Re-fetch all MCP data for the ticker(s) before regenerating the summary. "
            "Use when the data itself is missing or corrupt (e.g. after a connection drop), "
            "not just when the summary is bad. Patches data + summary in-place."
        )
    )
    args = parser.parse_args()

    min_chars = args.min_chars   # local variable — no global needed

    print()
    print("════════════════════════════════════════════════════")
    print("  Summary Repair Tool — Obsidian Capital")
    print(f"  {datetime.date.today().isoformat()}")
    if args.refetch:
        print("  Mode: FULL REFETCH (data + summary)")
    else:
        print("  Mode: SUMMARY ONLY")
    print("════════════════════════════════════════════════════")
    print()

    # ── Find research file ────────────────────────────────────────────────────
    research_file = args.file or find_latest_research_file()
    if not research_file:
        print("✗ No research JSON found in data/")
        sys.exit(1)

    print(f"  Research file: {research_file}")
    with open(research_file, encoding="utf-8") as f:
        research_data = json.load(f)
    print(f"  Loaded {len(research_data)} ticker records")
    print()

    # ── Load supporting context ───────────────────────────────────────────────
    macro_backdrop = load_macro_backdrop()
    nova_data      = load_nova_supplemental()
    print()

    # ── Import from weekly_research ───────────────────────────────────────────
    try:
        from weekly_research import generate_summary, research_ticker
    except ImportError as e:
        print(f"✗ Cannot import from weekly_research.py: {e}")
        sys.exit(1)

    # ── Detect bad data (stub responses from failed MCP calls) ────────────────
    # A healthy stock_info field is typically 500+ chars.
    # Stub/error responses from webmcp are typically under 250 chars.
    DATA_STUB_THRESHOLD = 250

    def has_bad_data(item: dict) -> tuple[bool, str]:
        """Return (bad, reason) if the data dict looks like failed MCP fetches."""
        data = item.get("data", {})
        if not data:
            return True, "empty data dict"
        stock_info = data.get("stock_info", "")
        if len(stock_info) < DATA_STUB_THRESHOLD:
            return True, f"stock_info too short ({len(stock_info)} chars — likely failed MCP fetch)"
        return False, ""

    # ── Identify tickers to repair ────────────────────────────────────────────
    ticker_map = {item["ticker"].upper(): item for item in research_data}

    if args.tickers:
        to_repair = []
        for t in args.tickers:
            t = t.upper()
            if t not in ticker_map:
                print(f"  ⚠ {t} not found in research file — skipping")
                continue
            item    = ticker_map[t]
            summary = item.get("summary", "")
            bad_data, data_reason = has_bad_data(item)
            _, sum_reason = is_broken(summary, min_chars)

            if args.refetch:
                reason = data_reason or sum_reason or "manually specified (refetch)"
            else:
                reason = sum_reason or "manually specified"

            to_repair.append((t, reason, bad_data))
        print(f"  Manual repair list: {len(to_repair)} ticker(s)")
    else:
        to_repair = []
        for item in research_data:
            ticker    = item.get("ticker", "").upper()
            summary   = item.get("summary", "")
            bad_data, data_reason = has_bad_data(item)
            broken, sum_reason   = is_broken(summary, min_chars)

            if bad_data:
                # Bad data always needs a refetch — flag it clearly
                to_repair.append((ticker, data_reason, True))
            elif broken:
                to_repair.append((ticker, sum_reason, False))

        # Split for reporting
        needs_refetch  = [(t, r) for t, r, bad in to_repair if bad]
        summary_only   = [(t, r) for t, r, bad in to_repair if not bad]
        print(f"  Auto-detected {len(to_repair)} tickers needing repair:")
        if needs_refetch:
            print(f"    {len(needs_refetch)} need full data refetch (bad/stub data)")
            for t, r in needs_refetch:
                print(f"      {t:8} — {r}")
        if summary_only:
            print(f"    {len(summary_only)} need summary-only repair (good data, bad summary)")
            for t, r in summary_only:
                print(f"      {t:8} — {r}")

        if needs_refetch and not args.refetch:
            print()
            print("  ⚠ Tickers with bad data detected. Re-run with --refetch to fix them:")
            print(f"    python3 repair_summaries.py --refetch {' '.join(t for t, _ in needs_refetch)}")
            print()

    if not to_repair:
        print()
        print("  ✓ No broken summaries or bad data found — nothing to repair.")
        print()
        return

    if not args.tickers:
        # Already printed above in detail
        pass
    else:
        print()
        print("  Tickers to repair:")
        for ticker, reason, bad_data in to_repair:
            mode = "REFETCH+SUMMARY" if (bad_data or args.refetch) else "SUMMARY ONLY"
            print(f"    {ticker:8} — {reason}  [{mode}]")
    print()

    if args.dry_run:
        print("  [dry-run] No changes made.")
        print()
        return

    # ── Build index map for in-place patching ─────────────────────────────────
    index_map = {item["ticker"].upper(): i for i, item in enumerate(research_data)}

    repaired = 0
    failed   = 0

    for ticker, reason, bad_data in to_repair:
        print(f"  ────────────────────────────────────────")
        do_refetch = bad_data or args.refetch
        mode_label = "REFETCH+SUMMARY" if do_refetch else "SUMMARY ONLY"
        print(f"  {ticker}  [{mode_label}]  (reason: {reason})")

        idx  = index_map.get(ticker)
        item = research_data[idx]

        # ── Step 1: Re-fetch data if needed ───────────────────────────────
        if do_refetch:
            print(f"    [refetch] calling research_ticker...", end=" ", flush=True)
            try:
                res = research_ticker(ticker)
                if res and res.get("data"):
                    research_data[idx]["data"] = res["data"]
                    item = research_data[idx]   # refresh local ref
                    stock_info_len = len(res["data"].get("stock_info", ""))
                    print(f"✓ (stock_info: {stock_info_len:,} chars)")
                else:
                    print(f"✗ research_ticker returned empty result")
                    failed += 1
                    continue
            except Exception as e:
                print(f"✗ Exception: {e}")
                failed += 1
                continue

        # ── Step 2: Regenerate summary ─────────────────────────────────────
        data = item.get("data", {})
        if not data:
            print(f"    ✗ No data dict found — cannot regenerate summary")
            failed += 1
            continue

        print(f"    [summary] asking Jupiter...", end=" ", flush=True)
        try:
            new_summary = generate_summary(
                ticker         = ticker,
                data           = data,
                macro_backdrop = macro_backdrop,
                nova_data      = nova_data,
            )
        except Exception as e:
            print(f"✗ Exception: {e}")
            failed += 1
            continue

        broken, reason2 = is_broken(new_summary, min_chars)
        today = datetime.date.today().isoformat()
        if broken:
            print(f"✗ Still broken ({reason2})")
            failed += 1
        else:
            print(f"✓ ({len(new_summary):,} chars)")
            repaired += 1

        research_data[idx]["summary"]              = new_summary
        research_data[idx]["summary_repaired"]     = not broken
        research_data[idx]["summary_repair_date"]  = today
        if do_refetch:
            research_data[idx]["data_refetched"]       = True
            research_data[idx]["data_refetch_date"]    = today

    # ── Save patched file ─────────────────────────────────────────────────────
    print()
    print(f"  Saving patched research file...")
    with open(research_file, "w", encoding="utf-8") as f:
        json.dump(research_data, f, indent=2, ensure_ascii=False)
    print(f"  ✓ Saved: {research_file}")
    print()
    print("════════════════════════════════════════════════════")
    print(f"  Repair Complete")
    print(f"  ✓ Repaired:  {repaired}")
    print(f"  ✗ Failed:    {failed}")
    print("════════════════════════════════════════════════════")
    print()


if __name__ == "__main__":
    main()
