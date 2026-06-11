# PRD: Halal-BDS-SP500 Index Fund

## Overview

A self-managed "direct index" that replicates the S&P 500 with Shariah + BDS compliance
filters applied. Runs on a separate Alpaca account from Halal_Dip_Trader. Two workflows:
a **monthly constituent scan** that rebuilds the target list, and a **daily investment flow**
that deploys available cash into the most underweight holdings via fractional notional orders.

No Telegram notifications — reporting is a rolling `.md` file committed to the repo by
GitHub Actions.

---

## Repository

`https://github.com/ohassen/Halal-BDS-SP500.git` — fresh repo, no existing code.

---

## Design Decisions (settled)

| Parameter | Value |
|---|---|
| Index size | 500 stocks, always backfilled to 500 |
| Weighting | Market-cap weighted |
| Replacement pool | Next-largest US stocks by market cap (rank 501+) passing both filters |
| Sharia entry bar | HalalScreener grade **B- or better** |
| Sharia warn | Grade == **D** → hold + warning flag |
| Sharia exit | Grade == **F** → sell immediately at monthly scan |
| BDS warn | N/A — UNKNOWN is treated as compliant (no warning) |
| BDS exit | AI returns `bds_friendly: NO` → sell immediately at monthly scan |
| Min daily cash to deploy | **$20** (skip run if below) |
| Daily buy strategy | Most-underweight-first cash-flow rebalancing |
| Notifications | None (Telegram not used here) |
| Account | Separate Alpaca account (paper first, then live) |

---

## Data Sources

### S&P 500 Constituent List
Use Wikipedia scrape — reliable, free, no API key:
```python
import pandas as pd
sp500 = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]
symbols = sp500['Symbol'].str.replace('.', '-').tolist()  # e.g. BRK.B → BRK-B for yfinance
```

### Market Cap (for weighting + replacement ranking)
Use `yfinance`:
```python
import yfinance as yf
info = yf.Ticker(symbol).info
market_cap = info.get('marketCap', 0)
```
For replacement pool (rank 501+): download Russell 1000 from Wikipedia
(`https://en.wikipedia.org/wiki/Russell_1000_Index`) and subtract S&P 500 symbols.

### Sharia Compliance
HalalScreener API — already integrated in HDT. Returns `grade` (A+ → F) + `status`
(Halal/Haram/Doubtful). Grade rank map from `backtest_engine.py`:
```python
GRADE_RANK = {"A+": 9, "A": 8, "A-": 7, "B+": 6, "B": 5, "B-": 4,
              "C+": 3, "C": 2, "C-": 1, "D": 0, "F": -1}
```
Endpoint: `GET https://halalscreener.app/api/v1/screen?symbol=TICKER`
Header: `Authorization: Bearer {HALALSCREENER_API_KEY}`

### BDS Compliance
Claude AI (training data only, no web search) — same pattern as HDT Cell 6 prompt but
stripped down to BDS-only. Return field: `bds_friendly: YES | NO | UNKNOWN`. Cache
results in SQLite; only re-check monthly for `UNKNOWN` or on new symbols.

---

## Compliance State Machine

```
                    ┌─────────────────────────────────────────────────┐
                    │  SHARIA                        BDS               │
                    │                                                   │
ENTRY ELIGIBLE:     │  grade ≥ B-                    bds_friendly=YES  │
                    │                                                   │
HOLD (no flag):     │  grade ≥ D (i.e. B- thru D+)  bds_friendly=YES  │
                    │                                                   │
HOLD + WARN:        │  grade == D                                      │
                    │                                                   │
FORCE SELL:         │  grade == F                    bds_friendly=NO   │
                    └─────────────────────────────────────────────────┘
```

BDS UNKNOWN is treated as compliant — no warning, no action. Only a confirmed NO triggers
a sell. A stock is FORCE SOLD if sharia=F OR bds=NO. Hysteresis: enters at B-,
only sharia exits at F — no churn at borderline grades.

---

## Database Schema

Single SQLite file `index_fund.db`.

```sql
-- Active/warned/removed constituents
CREATE TABLE constituents (
    symbol         TEXT PRIMARY KEY,
    company_name   TEXT,
    market_cap     REAL,
    target_weight  REAL,   -- normalized cap weight (sums to 1.0 across ACTIVE)
    sharia_grade   TEXT,
    bds_status     TEXT,   -- YES / NO / UNKNOWN
    index_status   TEXT,   -- ACTIVE | WARNED | REMOVED
    warning_reason TEXT,   -- 'sharia_D' | null  (bds_unknown is no longer a warning)
    added_date     TEXT,
    removed_date   TEXT,
    last_checked   TEXT
);

-- Compliance check history (sharia + BDS per symbol per date)
CREATE TABLE compliance_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    check_date   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    sharia_grade TEXT,
    sharia_status TEXT,  -- COMPLIANT / NON_COMPLIANT / UNKNOWN
    bds_status   TEXT,
    UNIQUE(check_date, symbol)
);

-- Transaction log
CREATE TABLE transactions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_date TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    action           TEXT NOT NULL,  -- BUY / SELL
    notional_amount  REAL,
    quantity         REAL,           -- fractional shares filled
    price            REAL,
    alpaca_order_id  TEXT,
    status           TEXT,
    reason           TEXT            -- 'rebalance' | 'forced_sell_sharia' | 'forced_sell_bds'
);

-- Change log for the rolling .md report
CREATE TABLE change_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    log_date       TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    event_type     TEXT NOT NULL,  -- ADDED | REMOVED | WARNED | WARNING_CLEARED
    old_grade      TEXT,
    new_grade      TEXT,
    bds_status     TEXT,
    reason         TEXT
);
```

