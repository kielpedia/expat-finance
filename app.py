"""
Household Finance — local budget and financial-health app for a US family living in the Netherlands.
Run with:  streamlit run app.py   (or use run.sh / run.bat)
"""
from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import os
import re

from finapp import ai, analysis, categorize, db, expat, fx, importers, rules, taxtreat

st.set_page_config(page_title="Household Finance", page_icon="💶", layout="wide")


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


# ------------------------------------------------------------------ shared state

@st.cache_resource
def get_con():
    return db.connect()


con = get_con()
db._migrate(con)  # cheap; keeps a long-running app in step with code updates


def get_conv():
    return fx.Converter(con, fallback_usd_per_eur=db.get_setting(con, "fallback_usd_per_eur", 1.10))


conv = get_conv()

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
profile = {**DEFAULT_PROFILE, **(db.get_setting(con, "profile") or {})}
YEAR = int(profile.get("tax_year") or date.today().year)
USD_NOW = conv.usd_per_eur()


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


# ------------------------------------------------------------------ navigation

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
    import re
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


PAGES = ["Overview", "Import statements", "Review & categorise", "Transactions", "Budget", "Accounts & net worth", "Stock options",
         "US filing", "Dutch tax & benefits", "Household & settings"]
with st.sidebar:
    st.title("💶 Household Finance")
    page = st.radio("Go to", PAGES, label_visibility="collapsed")
    st.divider()
    st.caption(f"Tax year **{rules.year_label(YEAR)}**  ·  1 EUR = {USD_NOW:.4f} USD")
    fx_date = fx.latest_date(con)
    st.caption(f"FX rates: {'ECB, ' + fx_date if fx_date else '⚠️ not loaded, using fallback'}")
    st.caption(f"Data file: `{db.DB_PATH}`")


# ================================================================== Overview
if page == "Overview":
    st.header("Overview")
    tx = load_tx()
    if tx.empty:
        st.info("No data yet. Start on **Household & settings** to describe your family, then **Import statements**.")
    h = analysis.health(con, conv, tx)
    c = st.columns(5)
    c[0].metric("Avg monthly income", eur(h["avg_income"]), f"${h['avg_income'] * USD_NOW:,.0f}", delta_color="off", delta_arrow="off")
    c[1].metric("Avg monthly spending", eur(h["avg_expenses"]), f"${h['avg_expenses'] * USD_NOW:,.0f}", delta_color="off", delta_arrow="off")
    c[2].metric("Savings rate", pct(h["savings_rate"]),
                help="Income minus spending, divided by income. Transfers between your own accounts are excluded.")
    c[3].metric("Emergency fund", f"{h['emergency_months']:.1f} mo" if h["emergency_months"] else "—",
                help="Checking + savings balances divided by average monthly spending. 3–6 months is the usual target.")
    nw = analysis.net_worth_series(con, conv)
    c[4].metric("Net worth", eur(nw["net_worth"].iloc[-1]) if len(nw) else "—",
                f"${nw['net_worth'].iloc[-1] * USD_NOW:,.0f}" if len(nw) else None, delta_color="off", delta_arrow="off")

    st.subheader(f"What to look at for {YEAR}")
    ops = expat.opportunities(con, conv, profile, tx, YEAR)
    icon = {"act": "🔴", "check": "🟡", "info": "🔵"}
    if not ops:
        st.success("Nothing flagged. Add more data or fill in your household details for better checks.")
    for o in ops:
        with st.expander(f"{icon[o['severity']]} **[{o['area']}]** {o['title']}", expanded=o["severity"] == "act"):
            st.markdown(o["body"])
    st.caption("🔴 act on it · 🟡 worth checking · 🔵 good to know. These are estimates, not tax advice; "
               "a cross-border (US/NL) tax adviser should confirm anything you file.")

    ms = analysis.monthly_summary(tx)
    if len(ms):
        SURPLUS, DEFICIT, INK, GRID = "#2a78d6", "#e34948", "#52514e", "rgba(0,0,0,0.08)"
        head = st.columns([3, 2, 1])
        head[0].subheader("Cash flow")
        try:
            saved_view = db.get_setting(con, "cashflow_view", "Net")
        except Exception:  # noqa: BLE001
            saved_view = "Net"
        views = ["Net", "Income vs spending"]
        view = head[1].segmented_control("View", views, default=saved_view if saved_view in views else "Net",
                                         label_visibility="collapsed", key="cf_view") or "Net"
        tg = head[2].container()
        in_usd = tg.toggle("USD", key="cf_usd")
        smooth = tg.toggle("Spread costs", key="cf_spread", value=True,
                           help="On: payments you set to spread (e.g. a yearly school fee) count as their monthly share, "
                                "like on the Budget page. Off: the actual cash that moved each month.")
        if smooth:
            tx = analysis.spread_view(tx)
            tx = tx[(tx["month"] >= ms["month"].min()) & (tx["month"] <= ms["month"].max())]
            ms = analysis.monthly_summary(tx)
        if view != saved_view:
            db.set_setting(con, "cashflow_view", view)
        k, sym = (USD_NOW, "$") if in_usd else (1.0, "€")
        m = ms.assign(income=ms["income"] * k, expenses=ms["expenses"] * k, net=ms["net"] * k)
        m["label"] = m["month"].dt.strftime("%b %Y")

        fig = go.Figure()
        if view == "Net":
            pos = m["net"] >= 0
            fig.add_bar(x=m["month"], y=m["net"], marker_color=[SURPLUS if p else DEFICIT for p in pos],
                        marker_line_width=0, showlegend=False,
                        customdata=m[["income", "expenses", "label"]].values,
                        hovertemplate="<b>%{customdata[2]}</b><br>Net " + sym + "%{y:,.0f}<br>"
                                      "Income " + sym + "%{customdata[0]:,.0f} · Spending " + sym + "%{customdata[1]:,.0f}"
                                      "<extra></extra>")
            avg = m["net"].mean()
            fig.add_hline(y=avg, line_dash="dot", line_width=1.5, line_color=INK,
                          annotation_text=f"avg {sym}{avg:,.0f}/mo", annotation_position="top left",
                          annotation_font_color=INK, annotation_font_size=12)
            fig.add_hline(y=0, line_width=1, line_color="rgba(0,0,0,0.35)")
            # label only the best and worst month, not every bar
            for idx, pos_label in ((m["net"].idxmax(), "top center"), (m["net"].idxmin(), "bottom center")):
                if len(m) > 1:
                    v = m.at[idx, "net"]
                    fig.add_annotation(x=m.at[idx, "month"], y=v, text=f"{sym}{v:,.0f}", showarrow=False,
                                       yshift=12 if v >= 0 else -12, font=dict(color=INK, size=12))
        else:
            fig.add_bar(x=m["month"], y=m["income"], name="Income", marker_color=SURPLUS, marker_line_width=0,
                        customdata=m["label"], hovertemplate="<b>%{customdata}</b><br>Income " + sym + "%{y:,.0f}<extra></extra>")
            fig.add_bar(x=m["month"], y=-m["expenses"], name="Spending", marker_color=DEFICIT, marker_line_width=0,
                        customdata=m[["expenses", "label"]].values,
                        hovertemplate="<b>%{customdata[1]}</b><br>Spending " + sym + "%{customdata[0]:,.0f}<extra></extra>")
            fig.add_scatter(x=m["month"], y=m["net"], name="Net", mode="lines+markers",
                            line=dict(color=INK, width=2), marker=dict(size=8, color=INK, line=dict(color="#fcfcfb", width=2)),
                            customdata=m["label"], hovertemplate="<b>%{customdata}</b><br>Net " + sym + "%{y:,.0f}<extra></extra>")
            fig.add_hline(y=0, line_width=1, line_color="rgba(0,0,0,0.35)")
        fig.update_layout(barmode="relative", bargap=0.3, barcornerradius=4, height=360,
                          margin=dict(t=10, b=10, l=10, r=10), hovermode="closest",
                          legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                          yaxis=dict(title=dict(text="USD" if in_usd else "EUR", font=dict(color=INK, size=12)), tickformat=",.0f", gridcolor=GRID, zeroline=False),
                          xaxis=dict(title=None, tickformat="%b %y", dtick="M1", showgrid=False),
                          plot_bgcolor="rgba(0,0,0,0)", clickmode="event+select", dragmode=False)
        ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points", key=f"cf_chart_{view}",
                             config={"displayModeBar": False})
        clicked = None
        try:
            pts = ev.selection.points if ev else []
            if pts:
                clicked = pd.Timestamp(pts[0]["x"]).to_period("M").to_timestamp()
                curve = pts[0].get("curve_number", pts[0].get("curveNumber"))
                clicked_flow = {0: "Income", 1: "Outflow"}.get(curve, "All") if view != "Net" else "All"
        except Exception:  # noqa: BLE001
            clicked = None
        # apply a click once; afterwards the month picker below is free to change it
        if clicked is not None and st.session_state.get("drill_last_click") != clicked:
            st.session_state["drill_last_click"] = clicked
            st.session_state["drill_select"] = clicked
            st.session_state["drill_flow"] = clicked_flow

        if view == "Net":
            n_pos = int((m["net"] >= 0).sum())
            c = st.columns(4)
            c[0].metric("Average net / month", f"{sym}{m['net'].mean():,.0f}")
            c[1].metric("Months in surplus", f"{n_pos} of {len(m)}")
            c[2].metric("Best month", f"{sym}{m['net'].max():,.0f}", m.loc[m['net'].idxmax(), 'label'], delta_color="off", delta_arrow="off")
            c[3].metric("Worst month", f"{sym}{m['net'].min():,.0f}", m.loc[m['net'].idxmin(), 'label'], delta_color="off", delta_arrow="off")
        with st.expander("Table"):
            t = m[["label", "income", "expenses", "net"]].rename(columns={"label": "Month", "income": "Income",
                                                                           "expenses": "Spending", "net": "Net"})
            fmt = "dollar" if in_usd else "euro"
            st.dataframe(t, hide_index=True, width="stretch",
                         column_config={c: st.column_config.NumberColumn(c, format=fmt) for c in ("Income", "Spending", "Net")})
        st.caption("Transfers between your own accounts are left out. "
                   + ("Spread payments count as their monthly share (switch off *Spread costs* for actual cash). "
                      if smooth else "Shows actual cash; spread payments count in full in the month paid. ")
                   + "**Click a bar** to see that month's transactions.")

        # ---------------------------------------------------------- month drill-down
        months = list(m["month"])
        if st.session_state.get("drill_select") not in months:
            st.session_state["drill_select"] = months[-1]
        st.subheader("Month detail")
        d = st.columns([2, 2, 2])
        month = d[0].selectbox("Month", months, format_func=lambda x: x.strftime("%B %Y"), key="drill_select")
        flow = d[1].segmented_control("Show", ["All", "Income", "Outflow"], default="All", key="drill_flow") or "All"
        incl_all = d[2].toggle("Include transfers", key="drill_all",
                               help="Transfers are left out of the chart; switch on to see every transaction that month.")

        mt = tx[tx["month"] == month].copy()
        base = mt if incl_all else mt[mt["budget"]]
        inc = base.loc[base["eur"] > 0, "eur"].sum() * k
        out = -base.loc[base["eur"] < 0, "eur"].sum() * k
        mm = st.columns(4)
        mm[0].metric("Income", f"{sym}{inc:,.0f}")
        mm[1].metric("Outflow", f"{sym}{out:,.0f}")
        mm[2].metric("Net", f"{sym}{inc - out:,.0f}")
        mm[3].metric("Transactions", f"{len(base):,}")

        view_tx = base if flow == "All" else base[base["eur"] > 0] if flow == "Income" else base[base["eur"] < 0]
        if view_tx.empty:
            st.caption("Nothing here for this month.")
        else:
            left_col, right_col = st.columns([1.25, 3.75])
            with left_col:
                cats = (view_tx.assign(v=view_tx["eur"].abs() * k).groupby("category")["v"].sum()
                        .sort_values(ascending=False).reset_index())
                tot = cats["v"].sum()
                cats["share"] = cats["v"] / tot if tot else 0
                st.markdown("**By category**")
                st.dataframe(cats, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(cats)),
                             column_config={"category": "Category",
                                            "v": st.column_config.NumberColumn("Amount", format="dollar" if in_usd else "euro"),
                                            "share": st.column_config.ProgressColumn("Share", format="percent",
                                                                                     min_value=0, max_value=1, width="small")})
            with right_col:
                st.markdown("**Transactions** · biggest first. Change a category or set a spread, then save.")
                show = view_tx.assign(amt=view_tx["eur"] * k)
                if "part" not in show:
                    show["part"] = ""
                show = show.reindex(show["amt"].abs().sort_values(ascending=False).index)
                show = show[["id", "date", "payee", "description", "account", "amt", "part", "category", "spread"]].copy()
                show["date"] = show["date"].dt.date
                ed = st.data_editor(show, hide_index=True, width="stretch", height=420,
                                    key=f"drill_ed_{month:%Y%m}_{flow}_{incl_all}_{smooth}",
                                    disabled=["date", "payee", "description", "account", "amt", "part"],
                                    column_config={"id": None, "date": st.column_config.DateColumn("Date", format="DD MMM"),
                                                   "payee": st.column_config.TextColumn("Payee", width="medium"),
                                                   "description": st.column_config.TextColumn("Description", width="small"),
                                                   "account": st.column_config.TextColumn("Account", width="small"),
                                                   "amt": st.column_config.NumberColumn(sym, format="dollar" if in_usd else "euro"),
                                                   "part": st.column_config.TextColumn("Part", width="small",
                                                                                       help="Share of a spread payment, e.g. 3/12"),
                                                   "category": st.column_config.SelectboxColumn("Category", options=category_options(), required=True),
                                                   "spread": spread_column(show)})
                changed = edited_rows(ed, show)
                if st.button(f"Save {len(changed)} changes", type="primary", disabled=changed.empty, key="drill_save"):
                    save_tx_edits(changed, show.drop_duplicates("id").set_index("id"))
                    st.rerun()


