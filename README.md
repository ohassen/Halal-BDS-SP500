# Halal-BDS-SP500

A self-managed **direct index** that replicates the S&P 500 with Shariah compliance (HalalScreener) and BDS filters applied. It runs entirely on free-tier infrastructure: scheduled GitHub Actions workflows trade a dedicated Alpaca account, and all state lives in a cached SQLite database plus committed CSV/Markdown artifacts. No server to host, no database to manage.

> **Disclaimer:** This repository and its contents are published for informational and educational purposes only. Nothing here constitutes financial advice, investment recommendations, or an offer to buy or sell any security. Invest at your own risk.

---

## How it works

**Constituent scan** (`constituent_scan.py`, runs daily at 09:00 ET — see note below): rebuilds a **strict 500-name list** from the S&P 500 (backfilled from the Russell 1000 when names are excluded), re-checks Sharia grades (monthly sweep) and BDS status (quarterly), force-sells any removed holdings, recomputes market-cap target weights, archives a monthly weights snapshot, appends any membership/status/grade/BDS changes to a permanent event log, and commits the updated public artifacts.

The list is held to exactly 500 names. Held S&P 500 members count toward the 500 — both `ACTIVE` (buy-eligible) and `WARNED` (grade D; held, no new buys) — and remaining slots are backfilled from the largest ACTIVE Russell 1000 names. Target weights are computed across exactly these 500 and sum to 100%.

**Daily invest** (`daily_invest.py`, weekdays 09:35 ET): deploys available cash into the most underweight holdings via fractional notional market orders. Skips when cash is below \$20.

### Why the scan runs every day

The scan re-checks Sharia grades through the HalalScreener API, whose free tier caps usage at ~100 requests/day. Sharia is screened as a **monthly calendar sweep**: on the 1st of each month the whole ~1,000-name universe (S&P 500 + the Russell 1000 replacement pool) becomes due, and the script re-checks up to `SHARIA_DAILY_CAP` (99) of them per run — ~10 days to work through the list — then goes dormant on Sharia until the next 1st. BDS status is re-screened separately, once per quarter (see below).

The index rebuild, force-sells, BDS check, and CSV commit run **every day** throughout the sweep, using each name's cached grade until its monthly slot comes up (a cold-start guard defers the rebuild only on a brand-new/empty database, until every name has a baseline grade). Because the throttle is an **API daily limit, not market hours**, the scan is scheduled **every day, including weekends** (`cron: '0 13 * * *'`); any force-sell orders placed while the market is closed are `TimeInForce.DAY` orders that queue for the next session.

## Compliance rules

| Condition | Action |
|---|---|
| Sharia grade ≥ B- and BDS = YES or UNKNOWN | ACTIVE — eligible for purchase |
| Sharia grade = D | WARNED — hold, no new purchases |
| Sharia grade = F | FORCE SELL |
| BDS = NO | FORCE SELL |
| BDS = UNKNOWN | Treated as compliant — no action |