---

## Monthly Workflow

**Trigger:** GitHub Actions cron — 1st trading day of each month, pre-market (e.g. 08:00 ET).

**Steps:**

1. **Fetch S&P 500 symbols** via Wikipedia scrape.
2. **Fetch market caps** for all S&P 500 + Russell 1000 symbols via yfinance `.info`.
3. **Sharia check** all symbols not checked in the last 30 days via HalalScreener API
   (rate-limit: ~0.5s between calls; ~600 symbols → ~5 min).
4. **BDS check** all symbols with stale or missing BDS cache (Claude AI, training data only;
   batch prompt acceptable — check 10 symbols per call to reduce cost).
5. **Apply state machine** to every symbol:
   - sharia F or bds=NO → REMOVED (sell if held)
   - sharia D → WARNED (hold, flag)
   - sharia ≥ B- and bds=YES or UNKNOWN → ACTIVE
6. **Build 500-name list**: start with S&P 500 ACTIVE symbols. If fewer than 500, backfill
   from Russell 1000 remainder ranked by market cap descending until 500 ACTIVE reached.
7. **Compute target weights**: for all 500 ACTIVE symbols,
   `weight_i = market_cap_i / sum(market_cap for all 500)`.
8. **Execute forced sells** for REMOVED symbols via Alpaca market sell orders.
9. **Persist** updated `constituents` table and `change_log` entries.
10. **Generate rolling report** (see Reporting section).

---

## Daily Workflow

**Trigger:** GitHub Actions cron — weekdays, ~09:35 ET (5 min after market open).

**Steps:**

1. **Check cash**: `GET /v2/account` → `cash`. If `cash < $20`, log and exit.
2. **Fetch positions**: `GET /v2/positions` → map `{symbol: market_value}`.
3. **Load target weights** from `constituents` where `index_status = 'ACTIVE'`.
4. **Compute gaps**:
   ```
   total_portfolio = sum(market_value for all positions) + cash
   actual_weight_i = market_value_i / total_portfolio
   gap_i = target_weight_i - actual_weight_i
   ```
5. **Rank by gap descending** (most underweight first). Filter to `gap > 0`.
6. **Allocate cash** across top-N gaps proportionally to gap size, subject to:
   - Each allocation ≥ $1.00 (Alpaca notional minimum)
   - Total allocations ≤ available cash
   - Suggested N: top 20 gaps (concentrates purchases without submitting 500 orders)
7. **Place fractional notional market orders** via Alpaca:
   ```python
   from alpaca.trading.requests import MarketOrderRequest
   from alpaca.trading.enums import OrderSide, TimeInForce
   MarketOrderRequest(symbol=sym, notional=dollar_amount,
                      side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
   ```
   ⚠️ **This is different from HDT** — HDT uses integer share counts with trailing stop buys.
   This index fund uses **notional (dollar) fractional market orders** for immediate execution.
8. **Save** each order to `transactions` table.
9. **Append** a one-line summary to the rolling `.md` report.

---

## Public vs Private data

This is a **public repo**. The rule: index composition and weights are public;
account balances and transaction dollar amounts are private.

| Artifact | Committed to repo (public) |
|---|---|
| `index/constituents.csv` | ✅ — symbol, company, grade, BDS status, target weight % |
| `reports/change_log.md` | ✅ — additions, removals, warnings (no dollar amounts) |
| `index_fund.db` | ❌ — contains transactions table with cash amounts; gitignored |
| `reports/daily_buys.md` | ❌ — reveals account size; gitignored |

Add to `.gitignore`:
```
index_fund.db
reports/daily_buys.md
logs/
```

---

## Reporting (no Telegram)

### `index/constituents.csv` (public, committed after every monthly scan)
Current index composition — the core public deliverable of the repo.
```
Symbol,Company,ShariaGrade,BDSStatus,TargetWeightPct,IndexStatus,Warning
AAPL,Apple Inc,A+,YES,7.12,ACTIVE,
MSFT,Microsoft Corp,B+,YES,6.87,ACTIVE,
AMGN,Amgen Inc,D,YES,0.43,WARNED,sharia_D
...
```