# ================================================================== Import
elif page == "Import statements":
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
            from finapp import ui_options
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


# ================================================================== Review & categorise
elif page == "Review & categorise":
    st.header("Review & categorise")
    st.markdown("Payments are grouped by payee, so you set the category once per payee instead of once per transaction. "
                "Tick **Rule** to apply it to future imports too.")
    tx = load_tx()
    if tx.empty:
        st.info("Nothing imported yet.")
        st.stop()
    REVIEW = ("Uncategorized", "Income: Other")
    f = st.columns([2, 2, 3])
    scope = f[0].radio("Show", ["Needs a category", "All payees"], horizontal=True)
    direction = f[1].radio("Money", ["Out", "In", "Both"], horizontal=True)
    q = f[2].text_input("Search payee")
    v = tx
    if direction == "Out":
        v = v[v["eur"] < 0]
    elif direction == "In":
        v = v[v["eur"] > 0]
    if q:
        v = v[(v["payee"] + " " + v["description"]).str.contains(q, case=False, regex=False)]

    g = (v.groupby("payee")
          .agg(n=("id", "size"), total=("eur", "sum"), typical=("eur", "median"),
               first=("date", "min"), last=("date", "max"),
               category=("category", lambda s: s.mode().iloc[0] if len(s.mode()) else "Uncategorized"),
               mixed=("category", "nunique"), needs=("category", lambda s: s.isin(REVIEW).sum()),
               example=("description", "first"))
          .reset_index())
    if scope == "Needs a category":
        g = g[g["needs"] > 0]
    g = g.sort_values("total", key=lambda s: s.abs(), ascending=False)
    if g.empty:
        st.success("Everything has a category. 🎉" if scope == "Needs a category" else "No matching payees.")
        st.stop()
    left = int(tx["category"].isin(REVIEW).sum())
    st.caption(f"{len(g)} payees shown · {left} transactions still need a category overall. Biggest amounts first.")

    # ---------------------------------------------------------- Claude suggestions
    ai_cfg = db.get_setting(con, "ai", {}) or {}
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ai_cfg.get("api_key")
    with st.expander("✨ Suggest categories with Claude", expanded=bool(st.session_state.get("ai_sugg"))):
        if True:
            incl = bool(ai_cfg.get("include_amounts", False))
            items = ai.build_items(tx, REVIEW, incl)
            if not items:
                st.caption("Nothing needs a category right now.")
            else:
                st.markdown("**What will be sent:** only the payee names below (numbers, dates, card and account "
                            "references already removed), whether each is money in or out"
                            + (", a rounded typical amount" if incl else "")
                            + ", and your list of category names. No dates, balances, IBANs, account names, notes "
                            "or household details. Untick anything you'd rather not send.")
                prev = pd.DataFrame(items)
                cols = ["send", "send_name", "direction"] + (["send_amount"] if incl else []) + ["n", "flag", "payee"]
                ped = st.data_editor(prev[cols], hide_index=True, width="stretch", height=300, key="ai_prev",
                                     disabled=[c for c in cols if c != "send"],
                                     column_config={"send": st.column_config.CheckboxColumn("Send"),
                                                    "send_name": st.column_config.TextColumn("Sent as"),
                                                    "direction": "In/out",
                                                    "send_amount": st.column_config.NumberColumn("Amount sent (€, rounded)"),
                                                    "n": st.column_config.NumberColumn("# txns", help="Stays on your computer"),
                                                    "flag": "Note", "payee": None})
                for it, s in zip(items, ped["send"]):
                    it["send"] = bool(s)
                n_send = sum(it["send"] for it in items)
                chat_mode, api_mode = st.tabs(["Copy & paste (uses your Claude subscription)", "Claude API (automatic)"])
                with chat_mode:
                    st.markdown(f"**1.** Copy this text (copy icon, top right of the box) and paste it into a new chat at "
                                f"claude.ai. It holds the {n_send} ticked payees above and nothing else.")
                    st.code(ai.chat_prompt(items, category_options(), incl), language=None, height=180, wrap_lines=True)
                    st.markdown("**2.** Copy Claude's whole reply and paste it here.")
                    reply = st.text_area("Claude's reply", height=140, key="ai_reply",
                                         placeholder='```json\n{"suggestions": [ … ]}\n```', label_visibility="collapsed")
                    if st.button("Read suggestions", type="primary", disabled=not reply.strip()):
                        try:
                            st.session_state["ai_sugg"] = ai.parse_chat_reply(reply, items, category_options(), incl)
                        except ai.AIError as e:
                            st.error(str(e))
                            st.stop()
                        log = db.get_setting(con, "ai_log", []) or []
                        log.append(ai.log_entry(ai.payload_preview(items, incl), "claude.ai chat (copy & paste)", {}))
                        db.set_setting(con, "ai_log", log[-50:])
                        st.rerun()
                    st.caption("Tip: in claude.ai, Settings → Privacy controls whether chats can be used to improve "
                               "Anthropic's models. Turn it off if you'd rather they weren't.")
                with api_mode:
                    if not api_key:
                        st.markdown("Add an Anthropic API key on **Household & settings** to send straight from the app. "
                                    "API usage is billed separately from a Claude subscription.")
                with api_mode, st.popover("See the exact request text", disabled=not api_key):
                    st.json({"categories": category_options(), "payees": ai.payload_preview(items, incl)})
                if api_key and api_mode.button(f"Send {n_send} payee names to Claude", type="primary", disabled=n_send == 0):
                    model = ai_cfg.get("model") or ai.DEFAULT_MODEL
                    with st.spinner("Asking Claude…"):
                        try:
                            sugg, usage = ai.suggest(api_key, items, category_options(), incl, model)
                        except ai.AIError as e:
                            st.error(str(e))
                            st.stop()
                    log = db.get_setting(con, "ai_log", []) or []
                    log.append(ai.log_entry(ai.payload_preview(items, incl), model, usage))
                    db.set_setting(con, "ai_log", log[-50:])
                    st.session_state["ai_sugg"] = sugg
                    st.rerun()

        sugg = st.session_state.get("ai_sugg") or {}
        if sugg:
            st.divider()
            st.markdown("**Claude's suggestions.** Nothing is saved until you click Apply. Change any category you disagree with.")
            sdf = pd.DataFrame([{"payee": p, **s} for p, s in sugg.items()])
            sdf = sdf.merge(g[["payee", "n", "total"]], on="payee", how="left") if len(g) else sdf.assign(n=None, total=None)
            sdf["accept"] = sdf["confidence"].isin(["high", "medium"]) & (sdf["category"] != "Uncategorized")
            sdf["rule"] = True
            sdf["new"] = sdf["new"].map(lambda x: "new category" if x else "")
            sopts = sorted(set(category_options()) | set(sdf["category"]))
            sed = st.data_editor(sdf[["accept", "payee", "category", "confidence", "reason", "new", "rule", "n", "total"]],
                                 hide_index=True, width="stretch", key="ai_res",
                                 disabled=["payee", "confidence", "reason", "new", "n", "total"],
                                 column_config={"accept": st.column_config.CheckboxColumn("Apply"),
                                                "category": st.column_config.SelectboxColumn("Category", options=sopts, required=True),
                                                "rule": st.column_config.CheckboxColumn("Rule"),
                                                "total": st.column_config.NumberColumn("Total", format="euro"),
                                                "n": "#", "new": ""})
            acc = sed[sed["accept"]]
            b = st.columns([2, 1, 3])
            if b[0].button(f"Apply {len(acc)} suggestions", type="primary", disabled=acc.empty):
                ids_by_payee = tx.groupby("payee")["id"].apply(list)
                custom = set(db.get_setting(con, "custom_categories", []) or [])
                for _, r in acc.iterrows():
                    ids = [int(i) for i in ids_by_payee.get(r["payee"], [])]
                    con.executemany("UPDATE transactions SET category=?, category_locked=1 WHERE id=? "
                                    "AND category IN ('Uncategorized','Income: Other')", [(r["category"], i) for i in ids])
                    if r["rule"]:
                        pat = rule_pattern(r["payee"])
                        con.execute("DELETE FROM cat_rules WHERE pattern=?", (pat,))
                        con.execute("INSERT INTO cat_rules(pattern, category, priority) VALUES(?,?,50)", (pat, r["category"]))
                    custom.add(r["category"])
                con.commit()
                db.set_setting(con, "custom_categories", sorted(custom - set(categorize.all_categories(con))))
                st.session_state.pop("ai_sugg", None)
                st.toast(f"Applied {len(acc)} suggestions")
                st.rerun()
            if b[1].button("Discard"):
                st.session_state.pop("ai_sugg", None)
                st.rerun()

    g["rule"] = True
    g["category_new"] = g["category"]
    opts = category_options()
    ed = st.data_editor(
        g[["payee", "n", "total", "typical", "last", "category_new", "rule", "example", "category"]],
        hide_index=True, width="stretch", height=520, key="review_ed",
        disabled=["payee", "n", "total", "typical", "last", "example", "category"],
        column_config={
            "payee": st.column_config.TextColumn("Payee", width="medium"),
            "n": st.column_config.NumberColumn("#", help="Number of transactions"),
            "total": st.column_config.NumberColumn("Total", format="euro"),
            "typical": st.column_config.NumberColumn("Typical", format="euro"),
            "last": st.column_config.DateColumn("Last"),
            "category_new": st.column_config.SelectboxColumn("Category", options=opts, required=True, width="medium"),
            "rule": st.column_config.CheckboxColumn("Rule", help="Also categorise this payee automatically in future imports"),
            "example": st.column_config.TextColumn("Example description", width="large"),
            "category": None,
        })
    changed = ed[ed["category_new"] != ed["category"]]
    c = st.columns([2, 3])
    if c[0].button(f"Save {len(changed)} payee{'s' if len(changed) != 1 else ''}", type="primary", disabled=changed.empty):
        n_tx = n_rules = 0
        ids_by_payee = v.groupby("payee")["id"].apply(list)
        for _, r in changed.iterrows():
            ids = ids_by_payee.get(r["payee"], [])
            con.executemany("UPDATE transactions SET category=?, category_locked=1 WHERE id=?",
                            [(r["category_new"], int(i)) for i in ids])
            n_tx += len(ids)
            if r["rule"]:
                pat = rule_pattern(r["payee"])
                con.execute("DELETE FROM cat_rules WHERE pattern=?", (pat,))
                con.execute("INSERT INTO cat_rules(pattern, category, priority) VALUES(?,?,50)", (pat, r["category_new"]))
                n_rules += 1
        con.commit()
        st.toast(f"Updated {n_tx} transactions · saved {n_rules} rules")
        st.rerun()
    c[1].caption("Saved categories are locked, so re-running the automatic rules never overwrites them.")
    add_category_box("review")

    with st.expander("Payee has a mix of things (e.g. Bol.com, Amazon)? Categorise single transactions"):
        pick = st.selectbox("Payee", g["payee"].tolist())
        sub = v[v["payee"] == pick][["id", "date", "description", "eur", "category", "spread"]].copy()
        sub["date"] = sub["date"].dt.date
        sed = st.data_editor(sub, hide_index=True, width="stretch", key="review_sub",
                             disabled=["date", "description", "eur"],
                             column_config={"id": None, "eur": st.column_config.NumberColumn("EUR", format="euro"),
                                            "category": st.column_config.SelectboxColumn("Category", options=opts, required=True),
                                            "spread": spread_column(sub)})
        diff = edited_rows(sed, sub)
        if st.button(f"Save {len(diff)} changes", key="save_sub", disabled=diff.empty):
            save_tx_edits(diff, sub.set_index("id").assign(category=None))   # categories set here are locked
            st.rerun()


