#!/usr/bin/env python3
"""
One-off, read-only smoke test for the Zoya migration (see PR #16).

Exercises check_sharia()'s exact code path (Zoya verdict -> yfinance grade) against a
handful of real symbols chosen to cover each branch: an expected-COMPLIANT large cap,
a name expected to come back NON_COMPLIANT, and a couple of others for spot-checking.
Prints results only -- makes no DB/network writes and places no trades.

Not part of the production pipeline; delete after use.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constituent_scan import check_sharia  # noqa: E402

TEST_SYMBOLS = ["AAPL", "MSFT", "JPM", "XOM", "NVDA"]

if __name__ == "__main__":
    print(f"Testing {len(TEST_SYMBOLS)} symbols against the live Zoya API...\n")
    for sym in TEST_SYMBOLS:
        grade, status = check_sharia(sym)
        print(f"RESULT {sym}: grade={grade} zoya_status={status}")
