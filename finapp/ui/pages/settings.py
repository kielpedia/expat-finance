"""Household & settings page."""
import os
from datetime import date

import pandas as pd
import streamlit as st

from finapp import ai, categorize, db, expat, fx, keystore
from finapp.ui import common
from finapp.ui.common import (
    eur,
    load_tx,
)


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
    st.header("Household & settings")
    p = dict(profile)

    # ---------------------------------------------------------- payslips, then the statement-based estimator
    from finapp.ui import payslips as ui_payslips
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
        SSN_OPTS = {True: "Yes", False: "No", None: "Not sure"}
        kids_df = pd.DataFrame(p["kids"] or [], columns=["name", "birth", "has_ssn"])
        kids_df["birth"] = pd.to_datetime(kids_df["birth"], errors="coerce").dt.date
        kids_df["has_ssn"] = [SSN_OPTS.get(x if isinstance(x, bool) else None) for x in kids_df["has_ssn"]]
        kids_ed = st.data_editor(kids_df, num_rows="dynamic", hide_index=True, width="stretch",
                                 column_config={"birth": st.column_config.DateColumn("Date of birth"),
                                                "has_ssn": st.column_config.SelectboxColumn(
                                                    "Has SSN", options=list(SSN_OPTS.values()), default="Not sure",
                                                    help="Needed for the US Child Tax Credit. Only 'Yes' counts.")})

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
                          "has_ssn": {"Yes": True, "No": False}.get(r["has_ssn"])} for i, (_, r) in enumerate(kids_ed.iterrows())
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
    store_ok = keystore.store_available()
    saved = keystore.has_saved_key()
    with st.form("ai_form"):
        c = st.columns([3, 2, 2])
        if env_key:
            c[0].text_input("API key", "Using ANTHROPIC_API_KEY from your environment", disabled=True)
            new_key = None
        else:
            new_key = c[0].text_input("API key", type="password", disabled=not store_ok,
                                      placeholder="•••••••• (saved)" if saved else "sk-ant-…")
        model = c[1].text_input("Model", ai_cfg.get("model") or ai.DEFAULT_MODEL)
        incl = c[2].checkbox("Also send a rounded typical amount (helps with vague payees)", ai_cfg.get("include_amounts", False))
        b = st.columns([1, 1, 4])
        if b[0].form_submit_button("Save"):
            try:
                if new_key:
                    keystore.set_key(new_key)
                ai_cfg.pop("api_key", None)
                ai_cfg.update(model=model.strip() or ai.DEFAULT_MODEL, include_amounts=incl)
                db.set_setting(con, "ai", ai_cfg)
                st.success("Saved.")
            except RuntimeError as e:
                st.error(str(e))
        if b[1].form_submit_button("Remove key") and saved:
            keystore.delete_key()
            st.success("Key removed.")
    if not env_key:
        st.caption("The key is kept in your computer's credential store (Windows Credential Manager, macOS Keychain or "
                   "your Linux keyring), not in the database, so backups don't contain it."
                   if store_ok else
                   "No credential store was found on this computer, so the app can't save a key. Set the "
                   "ANTHROPIC_API_KEY environment variable instead.")
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
        rows = [r for _, r in ed.iterrows() if isinstance(r["pattern"], str) and r["pattern"].strip()
                and isinstance(r["category"], str) and r["category"].strip()]
        bad = [(r["pattern"], categorize.check_pattern(r["pattern"])) for r in rows if categorize.check_pattern(r["pattern"])]
        if bad:
            st.error("Not saved. Fix these patterns first:\n\n" + "\n".join(f"- `{p}`: {why}" for p, why in bad))
        else:
            con.execute("DELETE FROM cat_rules")
            for r in rows:
                con.execute("INSERT INTO cat_rules(pattern, category, priority) VALUES(?,?,?)",
                            (r["pattern"].strip(), r["category"].strip(), int(r["priority"]) if pd.notna(r["priority"]) else 100))
            con.commit()
            st.rerun()
    if c[1].button("Re-categorise everything"):
        st.success(f"Updated {categorize.apply(con)} transactions (hand-edited ones kept).")

    st.subheader("Backup")
    st.caption(f"All data lives in one file: `{db.DB_PATH}`. Copy it to back up; it's not encrypted, so keep it somewhere private.")
    if st.button("Prepare a backup copy"):
        st.session_state["backup_bytes"] = db.backup_bytes(con)
    if st.session_state.get("backup_bytes"):
        st.download_button("Download the backup", st.session_state["backup_bytes"],
                           f"finance-backup-{date.today():%Y-%m-%d}.db", on_click=lambda: st.session_state.pop("backup_bytes", None))

    st.subheader("Restore from a backup")
    st.caption("Replaces **all** data in this app with a `.db` backup, or the `finance.db` file from another install. "
               "A copy of your current data is saved next to the database first.")
    if done := st.session_state.pop("restore_done", None):
        st.success(done)
    round_ = st.session_state.get("restore_round", 0)      # a new key empties the uploader after a restore
    upload = st.file_uploader("Backup file", type=["db", "sqlite", "sqlite3"], key=f"restore_upload_{round_}")
    if not upload:
        return
    data = upload.getvalue()
    describe = lambda s: (f"{s['accounts']:,} accounts, {s['transactions']:,} transactions"
                          + (f", {s['first']} to {s['last']}" if s["first"] else ""))
    try:
        found = db.inspect_backup(data)
    except db.BackupError as e:
        st.error(str(e))
        return
    st.markdown(f"**In this file:** {describe(found)}  \n**Will be replaced:** {describe(db.summary(con))}")
    sure = st.checkbox("Replace all my current data with this file", key=f"restore_confirm_{round_}")
    if st.button("Restore this backup", type="primary", disabled=not sure):
        try:
            safety = db.restore_bytes(con, data)
        except db.BackupError as e:
            st.error(str(e))
            return
        st.session_state["restore_round"] = round_ + 1
        st.session_state["restore_done"] = f"Restored {describe(found)}. Your previous data is saved as `{safety}`."
        st.rerun()
