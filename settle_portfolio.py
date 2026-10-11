#!/usr/bin/env python3
"""
settle_portfolio.py — Obsidian Capital

Two jobs, run in order because the second depends on the first:

1. PRICE REFRESH — every existing position in neptune_holdings.json gets
   its last_price/market_value/gain_loss/price_date updated against a
   current market price (latest daily close via yfinance — matching the
   same "latest close, not .info fields" convention already established
   elsewhere in this pipeline, not the stale html_import snapshot every
   position has been frozen at since inception).

2. TRADE SETTLEMENT — any APPROVE decision in jansky_trade_feedback.json
   gets applied to the portfolio: shares/cost-basis/cash updated, and an
   entry appended to trade_log. Nothing here was previously wired up —
   trade_log was empty and every position's price_date was identical
   (2026-05-21, "html_import") right up until this script existed,
   meaning no trade had ever actually been settled and no price had
   ever been refreshed since the original bulk import.

Idempotency: trade_log is checked before applying any decision, so
re-running this script (or running it against a jansky_trade_feedback.json
that still contains an already-settled trade inside its 2-week rolling
window) does not double-apply anything.

Usage:
    python3 settle_portfolio.py              # refresh + settle, write changes
    python3 settle_portfolio.py --dry-run    # preview only, no write
    python3 settle_portfolio.py --skip-refresh   # settlement only
    python3 settle_portfolio.py --skip-settlement  # price refresh only
"""

import os
import sys
import json
import datetime
import argparse

BASE_DIR         = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
NEPTUNE_HOLDINGS = f"{BASE_DIR}/neptune_holdings.json"
TRADE_FEEDBACK   = f"{BASE_DIR}/jansky_trade_feedback.json"


def _load_config() -> dict:
    """Load obsidian_config.json so the cash floor here always matches
    whatever jansky_review.py is using — same config key, same default.
    Oct 2026: settle_trades() previously had no cash-floor awareness at
    all; the floor was only ever a WARNING + prompt instruction shown to
    Jansky (the LLM) during review, which Jansky can approve straight
    through. This is the hard stop at the only point that actually
    writes to the portfolio."""
    config_path = os.path.join(BASE_DIR, "obsidian_config.json")
    defaults = {"cash_floor": {"min_cash_pct": 10.0}}
    if not os.path.exists(config_path):
        return defaults
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception:
        return defaults


_config = _load_config()
CASH_FLOOR_PCT = _config.get("cash_floor", {}).get("min_cash_pct", 10.0)

# A REDUCE that would leave less than this in the position closes it out instead.
# A $500K "exit" of an ADSK position bought at $209.40 and sold at $212.00 used to
# leave 29 shares ($6K) behind forever; a stub that small is never a deliberate hold.
DUST_EXIT_DOLLARS = 25_000

# Known ETF tickers this pipeline tracks — used to classify a brand-new
# NEW_POSITION as "etf" vs "equity" (matches mercurymcp's DEFAULT_ETFS list).
KNOWN_ETFS = {
    "IAU", "BITQ", "VDE", "FBTC", "IBIT",
    "GLD", "SLV", "PDBC", "DBA", "USO", "UNG", "CPER",
    "VNQ", "IYR", "XLRE",
    "TLT", "HYG",
}


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ Could not load {path}: {e}")
        return default


