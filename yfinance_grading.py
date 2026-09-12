#!/usr/bin/env python3
"""
In-house Financial-Ratio Grading (yfinance)

Zoya's compliance API only returns a 3-tier COMPLIANT / NON_COMPLIANT /
QUESTIONABLE verdict with no financial-ratio breakdown at any tier. This
module recovers HalalScreener-style letter-grade granularity by computing
the standard Shariah debt / securities / income ratios ourselves from
yfinance data.

This is a layer-2 risk/robustness filter, applied only to symbols already
marked COMPLIANT by Zoya (layer 1) -- it never overrides that compliance
decision.

Usage:
    from yfinance_grading import compute_ratios

    ratios, error = compute_ratios("AAPL")
    # ratios = {"as_of": "2026-06-30", "debt_ratio": 0.04, ...,
    #           "grade": "A+", "score": 96.7, "income_note": None}
    # error is set (and ratios is None) only when the underlying data is
    # unusable (e.g. no market cap, no balance sheet).
"""

from typing import Optional, Tuple

import yfinance as yf

DEBT_CEILING = 0.30
SECURITIES_CEILING = 0.30
INCOME_CEILING = 0.05

# (score_floor, grade) -- A+/A/B+/B/C+/C/C-/F published by HalalScreener;
# A-/B-/D/D- are unpublished, interpolated. F is reserved for an ACTUAL
# breach (worst_fraction >= 1.0 -> score <= 0) since every stock reaching
# this layer already passed as Zoya-COMPLIANT -- F here means our own
# ratio calc disagrees with Zoya's and shows a ceiling actually crossed,
# not merely close to it. D/D- absorb the near-threshold-but-compliant zone.
GRADE_BANDS = [
    (90, "A+"), (85, "A"), (80, "A-"), (70, "B+"), (65, "B"),
    (60, "B-"), (50, "C+"), (40, "C"), (30, "C-"), (10, "D"),
    (1, "D-"), (0, "F"),
]


def grade_from_score(score: float) -> str:
    """Map a 0-100 score to a HalalScreener-style letter grade."""
    for floor, grade in GRADE_BANDS:
        if score >= floor:
            return grade
    return "F"


def _row(df, latest_col, *names):
    """Return the first present, non-NaN row value among `names`."""
    for n in names:
        if n in df.index:
            val = df.loc[n, latest_col]
            if val == val:  # not NaN
                return float(val)
    return None


def compute_ratios(symbol: str) -> Tuple[Optional[dict], Optional[str]]:
    """Compute debt/securities/income Shariah ratios and a letter grade.

    Returns (ratios_dict, None) on success, or (None, error_reason) when
    the underlying yfinance data is unusable. Missing income-ratio data
    is recorded in `income_note` -- it is never treated as a silent 0 or
    scored as a penalty (explicit product decision).
    """
    t = yf.Ticker(symbol)
    bs = t.quarterly_balance_sheet  # NOT t.balance_sheet -- that's annual/stale
    inc = t.quarterly_income_stmt
    info = t.info
    market_cap = info.get("marketCap")
    if not market_cap:
        return None, "no market cap"
    if bs is None or bs.empty:
        return None, "no balance sheet data"

    bs_latest = bs.columns[0]
    total_debt = _row(bs, bs_latest, "Total Debt") or 0.0
    cash_and_sti = _row(bs, bs_latest,
        "Cash Cash Equivalents And Short Term Investments",
        "Cash And Cash Equivalents") or 0.0
    debt_ratio = total_debt / market_cap
    securities_ratio = cash_and_sti / market_cap

    income_ratio = None
    income_note = None
    if inc is not None and not inc.empty:
        inc_latest = inc.columns[0]
        total_revenue = _row(inc, inc_latest, "Total Revenue", "Operating Revenue")
        interest_income = _row(inc, inc_latest, "Interest Income", "Interest Income Non Operating")
        if interest_income is None:
            net_ii = _row(inc, inc_latest, "Net Interest Income")
            if net_ii is not None and net_ii > 0:
                interest_income = net_ii
                income_note = "used Net Interest Income fallback (nets against expense)"
        if interest_income is not None and total_revenue:
            income_ratio = interest_income / total_revenue
        else:
            income_note = income_note or "interest income not reported for this symbol"

    fractions = {"debt": debt_ratio / DEBT_CEILING, "securities": securities_ratio / SECURITIES_CEILING}
    if income_ratio is not None:
        fractions["income"] = income_ratio / INCOME_CEILING
    worst_key = max(fractions, key=fractions.get)
    worst_fraction = fractions[worst_key]
    score = max(0.0, 100.0 * (1.0 - worst_fraction))
    grade = grade_from_score(score)

    return {
        "as_of": str(bs_latest.date()),
        "debt_ratio": debt_ratio,
        "securities_ratio": securities_ratio,
        "income_ratio": income_ratio,
        "worst_driver": worst_key,
        "score": score,
        "grade": grade,
        "income_note": income_note,
    }, None
