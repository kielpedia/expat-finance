"""Overview page."""

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from finapp import analysis, db, expat
from finapp.ui import common
from finapp.ui.common import category_options, edited_rows, eur, load_tx, pct, save_tx_edits, spread_column

# Cash-flow periods, smallest window first. Calendar-based: "1 year" is the last 12 months including this one.
PERIODS = {"Current month": 1, "Last month": 1, "Year to date": None, "1 year": 12, "3 years": 36, "All time": None}


def period_range(period: str, today=None):
    """(first month, last month) for a period, as month-start timestamps; (None, None) for all time."""
    this = pd.Timestamp(today or date.today()).normalize().replace(day=1)
    if period == "All time":
        return None, None
    if period == "Current month":
        return this, this
    if period == "Last month":
        last = this - pd.DateOffset(months=1)
        return last, last
    if period == "Year to date":
        return this.replace(month=1), this
    return this - pd.DateOffset(months=PERIODS[period] - 1), this


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
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

        # period, smallest window first
        saved_period = db.get_setting(con, "cashflow_period", "1 year")
        period = st.segmented_control("Period", list(PERIODS), default=saved_period if saved_period in PERIODS else "1 year",
                                      key="cf_period", label_visibility="collapsed") or "1 year"
        if period != saved_period:
            db.set_setting(con, "cashflow_period", period)
        p_from, p_to = period_range(period)
        ms = ms[(ms["month"] >= p_from) & (ms["month"] <= p_to)] if p_from is not None else ms
        tx = tx[(tx["month"] >= p_from) & (tx["month"] <= p_to)] if p_from is not None else tx
        if ms.empty:
            st.info(f"No transactions in **{period.lower()}**"
                    + (f" ({p_from:%b %Y}" + (f" – {p_to:%b %Y})" if p_to != p_from else ")") if p_from is not None else "")
                    + ". Pick a longer period or import more statements.")
            return
        ms = ms.reset_index(drop=True)

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
                          annotation_font_color=INK, annotation_font_size=12,
                          annotation_bgcolor="rgba(255,255,255,0.85)")   # stays readable over a bar
            fig.add_hline(y=0, line_width=1, line_color="rgba(0,0,0,0.35)")
            # label only the best and worst month, not every bar
            for idx in (m["net"].idxmax(), m["net"].idxmin()):
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
                          xaxis=dict(title=None, tickformat="%b %y", showgrid=False,
                                     dtick="M1" if len(m) <= 14 else "M3" if len(m) <= 40 else "M6"),
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
        months = sorted(m["month"], reverse=True)          # newest first
        if st.session_state.get("drill_select") not in months:
            st.session_state["drill_select"] = months[0]
        st.subheader("Month detail")
        d = st.columns([2, 2, 2])
        month = d[0].selectbox("Month", months, format_func=lambda x: x.strftime("%B %Y"), key="drill_select")
        flow = d[1].segmented_control("Show", ["All", "Income", "Outflow"], default="All", key="drill_flow") or "All"
        incl_all = d[2].toggle("Include transfers", key="drill_all",
                               help="Transfers are left out of the chart; switch on to see every transaction that month.")

        mt = tx[tx["month"] == month].copy()
        base = mt if incl_all else mt[mt["budget"]]
        # with transfers shown, split by sign; otherwise income vs. spending (refunds offset spending)
        is_in = (base["eur"] > 0) if incl_all else analysis.income_mask(base)
        inc = base.loc[is_in, "eur"].sum() * k
        out = -base.loc[~is_in, "eur"].sum() * k
        mm = st.columns(4)
        mm[0].metric("Income", f"{sym}{inc:,.0f}")
        mm[1].metric("Outflow", f"{sym}{out:,.0f}")
        mm[2].metric("Net", f"{sym}{inc - out:,.0f}")
        mm[3].metric("Transactions", f"{len(base):,}")

        view_tx = base if flow == "All" else base[is_in] if flow == "Income" else base[~is_in]
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
