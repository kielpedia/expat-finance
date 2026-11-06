"""Transactions page."""

import pandas as pd
import streamlit as st

from finapp import categorize
from finapp.ui import common
from finapp.ui.common import add_category_box, category_options, dual, edited_rows, load_tx, save_tx_edits, spread_column


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
    st.header("Transactions")
    tx = load_tx()
    if tx.empty:
        st.info("Nothing imported yet.")
        st.stop()
    f = st.columns([2, 2, 2, 3])
    rng = f[0].date_input("Period", (tx["date"].min().date(), tx["date"].max().date()))
    acct = f[1].multiselect("Accounts", sorted(tx["account"].unique()))
    cats = f[2].multiselect("Categories", sorted(tx["category"].unique()))
    q = f[3].text_input("Search description / payee")
    old_oneoff = tx["exclude_budget"].fillna(0).astype(int).astype(bool) if "exclude_budget" in tx else pd.Series(False, index=tx.index)
    show_opts = ["All", "Spread payments", "Needs a category"] + (["Were marked one-off"] if old_oneoff.any() else [])
    if old_oneoff.any():
        oc = st.columns([5, 1], vertical_alignment="center")
        oc[0].info(f"**{int(old_oneoff.sum())} transactions were marked one-off.** That marker is gone: they now count "
                   "normally in your budget. To smooth a big yearly payment instead, pick a **Spread** for it "
                   "(Show → *Were marked one-off* lists them).", icon=":material/info:")
        if oc[1].button("Dismiss", key="oneoff_dismiss"):
            con.execute("UPDATE transactions SET exclude_budget=0")
            con.commit()
            st.rerun()
    only = st.radio("Show", show_opts, horizontal=True)
    v = tx.copy()
    if isinstance(rng, tuple) and len(rng) == 2:
        v = v[(v["date"].dt.date >= rng[0]) & (v["date"].dt.date <= rng[1])]
    if acct:
        v = v[v["account"].isin(acct)]
    if cats:
        v = v[v["category"].isin(cats)]
    if q:
        v = v[(v["description"] + " " + v["counterparty"] + " " + v["note"]).str.contains(q, case=False, regex=False)]
    if only == "Spread payments":
        v = v[v["spread_months"] > 1]
    elif only == "Were marked one-off":
        v = v[old_oneoff.loc[v.index]]
    elif only == "Needs a category":
        v = v[v["category"].isin(["Uncategorized", "Income: Other"])]
    st.caption(f"{len(v)} transaction{'' if len(v) == 1 else 's'} · net {dual(v['eur'].sum())}. For a payment that covers several months "
               "(a yearly school fee, annual insurance), pick a **Spread** so the budget shows its monthly share. "
               "Balances, net worth and actual cash flow still show the real payment.")

    all_cats = category_options()
    show = v[["id", "date", "account", "counterparty", "description", "amount", "currency", "eur", "usd",
              "category", "spread", "note"]].copy()
    show["date"] = show["date"].dt.date
    editable = ("category", "spread", "note")
    edited = st.data_editor(
        show, hide_index=True, width="stretch", height=520, disabled=[c for c in show.columns if c not in editable],
        column_config={
            "id": None,
            "amount": st.column_config.NumberColumn("Amount", format="%.2f"),
            "eur": st.column_config.NumberColumn("EUR", format="euro"),
            "usd": st.column_config.NumberColumn("USD", format="dollar"),
            "category": st.column_config.SelectboxColumn("Category", options=all_cats, required=True),
            "spread": spread_column(show),
            "note": st.column_config.TextColumn("Note"),
        }, key="tx_editor")
    changed = edited_rows(edited, show, ("category", "spread", "note"))
    c1, c2 = st.columns([1, 3])
    if len(changed) and c1.button(f"Save {len(changed)} changes", type="primary"):
        save_tx_edits(changed, show.set_index("id"), note=True)
        st.rerun()
    add_category_box("tx")
    with st.expander("Add a categorisation rule"):
        st.caption("Rules match payee + description (case-insensitive regex) and run before the built-in rules.")
        rc = st.columns([3, 2, 1])
        pat = rc[0].text_input("Text or regex", placeholder="e.g. swimming|zwembad")
        cat = rc[1].selectbox("Category", all_cats)
        new_cat = rc[1].text_input("…or a new category (Group: Sub)")
        if rc[2].button("Add & re-run"):
            con.execute("INSERT INTO cat_rules(pattern, category) VALUES(?, ?)", (pat, new_cat or cat))
            con.commit()
            n = categorize.apply(con)
            st.success(f"Re-categorised {n} transactions (hand-edited ones kept).")
