"""Payslip import and review (shown on Household & settings)."""
import pandas as pd
import streamlit as st

from finapp import db, payslips

PERSONS = {"me": "Me", "partner": "Partner"}


def eur(v):
    return "—" if v is None or pd.isna(v) else f"€{v:,.0f}"


def render(con, profile: dict, year: int):
    with st.expander("📄 Payslips: the most accurate salary figures", expanded=False):
        st.markdown("Upload payslip PDFs (Workday: *Pay → Payslips → download*). The text is read **on your computer**; "
                    "check what was found, correct anything that's off, and save. The latest payslip's year-to-date "
                    "column gives your real gross pay, pension and wage tax so far this year.")
        files = st.file_uploader("Payslip PDFs", type=["pdf"], accept_multiple_files=True, key="slip_upload")
        for f in files or []:
            _review(con, f)
        _saved(con)
        _apply(con, profile, year)


def _review(con, f):
    key = f"slip_{f.name}_{f.size}"
    try:
        text = payslips.pdf_text(f.getvalue())
        parsed = payslips.parse(text)
    except Exception as e:  # noqa: BLE001
        st.error(f"{f.name}: {e}")
        return
    with st.container(border=True):
        st.markdown(f"**{f.name}**")
        for n in payslips.check(parsed):
            st.warning(n, icon=":material/rule:")
        c = st.columns(3)
        person = c[0].selectbox("Whose payslip", list(PERSONS), format_func=PERSONS.get, key=f"{key}_who")
        employer = c[1].text_input("Employer", parsed["employer"], key=f"{key}_emp")
        period = c[2].text_input("Month (YYYY-MM)", parsed["period"] or "", key=f"{key}_per")
        fields = parsed["fields"]
        values = {}
        hdr = st.columns([2.2, 1.2, 1.2, 3.4])
        for col, lab in zip(hdr, ["", "This month (€)", "Year to date (€)", "Read from line"]):
            col.caption(f"**{lab}**")
        for k, (label, _pats) in payslips.FIELDS.items():
            f_ = fields.get(k, {})
            row = st.columns([2.2, 1.2, 1.2, 3.4], vertical_alignment="center")
            row[0].markdown(label)
            values[k] = row[1].number_input(label, 0.0, value=float(f_.get("value") or 0.0), step=10.0, format="%.2f",
                                            key=f"{key}_{k}", label_visibility="collapsed")
            if k in payslips.YTD_KEYS:
                values[f"ytd_{k}"] = row[2].number_input(f"{label} YTD", 0.0, value=float(f_.get("ytd") or 0.0), step=100.0,
                                                         format="%.2f", key=f"{key}_ytd_{k}", label_visibility="collapsed")
            else:
                row[2].write("")
            row[3].caption(" ".join(f_["line"].split())[:90] if f_ else ":gray[not found]")
        values = {k: (v if v else None) for k, v in values.items()}
        ok = bool(period and len(period) == 7 and period[4] == "-")
        if st.button("Save payslip", type="primary", key=f"{key}_save", disabled=not ok):
            payslips.save(con, person, employer.strip() or "Employer", period, values, f.name)
            st.toast(f"Saved {period} payslip")
            st.rerun()


def _saved(con):
    p = payslips.all_payslips(con)
    if p.empty:
        return
    st.markdown("**Saved payslips**")
    view = p[["id", "period", "person", "employer", "gross", "pension", "wage_tax", "et_allowance", "net", "ytd_gross",
              "ytd_wage_tax"]].copy()
    view["person"] = view["person"].map(PERSONS)
    money = {c: st.column_config.NumberColumn(lab, format="euro") for c, lab in
             (("gross", "Gross"), ("pension", "Pension"), ("wage_tax", "Wage tax"), ("et_allowance", "30% allowance"),
              ("net", "Net"), ("ytd_gross", "Gross YTD"), ("ytd_wage_tax", "Wage tax YTD"))}
    ev = st.dataframe(view, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="slips_tbl",
                      column_config={"id": None, "period": "Month", "person": "Whose", "employer": "Employer", **money})
    rows = ev.selection.rows if ev and ev.selection else []
    if rows:
        sel = view.iloc[rows[0]]
        if st.button(f":red[Delete the {sel['period']} payslip]", key="slip_del"):
            _dlg_delete(con, int(sel["id"]), sel["period"])


@st.dialog("Delete payslip?")
def _dlg_delete(con, sid, period):
    st.markdown(f"Removes the saved **{period}** payslip figures. The PDF itself isn't stored, so nothing else changes.")
    c = st.columns(2)
    if c[0].button("Cancel", width="stretch"):
        st.rerun()
    if c[1].button(":red[**Delete**]", width="stretch"):
        con.execute("DELETE FROM payslips WHERE id=?", (sid,))
        con.commit()
        st.rerun()


def _apply(con, profile, year):
    res = {who: payslips.yearly(con, who, year) for who in PERSONS}
    res = {k: v for k, v in res.items() if v}
    if not res:
        return
    st.markdown(f"**{year} from your payslips**")
    cols = st.columns(len(res))
    for col, (who, r) in zip(cols, res.items()):
        proj, ytd = r["projected"], r["ytd"]
        with col.container(border=True):
            cur = profile.get("me_salary_eur" if who == "me" else "partner_salary_eur") or 0
            st.metric(f"{PERSONS[who]}: gross for the year", eur(proj.get("gross")), f"currently set {eur(cur)}",
                       delta_color="off", delta_arrow="off")
            st.caption(f"From the {r['source']}, scaled to 12 months"
                        + (", plus 8% holiday pay not yet paid out" if r.get("holiday_added") else "") + ".")
            st.dataframe(pd.DataFrame([
                ("Gross", ytd.get("gross"), proj.get("gross")),
                ("Pension (employee)", ytd.get("pension"), proj.get("pension")),
                ("30% ruling allowance", ytd.get("et_allowance"), proj.get("et_allowance")),
                ("Wage tax withheld", ytd.get("wage_tax"), proj.get("wage_tax")),
                ("Net", ytd.get("net"), proj.get("net")),
            ], columns=["", "So far", "Full year (est.)"]), hide_index=True, width="stretch",
                column_config={"So far": st.column_config.NumberColumn(format="euro"),
                               "Full year (est.)": st.column_config.NumberColumn(format="euro")})
    if st.button("Use payslip figures for salary, pension and expat ruling", type="primary", key="slip_apply"):
        p = dict(profile)
        for who, r in res.items():
            g = r["projected"].get("gross")
            if g:
                p["me_salary_eur" if who == "me" else "partner_salary_eur"] = int(round(g, -2))
            if r.get("pension_pct"):
                p["sal_pension_pct"] = round(r["pension_pct"] * 100, 2)
            if (r["ytd"].get("et_allowance") or 0) > 0:
                er = dict(p.get("expat_ruling") or {})
                er.update(has=True, who=who)
                p["expat_ruling"] = er
        db.set_setting(con, "profile", p)
        st.toast("Salary, pension and expat-ruling settings updated from your payslips")
        st.rerun()


def wage_tax_ytd(con, year: int) -> dict:
    """Dutch wage tax withheld this year per person, from the latest payslip with a YTD figure (or the sum)."""
    out = {}
    for who in PERSONS:
        r = payslips.yearly(con, who, year)
        if r and r["ytd"].get("wage_tax"):
            out[who] = {"so_far": r["ytd"]["wage_tax"], "projected": r["projected"].get("wage_tax"), "through": r["latest"]}
    return out
