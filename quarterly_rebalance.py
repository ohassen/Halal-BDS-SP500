"""
Quarterly weight rebalance for Halal-BDS-SP500.

Trims holdings that have drifted materially ABOVE their index target weight,
selling each back down to target. Freed cash is redeployed into underweight
names by the Daily Investment job over the following sessions, so this script is
sell-only — there is no same-day rebuy and therefore no settlement race.

A position is trimmed only when its overshoot clears a hybrid band:

    actual_weight - target_weight  >  max(ABS_BAND, REL_BAND * target_weight)

The relative term catches concentration drift in larger names (where tracking
error actually accrues); the absolute floor stops tiny tail names from churning
on noise. Trims worth less than MIN_SELL_NOTIONAL are skipped to avoid dust.

Targets come from the git-committed index/constituents.csv (ACTIVE + WARNED, the
full index membership). Holdings that are no longer index members (target absent)
are left alone — constituent_scan handles compliance/index-exit force-sells.

Cadence: quarterly, aligned to S&P reconstitution (3rd Friday of Mar/Jun/Sep/Dec).
A market-cap-weighted book self-corrects price drift, so weights only need a retrim
when targets actually change — at reconstitution and compliance removals. The
workflow fires on days 22-26 of those months (after reconstitution propagates); a
market-open guard skips weekend/holiday firings, the first open day does the trim,
and later days in the window are no-ops (sell-only band; the daily job only buys
underweight names, so it can't re-breach the upper band). Also runs on demand via
workflow_dispatch.
"""

import csv
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
    INDEX_CSV,
    TODAY,
)

# ---------------------------------------------------------------------------
# Config — tune the rebalance band here
# ---------------------------------------------------------------------------

REL_BAND = 0.25          # trim when >25% above target (relative), OR ...
ABS_BAND = 0.005         # ... >0.5 percentage points above target (absolute)
MIN_SELL_NOTIONAL = 5.0  # skip trims worth less than this (avoid dust orders)

REBALANCE_MD = "reports/rebalance.md"


# ---------------------------------------------------------------------------
# Index targets
# ---------------------------------------------------------------------------

def load_index_targets() -> dict[str, float]:
    """Return {symbol: target_fraction} for every index member (ACTIVE + WARNED).

    Unlike daily_invest.load_target_weights (ACTIVE only), the rebalance trims
    against the full index target, since WARNED names are held at their weight.
    """
    targets: dict[str, float] = {}
    if not os.path.exists(INDEX_CSV):
        return targets
    with open(INDEX_CSV, newline="") as f:
        for row in csv.DictReader(f):
            try:
                targets[row["Symbol"]] = float(row["TargetWeightPct"]) / 100.0
            except (KeyError, ValueError):
                continue
    return targets


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run() -> None:
    os.makedirs("reports", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    alpaca = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)

    # The quarterly window spans several days; only act on an open-market day so
    # weekend/holiday firings skip cleanly instead of submitting rejected orders.
    if not alpaca.get_clock().is_open:
        print("Market is closed — skipping this run; the next open day in the "
              "quarterly window will rebalance.")
        conn.close()
        return

    targets = load_index_targets()
    if not targets:
        print(f"No index targets in {INDEX_CSV}. Run constituent_scan.py first.")
        conn.close()
        return

    account = alpaca.get_account()
    cash = float(account.cash)
    positions = alpaca.get_all_positions()
    pos_map = {p.symbol: float(p.market_value) for p in positions}
    total_portfolio = sum(pos_map.values()) + cash
    print(f"Portfolio value: ${total_portfolio:.2f} ({len(pos_map)} positions, ${cash:.2f} cash)")
    if total_portfolio <= 0:
        print("Empty portfolio — nothing to rebalance.")
        conn.close()
        return

    # Identify overweight holdings whose overshoot clears the hybrid band.
    trims: list[tuple[str, float, float, float]] = []
    for sym, mv in pos_map.items():
        target = targets.get(sym)
        if target is None:
            # Not an index member; leave compliance/index exits to constituent_scan.
            continue
        actual = mv / total_portfolio
        overshoot = actual - target
        band = max(ABS_BAND, REL_BAND * target)
        if overshoot > band:
            sell_notional = round(overshoot * total_portfolio, 2)
            if sell_notional >= MIN_SELL_NOTIONAL:
                trims.append((sym, sell_notional, actual, target))

    if not trims:
        print("No holdings exceed the rebalance band — nothing to trim.")
        conn.close()
        return

    trims.sort(key=lambda x: x[1], reverse=True)
    total = sum(t[1] for t in trims)
    print(f"Trimming {len(trims)} holding(s), total=${total:.2f}")

    for sym, notional, actual, target in trims:
        try:
            order = alpaca.submit_order(
                MarketOrderRequest(
                    symbol=sym,
                    notional=notional,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            status = str(order.status)
            order_id = str(order.id)
            print(
                f"  SELL {sym} ${notional:.2f} "
                f"({actual * 100:.2f}% → {target * 100:.2f}%) → {status} ({order_id})"
            )
        except Exception as e:
            status = "FAILED"
            order_id = None
            print(f"  ERROR selling {sym}: {e}")

        conn.execute(
            """INSERT INTO transactions
               (transaction_date, symbol, action, notional_amount, alpaca_order_id, status, reason)
               VALUES (?, ?, 'SELL', ?, ?, ?, 'rebalance_trim')""",
            (TODAY, sym, notional, order_id, status),
        )

    conn.commit()

    write_header = not os.path.exists(REBALANCE_MD)
    with open(REBALANCE_MD, "a") as f:
        if write_header:
            f.write("# Quarterly Rebalance Log (private — not committed)\n\n")
            f.write("| Date | Trims | Proceeds |\n")
            f.write("|------|-------|----------|\n")
        f.write(f"| {TODAY} | {len(trims)} | ${total:.2f} |\n")

    conn.close()
    print(
        f"Rebalance complete. ${total:.2f} trimmed across {len(trims)} holding(s); "
        "freed cash redeploys via the daily job."
    )


if __name__ == "__main__":
    run()
