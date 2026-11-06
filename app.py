"""
Household Finance — local budget and financial-health app for a US family living in the Netherlands.
Run with:  streamlit run app.py   (or use run.sh / run.bat)
"""

import os
from pathlib import Path

import streamlit as st

from finapp import db, fx, rules
from finapp.ui import common
from finapp.ui.pages import (
    accounts,
    budget,
    dutch_tax,
    import_statements,
    overview,
    review,
    settings,
    stock_options,
    transactions,
    us_filing,
)

st.set_page_config(page_title="Household Finance", page_icon="💶", layout="wide")


# The app has no login, so it must only be reachable from this computer. Refuse to show anything when it's
# listening on a network address or being reached through a reverse proxy. FINAPP_ALLOW_REMOTE=1 overrides
# this, at your own risk (put real authentication in front of it first).
def _local_only():
    if os.environ.get("FINAPP_ALLOW_REMOTE") == "1":
        return
    local = {"localhost", "127.0.0.1", "::1"}
    addr = str(st.get_option("server.address") or "").strip().lower()
    try:
        headers = {k.lower(): v for k, v in st.context.headers.items()}
    except Exception:  # noqa: BLE001  (no request context, e.g. in tests)
        headers = {}
    host = headers.get("host", "")
    host = host.rsplit("]", 1)[0].lstrip("[") if host.startswith("[") else host.split(":")[0]
    proxied = any(h in headers for h in ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip"))
    problem = None
    if addr not in local:
        problem = (f"it is listening on **{addr or 'every network interface'}**" +
                   " instead of localhost. Start it with `run.bat` / `run.sh`, which keep it on this computer.")
    elif proxied or (host and host.lower() not in local):
        problem = "it is being reached through a proxy or another hostname, so other people may be able to see it."
    if problem:
        st.error("**Household Finance won't open here:** " + problem + "\n\nThe app has no login, so it only runs for "
                 "this computer, at http://localhost:8501.", icon=":material/lock:")
        st.stop()


_local_only()


# Streamlit renders text between two "$" signs as math. This app prints a lot of dollar
# amounts, so escape "$" in every text element (and metric labels) once, here.
def _install_dollar_escaping():
    import functools
    import re

    from streamlit.delta_generator import DeltaGenerator

    def esc(s):
        return re.sub(r"(?<!\\)\$", r"\\$", s) if isinstance(s, str) else s

    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, body, *a, **k):
            return fn(self, esc(body), *a, **k)
        return inner

    for name in ("markdown", "caption", "info", "warning", "error", "success", "expander", "metric"):
        orig = getattr(DeltaGenerator, name)
        if getattr(orig, "_dollar_safe", False):
            continue
        patched = wrap(orig)
        patched._dollar_safe = True
        setattr(DeltaGenerator, name, patched)
        setattr(st, name, getattr(st._main, name))


_install_dollar_escaping()

common.refresh()

# Each page has its own address (http://localhost:8501/budget etc.), so reloading or bookmarking keeps you there.
PAGES = [
    st.Page(overview.render, title="Overview", url_path="overview", default=True),
    st.Page(import_statements.render, title="Import statements", url_path="import"),
    st.Page(review.render, title="Review & categorise", url_path="review"),
    st.Page(transactions.render, title="Transactions", url_path="transactions"),
    st.Page(budget.render, title="Budget", url_path="budget"),
    st.Page(accounts.render, title="Accounts & net worth", url_path="accounts"),
    st.Page(stock_options.render, title="Stock options", url_path="stock-options"),
    st.Page(us_filing.render, title="US filing", url_path="us-filing"),
    st.Page(dutch_tax.render, title="Dutch tax & benefits", url_path="dutch-tax"),
    st.Page(settings.render, title="Household & settings", url_path="settings"),
]
page = st.navigation(PAGES, expanded=True)
st.logo(str(Path(__file__).parent / "finapp" / "ui" / "logo.svg"), size="large")
with st.sidebar:
    st.caption(f"Tax year **{rules.year_label(common.YEAR)}**  ·  1 EUR = {common.USD_NOW:.4f} USD")
    fx_date = fx.latest_date(common.con)
    st.caption(f"FX rates: {'ECB, ' + fx_date if fx_date else '⚠️ not loaded, using fallback'}")
    st.caption(f"Data file: `{db.DB_PATH}`")

page.run()
