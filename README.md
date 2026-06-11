# Halal-BDS-SP500

A self-managed direct index that replicates the S&P 500 with Shariah compliance (HalalScreener) and BDS filters applied. Runs on a dedicated Alpaca account via two GitHub Actions workflows.

> **Disclaimer:** This repository and its contents are published for informational and educational purposes only. Nothing here constitutes financial advice, investment recommendations, or an offer to buy or sell any security. Invest at your own risk.

---

## How it works

**Monthly scan** (first Monday of each month, pre-market): rebuilds the 500-name target list, re-checks Sharia grades and BDS status, force-sells any removed holdings, recomputes market-cap weights, and commits updated public artifacts.

**Daily invest** (weekdays, 09:35 ET): deploys available cash into the most underweight holdings via fractional notional market orders.

## Compliance rules

| Condition | Action |
|---|---|
| Sharia grade ≥ B- and BDS = YES or UNKNOWN | ACTIVE — eligible for purchase |
| Sharia grade = D | WARNED — hold, no new purchases |
| Sharia grade = F | FORCE SELL |
| BDS = NO | FORCE SELL |
| BDS = UNKNOWN | Treated as compliant — no action |

## Public artifacts

- [`index/constituents.csv`](index/constituents.csv) — current index composition with grades, BDS status, and target weights
- [`reports/change_log.md`](reports/change_log.md) — rolling 30 trading days of additions, removals, and warnings

## Secrets required

Set these in the repository's **Settings → Secrets → Actions**:

| Secret | Description |
|---|---|
| `ALPACA_INDEX_API_KEY` | Alpaca API key (dedicated account) |
| `ALPACA_INDEX_API_SECRET` | Alpaca API secret |
| `HALALSCREENER_API_KEY` | HalalScreener API key |
| `OPENROUTER_API_KEY` | OpenRouter API key (for BDS classification via Claude Opus) |
