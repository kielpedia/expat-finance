import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "sample_data"


@pytest.fixture
def con(tmp_path, monkeypatch):
    """A fresh, empty database in a temp folder. Never touches ~/FinanceApp."""
    from finapp import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    yield c
    c.close()


class FixedConverter:
    """Stand-in for fx.Converter with a fixed USD rate, so tests don't need ECB data or the network."""

    def __init__(self, usd_per_eur=1.10):
        self.usd = usd_per_eur

    def per_eur(self, ccy, on=None):
        return 1.0 if ccy == "EUR" else self.usd

    def to_eur_series(self, df, amount_col="amount", ccy_col="currency", date_col="date"):
        return df[amount_col] / df[ccy_col].map(lambda c: self.per_eur(c))

    def usd_per_eur(self, on=None):
        return self.usd

    year_end_usd_per_eur = year_avg_usd_per_eur = lambda self, year: self.usd

    def uses_fallback(self, ccy="USD"):
        return False

    def rate_gap_days(self, ccy, on):
        return 0


@pytest.fixture
def conv():
    return FixedConverter()


@pytest.fixture
def loaded(con):
    """Database with the four sample statements imported and categorised."""
    from finapp import categorize, db, importers
    for name, acct, country, ccy, kind in [
        ("ing_joint_checking.csv", "ING joint", "NL", "EUR", "checking"),
        ("rabobank_savings.csv", "Rabo savings", "NL", "EUR", "savings"),
        ("chase_checking.csv", "Chase", "US", "USD", "checking"),
        ("amex.csv", "Amex", "US", "USD", "credit"),
    ]:
        text = importers.read_upload((SAMPLES / name).read_bytes())
        fmt = importers.detect_format(text)
        aid = db.upsert_account(con, acct, acct, country, ccy, kind)
        importers.save(con, aid, importers.parse(text, fmt), name, fmt)
    categorize.apply(con)
    return con
