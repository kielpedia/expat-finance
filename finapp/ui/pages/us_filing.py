"""US filing page."""
from datetime import date

import streamlit as st

from finapp import expat
from finapp.ui import common


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
    st.header(f"US filing checks · {YEAR}")
    fs = profile["filing_status"]
    rate = conv.year_end_usd_per_eur(YEAR)
    st.caption(f"Filing status: **{fs}** (change on Household & settings). Converting at 31 Dec ECB rate {rate:.4f} USD/EUR"
               + (f", overridden to Treasury rate {profile['treasury_usd_per_eur']}" if profile.get("treasury_usd_per_eur") else "") + ".")

    if not profile.get("treasury_usd_per_eur"):
        _gap = conv.rate_gap_days("USD", date(YEAR, 12, 31))
        if _gap is None:
            st.error("**Exchange rates aren't loaded**, so every dollar figure on this page uses a fixed fallback rate and "
                     "can be 5-10% off, enough to flip a threshold. Click **Download / refresh ECB rates** on Household & "
                     "settings first.", icon=":material/currency_exchange:")
        elif _gap > 7:
            st.warning(f"Exchange rates are {_gap} days old. Refresh them on Household & settings.",
                       icon=":material/currency_exchange:")
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
    from finapp.ui import payslips as ui_payslips
    wt = ui_payslips.wage_tax_ytd(con, YEAR)
    if wt:
        avg = conv.year_avg_usd_per_eur(YEAR)
        parts = [f"{'you' if k == 'me' else 'partner'}: €{v['so_far']:,.0f} through {v['through']} (full year est. "
                 f"€{(v['projected'] or 0):,.0f} ≈ ${(v['projected'] or 0) * avg:,.0f})" for k, v in wt.items()]
        st.info("**Dutch wage tax withheld (from payslips)**, the starting point for Form 1116: " + "; ".join(parts) +
                ". Your final Dutch tax comes from the aangifte; use that figure on the return.", icon=":material/receipt_long:")
    if cr["children_without_ssn"]:
        st.error("No SSN yet: " + ", ".join(cr["children_without_ssn"]) + ". The credit needs one issued before the return's due date.")
    if cr.get("children_ssn_unknown"):
        st.warning("Not answered on Household & settings: does " + ", ".join(cr["children_ssn_unknown"]) + " have an SSN? "
                   "Left out of the estimate until you say yes.")
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
