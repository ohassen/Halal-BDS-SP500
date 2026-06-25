"""
One-time initial buy for Halal-BDS-SP500.

Establishes a position in every ACTIVE constituent, sized proportional to its
target weight, deploying all available cash in a single run. Intended to be run
ONCE (manually via workflow_dispatch) to seed the portfolio; the Daily
Investment job then handles ongoing incremental cash.

Re-running is naturally safe: the first run deploys essentially all cash, so a
second run exits at the cash check below.

Steps:
  1. Check available cash — exit if < $20
  2. Fetch current positions and load ACTIVE target weights (prefer the
     committed index/constituents.csv)
  3. Compute each name's gap below target (from an empty account this equals
     its target weight) and allocate all cash proportional to gap, $1 minimum
     per name so every under-target name gets seeded
  4. Place fractional notional market orders
  5. Save orders to the transactions table
  6. Append a summary to gitignored reports/initial_buy.md
"""

import os
import sqlite3

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from init_db import init_db
from daily_invest import (
    ALPACA_KEY,
    ALPACA_SECRET,
    ALPACA_PAPER,
    DB_PATH,
    MIN_CASH,
    MIN_NOTIONAL,
    TODAY,
    load_target_weights,
)

INITIAL_BUY_MD = "reports/initial_buy.md"


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def compute_allocations(
    gaps: dict[str, float], cash: float
) -> list[tuple[str, float]]:
    """Split `cash` across `gaps` proportional to each name's gap below target.

    Every name is first reserved the $1 minimum so all under-target positions
    are actually established, then the remainder is distributed proportional to
    gap. If there isn't enough cash to give every name $1, the largest gaps are
    funded first until cash runs out.
    """
    syms = sorted(gaps, key=lambda s: gaps[s], reverse=True)
    n = len(syms)
    total_gap = sum(gaps.values())

    # Not enough cash to seed every name at the floor: fund largest gaps first.
    if cash < n * MIN_NOTIONAL:
        allocations: list[tuple[str, float]] = []
        remaining = cash
        for sym in syms:
            alloc = max(round(gaps[sym] / total_gap * cash, 2), MIN_NOTIONAL)
            if alloc > remaining:
                alloc = round(remaining, 2)
            if alloc < MIN_NOTIONAL:
                break
            allocations.append((sym, alloc))
            remaining -= alloc
            if remaining < MIN_NOTIONAL:
                break
        return allocations

    # Reserve $1 per name, distribute the rest proportional to gap.
    remainder = cash - n * MIN_NOTIONAL
    allocations = [
        (sym, round(MIN_NOTIONAL + (gaps[sym] / total_gap) * remainder, 2))
        for sym in syms
    ]

    # Correct cent-rounding drift so the total never exceeds available cash.
    drift = round(sum(a for _, a in allocations) - cash, 2)
    if drift > 0:
        top_sym, top_alloc = allocations[0]
        allocations[0] = (top_sym, round(top_alloc - drift, 2))
    return allocations


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run() -> None:
    os.makedirs("reports", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    alpaca = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
    print(f"Trading mode: {'PAPER' if ALPACA_PAPER else 'LIVE'}")

    # Step 1: Check cash
    account = alpaca.get_account()
    cash = float(account.cash)
    print(f"Available cash: ${cash:.2f}")
    if cash < MIN_CASH:
        print(f"Cash ${cash:.2f} < ${MIN_CASH} minimum. Skipping.")
        conn.close()
        return

    # Step 2: Fetch current positions
    positions = alpaca.get_all_positions()
    pos_map = {p.symbol: float(p.market_value) for p in positions}
    if pos_map:
        print(f"Note: account already holds {len(pos_map)} position(s); funding by gap to target.")

    # Load target weights (prefer committed CSV over cached DB)
    target_weights = load_target_weights(conn)
    if not target_weights:
        print("No ACTIVE constituents in CSV or DB. Run constituent_scan.py first.")
        conn.close()
        return

    # Step 3: Compute each name's gap below target, then allocate all cash
    # proportional to gap. From an empty account every current weight is 0, so
    # gaps equal target weights and this is a straight market-cap-weighted seed;
    # if positions already exist, it tops up the most-underweight names first.
    total_portfolio = sum(pos_map.values()) + cash
    gaps: dict[str, float] = {}
    for sym, target in target_weights.items():
        actual = pos_map.get(sym, 0.0) / total_portfolio
        gap = target - actual
        if gap > 0:
            gaps[sym] = gap

    if not gaps:
        print("All ACTIVE names already at or above target — nothing to buy.")
        conn.close()
        return

    allocations = compute_allocations(gaps, cash)
    total = sum(a for _, a in allocations)
    print(f"Seeding {len(allocations)} positions, total=${total:.2f}")

    # Steps 4 & 5: Place orders and save
    placed = 0
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
            placed += 1
            print(f"  BUY {sym} ${notional:.2f} → {status} ({order_id})")
        except Exception as e:
            status = "FAILED"
            order_id = None
            print(f"  ERROR buying {sym}: {e}")

        conn.execute(
            """INSERT INTO transactions
               (transaction_date, symbol, action, notional_amount, alpaca_order_id, status, reason)
               VALUES (?, ?, 'BUY', ?, ?, ?, 'initial_buy')""",
            (TODAY, sym, notional, order_id, status),
        )

    conn.commit()

    # Step 6: Append summary to private initial_buy.md
    write_header = not os.path.exists(INITIAL_BUY_MD)
    with open(INITIAL_BUY_MD, "a") as f:
        if write_header:
            f.write("# Initial Buy Log (private — not committed)\n\n")
            f.write("| Date | Positions Placed | Cash Deployed |\n")
            f.write("|------|------------------|---------------|\n")
        f.write(f"| {TODAY} | {placed} | ${total:.2f} |\n")

    conn.close()
    print(f"Initial buy complete. ${total:.2f} deployed across {placed} positions.")


if __name__ == "__main__":
    run()
