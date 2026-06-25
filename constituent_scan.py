"""
Constituent scan for Halal-BDS-SP500.

Steps:
  1. Fetch S&P 500 symbols from Wikipedia
  2. Fetch Russell 1000 symbols → build replacement pool (R1000 - SP500)
  3. Fetch market caps via yfinance
  4. Sharia compliance check via HalalScreener API (monthly calendar sweep, ≤99/day)
  5. BDS compliance check via Claude Opus + web search (Message Batches API, ~quarterly),
     scoped to the ~500 index names (S&P 500 + on-demand backfill); confirmed targets are
     blacklisted permanently and never re-screened
  6. Apply state machine → ACTIVE / WARNED / REMOVED
  7. Build 500-name list (backfill from R1000 replacement pool if needed)
  8. Compute market-cap target weights
  9. Execute forced sells via Alpaca for REMOVED holdings
 10. Persist to DB; write public artifacts; generate change_log entries
"""

import csv
import io
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

import anthropic
import pandas as pd
import requests
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from init_db import init_db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = "index_fund.db"
INDEX_CSV = "index/constituents.csv"
CHANGE_LOG_MD = "reports/change_log.md"
EVENT_LOG_CSV = "reports/event_log.csv"   # append-only, permanent history of all events
SNAPSHOT_DIR = "index/snapshots"          # one dated weights snapshot per month