- **Sharia grade** comes from the [HalalScreener](https://halalscreener.app) API.
- **BDS status** (whether a company is an explicit target of a Boycott, Divestment, Sanctions campaign) is classified by Claude Opus 4.8 **with web search** — one grounded request per symbol via the Anthropic Message Batches API — re-screened once per quarter (Mar/Jun/Sep/Dec). `UNKNOWN` is treated as compliant, since the vast majority of companies are simply not named in any campaign.

## Public artifacts

- [`index/constituents.csv`](index/constituents.csv) — the strict 500-name index composition with grades, BDS status, and target weights (weights sum to 100%)
- `index/snapshots/YYYY-MM.csv` — one dated snapshot of the full 500-name list and weights per calendar month, for historical/point-in-time reference
- [`reports/event_log.csv`](reports/event_log.csv) — **permanent, append-only** log of every event: `INDEX_ADDED`, `INDEX_REMOVED`, `STATUS_CHANGE`, `GRADE_CHANGE`, `BDS_CHANGE` (columns: `Date, Symbol, Company, EventType, OldValue, NewValue, Reason`)
- [`reports/change_log.md`](reports/change_log.md) — human-readable, rolling 30 trading days of additions, removals, and warnings
- [`reports/sharia_progress.md`](reports/sharia_progress.md) — progress of the multi-day Sharia re-check cycle

---

## Adopt this flow for your own account

You can fork this repository and run the same direct index against your own Alpaca account. The whole pipeline runs on GitHub Actions' free tier; the only external dependency is the database cache, which Actions persists between runs.

### 1. Prerequisites

Create accounts and gather API keys for each service:

| Service | Purpose | Notes |
|---|---|---|
| [Alpaca](https://alpaca.markets) | Brokerage / order execution | Use a **dedicated account**. Start with a paper account (`ALPACA_PAPER=true`) before going live. Fractional/notional trading must be enabled. |
| [HalalScreener](https://halalscreener.app) | Sharia compliance grades | Free tier ≈ 100 requests/day, 10/min. |
| [Anthropic API](https://www.anthropic.com) | BDS classification (Claude Opus 4.8 + web search) | Grounded, batched, re-screened quarterly. Has per-call cost; budget accordingly (~tens of $/quarter). |

### 2. Fork and configure the repository

1. **Fork** this repo to your own GitHub account (or use it as a template).
2. Update the bot `User-Agent` string in `constituent_scan.py` (`WIKI_HEADERS`) to point at your fork — it identifies your scraper to Wikipedia.
3. Enable GitHub Actions on the fork (Actions are disabled by default on forks: **Actions → "I understand my workflows, go ahead and enable them"**).

### 3. Add secrets and variables

In your fork, go to **Settings → Secrets and variables → Actions** and add:

**Secrets** (Settings → Secrets → Actions → *New repository secret*):

| Secret | Description |
|---|---|
| `ALPACA_INDEX_API_KEY` | Alpaca API key (dedicated account) |
| `ALPACA_INDEX_API_SECRET` | Alpaca API secret |
| `HALALSCREENER_API_KEY` | HalalScreener API key |
| `ANTHROPIC_API_KEY` | Anthropic API key (BDS classification — Claude Opus 4.8 + web search) |

**Variables** (Settings → Variables → Actions → *New repository variable*):

| Variable | Default | Description |
|---|---|---|
| `ALPACA_PAPER` | `true` | Set to `false` only when you are ready to trade real money. |

> The workflows have `permissions: contents: write` so the constituent scan can commit updated artifacts back to the repo. No further token setup is required — the default `GITHUB_TOKEN` is used.

### 4. Workflow schedules

Schedules live in `.github/workflows/`. Adjust the cron expressions for your timezone if needed (crons are in **UTC**):

- **`constituent_scan.yml`** — `0 13 * * *` (daily, 13:00 UTC / 09:00 ET). Runs every day, including weekends, by design (see ["Why the scan runs every day"](#why-the-scan-runs-every-day)).
- **`daily_invest.yml`** — `35 13 * * 1-5` (weekdays, 13:35 UTC / 09:35 ET). Weekdays only, since it places live buy orders.
- **`quarterly_rebalance.yml`** (workflow "Quarterly Rebalance") — `0 14 22-26 3,6,9,12 *` (quarterly: days 22–26 of Mar/Jun/Sep/Dec, after S&P reconstitution). Trims overweight holdings back to target; a market-open guard skips weekend/holiday firings.

You can also trigger either workflow manually from the **Actions** tab (both have `workflow_dispatch`).

### 5. First run / bootstrapping

1. From the **Actions** tab, manually run **Constituent Scan**. Because the database starts empty, the Sharia re-check will span several daily runs (~99 symbols/day → ~10 days for the full ~1,000-name universe). Track progress in `reports/sharia_progress.md`.
2. Once the first full rebuild completes, `index/constituents.csv` is populated with ACTIVE constituents and target weights.
3. The **Daily Investment** workflow then deploys cash into the most underweight ACTIVE names on its next weekday run.

### 6. Run locally (optional)

To test or run the scripts on your own machine:

```bash
pip install -r requirements.txt

# Export the same secrets the workflows use
export ALPACA_INDEX_API_KEY=...
export ALPACA_INDEX_API_SECRET=...
export HALALSCREENER_API_KEY=...
export ANTHROPIC_API_KEY=...
export ALPACA_PAPER=true        # keep paper trading while testing

python init_db.py        # create index_fund.db (also auto-created by the scripts)
python constituent_scan.py   # rebuild constituents / refresh compliance
python daily_invest.py   # deploy available cash
```

The SQLite database (`index_fund.db`) is gitignored locally and persisted between Actions runs via `actions/cache`. Losing the cache is harmless — the scan simply rebuilds it over the next few daily runs.

## Tunable parameters

Key constants you may want to adjust live near the top of the scripts:

| Constant | File | Default | Meaning |
|---|---|---|---|
| `MAX_INDEX_SIZE` | `constituent_scan.py` | `500` | Target number of constituents |
| `SHARIA_DAILY_CAP` | `constituent_scan.py` | `99` | Max Sharia API calls per run (keep under your tier's daily limit) |
| `SHARIA_RATE_LIMIT_S` | `constituent_scan.py` | `6.0` | Seconds between Sharia API calls (10/min) |
| `BDS_REFRESH_MONTHS` | `constituent_scan.py` | `{3,6,9,12}` | Calendar months the quarterly BDS web-search re-screen runs (Sharia re-screens monthly via a calendar sweep — no constant) |
| `BDS_BATCH_SIZE` | `constituent_scan.py` | `10` | Symbols per LLM BDS classification call |
| `MIN_CASH` | `daily_invest.py` | `20.0` | Skip the daily buy if account cash is below this |
| `MIN_NOTIONAL` | `daily_invest.py` | `1.0` | Minimum dollar amount per order |
| `TOP_N_GAPS` | `daily_invest.py` | `20` | How many of the most-underweight names to buy each day |

## Repository layout

```
.
├── .github/workflows/
│   ├── constituent_scan.yml # daily scan / rebuild workflow
│   └── daily_invest.yml     # weekday cash-deploy workflow
├── constituent_scan.py      # constituent rebuild + compliance checks
├── daily_invest.py          # underweight-gap cash deployment
├── init_db.py               # SQLite schema bootstrap
├── requirements.txt         # Python dependencies
├── index/
│   ├── constituents.csv     # public: strict 500-name index composition
│   └── snapshots/           # public: one dated weights snapshot per month
└── reports/                 # public: event_log.csv, change_log.md, sharia_progress.md
```

See [`HalalBDSSP500PRD.md`](HalalBDSSP500PRD.md) for the full product requirements and design rationale.
