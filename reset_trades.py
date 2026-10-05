#!/usr/bin/env python3
"""
reset_trades.py — Obsidian Capital

One-time (or occasional) clean slate for the trade loop:

  1. Restores neptune_holdings.json from a known-good original you point it
     at (--holdings-source). Without that flag, holdings are left alone.
  2. Empties jansky_trade_feedback.json (approved-but-never-executed and
     duplicate-run entries are what made Jupiter/Mercury treat approvals as
     held positions).
  3. Optionally (--clear-pitches) blanks trade_decisions.json so the next
     weekly_mercury.py / sector_ranking.py run starts with no stale pitches.

Everything it replaces is first copied to backups/reset_<timestamp>/.
Dry run by default — nothing is written until you pass --apply.

Usage:
    python3 reset_trades.py --holdings-source ~/neptune_holdings_original.json
    python3 reset_trades.py --holdings-source ~/neptune_holdings_original.json --clear-pitches --apply
"""

import os
import sys
import json
import shutil
import argparse
import datetime

BASE_DIR         = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
NEPTUNE_HOLDINGS = f"{BASE_DIR}/neptune_holdings.json"
TRADE_FEEDBACK   = f"{BASE_DIR}/jansky_trade_feedback.json"
TRADE_DECISIONS  = f"{BASE_DIR}/trade_decisions.json"
BACKUP_ROOT      = f"{BASE_DIR}/backups"

EMPTY_PITCHES = {
    "jupiter": {"run_date": None, "trade_count": 0, "trades": []},
    "mercury": {"run_date": None, "trade_count": 0, "trades": []},
}


def _atomic_write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _load(path: str):
    with open(path) as f:
        return json.load(f)


def describe_holdings(label: str, h: dict) -> None:
    pos = h.get("positions", {})
    s = h.get("summary", {})
    print(f"  {label}: {len(pos)} positions | cash ${s.get('cash_value', 0):,.0f} "
          f"({s.get('cash_pct', 0)}%) | total ${s.get('total_portfolio_value', 0):,.0f} "
          f"| trade_log entries: {len(h.get('trade_log', []))}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset the trade loop to a clean slate")
    ap.add_argument("--holdings-source", help="Path to the original neptune_holdings.json to restore")
    ap.add_argument("--clear-pitches", action="store_true",
                    help="Also blank trade_decisions.json (archived first)")
    ap.add_argument("--apply", action="store_true", help="Actually write changes (default: dry run)")
    args = ap.parse_args()

    print("═" * 60)
    print("  Trade Reset — Obsidian Capital")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}")
    print("═" * 60)

    # ── Validate the replacement holdings before touching anything ──────
    new_holdings = None
    if args.holdings_source:
        src = os.path.expanduser(args.holdings_source)
        if not os.path.exists(src):
            print(f"✗ --holdings-source not found: {src}")
            return 1
        try:
            new_holdings = _load(src)
        except Exception as e:
            print(f"✗ Could not parse {src}: {e}")
            return 1
        if not isinstance(new_holdings.get("positions"), dict) or not new_holdings["positions"]:
            print("✗ Source has no 'positions' dict — refusing to restore it.")
            return 1
        if "cash_value" not in new_holdings.get("summary", {}):
            print("✗ Source has no summary.cash_value — refusing to restore it.")
            return 1
        print("\n  Holdings to restore:")
        describe_holdings("source ", new_holdings)
        if new_holdings.get("trade_log"):
            print("  ⚠ Source already has trade_log entries — is this really the "
                  "pre-settlement original?")
    else:
        print("\n  (no --holdings-source given: neptune_holdings.json will be left as-is)")

    if os.path.exists(NEPTUNE_HOLDINGS):
        try:
            describe_holdings("current", _load(NEPTUNE_HOLDINGS))
        except Exception as e:
            print(f"  ⚠ current holdings unreadable: {e}")

    fb_n = 0
    if os.path.exists(TRADE_FEEDBACK):
        try:
            fb_n = len(_load(TRADE_FEEDBACK))
        except Exception:
            pass
    print(f"\n  jansky_trade_feedback.json: {fb_n} entries → will be emptied")
    if args.clear_pitches:
        print("  trade_decisions.json: will be blanked (Jupiter + Mercury sections)")
    else:
        print("  trade_decisions.json: left as-is (add --clear-pitches to blank it)")

    if not args.apply:
        print("\n  Dry run complete. Re-run with --apply to make these changes.")
        return 0

    # ── Back up everything we're about to replace ───────────────────────
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = f"{BACKUP_ROOT}/reset_{stamp}"
    os.makedirs(backup_dir, exist_ok=True)
    to_backup = [TRADE_FEEDBACK]
    if new_holdings is not None:
        to_backup.append(NEPTUNE_HOLDINGS)
    if args.clear_pitches:
        to_backup.append(TRADE_DECISIONS)
    for p in to_backup:
        if os.path.exists(p):
            shutil.copy2(p, backup_dir)
    print(f"\n  ✓ Backed up to {backup_dir}")

    # ── Apply ───────────────────────────────────────────────────────────
    if new_holdings is not None:
        _atomic_write_json(NEPTUNE_HOLDINGS, new_holdings)
        print(f"  ✓ Restored {NEPTUNE_HOLDINGS}")
    _atomic_write_json(TRADE_FEEDBACK, {})
    print(f"  ✓ Emptied {TRADE_FEEDBACK}")
    if args.clear_pitches:
        _atomic_write_json(TRADE_DECISIONS, EMPTY_PITCHES)
        print(f"  ✓ Blanked {TRADE_DECISIONS}")

    print("\n  Next: regenerate pitches (weekly_mercury.py, then sector_ranking.py), "
          "run jansky_review.py, then settle_portfolio.py --dry-run.")
    print("═" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
