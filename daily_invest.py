"""
Daily cash-deploy workflow for Halal-BDS-SP500.

Steps:
  1. Check available cash — exit if < $20
  2. Fetch current Alpaca positions
  3. Load target weights for ACTIVE constituents — preferring the git-committed
     index/constituents.csv, falling back to the cached DB
  4. Compute portfolio gaps (target_weight - actual_weight)
  5. Rank by gap descending, take top 100
  6. Allocate cash proportionally, enforce $1 minimum per order
  7. Place fractional notional market orders
  8. Save orders to transactions table
  9. Append summary to gitignored reports/daily_buys.md
"""

import csv
import os
import sqlite3
from datetime import date

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from init_db import init_db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = "index_fund.db"
INDEX_CSV = "index/constituents.csv"
DAILY_BUYS_MD = "reports/daily_buys.md"

ALPACA_KEY = os.environ["ALPACA_INDEX_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_INDEX_API_SECRET"]
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

MIN_CASH = 20.0
MIN_NOTIONAL = 1.0
TOP_N_GAPS = 100
TODAY = date.today().isoformat()


# ---------------------------------------------------------------------------
# Target weights
# ---------------------------------------------------------------------------

def load_target_weights(conn: sqlite3.Connection) -> dict[str, float]:
    """Return {symbol: target_weight_fraction} for ACTIVE constituents.

    The git-committed index/constituents.csv is the source of truth and is
    preferred: monthly_scan commits it, and the daily workflow checks it out,
    so it always reflects the latest scan. The cached index_fund.db, by
    contrast, can lag many runs behind because the two workflows keep separate
    GitHub Actions cache key counters. We fall back to the DB only when the CSV
    is missing or has no ACTIVE rows.

    Note: the CSV stores TargetWeightPct as a percentage (e.g. 11.2894), while
    the DB stores target_weight as a fraction; we normalise both to fractions.
    """
    if os.path.exists(INDEX_CSV):
        weights: dict[str, float] = {}
        with open(INDEX_CSV, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("IndexStatus") != "ACTIVE":
                    continue
                try:
                    weights[row["Symbol"]] = float(row["TargetWeightPct"]) / 100.0
                except (KeyError, ValueError):
                    continue
        if weights:
            print(f"Loaded {len(weights)} ACTIVE constituents from {INDEX_CSV}")
            return weights
        print(f"{INDEX_CSV} present but no ACTIVE rows; falling back to DB.")

    rows = conn.execute(
        "SELECT symbol, target_weight FROM constituents WHERE index_status = 'ACTIVE'"
    ).fetchall()
    print(f"Loaded {len(rows)} ACTIVE constituents from DB ({DB_PATH})")
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run() -> None:
    os.makedirs("reports", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    alpaca = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)

    # Step 1: Check cash
    account = alpaca.get_account()
    cash = float(account.cash)
    print(f"Available cash: ${cash:.2f}")
    if cash < MIN_CASH:
        print(f"Cash ${cash:.2f} < ${MIN_CASH} minimum. Skipping.")
        conn.close()
        return

    # Step 2: Fetch positions
    positions = alpaca.get_all_positions()
    pos_map: dict[str, float] = {p.symbol: float(p.market_value) for p in positions}

    # Step 3: Load target weights (prefer committed CSV over cached DB)
    target_weights = load_target_weights(conn)
    if not target_weights:
        print("No ACTIVE constituents in CSV or DB. Run monthly_scan.py first.")
        conn.close()
        return

    # Step 4: Compute gaps
    total_portfolio = sum(pos_map.values()) + cash
    gaps: dict[str, float] = {}
    for sym, target in target_weights.items():
        actual = pos_map.get(sym, 0.0) / total_portfolio
        gap = target - actual
        if gap > 0:
            gaps[sym] = gap

    if not gaps:
        print("Portfolio is fully balanced — no gaps to fill.")
        conn.close()
        return

    # Step 5: Rank top N gaps
    ranked = sorted(gaps.items(), key=lambda x: x[1], reverse=True)[:TOP_N_GAPS]
    print(f"Top {len(ranked)} gaps identified (total gap={sum(g for _, g in ranked):.6f})")

    # Step 6: Allocate cash proportionally
    total_gap = sum(g for _, g in ranked)
    allocations: list[tuple[str, float]] = []
    remaining_cash = cash

    for sym, gap in ranked:
        raw_alloc = (gap / total_gap) * cash
        alloc = max(round(raw_alloc, 2), MIN_NOTIONAL)
        if alloc > remaining_cash:
            alloc = round(remaining_cash, 2)
        if alloc < MIN_NOTIONAL:
            continue
        allocations.append((sym, alloc))
        remaining_cash -= alloc
        if remaining_cash < MIN_NOTIONAL:
            break

    print(f"Placing {len(allocations)} orders, total=${sum(a for _, a in allocations):.2f}")

    # Steps 7 & 8: Place orders and save
    order_summaries: list[str] = []

    for sym, notional in allocations:
        try:
            order = alpaca.submit_order(
                MarketOrderRequest(
                    symbol=sym,
                    notional=notional,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            status = str(order.status)
            order_id = str(order.id)
            print(f"  BUY {sym} ${notional:.2f} → {status} ({order_id})")
            order_summaries.append(f"{sym} ${notional:.2f}")
        except Exception as e:
            status = "FAILED"
            order_id = None
            print(f"  ERROR buying {sym}: {e}")
            order_summaries.append(f"{sym} FAILED")

        conn.execute(
            """INSERT INTO transactions
               (transaction_date, symbol, action, notional_amount, alpaca_order_id, status, reason)
               VALUES (?, ?, 'BUY', ?, ?, ?, 'rebalance')""",
            (TODAY, sym, notional, order_id, status),
        )

    conn.commit()

    # Step 9: Append summary to private daily_buys.md
    summary_line = (
        f"| {TODAY} | {len(allocations)} orders | "
        f"${sum(a for _, a in allocations):.2f} deployed | "
        f"{', '.join(order_summaries[:5])}{'...' if len(order_summaries) > 5 else ''} |\n"
    )

    write_header = not os.path.exists(DAILY_BUYS_MD)
    with open(DAILY_BUYS_MD, "a") as f:
        if write_header:
            f.write("# Daily Buy Log (private — not committed)\n\n")
            f.write("| Date | Orders | Cash Deployed | Top Buys |\n")
            f.write("|------|--------|---------------|----------|\n")
        f.write(summary_line)

    conn.close()
    print(f"Daily invest complete. ${sum(a for _, a in allocations):.2f} deployed across {len(allocations)} symbols.")


if __name__ == "__main__":
    run()
