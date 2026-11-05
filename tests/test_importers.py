import pandas as pd
import pytest

from conftest import SAMPLES
from finapp import importers


@pytest.mark.parametrize("raw, dec, want", [
    ("1.234,56", ",", 1234.56), ("-12,3", ",", -12.3), ("1,234.56", ".", 1234.56), ("$1,200.00", ".", 1200.0),
    ("(45.00)", ".", -45.0), ("+5", None, 5.0), ("1.234,56", None, 1234.56), ("1,234.56", None, 1234.56),
    ("12,50", None, 12.5), ("1,234", None, 1234.0), ("", None, float("nan")),
])
def test_parse_number(raw, dec, want):
    got = importers.parse_number(raw, dec)
    assert (pd.isna(got) and pd.isna(want)) or got == pytest.approx(want)


@pytest.mark.parametrize("name, fmt", [
    ("ing_joint_checking.csv", "ing"), ("rabobank_savings.csv", "rabobank"),
    ("amex.csv", "amex"), ("chase_checking.csv", "generic_us"),
])
def test_detect_format(name, fmt):
    assert importers.detect_format(importers.read_upload((SAMPLES / name).read_bytes())) == fmt


def test_ing_signs_and_balance():
    df = importers.parse_ing(importers.read_upload((SAMPLES / "ing_joint_checking.csv").read_bytes()))
    assert (df.loc[df["counterparty"] == "ACME BV", "amount"] > 0).all()            # salary in
    assert (df.loc[df["counterparty"] == "Picnic", "amount"] < 0).all()              # groceries out
    assert df["balance_after"].notna().all()
    assert set(df["currency"]) == {"EUR"}


def test_amex_charges_are_negative():
    t = importers.read_upload((SAMPLES / "amex.csv").read_bytes())
    df = importers.parse(t, "amex")
    assert (df.loc[df["description"].str.contains("AMAZON"), "amount"] < 0).all()
    assert (df.loc[df["description"].str.contains("AUTOPAY"), "amount"] > 0).all()


def test_reimport_skips_duplicates(con):
    from finapp import db
    t = importers.read_upload((SAMPLES / "ing_joint_checking.csv").read_bytes())
    df = importers.parse(t, "ing")
    aid = db.upsert_account(con, "ING", "ING")
    n1, added1 = importers.save(con, aid, df, "a.csv", "ing")
    n2, added2 = importers.save(con, aid, df, "a.csv", "ing")
    assert added1 == n1 and added2 == 0


def test_same_day_identical_rows_are_kept(con):
    from finapp import db
    df = pd.DataFrame({"date": pd.to_datetime(["2026-01-05"] * 2), "amount": [-3.5, -3.5], "description": ["Coffee"] * 2,
                       "counterparty": ["Cafe"] * 2, "balance_after": [None, None], "currency": "EUR", "account_hint": None})
    aid = db.upsert_account(con, "X")
    assert importers.save(con, aid, df, "x", "ing") == (2, 2)