HALALSCREENER_BASE = "https://halalscreener.app/api/v1/screen"
HALALSCREENER_KEY = os.environ["HALALSCREENER_API_KEY"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ALPACA_KEY = os.environ["ALPACA_INDEX_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_INDEX_API_SECRET"]
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

GRADE_RANK = {
    "A+": 9, "A": 8, "A-": 7,
    "B+": 6, "B": 5, "B-": 4,
    "C+": 3, "C": 2, "C-": 1,
    "D": 0, "F": -1,
}

MAX_INDEX_SIZE = 500
SHARIA_RATE_LIMIT_S = 6.0   # 10 requests/minute free tier limit
SHARIA_DAILY_CAP = 99       # one under the ~100/day HalalScreener free-tier limit
SHARIA_MAX_RETRIES = 3

# BDS screening: web-search-grounded classification via the Message Batches API,
# refreshed once per calendar quarter (aligned to S&P reconstitution: Mar/Jun/Sep/Dec).
# The scan runs daily, so a committed marker file (BDS_STATE_FILE) records the last
# refresh; the screen only re-runs in a quarter-end month it hasn't already covered.
# Off-cycle runs only screen brand-new symbols and carry the rest forward.
BDS_MODEL = os.environ.get("BDS_MODEL", "claude-opus-4-8")
BDS_REFRESH_MONTHS = {3, 6, 9, 12}
BDS_BATCH_POLL_S = 30
BDS_BATCH_MAX_WAIT = 3 * 60 * 60  # seconds to wait for the batch before carrying forward
BDS_STATE_FILE = "index/bds_state.json"
# A confirmed BDS target (verdict NO) is blacklisted permanently — never re-screened, never
# re-admitted. The committed JSON file is the durable source of truth (survives Actions cache
# loss); the bds_blacklist DB table mirrors it.
BDS_BLACKLIST_FILE = "index/bds_blacklist.json"
# Quarterly screening is scoped to roughly the 500 index names: the S&P 500 plus only enough
# Russell 1000 backfill candidates to fill vacated slots, screened highest-market-cap first.
# The buffer over-screens slightly so names that come back targeted don't leave the index short.
BDS_BACKFILL_BUFFER = 25
BDS_BACKFILL_MAX_ROUNDS = 3
BDS_SYSTEM = (
    "You are a compliance analyst determining whether a publicly traded company is a "
    "current, explicit target of the BDS (Boycott, Divestment, Sanctions) movement "
    "against Israel's occupation of Palestinian territories.\n\n"
    "Use the web_search tool to consult authoritative, current sources before deciding "
    "— for example the official BDS movement (bdsmovement.net) campaigns, the AFSC "
    "Investigate project (investigate.afsc.org), and reputable news coverage. "
    "Distinguish a company that is an explicit BDS/divestment target (named in a boycott "
    "or divestment campaign) from one merely criticized or discussed.\n\n"
    "Decide one of:\n"
    "- TARGETED: the company is an explicit, current target of a BDS boycott or "
    "divestment campaign.\n"
    "- NOT_TARGETED: no credible evidence the company is a current BDS target.\n"
    "- UNKNOWN: genuinely indeterminate after searching.\n\n"
    "End your response with exactly one line containing your verdict:\n"
    "VERDICT: TARGETED  (or)  VERDICT: NOT_TARGETED  (or)  VERDICT: UNKNOWN"
)

TODAY = date.today().isoformat()

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

WIKI_HEADERS = {
    "User-Agent": "Halal-BDS-SP500-bot/1.0 (https://github.com/ohassen/Halal-BDS-SP500; automated index fund)"
}


def _wiki_tables(url: str) -> list:
    """Fetch Wikipedia page with a bot User-Agent and parse HTML tables."""
    resp = requests.get(url, headers=WIKI_HEADERS, timeout=30)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def fetch_sp500() -> dict[str, str]:
    """Returns {symbol: company_name} for S&P 500 from Wikipedia."""
    try:
        df = _wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        df["Symbol"] = df["Symbol"].str.replace(".", "-", regex=False)
        return dict(zip(df["Symbol"], df["Security"]))
    except Exception as e:
        raise RuntimeError(f"Failed to fetch S&P 500 from Wikipedia: {e}") from e


def fetch_russell1000() -> set[str]:
    """Returns set of R1000 symbols from Wikipedia."""
    try:
        tables = _wiki_tables("https://en.wikipedia.org/wiki/Russell_1000_Index")
        for df in tables:
            cols = [str(c).lower() for c in df.columns]
            for col_name in ("ticker", "symbol"):
                if col_name in cols:
                    idx = cols.index(col_name)
                    return set(df.iloc[:, idx].astype(str).str.replace(".", "-", regex=False).dropna())
        raise ValueError("No Ticker/Symbol column found in any R1000 Wikipedia table")
    except Exception as e:
        print(f"WARNING: Failed to fetch Russell 1000 ({e}). Replacement pool will be empty.")
        return set()


def fetch_market_caps(symbols: list[str]) -> dict[str, float]:
    """Batch-fetch market caps via yfinance. Returns {symbol: market_cap}."""
    caps = {}
    # yfinance Tickers handles batching internally
    tickers = yf.Tickers(" ".join(symbols))
    for sym in symbols:
        try:
            info = tickers.tickers[sym].info
            caps[sym] = float(info.get("marketCap") or 0)
        except Exception:
            caps[sym] = 0.0
    return caps


# ---------------------------------------------------------------------------
# Compliance checks
# ---------------------------------------------------------------------------

def check_sharia(symbol: str) -> tuple[str, str]:
    """
    Query HalalScreener for one symbol.
    Returns (grade, status) or ("UNKNOWN", "UNKNOWN") on failure.
    """
    headers = {"Authorization": f"Bearer {HALALSCREENER_KEY}"}
    for attempt in range(SHARIA_MAX_RETRIES):
        try:
            resp = requests.get(
                HALALSCREENER_BASE,
                params={"symbol": symbol},
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 429:
                wait = 2 ** attempt * 2
                print(f"  HalalScreener 429 on {symbol}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code == 200:
                data = resp.json()
                grade = data.get("grade", "UNKNOWN")
                status = data.get("status", "UNKNOWN")
                return grade, status
            print(f"  HalalScreener HTTP {resp.status_code} for {symbol}")
            return "UNKNOWN", "UNKNOWN"
        except requests.RequestException as e:
            print(f"  HalalScreener error for {symbol}: {e}")
            if attempt < SHARIA_MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    return "UNKNOWN", "UNKNOWN"


_VERDICT_RE = re.compile(r"VERDICT:\s*(TARGETED|NOT_TARGETED|UNKNOWN)", re.IGNORECASE)
# Model-facing verdicts map to the stored BDS codes (YES = bds-friendly / not targeted).
_VERDICT_TO_BDS = {"TARGETED": "NO", "NOT_TARGETED": "YES", "UNKNOWN": "UNKNOWN"}


def _parse_bds_verdict(message) -> str:
    """Map a model response (which includes web-search blocks) to a stored BDS code."""
    text = " ".join(
        b.text for b in message.content if getattr(b, "type", None) == "text"
    )
    matches = _VERDICT_RE.findall(text)
    if not matches:
        return "UNKNOWN"
    return _VERDICT_TO_BDS.get(matches[-1].upper(), "UNKNOWN")


def check_bds_web_batch(targets: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Classify BDS status for each {symbol: company} via Opus + web search.

    Submits one grounded request per symbol through the Message Batches API (50%
    cheaper, asynchronous) and returns ({symbol: "YES"|"NO"|"UNKNOWN"}, completed).
    `completed` is False if the batch is cancelled after BDS_BATCH_MAX_WAIT, so the
    caller can carry forward cached values and retry next run rather than recording
    a quarterly refresh that never finished.
    """
    if not targets:
        return {}, True

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    requests_payload = [
        {
            "custom_id": f"bds-{sym}",
            "params": {
                "model": BDS_MODEL,
                "max_tokens": 4096,
                "system": BDS_SYSTEM,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Company: {name} (ticker {sym}). Is this company currently "
                        "targeted by the BDS movement? Search the web, then give your verdict."
                    ),
                }],
                "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
                "output_config": {"effort": "medium"},
            },
        }
        for sym, name in targets.items()
    ]

    batch = client.messages.batches.create(requests=requests_payload)
    print(f"  BDS batch {batch.id} created ({len(requests_payload)} requests); polling…")

    deadline = time.time() + BDS_BATCH_MAX_WAIT
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        if time.time() > deadline:
            print(f"  WARNING: BDS batch {batch.id} exceeded {BDS_BATCH_MAX_WAIT}s — "
                  "cancelling and carrying forward cached status.")
            try:
                client.messages.batches.cancel(batch.id)
            except Exception as e:
                print(f"  (batch cancel failed: {e})")
            return {}, False
        time.sleep(BDS_BATCH_POLL_S)

    results: dict[str, str] = {}
    for r in client.messages.batches.results(batch.id):
        sym = r.custom_id[len("bds-"):]
        if r.result.type == "succeeded":
            results[sym] = _parse_bds_verdict(r.result.message)
        else:
            print(f"  BDS request for {sym} did not succeed ({r.result.type}); UNKNOWN")
            results[sym] = "UNKNOWN"

    targeted = sum(1 for v in results.values() if v == "NO")
    unknown = sum(1 for v in results.values() if v == "UNKNOWN")
    print(f"  BDS batch complete: {len(results)} resolved — {targeted} targeted, {unknown} unknown")
    return results, True


def load_bds_from_csv() -> dict[str, str]:
    """Carry-forward source for BDS status between quarterly refreshes.

    The committed index/constituents.csv survives GitHub Actions cache loss (unlike
    the cached DB), so off-cycle runs reuse the last published verdicts.
    """
    out: dict[str, str] = {}
    if not os.path.exists(INDEX_CSV):
        return out
    with open(INDEX_CSV, newline="") as f:
        for row in csv.DictReader(f):
            sym, bds = row.get("Symbol"), row.get("BDSStatus")
            if sym and bds:
                out[sym] = bds
    return out


def _bds_last_refresh() -> "date | None":
    try:
        with open(BDS_STATE_FILE) as f:
            return date.fromisoformat(json.load(f)["last_refresh"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _write_bds_refresh(d: date) -> None:
    os.makedirs(os.path.dirname(BDS_STATE_FILE), exist_ok=True)
    with open(BDS_STATE_FILE, "w") as f:
        json.dump({"last_refresh": d.isoformat()}, f)


def load_bds_blacklist(conn: sqlite3.Connection) -> set[str]:
    """Return the set of permanently-blacklisted symbols (confirmed BDS targets).

    The committed index/bds_blacklist.json is the durable source of truth — it survives
    GitHub Actions cache loss, unlike the DB. On load we fold any file entries the DB is
    missing back into the bds_blacklist table, then return the union of symbols.
    """
    if os.path.exists(BDS_BLACKLIST_FILE):
        try:
            with open(BDS_BLACKLIST_FILE) as f:
                entries = json.load(f)
        except (json.JSONDecodeError, ValueError):
            entries = []
        for e in entries:
            sym = e.get("symbol") if isinstance(e, dict) else None
            if sym:
                conn.execute(
                    "INSERT OR IGNORE INTO bds_blacklist (symbol, company, date_flagged) "
                    "VALUES (?, ?, ?)",
                    (sym, e.get("company") or sym, e.get("date") or TODAY),
                )
        conn.commit()
    rows = conn.execute("SELECT symbol FROM bds_blacklist").fetchall()
    return {r[0] for r in rows}


def save_bds_blacklist(conn: sqlite3.Connection, entries: list[tuple[str, str]]) -> None:
    """Persist blacklisted (symbol, company) pairs to the DB and rewrite the committed file.

    Idempotent: existing symbols keep their original date_flagged (INSERT OR IGNORE); only
    genuinely new targets get today's date. The JSON file is rewritten from the full table so
    the durable file and the DB mirror never drift apart.
    """
    for sym, company in entries:
        conn.execute(
            "INSERT OR IGNORE INTO bds_blacklist (symbol, company, date_flagged) "
            "VALUES (?, ?, ?)",
            (sym, company or sym, TODAY),
        )
    conn.commit()
    rows = conn.execute(
        "SELECT symbol, company, date_flagged FROM bds_blacklist ORDER BY symbol"
    ).fetchall()
    os.makedirs(os.path.dirname(BDS_BLACKLIST_FILE), exist_ok=True)
    with open(BDS_BLACKLIST_FILE, "w") as f:
        json.dump(
            [{"symbol": r[0], "company": r[1], "date": r[2]} for r in rows],
            f, indent=2,
        )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def load_constituents(conn: sqlite3.Connection) -> dict[str, dict]:
    """Load all rows from constituents as {symbol: row_dict}."""
    rows = conn.execute("SELECT * FROM constituents").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM constituents LIMIT 0").description]
    return {r[0]: dict(zip(cols, r)) for r in rows}


def needs_sharia_check(last_checked: str | None) -> bool:
    """True if this symbol hasn't been Sharia-screened yet this calendar month.

    Drives the monthly sweep: on the 1st everything is due; once a name is
    re-checked its timestamp moves into the current month and it stops being due,
    so the screen goes dormant until the next 1st.
    """
    if not last_checked:
        return True
    try:
        return date.fromisoformat(last_checked) < date.today().replace(day=1)
    except ValueError:
        return True


def _quarter(d: date) -> tuple[int, int]:
    """(year, quarter-index 0-3) — gates the BDS screen to once per calendar quarter."""
    return (d.year, (d.month - 1) // 3)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def _grade_allows_active(grade: str) -> bool:
    """True if the Sharia grade alone permits ACTIVE (mirrors classify_symbol's ACTIVE
    branches): grade >= B-, or an unknown grade. D / C-tier -> WARNED; F -> REMOVED.

    Used to pre-filter Russell 1000 backfill candidates before BDS screening, so we only
    spend web-search calls on names that could actually enter the index.
    """
    r = GRADE_RANK.get(grade)
    return r is None or r >= GRADE_RANK["B-"]


def classify_symbol(sharia_grade: str, bds_status: str) -> tuple[str, str | None]:
    """
    Returns (index_status, warning_reason).
    index_status: ACTIVE | WARNED | REMOVED
    """
    grade_rank = GRADE_RANK.get(sharia_grade, None)

    # Force sell conditions
    if sharia_grade == "F":
        return "REMOVED", "sharia_F"
    if bds_status == "NO":
        return "REMOVED", "bds_NO"

    # Warning condition
    if sharia_grade == "D":
        return "WARNED", "sharia_D"

    # BDS: only a confirmed target (bds_status == "NO", force-sold above) is
    # excluded. UNKNOWN is the normal state for the vast majority of companies —
    # they simply aren't named in any BDS campaign — so it is treated as eligible,
    # exactly like a confirmed "not targeted" (YES). UNKNOWN is never a warning.
    if grade_rank is not None and grade_rank >= GRADE_RANK["B-"]:
        return "ACTIVE", None

    # Grade below B- but not D or F (i.e. C+, C, C-) — not eligible for entry
    # but we still hold with a warning rather than force-selling
    if grade_rank is not None and grade_rank < GRADE_RANK["B-"] and grade_rank > GRADE_RANK["D"]:
        return "WARNED", "sharia_below_B-"

    # Unknown grade — treat as compliant
    return "ACTIVE", None


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

PROGRESS_MD = "reports/sharia_progress.md"
GRADE_EMOJI = {"A+": "✅", "A": "✅", "A-": "✅", "B+": "⚠️", "B": "⚠️", "B-": "⚠️",
               "C": "❌", "D": "❌", "F": "❌", "NOT_COVERED": "❓", "UNKNOWN": "❓"}


def _write_sharia_progress(conn: sqlite3.Connection, checked_today: int, remaining: int) -> None:
    rows = conn.execute(
        "SELECT symbol, sharia_grade, last_checked FROM constituents "
        "WHERE last_checked IS NOT NULL ORDER BY symbol"
    ).fetchall()
    total = len(rows) + remaining
    os.makedirs("reports", exist_ok=True)
    with open(PROGRESS_MD, "w") as f:
        f.write(f"# Sharia Screening Progress\n\n")
        f.write(f"**{len(rows)} / {total} symbols screened** — {remaining} remaining\n\n")
        f.write(f"_Last updated: {TODAY} (+{checked_today} today)_\n\n")
        f.write("| Symbol | Grade | Checked |\n")
        f.write("|--------|-------|--------|\n")
        for sym, grade, checked in rows:
            emoji = GRADE_EMOJI.get(grade, "❓")
            f.write(f"| {sym} | {emoji} {grade} | {checked} |\n")
    print(f"  Progress report written: {len(rows)}/{total} symbols → {PROGRESS_MD}")


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run() -> None:
    os.makedirs("index", exist_ok=True)
    os.makedirs("reports", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)

    print("=== Step 1: Fetch S&P 500 ===")
    sp500 = fetch_sp500()
    sp500_syms = list(sp500.keys())
    print(f"  {len(sp500_syms)} S&P 500 symbols")

    print("=== Step 2: Fetch Russell 1000 replacement pool ===")
    r1000_syms = fetch_russell1000()
    replacement_pool_syms = sorted(r1000_syms - set(sp500_syms))
    print(f"  {len(replacement_pool_syms)} replacement candidates")

    all_syms = sp500_syms + replacement_pool_syms

    print("=== Step 3: Fetch market caps ===")
    market_caps = fetch_market_caps(all_syms)
    print(f"  Fetched caps for {sum(1 for v in market_caps.values() if v > 0)} symbols")

    print("=== Step 4: Sharia compliance check ===")
    existing = load_constituents(conn)
    sharia_results: dict[str, tuple[str, str]] = {}

    # Sharia is screened as a monthly calendar sweep: on the 1st of each month the
    # whole universe is due; we re-check up to SHARIA_DAILY_CAP/day (free-tier limit)
    # over the following ~10 days, then go dormant until the next 1st.
    # Priority: never-checked first, then oldest last_checked first.
    due_syms = []
    for sym in all_syms:
        row = existing.get(sym)
        last_checked = row["last_checked"] if row else None
        if needs_sharia_check(last_checked):
            due_syms.append((last_checked or "", sym))
        else:
            sharia_results[sym] = (row["sharia_grade"], "cached")

    due_syms.sort()  # None/"" sorts first → never-checked get priority
    to_check = [sym for _, sym in due_syms[:SHARIA_DAILY_CAP]]
    deferred = [sym for _, sym in due_syms[SHARIA_DAILY_CAP:]]

    print(f"  {len(sharia_results)} fresh this month, {len(to_check)} to check, "
          f"{len(deferred)} deferred to a later run")

    # Carry forward the existing grade for deferred symbols (still valid until their
    # monthly slot comes up); UNKNOWN only if there is no grade on record at all.
    for sym in deferred:
        row = existing.get(sym)
        sharia_results[sym] = (row["sharia_grade"] if row else "UNKNOWN", "deferred")

    for i, sym in enumerate(to_check, 1):
        grade, status = check_sharia(sym)
        sharia_results[sym] = (grade, status)
        print(f"  [{i}/{len(to_check)}] {sym}: {grade}")
        if i < len(to_check):
            time.sleep(SHARIA_RATE_LIMIT_S)

    print(f"  Checked/cached {len(sharia_results)} symbols")

    # Persist freshly-checked grades immediately so later runs skip them this month
    for sym, (grade, source) in sharia_results.items():
        if source not in ("cached", "deferred"):
            conn.execute(
                """INSERT INTO constituents (symbol, sharia_grade, last_checked)
                   VALUES (?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     sharia_grade=excluded.sharia_grade,
                     last_checked=excluded.last_checked""",
                (sym, grade, TODAY),
            )
    conn.commit()

    # Cold-start guard: defer the full rebuild ONLY while some name has never been
    # graded at all (empty DB / cache loss / brand-new ticker). In steady state every
    # name carries a prior grade, so the rebuild + force-sells + BDS + CSV commit run
    # daily even mid-sweep, using cached grades for names not yet re-checked this month.
    checked_now = set(to_check)
    ungraded_remaining = [
        sym for sym in all_syms
        if not (existing.get(sym) or {}).get("last_checked") and sym not in checked_now
    ]
    if ungraded_remaining:
        _write_sharia_progress(conn, len(to_check), len(ungraded_remaining))
        conn.close()
        print(
            f"Cold start: {len(to_check)} graded this run, {len(ungraded_remaining)} "
            "still ungraded. Deferring rebuild until the universe has baseline grades; "
            "re-running next scan."
        )
        return

    print("=== Step 5: BDS compliance check (scoped to index needs) ===")
    # Seed from cache (committed CSV first — survives Actions cache loss — then DB) so
    # symbols not re-checked this run carry forward their last verdict.
    bds_results: dict[str, str] = {}
    csv_bds = load_bds_from_csv()
    for sym in all_syms:
        cached = (existing.get(sym) or {}).get("bds_status") or csv_bds.get(sym)
        if cached:
            bds_results[sym] = cached

    # Permanent blacklist: any company ever confirmed targeted (BDS == NO) is excluded
    # forever — never re-screened, never re-admitted. Fold in any pre-existing NO carried in
    # the cache, force NO, and exclude these from every screen target set below.
    blacklist = load_bds_blacklist(conn)
    blacklist.update(s for s in all_syms if bds_results.get(s) == "NO")
    for sym in blacklist:
        bds_results[sym] = "NO"
    print(f"  {len(blacklist)} symbol(s) permanently blacklisted (forced NO, never re-screened)")

    def _grade(s: str) -> str:
        return sharia_results.get(s, ("UNKNOWN", ""))[0]

    last_refresh = _bds_last_refresh()
    due = last_refresh is None or (
        date.today().month in BDS_REFRESH_MONTHS
        and _quarter(last_refresh) != _quarter(date.today())
    )
    completed = True  # every batch we ran this quarter finished (gates the refresh marker)

    if not ANTHROPIC_API_KEY:
        print("  WARNING: ANTHROPIC_API_KEY not set — skipping web BDS check, "
              "carrying forward cached status (blacklist still enforced).")
    elif due:
        # Phase A — re-screen the S&P 500 names that can be index members: not blacklisted
        # and not Sharia-F (an F is excluded regardless of BDS). This is last quarter's
        # passers plus any new S&P members; permanent failures are skipped entirely.
        sp_targets = {
            s: sp500.get(s, s) for s in sp500_syms
            if s not in blacklist and _grade(s) != "F"
        }
        print(f"  Quarterly BDS refresh due (last refresh: {last_refresh}) — Phase A: "
              f"web-checking {len(sp_targets)} S&P name(s) via {BDS_MODEL} batch")
        fresh, ok = check_bds_web_batch(sp_targets)
        bds_results.update(fresh)
        completed &= ok
        blacklist.update(s for s, v in fresh.items() if v == "NO")

        # Size the backfill from the provisional S&P index (ACTIVE + WARNED count toward 500).
        sp_members = [
            s for s in sp500_syms
            if market_caps.get(s, 0) > 0
            and classify_symbol(_grade(s), bds_results.get(s, "UNKNOWN"))[0] in ("ACTIVE", "WARNED")
        ]
        shortfall = MAX_INDEX_SIZE - min(len(sp_members), MAX_INDEX_SIZE)
        print(f"  S&P yields {len(sp_members)} index member(s); backfill shortfall: {shortfall}")

        # Phase B — screen only enough Russell 1000 candidates (highest market cap first,
        # grade-eligible, non-blacklisted) to cover the shortfall, plus a buffer for names
        # that come back targeted. Lower-cap pool names are never screened.
        if shortfall > 0:
            pool = sorted(
                (s for s in replacement_pool_syms
                 if s not in blacklist and market_caps.get(s, 0) > 0
                 and _grade_allows_active(_grade(s))),
                key=lambda s: market_caps.get(s, 0), reverse=True,
            )
            cursor = 0
            for _ in range(BDS_BACKFILL_MAX_ROUNDS):
                ok_count = sum(
                    1 for s in pool[:cursor]
                    if bds_results.get(s) and bds_results.get(s) != "NO"
                )
                if ok_count >= shortfall or cursor >= len(pool):
                    break
                want = (shortfall - ok_count) + BDS_BACKFILL_BUFFER
                tranche = {
                    s: sp500.get(s, s) for s in pool[cursor:cursor + want]
                    if not bds_results.get(s)
                }
                cursor += want
                if not tranche:
                    continue
                print(f"  Phase B: web-checking {len(tranche)} backfill candidate(s)")
                fresh, ok = check_bds_web_batch(tranche)
                bds_results.update(fresh)
                completed &= ok
                blacklist.update(s for s, v in fresh.items() if v == "NO")

        if completed:
            _write_bds_refresh(date.today())
    else:
        # Off-cycle: only screen brand-new index-relevant names with no verdict yet (e.g. a
        # new S&P member added mid-quarter). Everything else carries forward unchanged.
        targets = {
            s: sp500.get(s, s) for s in sp500_syms
            if s not in blacklist and _grade(s) != "F" and not bds_results.get(s)
        }
        if targets:
            print(f"  BDS refresh not due (last refresh: {last_refresh}); web-checking "
                  f"{len(targets)} new index name(s), carrying the rest forward")
            fresh, _ = check_bds_web_batch(targets)
            bds_results.update(fresh)
            blacklist.update(s for s, v in fresh.items() if v == "NO")
        else:
            print(f"  BDS refresh not due (last refresh: {last_refresh}); nothing new to screen")

    # Persist the permanent blacklist (newly-confirmed targets + any folded-in cached NOs)
    # to the committed file and the DB mirror.
    all_no = sorted(s for s, v in bds_results.items() if v == "NO")
    if all_no:
        save_bds_blacklist(conn, [(s, sp500.get(s, s)) for s in all_no])

    print(f"  BDS status resolved for {len(bds_results)} symbols; "
          f"{len(all_no)} permanently blacklisted")

    print("=== Step 6: Apply state machine ===")
    classifications: dict[str, tuple[str, str | None]] = {}
    for sym in all_syms:
        grade = sharia_results.get(sym, ("UNKNOWN", "UNKNOWN"))[0]
        bds = bds_results.get(sym, "UNKNOWN")
        status, reason = classify_symbol(grade, bds)
        classifications[sym] = (status, reason)

    print("=== Step 7: Build strict 500-name list ===")
    # Held S&P 500 members come first: ACTIVE (buy-eligible) and WARNED (held, no new
    # buys) both count toward the 500. Sort by market cap so that, in the rare case the
    # held S&P set exceeds 500, we keep the largest names.
    sp500_members = [
        s for s in sp500_syms
        if classifications[s][0] in ("ACTIVE", "WARNED") and market_caps.get(s, 0) > 0
    ]
    sp500_members.sort(key=lambda s: market_caps.get(s, 0), reverse=True)
    index_syms = sp500_members[:MAX_INDEX_SIZE]

    # Backfill any remaining slots from the Russell 1000 replacement pool (ACTIVE only).
    if len(index_syms) < MAX_INDEX_SIZE:
        pool_active = [
            s for s in replacement_pool_syms
            if classifications[s][0] == "ACTIVE" and market_caps.get(s, 0) > 0
        ]
        pool_active.sort(key=lambda s: market_caps.get(s, 0), reverse=True)
        needed = MAX_INDEX_SIZE - len(index_syms)
        index_syms.extend(pool_active[:needed])

    index_set = set(index_syms)
    print(f"  Index size: {len(index_syms)} (target: {MAX_INDEX_SIZE})")

    print("=== Step 8: Compute target weights ===")
    # Weights are computed over exactly the 500 index members (ACTIVE + WARNED) and sum
    # to 1.0. WARNED names hold their weight; daily_invest only deploys cash into ACTIVE.
    total_cap = sum(market_caps.get(s, 0) for s in index_syms)
    if total_cap == 0:
        raise RuntimeError("Total market cap is 0 — cannot compute weights")

    target_weights: dict[str, float] = {
        sym: market_caps.get(sym, 0) / total_cap for sym in index_syms
    }

    weight_sum = sum(target_weights.values())
    assert abs(weight_sum - 1.0) < 1e-6, f"Weights sum to {weight_sum}, expected 1.0"
    print(f"  Weights computed, sum={weight_sum:.8f}")

    print("=== Step 9: Execute forced sells ===")
    alpaca = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
    print(f"Trading mode: {'PAPER' if ALPACA_PAPER else 'LIVE'}")
    positions = {p.symbol: p for p in alpaca.get_all_positions()}

    removed_syms = [
        s for s in all_syms
        if classifications[s][0] == "REMOVED" and s in positions
    ]
    print(f"  Force-selling {len(removed_syms)} symbol(s): {removed_syms}")

    for sym in removed_syms:
        try:
            order = alpaca.submit_order(
                MarketOrderRequest(
                    symbol=sym,
                    qty=float(positions[sym].qty),
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            conn.execute(
                """INSERT INTO transactions
                   (transaction_date, symbol, action, quantity, alpaca_order_id, status, reason)
                   VALUES (?, ?, 'SELL', ?, ?, ?, ?)""",
                (
                    TODAY, sym,
                    float(positions[sym].qty),
                    str(order.id),
                    str(order.status),
                    classifications[sym][1],
                ),
            )
            print(f"  Sold {sym}: order {order.id}")
        except Exception as e:
            print(f"  ERROR selling {sym}: {e}")
            conn.execute(
                """INSERT INTO transactions
                   (transaction_date, symbol, action, status, reason)
                   VALUES (?, ?, 'SELL', 'FAILED', ?)""",
                (TODAY, sym, str(e)),
            )

    print("=== Step 10: Persist to DB + generate reports ===")
    change_events: list[tuple] = []
    # Permanent event log rows: (date, symbol, company, event_type, old_value, new_value, reason)
    event_rows: list[tuple] = []

    for sym in all_syms:
        grade, _ = sharia_results.get(sym, ("UNKNOWN", "UNKNOWN"))
        bds = bds_results.get(sym, "UNKNOWN")
        status, reason = classifications[sym]
        cap = market_caps.get(sym, 0)
        weight = target_weights.get(sym, 0.0)
        company = sp500.get(sym, sym)

        old_row = existing.get(sym)
        old_status = old_row["index_status"] if old_row else None
        old_grade = old_row["sharia_grade"] if old_row else None
        old_bds = old_row["bds_status"] if old_row else None
        old_member = bool(old_row) and (old_row.get("target_weight") or 0) > 0
        new_member = sym in index_set

        # Determine event type for the rolling change log (human-readable, 30-day view)
        if status == "REMOVED" and old_status != "REMOVED":
            event = "REMOVED"
            change_events.append((TODAY, sym, event, old_grade, grade, bds, reason))
        elif status == "WARNED" and old_status not in ("WARNED",):
            event = "WARNED"
            change_events.append((TODAY, sym, event, old_grade, grade, bds, reason))
        elif status == "ACTIVE" and old_status in ("WARNED", "REMOVED", None):
            event = "ADDED" if old_status is None else "WARNING_CLEARED"
            change_events.append((TODAY, sym, event, old_grade, grade, bds, reason))

        # Permanent event log: index membership, status, grade and BDS transitions
        if new_member and not old_member:
            event_rows.append((TODAY, sym, company, "INDEX_ADDED", "", status, reason or ""))
        elif old_member and not new_member:
            event_rows.append((TODAY, sym, company, "INDEX_REMOVED", old_status or "", status, reason or ""))
        if old_status and old_status != status:
            event_rows.append((TODAY, sym, company, "STATUS_CHANGE", old_status, status, reason or ""))
        if old_grade and grade != "UNKNOWN" and old_grade != grade:
            event_rows.append((TODAY, sym, company, "GRADE_CHANGE", old_grade, grade, ""))
        if old_bds and bds != "UNKNOWN" and old_bds != bds:
            event_rows.append((TODAY, sym, company, "BDS_CHANGE", old_bds, bds, ""))

        removed_date = TODAY if status == "REMOVED" else (old_row["removed_date"] if old_row else None)
        added_date = (old_row["added_date"] if old_row else None) or (TODAY if status != "REMOVED" else None)

        conn.execute(
            """INSERT INTO constituents
               (symbol, company_name, market_cap, target_weight, sharia_grade, bds_status,
                index_status, warning_reason, added_date, removed_date, last_checked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 company_name=excluded.company_name,
                 market_cap=excluded.market_cap,
                 target_weight=excluded.target_weight,
                 sharia_grade=excluded.sharia_grade,
                 bds_status=excluded.bds_status,
                 index_status=excluded.index_status,
                 warning_reason=excluded.warning_reason,
                 removed_date=excluded.removed_date,
                 last_checked=excluded.last_checked""",
            (sym, company, cap, weight, grade, bds, status, reason, added_date, removed_date, TODAY),
        )

        # Compliance history
        sharia_status_str = (
            "NON_COMPLIANT" if grade == "F"
            else "COMPLIANT" if GRADE_RANK.get(grade, -2) >= GRADE_RANK["B-"]
            else "UNKNOWN"
        )
        conn.execute(
            """INSERT OR IGNORE INTO compliance_history
               (check_date, symbol, sharia_grade, sharia_status, bds_status)
               VALUES (?, ?, ?, ?, ?)""",
            (TODAY, sym, grade, sharia_status_str, bds),
        )

    # Insert change log entries
    for event in change_events:
        conn.execute(
            """INSERT INTO change_log (log_date, symbol, event_type, old_grade, new_grade, bds_status, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            event,
        )

    conn.commit()

    # --- Public artifacts ---
    # constituents.csv is the strict index membership: exactly the symbols selected into
    # the 500 (ACTIVE + WARNED), ordered by target weight descending.
    placeholders = ",".join("?" * len(index_syms))
    member_rows = conn.execute(
        f"""SELECT symbol, company_name, sharia_grade, bds_status, target_weight, index_status, warning_reason
            FROM constituents WHERE symbol IN ({placeholders})
            ORDER BY target_weight DESC""",
        index_syms,
    ).fetchall()

    csv_rows = []
    for r in member_rows:
        csv_rows.append({
            "Symbol": r[0],
            "Company": r[1],
            "ShariaGrade": r[2],
            "BDSStatus": r[3],
            "TargetWeightPct": round((r[4] or 0) * 100, 4),
            "IndexStatus": r[5],
            "Warning": r[6] or "",
        })
    df = pd.DataFrame(csv_rows)
    df.to_csv(INDEX_CSV, index=False)
    print(f"  Wrote {INDEX_CSV} ({len(csv_rows)} rows)")

    # Monthly weights snapshot: one dated file per calendar month (refreshed if more than
    # one full scan lands in the same month).
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snapshot_path = f"{SNAPSHOT_DIR}/{TODAY[:7]}.csv"
    df.to_csv(snapshot_path, index=False)
    print(f"  Wrote monthly snapshot {snapshot_path}")

    # Permanent, append-only event log
    _append_event_log(event_rows)

    _write_change_log(conn)
    conn.close()
    print("Constituent scan complete.")


def _append_event_log(event_rows: list[tuple]) -> None:
    """Append events to a permanent, never-trimmed CSV. Header written once."""
    if not event_rows:
        print(f"  No events to log this run ({EVENT_LOG_CSV} unchanged)")
        return
    os.makedirs(os.path.dirname(EVENT_LOG_CSV), exist_ok=True)
    write_header = not os.path.exists(EVENT_LOG_CSV)
    new = pd.DataFrame(
        event_rows,
        columns=["Date", "Symbol", "Company", "EventType", "OldValue", "NewValue", "Reason"],
    )
    new.to_csv(EVENT_LOG_CSV, mode="a", header=write_header, index=False)
    print(f"  Appended {len(event_rows)} event(s) to {EVENT_LOG_CSV}")


def _write_change_log(conn: sqlite3.Connection) -> None:
    """Prepend today's changes to change_log.md and drop entries > 30 trading days old."""
    # Fetch today's events
    todays = conn.execute(
        "SELECT symbol, event_type, old_grade, new_grade, bds_status, reason FROM change_log WHERE log_date = ?",
        (TODAY,),
    ).fetchall()

    # Count active/warned
    active_count = conn.execute(
        "SELECT COUNT(*) FROM constituents WHERE index_status = 'ACTIVE'"
    ).fetchone()[0]
    warned_count = conn.execute(
        "SELECT COUNT(*) FROM constituents WHERE index_status = 'WARNED'"
    ).fetchone()[0]

    added = [(r[0], r[2], r[3], r[4]) for r in todays if r[1] == "ADDED"]
    removed = [(r[0], r[5]) for r in todays if r[1] == "REMOVED"]
    warned = [(r[0], r[3]) for r in todays if r[1] == "WARNED"]
    cleared = [(r[0], r[3]) for r in todays if r[1] == "WARNING_CLEARED"]

    # Build new section
    lines = [
        f"## {TODAY} (Constituent Scan)",
    ]
    if added:
        lines += [f"\n### Added ({len(added)})", "| Symbol | Old Grade | New Grade | BDS |", "|--------|-----------|-----------|-----|"]
        for sym, og, ng, bds in added:
            lines.append(f"| {sym} | {og or '-'} | {ng} | {bds} |")

    if removed:
        lines += [f"\n### Removed ({len(removed)}) — Force Sold", "| Symbol | Reason |", "|--------|--------|"]
        for sym, reason in removed:
            lines.append(f"| {sym} | {reason} |")

    if warned:
        lines += [f"\n### Warned ({len(warned)}) — Grade slipped", "| Symbol | Grade |", "|--------|-------|"]
        for sym, grade in warned:
            lines.append(f"| {sym} | {grade} |")

    if cleared:
        lines += [f"\n### Warning Cleared ({len(cleared)})", "| Symbol | Grade |", "|--------|-------|"]
        for sym, grade in cleared:
            lines.append(f"| {sym} | {grade} |")

    if not any([added, removed, warned, cleared]):
        lines.append("\n_No changes this month._")

    new_section = "\n".join(lines) + "\n\n---\n"

    # Read existing log (drop if too old)
    cutoff = (date.today() - timedelta(days=42)).isoformat()  # ~30 trading days

    existing_content = ""
    if os.path.exists(CHANGE_LOG_MD):
        with open(CHANGE_LOG_MD, "r") as f:
            existing_content = f.read()

    # Strip header to just sections, then prepend new section
    header = (
        "# Halal-BDS-SP500 Change Log\n\n"
        "> **Disclaimer:** This index is published for informational purposes only and does not "
        "constitute financial advice. Past performance does not guarantee future results.\n\n"
        f"_Last updated: {TODAY} | Active: {active_count} | Warned: {warned_count}_\n\n---\n\n"
    )

    # Keep only sections from the existing content (everything after the first ---)
    body_start = existing_content.find("## ")
    old_sections = existing_content[body_start:] if body_start != -1 else ""

    # Drop sections older than cutoff
    filtered_sections = []
    for section in old_sections.split("\n## "):
        if not section.strip():
            continue
        section_date = section[:10]
        if section_date >= cutoff:
            filtered_sections.append("## " + section if not section.startswith("## ") else section)

    with open(CHANGE_LOG_MD, "w") as f:
        f.write(header + new_section + "\n".join(filtered_sections))

    print(f"  Wrote {CHANGE_LOG_MD}")


if __name__ == "__main__":
    run()
