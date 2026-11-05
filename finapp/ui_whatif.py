"""Budget 'What if…' simulator: remove or change income sources and expenses, see the monthly effect."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from . import analysis, db

SURPLUS, DEFICIT, INK = "#2a78d6", "#e34948", "#52514e"
ACTIONS = ["Remove", "Change by %", "Set to €/month"]
NEW_INC, NEW_EXP = "New income", "New expense"
COLS = ["what", "action", "value", "note"]


def eur(v):
    return "—" if v is None or pd.isna(v) else f"€{v:,.0f}"


def baselines(v: pd.DataFrame, nm: int) -> dict:
    """Per-month amounts (positive) for each thing you can change, keyed by its label."""
    b = v[v["budget"]]
    out = {}
    inc_rows = b[analysis.income_mask(b)]
    inc = inc_rows.groupby("payee")["eur"].sum().sort_values(ascending=False) / nm
    for p, a in inc[inc > 0].items():
        out[f"Income · {p}"] = ("income", a)
    exp = analysis.spending_rows(b)
    cats = -exp.groupby("category")["eur"].sum() / nm
    for c, a in cats[cats > 0].sort_values(ascending=False).items():
        out[f"Category · {c}"] = ("expense", a)
    pays = -exp.groupby("payee")["eur"].sum() / nm
    for p, a in pays[pays > 0].sort_values(ascending=False).head(60).items():
        out[f"Payee · {p}"] = ("expense", a)
    return out


def after(base: float, action: str, value) -> float:
    value = 0.0 if value is None or pd.isna(value) else float(value)
    if action == "Remove":
        return 0.0
    if action == "Change by %":
        return max(0.0, base * (1 + value / 100))
    return max(0.0, value)


def render(con, conv, v: pd.DataFrame, nm: int, period_label: str):
    st.subheader("What if…")
    st.caption(f"Try changes against your average month in **{period_label}**: lose or change an income, cut or drop a "
               "category or a payee, or add something new. Nothing in your data changes. Income is take-home pay as it "
               "arrives in your accounts, so a lower salary means lower net pay, not gross.")
    base = baselines(v, nm)
    ms = analysis.monthly_summary(v)
    inc0 = ms["income"].sum() / nm if len(ms) else 0.0
    exp0 = ms["expenses"].sum() / nm if len(ms) else 0.0

    # editor: the starting table comes from the saved scenario once per session, so edits aren't reset each run
    if "wi_start" not in st.session_state:
        saved = db.get_setting(con, "budget_scenario", []) or []
        st.session_state["wi_start"] = pd.DataFrame(saved, columns=COLS)
        st.session_state["wi_ver"] = 0
    start = st.session_state["wi_start"].copy()
    start["value"] = pd.to_numeric(start["value"], errors="coerce").astype(float)
    start["note"] = start["note"].fillna("").astype(str)
    opts = list(base) + [NEW_INC, NEW_EXP]
    opts += [w for w in start["what"].dropna() if w not in opts]
    ed = st.data_editor(
        start, num_rows="dynamic", hide_index=True, width="stretch", key=f"wi_ed_{st.session_state['wi_ver']}",
        column_config={
            "what": st.column_config.SelectboxColumn(
                "What", options=opts, required=True, width="large",
                help="Income · a payer (e.g. an employer) · Category · a spending category · Payee · one shop or "
                     "subscription · or a new income/expense."),
            "action": st.column_config.SelectboxColumn("Change", options=ACTIONS, required=True, default="Remove",
                                                       help="New income/expense always uses the amount as €/month."),
            "value": st.column_config.NumberColumn("Amount", format="%.0f",
                                                   help="% for 'Change by %' (e.g. -20), € per month otherwise."),
            "note": st.column_config.TextColumn("Note"),
        })
    rows = ed.dropna(subset=["what"]).copy()
    rows["action"] = rows["action"].fillna("Remove")
    rows["note"] = rows["note"].where(rows["note"].notna(), None)
    to_save = rows[COLS].astype(object).where(rows[COLS].notna(), None).to_dict("records")
    if to_save != (db.get_setting(con, "budget_scenario", []) or []):
        db.set_setting(con, "budget_scenario", to_save)
    bc = st.columns([1, 5])
    if bc[0].button("Clear all", key="wi_clear", disabled=rows.empty):
        db.set_setting(con, "budget_scenario", [])
        st.session_state["wi_start"] = pd.DataFrame(columns=COLS)
        st.session_state["wi_ver"] += 1
        st.rerun()
    if rows.empty:
        bc[1].caption("Add a row to start, e.g. *Income · (employer)* → Remove, or *Category · Food: Groceries* → "
                      "Change by % → -15. Rows are kept until you clear them.")
        return

    # effect of each change
    eff, warn = [], []
    for _, r in rows.iterrows():
        w, a, val = r["what"], r["action"], r["value"]
        if w in (NEW_INC, NEW_EXP):
            side, b0 = ("income" if w == NEW_INC else "expense"), 0.0
            b1 = 0.0 if a == "Remove" else after(0.0, "Set to €/month", val if a == "Set to €/month" else val)
        else:
            side, b0 = base.get(w, (("income" if w.startswith("Income") else "expense"), 0.0))
            if w not in base:
                warn.append(f"*{w}* has no money in this period, so changing it does nothing.")
            b1 = after(b0, a, val)
        d_net = (b1 - b0) if side == "income" else (b0 - b1)
        note = r["note"] if isinstance(r["note"], str) and r["note"].strip() else None
        label = (note or w).replace("Income · ", "").replace("Category · ", "").replace("Payee · ", "")
        eff.append({"label": label, "what": w, "side": side, "before": b0, "after": b1, "net": d_net})
    eff = pd.DataFrame(eff)
    cats_changed = {w.split(" · ", 1)[1] for w in eff["what"] if w.startswith("Category · ")}
    exp_payees = analysis.spending_rows(v)
    for w in eff["what"]:
        if w.startswith("Payee · "):
            p = w.split(" · ", 1)[1]
            pc = set(exp_payees.loc[exp_payees["payee"] == p, "category"])
            if pc & cats_changed:
                warn.append(f"*{p}* is also part of a category you changed ({', '.join(sorted(pc & cats_changed))}), "
                            "so it's counted twice.")
    for w_ in warn:
        st.warning(w_, icon=":material/info:")

    inc1 = inc0 + eff.loc[eff["side"] == "income", "after"].sum() - eff.loc[eff["side"] == "income", "before"].sum()
    exp1 = exp0 + eff.loc[eff["side"] == "expense", "after"].sum() - eff.loc[eff["side"] == "expense", "before"].sum()
    net0, net1 = inc0 - exp0, inc1 - exp1
    bal = analysis.balance_on(con, conv, pd.Timestamp.today())
    liquid = bal.loc[bal["kind"].isin(analysis.LIQUID_KINDS), "eur"].clip(lower=0).sum() if not bal.empty else 0.0

    c = st.columns(5)
    c[0].metric("Income / month", eur(inc1), f"{inc1 - inc0:+,.0f} €", delta_color="normal")
    c[1].metric("Spending / month", eur(exp1), f"{exp1 - exp0:+,.0f} €", delta_color="inverse")
    c[2].metric("Net / month", eur(net1), f"{net1 - net0:+,.0f} € ({(net1 - net0) * 12:+,.0f} € a year)")
    sr0 = net0 / inc0 if inc0 else None
    sr1 = net1 / inc1 if inc1 else None
    c[3].metric("Savings rate", f"{sr1:.0%}" if sr1 is not None else "—",
                f"{(sr1 - sr0) * 100:+.0f} pts" if sr0 is not None and sr1 is not None else None)
    if net1 < 0:
        c[4].metric("Savings last", f"{liquid / -net1:.0f} months" if liquid else "—",
                    help=f"Checking + savings today ({eur(liquid)}) divided by the monthly shortfall.")
    else:
        c[4].metric("Emergency fund", f"{liquid / exp1:.1f} mo" if exp1 else "—",
                    f"{(liquid / exp1 - liquid / exp0):+.1f} mo" if exp1 and exp0 else None,
                    help="Checking + savings today divided by monthly spending in this scenario.")
    if net1 < 0 <= net0:
        st.error(f"This scenario turns a monthly surplus of {eur(net0)} into a shortfall of {eur(-net1)}. "
                 + (f"Your checking and savings ({eur(liquid)}) would cover it for about {liquid / -net1:.0f} months."
                    if liquid else ""), icon=":material/trending_down:")

    # waterfall: today's net -> each change -> scenario net
    fig = go.Figure(go.Waterfall(
        x=["Now"] + list(eff["label"]) + ["After changes"],
        y=[net0] + list(eff["net"]) + [net1],
        measure=["absolute"] + ["relative"] * len(eff) + ["total"],
        increasing=dict(marker=dict(color=SURPLUS)), decreasing=dict(marker=dict(color=DEFICIT)),
        totals=dict(marker=dict(color=INK)), connector=dict(line=dict(color="rgba(0,0,0,0.25)", width=1)),
        text=[eur(net0)] + [f"{x:+,.0f}" for x in eff["net"]] + [eur(net1)], textposition="outside",
        hovertemplate="%{x}: €%{y:,.0f}<extra></extra>"))
    fig.add_hline(y=0, line_width=1, line_color="rgba(0,0,0,0.35)")
    fig.update_layout(height=340, margin=dict(t=30, b=10, l=10, r=10), plot_bgcolor="rgba(0,0,0,0)", showlegend=False,
                      yaxis=dict(title=dict(text="Net € per month", font=dict(size=12)), tickformat=",.0f",
                                 gridcolor="rgba(0,0,0,0.08)"), xaxis=dict(title=None))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    t = eff.assign(type=eff["side"].map({"income": "Income", "expense": "Expense"}),
                   net=[f"{'+' if x >= 0 else '−'}€{abs(x):,.0f}" for x in eff["net"]])
    st.dataframe(t[["label", "type", "before", "after", "net"]], hide_index=True, width="stretch",
                 column_config={"label": "Change", "type": "Type",
                                "before": st.column_config.NumberColumn("Now €/mo", format="euro"),
                                "after": st.column_config.NumberColumn("After €/mo", format="euro"),
                                "net": st.column_config.TextColumn("Effect on net / mo")})
