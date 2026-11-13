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


def test_cash_flow_keeps_actual_cash_and_shows_spread_separately():
    # a yearly school fee paid in January, spread over 12 months; salary every month
    rows = [("2026-01-15", -1200, "Family: School")] + [(f"2026-{m:02d}-25", 3000, "Income: Salary") for m in (1, 2, 3)]
    df = frame(rows)
    df = df.assign(id=range(len(df)), amount=df["eur"], usd=df["eur"],
                   spread_months=[12, 1, 1, 1], spread_dir="next")
    cf = analysis.cash_flow(df).set_index("month")
    jan, mar = cf.loc[pd.Timestamp("2026-01-01")], cf.loc[pd.Timestamp("2026-03-01")]
    # actual cash: the whole fee in January, nothing in March
    assert jan["expenses"] == pytest.approx(1200) and mar["expenses"] == 0
    assert mar["net"] == pytest.approx(3000)
    # with spread costs: 100 a month, shown next to the actual figures, not instead of them
    assert jan["expenses_spread"] == pytest.approx(100) and mar["expenses_spread"] == pytest.approx(100)
    assert mar["net_spread"] == pytest.approx(2900) and mar["spread_effect"] == pytest.approx(-100)
    assert jan["spread_effect"] == pytest.approx(1100)
    assert list(cf.index) == list(pd.date_range("2026-01-01", "2026-03-01", freq="MS"))   # no months past the data


def test_cash_flow_without_spreads_matches_monthly_summary():
    df = frame([("2026-03-01", 3000, "Income: Salary"), ("2026-03-05", -200, "Shopping")])
    df = df.assign(id=[1, 2], amount=df["eur"], usd=df["eur"], spread_months=1, spread_dir="next")
    cf = analysis.cash_flow(df).iloc[0]
    assert cf["net"] == cf["net_spread"] == 2800 and cf["spread_effect"] == 0
    assert analysis.cash_flow(df.iloc[0:0]).empty
