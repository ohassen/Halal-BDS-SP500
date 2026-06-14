# Halal-BDS-SP500

A self-managed **direct index** that replicates the S&P 500 with Shariah compliance (HalalScreener) and BDS filters applied. It runs entirely on free-tier infrastructure: two GitHub Actions workflows trade a dedicated Alpaca account, and all state lives in a cached SQLite database plus committed CSV/Markdown artifacts. No server to host, no database to manage.

> **Disclaimer:** This repository and its contents are published for informational and educational purposes only. Nothing here constitutes financial advice, investment recommendations, or an offer to buy or sell any security. Invest at your own risk.

---

## How it works

**Monthly scan** (`monthly_scan.py`, runs daily at 09:00 ET — see note below): rebuilds the 500-name target list from the S&P 500 (backfilled from the Russell 1000 when names are excluded), re-checks Sharia grades and BDS status, force-sells any removed holdings, recomputes market-cap target weights, and commits updated public artifacts.

**Daily invest** (`daily_invest.py`, weekdays 09:35 ET): deploys available cash into the most underweight holdings via fractional notional market orders. Skips when cash is below \$20.

### Why the "monthly" scan runs every day

The scan re-checks Sharia grades through the HalalScreener API, whose free tier caps usage at ~100 requests/day. The script therefore refreshes at most `SHARIA_DAILY_CAP` (95) of the most-stale symbols per run, exits early, and resumes the next day until all ~500 names have been refreshed within their 30-day staleness window. The full rebuild + force-sells only execute once every stale symbol has been refreshed.

Because this throttling is driven purely by an **API daily limit and not by market hours**, the scan is scheduled **every day, including weekends** (`cron: '0 13 * * *'`). Weekends add two extra refresh days per week, so the rolling re-check completes sooner. On partial-check days the script returns before any trading; any force-sell orders submitted while the market is closed are `TimeInForce.DAY` orders that simply queue for the next session.

## Compliance rules

| Condition | Action |
|---|---|
| Sharia grade ≥ B- and BDS = YES or UNKNOWN | ACTIVE — eligible for purchase |
| Sharia grade = D | WARNED — hold, no new purchases |
| Sharia grade = F | FORCE SELL |
| BDS = NO | FORCE SELL |
| BDS = UNKNOWN | Treated as compliant — no action |

- **Sharia grade** comes from the [HalalScreener](https://halalscreener.app) API.
- **BDS status** (whether a company appears on Boycott, Divestment, Sanctions lists) is classified by an LLM (`anthropic/claude-opus-4-8` via OpenRouter) from its training data, in batches of 10 symbols.

## Public artifacts

- [`index/constituents.csv`](index/constituents.csv) — current index composition with grades, BDS status, and target weights
- [`reports/change_log.md`](reports/change_log.md) — rolling 30 trading days of additions, removals, and warnings
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
| [OpenRouter](https://openrouter.ai) | BDS classification via an LLM | Uses `anthropic/claude-opus-4-8`. Has per-call cost; budget accordingly. |

### 2. Fork and configure the repository

1. **Fork** this repo to your own GitHub account (or use it as a template).
2. Update the bot `User-Agent` string in `monthly_scan.py` (`WIKI_HEADERS`) to point at your fork — it identifies your scraper to Wikipedia.
3. Enable GitHub Actions on the fork (Actions are disabled by default on forks: **Actions → "I understand my workflows, go ahead and enable them"**).

### 3. Add secrets and variables

In your fork, go to **Settings → Secrets and variables → Actions** and add:

**Secrets** (Settings → Secrets → Actions → *New repository secret*):

| Secret | Description |
|---|---|
| `ALPACA_INDEX_API_KEY` | Alpaca API key (dedicated account) |
| `ALPACA_INDEX_API_SECRET` | Alpaca API secret |
| `HALALSCREENER_API_KEY` | HalalScreener API key |
| `OPENROUTER_API_KEY` | OpenRouter API key (BDS classification) |

**Variables** (Settings → Variables → Actions → *New repository variable*):

| Variable | Default | Description |
|---|---|---|
| `ALPACA_PAPER` | `true` | Set to `false` only when you are ready to trade real money. |

> The workflows have `permissions: contents: write` so the monthly scan can commit updated artifacts back to the repo. No further token setup is required — the default `GITHUB_TOKEN` is used.

### 4. Workflow schedules

Schedules live in `.github/workflows/`. Adjust the cron expressions for your timezone if needed (crons are in **UTC**):

- **`monthly_scan.yml`** — `0 13 * * *` (daily, 13:00 UTC / 09:00 ET). Runs every day, including weekends, by design (see ["Why the monthly scan runs every day"](#why-the-monthly-scan-runs-every-day)).
- **`daily_invest.yml`** — `35 13 * * 1-5` (weekdays, 13:35 UTC / 09:35 ET). Weekdays only, since it places live buy orders.

You can also trigger either workflow manually from the **Actions** tab (both have `workflow_dispatch`).

### 5. First run / bootstrapping

1. From the **Actions** tab, manually run **Monthly Constituent Scan**. Because the database starts empty, the Sharia re-check will span several daily runs (~95 symbols/day → ~6 days for the full S&P 500). Track progress in `reports/sharia_progress.md`.
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
export OPENROUTER_API_KEY=...
export ALPACA_PAPER=true        # keep paper trading while testing

python init_db.py        # create index_fund.db (also auto-created by the scripts)
python monthly_scan.py   # rebuild constituents / refresh compliance
python daily_invest.py   # deploy available cash
```

The SQLite database (`index_fund.db`) is gitignored locally and persisted between Actions runs via `actions/cache`. Losing the cache is harmless — the scan simply rebuilds it over the next few daily runs.

## Tunable parameters

Key constants you may want to adjust live near the top of the scripts:

| Constant | File | Default | Meaning |
|---|---|---|---|
| `MAX_INDEX_SIZE` | `monthly_scan.py` | `500` | Target number of constituents |
| `SHARIA_DAILY_CAP` | `monthly_scan.py` | `95` | Max Sharia API calls per run (keep under your tier's daily limit) |
| `SHARIA_RATE_LIMIT_S` | `monthly_scan.py` | `6.0` | Seconds between Sharia API calls (10/min) |
| `STALE_DAYS` | `monthly_scan.py` | `30` | Re-check a symbol only if last checked ≥ this many days ago |
| `BDS_BATCH_SIZE` | `monthly_scan.py` | `10` | Symbols per LLM BDS classification call |
| `MIN_CASH` | `daily_invest.py` | `20.0` | Skip the daily buy if account cash is below this |
| `MIN_NOTIONAL` | `daily_invest.py` | `1.0` | Minimum dollar amount per order |
| `TOP_N_GAPS` | `daily_invest.py` | `20` | How many of the most-underweight names to buy each day |

## Repository layout

```
.
├── .github/workflows/
│   ├── monthly_scan.yml     # daily scan / rebuild workflow
│   └── daily_invest.yml     # weekday cash-deploy workflow
├── monthly_scan.py          # constituent rebuild + compliance checks
├── daily_invest.py          # underweight-gap cash deployment
├── init_db.py               # SQLite schema bootstrap
├── requirements.txt         # Python dependencies
├── index/constituents.csv   # public: current index composition
└── reports/                 # public: change_log.md, sharia_progress.md
```

See [`HalalBDSSP500PRD.md`](HalalBDSSP500PRD.md) for the full product requirements and design rationale.
