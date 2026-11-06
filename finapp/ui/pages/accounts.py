"""Accounts & net worth page."""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from finapp import analysis, taxtreat
from finapp.ui import common
from finapp.ui.common import dual


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
    st.header("Accounts & net worth")
    accts = analysis.accounts(con)
    KINDS = ["checking", "savings", "brokerage", "retirement", "options", "credit", "loan", "mortgage"]
    OWNERS = ["joint", "me", "partner"]

    def _wipe(aid, keep_account, keep_manual=True):
        con.execute("DELETE FROM transactions WHERE account_id=?", (aid,))
        con.execute("DELETE FROM balances WHERE account_id=?" + (" AND source='import'" if keep_account and keep_manual else ""), (aid,))
        con.execute("DELETE FROM imports WHERE account_id=?", (aid,))
        if not keep_account:
            con.execute("DELETE FROM accounts WHERE id=?", (aid,))
        con.commit()

    def _details_form(prefix, row=None):
        row = row or {}
        inst0 = row.get("institution")
        inst0 = inst0 if isinstance(inst0, str) and inst0.lower() != "nan" else ""
        name = st.text_input("Name", row.get("name", ""), key=f"{prefix}_name")
        inst = st.text_input("Institution", inst0, key=f"{prefix}_inst")
        c = st.columns(2)
        kind = c[0].selectbox("Type", KINDS, index=KINDS.index(row.get("kind", "checking")) if row.get("kind") in KINDS else 0,
                              key=f"{prefix}_kind")
        owner = c[1].selectbox("Owner", OWNERS, index=OWNERS.index(row.get("owner", "joint")) if row.get("owner") in OWNERS else 0,
                               key=f"{prefix}_owner")
        c = st.columns(2)
        country = c[0].text_input("Country of the bank (NL, US…)", row.get("country", "NL"), key=f"{prefix}_country").strip().upper()
        currency = c[1].text_input("Currency", row.get("currency", "EUR"), key=f"{prefix}_ccy").strip().upper()
        st.caption("Anything not 'US' counts as a foreign account for FBAR. Credit cards and loans hold negative balances.")
        cur_t, guessed = taxtreat.effective({**row, "kind": kind, "name": name})
        treatment = st.selectbox("Tax treatment", taxtreat.ORDER, index=taxtreat.ORDER.index(cur_t),
                                 format_func=taxtreat.label, key=f"{prefix}_tax",
                                 help="Decides how the account is treated for Dutch box 3 and shown on the US page."
                                      + (" Currently a guess from the name and type." if guessed else ""))
        share = row.get("roth_share")
        share = float(share) if share is not None and pd.notna(share) else None
        if treatment == "mixed":
            st.caption("Find the split on your provider's *balances by source* page (Fidelity: NetBenefits → Balances → "
                       "Sources). Enter the Roth part as a percentage, or as an amount and the app works out the percentage.")
            sc = st.columns(2)
            pct = sc[0].number_input("Roth share (%)", 0.0, 100.0, float(round((share or 0.0) * 100, 2)), 1.0, key=f"{prefix}_rothpct")
            latest = row.get("bal")
            latest = float(latest) if latest is not None and pd.notna(latest) else None
            amt = sc[1].number_input(f"…or Roth amount ({currency})", 0.0, value=0.0, step=100.0, key=f"{prefix}_rothamt",
                                     help="Uses the account's latest value to turn the amount into a percentage."
                                     if latest and pd.notna(latest) else "Save a value for this account first to use this.")
            if amt and latest and pd.notna(latest) and latest > 0:
                pct = min(100.0, amt / latest * 100)
                st.caption(f"= {pct:.1f}% of the latest value ({latest:,.2f} {currency}).")
            share = pct / 100
        return name.strip(), inst.strip(), country or "NL", currency or "EUR", kind, owner, treatment, share

    @st.dialog("Add an account")
    def dlg_add():
        vals = _details_form("add")
        if st.button("Add account", type="primary", disabled=not vals[0]):
            if (accts["name"] == vals[0]).any():
                st.error("There's already an account with that name.")
                return
            con.execute("INSERT INTO accounts(name, institution, country, currency, kind, owner, tax_treatment, roth_share, "
                        "split_updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (*vals, date.today().isoformat() if vals[6] == "mixed" else None))
            con.commit()
            st.rerun()

    @st.dialog("Edit account")
    def dlg_edit(row):
        vals = _details_form(f"edit{row['id']}", row)
        c = st.columns(2)
        if c[0].button("Cancel", width="stretch"):
            st.rerun()
        if c[1].button("Save", type="primary", width="stretch", disabled=not vals[0]):
            if ((accts["name"] == vals[0]) & (accts["id"] != row["id"])).any():
                st.error("Another account already has that name.")
                return
            split_changed = vals[6] == "mixed" and (row.get("roth_share") is None or pd.isna(row.get("roth_share"))
                                                     or abs(float(row["roth_share"]) - (vals[7] or 0)) > 1e-9)
            con.execute("UPDATE accounts SET name=?, institution=?, country=?, currency=?, kind=?, owner=?, tax_treatment=?, "
                        "roth_share=?, split_updated=COALESCE(?, split_updated) WHERE id=?",
                        (*vals, date.today().isoformat() if split_changed else None, int(row["id"])))
            con.commit()
            st.rerun()

    @st.dialog("Delete account?")
    def dlg_delete(row):
        st.markdown(f"This permanently deletes **{row['name']}** together with its **{row['txns']} transactions** and "
                    f"**{row['bals']} balance points**. Your categorisation rules are kept.")
        st.error("This can't be undone.", icon="⚠️")
        c = st.columns(2)
        if c[0].button("Cancel", width="stretch", key=f"del_cancel_{row['id']}"):
            st.rerun()
        if c[1].button(":red[**Delete account**]", width="stretch", key=f"del_go_{row['id']}"):
            _wipe(int(row["id"]), keep_account=False)
            st.toast(f"Deleted {row['name']}")
            st.rerun()

    @st.dialog("Remove all transactions?")
    def dlg_empty(row):
        st.markdown(f"Removes the **{row['txns']} transactions** in **{row['name']}** but keeps the account and its settings, "
                    "so you can import into it again.")
        keep_manual = st.checkbox("Keep balance snapshots I entered by hand", True, key=f"empty_keep_{row['id']}")
        c = st.columns(2)
        if c[0].button("Cancel", width="stretch", key=f"empty_cancel_{row['id']}"):
            st.rerun()
        if c[1].button(":red[**Remove transactions**]", width="stretch", key=f"empty_go_{row['id']}"):
            _wipe(int(row["id"]), keep_account=True, keep_manual=keep_manual)
            st.toast(f"Emptied {row['name']}")
            st.rerun()

    @st.dialog("Undo an import")
    def dlg_undo(row):
        imps = pd.read_sql(
            "SELECT i.id, i.at, i.filename, (SELECT COUNT(*) FROM transactions t WHERE t.import_id=i.id) AS n "
            "FROM imports i WHERE i.account_id=? ORDER BY i.id DESC", con, params=(int(row["id"]),))
        imps = imps[imps["n"] > 0]
        if imps.empty:
            st.caption("No imports left to undo for this account.")
            return
        pick = st.radio("Which file?", imps.index,
                        format_func=lambda i: f"{imps.at[i, 'filename']} · imported {imps.at[i, 'at']} · {imps.at[i, 'n']} transactions",
                        key=f"undo_pick_{row['id']}")
        r = imps.loc[pick]
        st.markdown(f"Removes the **{r['n']} transactions** that came from **{r['filename']}**. "
                    "Transactions from other files stay.")
        c = st.columns(2)
        if c[0].button("Cancel", width="stretch", key=f"undo_cancel_{row['id']}"):
            st.rerun()
        if c[1].button(":red[**Undo import**]", width="stretch", key=f"undo_go_{row['id']}"):
            iid = int(r["id"])
            span = con.execute("SELECT MIN(date) lo, MAX(date) hi FROM transactions WHERE import_id=?", (iid,)).fetchone()
            con.execute("DELETE FROM transactions WHERE import_id=?", (iid,))
            if span and span["lo"]:
                con.execute("DELETE FROM balances WHERE account_id=? AND source='import' AND date BETWEEN ? AND ? "
                            "AND date NOT IN (SELECT date FROM transactions WHERE account_id=? AND balance_after IS NOT NULL)",
                            (int(row["id"]), span["lo"], span["hi"], int(row["id"])))
            con.execute("DELETE FROM imports WHERE id=?", (iid,))
            con.commit()
            st.toast(f"Undid {r['filename']}")
            st.rerun()

    head = st.columns([6, 1])
    head[0].caption("Use the :material/more_vert: button on a row to edit it, undo an import, clear its transactions or delete it. "
                    "Under each type is the account's tax treatment; a **?** means it's a guess, so set it with Edit details.")
    if head[1].button("➕ Add account", width="stretch"):
        dlg_add()

    if accts.empty:
        st.info("No accounts yet. They're created when you import a statement, or add one here.")
    else:
        stats = pd.read_sql(
            "SELECT a.*, (SELECT COUNT(*) FROM transactions t WHERE t.account_id=a.id) AS txns, "
            "(SELECT COUNT(*) FROM balances b WHERE b.account_id=a.id) AS bals, "
            "(SELECT MIN(date) FROM transactions t WHERE t.account_id=a.id) AS first, "
            "(SELECT MAX(date) FROM transactions t WHERE t.account_id=a.id) AS last, "
            "(SELECT balance FROM balances b WHERE b.account_id=a.id ORDER BY date DESC LIMIT 1) AS bal, "
            "(SELECT date FROM balances b WHERE b.account_id=a.id ORDER BY date DESC LIMIT 1) AS bal_date "
            "FROM accounts a ORDER BY a.country, a.name", con)
        W = [3.2, 1.3, 0.8, 1.0, 1.6, 2, 0.8]
        with st.container(border=True):
            h = st.columns(W, vertical_alignment="center")
            for col, label in zip(h, ["Account", "Type", "Country", "Owner", "Transactions", "Latest balance", ""]):
                col.caption(f"**{label}**")
            for _, r in stats.iterrows():
                row = r.to_dict()
                c = st.columns(W, vertical_alignment="center")
                inst = row.get("institution")
                inst = inst if isinstance(inst, str) and inst.strip() and inst.lower() != "nan" else ""
                c[0].markdown(f"**{row['name']}**" + (f"  \n:gray[{inst}]" if inst else ""))
                t_key, t_guess = taxtreat.effective(row)
                t_txt = taxtreat.label(t_key).split(":")[0]
                if t_key == "mixed" and pd.notna(row.get("roth_share")):
                    t_txt = f"Mixed · {float(row['roth_share']):.0%} Roth"
                c[1].markdown(f"{row['kind']}  \n:gray[{t_txt}{' ?' if t_guess and t_key not in ('cash', 'debt') else ''}]")
                c[2].write(row["country"])
                c[3].write(row["owner"] or "")
                rng = f"  \n:gray[{row['first'][:7]} → {row['last'][:7]}]" if row["txns"] else ""
                c[4].markdown(f"{row['txns']:,}{rng}")
                if pd.notna(row["bal"]):
                    c[5].markdown(f"{row['bal']:,.2f} {row['currency']}  \n:gray[{row['bal_date']}]")
                else:
                    c[5].markdown(":gray[none yet]")
                with c[6].popover("", icon=":material/more_vert:", help="Actions", width="stretch"):
                    if st.button("✏️ Edit details", key=f"a_edit_{row['id']}", width="stretch"):
                        dlg_edit(row)
                    if st.button("↩️ Undo an import", key=f"a_undo_{row['id']}", width="stretch", disabled=row["txns"] == 0):
                        dlg_undo(row)
                    if st.button("🧹 Remove all transactions", key=f"a_empty_{row['id']}", width="stretch",
                                 disabled=row["txns"] == 0):
                        dlg_empty(row)
                    st.divider()
                    if st.button(":red[:material/delete: **Delete account**]", key=f"a_del_{row['id']}", width="stretch"):
                        dlg_delete(row)

    st.subheader("Add a balance snapshot")
    st.caption("For brokerage, 401(k)/IRA, pension, mortgage and loans, or any account without a running balance in its export. "
               "For FBAR, record each account's highest balance of the year too.")
    if len(accts):
        bc = st.columns([3, 2, 2, 1])
        a = bc[0].selectbox("Account", accts["name"])
        d = bc[1].date_input("Date", date.today())
        ccy = accts.loc[accts["name"] == a, "currency"].iloc[0]
        val = bc[2].number_input(f"Balance ({ccy})", value=0.0, step=100.0)
        if bc[3].button("Save", type="primary"):
            aid = int(accts.loc[accts["name"] == a, "id"].iloc[0])
            con.execute("INSERT INTO balances(account_id, date, balance, source) VALUES(?,?,?,'manual') "
                        "ON CONFLICT(account_id, date) DO UPDATE SET balance=excluded.balance, source='manual'",
                        (aid, d.isoformat(), val))
            con.commit()
            st.rerun()

    nw = analysis.net_worth_series(con, conv)
    if len(nw):
        st.subheader("Net worth")
        last = nw.iloc[-1]
        c = st.columns(3)
        c[0].metric("Assets", dual(last["assets"]))
        c[1].metric("Liabilities", dual(last["liabilities"]))
        c[2].metric("Net worth", dual(last["net_worth"]))
        cov = analysis.coverage(con, conv)
        missing = cov[cov["status"] == "missing"]
        stale = cov[cov["status"] == "stale"]
        if len(missing):
            st.warning("**Not counted in net worth (no value yet):** " + ", ".join(missing["name"]) +
                       ". Their exports don't include a balance. For Schwab, import a *Positions* file; "
                       "for anything else, add a balance snapshot above.", icon=":material/error:")
        if len(stale):
            st.info("**Using an older value:** " + ", ".join(f"{r['name']} ({r['last_value']:%d %b %Y})" for _, r in stale.iterrows()) +
                    ". Add a fresh snapshot or Positions import to bring them up to date.", icon=":material/schedule:")
        show_usd = st.toggle("Show in USD")
        k = USD_NOW if show_usd else 1
        fig = go.Figure()
        fig.add_scatter(x=nw["date"], y=nw["assets"] * k, name="Assets", fill="tozeroy", line_color="#2e7d5b")
        fig.add_scatter(x=nw["date"], y=-nw["liabilities"] * k, name="Liabilities", fill="tozeroy", line_color="#c0504d")
        fig.add_scatter(x=nw["date"], y=nw["net_worth"] * k, name="Net worth", line=dict(color="#222", width=3))
        fig.update_layout(height=360, yaxis_title="USD" if show_usd else "EUR", margin=dict(t=10))
        st.plotly_chart(fig, width="stretch")
        cur = analysis.balance_on(con, conv, pd.Timestamp.today())
        if len(cur):
            cur["usd"] = cur["eur"] * USD_NOW
            st.dataframe(cur[["name", "kind", "country", "currency", "balance", "eur", "usd", "date"]], hide_index=True, width="stretch",
                         column_config={"balance": st.column_config.NumberColumn("Balance (local)", format="%.2f"),
                                        "eur": st.column_config.NumberColumn("EUR", format="euro"),
                                        "usd": st.column_config.NumberColumn("USD", format="dollar"),
                                        "date": st.column_config.DateColumn("As of")})
            by_ccy = cur.groupby("currency")["eur"].sum()
            if len(by_ccy) > 1:
                st.caption("Currency exposure: " + " · ".join(f"{c} {v / cur['eur'].sum():.0%}" for c, v in by_ccy.items()))
