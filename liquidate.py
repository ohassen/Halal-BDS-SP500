"""
Full portfolio liquidation for Halal-BDS-SP500.

Sells EVERY open position at market (Alpaca close_all_positions) and cancels any
open orders. Intended to be paired with the Initial Buy workflow to fully reset
and re-seed the portfolio across all ACTIVE constituents at target weight:

    1. Run "Liquidate All"  -> account goes to cash
    2. Run "Initial Buy"    -> cash redistributed across the full index by gap

Guarded: refuses to run unless CONFIRM_LIQUIDATE=LIQUIDATE, so it cannot fire by
accident. Run only while the market is open so the market orders fill promptly.

Steps:
  1. Confirm the guard
  2. Snapshot open positions
  3. Cancel open orders and close every position at market
  4. Record SELL rows in the transactions table
  5. Append a summary to gitignored reports/liquidate.md
"""

import os
import sqlite3
import time

from alpaca.trading.client import TradingClient

from init_db import init_db
from daily_invest import (
    ALPACA_KEY,
    ALPACA_SECRET,
    ALPACA_PAPER,
    DB_PATH,
    TODAY,
)

LIQUIDATE_MD = "reports/liquidate.md"

# After submitting the market sells, wait until the account is genuinely flat
# before returning. This matters when pairing with Initial Buy: if the buy reads
# the account while sell proceeds are already in cash but the position rows
# haven't zeroed yet, it sees those names as still-held and skips them. Blocking
# here until positions clear guarantees the next run starts from a clean slate.
SETTLE_TIMEOUT = 180   # seconds to wait for positions to clear
SETTLE_POLL = 5        # seconds between checks


def run() -> None:
    if os.environ.get("CONFIRM_LIQUIDATE") != "LIQUIDATE":
        print(
            "Refusing to liquidate: set CONFIRM_LIQUIDATE=LIQUIDATE to proceed "
            "(the workflow's confirm input must be exactly LIQUIDATE)."
        )
        return

    os.makedirs("reports", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    alpaca = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)

    positions = alpaca.get_all_positions()
    if not positions:
        print("No open positions — nothing to liquidate.")
        conn.close()
        return

    total = sum(float(p.market_value) for p in positions)
    print(f"Liquidating {len(positions)} position(s), market value ${total:.2f}")

    # Cancel any open orders and close every position at market in one call.
    responses = alpaca.close_all_positions(cancel_orders=True)

    # Map symbol -> submitted order id where the SDK returned one.
    order_ids: dict[str, str] = {}
    for r in responses or []:
        sym = getattr(r, "symbol", None)
        body = getattr(r, "body", None)
        oid = getattr(body, "id", None)
        if sym and oid is not None:
            order_ids[sym] = str(oid)

    for p in positions:
        notional = round(float(p.market_value), 2)
        oid = order_ids.get(p.symbol)
        print(f"  SELL {p.symbol} ${notional:.2f} → submitted ({oid or 'n/a'})")
        conn.execute(
            """INSERT INTO transactions
               (transaction_date, symbol, action, notional_amount, alpaca_order_id, status, reason)
               VALUES (?, ?, 'SELL', ?, ?, 'submitted', 'liquidate')""",
            (TODAY, p.symbol, notional, oid),
        )

    conn.commit()

    write_header = not os.path.exists(LIQUIDATE_MD)
    with open(LIQUIDATE_MD, "a") as f:
        if write_header:
            f.write("# Liquidation Log (private — not committed)\n\n")
            f.write("| Date | Positions Sold | Market Value |\n")
            f.write("|------|----------------|--------------|\n")
        f.write(f"| {TODAY} | {len(positions)} | ${total:.2f} |\n")

    conn.close()
    print(
        f"Liquidation submitted: {len(positions)} position(s), ${total:.2f}."
    )

    # Block until the account is flat so a follow-up Initial Buy sees a clean
    # slate (see SETTLE_TIMEOUT note above).
    deadline = time.time() + SETTLE_TIMEOUT
    remaining = positions
    while remaining and time.time() < deadline:
        time.sleep(SETTLE_POLL)
        remaining = alpaca.get_all_positions()
        print(f"  settling… {len(remaining)} position(s) still open")

    if remaining:
        print(
            f"WARNING: {len(remaining)} position(s) still open after "
            f"{SETTLE_TIMEOUT}s. Wait for them to clear before running Initial Buy."
        )
    else:
        cash = float(alpaca.get_account().cash)
        print(
            f"Account is flat. Available cash: ${cash:.2f}. "
            "Run Initial Buy to redistribute across the index."
        )


if __name__ == "__main__":
    run()
