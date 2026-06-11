import sqlite3


def init_db(db_path: str = "index_fund.db") -> None:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS constituents (
            symbol         TEXT PRIMARY KEY,
            company_name   TEXT,
            market_cap     REAL,
            target_weight  REAL,
            sharia_grade   TEXT,
            bds_status     TEXT,
            index_status   TEXT,
            warning_reason TEXT,
            added_date     TEXT,
            removed_date   TEXT,
            last_checked   TEXT
        );

        CREATE TABLE IF NOT EXISTS compliance_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            check_date     TEXT NOT NULL,
            symbol         TEXT NOT NULL,
            sharia_grade   TEXT,
            sharia_status  TEXT,
            bds_status     TEXT,
            UNIQUE(check_date, symbol)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_date TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            action           TEXT NOT NULL,
            notional_amount  REAL,
            quantity         REAL,
            price            REAL,
            alpaca_order_id  TEXT,
            status           TEXT,
            reason           TEXT
        );

        CREATE TABLE IF NOT EXISTS change_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date    TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            old_grade   TEXT,
            new_grade   TEXT,
            bds_status  TEXT,
            reason      TEXT
        );
    """)

    conn.commit()
    conn.close()
    print(f"Database initialised: {db_path}")


if __name__ == "__main__":
    init_db()
