#!/usr/bin/env python3
"""
Prune Stale Tickers — Obsidian Capital
Removes entries from nova_supplemental.json for tickers that are no longer
in watchlist.json. Run this once, right after editing watchlist.json to
drop or swap a ticker (e.g. HUBG -> ODFL), so Nova's earnings/legal cache
doesn't keep flagging a name the pipeline no longer researches.

research_*.json / rankings_*.json need no equivalent treatment — those are
rebuilt fresh from the current watchlist every run (weekly_research.py /
sector_ranking.py only iterate over tickers currently in watchlist.json),
so a dropped ticker simply stops appearing next week on its own.
nova_supplemental.json is the one persistent, incrementally-updated cache
that never self-prunes, which is why a removed ticker's stale earnings/
legal data otherwise lingers indefinitely and keeps tripping Jansky's
staleness pre-checks.

Usage:
  python3 prune_stale_tickers.py            # dry run — shows what would be removed
  python3 prune_stale_tickers.py --apply    # actually writes the pruned file

Run standalone, after editing watchlist.json:
  python3 ~/stock_dashboard/prune_stale_tickers.py --apply
"""

import json
import os
import sys
import shutil
import datetime

BASE_DIR       = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
WATCHLIST_JSON = f"{BASE_DIR}/watchlist.json"
NOVA_JSON      = f"{BASE_DIR}/nova_supplemental.json"
BACKUP_DIR     = f"{BASE_DIR}/backups"


def load_active_tickers() -> set[str]:
    """Flatten watchlist.json's sectors into one set of active uppercase tickers."""
    if not os.path.exists(WATCHLIST_JSON):
        print(f"  ✗ watchlist.json not found at {WATCHLIST_JSON}")
        sys.exit(1)
    with open(WATCHLIST_JSON) as f:
        data = json.load(f)
    sectors = data.get("sectors", data)  # handle either structure
    active  = set()
    for tickers in sectors.values():
        for t in tickers:
            active.add(t.upper())
    return active


def load_nova_json() -> dict:
    if not os.path.exists(NOVA_JSON):
        print(f"  ✗ nova_supplemental.json not found at {NOVA_JSON}")
        sys.exit(1)
    with open(NOVA_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def save_nova_json(data: dict) -> None:
    """Atomic write, matching app3.py's own _save_nova_json()."""
    tmp = NOVA_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, NOVA_JSON)


def main():
    apply = "--apply" in sys.argv

    print(f"\n{'═'*52}")
    print(f"  Prune Stale Tickers — Obsidian Capital")
    print(f"  {datetime.date.today().isoformat()}")
    print(f"  Mode: {'APPLY (will write changes)' if apply else 'DRY RUN (no changes will be written)'}")
    print(f"{'═'*52}\n")

    active_tickers = load_active_tickers()
    print(f"  ✓ watchlist.json — {len(active_tickers)} active tickers")

    nova_data = load_nova_json()
    print(f"  ✓ nova_supplemental.json — {len(nova_data)} ticker records")

    stale = sorted(t for t in nova_data if t.upper() not in active_tickers)

    if not stale:
        print(f"\n  ✓ Nothing to prune — every ticker in nova_supplemental.json"
              f" is still in the watchlist.\n")
        return

    print(f"\n  {len(stale)} stale ticker(s) found (no longer in watchlist.json):")
    for t in stale:
        rec          = nova_data[t]
        company      = rec.get("company_name", "?")
        has_earnings = "earnings" in rec
        has_legal    = "legal" in rec
        tags = []
        if has_earnings: tags.append("earnings")
        if has_legal:    tags.append("legal")
        print(f"    ✗ {t:<6} {company}  [{', '.join(tags) or 'no data'}]")

    if not apply:
        print(f"\n  Dry run only — re-run with --apply to actually remove"
              f" these {len(stale)} record(s).\n")
        return

    # ── Backup before touching anything (kept, not deleted, per convention) ──
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp       = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    backup_path = f"{BACKUP_DIR}/nova_supplemental_{stamp}_pre_prune.json"
    shutil.copy2(NOVA_JSON, backup_path)
    print(f"\n  ✓ Backup saved: {backup_path}")

    for t in stale:
        del nova_data[t]

    save_nova_json(nova_data)
    print(f"  ✓ Removed {len(stale)} record(s), {len(nova_data)} remain")
    print(f"  ✓ Saved: {NOVA_JSON}")
    print(f"\n{'═'*52}")
    print(f"  Prune complete.")
    print(f"{'═'*52}\n")


if __name__ == "__main__":
    main()