### `reports/change_log.md` (public, rolling 30 trading days)
Committed by GitHub Actions after each monthly scan. Each run prepends new entries
and drops entries older than 30 trading days.

```markdown
# Halal-BDS-SP500 Change Log

> **Disclaimer:** This index is published for informational purposes only and does not
> constitute financial advice. Past performance does not guarantee future results.

_Last updated: 2026-06-01 | Active: 498 | Warned: 6_

---

## 2026-06-01 (Monthly Scan)
### Added (3)
| Symbol | Company | Grade | BDS | Market Cap |
|--------|---------|-------|-----|-----------|
| DECK | Deckers Outdoor | B | YES | $22.4B |

### Removed (2) — Force Sold
| Symbol | Company | Reason |
|--------|---------|--------|
| META | Meta Platforms | BDS = NO |

### Warned (1) — Grade slipped to D
| Symbol | Company | Grade |
|--------|---------|-------|
| AMGN | Amgen | D |

---
## 2026-05-01 (Monthly Scan)
...
```

### README.md disclaimer (public)
Top of README must include:
```
> ⚠️ **Disclaimer:** This repository and its contents are published for informational
> and educational purposes only. Nothing here constitutes financial advice,
> investment recommendations, or an offer to buy or sell any security.
> Invest at your own risk.
```

---

## GitHub Actions Architecture

```
.github/workflows/
  monthly_scan.yml      # cron: '0 13 1-7 * 1'  (first Monday of month, 09:00 ET)
  daily_invest.yml      # cron: '0 13 * * 1-5'  (09:00 ET weekdays)
```

**Secrets needed** (separate from HDT):
- `ALPACA_INDEX_API_KEY` / `ALPACA_INDEX_API_SECRET` (separate account)
- `HALALSCREENER_API_KEY` (can share with HDT)
- `ANTHROPIC_API_KEY` (can share with HDT)

**Artifact commits:** Each workflow commits only the public artifacts —
`index/constituents.csv` and `reports/change_log.md` — back to the repo
(same rebase-and-push pattern as HDT's workflows). `index_fund.db` and
`reports/daily_buys.md` are gitignored; the DB persists via a GitHub Actions
cache or artifact instead (decide in implementation).

---

## Code to Reuse from HDT

| Pattern | HDT source | Notes |
|---|---|---|
| HalalScreener API call + grade parsing | `halal_dip_trader.ipynb` Cell 5 | Copy `check_halalscreener_compliance()`, keep grade field |
| GRADE_RANK dict | `backtest_engine.py` lines 680-686 | Copy verbatim |
| BDS Claude prompt | `halal_dip_trader.ipynb` Cell 6 | Strip to BDS-only, no dip analysis, no web search |
| SQLite init pattern | `halal_dip_trader.ipynb` Cell 4 | New schema, same pattern |
| Alpaca client init | `execute_buys.ipynb` Cell 1 | Same, different env var names |
| GitHub Actions commit-back | `.github/workflows/staging-morning-scan.yml` | Same rebase-and-push pattern |

---

## Key Differences from HDT

- **Fractional notional orders** (not integer shares, not trailing stops)
- **No dip scanning** — buy the index every day regardless of price movement
- **No AI research per trade** — AI only used for BDS classification (monthly, cached)
- **No Telegram** — `.md` file reports committed to repo
- **No blacklist / 30-day exclusion** — index fund always holds its 500 names

---

## Open Questions for Implementation

1. **Russell 1000 source**: Wikipedia page for Russell 1000 works but may lag.
   Alternative: use yfinance to pull all US equities above $5B market cap as the
   replacement pool — more dynamic but no guaranteed "rank 501" semantics.

2. **HalalScreener rate limits**: ~0.5s between calls, 600 symbols = ~5 min.
   Confirm no daily call quota that would be exhausted by the initial run.

3. **Notional order minimum on PAPER**: Alpaca paper accounts support fractional/notional
   orders — confirm `notional` param works identically on paper vs live.

4. **Dividend handling**: Out of scope for v1. Future: track dividends and implement
   purification (donate haram income %). 

5. **Monthly sell of F-graded stocks**: Should removed symbols be added to a re-entry
   cooldown (e.g. 3 months)? Out of scope for v1 — simply remove and backfill.

---

## Verification Plan

1. Run monthly scan against paper account: confirm `constituents` table has 500 ACTIVE rows
   with `target_weight` values summing to 1.0 (within float tolerance).
2. Manually force one symbol to grade F in DB; re-run monthly sell step; confirm Alpaca
   market sell order placed and symbol status → REMOVED.
3. Manually force one symbol to grade D; confirm it stays in constituents with
   `index_status = WARNED` and `warning_reason = 'sharia_D'`; confirm change_log entry.
4. Run daily invest with $50 paper cash; confirm fractional notional orders placed for the
   most underweight names; confirm `transactions` table updated.
5. Read `reports/change_log.md` and `reports/daily_buys.md` after a commit cycle.
