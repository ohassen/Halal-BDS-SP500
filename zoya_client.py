#!/usr/bin/env python3
"""
Zoya GraphQL Client

Thin client for the Zoya Shariah-compliance API (https://zoya.finance).

Zoya returns a 3-tier compliance verdict (COMPLIANT / NON_COMPLIANT /
QUESTIONABLE) with no financial-ratio breakdown exposed at any tier --
confirmed by direct GraphQL schema probing. Granularity is recovered
separately via an in-house yfinance-based grade (see yfinance_grading.py),
computed only for symbols Zoya marks COMPLIANT.

Usage:
    from zoya_client import get_zoya_report

    report = get_zoya_report("AAPL")
    # {"symbol": "AAPL", "status": "COMPLIANT", "purification_ratio": 0.01,
    #  "report_date": "2026-08-01"}
"""

import os
from typing import Optional

import requests

ZOYA_GRAPHQL_URL = "https://api.zoya.finance/graphql"

# Uses a GraphQL variable for the symbol (rather than string-interpolating
# it into the query text) so arbitrary ticker input can never malform the
# query -- same query shape as `report(symbol: "AAPL") { status
# purificationRatio reportDate }`, just parameterized.
_REPORT_QUERY = """
query CompliaceReport($symbol: String!) {
  basicCompliance {
    report(symbol: $symbol) {
      status
      purificationRatio
      reportDate
    }
  }
}
"""


def get_zoya_report(symbol: str, api_key: Optional[str] = None, timeout: int = 10) -> Optional[dict]:
    """Fetch a basic compliance report for `symbol` from the Zoya GraphQL API.

    Returns a dict with symbol/status/purification_ratio/report_date on
    success, or None on any request/parsing failure or missing API key
    (mirrors check_halalscreener_compliance()'s existing "return None on
    failure" behavior in the notebook pipeline).
    """
    api_key = api_key or os.getenv("ZOYA_API_KEY")
    if not api_key:
        return None

    # The key value already includes its "live-" prefix -- pass it through
    # as-is, no "Bearer " or other scheme prefix.
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    payload = {"query": _REPORT_QUERY, "variables": {"symbol": symbol}}

    try:
        response = requests.post(ZOYA_GRAPHQL_URL, json=payload, headers=headers, timeout=timeout)
        if response.status_code != 200:
            return None
        data = response.json()
        if "errors" in data:
            return None
        report = (data.get("data") or {}).get("basicCompliance", {}).get("report")
        if not report:
            return None
        return {
            "symbol": symbol,
            "status": report.get("status"),
            "purification_ratio": report.get("purificationRatio"),
            "report_date": report.get("reportDate"),
        }
    except Exception:
        return None
