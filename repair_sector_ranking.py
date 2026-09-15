#!/usr/bin/env python3
"""
repair_sector_ranking.py — Surgical sector ranking repair for Obsidian Capital.

Identifies sectors that are missing or failed in the latest rankings JSON,
re-runs rank_sector() and pitch_trades_for_sector() for only those sectors,
patches results in-place, then re-runs the delta tracker and writes the
sectors+delta dashboard fragment.

Usage:
    # Auto-detect failed/missing sectors and repair
    python3 repair_sector_ranking.py

    # Repair specific sectors
    python3 repair_sector_ranking.py Utilities Semiconductors

    # Re-run delta + fragment only (no ranking re-runs)
    python3 repair_sector_ranking.py --delta-only

    # Dry run — show what would be repaired without making changes
    python3 repair_sector_ranking.py --dry-run

    # Repair a specific rankings file
    python3 repair_sector_ranking.py --file data/rankings_20260621_2011.json
"""

import os
import sys
import json
import glob
import argparse
import datetime
import shutil

BASE_DIR   = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
DATA_DIR   = f"{BASE_DIR}/data"
REPORT_DIR = f"{BASE_DIR}/reports"
sys.path.insert(0, BASE_DIR)


def find_latest_rankings() -> str | None:
    files = sorted(glob.glob(f"{DATA_DIR}/rankings_*.json"))
    return files[-1] if files else None


def find_latest_research() -> str | None:
    files = sorted(glob.glob(f"{DATA_DIR}/research_*.json"))
    return files[-1] if files else None


def is_failed_ranking(ranking: dict) -> tuple[bool, str]:
    """Return (failed, reason) for a ranking dict."""
    if not ranking:
        return True, "empty ranking"
    if "error" in ranking:
        return True, f"error: {ranking['error']}"
    stocks = ranking.get("stocks", [])
    if not stocks:
        return True, "empty stocks list"
    # Check for placeholder ? ticker (M2.7 JSON artifact)
    bad_tickers = [s.get("ticker","?") for s in stocks
                   if not s.get("ticker") or s.get("ticker") == "?"]
    if bad_tickers:
        return True, f"missing/invalid ticker in stocks list"
    # Check for empty strengths/risks
    empty = [s.get("ticker","?") for s in stocks
             if not s.get("strengths") or not s.get("risks")]
    if empty:
        return True, f"empty strengths/risks for: {', '.join(empty)}"
    return False, ""


