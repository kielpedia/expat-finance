"""Every page renders without an exception on the sample data (Streamlit's AppTest, no browser)."""
import pytest
import streamlit as st
from conftest import ROOT
from streamlit.testing.v1 import AppTest

PAGES = {  # module -> page heading starts with
    "overview": "Overview", "import_statements": "Import statements", "review": "Review", "transactions": "Transactions",
    "budget": "Budget", "accounts": "Accounts", "stock_options": "Stock options", "us_filing": "US filing",
    "dutch_tax": "Dutch tax", "settings": "Household",
}
SCRIPT = """
from finapp.ui import common
from finapp.ui.pages import {mod}
common.refresh()
{mod}.render()
"""


@pytest.mark.parametrize("mod", PAGES)
def test_page_renders(mod, loaded):
    from finapp import db
    db.set_setting(loaded, "profile", {"municipality": "Utrecht", "residents": 3, "home_owner": False,
                                       "kids": [{"name": "A", "birth": "2020-04-01", "has_ssn": True}]})
    st.cache_resource.clear()                           # use this test's database, not a cached connection
    at = AppTest.from_string(SCRIPT.format(mod=mod), default_timeout=120)
    at.run()
    assert not at.exception, at.exception[0].message if at.exception else ""
    assert at.header[0].value.startswith(PAGES[mod])


def test_app_opens_on_overview_with_page_navigation(loaded, monkeypatch):
    monkeypatch.setenv("FINAPP_ALLOW_REMOTE", "1")      # AppTest has no server address; the guard is tested below
    st.cache_resource.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception
    assert at.header[0].value == "Overview"


def test_refuses_to_run_on_a_network_address(loaded, monkeypatch):
    monkeypatch.delenv("FINAPP_ALLOW_REMOTE", raising=False)
    monkeypatch.setattr(st, "get_option", lambda key: "0.0.0.0" if key == "server.address" else None)
    st.cache_resource.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    assert "won't open here" in at.error[0].value
    assert not at.header


def test_server_mode_refuses_without_the_proxy_secret(loaded, monkeypatch):
    monkeypatch.delenv("FINAPP_ALLOW_REMOTE", raising=False)
    monkeypatch.setenv("FINAPP_MODE", "server")
    monkeypatch.setenv("FINAPP_PROXY_SECRET", "s" * 40)
    st.cache_resource.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()                                            # AppTest sends no headers, like a request that skips the proxy
    assert "reverse proxy" in at.error[0].value
    assert not at.header


@pytest.mark.parametrize("period, first, last", [
    ("Current month", "2026-11-01", "2026-11-01"),
    ("Last month", "2026-10-01", "2026-10-01"),
    ("Year to date", "2026-01-01", "2026-11-01"),
    ("1 year", "2025-12-01", "2026-11-01"),
    ("3 years", "2023-12-01", "2026-11-01"),
])
def test_cash_flow_periods(period, first, last):
    import pandas as pd

    from finapp.ui.pages.overview import period_range
    assert period_range(period, today="2026-11-05") == (pd.Timestamp(first), pd.Timestamp(last))


def test_cash_flow_period_order():
    from finapp.ui.pages.overview import PERIODS, period_range
    assert list(PERIODS) == ["Current month", "Last month", "Year to date", "1 year", "3 years", "All time"]
    assert period_range("All time") == (None, None)
    assert period_range("Last month", today="2026-01-15")[0].strftime("%Y-%m") == "2025-12"
