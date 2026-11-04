"""Stock options page."""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from . import db, options
from .importers import _decode

ITM, UNDER, INK, GRID = "#2a78d6", "#e34948", "#52514e", "rgba(0,0,0,0.08)"
STATUS_ICON = {"in the money": "🟢", "underwater": "🔴", "no price": "⚪", "expired": "⚫", "fully exercised": "✔️"}


DEFAULT_CFG = {"tax_rate": 0.50, "after_tax": True, "in_networth": True, "years": 10, "work_country": "NL"}


def _prices(con):
    """{symbol: price} for every ticker with grants, plus {symbol: date set}."""
    lp = options.latest_prices(con)
    return {k: p for k, (p, _) in lp.items() if p}, {k: d for k, (_, d) in lp.items()}


def _sync_nw(con, cfg):
    prices, _ = _prices(con)
    if cfg["in_networth"] and prices:
        options.sync_networth(con, prices, cfg["tax_rate"], cfg["after_tax"])


def render(con, conv):
    st.header("Stock options")
    cfg = load_cfg(con)
    usd_per_eur = conv.usd_per_eur()
    g = options.grants(con)
    syms = options.symbols(con)

    # ---------------------------------------------------------- one price row per company, + settings
    head = st.columns([5, 1.6], vertical_alignment="bottom")
    with head[1].popover("Settings", icon=":material/tune:", width="stretch"):
        c_rate = st.slider("Estimated tax on exercise", 0, 60, int(cfg["tax_rate"] * 100), 1, format="%d%%",
                           help="Used for the after-tax estimate. In the Netherlands the spread is box 1 income "
                                "(top rate 49.5% in 2026); in the US it's wage income.") / 100
        c_after = st.toggle("Show after-tax value", cfg["after_tax"])
        c_nw = st.toggle("Include options in net worth", cfg["in_networth"],
                         help=f"Adds an account called '{options.NETWORTH_ACCOUNT}' holding the in-the-money value of all your grants.")
        c_years = st.number_input("Default term for imported grants without an expiry date (years)", 1, 15, int(cfg["years"]))
        c_wc = st.selectbox("Where you work now (for new grants)", ["NL", "US", "other"],
                            index=["NL", "US", "other"].index(cfg.get("work_country", "NL")))
        if st.button("Save settings"):
            cfg.update(tax_rate=c_rate, after_tax=c_after, in_networth=c_nw, years=int(c_years), work_country=c_wc)
            cfg.pop("symbol", None)
            db.set_setting(con, "options_cfg", cfg)
            if c_nw:
                _sync_nw(con, cfg)
            else:
                con.execute("DELETE FROM balances WHERE account_id IN (SELECT id FROM accounts WHERE name=?)",
                            (options.NETWORTH_ACCOUNT,))
                con.execute("DELETE FROM accounts WHERE name=?", (options.NETWORTH_ACCOUNT,))
                con.commit()
            st.rerun()
    if syms:
        head[0].caption("Share prices (USD). Type a price and Save, or Sync to fetch it; only the ticker is sent.")
    prices, price_dates = _prices(con)
    company = (g.groupby("symbol")["company"].first().to_dict() if not g.empty else {})
    for sym in syms:
        row = st.columns([2, 1.3, 1.3, 1.3], vertical_alignment="bottom")
        name = company.get(sym) or sym
        new_price = row[0].number_input(f"{sym}" + (f" · {name}" if name != sym else ""), min_value=0.0,
                                        value=float(prices.get(sym) or 0.0), step=1.0, format="%.2f", key=f"opt_price_{sym}")
        row[1].caption(f"Price last set  \n**{price_dates.get(sym) or 'never'}**")
        if row[2].button("Save price", type="primary", disabled=new_price <= 0, width="stretch", key=f"opt_save_{sym}"):
            options.set_price(con, sym, new_price)
            _sync_nw(con, cfg)
            st.rerun()
        if row[3].button("Sync price", icon=":material/sync:", width="stretch", key=f"opt_sync_{sym}",
                         help=f"Fetch the latest {sym} price from Yahoo Finance (Stooq as backup). Only the ticker is sent."):
            try:
                got = options.fetch_price(sym)
            except options.PriceError as e:
                st.session_state["opt_price_msg"] = ("error", f"{sym}: {e}")
            else:
                options.set_price(con, sym, got["price"])
                _sync_nw(con, cfg)
                st.session_state["opt_price_msg"] = ("ok", f"{sym} ${got['price']:,.2f} from {got['source']}"
                                                     + (f" ({got['as_of']})" if got["as_of"] else ""))
                st.session_state.pop(f"opt_price_{sym}", None)
            st.rerun()

    msg = st.session_state.pop("opt_price_msg", None)
    if msg:
        (st.success if msg[0] == "ok" else st.error)(msg[1])

    tab_over, tab_grants, tab_ex, tab_imp = st.tabs(["Overview", "Grants", "Exercises & tax", "Import"])

    # ---------------------------------------------------------- import (first, so a fresh install has somewhere to go)
    with tab_imp:
        _import(con, cfg)

    if g.empty:
        with tab_over:
            st.info("No grants yet. Import your grant history on the **Import** tab, or add one by hand there.")
        return
    missing = [s for s in syms if not prices.get(s)]
    if len(missing) == len(syms):
        with tab_over:
            st.warning(f"**No share price saved yet for {', '.join(missing)}**, so nothing can be valued. Type today's price "
                       "at the top and click **Save price**, or **Sync price**.", icon=":material/price_change:")
            _diagnostics(con, g, prices)
        with tab_grants:
            st.caption("Save a share price first.")
        return

    v = options.value(g, prices)
    s = options.summary(v, cfg["tax_rate"])

    # ---------------------------------------------------------- overview
    with tab_over:
        if missing:
            st.warning(f"No price saved for **{', '.join(missing)}**; those grants count as zero until you add one.",
                       icon=":material/price_change:")
        live_all = v[v["status"].isin(["in the money", "underwater"])]
        for sym, ls in live_all.groupby("symbol"):
            if (ls["status"] == "underwater").all():
                p = float(ls["price"].iloc[0])
                hint = (" Your strikes go up to ${:,.2f}, far above the price, which usually means grants from before a "
                        "stock split weren't adjusted. Use **Values look wrong?** below to fix it.".format(ls["strike"].max())
                        if ls["strike"].min() > 2 * p else "")
                st.error(f"**Every {sym} grant is underwater at ${p:,.2f}.** Check the price you saved is today's {sym} price "
                         f"(it was saved {price_dates.get(sym)}).{hint}", icon=":material/warning:")
        sc = options.split_candidates(con)
        if len(sc):
            which = ", ".join(sorted(sc["symbol"].unique()))
            st.warning(f"**{len(sc)} {which} grants from before a stock split look unadjusted.** Unadjusted grants show too few "
                       "options at too high a strike, which badly understates their value.")
            if st.button(f"Adjust {len(sc)} grants for the split", type="primary"):
                for ratio, part in sc.groupby("ratio"):
                    options.apply_split(con, part["id"].tolist(), ratio)
                st.rerun()

        c = st.columns(4)
        val = s["after_tax"] if cfg["after_tax"] else s["intrinsic"]
        c[0].metric("Value now" + (" (after est. tax)" if cfg["after_tax"] else ""), f"${val:,.0f}",
                    f"€{val / usd_per_eur:,.0f}", delta_color="off", delta_arrow="off",
                    help="Vested, unexpired options × (price − strike). Underwater grants count as zero.")
        c[1].metric("Before tax", f"${s['intrinsic']:,.0f}", f"€{s['intrinsic'] / usd_per_eur:,.0f}",
                    delta_color="off", delta_arrow="off")
        c[2].metric("Options outstanding", f"{s['options']:,.0f}", f"{s['grants']} grants", delta_color="off", delta_arrow="off")
        c[3].metric("Underwater grants", f"{s['underwater']}", f"lowest strike ${s['breakeven']:,.2f}" if s["breakeven"] else None,
                    delta_color="off", delta_arrow="off")
        if s["unvested_value"] > 0:
            st.caption(f"Plus ${s['unvested_value']:,.0f} in unvested options, not counted above.")
        if len(syms) > 1:
            bc = options.by_company(v)
            st.dataframe(bc, hide_index=True, width="stretch",
                         column_config={"symbol": "Ticker", "company": "Company",
                                        "price": st.column_config.NumberColumn("Price", format="dollar"),
                                        "grants": "Grants", "options": st.column_config.NumberColumn("Options", format="%,.0f"),
                                        "intrinsic": st.column_config.NumberColumn("Value before tax", format="dollar"),
                                        "underwater": "Underwater"})
        _diagnostics(con, g, prices)

        soon = s["expiring_12m"]
        if len(soon):
            st.error(f"**{len(soon)} in-the-money grant(s) expire within 12 months**, worth ${soon['intrinsic'].sum():,.0f} "
                     f"before tax. The first expires {soon['expiration'].min():%d %b %Y}. Options that expire unexercised are lost.",
                     icon=":material/timer:")

        live = v[v["status"].isin(["in the money", "underwater"])].copy()
        if len(live):
            st.subheader("Value by grant")
            live["label"] = live["symbol"] + " · " + live["grant_date"].dt.strftime("%d %b %Y")
            fig = go.Figure()
            fig.add_bar(x=live["grant_date"], y=live["intrinsic"],
                        marker_color=[ITM if st_ == "in the money" else UNDER for st_ in live["status"]], marker_line_width=0,
                        customdata=live[["label", "strike", "vested_out", "expiration"]].values,
                        hovertemplate="<b>%{customdata[0]}</b><br>Strike $%{customdata[1]:,.2f} · "
                                      "%{customdata[2]:,.0f} options<br>Value $%{y:,.0f}<br>Expires %{customdata[3]|%d %b %Y}"
                                      "<extra></extra>")
            # underwater grants as a short red tick so they're visible at zero
            uw = live[live["status"] == "underwater"]
            if len(uw):
                fig.add_scatter(x=uw["grant_date"], y=[0] * len(uw), mode="markers", name="Underwater",
                                marker=dict(symbol="line-ew-open", size=14, color=UNDER, line=dict(width=3)),
                                customdata=uw[["label", "strike"]].values,
                                hovertemplate="<b>%{customdata[0]}</b><br>Underwater: strike $%{customdata[1]:,.2f}"
                                              "<extra></extra>")
            fig.update_layout(height=320, bargap=0.25, barcornerradius=3, showlegend=False, margin=dict(t=10, b=10, l=10, r=10),
                              yaxis=dict(title=dict(text="USD", font=dict(color=INK, size=12)), tickformat=",.0f", gridcolor=GRID),
                              xaxis=dict(title=None, showgrid=False), plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
            st.caption("Blue: in the money. Red tick: underwater (strike above today's price), worth nothing at today's price.")

            priced = [x for x in syms if prices.get(x)]
            sim_sym = (st.segmented_control("Simulate", priced, default=priced[0], key="sim_sym") or priced[0]) \
                if len(priced) > 1 else priced[0]
            _simulator(v[v["symbol"] == sim_sym], g[g["symbol"] == sim_sym], prices[sim_sym], cfg, usd_per_eur, sim_sym)

    # ---------------------------------------------------------- grants
    with tab_grants:
        show_all = st.toggle("Show expired and fully exercised grants", False)
        t = v if show_all else v[v["status"].isin(options.LIVE)]
        t = t.sort_values("grant_date", ascending=False)
        view = t[["id", "symbol", "status", "grant_date", "strike", "quantity", "outstanding", "vested_out", "spread", "intrinsic",
                  "expiration", "days_left", "plan_type", "work_country"]].copy()
        view["status"] = view["status"].map(lambda x: f"{STATUS_ICON.get(x, '')} {x}")
        for col in ("grant_date", "expiration"):
            view[col] = view[col].dt.date
        st.caption("Select a row to record an exercise, edit or delete the grant.")
        ev = st.dataframe(view, hide_index=True, width="stretch", height=420, on_select="rerun", selection_mode="single-row",
                          key="grants_table",
                          column_config={"id": None, "symbol": "Ticker", "status": "Status",
                                         "grant_date": st.column_config.DateColumn("Granted"),
                                         "strike": st.column_config.NumberColumn("Strike", format="dollar"),
                                         "quantity": st.column_config.NumberColumn("Granted", format="%,.0f"),
                                         "outstanding": st.column_config.NumberColumn("Outstanding", format="%,.0f"),
                                         "vested_out": st.column_config.NumberColumn("Vested", format="%,.0f"),
                                         "spread": st.column_config.NumberColumn("Spread / option", format="dollar"),
                                         "intrinsic": st.column_config.NumberColumn("Value", format="dollar"),
                                         "expiration": st.column_config.DateColumn("Expires"),
                                         "days_left": st.column_config.NumberColumn("Days left", format="%,d"),
                                         "plan_type": "Type", "work_country": st.column_config.TextColumn(
                                             "Worked in", help="Where you worked when this grant was earned. Matters for "
                                                               "splitting exercise income between the US and NL.")})
        rows = ev.selection.rows if ev and ev.selection else []
        if rows:
            sel = view.iloc[rows[0]]
            grant = g[g["id"] == sel["id"]].iloc[0].to_dict()
            b = st.columns(3)
            if b[0].button("Record an exercise", type="primary", width="stretch"):
                _dlg_exercise(con, grant, prices.get(grant["symbol"]))
            if b[1].button("Edit grant", width="stretch"):
                _dlg_edit(con, grant)
            if b[2].button(":red[Delete grant]", width="stretch"):
                _dlg_delete(con, grant)

    # ---------------------------------------------------------- exercises & tax
    with tab_ex:
        ex = options.exercises(con)
        if ex.empty:
            st.caption("No exercises recorded yet. Select a grant on the Grants tab and choose **Record an exercise**.")
        else:
            years = sorted(ex["date"].dt.year.unique(), reverse=True)
            yr = st.selectbox("Tax year", years)
            inc = options.exercise_income(ex, yr)
            tot = inc["spread_income"].sum()
            c = st.columns(3)
            c[0].metric(f"Exercise income {yr}", f"${tot:,.0f}", f"€{tot / conv.year_avg_usd_per_eur(yr):,.0f}",
                        delta_color="off", delta_arrow="off")
            c[1].metric("Exercises", f"{len(inc)}")
            c[2].metric("Options exercised", f"{inc['quantity'].sum():,.0f}")
            shown = inc[["date", "symbol", "grant_date", "quantity", "strike", "fmv", "spread_income", "method", "work_country"]].copy()
            shown["date"] = shown["date"].dt.date
            shown["grant_date"] = shown["grant_date"].dt.date
            st.dataframe(shown, hide_index=True, width="stretch",
                         column_config={"date": "Exercised", "symbol": "Ticker", "grant_date": "Granted",
                                        "quantity": st.column_config.NumberColumn("Options", format="%,.0f"),
                                        "strike": st.column_config.NumberColumn("Strike", format="dollar"),
                                        "fmv": st.column_config.NumberColumn("Price at exercise", format="dollar"),
                                        "spread_income": st.column_config.NumberColumn("Spread (taxable)", format="dollar"),
                                        "method": "Method", "work_country": "Worked in at grant"})
            us_part = inc.loc[inc["work_country"] == "US", "spread_income"].sum()
            st.markdown("**How this is taxed** (check the details with a cross-border adviser):")
            st.markdown(f"""
- **Netherlands:** the spread is box 1 employment income in the year you exercise (or, if the shares can't be traded
  straight away, when they can). Your employer normally runs it through Dutch payroll.
- **US:** the spread on non-qualified options is wage income on your return. As a Dutch resident you'd normally claim
  the **Foreign Tax Credit** for the Dutch tax on it.
- **Split between countries:** income from options earned while working in another country is divided pro rata
  over the period it was earned. {"Some of this year's exercises (" + f"${us_part:,.0f}" + ") come from grants earned while you worked in the US, so part of that income may be US-source." if us_part else "All of this year's exercises come from grants earned while working in the Netherlands."}
- **Shares you keep** after exercising are ordinary investments: box 3 in the Netherlands, and your US cost basis is the
  price on the exercise date.
""")

    # ---------------------------------------------------------- keep net worth in step
    _sync_nw(con, cfg)


# ------------------------------------------------------------------ import tab

def load_cfg(con):
    cfg = dict(DEFAULT_CFG)
    cfg.update(db.get_setting(con, "options_cfg", {}) or {})
    return cfg


def import_file(con, cfg, raw: bytes, name: str, kp: str):
    """Preview, map and import one grant file. kp keeps widget keys unique per place it is shown."""
    try:
        df = options.read_upload(raw, name)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        df = None
    if df is None:
        return
    guess = options.guess_columns(list(df.columns))
    with st.expander("Column mapping", expanded=not {"grant_date", "strike"} <= set(guess)):
        opts = [None] + list(df.columns)
        mapping = {}
        fields = [("grant_date", "Grant date"), ("strike", "Strike / grant price"), ("quantity", "Granted"),
                  ("outstanding", "Outstanding / exercisable"), ("vested", "Vested"), ("exercised", "Exercised"),
                  ("cancelled", "Expired / cancelled"), ("expiration", "Expiry date"), ("grant_ref", "Grant ID"),
                  ("plan_type", "Type"), ("symbol", "Ticker symbol"), ("company", "Company")]
        cols = st.columns(6)
        for i, (k, label) in enumerate(fields):
            mapping[k] = cols[i % 6].selectbox(label, opts, index=opts.index(guess[k]) if guess.get(k) is not None and guess[k] in opts else 0,
                                               key=f"{kp}map_{k}")
    try:
        parsed = options.parse_grants(df, mapping, "", cfg["years"])
    except Exception as e:  # noqa: BLE001
        st.error(f"Couldn't read the grants: {e}")
        return
    if options.missing_symbol(parsed):
        n_missing = int((parsed["symbol"] == "").sum())
        st.info(f"{'This file has' if n_missing == len(parsed) else f'{n_missing} rows have'} no ticker symbol column. "
                "Which company's stock are these options on?", icon=":material/help:")
        k = st.columns([1, 2])
        sym = options.clean_symbol(k[0].text_input("Ticker symbol", key=f"{kp}sym", placeholder="e.g. MSFT"))
        comp = k[1].text_input("Company name (optional)", key=f"{kp}comp")
        if sym:
            fill = parsed["symbol"] == ""
            parsed.loc[fill, "symbol"] = sym
            parsed.loc[fill & ((parsed["company"] == "") | parsed["company"].isna()), "company"] = comp.strip() or sym
            parsed.loc[fill & (parsed["company"] == ""), "company"] = sym
    ready = len(parsed) and not options.missing_symbol(parsed)

    by_sym = parsed[parsed["symbol"] != ""].groupby("symbol").size() if len(parsed) else []
    st.caption(f"{len(parsed)} grants found · {parsed['quantity'].sum():,.0f} options granted · "
               f"{parsed['exercised'].sum():,.0f} exercised"
               + (" · " + ", ".join(f"{n} {s}" for s, n in by_sym.items()) if len(by_sym) else ""))
    st.dataframe(parsed.head(12), hide_index=True, width="stretch")

    # split question, one per ticker that has grants older than a known split
    adjusted, unanswered = {}, False
    pre = options.presplit_in_file(con, parsed) if ready else {}
    for sym, hits in pre.items():
        desc = " and ".join(f"{int(r) if r == int(r) else r}-for-1 on {d:%d %b %Y}" for d, r in hits)
        guess_unadj = options.looks_unadjusted(con, parsed, sym)
        ans = st.radio(f"Are the {sym} grants from before its stock split ({desc}) already adjusted for it?",
                       ["No, adjust them", "Yes, already adjusted"],
                       index=None if guess_unadj is None else (0 if guess_unadj else 1), key=f"{kp}split_{sym}",
                       horizontal=True,
                       help="Compare a pre-split grant's strike with your plan website. Strikes many times today's share "
                            "price usually mean the file is unadjusted.")
        if ans is None:
            unanswered = True
        else:
            adjusted[sym] = ans.startswith("Yes")
    wc = st.selectbox("Where did you work when these were granted?", ["NL", "US", "other"],
                      index=["NL", "US", "other"].index(cfg.get("work_country", "NL")), key=f"{kp}wc",
                      help="You can change it per grant afterwards (Grants → Edit).")
    if unanswered:
        st.caption("Answer the split question above to import.")
    if st.button(f"Import {len(parsed)} grants", type="primary", disabled=not ready or unanswered, key=f"{kp}go"):
        added, updated = options.save_grants(con, parsed, wc, adjusted=adjusted)
        st.toast(f"Added {added} grants, updated {updated} already known.")
        st.rerun()


def _import(con, cfg):
    st.markdown("Export your grant history from your stock-plan website. **E*TRADE:** *Stock Plan → Holdings → "
                "Download → Download Expanded* gives an Excel file (ByBenefitType); upload it as is. Other plans: CSV or Excel "
                "from the grants or holdings page. Columns, including the ticker and company, are matched automatically; "
                "check the mapping and preview before importing. Grants from several companies can live side by side.")
    f = st.file_uploader("Grant history (CSV or Excel)", type=["csv", "txt", "xlsx", "xls"], key="opt_upload")
    if f:
        import_file(con, cfg, f.getvalue(), f.name, "opt_")

    with st.expander("Add a grant by hand"):
        known = options.symbols(con)
        with st.form("opt_manual"):
            c = st.columns(4)
            sym_in = c[0].text_input("Ticker symbol", known[0] if len(known) == 1 else "", placeholder="e.g. MSFT")
            comp = c[1].text_input("Company (optional)")
            gd = c[2].date_input("Grant date", date.today())
            strike = c[3].number_input("Strike (USD)", 0.0, step=0.01, format="%.2f")
            c2 = st.columns(4)
            qty = c2[0].number_input("Options granted", 0.0, step=1.0)
            exp = c2[1].date_input("Expires", date(gd.year + int(cfg["years"]), gd.month, min(gd.day, 28)))
            wc = c2[2].selectbox("Worked in", ["NL", "US", "other"])
            pt = c2[3].selectbox("Type", ["NSO", "ISO"])
            ref = st.text_input("Grant ID (optional)")
            sym = options.clean_symbol(sym_in)
            if st.form_submit_button("Add grant", type="primary"):
                if not sym:
                    st.error("Enter the ticker symbol of the company's stock.")
                elif strike > 0 and qty > 0:
                    row = pd.DataFrame([{"grant_date": pd.Timestamp(gd), "strike": strike, "quantity": qty, "vested": None,
                                         "exercised": 0.0, "expiration": pd.Timestamp(exp), "plan_type": pt,
                                         "symbol": sym, "company": comp.strip() or sym, "grant_ref": ref or None}])
                    options.save_grants(con, row, wc, adjusted=True)
                    st.rerun()


# ------------------------------------------------------------------ dialogs

@st.dialog("Record an exercise")
def _dlg_exercise(con, grant, price):
    out = float(grant["quantity"] - (grant["exercised"] or 0) - (grant["cancelled"] or 0))
    st.markdown(f"Grant of **{pd.Timestamp(grant['grant_date']):%d %b %Y}** at **${grant['strike']:,.2f}** · "
                f"{out:,.0f} options left")
    c = st.columns(2)
    d = c[0].date_input("Exercise date", date.today())
    q = c[1].number_input("Options exercised", 0.0, out, out, 1.0)
    fmv = st.number_input("Share price at exercise (USD)", 0.0, value=float(price or 0.0), step=0.01, format="%.2f")
    method = st.selectbox("How", ["same-day sale", "sell to cover", "exercise and hold"])
    if q and fmv:
        st.caption(f"Spread (taxable income): ${max(0.0, fmv - grant['strike']) * q:,.2f}")
    cc = st.columns(2)
    if cc[0].button("Cancel", width="stretch"):
        st.rerun()
    if cc[1].button("Save exercise", type="primary", width="stretch", disabled=not (q and fmv)):
        con.execute("INSERT INTO option_exercises(grant_id, date, quantity, fmv, method) VALUES(?,?,?,?,?)",
                    (int(grant["id"]), d.isoformat(), q, fmv, method))
        con.execute("UPDATE option_grants SET exercised=COALESCE(exercised,0)+? WHERE id=?", (q, int(grant["id"])))
        con.commit()
        st.rerun()


@st.dialog("Edit grant")
def _dlg_edit(con, grant):
    c = st.columns(2)
    strike = c[0].number_input("Strike (USD)", 0.0, value=float(grant["strike"]), step=0.01, format="%.2f")
    qty = c[1].number_input("Options granted", 0.0, value=float(grant["quantity"]), step=1.0)
    c = st.columns(2)
    exercised = c[0].number_input("Exercised", 0.0, value=float(grant["exercised"] or 0), step=1.0)
    cancelled = c[1].number_input("Cancelled / forfeited", 0.0, value=float(grant["cancelled"] or 0), step=1.0)
    c = st.columns(2)
    vested_all = c[0].checkbox("Fully vested", grant["vested"] is None or pd.isna(grant["vested"]))
    vested = None if vested_all else c[1].number_input("Vested so far", 0.0, value=float(grant["vested"] or 0), step=1.0)
    exp = st.date_input("Expires", pd.Timestamp(grant["expiration"]).date() if pd.notna(grant["expiration"]) else date.today())
    wc = st.selectbox("Worked in when granted", ["NL", "US", "other"],
                      index=["NL", "US", "other"].index(grant["work_country"] if grant["work_country"] in ("NL", "US") else "other"))
    cc = st.columns(2)
    if cc[0].button("Cancel", width="stretch"):
        st.rerun()
    if cc[1].button("Save", type="primary", width="stretch"):
        con.execute("UPDATE option_grants SET strike=?, quantity=?, exercised=?, cancelled=?, vested=?, expiration=?, "
                    "work_country=? WHERE id=?", (strike, qty, exercised, cancelled, vested, exp.isoformat(), wc, int(grant["id"])))
        con.commit()
        st.rerun()


@st.dialog("Delete grant?")
def _dlg_delete(con, grant):
    st.markdown(f"Deletes the grant of **{pd.Timestamp(grant['grant_date']):%d %b %Y}** at **${grant['strike']:,.2f}** "
                "and any exercises recorded against it.")
    cc = st.columns(2)
    if cc[0].button("Cancel", width="stretch"):
        st.rerun()
    if cc[1].button(":red[**Delete grant**]", width="stretch"):
        con.execute("DELETE FROM option_exercises WHERE grant_id=?", (int(grant["id"]),))
        con.execute("DELETE FROM option_grants WHERE id=?", (int(grant["id"]),))
        con.commit()
        st.rerun()


def _diagnostics(con, g, prices):
    with st.expander("Values look wrong? Check prices, strikes and stock splits"):
        live = g[g["outstanding"] > 0]
        for sym in sorted(g["symbol"].unique()):
            ls = live[live["symbol"] == sym]
            c = st.columns(4)
            c[0].markdown(f"**{sym}**")
            c[1].metric("Price used", f"${prices[sym]:,.2f}" if prices.get(sym) else "none saved")
            if len(ls):
                c[2].metric("Strike range (outstanding)", f"{ls['strike'].min():,.2f} – {ls['strike'].max():,.2f}")
                c[3].metric("Outstanding options", f"{ls['outstanding'].sum():,.0f}")
        st.caption("Compare these with your plan website: strikes there should be in the same range, and the option count "
                   "should match. After a stock split, older grants should show strikes divided by the split ratio.")
        for _, r in options.presplit_summary(con).iterrows():
            st.markdown(f"**{r['symbol']} {r['ratio']:g}-for-1 split on {r['split_date']}:** {r['grants_before']} grants are "
                        f"older; {r['marked_adjusted']} are marked as adjusted. Their strikes now run "
                        f"${r['strike_min']:,.2f} – ${r['strike_max']:,.2f}.")
            b = st.columns(2)
            if b[0].button(f"Adjust these {r['grants_before']} grants (strike ÷{r['ratio']:g}, options ×{r['ratio']:g})",
                           key=f"force_{r['symbol']}_{r['split_date']}", width="stretch"):
                _dlg_split(con, r["ids"], r["ratio"], "apply")
            if b[1].button(f"Undo one adjustment (strike ×{r['ratio']:g}, options ÷{r['ratio']:g})",
                           key=f"undo_{r['symbol']}_{r['split_date']}", width="stretch"):
                _dlg_split(con, r["ids"], r["ratio"], "undo")

        st.markdown("**Stock splits the app knows about** for your tickers. Add one if your company split and it's missing.")
        sp = options.splits(con)
        mine = sp[sp["symbol"].isin(g["symbol"].unique())]
        if len(mine):
            st.dataframe(mine.assign(date=mine["date"].dt.date), hide_index=True, width="stretch",
                         column_config={"symbol": "Ticker", "date": "First split-adjusted day",
                                        "ratio": st.column_config.NumberColumn("New shares per old", format="%g")})
        with st.form("opt_add_split"):
            a = st.columns([1, 1, 1, 1], vertical_alignment="bottom")
            syms = sorted(g["symbol"].unique())
            s_sym = a[0].selectbox("Ticker", syms)
            s_date = a[1].date_input("First day trading split-adjusted", date.today())
            s_ratio = a[2].number_input("Ratio (new per old)", 1.0, 1000.0, 2.0, 1.0)
            if a[3].form_submit_button("Add split") and s_ratio > 1:
                options.add_split(con, s_sym, s_date, s_ratio)
                st.rerun()


@st.dialog("Change split adjustment?")
def _dlg_split(con, ids, ratio, mode):
    g = options.grants(con)
    pre = g[g["id"].isin(ids)]
    if mode == "apply":
        st.markdown(f"Strikes on **{len(pre)} grants** go from ${pre['strike'].min():,.2f}–${pre['strike'].max():,.2f} to "
                    f"**${pre['strike'].min() / ratio:,.2f}–${pre['strike'].max() / ratio:,.2f}**, and the option counts "
                    f"are multiplied by {int(ratio)}.")
    else:
        st.markdown(f"Strikes on **{len(pre)} grants** go from ${pre['strike'].min():,.2f}–${pre['strike'].max():,.2f} back to "
                    f"**${pre['strike'].min() * ratio:,.2f}–${pre['strike'].max() * ratio:,.2f}**, and the option counts "
                    f"are divided by {int(ratio)}.")
    st.caption("Pick whichever makes the strikes match your plan website. You can switch back any time.")
    c = st.columns(2)
    if c[0].button("Cancel", width="stretch"):
        st.rerun()
    if c[1].button("Apply" if mode == "apply" else "Undo", type="primary", width="stretch"):
        (options.force_split if mode == "apply" else options.undo_split)(con, ids, ratio)
        st.rerun()


def _simulator(v, g, price, cfg, usd_per_eur, sym):
    st.subheader("Simulate a different price")
    live_g = g[g["outstanding"] > 0]
    if live_g.empty:
        return
    tax = cfg["tax_rate"]
    c = st.columns([1.2, 2.8])
    mode = c[0].segmented_control("Set by", ["Price", "% change"], default="Price", key="sim_mode") or "Price"
    if mode == "Price":
        top_p = float(round(max(price * 3, live_g['strike'].max() * 1.5), 0))
        if st.session_state.get(f"sim_price_{sym}", 0) > top_p:
            st.session_state.pop(f"sim_price_{sym}")
        sim = c[1].slider(f"{sym} price (USD)", 0.0, top_p, float(min(price, top_p)), 0.5, format="$%.2f", key=f"sim_price_{sym}")
    else:
        pct = c[1].slider("Change from today", -90, 300, 0, 1, format="%d%%", key=f"sim_pct_{sym}")
        sim = price * (1 + pct / 100)

    vs = options.value(g, {sym: sim})
    sv = options.summary(vs, tax)
    s0 = options.summary(v, tax)
    now_val = s0["intrinsic"]
    sim_val = sv["intrinsic"]
    live_now = v[v["status"].isin(["in the money", "underwater"])]
    live_sim = vs[vs["status"].isin(["in the money", "underwater"])]
    itm = int((live_sim["status"] == "in the money").sum())

    m = st.columns(4)
    m[0].metric(f"Value at ${sim:,.2f}", f"${sim_val:,.0f}", f"{sim_val - now_val:+,.0f} vs today",
                delta_color="normal" if sim_val != now_val else "off")
    m[1].metric("After est. tax", f"${sim_val * (1 - tax):,.0f}", f"€{sim_val * (1 - tax) / usd_per_eur:,.0f}",
                delta_color="off", delta_arrow="off")
    m[2].metric("Share price change", f"{(sim / price - 1):+.1%}" if price else "—",
                f"value {(sim_val / now_val - 1):+.1%}" if now_val else None, delta_color="off", delta_arrow="off",
                help="Options move more than the share price: a small price change is a bigger change in value.")
    m[3].metric("Grants in the money", f"{itm} of {len(live_sim)}",
                f"{itm - int((live_now['status'] == 'in the money').sum()):+d} vs today", delta_color="off", delta_arrow="off")

    # value curve across prices, one line, today and simulated marked
    lo, hi = 0.0, max(price * 2.2, sim * 1.15, live_g["strike"].max() * 1.3)
    prices = [lo + (hi - lo) * i / 160 for i in range(161)]
    vals = [float(((p - live_now["strike"]).clip(lower=0) * live_now["vested_out"]).sum()) for p in prices]
    fig = go.Figure()
    fig.add_scatter(x=prices, y=vals, mode="lines", line=dict(color=ITM, width=2), name="Value before tax",
                    hovertemplate=f"{sym} $%{{x:,.2f}}<br>Value $%{{y:,.0f}}<extra></extra>")
    fig.add_scatter(x=prices, y=[x * (1 - tax) for x in vals], mode="lines", line=dict(color=INK, width=2, dash="dot"),
                    name="After est. tax", hovertemplate=f"{sym} $%{{x:,.2f}}<br>After tax $%{{y:,.0f}}<extra></extra>")
    for x, y, label in ((price, now_val, "today"), (sim, sim_val, "simulated")):
        fig.add_scatter(x=[x], y=[y], mode="markers+text", text=[f"{label} ${x:,.0f}"], textposition="top left",
                        marker=dict(size=10, color=ITM if label == "today" else UNDER, line=dict(color="#fcfcfb", width=2)),
                        showlegend=False, hovertemplate=f"{label}: {sym} $%{{x:,.2f}} → $%{{y:,.0f}}<extra></extra>",
                        textfont=dict(color=INK, size=12))
    fig.update_layout(height=320, margin=dict(t=10, b=10, l=10, r=10), plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=1.08, x=0),
                      xaxis=dict(title=dict(text=f"{sym} share price (USD)", font=dict(color=INK, size=12)), tickformat=",.0f",
                                 showgrid=False),
                      yaxis=dict(title=dict(text="Options value (USD)", font=dict(color=INK, size=12)), tickformat=",.0f",
                                 gridcolor=GRID, rangemode="tozero"))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.caption(f"The line bends upward as more grants come into the money. Below ${live_g['strike'].min():,.2f} "
               "(your lowest strike) every grant is worth zero.")

    with st.expander("Per grant at the simulated price"):
        t = live_sim[["grant_date", "strike", "vested_out", "intrinsic", "status"]].copy()
        t = t.assign(today=live_now.set_index("id").reindex(live_sim["id"])["intrinsic"].values)
        t["change"] = t["intrinsic"] - t["today"]
        t["grant_date"] = t["grant_date"].dt.date
        st.dataframe(t.sort_values("grant_date", ascending=False), hide_index=True, width="stretch",
                     column_config={"grant_date": st.column_config.DateColumn("Granted"),
                                    "strike": st.column_config.NumberColumn("Strike", format="dollar"),
                                    "vested_out": st.column_config.NumberColumn("Options", format="%,.0f"),
                                    "today": st.column_config.NumberColumn("Value today", format="dollar"),
                                    "intrinsic": st.column_config.NumberColumn("Value simulated", format="dollar"),
                                    "change": st.column_config.NumberColumn("Change", format="dollar"),
                                    "status": "At simulated price"})

    with st.expander("Quick table: −30% to +30%"):
        wi = options.what_if(v)
        wi["price"] = price * (1 + wi["move"])
        wi = wi[["move", "price", "value"]]
        wi["after_tax"] = wi["value"] * (1 - tax)
        wi["move"] = wi["move"].map(lambda x: "today" if abs(x) < 1e-9 else f"{x:+.0%}")
        st.dataframe(wi, hide_index=True, width="stretch",
                     column_config={"move": "Price move", "price": st.column_config.NumberColumn(f"{sym} price", format="dollar"),
                                    "value": st.column_config.NumberColumn("Value before tax", format="dollar"),
                                    "after_tax": st.column_config.NumberColumn("After est. tax", format="dollar")})
