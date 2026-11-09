import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DEFAULT_DIR = Path(os.environ.get("FINAPP_HOME", Path.home() / "FinanceApp"))
DB_PATH = Path(os.environ.get("FINAPP_DB", DEFAULT_DIR / "finance.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    institution TEXT,
    country TEXT NOT NULL DEFAULT 'NL',      -- NL, US, other ISO code
    currency TEXT NOT NULL DEFAULT 'EUR',
    kind TEXT NOT NULL DEFAULT 'checking',   -- checking savings brokerage retirement credit loan mortgage
    owner TEXT DEFAULT 'joint',              -- me, partner, joint
    notes TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount REAL NOT NULL,                    -- in account currency, negative = money out
    description TEXT,
    counterparty TEXT,
    category TEXT,
    category_locked INTEGER DEFAULT 0,       -- 1 = set by hand, never auto-overwrite
    balance_after REAL,
    import_id INTEGER,
    hash TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_tx_acct ON transactions(account_id);
CREATE TABLE IF NOT EXISTS balances (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    balance REAL NOT NULL,
    source TEXT DEFAULT 'manual',            -- manual, import
    PRIMARY KEY (account_id, date)
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    filename TEXT, account_id INTEGER, fmt TEXT, rows INTEGER, added INTEGER,
    at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS fx_rates (
    date TEXT NOT NULL,
    currency TEXT NOT NULL,
    per_eur REAL NOT NULL,                   -- units of currency per 1 EUR (ECB convention)
    PRIMARY KEY (date, currency)
);
CREATE TABLE IF NOT EXISTS cat_rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,                   -- case-insensitive substring or regex
    category TEXT NOT NULL,
    priority INTEGER DEFAULT 100
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not DB_PATH.exists()
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    if new and os.name != "nt":
        try:
            os.chmod(DB_PATH, 0o600)   # readable by you only (Windows uses your profile's permissions instead)
        except OSError:
            pass
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    """Add columns introduced after the first release, keeping existing data."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(transactions)")}
    if "exclude_budget" not in cols:   # old 'one-off' flag, no longer used (kept so old databases still open)
        con.execute("ALTER TABLE transactions ADD COLUMN exclude_budget INTEGER DEFAULT 0")
    if "note" not in cols:
        con.execute("ALTER TABLE transactions ADD COLUMN note TEXT")
    if "spread_months" not in cols:    # >1 = budget spreads this payment over that many months
        con.execute("ALTER TABLE transactions ADD COLUMN spread_months INTEGER DEFAULT 1")
    if "spread_dir" not in cols:       # 'next' = starts in the payment month, 'past' = ends in it
        con.execute("ALTER TABLE transactions ADD COLUMN spread_dir TEXT DEFAULT 'next'")
    acols = {r["name"] for r in con.execute("PRAGMA table_info(accounts)")}
    for col, typ in (("tax_treatment", "TEXT"), ("roth_share", "REAL"), ("split_updated", "TEXT")):
        if col not in acols:   # tax treatment per account; roth_share (0-1) for mixed plans
            con.execute(f"ALTER TABLE accounts ADD COLUMN {col} {typ}")
    done = {r["key"] for r in con.execute("SELECT key FROM settings WHERE key LIKE 'migr_%'")}
    if "migr_rules_v2" not in done:
        # v2 rules split toeslagen into separate categories and fixed some mismatches (Wise, Disney Plus, 'loon');
        # re-sort the automatically categorised rows those rules touched. Hand-set categories are never changed.
        from . import categorize
        categorize.apply(con, only_categories=["Income: Toeslagen", "Income: Salary", "Transfer", "Food: Groceries",
                                               "Housing: Utilities", "Transfer: Investing"])
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('migr_rules_v2', 'true')")
    from . import options          # stock option tables (CREATE IF NOT EXISTS, cheap)
    options.setup(con)
    from . import payslips
    payslips.setup(con)
    con.commit()


def backup_bytes(con) -> bytes:
    """A consistent copy of the database (SQLite's backup API, safe while the app is writing), without any
    API key an older version may have stored."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "backup.db"
        dst = sqlite3.connect(path)
        con.backup(dst)
        _strip_api_key(dst)
        dst.close()
        return path.read_bytes()


def _strip_api_key(con):
    """Remove an API key an older version may have stored in the settings table."""
    row = con.execute("SELECT value FROM settings WHERE key='ai'").fetchone()
    if row:
        try:
            cfg = json.loads(row[0]) or {}
            if cfg.pop("api_key", None) is not None:
                con.execute("UPDATE settings SET value=? WHERE key='ai'", (json.dumps(cfg),))
                con.commit()
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass


class BackupError(ValueError):
    """A backup can't be restored. The message is written for the person using the app."""


REQUIRED_COLUMNS = {"accounts": {"id", "name"}, "transactions": {"account_id", "date", "amount"}, "settings": {"key", "value"}}


def _check_backup(con):
    """Raise BackupError unless this is an intact database from this app."""
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise BackupError("This file is damaged, so it can't be restored.")
    if con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view') LIMIT 1").fetchone():
        raise BackupError("This file contains database code this app never creates, so it wasn't restored.")
    for table, needed in REQUIRED_COLUMNS.items():
        if not needed <= {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}:
            raise BackupError("This is a database, but not a backup from this app.")


@contextmanager
def _opened_backup(data: bytes):
    """A checked, upgraded temporary copy of an uploaded backup. The upload itself is never trusted as-is."""
    if not data.startswith(b"SQLite format 3\x00"):
        raise BackupError("This isn't a database file. Choose a `.db` backup made by this app.")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "restore.db"
        path.write_bytes(data)
        src = sqlite3.connect(path)
        try:
            src.row_factory = sqlite3.Row
            try:
                src.execute("PRAGMA trusted_schema = OFF")
                _check_backup(src)
                src.execute("PRAGMA journal_mode = DELETE")
                src.executescript(SCHEMA)
                _migrate(src)
                _strip_api_key(src)
            except sqlite3.Error as e:
                raise BackupError(f"This file couldn't be read as a backup: {e}") from e
            yield src
        finally:
            src.close()


def summary(con) -> dict:
    """What a database holds, to show before replacing anything."""
    first, last = con.execute("SELECT MIN(date), MAX(date) FROM transactions").fetchone()
    return {"accounts": con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            "transactions": con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            "first": first, "last": last}


def inspect_backup(data: bytes) -> dict:
    """The summary of an uploaded backup, or BackupError if it can't be restored. Changes nothing."""
    with _opened_backup(data) as src:
        return summary(src)


def restore_bytes(con, data: bytes) -> Path:
    """Replace everything in the database with an uploaded backup, and return the safety copy made first.

    Copies into the open connection with SQLite's backup API, so the running app keeps working and a failure
    part-way leaves the current data as it was."""
    with _opened_backup(data) as src:
        try:
            con.commit()
            safety = DB_PATH.with_name(f"finance-before-restore-{datetime.now():%Y%m%d-%H%M%S-%f}.db")
            safety.touch(mode=0o600)
            safety.write_bytes(backup_bytes(con))
            src.backup(con)
        except (OSError, sqlite3.Error) as e:
            raise BackupError(f"Nothing was changed; the restore failed: {e}") from e
    return safety


def get_setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_setting(con, key, value):
    con.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    con.commit()


def upsert_account(con, name, institution="", country="NL", currency="EUR", kind="checking", owner="joint"):
    row = con.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO accounts(name, institution, country, currency, kind, owner) VALUES(?,?,?,?,?,?)",
        (name, institution, country, currency, kind, owner),
    )
    con.commit()
    return cur.lastrowid
