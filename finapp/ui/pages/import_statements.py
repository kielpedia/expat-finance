"""Import statements page."""
import re
from datetime import date

import pandas as pd
import streamlit as st

from finapp import analysis, categorize, db, importers, taxtreat
from finapp.ui import common


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
    st.header("Import statements")
    st.markdown("Export transactions as **CSV** from your bank (TXT for ABN AMRO) and drop them here. "
                "Re-importing an overlapping period is safe; duplicates are skipped.")
    with st.expander("Where to find the export in each bank"):
        st.markdown("""
- **ING**: Mijn ING → account → *Downloaden* → format *Kommagescheiden CSV* (semicolon also works)
- **Rabobank**: Rabo Internetbankieren → *Downloaden transacties* → *CSV*
- **ABN AMRO**: Internet Banking (website) → *Mutaties downloaden* → *TXT*; the file may be saved as .TAB, which works too
- **bunq**: app → account → *Export statement* → *CSV*
- **Wise**: Statements → *CSV*, one export per currency balance
- **US banks/cards (Chase, BofA, Amex, Citi, Capital One…)**: Activity → Download → CSV
- **Fidelity**: *Accounts → Positions → Download* (all accounts in one file; each one is saved separately)
- **Schwab**: *History → Export* for transactions, and *Positions → Export* for each account's current value
  (Positions works for brokerage and IRAs, and the all-accounts view exports every account in one file).
  Other brokers/401(k)s: add a balance snapshot on *Accounts & net worth*.
""")
    accts = analysis.accounts(con)
    files = st.file_uploader("CSV / TXT / TAB files, or Excel stock-option files (E*TRADE)",
                             type=["csv", "txt", "tsv", "tab", "xlsx", "xls"], accept_multiple_files=True)
    for f in files or []:
        st.divider()
        if f.name.lower().endswith((".xlsx", ".xls")):
            from finapp.ui.pages import stock_options as ui_options
            st.subheader(f.name)
            st.caption("Excel file: read as a **stock option grant** export. It will show up on the **Stock options** page.")
            ui_options.import_file(con, ui_options.load_cfg(con), f.getvalue(), f.name, f"imp_{f.name}_")
            continue
        raw_text = importers._decode(f.getvalue())
        pos_kind = importers.detect_positions(raw_text)
        if pos_kind:
            broker, inst_label = pos_kind
            st.subheader(f.name)
            try:
                sections = importers.parse_positions(raw_text, broker, f.name)
            except Exception as e:  # noqa: BLE001
                st.error(f"Couldn't read this {inst_label} positions file: {e}")
                continue
            st.caption(f"{inst_label} **Positions** export: sets each account's total value (holdings + cash) on the date of the "
                       "export. It doesn't add transactions. Export it again later, or add snapshots, to build history.")
            names = ["➕ New account"] + accts["name"].tolist()
            if len(sections) > 1:
                st.info(f"This file has **{len(sections)} accounts**, totalling **${sum(x['total'] for x in sections):,.2f}**. "
                        "Each one is saved to its own account below.")
            for k, sec in enumerate(sections):
                with st.container(border=True):
                    c = st.columns([3, 2, 2])
                    c[0].markdown(f"**{sec['section']}**")
                    c[1].metric("Account value", f"${sec['total']:,.2f}")
                    c[2].metric("As of", f"{sec['date']:%d %b %Y}")
                    if sec["mismatch"]:
                        st.warning(f"The file's total row says ${sec['total']:,.2f}, but the rows above it add up to "
                                   f"${sec['sum_of_rows']:,.2f}. Check the breakdown below and correct the value if needed.")
                    if sec["unpriced_rows"]:
                        st.warning(f"{sec['unpriced_rows']} row(s) have no market value the app could read.")
                    with st.expander(f"How this was worked out · {len(sec['holdings'])} holdings"):
                        st.caption(f"Value taken from {sec['total_source']}. Rows add up to ${sec['sum_of_rows']:,.2f}. "
                                   "Shown only here on your computer.")
                        st.dataframe(sec["audit"], hide_index=True, width="stretch",
                                     column_config={"row": "Row in file", "used_as": "Used as",
                                                    "market_value": st.column_config.NumberColumn("Market value", format="dollar")})
                        st.dataframe(sec["holdings"], hide_index=True, width="stretch",
                                     column_config={"market_value": st.column_config.NumberColumn("Market value", format="dollar"),
                                                    "cost_basis": st.column_config.NumberColumn("Cost basis", format="dollar")})
                    value = st.number_input("Value to save (USD). Change it if it doesn't match Schwab's account value.",
                                            value=float(sec["total"]), step=100.0, format="%.2f", key=f"pos_v_{f.name}_{k}")
                    sec["total"] = value
                    tail = re.sub(r"\D", "", sec["section"])[-3:]
                    pre = next((i + 1 for i, n in enumerate(accts["name"]) if tail and tail in n), 0)
                    a = st.columns([3, 2, 2, 1])
                    target = a[0].selectbox("Into account", names, index=pre, key=f"pos_acct_{f.name}_{k}")
                    if target == names[0]:
                        new_name = a[1].text_input("Account name", f"{inst_label.split()[-1]} {sec['section']}"[:60], key=f"pos_n_{f.name}_{k}")
                        kinds = ["brokerage", "retirement", "savings", "checking"]
                        kind = a[2].selectbox("Type", kinds, index=kinds.index(importers.guess_kind(sec["section"])),
                                              key=f"pos_k_{f.name}_{k}")
                        g = taxtreat.guess(kind, sec["section"])
                        tc = st.columns([3, 2, 3])
                        new_t = tc[0].selectbox("Tax treatment", taxtreat.ORDER, index=taxtreat.ORDER.index(g),
                                                format_func=taxtreat.label, key=f"pos_t_{f.name}_{k}")
                        new_share = None
                        if new_t == "mixed":
                            new_share = tc[1].number_input("Roth share (%)", 0.0, 100.0, 0.0, 1.0, key=f"pos_s_{f.name}_{k}",
                                                           help="From your provider's balances-by-source page.") / 100
                            tc[2].caption(f"= ${sec['total'] * new_share:,.2f} Roth · ${sec['total'] * (1 - new_share):,.2f} pre-tax")
                    else:
                        arow = accts.loc[accts["name"] == target].iloc[0].to_dict()
                        t_key, t_guess = taxtreat.effective(arow)
                        if t_key == "mixed" and pd.notna(arow.get("roth_share")):
                            sh = float(arow["roth_share"])
                            st.caption(f"Split using {sh:.0%} Roth (set {arow.get('split_updated') or 'earlier'}): "
                                       f"${sec['total'] * sh:,.2f} Roth · ${sec['total'] * (1 - sh):,.2f} pre-tax. "
                                       "Update it under Accounts → ⋮ → Edit details if the split has changed.")
                        else:
                            st.caption(f"Tax treatment: {taxtreat.label(t_key)}" + (" (a guess; set it under Accounts → ⋮ → Edit details)"
                                                                                   if t_guess else ""))
                    if a[3].button("Save value", key=f"pos_go_{f.name}_{k}", type="primary"):
                        if target == names[0]:
                            aid = db.upsert_account(con, new_name, inst_label, "US", "USD", kind)
                            con.execute("UPDATE accounts SET tax_treatment=?, roth_share=?, split_updated=? WHERE id=?",
                                        (new_t, new_share, date.today().isoformat() if new_t == "mixed" else None, aid))
                        else:
                            aid = int(accts.loc[accts["name"] == target, "id"].iloc[0])
                        con.execute("INSERT INTO balances(account_id, date, balance, source) VALUES(?,?,?,'positions') "
                                    "ON CONFLICT(account_id, date) DO UPDATE SET balance=excluded.balance, source='positions'",
                                    (aid, sec["date"].strftime("%Y-%m-%d"), float(sec["total"])))
                        con.commit()
                        st.success(f"Saved ${sec['total']:,.2f} as of {sec['date']:%d %b %Y}.")
            continue

        text = importers.read_upload(f.getvalue())
        fmt = importers.detect_format(text)
        st.subheader(f.name)
        cols = st.columns([2, 2, 1])
        fmt = cols[0].selectbox("Format", list(importers.FORMATS), index=list(importers.FORMATS).index(fmt),
                                format_func=lambda k: importers.FORMATS[k][0], key=f"fmt_{f.name}")
        label, default_ccy, default_country = importers.FORMATS[fmt]

        kw = {}
        if fmt == "generic_us":
            raw = importers._read(text)
            guess = importers.guess_mapping(raw)
            with st.expander("Column mapping", expanded=not (guess["date"] and (guess["amount"] or guess["debit"]))):
                opts = [None] + list(raw.columns)
                m = {}
                mc = st.columns(6)
                for i, k in enumerate(["date", "description", "amount", "debit", "credit", "balance"]):
                    m[k] = mc[i].selectbox(k, opts, index=opts.index(guess[k]) if guess[k] in opts else 0, key=f"m_{k}_{f.name}")
                kw["mapping"] = m
                ic = st.columns(3)
                kw["invert"] = ic[0].checkbox("Charges are positive numbers (credit cards)", key=f"inv_{f.name}")
                kw["dayfirst"] = ic[1].checkbox("Dates are day-first", key=f"df_{f.name}")
                kw["currency"] = ic[2].text_input("Currency", default_ccy, key=f"ccy_{f.name}").upper()

        try:
            df = importers.parse(text, fmt, **kw)
        except Exception as e:  # noqa: BLE001
            st.error(f"Couldn't read this file as {label}: {e}")
            continue
        if df.empty:
            st.warning("No transactions found.")
            continue
        hint = df["account_hint"].dropna().astype(str).str.strip()
        hint = hint[hint != ""].iloc[0] if len(hint[hint != ""]) else ""
        st.caption(f"{len(df)} rows · {df['date'].min():%d %b %Y} – {df['date'].max():%d %b %Y} · "
                   f"in {df['amount'].clip(lower=0).sum():,.2f} / out {-df['amount'].clip(upper=0).sum():,.2f}")
        st.dataframe(df.head(8), width="stretch", hide_index=True)

        names = ["➕ New account"] + accts["name"].tolist()
        pre = next((i + 1 for i, n in enumerate(accts["name"]) if hint and hint in n), 0)
        target = cols[1].selectbox("Into account", names, index=pre, key=f"acct_{f.name}")
        new = {}
        if target == names[0]:
            nc = st.columns(5)
            new["name"] = nc[0].text_input("Account name", f"{label.split(' (')[0]} {hint[-4:]}".strip(), key=f"n_{f.name}")
            new["kind"] = nc[1].selectbox("Type", ["checking", "savings", "credit", "brokerage", "retirement", "loan"], key=f"k_{f.name}")
            ccys = df["currency"].dropna().unique()
            new["currency"] = nc[2].text_input("Currency", ccys[0] if len(ccys) else default_ccy, key=f"c_{f.name}").upper()
            new["country"] = nc[3].text_input("Country (bank location)", default_country, key=f"co_{f.name}").upper()
            new["owner"] = nc[4].selectbox("Owner", ["joint", "me", "partner"], key=f"o_{f.name}")
        if cols[2].button("Import", key=f"go_{f.name}", type="primary"):
            if target == names[0]:
                if not new["name"]:
                    st.error("Give the account a name.")
                    st.stop()
                aid = db.upsert_account(con, new["name"], label, new["country"], new["currency"], new["kind"], new["owner"])
            else:
                aid = int(accts.loc[accts["name"] == target, "id"].iloc[0])
            n, added = importers.save(con, aid, df, f.name, fmt)
            categorize.apply(con, only_uncategorized=True)
            st.success(f"Imported {added} new transactions ({n - added} duplicates skipped).")