def fetch_latest_close(ticker: str):
    """Latest daily close via yfinance — same convention used elsewhere
    in this pipeline (get_etf_data, get_agricultural_prices, etc.):
    history(), not .info fields, which have shown basis-mismatch bugs."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="5d")
        if hist is None or hist.empty:
            return None
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception as e:
        print(f"    ⚠ {ticker}: price fetch failed — {e}")
        return None


def refresh_prices(holdings: dict, dry_run: bool) -> dict:
    print("\n── Price Refresh ──────────────────────────────────────")
    positions = holdings.get("positions", {})
    today = datetime.date.today().isoformat()
    refreshed, failed = 0, []

    for ticker, pos in positions.items():
        price = fetch_latest_close(ticker)
        if price is None:
            failed.append(ticker)
            continue

        old_price = pos.get("last_price")
        shares    = pos.get("shares", 0)
        cost_basis = pos.get("cost_basis", 0)

        new_market_value = round(shares * price, 2)
        new_gain_loss     = round(new_market_value - cost_basis, 2)
        new_gain_loss_pct = round((new_gain_loss / cost_basis) * 100, 2) if cost_basis else 0.0

        print(f"  {ticker:6s}  ${old_price:>10.2f} → ${price:>10.2f}"
              f"   MV: ${new_market_value:,.2f}")

        if not dry_run:
            pos["last_price"]     = round(price, 2)
            pos["market_value"]   = new_market_value
            pos["gain_loss"]      = new_gain_loss
            pos["gain_loss_pct"]  = new_gain_loss_pct
            pos["price_date"]     = today
            pos["price_source"]   = "yfinance_refresh"

        refreshed += 1

    print(f"\n  Refreshed: {refreshed}   Failed: {len(failed)}"
          + (f"  ({', '.join(failed)})" if failed else ""))
    return holdings


def already_settled(trade_log: list, ticker: str, date: str, action: str,
                    dollars, pitch_id=None) -> bool:
    """Idempotency guard — has this exact decision already been logged?

    Oct 2026: this used to match on the Jansky REVIEW date. Re-running
    jansky_review.py on a later day re-approves the same pitch under a new
    review date, so the same trade was settled again (duplicate fills).
    Now matches on the PITCH's own identity (pitched_at / run_date, carried
    through jansky_trade_feedback.json) when both sides have one; the
    review-date match is kept only as a fallback for log entries written
    before pitch_id existed.
    """
    for entry in trade_log:
        if (entry.get("ticker") != ticker
                or entry.get("action") != action
                or entry.get("dollars") != dollars):
            continue
        logged_pitch = entry.get("pitch_id")
        if pitch_id and logged_pitch:
            if logged_pitch == pitch_id:
                return True
            continue  # same ticker/action/size but a different, newer pitch
        if entry.get("decision_date") == date:
            return True
    return False


def _mark(decision: dict, status: str, note: str = "") -> None:
    """Record what settlement did with this approval, on the feedback entry
    itself. Jupiter/Mercury read jansky_trade_feedback.json, and before this
    they could not tell an executed approval from one that never ran — so
    they treated every approved pitch as a held position."""
    decision["settlement_status"] = status          # settled | cash_floor_rejected | not_executed
    decision["settlement_note"]   = note
    decision["settlement_checked"] = datetime.date.today().isoformat()


def settle_trades(holdings: dict, feedback: dict, dry_run: bool,
                   ignore_cash_floor: bool = False) -> dict:
    print("\n── Trade Settlement ───────────────────────────────────")
    positions  = holdings.setdefault("positions", {})
    trade_log  = holdings.setdefault("trade_log", [])
    summary    = holdings.setdefault("summary", {})
    cash       = summary.get("cash_value", 0.0)
    today      = datetime.date.today().isoformat()

    # Oct 2026: total portfolio value is (to a very close approximation)
    # invariant across these trades — a buy or sell just moves `dollars`
    # between cash and market_value at the fetched execution price, it
    # doesn't change the total. Snapshotting it once here, before any
    # trade is applied, gives a stable denominator for the cash-floor
    # check below rather than recomputing (and drifting) mid-loop.
    total_value = cash + sum(p.get("market_value", 0) for p in positions.values())

    approved = {t: d for t, d in feedback.items()
                if d.get("decision") == "APPROVE" and not d.get("superseded")}
    _sup = [t for t, d in feedback.items()
            if d.get("decision") == "APPROVE" and d.get("superseded")]
    if _sup:
        print(f"  (ignoring {len(_sup)} approval(s) from an earlier review that a "
              f"newer Jansky run replaced: {', '.join(_sup)})")
    if not approved:
        print("  No approved trades in jansky_trade_feedback.json.")
        return holdings

    # Oct 2026: process sells before buys regardless of file order, so
    # sell proceeds count toward cash *before* the floor is checked for
    # any buy — this was the same-pass netting gap in jansky_review.py's
    # own (advisory-only) floor warning. Order within each group is
    # preserved from jansky_trade_feedback.json.
    sells = [(t, d) for t, d in approved.items() if d.get("action") == "REDUCE_POSITION"]
    buys  = [(t, d) for t, d in approved.items() if d.get("action") != "REDUCE_POSITION"]

    # Oct 2026: buys are settled in Jansky's own conviction order, not raw
    # file order. jansky_review.py now asks for a "priority" int per
    # approved buy (1 = fund first); without this, whichever ticker
    # happened to sit first in jansky_trade_feedback.json got funded and
    # the cash floor cut off whatever came after — not necessarily the
    # weakest idea. Missing/invalid priority (older feedback entries
    # written before this field existed, or a parse miss) sorts last, so
    # an unranked buy is the first one the floor cuts, never silently
    # treated as top conviction.
    buys.sort(key=lambda item: item[1].get("priority", 999)
              if isinstance(item[1].get("priority"), int) else 999)

    ordered = sells + buys

    applied, skipped = 0, 0
    cash_floor_rejected = []

    for ticker, decision in ordered:
        action  = decision.get("action")
        dollars = decision.get("dollars")
        d_date  = decision.get("date", today)
        pitch_id = decision.get("pitched_at") or decision.get("run_date")

        if not action or not dollars:
            print(f"  ⚠ {ticker}: missing action/dollars, skipping")
            _mark(decision, "not_executed", "missing action/dollars")
            continue

        if already_settled(trade_log, ticker, d_date, action, dollars, pitch_id):
            print(f"  – {ticker}: already settled ({action}, ${dollars:,.0f} on {d_date}) — skipping")
            _mark(decision, "settled", "already settled in an earlier run")
            skipped += 1
            continue

        # Oct 2026 hard stop: a buy that would drop cash below the floor
        # is rejected here, at the only point that actually writes to
        # the portfolio — not left to Jansky's prompt-level warning,
        # which can be (and has been) approved straight through.
        is_buy = action in ("NEW_POSITION", "ADD_TO_POSITION")
        if is_buy and not ignore_cash_floor:
            projected_cash = cash - dollars
            projected_pct  = (projected_cash / total_value * 100) if total_value else 0.0
            if projected_pct < CASH_FLOOR_PCT:
                print(f"  ✗ {ticker}: REJECTED — {action} ${dollars:,.0f} would drop cash to "
                      f"{projected_pct:.1f}% (floor: {CASH_FLOOR_PCT:.0f}%)")
                cash_floor_rejected.append(
                    f"{ticker} ({action}, ${dollars:,.0f} → projected {projected_pct:.1f}%)"
                )
                _mark(decision, "cash_floor_rejected",
                      f"would drop cash to {projected_pct:.1f}%")
                continue

        price = fetch_latest_close(ticker)
        if price is None:
            print(f"  ⚠ {ticker}: could not fetch price, skipping settlement this run")
            _mark(decision, "not_executed", "price fetch failed")
            continue

        pos = positions.get(ticker)

        if action == "NEW_POSITION":
            if pos is not None:
                print(f"  ⚠ {ticker}: NEW_POSITION but already held — skipping (use ADD_TO_POSITION next run)")
                _mark(decision, "not_executed", "NEW_POSITION but already held")
                continue
            shares = dollars / price
            positions[ticker] = {
                "company":           ticker,
                "ticker":            ticker,
                "shares":            round(shares, 6),
                "avg_cost_per_share": round(price, 2),
                "cost_basis":        round(dollars, 2),
                "last_price":        round(price, 2),
                "market_value":      round(dollars, 2),
                "gain_loss":         0.0,
                "gain_loss_pct":     0.0,
                "asset_type":        "etf" if ticker in KNOWN_ETFS else "equity",
                "price_date":        today,
                "price_source":      "trade_settlement",
            }
            cash -= dollars
            print(f"  ✓ {ticker}: NEW_POSITION  {shares:,.2f} sh @ ${price:.2f}  (${dollars:,.0f})")

        elif action == "ADD_TO_POSITION":
            if pos is None:
                print(f"  ⚠ {ticker}: ADD_TO_POSITION but not currently held — skipping")
                _mark(decision, "not_executed", "ADD_TO_POSITION but not held")
                continue
            add_shares    = dollars / price
            new_shares    = pos["shares"] + add_shares
            new_cost_basis = pos["cost_basis"] + dollars
            pos["shares"]             = round(new_shares, 6)
            pos["cost_basis"]         = round(new_cost_basis, 2)
            pos["avg_cost_per_share"] = round(new_cost_basis / new_shares, 2)
            pos["last_price"]         = round(price, 2)
            pos["market_value"]       = round(new_shares * price, 2)
            pos["gain_loss"]          = round(pos["market_value"] - new_cost_basis, 2)
            pos["gain_loss_pct"]      = round((pos["gain_loss"] / new_cost_basis) * 100, 2)
            pos["price_date"]         = today
            pos["price_source"]       = "trade_settlement"
            cash -= dollars
            print(f"  ✓ {ticker}: ADD_TO_POSITION  +{add_shares:,.2f} sh @ ${price:.2f}  (${dollars:,.0f})")

        elif action == "REDUCE_POSITION":
            executed_dollars = dollars
            if pos is None:
                print(f"  ⚠ {ticker}: REDUCE_POSITION but not currently held — skipping")
                _mark(decision, "not_executed", "REDUCE_POSITION but not held")
                continue
            sell_shares = dollars / price
            _remaining_value = (pos["shares"] - sell_shares) * price
            if sell_shares >= pos["shares"] or _remaining_value < DUST_EXIT_DOLLARS:
                # Full exit (including a would-be sliver below the dust threshold)
                proceeds = pos["shares"] * price
                cash += proceeds
                executed_dollars = proceeds
                _dust = (f"  [remainder ${max(_remaining_value, 0):,.0f} < "
                         f"${DUST_EXIT_DOLLARS:,.0f} dust threshold, closed out]"
                         if sell_shares < pos["shares"] else "")
                print(f"  ✓ {ticker}: REDUCE_POSITION → FULL EXIT  "
                      f"{pos['shares']:,.2f} sh @ ${price:.2f}  (${proceeds:,.0f}){_dust}")
                del positions[ticker]
            else:
                frac_sold = sell_shares / pos["shares"]
                cost_basis_removed = pos["cost_basis"] * frac_sold
                new_shares = pos["shares"] - sell_shares
                new_cost_basis = pos["cost_basis"] - cost_basis_removed
                pos["shares"]        = round(new_shares, 6)
                pos["cost_basis"]    = round(new_cost_basis, 2)
                # avg_cost_per_share intentionally unchanged — proportional
                # cost-basis reduction keeps the remaining shares' average
                # cost the same, standard treatment absent per-lot tracking
                pos["last_price"]    = round(price, 2)
                pos["market_value"]  = round(new_shares * price, 2)
                pos["gain_loss"]     = round(pos["market_value"] - new_cost_basis, 2)
                pos["gain_loss_pct"] = round((pos["gain_loss"] / new_cost_basis) * 100, 2) if new_cost_basis else 0.0
                pos["price_date"]    = today
                pos["price_source"]  = "trade_settlement"
                cash += dollars
                print(f"  ✓ {ticker}: REDUCE_POSITION  -{sell_shares:,.2f} sh @ ${price:.2f}  (${dollars:,.0f})")
        else:
            print(f"  ⚠ {ticker}: unrecognized action '{action}', skipping")
            _mark(decision, "not_executed", f"unrecognized action {action}")
            continue

        _mark(decision, "settled", f"executed {today}")
        _entry = {
            "settled_date":  today,
            "decision_date": d_date,
            "pitch_id":      pitch_id,
            "ticker":        ticker,
            "action":        action,
            "dollars":       dollars,
            "price":         round(price, 2),
            "rationale":     decision.get("rationale", "")[:300],
            "pitched_by":    decision.get("pitched_by"),
        }
        # A full exit credits the position's real value, which can differ from the
        # pitched dollars; record it so the log reconciles with cash.
        if action == "REDUCE_POSITION" and abs(executed_dollars - dollars) > 1:
            _entry["executed_dollars"] = round(executed_dollars, 2)
        trade_log.append(_entry)
        applied += 1

    summary["cash_value"] = round(cash, 2)
    print(f"\n  Applied: {applied}   Already settled (skipped): {skipped}"
          f"   Cash-floor rejected: {len(cash_floor_rejected)}")

    if cash_floor_rejected:
        print(f"\n  ⚠ Rejected for breaching the {CASH_FLOOR_PCT:.0f}% cash floor "
              f"(still APPROVE'd in jansky_trade_feedback.json — will be retried "
              f"next run unless overwritten by a new weekly_research.py pass):")
        for item in cash_floor_rejected:
            print(f"    - {item}")

    if dry_run:
        print("\n  (dry run — no changes written)")

    return holdings


def recompute_summary(holdings: dict) -> dict:
    positions = holdings.get("positions", {})
    summary   = holdings.setdefault("summary", {})

    equity_value = sum(p["market_value"] for p in positions.values() if p.get("asset_type") == "equity")
    etf_value    = sum(p["market_value"] for p in positions.values() if p.get("asset_type") == "etf")
    total_cost   = sum(p.get("cost_basis", 0) for p in positions.values())
    invested     = equity_value + etf_value
    cash         = summary.get("cash_value", 0.0)
    total_value  = invested + cash
    total_gain   = sum(p.get("gain_loss", 0) for p in positions.values())

    summary["total_portfolio_value"] = round(total_value, 2)
    summary["total_invested"]        = round(invested, 2)
    summary["total_equity_value"]    = round(equity_value, 2)
    summary["total_etf_value"]       = round(etf_value, 2)
    summary["cash_pct"]              = round((cash / total_value) * 100, 2) if total_value else 0.0
    summary["total_cost_basis"]      = round(total_cost, 2)
    summary["total_gain_loss"]       = round(total_gain, 2)
    summary["total_gain_loss_pct"]   = round((total_gain / total_cost) * 100, 2) if total_cost else 0.0

    return holdings


def main():
    parser = argparse.ArgumentParser(description="Refresh prices and settle approved trades for neptune_holdings.json")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--skip-refresh", action="store_true", help="Skip price refresh, settlement only")
    parser.add_argument("--skip-settlement", action="store_true", help="Skip trade settlement, price refresh only")
    parser.add_argument("--ignore-cash-floor", action="store_true",
                         help=f"Settle buys even if they breach the {CASH_FLOOR_PCT:.0f}% cash floor "
                              f"(manual override — use deliberately, not as a default)")
    args = parser.parse_args()

    print("═" * 56)
    print("  Portfolio Settlement — Obsidian Capital")
    print(f"  {datetime.date.today().isoformat()}")
    if args.dry_run:
        print("  Mode: DRY RUN (no changes will be written)")
    if args.ignore_cash_floor:
        print(f"  ⚠ Cash floor override active — buys will NOT be blocked "
              f"below {CASH_FLOOR_PCT:.0f}%")
    else:
        print(f"  Cash floor: {CASH_FLOOR_PCT:.0f}% (hard stop on buys)")
    print("═" * 56)

    holdings = load_json(NEPTUNE_HOLDINGS, None)
    if holdings is None:
        print(f"✗ {NEPTUNE_HOLDINGS} not found — nothing to do.")
        sys.exit(1)

    if not args.skip_refresh:
        holdings = refresh_prices(holdings, args.dry_run)

    if not args.skip_settlement:
        feedback = load_json(TRADE_FEEDBACK, {})
        holdings = settle_trades(holdings, feedback, args.dry_run,
                                  ignore_cash_floor=args.ignore_cash_floor)

    holdings = recompute_summary(holdings)
    holdings["last_updated"] = datetime.date.today().isoformat()

    print("\n── Updated Summary ────────────────────────────────────")
    s = holdings["summary"]
    print(f"  Total portfolio value:  ${s['total_portfolio_value']:,.2f}")
    print(f"  Cash:                   ${s['cash_value']:,.2f}  ({s['cash_pct']:.1f}%)")
    print(f"  Total gain/loss:        ${s['total_gain_loss']:,.2f}  ({s['total_gain_loss_pct']:.1f}%)")

    if args.dry_run:
        print("\n(dry run complete — neptune_holdings.json NOT modified)")
    else:
        with open(NEPTUNE_HOLDINGS, "w") as f:
            json.dump(holdings, f, indent=2)
        print(f"\n✓ Saved: {NEPTUNE_HOLDINGS}")
        # Write each approval's settlement status back so downstream
        # prompts can tell executed approvals from unexecuted ones.
        if not args.skip_settlement and feedback:
            tmp = TRADE_FEEDBACK + ".tmp"
            with open(tmp, "w") as f:
                json.dump(feedback, f, indent=2)
            os.replace(tmp, TRADE_FEEDBACK)
            print(f"✓ Settlement status written back: {TRADE_FEEDBACK}")

    print("═" * 56)


if __name__ == "__main__":
    main()
