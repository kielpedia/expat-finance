"""Demo household: the made-up statements in sample_data/, categorised, plus a matching household profile.

Loaded only when FINAPP_DEMO_DATA=1 *and* the database has no accounts and no transactions, so it can never mix
into real data. Meant for throwaway PR previews (whose database starts empty) and for trying the app.
"""
import os
import threading
from pathlib import Path

from . import categorize, db, importers

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"

# file, account name, country, currency, kind (same set the tests use)
STATEMENTS = [
    ("ing_joint_checking.csv", "ING joint", "NL", "EUR", "checking"),
    ("rabobank_savings.csv", "Rabo savings", "NL", "EUR", "savings"),
    ("chase_checking.csv", "Chase", "US", "USD", "checking"),
    ("amex.csv", "Amex", "US", "USD", "credit"),
]

PROFILE = {
    "partner": True,
    "me_salary_eur": 72000,
    "partner_salary_eur": 45000,
    "municipality": "Utrecht",
    "residents": 3,
    "home_owner": False,
    "kids": [{"name": "Sam", "birth": "2021-03-14", "has_ssn": True}],
}

_lock = threading.Lock()


def wanted(env=os.environ) -> bool:
    return str(env.get("FINAPP_DEMO_DATA", "")).strip().lower() in {"1", "true", "yes", "on"}


def is_empty(con) -> bool:
    return (con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None
            and con.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is None)


def is_demo(con) -> bool:
    return bool(db.get_setting(con, "demo_data", False))


def load(con) -> bool:
    """Import the demo household into an empty database. Returns False (and changes nothing) otherwise."""
    with _lock:
        if not is_empty(con):
            return False
        for name, acct, country, ccy, kind in STATEMENTS:
            text = importers.read_upload((SAMPLES / name).read_bytes())
            fmt = importers.detect_format(text)
            aid = db.upsert_account(con, acct, acct, country, ccy, kind)
            importers.save(con, aid, importers.parse(text, fmt), name, fmt)
        categorize.apply(con)
        if db.get_setting(con, "profile") is None:
            db.set_setting(con, "profile", PROFILE)
        db.set_setting(con, "demo_data", True)
        return True


def ensure(con, env=os.environ) -> bool:
    """Load the demo household if FINAPP_DEMO_DATA asks for it and the database is empty."""
    return wanted(env) and load(con)
