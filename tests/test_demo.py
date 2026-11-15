"""Demo household (FINAPP_DEMO_DATA=1): loads into an empty database only, never next to real data."""
from finapp import db, demo


def test_wanted_reads_the_flag():
    assert demo.wanted({"FINAPP_DEMO_DATA": "1"})
    assert demo.wanted({"FINAPP_DEMO_DATA": " True "})
    assert not demo.wanted({"FINAPP_DEMO_DATA": "0"})
    assert not demo.wanted({})


def test_loads_into_an_empty_database(con):
    assert demo.ensure(con, {"FINAPP_DEMO_DATA": "1"})
    assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == len(demo.STATEMENTS)
    assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > 500
    assert con.execute("SELECT COUNT(*) FROM transactions WHERE category IS NULL OR category = ''").fetchone()[0] \
        < con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert demo.is_demo(con)
    assert db.get_setting(con, "profile")["municipality"] == "Utrecht"


def test_second_load_changes_nothing(con):
    demo.load(con)
    before = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert not demo.load(con)
    assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before


def test_never_touches_a_database_with_data(con):
    db.upsert_account(con, "My real bank")
    assert not demo.ensure(con, {"FINAPP_DEMO_DATA": "1"})
    assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert not demo.is_demo(con)


def test_off_without_the_flag(con):
    assert not demo.ensure(con, {})
    assert demo.is_empty(con)


def test_app_starts_with_demo_data(con, monkeypatch):
    import streamlit as st
    from conftest import ROOT
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("FINAPP_DEMO_DATA", "1")
    st.cache_resource.clear()                           # use this test's database, not a cached connection
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, at.exception[0].message if at.exception else ""
    assert any("Demo data" in w.value for w in at.sidebar.warning)
    assert demo.is_demo(con)
