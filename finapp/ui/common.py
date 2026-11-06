"""Shared state and helpers for every page: the database connection, exchange rates, the household profile,
and small formatting and editing helpers. `refresh()` runs at the start of each script run."""
import re
from datetime import date

import pandas as pd
import streamlit as st

from finapp import analysis, categorize, db, fx, keystore

DEFAULT_PROFILE = {
    "tax_year": date.today().year,
    "filing_status": "MFJ",
    "partner": True,
    "partner_us_person": False,
    "me_has_ssn": True,
    "me_salary_eur": 0,
    "partner_salary_eur": 0,
    "kids": [],
    "expat_ruling": {"has": False, "who": "me", "first_applied_year": 2025, "under30_master": False},
    "box3_include_us_retirement": False,
    "box3_override": None,
    "treasury_usd_per_eur": None,
}

# Set by refresh() on every run; pages read them through state().
con = conv = profile = None
YEAR = date.today().year
USD_NOW = 1.0


@st.cache_resource
def get_con():
    return db.connect()


def refresh():
    """(Re)load the per-run state: connection, migrations, exchange rates, profile."""
    global con, conv, profile, YEAR, USD_NOW
    con = get_con()
    db._migrate(con)               # cheap; keeps a long-running app in step with code updates
    keystore.migrate_from_db(con)  # older versions kept the API key in the database
    conv = fx.Converter(con, fallback_usd_per_eur=db.get_setting(con, "fallback_usd_per_eur", 1.10))
    profile = {**DEFAULT_PROFILE, **(db.get_setting(con, "profile") or {})}
    YEAR = int(profile.get("tax_year") or date.today().year)
    USD_NOW = conv.usd_per_eur()


def state():
    """(con, conv, profile, YEAR, USD_NOW) for this run."""
    return con, conv, profile, YEAR, USD_NOW


def eur(v, dec=0):
    if v is None or pd.isna(v):
        return "—"
    return f"€{v:,.{dec}f}"


def dual(v, on=None, dec=0):
    """€1,234 · $1,345 — USD at the given date's rate (today by default)."""
    if v is None or pd.isna(v):
        return "—"
    r = conv.per_eur("USD", on) if on is not None else USD_NOW
    return f"€{v:,.{dec}f} · ${v * r:,.{dec}f}"


def pct(v):
    return "—" if v is None or pd.isna(v) else f"{v:.0%}"


def load_tx(start=None, end=None):
    return analysis.transactions(con, conv, start, end)


def category_options():
    custom = db.get_setting(con, "custom_categories", []) or []
    return sorted(set(categorize.all_categories(con)) | set(custom))


def spread_column(df):
    return st.column_config.SelectboxColumn(
        "Spread", options=analysis.spread_options(df["spread"].unique() if "spread" in df else ()), required=True,
        help="Budget only: spread this payment evenly over months. 'Next 12 months' starts in the month it was paid "
             "(a yearly school fee or insurance); 'Past 12 months' ends in it (a yearly energy settlement). "
             "Cash flow, balances and net worth still show the real payment.")


def save_tx_edits(changed, orig, note=False):
    """Write category / spread (and optionally note) edits; a hand-changed category gets locked."""
    for _, r in changed.iterrows():
        i = int(r["id"])
        lock = r["category"] != orig.at[i, "category"] if i in orig.index else True
        n, d = analysis.parse_spread(r["spread"])
        if note:
            con.execute("UPDATE transactions SET category=?, spread_months=?, spread_dir=?, note=?, "
                        "category_locked = CASE WHEN ? THEN 1 ELSE category_locked END WHERE id=?",
                        (r["category"], n, d, r["note"] or None, int(lock), i))
        else:
            con.execute("UPDATE transactions SET category=?, spread_months=?, spread_dir=?, "
                        "category_locked = CASE WHEN ? THEN 1 ELSE category_locked END WHERE id=?",
                        (r["category"], n, d, int(lock), i))
    con.commit()


def edited_rows(ed, orig, cols=("category", "spread")):
    m = pd.Series(False, index=ed.index)
    for c in cols:
        m |= ed[c].fillna("") != orig[c].fillna("")
    return ed[m]


def rule_pattern(payee: str) -> str:
    """Regex that matches this payee in raw bank text, tolerating store numbers etc. between words."""
    words = [re.escape(w) for w in payee.split() if w]
    return r".{0,12}?".join(words) if words else re.escape(payee)


def add_category_box(key):
    with st.expander("Need a category that isn't in the list?"):
        c = st.columns([3, 1])
        new = c[0].text_input("New category, as Group: Sub (e.g. Family: Sports & clubs)", key=f"newcat_{key}")
        if c[1].button("Add", key=f"addcat_{key}") and new.strip():
            custom = db.get_setting(con, "custom_categories", []) or []
            db.set_setting(con, "custom_categories", sorted(set(custom) | {new.strip()}))
            st.rerun()

