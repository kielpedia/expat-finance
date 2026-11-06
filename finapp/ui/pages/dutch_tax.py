"""Dutch tax & benefits page."""

import streamlit as st

from finapp import expat, rules
from finapp.ui import common
from finapp.ui.common import dual, eur, load_tx


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
    st.header(f"Dutch tax & benefits · {YEAR}")
    partner = profile.get("partner", True)
    rules.get(YEAR)  # fails early if the year has no rules block

    from finapp.ui import box3 as ui_box3
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