def main():
    parser = argparse.ArgumentParser(
        description="Surgically repair failed sector rankings."
    )
    parser.add_argument(
        "sectors", nargs="*",
        help="Sector names to repair. If omitted, auto-detects failed sectors."
    )
    parser.add_argument(
        "--file", "-f", default=None,
        help="Rankings JSON to repair. Defaults to the latest."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be repaired without making changes."
    )
    parser.add_argument(
        "--delta-only", action="store_true",
        help="Skip ranking re-runs; just re-run the delta tracker and write the fragment."
    )
    parser.add_argument(
        "--no-trades", action="store_true",
        help="Skip trade pitch re-runs (ranking only)."
    )
    args = parser.parse_args()

    stamp    = datetime.datetime.now().strftime('%Y%m%d_%H%M')
    run_date = datetime.datetime.now().strftime('%Y-%m-%d')

    print()
    print("════════════════════════════════════════════════════")
    print("  Sector Ranking Repair Tool — Obsidian Capital")
    print(f"  {run_date}")
    if args.delta_only:
        print("  Mode: DELTA + FRAGMENT ONLY")
    elif args.dry_run:
        print("  Mode: DRY RUN")
    else:
        print("  Mode: SURGICAL REPAIR")
    print("════════════════════════════════════════════════════")
    print()

    # ── Import from sector_ranking ────────────────────────────────────────────
    try:
        from sector_ranking import (
            load_sectors, load_latest_research, load_holdings,
            load_trade_feedback, load_config,
            build_brief, rank_sector, pitch_trades_for_sector,
            load_prev_research, load_prev_rankings,
            compute_deltas, generate_delta_summary, generate_delta_html,
            generate_rankings_html, write_sectors_delta_fragment,
            archive_trade_decisions, build_holdings_context,
        )
    except ImportError as e:
        print(f"✗ Cannot import from sector_ranking.py: {e}")
        sys.exit(1)

    # ── Load config and paths ─────────────────────────────────────────────────
    config   = load_config()
    frag_dir = os.path.join(BASE_DIR, config.get("fragment_dir", "fragments"))
    os.makedirs(frag_dir, exist_ok=True)
    fragment_path = os.path.join(frag_dir, "sectors_delta_fragment.html")
    legacy_path   = os.path.join(BASE_DIR, "sectors_delta_fragment.html")

    # ── Load rankings file ────────────────────────────────────────────────────
    rankings_file = args.file or find_latest_rankings()
    if not rankings_file:
        print("✗ No rankings JSON found in data/ — run sector_ranking.py first.")
        sys.exit(1)

    print(f"  Rankings file: {rankings_file}")
    with open(rankings_file) as f:
        all_rankings: list[dict] = json.load(f)
    print(f"  Loaded {len(all_rankings)} sector ranking(s)")

    # ── Load research data ────────────────────────────────────────────────────
    research_file = find_latest_research()
    if not research_file:
        print("✗ No research JSON found in data/")
        sys.exit(1)
    print(f"  Research file: {research_file}")
    all_results = load_latest_research(research_file)
    results_by_ticker = {r["ticker"].upper(): r for r in all_results}

    # ── Load supporting data ──────────────────────────────────────────────────
    sectors  = load_sectors()
    holdings = load_holdings()
    feedback = load_trade_feedback()

    if holdings:
        total_val = holdings.get("summary", {}).get("total_portfolio_value", 0)
        n_pos     = len(holdings.get("positions", {}))
        print(f"  ✓ Neptune holdings ({n_pos} positions, ${total_val:,.0f} total)")
    print()

    # ── Delta-only mode — skip straight to delta + fragment ───────────────────
    if args.delta_only:
        print("  Skipping ranking re-runs — proceeding to delta tracker.")
        _run_delta_and_fragment(
            all_rankings, all_results, run_date, stamp,
            fragment_path, legacy_path, config
        )
        return

    # ── Build index of existing rankings by sector name ───────────────────────
    rankings_by_sector = {r.get("sector", ""): r for r in all_rankings}

    # ── Identify sectors to repair ────────────────────────────────────────────
    if args.sectors:
        # Case-insensitive match
        to_repair = []
        for name in args.sectors:
            matched = next(
                (k for k in sectors if k.lower() == name.lower()), None
            )
            if not matched:
                print(f"  ⚠ Sector '{name}' not found in watchlist.json — skipping")
                continue
            existing = rankings_by_sector.get(matched, {})
            failed, reason = is_failed_ranking(existing)
            to_repair.append((matched, reason if failed else "manually specified"))
        print(f"  Manual repair list: {len(to_repair)} sector(s)")
    else:
        to_repair = []
        # Check each sector from the watchlist
        for sector_name in sectors:
            existing = rankings_by_sector.get(sector_name, {})
            if not existing:
                to_repair.append((sector_name, "missing from rankings JSON"))
            else:
                failed, reason = is_failed_ranking(existing)
                if failed:
                    to_repair.append((sector_name, reason))
        print(f"  Auto-detected {len(to_repair)} failed/missing sector(s)")

    if not to_repair:
        print()
        print("  ✓ All sectors look healthy — nothing to repair.")
        print()
        print("  Re-running delta tracker and writing fragment...")
        _run_delta_and_fragment(
            all_rankings, all_results, run_date, stamp,
            fragment_path, legacy_path, config
        )
        return

    print()
    print("  Sectors to repair:")
    for sector_name, reason in to_repair:
        tickers = sectors.get(sector_name, [])
        print(f"    {sector_name:<15} — {reason}  ({', '.join(tickers)})")
    print()

    if args.dry_run:
        print("  [dry-run] No changes made.")
        print()
        return

    # ── Repair loop ───────────────────────────────────────────────────────────
    repaired = 0
    failed   = 0

    for sector_name, reason in to_repair:
        print(f"  {'─'*48}")
        print(f"  {sector_name}  (reason: {reason})")

        tickers   = sectors.get(sector_name, [])
        available = [t for t in tickers if t.upper() in results_by_ticker]
        missing   = [t for t in tickers if t.upper() not in results_by_ticker]

        if missing:
            print(f"    ⚠ Missing research data for: {', '.join(missing)}")
        if len(available) < 2:
            print(f"    ✗ Need at least 2 tickers — cannot repair {sector_name}")
            failed += 1
            continue

        briefs = []
        for ticker in available:
            res   = results_by_ticker[ticker.upper()]
            brief = build_brief(res)
            briefs.append((ticker, brief))

        # ── Re-run ranking ────────────────────────────────────────────────
        ranking = rank_sector(sector_name, briefs)
        if not ranking or "stocks" not in ranking:
            print(f"    ✗ Ranking still failed for {sector_name}")
            failed += 1
            continue

        ranking["sector"] = sector_name

        # ── Re-run trade pitch ────────────────────────────────────────────
        new_trades = []
        if not args.no_trades and holdings:
            new_trades = pitch_trades_for_sector(
                sector_name, available, briefs, holdings, config, feedback
            )
            for trade in new_trades:
                trade["sector"]     = sector_name
                trade["pitched_by"] = "jupiter"
                trade["pitched_at"] = datetime.datetime.now().isoformat()
                trade["run_date"]   = run_date

        # ── Patch into rankings list ──────────────────────────────────────
        if sector_name in rankings_by_sector:
            # Replace existing entry
            idx = next(i for i, r in enumerate(all_rankings)
                       if r.get("sector") == sector_name)
            all_rankings[idx] = ranking
        else:
            # Add new entry
            all_rankings.append(ranking)

        rankings_by_sector[sector_name] = ranking

        # ── Patch trade decisions JSON ────────────────────────────────────
        if new_trades:
            trade_decisions_path = os.path.join(BASE_DIR, "trade_decisions.json")
            existing_decisions   = {}
            if os.path.exists(trade_decisions_path):
                try:
                    with open(trade_decisions_path) as f:
                        existing_decisions = json.load(f)
                except Exception:
                    existing_decisions = {}

            # Remove old trades for this sector and add new ones
            jupiter_section = existing_decisions.get("jupiter", {})
            old_trades = jupiter_section.get("trades", [])
            kept_trades = [t for t in old_trades
                           if t.get("sector") != sector_name]
            kept_trades.extend(new_trades)

            jupiter_section["trades"]      = kept_trades
            jupiter_section["trade_count"] = len(kept_trades)
            existing_decisions["jupiter"]  = jupiter_section
            existing_decisions["_meta"]    = {
                "last_updated": datetime.datetime.now().isoformat(),
                "note": "Patched by repair_sector_ranking.py"
            }

            with open(trade_decisions_path, "w") as f:
                json.dump(existing_decisions, f, indent=2)
            print(f"    ✓ Trade decisions patched ({len(new_trades)} new trades for {sector_name})")

        for s in ranking["stocks"]:
            v     = s.get("verdict", "?")
            t     = s.get("ticker", "?")
            score = s.get("score", "?")
            print(f"    {'🟢' if v=='ACCUMULATE' else '🟡' if v=='WATCH' else '🔴'} "
                  f"{t:6s} {v:10s} score={score}/10")

        repaired += 1

    # ── Save patched rankings JSON ─────────────────────────────────────────────
    print()
    print(f"  Saving patched rankings file...")
    with open(rankings_file, "w") as f:
        json.dump(all_rankings, f, indent=2)
    print(f"  ✓ Saved: {rankings_file}")

    # Archive patched copy
    archive_path = f"{REPORT_DIR}/rankings_{stamp}.json"
    shutil.copy2(rankings_file, archive_path)
    print(f"  ✓ Archived: {archive_path}")

    print()
    print("════════════════════════════════════════════════════")
    print(f"  Repair Complete")
    print(f"  ✓ Repaired:  {repaired}")
    print(f"  ✗ Failed:    {failed}")
    print("════════════════════════════════════════════════════")
    print()

    # ── Re-run delta tracker and write fragment ────────────────────────────────
    _run_delta_and_fragment(
        all_rankings, all_results, run_date, stamp,
        fragment_path, legacy_path, config
    )


