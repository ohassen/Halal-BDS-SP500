# Halal-BDS-SP500

A self-managed **direct index** that tracks the S&P 500 while excluding companies that fail **Shariah-compliance** screening or are **targets of a BDS (Boycott, Divestment, Sanctions) campaign**. It buys and holds the underlying stocks itself (no fund, no ETF wrapper) through a dedicated [Alpaca](https://alpaca.markets) brokerage account.

Everything runs on free-tier infrastructure: scheduled GitHub Actions workflows do the work, and all state lives in a cached SQLite database plus CSV/Markdown files committed to this repository. There is no server to host and no database to manage. You can read the current index, its history, and every change ever made directly in this repo, or fork it and run the same strategy on your own account.

> **Disclaimer:** This repository and its contents are published for informational and educational purposes only. Nothing here constitutes financial advice, religious or legal rulings, investment recommendations, or an offer to buy or sell any security. Screening data comes from third-party sources and an AI model, and **can be wrong** (see [Known limitations](#known-limitations)). Invest at your own risk.

---

## At a glance

- **Universe:** the S&P 500, backfilled from the Russell 1000 whenever a company is excluded, so the index always holds exactly **500 names**.
- **Weighting:** market-cap weighted across those 500 names (weights sum to 100%).
- **Sharia screening:** every company is checked monthly using the [Zoya](https://zoya.finance) compliance API, plus an in-house financial-ratio letter grade (`A+` to `F`) for the companies Zoya passes.
- **BDS screening:** every company is checked quarterly by a Claude AI model with live web search.
- **Trading:** a daily job invests available cash into the most underweight holdings; a quarterly job trims positions that have drifted above target. Anything that fails screening is force-sold.
- **Transparent:** the composition, weights, grades, BDS blacklist, and a permanent event log are all public files in this repo.

---

## How it works

**Constituent scan** (`constituent_scan.py`, runs daily at 09:00 ET): rebuilds a **strict 500-name list** from the S&P 500 (backfilled from the Russell 1000 when names are excluded), re-checks Sharia grades (monthly) and BDS status (quarterly), force-sells any removed holdings, recomputes market-cap target weights, archives a monthly weights snapshot, appends any membership/status/grade/BDS changes to a permanent event log, and commits the updated public artifacts.

The list is held to exactly 500 names. Held S&P 500 members count toward the 500 whether they are `ACTIVE` (buy-eligible) or `WARNED` (held, but no new buys), and the remaining slots are backfilled from the largest `ACTIVE` Russell 1000 names. Target weights are computed across exactly these 500 and sum to 100%.

**Daily invest** (`daily_invest.py`, weekdays 09:35 ET): deploys available cash into the most underweight holdings using fractional, notional market orders. It skips the day when cash is below \$20.

**Quarterly rebalance** (`quarterly_rebalance.py`, after each S&P reconstitution): sells holdings that have drifted materially above their target weight. The freed cash is then redeployed into underweight names by the daily job over the following sessions.

### Why the scan runs every day

Sharia is screened as a **monthly calendar sweep**: on the 1st of each month, the whole ~1,000-name universe (S&P 500 plus the Russell 1000 replacement pool) becomes due and is re-checked in a single run, after which Sharia screening stays dormant until the next 1st. BDS status is re-screened separately, once per quarter.

The index rebuild, force-sells, BDS check, and CSV commit run **every day**, using each company's cached grade between monthly sweeps. The scan is scheduled every day, including weekends (`cron: '0 13 * * *'`), so that exclusions are picked up promptly. Any force-sell orders placed while the market is closed are `TimeInForce.DAY` orders that queue for the next session.

---

## Screening rules

| Condition | Action |
|---|---|
| Sharia grade B- or better, and BDS = YES or UNKNOWN | **ACTIVE** — eligible for purchase |
| Sharia grade C+, C, C-, or D (below B-) | **WARNED** — keep holding, no new purchases |
| Sharia grade = F | **FORCE SELL** |
| BDS = NO (confirmed target) | **FORCE SELL** and **permanently blacklisted** |
| BDS = UNKNOWN | Treated as compliant — no action |
| Grade could not be determined | Treated as compliant until a grade is available |

### Sharia screening

- **Zoya decides compliance.** Zoya's API returns one of three verdicts per stock: `COMPLIANT`, `NON_COMPLIANT`, or `QUESTIONABLE`. This project is deliberately strict: `QUESTIONABLE` is treated the same as `NON_COMPLIANT` (no grey-zone pass), and both are graded `F` and force-sold.
- **A letter grade measures how comfortably a company passes.** Zoya's API gives a verdict but no ratio breakdown, so for companies Zoya marks `COMPLIANT`, this project computes its own letter grade (`A+` to `F`) from financial statements pulled via [yfinance](https://github.com/ranaroussi/yfinance): debt, cash-and-securities, and non-compliant-income ratios relative to common screening thresholds. The grade is a robustness signal layered on top of Zoya's verdict. It can move a passing company to `WARNED` (thin margin), but it never overrides a Zoya failure.
- **Grade bands:** `A+` (score ≥ 90) down through `B-` (≥ 60) are fully eligible; `C+` through `D-` are held but not added to; `F` is sold.

### BDS screening

- **Who is flagged:** a company is flagged if it is an explicit target of a BDS campaign, is materially complicit in the Israeli military/occupation/settlements, is a named major shareholder of an Israeli defense contractor or weapons manufacturer (not merely incidental passive index exposure), or operates an R&D/engineering/development center in Israel (a sales-only office does not count).
- **How it is checked:** each company is classified by Claude Opus 5.5 **with web search** — one grounded request per symbol through the Anthropic Message Batches API — once per quarter (Mar/Jun/Sep/Dec, aligned with S&P reconstitution). `UNKNOWN` (nothing found) is treated as compliant, since the vast majority of companies are simply not named in any campaign. If the model declines or can't answer a request for a company, that company is recorded as `UNKNOWN`; if the whole batch fails or times out, the previous results are carried forward and the quarter is retried on a later run.
- **Scoped screening:** each quarter only re-checks roughly the 500 index names: the S&P 500 plus just enough Russell 1000 backfill candidates (highest market cap first) to fill vacated slots. This keeps the cost near ~500 grounded requests per quarter.
- **Permanent blacklist:** once a company is confirmed targeted (`BDS = NO`), it is blacklisted **forever**. It is recorded in [`index/bds_blacklist.json`](index/bds_blacklist.json), never re-screened, and never re-admitted to the index even if a later check would clear it.

### Known limitations

No automated screen is a substitute for your own judgment or for a qualified scholar's guidance.

- **Third-party Sharia data can be wrong or lag reality.** Screening providers use different methodologies and update at different times, and any single provider can misclassify a company.
- **BDS classification is AI-generated.** The model searches the web and can miss a campaign, misread a source, or be wrong about a company's operations. Treat `BDS = YES/UNKNOWN` as "nothing found", not as proof of compliance. The criteria above are also a judgment call that you may want to adjust for your own standards (see `BDS_SYSTEM` in `constituent_scan.py`).
- **Financial-ratio grades are approximations.** They come from public financial statements via yfinance and simplified thresholds, and may lag a company's latest filings or mis-handle unusual statements. A missing or unusable data set results in an unknown grade, which is treated as compliant as long as Zoya passes the company.
- **Spot-check holdings** you care about against a second source before relying on this index.

---

## Public artifacts

- [`index/constituents.csv`](index/constituents.csv) — the strict 500-name index composition with grades, BDS status, and target weights (weights sum to 100%)
- [`index/bds_blacklist.json`](index/bds_blacklist.json) — **permanent** list of companies confirmed as BDS targets (`symbol`, `company`, `date` first flagged); these are excluded forever and never re-screened
- `index/snapshots/YYYY-MM.csv` — one dated snapshot of the full 500-name list and weights per calendar month, for historical/point-in-time reference
- [`reports/event_log.csv`](reports/event_log.csv) — **permanent, append-only** log of every event: `INDEX_ADDED`, `INDEX_REMOVED`, `STATUS_CHANGE`, `GRADE_CHANGE`, `BDS_CHANGE` (columns: `Date, Symbol, Company, EventType, OldValue, NewValue, Reason`)
- [`reports/change_log.md`](reports/change_log.md) — human-readable, rolling 30 trading days of additions, removals, and warnings
- [`reports/sharia_progress.md`](reports/sharia_progress.md) — status of the monthly Sharia sweep (grade and last-checked date per symbol)

---

## Run your own copy

You can fork this repository and run the same direct index against your own Alpaca account. The whole pipeline runs on GitHub Actions' free tier; the only external state is the database cache, which Actions persists between runs.

### 1. Prerequisites

Create accounts and gather API keys for each service:

| Service | Purpose | Notes |
|---|---|---|
| [Alpaca](https://alpaca.markets) | Brokerage / order execution | Use a **dedicated account**. Start with a paper account (`ALPACA_PAPER=true`) before going live. Fractional/notional trading must be enabled. |
| [Zoya](https://zoya.finance) | Sharia compliance verdicts | Rate limited to 10 requests/second; no published daily call cap. Check Zoya's current API terms and pricing. |
| [Anthropic API](https://www.anthropic.com) | BDS classification (Claude Opus 5.5 + web search) | Grounded, batched, re-screened quarterly. Has a per-call cost; budget on the order of tens of dollars per quarter. |

Prefer a different Sharia data source? See [Using HalalScreener instead of Zoya](#using-halalscreener-instead-of-zoya).

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
| `ZOYA_API_KEY` | Zoya API key |
| `ANTHROPIC_API_KEY` | Anthropic API key (BDS classification — the Claude model set by `BDS_MODEL`, plus web search) |

**Variables** (Settings → Variables → Actions → *New repository variable*):

| Variable | Default | Description |
|---|---|---|
| `ALPACA_PAPER` | `true` | Selects the Alpaca endpoint: `true` = paper, `false` = **live**. Flip to `false` only when you are ready to trade real money. Every run logs the resolved `Trading mode: PAPER/LIVE`. |
| `BDS_MODEL` | `claude-opus-5-5` | Anthropic model ID for the BDS classifier. Any current Claude model ID works (for example, a cheaper tier to reduce cost). |

> The workflows have `permissions: contents: write` so the constituent scan can commit updated artifacts back to the repo. No further token setup is required — the default `GITHUB_TOKEN` is used.

### 4. Workflow schedules

Schedules live in `.github/workflows/`. Adjust the cron expressions for your timezone if needed (crons are in **UTC**):

| Workflow | Schedule | Purpose |
|---|---|---|
| `constituent_scan.yml` | `0 13 * * *` (daily, 09:00 ET) | Rebuild the index, run compliance checks, force-sell exclusions. Runs every day including weekends by design (see [Why the scan runs every day](#why-the-scan-runs-every-day)). |
| `daily_invest.yml` | `35 13 * * 1-5` (weekdays, 09:35 ET) | Deploy available cash into the most underweight holdings. Weekdays only, since it places live buy orders. |
| `quarterly_rebalance.yml` | `0 14 22-26 3,6,9,12 *` (days 22–26 of Mar/Jun/Sep/Dec) | Trim overweight holdings back to target after S&P reconstitution. A market-open guard skips weekend/holiday firings. |
| `initial_buy.yml` | manual only | One-time seeding of the portfolio across all `ACTIVE` names at target weight. |
| `liquidate.yml` | manual only | Sell **everything** and cancel open orders. Guarded: you must type `LIQUIDATE` to confirm. Pair with Initial Buy to reset the portfolio. |

Every workflow can also be triggered manually from the **Actions** tab (`workflow_dispatch`).

### 5. First run

1. From the **Actions** tab, manually run **Constituent Scan**. The first run screens the entire ~1,000-name universe in one pass, which can take a while depending on Zoya and yfinance response times. Track progress in `reports/sharia_progress.md`.
2. Once the scan completes, `index/constituents.csv` is populated with `ACTIVE` constituents and target weights.
3. Run **Initial Buy** once to seed the portfolio, or simply let the **Daily Investment** workflow deploy cash into the most underweight `ACTIVE` names on its next weekday run.

### 6. Run locally (optional)

```bash
pip install -r requirements.txt

# Export the same secrets the workflows use
export ALPACA_INDEX_API_KEY=...
export ALPACA_INDEX_API_SECRET=...
export ZOYA_API_KEY=...
export ANTHROPIC_API_KEY=...
export ALPACA_PAPER=true           # keep paper trading while testing (false = live endpoint)
export BDS_MODEL=claude-opus-5-5   # optional: choose the BDS classifier model

python init_db.py            # create index_fund.db (also auto-created by the scripts)
python constituent_scan.py   # rebuild constituents / refresh compliance
python daily_invest.py       # deploy available cash
```

The SQLite database (`index_fund.db`) is gitignored locally and persisted between Actions runs via `actions/cache`. Losing the cache is harmless: the next scan rebuilds it.

---

## Using HalalScreener instead of Zoya

Zoya is the default Sharia data source, but the screening step is isolated in a single function (`check_sharia()` in `constituent_scan.py`), so you can swap in another provider. [HalalScreener](https://halalscreener.app) is a natural alternative because it returns a letter grade (`A+` to `F`) directly, which means you would not need the yfinance grading step.

> **Warning: HalalScreener has had accuracy problems.** It has marked some companies as Halal that should not be. For example, **Netflix (`NFLX`) has been marked Halal when it should not be.** If you use it, do not treat its output as authoritative: spot-check holdings (especially large positions and media, financial, and entertainment names) against a second source, and consider adding names you know to be non-compliant to a manual override. Zoya and every other provider can also be wrong; this warning is simply about errors that have actually been observed.

**You will need to rework the pipeline for HalalScreener's free-tier limits.** The free tier allows roughly **10 requests per minute and about 100 requests per day**, whereas the default pipeline assumes it can check the whole ~1,000-name universe in one run. To use the free tier you must:

1. **Replace `check_sharia()`** with a call to `GET https://halalscreener.app/api/v1/screen?symbol=TICKER` using an `Authorization: Bearer <key>` header. The response includes `grade` and `status` fields, which map directly to what the pipeline already stores.
2. **Throttle requests** to stay under the per-minute limit (about one call every 6 seconds), and retry with backoff on HTTP `429`.
3. **Cap each run at ~99 symbols** and spread the monthly sweep across ~10 days. Process the oldest-checked names first and let the rest keep their cached grade until their turn comes.
4. **Handle a cold start.** On an empty database nothing has a cached grade, so the first full baseline takes roughly ten days of daily runs. Decide how the index should behave during that window (for example, build the index only from names that have been graded so far).
5. **Update the workflow** (`.github/workflows/constituent_scan.yml`) to pass your HalalScreener key as a secret instead of `ZOYA_API_KEY`, and remove the now-unused `zoya_client.py` / `yfinance_grading.py` imports.

If you upgrade to a paid HalalScreener plan with a higher daily limit, steps 3 and 4 may be unnecessary; check their current plans. For a working reference implementation of the free-tier flow (throttling, daily cap, deferred queue, and cold-start handling), see the last version of `constituent_scan.py` that used HalalScreener:

```bash
git show ff2f069:constituent_scan.py
```

---

## Tunable parameters

Key constants near the top of the scripts:

| Constant | File | Default | Meaning |
|---|---|---|---|
| `MAX_INDEX_SIZE` | `constituent_scan.py` | `500` | Target number of constituents |
| `SHARIA_RATE_LIMIT_S` | `constituent_scan.py` | `0.3` | Seconds between Sharia API calls (polite pacing; Zoya allows up to 10 requests/second) |
| `BDS_REFRESH_MONTHS` | `constituent_scan.py` | `{3,6,9,12}` | Calendar months the quarterly BDS web-search re-screen runs (Sharia re-screens monthly via a calendar sweep — no constant) |
| `BDS_MODEL` | `constituent_scan.py` | `claude-opus-5-5` | Anthropic model for the BDS classifier (overridable via the `BDS_MODEL` env/repo variable) |
| `BDS_BACKFILL_BUFFER` | `constituent_scan.py` | `25` | Extra Russell 1000 backfill candidates screened beyond the exact shortfall, so names that come back targeted don't leave the index short |
| `MIN_CASH` | `daily_invest.py` | `20.0` | Skip the daily buy if account cash is below this |
| `MIN_NOTIONAL` | `daily_invest.py` | `1.0` | Minimum dollar amount per order |
| `TOP_N_GAPS` | `daily_invest.py` | `20` | How many of the most-underweight names to buy each day |

## Repository layout

```
.
├── .github/workflows/
│   ├── constituent_scan.yml     # daily scan / rebuild
│   ├── daily_invest.yml         # weekday cash deployment
│   ├── quarterly_rebalance.yml  # quarterly trim of overweight holdings
│   ├── initial_buy.yml          # one-time portfolio seeding (manual)
│   └── liquidate.yml            # guarded full liquidation (manual)
├── constituent_scan.py          # constituent rebuild + compliance checks
├── zoya_client.py               # Zoya GraphQL client (Sharia compliance verdicts)
├── yfinance_grading.py          # financial-ratio letter grading for Zoya-compliant names
├── daily_invest.py              # underweight-gap cash deployment
├── quarterly_rebalance.py       # sell-only trim of overweight positions
├── initial_buy.py               # one-time initial portfolio purchase
├── liquidate.py                 # full liquidation helper
├── init_db.py                   # SQLite schema bootstrap
├── requirements.txt             # Python dependencies
├── index/
│   ├── constituents.csv         # public: strict 500-name index composition
│   ├── bds_blacklist.json       # public: permanent list of confirmed BDS targets
│   └── snapshots/               # public: one dated weights snapshot per month
└── reports/                     # public: event_log.csv, change_log.md, sharia_progress.md
```

See [`HalalBDSSP500PRD.md`](HalalBDSSP500PRD.md) for the product requirements and design notes.
