import pandas as pd
import pytest

from finapp import analysis
from finapp.categorize import group_of, is_budget


def frame(rows):
    df = pd.DataFrame(rows, columns=["date", "eur", "category"])
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    df["group"] = df["category"].map(group_of)
    df["budget"] = df["category"].map(is_budget)
    df["income"] = df["group"] == "Income"
    return df


def test_refund_reduces_spending_not_income():
    df = frame([("2026-03-01", 3000, "Income: Salary"), ("2026-03-05", -200, "Shopping"),
                ("2026-03-20", 200, "Shopping"),          # returned the item
                ("2026-03-21", -500, "Transfer")])        # transfers are left out
    ms = analysis.monthly_summary(df).iloc[0]
    assert ms["income"] == 3000
    assert ms["expenses"] == 0
    assert ms["savings_rate"] == 1.0


def test_by_category_nets_refunds():
    df = frame([("2026-03-05", -200, "Shopping"), ("2026-03-20", 50, "Shopping"), ("2026-03-07", -80, "Food: Groceries")])
    out = analysis.by_category(df).set_index("category")["eur"]
    assert out["Shopping"] == 150 and out["Food: Groceries"] == 80


def test_spread_view_splits_evenly():
    df = frame([("2026-01-15", -1200, "Insurance")])
    df = df.assign(id=1, amount=-1200.0, usd=-1300.0, spread_months=12, spread_dir="next")
    sv = analysis.spread_view(df)
    assert len(sv) == 12 and sv["eur"].sum() == pytest.approx(-1200)
    assert sv["month"].min() == pd.Timestamp("2026-01-01") and sv["month"].max() == pd.Timestamp("2026-12-01")


def test_health_leaves_out_current_month(loaded, conv):
    tx = analysis.transactions(loaded, conv)
    h = analysis.health(loaded, conv, tx)
    assert h["avg_income"] > 0 and 0 < h["savings_rate"] < 1
    assert h["months_used"] == 6


def test_sample_budget_is_sane(loaded, conv):
    tx = analysis.transactions(loaded, conv)
    ms = analysis.monthly_summary(tx)
    # sample household: two salaries, kinderbijslag every quarter; spending well below income
    assert ms["income"].median() == pytest.approx(4650 + 2850, rel=0.01)
    assert (ms["expenses"] > 0).all()