def _run_delta_and_fragment(all_rankings, all_results, run_date, stamp,
                             fragment_path, legacy_path, config):
    """Re-run the delta tracker and write the sectors+delta fragment."""
    from sector_ranking import (
        load_prev_research, load_prev_rankings,
        compute_deltas, generate_delta_summary, generate_delta_html,
        generate_rankings_html, write_sectors_delta_fragment,
    )
    import glob

    print(f"  {'─'*48}")
    print(f"  Delta Tracker")
    print(f"  {'─'*48}")

    prev_results  = load_prev_research()
    prev_rankings = load_prev_rankings()

    if not prev_results:
        print("  ⚠ No previous research data — delta shows 'first run'")
        delta_html = generate_delta_html([], "", run_date, "first run")
    else:
        prev_files  = sorted(glob.glob(f"{DATA_DIR}/research_*.json"))
        prev_date   = prev_files[-2].split("research_")[1][:8] if len(prev_files) >= 2 else "prev"
        prev_date_f = f"{prev_date[:4]}-{prev_date[4:6]}-{prev_date[6:8]}"

        print(f"  Comparing: {prev_date_f} → {run_date}")
        deltas = compute_deltas(all_results, prev_results, all_rankings, prev_rankings)
        print(f"  {len(deltas)} tickers with significant changes")

        delta_narrative = generate_delta_summary(deltas, run_date, prev_date_f)
        delta_html      = generate_delta_html(deltas, delta_narrative, run_date, prev_date_f)

    rankings_html = generate_rankings_html(all_rankings, run_date)
    write_sectors_delta_fragment(rankings_html, delta_html, fragment_path)
    shutil.copy2(fragment_path, legacy_path)

    archive_frag = f"{REPORT_DIR}/sectors_delta_fragment_{stamp}.html"
    shutil.copy2(fragment_path, archive_frag)
    print(f"  ✓ Fragment written: {fragment_path}")
    print(f"  ✓ Fragment archived: {archive_frag}")
    print()
    print("  Run rebuild_dashboard.sh to refresh the HTML dashboard.")
    print()


if __name__ == "__main__":
    main()
