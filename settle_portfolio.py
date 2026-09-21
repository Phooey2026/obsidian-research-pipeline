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


def already_settled(trade_log: list, ticker: str, date: str, action: str, dollars) -> bool:
    """Idempotency guard — has this exact decision already been logged?"""
    for entry in trade_log:
        if (entry.get("ticker") == ticker
                and entry.get("decision_date") == date
                and entry.get("action") == action
                and entry.get("dollars") == dollars):
            return True
    return False


def settle_trades(holdings: dict, feedback: dict, dry_run: bool) -> dict:
    print("\n── Trade Settlement ───────────────────────────────────")
    positions  = holdings.setdefault("positions", {})
    trade_log  = holdings.setdefault("trade_log", [])
    summary    = holdings.setdefault("summary", {})
    cash       = summary.get("cash_value", 0.0)
    today      = datetime.date.today().isoformat()

    approved = {t: d for t, d in feedback.items() if d.get("decision") == "APPROVE"}
    if not approved:
        print("  No approved trades in jansky_trade_feedback.json.")
        return holdings

    applied, skipped = 0, 0

    for ticker, decision in approved.items():
        action  = decision.get("action")
        dollars = decision.get("dollars")
        d_date  = decision.get("date", today)

        if not action or not dollars:
            print(f"  ⚠ {ticker}: missing action/dollars, skipping")
            continue

        if already_settled(trade_log, ticker, d_date, action, dollars):
            print(f"  – {ticker}: already settled ({action}, ${dollars:,.0f} on {d_date}) — skipping")
            skipped += 1
            continue

        price = fetch_latest_close(ticker)
        if price is None:
            print(f"  ⚠ {ticker}: could not fetch price, skipping settlement this run")
            continue

        pos = positions.get(ticker)

        if action == "NEW_POSITION":
            if pos is not None:
                print(f"  ⚠ {ticker}: NEW_POSITION but already held — skipping (use ADD_TO_POSITION next run)")
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
            if pos is None:
                print(f"  ⚠ {ticker}: REDUCE_POSITION but not currently held — skipping")
                continue
            sell_shares = dollars / price
            if sell_shares >= pos["shares"]:
                # Full exit
                proceeds = pos["shares"] * price
                cash += proceeds
                print(f"  ✓ {ticker}: REDUCE_POSITION → FULL EXIT  "
                      f"{pos['shares']:,.2f} sh @ ${price:.2f}  (${proceeds:,.0f})")
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
            continue

        trade_log.append({
            "settled_date":  today,
            "decision_date": d_date,
            "ticker":        ticker,
            "action":        action,
            "dollars":       dollars,
            "price":         round(price, 2),
            "rationale":     decision.get("rationale", "")[:300],
            "pitched_by":    decision.get("pitched_by"),
        })
        applied += 1

    summary["cash_value"] = round(cash, 2)
    print(f"\n  Applied: {applied}   Already settled (skipped): {skipped}")

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
    args = parser.parse_args()

    print("═" * 56)
    print("  Portfolio Settlement — Obsidian Capital")
    print(f"  {datetime.date.today().isoformat()}")
    if args.dry_run:
        print("  Mode: DRY RUN (no changes will be written)")
    print("═" * 56)

    holdings = load_json(NEPTUNE_HOLDINGS, None)
    if holdings is None:
        print(f"✗ {NEPTUNE_HOLDINGS} not found — nothing to do.")
        sys.exit(1)

    if not args.skip_refresh:
        holdings = refresh_prices(holdings, args.dry_run)

    if not args.skip_settlement:
        feedback = load_json(TRADE_FEEDBACK, {})
        holdings = settle_trades(holdings, feedback, args.dry_run)

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

    print("═" * 56)


if __name__ == "__main__":
    main()