# ================================================================== Transactions
elif page == "Transactions":
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
        pat = rc[0].text_input("Text or regex", placeholder="e.g. partou|smallsteps")
        cat = rc[1].selectbox("Category", all_cats)
        new_cat = rc[1].text_input("…or a new category (Group: Sub)")
        if rc[2].button("Add & re-run"):
            con.execute("INSERT INTO cat_rules(pattern, category) VALUES(?, ?)", (pat, new_cat or cat))
            con.commit()
            n = categorize.apply(con)
            st.success(f"Re-categorised {n} transactions (hand-edited ones kept).")


# ================================================================== Budget
elif page == "Budget":
    st.header("Budget")
    tx_actual = load_tx()
    if tx_actual.empty:
        st.info("Import some statements first.")
        st.stop()
    months = sorted(pd.Timestamp(m) for m in tx_actual["month"].unique())
    latest = months[-1]
    # budget view: spread payments become monthly parts (only months you have statements for)
    tx = analysis.spread_view(tx_actual)
    tx = tx[(tx["month"] >= months[0]) & (tx["month"] <= latest)]

    # ---------------------------------------------------------- period picker (replaces the range slider)
    presets = ["Latest month", "Previous month", "Last 3 months", "Last 6 months", "Last 12 months", "Year to date", "Custom"]
    pend = st.session_state.pop("bud_pending", None)
    if pend:   # a click on a month outside the period widens it (applied before the widgets are drawn)
        st.session_state.update(pend)
    preset = st.segmented_control("Period", presets,
                                     default=None if "bud_preset" in st.session_state else "Last 12 months",
                                     key="bud_preset") or "Last 12 months"
    if preset == "Latest month":
        p_from = p_to = latest
    elif preset == "Previous month":
        p_from = p_to = months[-2] if len(months) > 1 else latest
    elif preset.startswith("Last "):
        n = int(preset.split()[1])
        p_from, p_to = months[max(0, len(months) - n)], latest
    elif preset == "Year to date":
        p_from, p_to = next((m for m in months if m.year == latest.year), latest), latest
    else:
        fmt = lambda m: m.strftime("%b %Y")
        pc = st.columns([1, 1, 2])
        p_from = pc[0].selectbox("From", months, index=max(0, len(months) - 3), format_func=fmt, key="bud_from")
        p_to = pc[1].selectbox("To", [m for m in months if m >= p_from], index=len([m for m in months if m >= p_from]) - 1,
                               format_func=fmt, key="bud_to")
    v = tx[(tx["month"] >= p_from) & (tx["month"] <= p_to)]
    nm = max(1, v["month"].nunique())
    st.caption(f"Showing **{p_from:%b %Y}" + (f" – {p_to:%b %Y}**" if p_to != p_from else "**") +
               f" ({nm} month{'s' if nm != 1 else ''}). Amounts below are **per month** averages over this period.")
    ms = analysis.monthly_summary(v)

    c = st.columns(4)
    for col, label, val in ((c[0], "Income / month", ms["income"].mean()), (c[1], "Spending / month", ms["expenses"].mean()),
                            (c[2], "Net / month", ms["net"].mean())):
        col.metric(label, eur(val), f"${val * USD_NOW:,.0f}", delta_color="off", delta_arrow="off")
    c[3].metric("Savings rate", pct(ms["net"].sum() / ms["income"].sum() if ms["income"].sum() else None))
    sc = analysis.spread_costs(tx_actual[tx_actual["budget"]])
    if len(sc):
        sc = sc[(sc["covers_to"] >= p_from) & (sc["covers_from"] <= p_to)]
    if len(sc):
        in_p = v[v["id"].isin(sc["id"]) & (v["part"] != "")].groupby("id")["eur"].sum()
        sc = sc.assign(in_period=sc["id"].map(in_p).fillna(0.0))
        now = sc[(sc["covers_from"] <= p_to) & (sc["covers_to"] >= p_to)]
        with st.expander(f"Spread payments: {len(sc)} in this period · "
                         f"{eur(-now['per_month'].sum())} a month in {p_to:%b %Y}"):
            st.caption("These count as an equal share each month they cover instead of all at once. "
                       "The per-month total is roughly what to set aside to pay them without a dent in one month. "
                       "Change or remove a spread on the Transactions page (Show → *Spread payments*).")
            o = sc[["date", "payee", "category", "eur", "spread", "per_month", "covers_from", "covers_to", "in_period"]].copy()
            o["date"] = o["date"].dt.date
            o["covers"] = [f"{a:%b %Y} – {b:%b %Y}" for a, b in zip(o["covers_from"], o["covers_to"])]
            for c_ in ("eur", "per_month", "in_period"):
                o[c_] = -o[c_]
            st.dataframe(o.drop(columns=["covers_from", "covers_to"]), hide_index=True, width="stretch",
                         column_order=["date", "payee", "category", "eur", "spread", "covers", "per_month", "in_period"],
                         column_config={"date": st.column_config.DateColumn("Paid"), "payee": "Payee", "category": "Category",
                                        "eur": st.column_config.NumberColumn("Paid €", format="euro"),
                                        "spread": "Spread", "covers": "Covers",
                                        "per_month": st.column_config.NumberColumn("Per month", format="euro"),
                                        "in_period": st.column_config.NumberColumn("In this period", format="euro")})

    # ---------------------------------------------------------- where the money goes (click a bar to drill in)
    cat = analysis.by_category(v)
    cat["per_month"] = cat["eur"] / nm
    st.subheader("Where the money goes")
    if cat.empty:
        st.caption("No spending in this period.")
    else:
        groups = sorted(cat["group"].unique())
        palette = dict(zip(groups, px.colors.qualitative.Safe * 3))
        cat = cat.sort_values("per_month", ascending=False).reset_index(drop=True)
        figc = go.Figure()
        for gname in groups:
            gg = cat[cat["group"] == gname]
            figc.add_bar(y=gg["category"], x=gg["per_month"], orientation="h", name=gname, marker_color=palette[gname],
                         marker_line_width=0, customdata=gg[["eur"]].values,
                         hovertemplate="<b>%{y}</b><br>€%{x:,.0f} per month · €%{customdata[0]:,.0f} in total"
                                       "<br><i>click to see transactions</i><extra></extra>")
        figc.update_layout(height=max(260, 26 * len(cat) + 60), barmode="stack", bargap=0.25, barcornerradius=3,
                           margin=dict(t=10, b=10, l=10, r=10), plot_bgcolor="rgba(0,0,0,0)",
                           legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0, title=None),
                           yaxis=dict(categoryorder="array", categoryarray=list(cat["category"])[::-1], title=None),
                           xaxis=dict(title=dict(text="€ per month", font=dict(size=12)), tickformat=",.0f",
                                      gridcolor="rgba(0,0,0,0.08)"),
                           clickmode="event+select", dragmode=False)
        evc = st.plotly_chart(figc, width="stretch", on_select="rerun", selection_mode="points",
                              key=f"bud_cat_chart_{p_from:%Y%m}_{p_to:%Y%m}", config={"displayModeBar": False})
        try:
            pts = evc.selection.points if evc else []
            clicked_cat = pts[0].get("y") if pts else None
        except Exception:  # noqa: BLE001
            clicked_cat = None
        cats = list(cat["category"])
        if clicked_cat and clicked_cat in cats and st.session_state.get("bud_cat_click") != clicked_cat:
            st.session_state["bud_cat_click"] = clicked_cat
            st.session_state["bud_cat"] = clicked_cat
            st.session_state.pop("bud_month", None)
        if st.session_state.get("bud_cat") not in cats:
            st.session_state["bud_cat"] = cats[0]

        # ---------------------------------------------------------- category detail
        with st.container(border=True):
            dc = st.columns([2.5, 2, 1.5], vertical_alignment="bottom")
            sel_cat = dc[0].selectbox("Category", cats, key="bud_cat",
                                      help="Click a bar above, or pick here.")
            ctx = tx[(tx["category"] == sel_cat) & (tx["budget"]) & (tx["eur"] < 0)]
            # month-by-month history of this category (all months on file, period highlighted)
            hist = (ctx.groupby("month")["eur"].sum().abs().reindex(months, fill_value=0.0)).reset_index()
            hist.columns = ["month", "amt"]
            per_months = [m for m in months if p_from <= m <= p_to]
            mopts = ["All months in period"] + [m for m in sorted(per_months, reverse=True)]
            if st.session_state.get("bud_month") not in mopts:
                st.session_state["bud_month"] = per_months[-1] if len(per_months) == 1 else "All months in period"
            avg = hist.loc[hist["month"].isin(per_months), "amt"].mean()
            dc[2].metric("Average per month", eur(avg))

            figm = go.Figure()
            sel_m = st.session_state.get("bud_month")
            colors = ["#2a78d6" if (m in per_months and (sel_m == "All months in period" or m == sel_m)) else "#c9d6ea"
                      for m in hist["month"]]
            figm.add_bar(x=hist["month"], y=hist["amt"], marker_color=colors, marker_line_width=0,
                         hovertemplate="%{x|%b %Y}: €%{y:,.0f}<br><i>click to see that month</i><extra></extra>")
            figm.add_hline(y=avg, line_dash="dot", line_width=1.5, line_color="#52514e",
                           annotation_text=f"avg {eur(avg)}", annotation_position="top left", annotation_font_size=11)
            figm.update_layout(height=220, bargap=0.3, barcornerradius=3, margin=dict(t=10, b=10, l=10, r=10),
                               plot_bgcolor="rgba(0,0,0,0)", showlegend=False, clickmode="event+select", dragmode=False,
                               yaxis=dict(tickformat=",.0f", gridcolor="rgba(0,0,0,0.08)", title=None),
                               xaxis=dict(tickformat="%b %y", dtick="M1", title=None))
            evm = st.plotly_chart(figm, width="stretch", on_select="rerun", selection_mode="points",
                                  key=f"bud_month_chart_{sel_cat}", config={"displayModeBar": False})
            try:
                mp = evm.selection.points if evm else []
                clicked_m = pd.Timestamp(mp[0]["x"]).to_period("M").to_timestamp() if mp else None
            except Exception:  # noqa: BLE001
                clicked_m = None
            if clicked_m is not None and st.session_state.get("bud_month_click") != (sel_cat, clicked_m):
                st.session_state["bud_month_click"] = (sel_cat, clicked_m)
                if clicked_m not in per_months:      # clicked outside the period: widen to a custom period
                    st.session_state["bud_pending"] = {"bud_preset": "Custom", "bud_from": min(clicked_m, p_from),
                                                       "bud_to": max(clicked_m, p_to)}
                st.session_state["bud_month"] = clicked_m
                st.rerun()
            sel_m = dc[1].selectbox("Month", mopts, key="bud_month",
                                    format_func=lambda m: m if isinstance(m, str) else m.strftime("%B %Y"))

            show_tx = ctx[ctx["month"].isin(per_months)] if sel_m == "All months in period" else ctx[ctx["month"] == sel_m]
            st.caption(f"**{len(show_tx)} transaction{'' if len(show_tx) == 1 else 's'}** · {eur(-show_tx['eur'].sum())} "
                       f"in {sel_m if isinstance(sel_m, str) else sel_m.strftime('%B %Y')}. Change a category or set a "
                       "spread, then save. *Part* shows a monthly share of a spread payment.")
            if len(show_tx):
                tshow = show_tx.sort_values("date", ascending=False)[["id", "date", "payee", "description", "account", "eur",
                                                                       "part", "category", "spread"]].copy()
                tshow["date"] = tshow["date"].dt.date
                tshow["eur"] = -tshow["eur"]
                ted = st.data_editor(tshow, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(tshow)),
                                     key=f"bud_tx_{sel_cat}_{sel_m}", disabled=["date", "payee", "description", "account", "eur", "part"],
                                     column_config={"id": None, "date": st.column_config.DateColumn("Date", format="DD MMM YYYY"),
                                                    "payee": "Payee", "description": st.column_config.TextColumn("Description", width="medium"),
                                                    "account": "Account", "eur": st.column_config.NumberColumn("€", format="euro"),
                                                    "part": st.column_config.TextColumn("Part", width="small",
                                                                                        help="Share of a spread payment, e.g. 3/12"),
                                                    "category": st.column_config.SelectboxColumn("Category", options=category_options(),
                                                                                                 required=True),
                                                    "spread": spread_column(tshow)})
                chg = edited_rows(ted, tshow).drop_duplicates("id", keep="last")
                if st.button(f"Save {len(chg)} changes", type="primary", disabled=chg.empty, key="bud_tx_save"):
                    save_tx_edits(chg, tshow.drop_duplicates("id").set_index("id"))
                    st.rerun()

    # ---------------------------------------------------------- targets per group
    with st.expander("Monthly budget targets per group"):
        st.caption("Set a target per group; the table shows this period's average against it.")
        targets = db.get_setting(con, "budget_targets", {}) or {}
        grp = cat.groupby("group")["per_month"].sum().sort_values(ascending=False).reset_index() if len(cat) else \
            pd.DataFrame(columns=["group", "per_month"])
        grp["target"] = grp["group"].map(lambda g: targets.get(g))
        grp["vs target"] = grp["per_month"] - grp["target"]
        ed = st.data_editor(grp, hide_index=True, width="stretch", disabled=["group", "per_month", "vs target"],
                            column_config={"group": "Group", "per_month": st.column_config.NumberColumn("Actual €/mo", format="euro"),
                                           "target": st.column_config.NumberColumn("Target €/mo", format="euro"),
                                           "vs target": st.column_config.NumberColumn("Over (+) / under", format="€%+.0f")})
        if st.button("Save targets"):
            db.set_setting(con, "budget_targets", {r["group"]: r["target"] for _, r in ed.iterrows() if pd.notna(r["target"])})
            st.rerun()

    from finapp import ui_whatif
    ui_whatif.render(con, conv, v, nm, f"{p_from:%b %Y}" + (f" – {p_to:%b %Y}" if p_to != p_from else ""))

    st.subheader("Spending by group over time")
    trend = v[(v["budget"]) & (v["eur"] < 0)].groupby(["month", "group"])["eur"].sum().abs().reset_index()
    if len(trend):
        fig = px.bar(trend, x="month", y="eur", color="group", color_discrete_sequence=px.colors.qualitative.Set2)
        fig.update_layout(height=360, yaxis_title="EUR", xaxis_title=None, margin=dict(t=10))
        st.plotly_chart(fig, width="stretch")

    # ---------------------------------------------------------- spending check
    from finapp import benchmarks as bm
    st.subheader("Where you might be overspending")
    local = bm.municipal_charges(profile)
    t_trend, t_dutch, t_local = st.tabs(["Rising vs. your own history", "Compared with Dutch households",
                                         f"{local['municipality']} local charges" if local else "Local charges"])
    with t_trend:
        tr = bm.trends(tx)
        if tr.empty:
            st.caption("Needs at least 6 months of statements.")
        else:
            up = tr[(tr["change"] > 25) & ((tr["pct"].isna()) | (tr["pct"] > 0.10))].head(8)
            st.caption(f"Average per month in the last 3 months ({tr['months_recent'].iloc[0]}) vs. the 9 months before. "
                       "Transfers are left out; spread payments count as their monthly share.")
            if up.empty:
                st.success("No category is noticeably up. Spending is in line with your own past months.")
            else:
                tot_up = up["change"].sum()
                st.markdown(f"**{len(up)} categories are up**, about **{eur(tot_up)} a month** more than before "
                            f"(≈ {eur(tot_up * 12)} a year).")
                st.dataframe(up[["category", "before", "recent", "change", "pct", "drivers"]], hide_index=True, width="stretch",
                             column_config={"category": "Category",
                                            "before": st.column_config.NumberColumn("Before /mo", format="euro"),
                                            "recent": st.column_config.NumberColumn("Last 3 mo /mo", format="euro"),
                                            "change": st.column_config.NumberColumn("Change /mo", format="€%+.0f"),
                                            "pct": st.column_config.NumberColumn("Change", format="percent"),
                                            "drivers": st.column_config.TextColumn("Mostly from", width="large")})
            down = tr[(tr["change"] < -25)].sort_values("change").head(3)
            if len(down):
                st.caption("Down: " + ", ".join(f"{r['category']} ({eur(r['change'])}/mo)" for _, r in down.iterrows()))

    with t_dutch:
        htype = bm.household_type(profile)
        spend = v[(v["budget"]) & (v["eur"] < 0)]
        if spend.empty:
            st.caption("No spending in this period.")
        else:
            cmp_ = bm.shares_vs_cbs(spend, htype, nm)
            st.caption(f"How your spending splits across five groups, against **{bm.HOUSEHOLD_LABEL[htype]}** in the "
                       "Netherlands (CBS 2020, the latest detailed survey). Shares, not amounts, so your income level "
                       "doesn't skew it. Childcare and insurance count as *Other*.")
            fig = go.Figure()
            fig.add_bar(y=cmp_["group"], x=cmp_["you_share"], orientation="h", name="You", marker_color="#2a78d6",
                        hovertemplate="You: %{x:.1f}%<extra></extra>")
            fig.add_bar(y=cmp_["group"], x=cmp_["cbs_share"], orientation="h", name=f"Dutch {bm.HOUSEHOLD_LABEL[htype]}",
                        marker_color="#b8b7b0", hovertemplate="CBS: %{x:.1f}%<extra></extra>")
            fig.update_layout(barmode="group", height=300, bargap=0.3, barcornerradius=3, margin=dict(t=10, b=10, l=10, r=10),
                              legend=dict(orientation="h", y=1.12, x=0), plot_bgcolor="rgba(0,0,0,0)",
                              xaxis=dict(ticksuffix="%", gridcolor="rgba(0,0,0,0.08)"), yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
            over = cmp_[cmp_["diff_pts"] > 3].sort_values("diff_pts", ascending=False)
            gsp = spend.assign(g=spend["category"].map(bm.cbs_group), amt=-spend["eur"])
            for _, r in over.iterrows():
                gi = bm.GROUPS.index(r["group"])
                top = (gsp[gsp["g"] == gi].groupby("category")["amt"].sum() / nm).sort_values(ascending=False).head(3)
                why = ", ".join(f"{c} {eur(a)}" for c, a in top.items())
                st.markdown(f"- **{r['group']}**: {r['you_share']:.0f}% of your spending vs. {r['cbs_share']:.0f}% typical, "
                            f"about **{eur(r['diff_month'])} a month** above the typical split. Mostly: {why} per month.")
            if over.empty:
                st.success("Your split is close to the typical Dutch household like yours.")
            gc = bm.groceries_check(v, profile, nm)
            st.markdown(f"**Groceries:** {eur(gc['you'])}/month. The median Dutch household spends about {eur(gc['median'])} "
                        f"(ABN AMRO bank data, 2024–25); Nibud's minimum for healthy home cooking for {gc['adults']} adult(s)"
                        + (f" and {gc['kids']} child(ren)" if gc["kids"] else "") + f" is about {eur(gc['nibud_min'])}.")
            st.caption("Sources: " + " · ".join(f"[{n}]({u})" for n, u in (bm.SOURCES["cbs_shares"], bm.SOURCES["groceries"])))

    with t_local:
        if not local:
            st.info("Set where you live on **Household & settings** (municipality, people at your address, and whether "
                    "you own your home) to check your municipal tax payments here.")
        else:
            ch = local
            st.caption(f"Expected yearly municipal charges for **{ch['municipality']}**"
                       + (f" ({ch['year']} tariffs, {ch['collector']})" if ch.get("year") else "")
                       + ", to check your *Taxes* payments. Change your situation on **Household & settings**.")
            yr_tx = tx[tx["date"].dt.year == YEAR]
            rx = ch["payee"] + "|gemeente"
            paid = -yr_tx.loc[(yr_tx["category"] == "Taxes") &
                              (yr_tx["counterparty"].str.contains(rx, case=False, na=False) |
                               yr_tx["description"].str.contains(rx, case=False, na=False)), "eur"].sum()
            res_n = ch["residents"]
            m3 = st.columns(4)
            m3[0].metric("Waste charge (afvalstoffenheffing)", eur(ch["afval"]), f"{res_n} {'person' if res_n == 1 else 'people'}",
                         delta_color="off", delta_arrow="off")
            m3[1].metric("Sewer charge (rioolheffing)", eur(ch["riool"]), "owners only", delta_color="off", delta_arrow="off")
            m3[2].metric("Property tax (OZB)", eur(ch["ozb"]), f"{ch['ozb_pct'] * 100:.4f}% of WOZ" if ch["owner"] else "owners only",
                         delta_color="off", delta_arrow="off")
            m3[3].metric(f"Expected {YEAR} / paid so far", eur(ch["total"]), f"paid {eur(paid)} to {ch['collector']}",
                         delta_color="off", delta_arrow="off")
            if paid > ch["total"] * 1.1 and paid > 0:
                st.warning(f"You've paid more to {ch['collector']} this year than these charges add up to. Check the assessment "
                           "(aanslag) for extra items like a dog tax, parking permit, or a wrong number of residents.")
            note = "Renters pay only the waste charge"
            note += f"; the water board ({ch['water_board']}) charges separately and isn't included." if ch.get("water_board") \
                else "; the water board charges separately and isn't included."
            st.caption((f"Source: [{ch['source'][0]}]({ch['source'][1]}). " if ch.get("source") else "Rates you entered. ") + note)

    st.subheader("Recurring payments")
    rec = analysis.recurring(tx_actual[(tx_actual["month"] >= p_from) & (tx_actual["month"] <= p_to)])
    if len(rec):
        st.caption(f"{len(rec)} payees charged you a similar amount in 3+ months · about {dual(rec['annual'].sum())} a year")
        st.dataframe(rec[["payee", "category", "avg", "annual", "months", "last"]], hide_index=True, width="stretch",
                     column_config={"avg": st.column_config.NumberColumn("Typical €", format="euro"),
                                    "annual": st.column_config.NumberColumn("€/year", format="euro"),
                                    "last": st.column_config.DateColumn("Last seen")})
    else:
        st.caption("Needs at least three months of data.")

    unc = v[v["category"] == "Uncategorized"]
    if len(unc):
        st.warning(f"{len(unc)} transaction{'' if len(unc) == 1 else 's'} ({eur(-unc['eur'].sum())}) "
                   f"{'is' if len(unc) == 1 else 'are'} uncategorized. Sort them out on **Review & categorise**.")


# ================================================================== Accounts & net worth
elif page == "Accounts & net worth":
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


# ================================================================== Stock options
elif page == "Stock options":
    from finapp import ui_options
    ui_options.render(con, conv)


# ================================================================== US filing
elif page == "US filing":
    st.header(f"US filing checks · {YEAR}")
    fs = profile["filing_status"]
    rate = conv.year_end_usd_per_eur(YEAR)
    st.caption(f"Filing status: **{fs}** (change on Household & settings). Converting at 31 Dec ECB rate {rate:.4f} USD/EUR"
               + (f", overridden to Treasury rate {profile['treasury_usd_per_eur']}" if profile.get("treasury_usd_per_eur") else "") + ".")

    f = expat.fbar_8938(con, conv, YEAR, fs, profile.get("treasury_usd_per_eur"))
    st.subheader("FBAR and Form 8938")
    c = st.columns(3)
    c[0].metric("Max combined foreign balances", f"${f['aggregate_max_usd']:,.0f}")
    c[1].metric("FBAR (> $10,000)", "Required" if f["fbar_required"] else "Not triggered")
    c[2].metric(f"Form 8938 (> ${f['f8938_thresholds'][0]:,.0f} / ${f['f8938_thresholds'][1]:,.0f})",
                "Likely required" if f["f8938_required"] else "Not triggered")
    if len(f["accounts"]):
        st.dataframe(f["accounts"], hide_index=True, width="stretch",
                     column_config={"max_local": st.column_config.NumberColumn("Max (local)", format="%.2f"),
                                    "max_usd": st.column_config.NumberColumn("Max USD", format="dollar"),
                                    "year_end_usd": st.column_config.NumberColumn("31 Dec USD", format="dollar")})
        st.download_button("Download FBAR worksheet (CSV)", f["accounts"].to_csv(index=False), f"fbar_{YEAR}.csv")
    st.caption("The combined figure sums each account's own maximum, which is how FBAR's threshold test works. "
               "Max balances come from running balances in your imports plus any snapshots you add. "
               "Joint accounts with a non-US spouse still go on your FBAR.")

    st.subheader("Child Tax Credit and FEIE vs. Foreign Tax Credit")
    cr = expat.us_family_credits(profile, conv, YEAR)
    c = st.columns(3)
    c[0].metric("Earned income (est.)", f"${cr['earned_usd']:,.0f}", help=f"Household salary × {YEAR} average {cr['usd_per_eur_avg']:.4f} USD/EUR")
    c[1].metric("Refundable CTC if you use FTC", f"${cr['actc_with_ftc']:,.0f}")
    c[2].metric("Refundable CTC if you use FEIE", "$0")
    st.markdown(f"""
- **FEIE** excludes up to **${cr['feie_max']:,}** of foreign earned income per person (Form 2555), but anyone who claims it can't get the refundable
  *Additional* Child Tax Credit.
- **FTC** (Form 1116) credits the Dutch tax you paid. Dutch box 1 rates (35.75–49.5%) are above US rates, so it usually wipes out US income tax,
  leaves a carryforward, *and* keeps the refundable CTC: up to $1,700 per child, at 15% of earned income above $2,500.
- If you've claimed the FEIE before and revoke it, you can't claim it again for 5 years without IRS consent, so pick deliberately.
""")
    from finapp import ui_payslips
    wt = ui_payslips.wage_tax_ytd(con, YEAR)
    if wt:
        avg = conv.year_avg_usd_per_eur(YEAR)
        parts = [f"{'you' if k == 'me' else 'partner'}: €{v['so_far']:,.0f} through {v['through']} (full year est. "
                 f"€{(v['projected'] or 0):,.0f} ≈ ${(v['projected'] or 0) * avg:,.0f})" for k, v in wt.items()]
        st.info("**Dutch wage tax withheld (from payslips)**, the starting point for Form 1116: " + "; ".join(parts) +
                ". Your final Dutch tax comes from the aangifte; use that figure on the return.", icon=":material/receipt_long:")
    if cr["children_without_ssn"]:
        st.error("No SSN yet: " + ", ".join(cr["children_without_ssn"]) + ". The credit needs one issued before the return's due date.")
    if fs == "MFJ" and not profile.get("partner_us_person"):
        st.info("Filing jointly with a non-US spouse requires a §6013(g) election and puts their worldwide income on the US return. "
                "Compare with Married Filing Separately or Head of Household before choosing.")

    st.subheader("Investment & retirement accounts by tax treatment")
    rt = expat.retirement_by_treatment(con, conv)
    if rt.empty:
        st.caption("No investment or retirement account values yet. Import a Positions file or add a snapshot.")
    else:
        tot = rt[["usd", "pre_tax", "roth", "taxable", "other"]].sum()
        c = st.columns(4)
        c[0].metric("Pre-tax (taxed when withdrawn)", f"${tot['pre_tax']:,.0f}")
        c[1].metric("Roth (tax-free if qualified)", f"${tot['roth']:,.0f}")
        c[2].metric("Taxable", f"${tot['taxable']:,.0f}")
        c[3].metric("HSA / 529 / other", f"${tot['other']:,.0f}")
        money = {k: st.column_config.NumberColumn(v, format="dollar") for k, v in
                 (("usd", "Value"), ("pre_tax", "Pre-tax"), ("roth", "Roth"), ("taxable", "Taxable"), ("other", "Other"))}
        st.dataframe(rt, hide_index=True, width="stretch",
                     column_config={"account": "Account", "treatment": "Tax treatment",
                                    "as_of": st.column_config.DateColumn("Value as of"), **money})
        st.caption("Latest value of each account at today's exchange rate. Mixed plans are split by the Roth share you set "
                   "on the account; '(guessed)' means the treatment hasn't been set yet (Accounts & net worth → ⋮ → Edit details). "
                   "Only the taxable accounts produce yearly US tax forms (1099s); retirement accounts are taxed on withdrawal.")

    st.subheader("PFIC exposure")
    pf = expat.pfic_flags(con)
    if pf:
        for p in pf:
            st.warning(p)
        st.markdown("EU-domiciled funds and ETFs (e.g. VWRL, IWDA, Meesman funds) are generally **PFICs** for US persons: "
                    "Form 8621 per fund each year and harsh default taxation of gains. Talk to an adviser before buying more.")
    else:
        st.success("No non-US brokerage accounts or fund-platform payments found.")


# ================================================================== Dutch tax & benefits
elif page == "Dutch tax & benefits":
    st.header(f"Dutch tax & benefits · {YEAR}")
    partner = profile.get("partner", True)
    r = rules.get(YEAR)

    from finapp import ui_box3
    b = ui_box3.render(con, conv, profile, YEAR, eur, dual)

    st.subheader("Child benefits")
    kids = profile.get("kids", [])
    if not kids:
        st.info("Add your children on Household & settings.")
    else:
        tx = load_tx(f"{YEAR}-01-01", f"{YEAR}-12-31")
        kb = expat.kinderbijslag_expected(kids, YEAR)
        got = tx.loc[tx["category"] == "Income: Kinderbijslag", "eur"].sum() if len(tx) else 0
        income = (profile.get("me_salary_eur") or 0) + ((profile.get("partner_salary_eur") or 0) if partner else 0)
        er = expat.expat_ruling_check(profile, YEAR)
        if er:
            income -= er["allowance"]
        kgb = expat.kindgebonden_budget(kids, income, b["base"], partner, YEAR)
        c = st.columns(3)
        c[0].metric("Kinderbijslag expected", eur(kb["total"]), help="Not income-tested. Paid quarterly by the SVB, starting the quarter after birth.")
        c[1].metric("Kinderbijslag received (imported)", eur(got))
        c[2].metric("Kindgebonden budget (est.)", eur(kgb.get("amount")), help=kgb.get("reason") or
                    f"Max €{kgb.get('max', 0):,.0f}, reduced by 7.6% of toetsingsinkomen above €{kgb.get('threshold', 0):,.0f}; "
                    f"€0 from about €{kgb.get('zero_point', 0):,.0f}.")
        st.caption(f"Toetsingsinkomen used: {eur(income)} (salaries minus any expat-ruling allowance). "
                   "Kindgebonden budget figures are approximate; confirm with a proefberekening at toeslagen.nl.")
        childcare = -tx.loc[tx["category"] == "Family: Childcare", "eur"].sum() if len(tx) else 0
        if childcare:
            st.info(f"Childcare spending this year: {eur(childcare)}. If both partners work and the provider is LRK-registered, "
                    "check kinderopvangtoeslag at toeslagen.nl.")

    st.subheader("Expat ruling (30% → 27%)")
    er = expat.expat_ruling_check(profile, YEAR)
    er_cfg = profile.get("expat_ruling", {}) or {}
    who = "your partner's" if er_cfg.get("who") == "partner" else "your"
    if not er:
        st.info("Not switched on yet. Go to **Household & settings → Expat ruling**, tick *Someone in the household has it*, "
                "choose who, and click **Save** at the bottom of that form.", icon=":material/info:")
    elif not er["gross"]:
        st.warning(f"The expat ruling is on for **{'your partner' if er_cfg.get('who') == 'partner' else 'you'}**, but "
                   f"{who} gross salary is €0, so nothing can be worked out. Enter {who} yearly gross salary on "
                   "**Household & settings** (and check *Who* is the right person), then click **Save**. "
                   "Not sure of the gross figure? Use *Estimate gross salaries from your statements* there.",
                   icon=":material/warning:")
    else:
        st.caption(f"Based on {who} gross salary of {eur(er['gross'])}, ruling first applied in {er['first_applied_year']}.")
        c = st.columns(4)
        c[0].metric("Tax-free allowance", eur(er["allowance"]))
        c[1].metric("Taxable salary", eur(er["taxable"]))
        c[2].metric("Above salary norm by", eur(er["margin"]), help=f"Norm {YEAR}: €{er['norm']:,.0f}")
        c[3].metric("Allowance from 2027 (est.)", eur(er["allowance_2027"]), f"{er['rate_2027']:.0%} rate", delta_color="off", delta_arrow="off")
        if er["capped_by_norm"]:
            st.warning("Your allowance is limited because taxable salary must stay above the norm.")


# ================================================================== Settings
elif page == "Household & settings":
    st.header("Household & settings")
    p = dict(profile)

    # ---------------------------------------------------------- payslips, then the statement-based estimator
    from finapp import ui_payslips
    ui_payslips.render(con, p, YEAR)

    from finapp import salary as sal
    with st.expander("💡 Estimate gross salaries from your statements", expanded=not (p.get("me_salary_eur") or 0)):
        tx_all = load_tx()
        found = sal.detect(tx_all) if not tx_all.empty else pd.DataFrame()
        if found.empty:
            st.caption("No salary payments found yet. Import your bank statements, and make sure salary deposits are "
                       "categorised as **Income: Salary** (Review & categorise).")
        else:
            st.markdown("Works backwards from the **net pay that reached your account** over the last 12 months to a likely "
                        f"**gross yearly salary**, using {YEAR} Dutch wage tax. Assign each employer to a person, check the "
                        "assumptions, then use the result.")
            who_opts = ["me", "partner", "ignore"]
            fc = st.columns([3, 1.3, 1.3, 1.2, 1.4])
            for col, lab in zip(fc, ["Employer (payer)", "Net received", "Per year", "Months", "Who"]):
                col.caption(f"**{lab}**")
            assign = {}
            for i, r in found.iterrows():
                fc = st.columns([3, 1.3, 1.3, 1.2, 1.4], vertical_alignment="center")
                fc[0].markdown(f"**{r['payer']}**  \n:gray[{r['first']:%b %Y} → {r['last']:%b %Y}]")
                fc[1].write(eur(r["net_period"]))
                fc[2].markdown(eur(r["net_year"]) + ("  \n:gray[scaled up]" if r["annualized"] else ""))
                fc[3].write(str(r["months"]))
                assign[r["payer"]] = fc[4].selectbox("Who", who_opts, index=min(i, 2), key=f"sal_who_{i}",
                                                     label_visibility="collapsed")
            if found["annualized"].any():
                st.caption("Fewer than 12 months found for some employers: their typical month × 12, plus 8% holiday pay.")

            er = p.get("expat_ruling", {}) or {}
            a = st.columns(3)
            pen = a[0].number_input("Your pension contribution (% of gross)", 0.0, 25.0, float(p.get("sal_pension_pct", 4.0)),
                                    0.5, help="The employee share deducted on the payslip. Check a payslip ('pensioenpremie "
                                              "werknemer'); 3–8% is common.") / 100
            r_me = a[1].checkbox("Expat ruling on my salary", er.get("has") and er.get("who", "me") == "me", key="sal_r_me")
            r_pa = a[2].checkbox("Expat ruling on partner's salary", er.get("has") and er.get("who") == "partner", key="sal_r_pa")
            fn = sal.ruling_allowance_fn(YEAR, er.get("under30_master", False), er.get("first_applied_year", 2025))

            res = {}
            for person, ruling in (("me", r_me), ("partner", r_pa)):
                net = sum(found.loc[found["payer"] == k, "net_year"].sum() for k, w in assign.items() if w == person)
                if net > 0:
                    res[person] = sal.gross_from_net(net, YEAR, pen, fn if ruling else None)

            if res:
                cols = st.columns(len(res))
                for col, (person, r) in zip(cols, res.items()):
                    with col.container(border=True):
                        st.metric(f"Estimated gross: {'you' if person == 'me' else 'partner'}", eur(r["gross"]),
                                  f"currently set {eur(p.get('me_salary_eur' if person == 'me' else 'partner_salary_eur') or 0)}",
                                  delta_color="off", delta_arrow="off")
                        st.dataframe(pd.DataFrame([
                            ("Gross salary (incl. holiday pay)", r["gross"]),
                            ("− pension contribution", -r["pension"]),
                            ("− expat ruling allowance (paid tax-free)", -r["allowance"]),
                            ("= taxable wage", r["taxable"]),
                            ("Wage tax before credits", r["tax_before_credits"]),
                            ("− tax credits (AHK + arbeidskorting)", -r["credits"]),
                            ("= wage tax", r["wage_tax"]),
                            ("Net pay (should match your statements)", r["net"]),
                        ], columns=["", "€ per year"]), hide_index=True, width="stretch",
                            column_config={"€ per year": st.column_config.NumberColumn(format="euro")})
                st.caption("An estimate: a lease car, bonuses or equity paid through payroll, extra deductions, or a 13th month "
                           "will move it. If you have a payslip or annual statement (jaaropgaaf), that number is better.")
                if st.button("Use these salaries", type="primary"):
                    if "me" in res:
                        p["me_salary_eur"] = int(round(res["me"]["gross"], -2))
                    if "partner" in res:
                        p["partner_salary_eur"] = int(round(res["partner"]["gross"], -2))
                    p["sal_pension_pct"] = pen * 100
                    db.set_setting(con, "profile", p)
                    st.toast("Salaries updated")
                    st.rerun()

    st.subheader("Where you live")
    st.caption("Used for the local-charges check on the Budget page.")
    from finapp import benchmarks as bm
    muni_opts = ["Not set"] + bm.municipality_options()
    cur_muni = p.get("municipality") or "Not set"
    lw = st.columns([2, 1, 1, 1.4], vertical_alignment="bottom")
    muni = lw[0].selectbox("Municipality", muni_opts, index=muni_opts.index(cur_muni) if cur_muni in muni_opts else 0,
                           key="loc_muni",
                           help="Listed municipalities have their tariffs built in. Pick 'Other' and type the rates from "
                                "your own assessment (aanslag) if yours isn't listed.")
    n_adults = 2 if p.get("partner", True) else 1
    n_kids = len([k for k in p.get("kids", []) if k.get("birth")])
    res_n = lw[1].number_input("People registered at your address", 1, 12, int(p.get("residents") or n_adults + n_kids),
                               key="loc_res")
    owner = lw[2].toggle("We own our home", bool(p.get("home_owner", False)), key="loc_owner")
    woz = lw[3].number_input("WOZ value (€)", 0, 5_000_000, int(p.get("woz") or 0), 10_000, disabled=not owner, key="loc_woz",
                             help="On your WOZ beschikking / municipal tax assessment.")
    own = dict(p.get("local_rates") or {})
    if muni == bm.OTHER_MUNICIPALITY:
        oc = st.columns([2, 2, 1, 1, 1])
        own["name"] = oc[0].text_input("Municipality name", own.get("name", ""), key="loc_name")
        own["collector"] = oc[1].text_input("Who sends the bill (optional)", own.get("collector", ""), key="loc_coll",
                                            help="E.g. a regional tax office such as BghU, BSGW or SVHW. Used to find the payments.")
        own["afval"] = oc[2].number_input("Waste charge €/yr", 0.0, 5000.0, float(own.get("afval") or 0.0), 1.0, key="loc_afval",
                                          help="For your household size, from your assessment.")
        own["riool"] = oc[3].number_input("Sewer charge €/yr", 0.0, 5000.0, float(own.get("riool") or 0.0), 1.0, key="loc_riool",
                                          help="Owner's part; leave 0 if you rent.")
        own["ozb_pct"] = oc[4].number_input("OZB % of WOZ", 0.0, 1.0, float(own.get("ozb_pct") or 0.0), 0.0001,
                                            format="%.4f", key="loc_ozb", help="E.g. 0.0806 for 0.0806%.")
    if st.button("Save where you live"):
        p.update(municipality=None if muni == "Not set" else muni, residents=int(res_n), home_owner=bool(owner),
                 woz=int(woz), local_rates=own if muni == bm.OTHER_MUNICIPALITY else p.get("local_rates"))
        db.set_setting(con, "profile", p)
        st.toast("Saved")
        st.rerun()

    with st.form("profile"):
        st.subheader("Household")
        c = st.columns(4)
        p["tax_year"] = c[0].number_input("Tax year", 2020, 2035, int(p["tax_year"]))
        p["filing_status"] = c[1].selectbox("US filing status", ["MFJ", "MFS", "HOH", "single"],
                                            index=["MFJ", "MFS", "HOH", "single"].index(p["filing_status"]))
        p["partner"] = c[2].checkbox("Fiscal partner in NL", p["partner"])
        p["partner_us_person"] = c[3].checkbox("Partner is a US citizen / green-card holder", p["partner_us_person"])
        c = st.columns(3)
        p["me_salary_eur"] = c[0].number_input("Your gross salary (€/yr)", 0, 2_000_000, int(p["me_salary_eur"] or 0), 1000)
        p["partner_salary_eur"] = c[1].number_input("Partner's gross salary (€/yr)", 0, 2_000_000, int(p["partner_salary_eur"] or 0), 1000)
        p["me_has_ssn"] = c[2].checkbox("You have an SSN", p["me_has_ssn"])

        st.subheader("Children")
        kids_df = pd.DataFrame(p["kids"] or [], columns=["name", "birth", "has_ssn"])
        kids_df["birth"] = pd.to_datetime(kids_df["birth"], errors="coerce").dt.date
        kids_df["has_ssn"] = kids_df["has_ssn"].fillna(True).astype(bool)
        kids_ed = st.data_editor(kids_df, num_rows="dynamic", hide_index=True, width="stretch",
                                 column_config={"birth": st.column_config.DateColumn("Date of birth"),
                                                "has_ssn": st.column_config.CheckboxColumn("Has SSN", default=True)})

        st.subheader("Expat ruling")
        er = dict(p["expat_ruling"])
        c = st.columns(4)
        er["has"] = c[0].checkbox("Someone in the household has it", er.get("has", False))
        er["who"] = c[1].selectbox("Who", ["me", "partner"], index=["me", "partner"].index(er.get("who", "me")))
        er["first_applied_year"] = c[2].number_input("First applied in", 2015, 2035, int(er.get("first_applied_year", 2025)))
        er["under30_master"] = c[3].checkbox("Under 30 with a master's (lower norm)", er.get("under30_master", False))
        p["expat_ruling"] = er

        st.subheader("Calculation options")
        c = st.columns(3)
        _r, _t = expat.box3_settings(p)
        p["box3_include_roth"] = c[0].checkbox("Count Roth money in box 3", _r)
        p["box3_include_traditional"] = c[0].checkbox("Count pre-tax retirement money in box 3", _t)
        p.pop("box3_include_us_retirement", None)
        t = c[1].number_input("Treasury Reporting Rate, 31 Dec (USD per EUR; 0 = use ECB)", 0.0, 3.0,
                              float(p.get("treasury_usd_per_eur") or 0.0), 0.0001, format="%.4f")
        p["treasury_usd_per_eur"] = t or None
        fb = c[2].number_input("Fallback USD per EUR (used only without ECB rates)", 0.5, 2.0,
                               float(db.get_setting(con, "fallback_usd_per_eur", 1.10)), 0.01)
        if st.form_submit_button("Save", type="primary"):
            p["kids"] = [{"name": (r["name"] if pd.notna(r["name"]) and str(r["name"]).strip() else f"Child {i + 1}"), "birth": pd.to_datetime(r["birth"]).date().isoformat() if pd.notna(r["birth"]) and str(r["birth"]).strip() else None,
                          "has_ssn": bool(r["has_ssn"])} for i, (_, r) in enumerate(kids_ed.iterrows())
                         if (pd.notna(r["birth"]) and str(r["birth"]).strip()) or (pd.notna(r["name"]) and r["name"])]
            db.set_setting(con, "profile", p)
            db.set_setting(con, "fallback_usd_per_eur", fb)
            st.success("Saved.")
            st.rerun()

    st.subheader("Claude category suggestions (optional)")
    ai_cfg = db.get_setting(con, "ai", {}) or {}
    env_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    st.caption("Used only when you click **Send** on Review & categorise, after you've seen exactly what goes out. "
               "Create a key at console.anthropic.com (API usage is billed separately from a Claude subscription; "
               "categorising a few hundred payees costs a few cents). Under Anthropic's API terms, requests aren't used for "
               "training and are deleted within 30 days, longer only if flagged for a policy violation.")
    with st.form("ai_form"):
        c = st.columns([3, 2, 2])
        if env_key:
            c[0].text_input("API key", "Using ANTHROPIC_API_KEY from your environment", disabled=True)
            new_key = None
        else:
            new_key = c[0].text_input("API key", type="password",
                                      placeholder="•••••••• (saved)" if ai_cfg.get("api_key") else "sk-ant-…")
        model = c[1].text_input("Model", ai_cfg.get("model") or ai.DEFAULT_MODEL)
        incl = c[2].checkbox("Also send a rounded typical amount (helps with vague payees)", ai_cfg.get("include_amounts", False))
        b = st.columns([1, 1, 4])
        if b[0].form_submit_button("Save"):
            if new_key:
                ai_cfg["api_key"] = new_key.strip()
            ai_cfg.update(model=model.strip() or ai.DEFAULT_MODEL, include_amounts=incl)
            db.set_setting(con, "ai", ai_cfg)
            st.success("Saved.")
        if b[1].form_submit_button("Remove key") and ai_cfg.get("api_key"):
            ai_cfg.pop("api_key")
            db.set_setting(con, "ai", ai_cfg)
            st.success("Key removed.")
    if not env_key:
        st.caption("The key is stored in your local database file in plain text, like the rest of your data. "
                   "For extra care, set it as the ANTHROPIC_API_KEY environment variable instead and leave this blank.")
    log = db.get_setting(con, "ai_log", []) or []
    if log:
        with st.expander(f"History of what was sent ({len(log)} requests)"):
            for e in reversed(log):
                st.markdown(f"**{e['at']}** · {e['count']} payees · {e['model']}")
                st.caption(", ".join(e["payees"]))

    st.subheader("Exchange rates")
    c = st.columns([1, 3])
    if c[0].button("Download / refresh ECB rates"):
        try:
            n = fx.refresh(con)
            st.success(f"Loaded {n:,} daily rates.")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Couldn't reach the ECB: {e}")
    c[1].caption("Daily reference rates back to 1999, for every currency the ECB publishes. Downloaded only when you click.")

    st.subheader("Your categorisation rules")
    rr = pd.read_sql("SELECT id, pattern, category, priority FROM cat_rules ORDER BY priority, id", con)
    ed = st.data_editor(rr, num_rows="dynamic", hide_index=True, width="stretch", column_config={"id": None})
    c = st.columns(3)
    if c[0].button("Save rules"):
        con.execute("DELETE FROM cat_rules")
        for _, r in ed.iterrows():
            if r["pattern"] and r["category"]:
                con.execute("INSERT INTO cat_rules(pattern, category, priority) VALUES(?,?,?)",
                            (r["pattern"], r["category"], int(r["priority"]) if pd.notna(r["priority"]) else 100))
        con.commit()
        st.rerun()
    if c[1].button("Re-categorise everything"):
        st.success(f"Updated {categorize.apply(con)} transactions (hand-edited ones kept).")

    st.subheader("Backup")
    st.caption(f"All data lives in one file: `{db.DB_PATH}`. Copy it to back up; it's not encrypted, so keep it somewhere private.")
    with open(db.DB_PATH, "rb") as fh:
        st.download_button("Download a copy of the database", fh.read(), "finance-backup.db")
