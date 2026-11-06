"""Dutch tax page: box 3 estimate, account by account, with the calculation spelled out."""
import pandas as pd
import streamlit as st

from finapp import db, expat, rules


def render(con, conv, profile: dict, year: int, eur, dual) -> dict:
    st.subheader("Box 3 (savings & investments)")
    st.markdown(
        "**Box 3 taxes what you own, not what you earn.** The Belastingdienst assumes your savings and investments "
        "made a fixed return, and takes 36% of that assumed return on everything above the tax-free amount. It only "
        "looks at what you had on **1 January** (the *peildatum*). Your salary is box 1 and is already taxed through "
        "your payslip, so it doesn't change this figure; only what you've saved and invested does.")

    yc = st.columns([3, 2])
    modes = [f"Tax year {year} (owned on 1 Jan {year})", f"Preview {year + 1} (today's values)"]
    mode = yc[0].segmented_control("Year", modes, default=modes[0], key="b3_mode", label_visibility="collapsed") or modes[0]
    preview = mode == modes[1]
    tax_year = year + 1 if preview else year
    peil = pd.Timestamp.today().normalize() if preview else pd.Timestamp(year, 1, 1)
    p = rules.get(tax_year)["box3"]
    if preview:
        yc[1].caption(f"Uses your latest values as a stand-in for 1 Jan {year + 1}, with {max(rules.RULES)} rates.")

    if expat.partial_non_resident_option(profile, tax_year):
        st.info(f"**Your expat ruling may make box 3 €0 for {tax_year}.** Because the ruling already applied in December "
                "2023, you can still choose *partial non-resident taxpayer* status in your tax return up to and including "
                "2026. With it, box 3 only covers Dutch real estate (not your home) and you skip the rest of this "
                "calculation. It's a choice in the aangifte, it ends after 2026, and it can affect other things (e.g. "
                "tax credits), so confirm with your adviser. The estimate below is what you'd pay **without** it.",
                icon=":material/lightbulb:")

    inc_roth, inc_trad = expat.box3_settings(profile)
    tc = st.columns(2)
    n_roth = tc[0].toggle("Count Roth money in box 3", inc_roth, key="b3_roth",
                          help="Whether the Netherlands taxes a US Roth account in box 3 is debated. Check with a "
                               "cross-border adviser; switch on to see the effect.")
    n_trad = tc[1].toggle("Count pre-tax retirement money in box 3", inc_trad, key="b3_trad",
                          help="401(k)s and traditional IRAs are generally treated as pensions under the US–NL treaty "
                               "and left out of box 3.")
    if (n_roth, n_trad) != (inc_roth, inc_trad):
        profile.update(box3_include_roth=n_roth, box3_include_traditional=n_trad)
        profile.pop("box3_include_us_retirement", None)
        db.set_setting(con, "profile", profile)
        st.rerun()

    # ------------------------------------------------------------------ step 1: what counts
    st.markdown(f"##### 1 · What you owned on {peil.day} {peil:%B %Y}")
    acc = expat.box3_accounts(con, conv, peil, profile)
    saved_all = db.get_setting(con, "box3_values", {}) or {}
    saved = saved_all.get(str(tax_year), {}) if not preview else {}
    acc["key"] = [f"{i}:{t}" for i, t in zip(acc["account_id"], acc["part"])]
    acc["use"] = [float(saved.get(k, v)) for k, v in zip(acc["key"], acc["value"])]
    acc["edited"] = acc["key"].isin(saved.keys())
    acc["where"] = [("✎ your value" if e else
                     f"{d.day} {d:%b %Y}" + (" · estimated" if s == "estimated" else "") if s != "no value" else "no value yet")
                    for e, d, s in zip(acc["edited"], acc["date"], acc["source"])]
    acc["note"] = [r if r else ("Earliest value we have; replace with the 1 January figure" if s == "estimated" else "")
                   for r, s in zip(acc["reason"], acc["source"])]

    est = acc[(acc["source"] == "estimated") & (acc["bucket"] != "Left out") & ~acc["edited"]]
    if len(est) and not preview:
        st.warning(f"**{len(est)} account{'s have' if len(est) != 1 else ' has'} no value on 1 January {year}** "
                   f"({', '.join(est['account'])}), so the earliest value on file is used instead. For an exact figure, "
                   f"type the 1 January balance from the year-end or January statement in the *Value* column.",
                   icon=":material/event_busy:")
    guessed = acc[acc["guessed"]]
    if len(guessed):
        st.warning("Tax treatment is only guessed for: " + ", ".join(sorted(set(guessed["account"]))) +
                   ". Set it under Accounts & net worth → ⋮ → Edit details, especially for Roth or mixed plans.")

    ver = st.session_state.setdefault("b3_ver", 0)
    show = acc[["key", "account", "bucket", "use", "where", "note"]].copy()
    ed = st.data_editor(show, hide_index=True, width="stretch", key=f"b3_ed_{tax_year}_{ver}",
                        disabled=["account", "bucket", "where", "note"],
                        height=min(560, 38 + 35 * len(show)),
                        column_config={"key": None, "account": st.column_config.TextColumn("Account", width="medium"),
                                       "bucket": st.column_config.TextColumn("Counts as", width="small"),
                                       "use": st.column_config.NumberColumn("Value (€)", format="euro",
                                                                            help="Debts are negative. Edit to correct."),
                                       "where": st.column_config.TextColumn("Value from", width="medium"),
                                       "note": st.column_config.TextColumn("Note", width="large")})
    ex = saved_all.get(f"{tax_year}_extra", {}) if not preview else {}
    xc = st.columns([2, 2, 1.3], vertical_alignment="bottom")
    x_assets = xc[0].number_input("Other assets not in the app (€)", 0.0, value=float(ex.get("assets", 0.0)), step=500.0,
                                  key=f"b3_xa_{tax_year}",
                                  help="Crypto, cash at home, money lent to family, a second home, a US bank account you "
                                       "haven't imported…")
    x_debts = xc[1].number_input("Other debts not in the app (€)", 0.0, value=float(ex.get("debts", 0.0)), step=500.0,
                                 key=f"b3_xd_{tax_year}", help="Not your own-home mortgage.")
    changed = (ed["use"] - show["use"]).abs() > 0.5
    if not preview:
        if xc[2].button("Save values", type="primary", disabled=not changed.any() and
                        (x_assets, x_debts) == (float(ex.get("assets", 0.0)), float(ex.get("debts", 0.0)))):
            vals = dict(saved)
            for k, u, v0 in zip(ed["key"], ed["use"], acc["value"]):
                if abs(float(u or 0) - float(v0)) > 0.5:
                    vals[k] = float(u or 0)
                else:
                    vals.pop(k, None)
            saved_all[str(tax_year)] = vals
            saved_all[f"{tax_year}_extra"] = {"assets": x_assets, "debts": x_debts}
            db.set_setting(con, "box3_values", saved_all)
            st.session_state["b3_ver"] = ver + 1
            st.rerun()
        if saved and xc[2].button("Reset to statements", key="b3_reset"):
            saved_all.pop(str(tax_year), None)
            db.set_setting(con, "box3_values", saved_all)
            st.session_state["b3_ver"] = ver + 1
            st.rerun()

    use = ed.assign(bucket=acc["bucket"].values)
    bank = use.loc[use["bucket"] == "Savings", "use"].sum()
    other = use.loc[use["bucket"] == "Investments", "use"].sum() + x_assets
    debts = abs(use.loc[use["bucket"] == "Debt", "use"].clip(upper=0).sum()) + x_debts
    partner = profile.get("partner", True)
    b = expat.box3(bank, other, debts, partner, tax_year)

    # ------------------------------------------------------------------ step 2: the calculation
    st.markdown(f"##### 2 · The calculation for {tax_year}")
    n = 2 if partner else 1
    thr = p["debt_threshold_per_person"] * n
    debt_net = max(0.0, debts - thr)
    m = st.columns(3)
    m[0].metric(f"Estimated box 3 tax {tax_year}", dual(b["tax"]))
    m[1].metric("Set aside per month", eur(b["tax"] / 12))
    if b["headroom"] >= 0:
        m[2].metric("Room below the tax-free amount", eur(b["headroom"]))
        st.success(f"What you own after debts ({eur(b['base'])}) is below the tax-free amount ({eur(b['exemption'])}"
                   f"{' for you and your partner' if partner else ''}), so there's **no box 3 tax** for {tax_year}.")
    else:
        m[2].metric("Above the tax-free amount by", eur(-b["headroom"]))
    share = b["taxable_base"] / b["base"] if b["base"] > 0 else 0.0
    steps = [
        ("Savings & bank", eur(bank), f"× {p['rate_bank']:.2%} assumed return" + (" (provisional)" if tax_year == 2026 else ""),
         eur(bank * p["rate_bank"])),
        ("Investments & other", eur(other), f"× {p['rate_other']:.2%} assumed return", eur(other * p["rate_other"])),
        ("Debts", eur(debts), f"first {eur(thr)} doesn't count; {eur(debt_net)} × {p['rate_debt']:.2%}",
         eur(-debt_net * p["rate_debt"]) if debt_net else eur(0)),
        ("= Assumed return", "", f"{b['effective_rate']:.2%} of what you own after debts", eur(b["notional_return"])),
        ("What you own after debts", eur(b["base"]), "", ""),
        ("− Tax-free amount", eur(b["exemption"]), f"{eur(p['exemption_per_person'])} per person" + (", ×2 with a fiscal partner" if partner else ""), ""),
        ("= Taxable part", eur(b["taxable_base"]), f"{share:.0%} of what you own", ""),
        ("Taxable return", "", f"assumed return × {share:.0%}", eur(b["taxable_benefit"])),
        ("Box 3 tax", "", f"× {p['tax_rate']:.0%}", eur(b["tax"])),
    ]
    st.dataframe(pd.DataFrame(steps, columns=["Step", "Amount", "How", "Return / tax"]), hide_index=True, width="stretch",
                 column_config={"How": st.column_config.TextColumn(width="large")})
    st.caption("Fiscal partners can split box 3 between them however they like in the aangifte; the total is the same. "
               "If your *actual* return was lower than assumed (e.g. a bad year on the markets), the "
               "*tegenbewijsregeling* lets you pay on the real return instead. Paid through your yearly tax return, or "
               "monthly if you have a provisional assessment (voorlopige aanslag).")
    return b
