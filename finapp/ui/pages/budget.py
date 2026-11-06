"""Budget page."""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from finapp import analysis, db
from finapp.ui import common
from finapp.ui.common import category_options, dual, edited_rows, eur, load_tx, pct, save_tx_edits, spread_column


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
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

    from finapp.ui import whatif as ui_whatif
    ui_whatif.render(con, conv, v, nm, f"{p_from:%b %Y}" + (f" – {p_to:%b %Y}" if p_to != p_from else ""))

    st.subheader("Spending by group over time")
    trend = analysis.spending_rows(v).groupby(["month", "group"])["eur"].sum().mul(-1).clip(lower=0).reset_index()
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
        spend = analysis.spending_rows(v)
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
