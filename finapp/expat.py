"""
US-expat-in-the-Netherlands checks.

Everything here is an estimate meant to tell you *what to look at*. It does not
replace a cross-border tax adviser, and it doesn't file anything.
"""
from datetime import date

import pandas as pd

from . import rules, taxtreat
from .analysis import balance_history, balance_on

FUND_PLATFORMS = r"degiro|meesman|brand new day|peaks|bux|saxo|trading 212|lynx|abn amro beleggen|ing beleggen|rabo beleggen|vwrl|vwce|iwda|ishares|vanguard ftse|northern trust|bewaarbedrijf"


# ------------------------------------------------------------------ helpers

def age_on(birth: str, on: date) -> int:
    b = pd.Timestamp(birth).date()
    return on.year - b.year - ((on.month, on.day) < (b.month, b.day))


def band(age: int) -> str | None:
    if age < 0 or age >= 18:
        return None
    return "0-5" if age <= 5 else "6-11" if age <= 11 else "12-17"


# ------------------------------------------------------------------ US: FBAR and Form 8938

def foreign_account_maxima(con, conv, year: int, usd_per_eur_override: float | None = None) -> pd.DataFrame:
    """
    Max balance per non-US account during `year`, converted to USD at the year-end rate.
    FBAR officially uses the Treasury Reporting Rate for 31 Dec; ECB's rate is close.
    Enter the Treasury rate on the US page to match it exactly.
    """
    b = balance_history(con, conv)
    accts = pd.read_sql("SELECT * FROM accounts WHERE country != 'US'", con)
    rate = usd_per_eur_override or conv.year_end_usd_per_eur(year)
    rows = []
    for _, a in accts.iterrows():
        ab = b[(b["account_id"] == a["id"])] if not b.empty else b
        in_year = ab[ab["date"].dt.year == year] if not ab.empty else ab
        # the balance carried into the year also counts as held on 1 Jan
        prior = ab[ab["date"].dt.year < year].tail(1) if not ab.empty else ab
        vals = pd.concat([prior["balance"], in_year["balance"]]) if not ab.empty else pd.Series(dtype=float)
        if a["kind"] in ("credit", "loan", "mortgage"):
            continue
        if vals.empty:
            rows.append({"account": a["name"], "currency": a["currency"], "max_local": None,
                         "max_usd": None, "year_end_usd": None, "note": "No balances recorded"})
            continue
        mx = float(vals.max())
        ye = float(vals.iloc[-1])
        to_usd = lambda v: v / conv.per_eur(a["currency"], date(year, 12, 31)) * rate
        rows.append({"account": a["name"], "currency": a["currency"], "max_local": mx,
                     "max_usd": to_usd(mx), "year_end_usd": to_usd(ye), "note": ""})
    return pd.DataFrame(rows)


def fbar_8938(con, conv, year: int, filing_status: str, usd_per_eur_override=None) -> dict:
    r = rules.get(year)
    m = foreign_account_maxima(con, conv, year, usd_per_eur_override)
    agg_max = m["max_usd"].fillna(0).sum() if not m.empty else 0
    agg_ye = m["year_end_usd"].fillna(0).sum() if not m.empty else 0
    ye_thr, any_thr = r["form8938_abroad"].get(filing_status, r["form8938_abroad"]["single"])
    missing = int(m["max_usd"].isna().sum()) if not m.empty else 0
    return {
        "accounts": m,
        "aggregate_max_usd": agg_max,
        "aggregate_year_end_usd": agg_ye,
        "fbar_required": agg_max > r["fbar_threshold_usd"],
        "fbar_threshold": r["fbar_threshold_usd"],
        "f8938_required": agg_ye > ye_thr or agg_max > any_thr,
        "f8938_thresholds": (ye_thr, any_thr),
        "missing_balances": missing,
    }


def pfic_flags(con) -> list[str]:
    hits = []
    for a in con.execute("SELECT name, kind, country FROM accounts WHERE country != 'US' AND kind IN ('brokerage','retirement')"):
        hits.append(f"Account **{a['name']}** is a non-US {a['kind']} account.")
    rows = con.execute(
        "SELECT DISTINCT COALESCE(counterparty,'') || ' ' || COALESCE(description,'') AS txt FROM transactions").fetchall()
    import re
    rx = re.compile(FUND_PLATFORMS, re.I)
    found = sorted({m.group(0).lower() for r in rows if (m := rx.search(r["txt"]))})
    if found:
        hits.append("Payments to fund platforms or EU-domiciled funds: " + ", ".join(found) + ".")
    return hits


# ------------------------------------------------------------------ US: CTC and FEIE vs FTC

def us_family_credits(profile: dict, conv, year: int) -> dict:
    r = rules.get(year)
    ctc = r["ctc"]
    fs = profile.get("filing_status", "MFJ")
    usd = conv.year_avg_usd_per_eur(year)
    earned_eur = profile.get("me_salary_eur", 0) or 0
    if fs == "MFJ":
        earned_eur += profile.get("partner_salary_eur", 0) or 0
    earned_usd = earned_eur * usd
    dec31 = date(year, 12, 31)
    kids = profile.get("kids", [])
    eligible = [k for k in kids if k.get("birth") and age_on(k["birth"], dec31) < 17 and k.get("has_ssn", True)]
    no_ssn = [k.get("name", "child") for k in kids if k.get("birth") and age_on(k["birth"], dec31) < 17 and not k.get("has_ssn", True)]
    parent_ssn = profile.get("me_has_ssn", True)

    n = len(eligible) if parent_ssn else 0
    full = ctc["per_child"] * n
    over = max(0, earned_usd - ctc["phaseout_start"].get(fs, 200_000))
    credit = max(0, full - 50 * -(-over // 1000))
    refundable_ftc = min(ctc["refundable_cap"] * n, max(0, ctc["phase_in"] * (earned_usd - ctc["earned_income_floor"])), credit)

    # salary paid tax-free under the expat ruling is still US income, but no Dutch tax was paid on it
    ruling = profile.get("expat_ruling", {})
    ruling_allowance_usd = 0
    if ruling.get("has"):
        g = profile.get("me_salary_eur", 0) if ruling.get("who", "me") == "me" else profile.get("partner_salary_eur", 0)
        ruling_allowance_usd = expat_ruling_check(profile, year).get("allowance", 0) * usd if g else 0

    return {
        "usd_per_eur_avg": usd,
        "earned_usd": earned_usd,
        "eligible_children": [k.get("name", "child") for k in eligible],
        "children_without_ssn": no_ssn,
        "parent_ssn": parent_ssn,
        "credit_total": credit,
        "actc_with_ftc": refundable_ftc,
        "actc_with_feie": 0.0,
        "feie_max": r["feie_max_usd"],
        "ruling_allowance_usd": ruling_allowance_usd,
    }


# ------------------------------------------------------------------ NL: Box 3

def box3_settings(profile: dict) -> tuple[bool, bool]:
    """(include_roth, include_traditional). Older single setting switches both on."""
    old = bool(profile.get("box3_include_us_retirement", False))
    return (bool(profile.get("box3_include_roth", old)), bool(profile.get("box3_include_traditional", old)))


def box3_assets_from_balances(con, conv, year: int, profile: dict | bool | None = None) -> dict:
    """Peildatum 1 January: bank, other assets, debts. Each account is split by tax treatment first,
    so a mixed 401(k) can put its Roth part and its pre-tax part in different buckets."""
    if isinstance(profile, bool):   # older call style
        profile = {"box3_include_us_retirement": profile}
    inc_roth, inc_trad = box3_settings(profile or {})
    last = balance_on(con, conv, pd.Timestamp(year, 1, 1))
    out = {"bank": 0.0, "other": 0.0, "debts": 0.0, "detail": [], "unset_retirement": []}
    if last.empty:
        return out
    for _, r in last.iterrows():
        eur = float(r["eur"])
        t, guessed = taxtreat.effective(r)
        if guessed and r["kind"] == "retirement":
            out["unset_retirement"].append(r["name"])
        for part, amt in taxtreat.portions(r, eur):
            bucket = taxtreat.box3_bucket(part, inc_roth, inc_trad)
            if bucket == "bank":
                out["bank"] += amt
            elif bucket == "other":
                out["other"] += amt
            elif bucket == "debt":
                out["debts"] += abs(amt)
            out["detail"].append({"account": r["name"], "treatment": taxtreat.label(part) + (" (guessed)" if guessed and t not in ("cash", "debt") else ""),
                                  "eur": amt, "bucket": {"bank": "savings", "other": "investments", "debt": "debt"}.get(bucket, "left out")})
    return out


REASON_OUT = {
    "traditional": "Pre-tax retirement: treated as a pension under the US–NL treaty (switch above to count it)",
    "roth": "Roth: left out (switch above to count it)",
    "nl_pension": "Dutch pension product: box 1, not box 3",
    "options": "Stock options: taxed in box 1 when exercised, not box 3",
    "mortgage": "Own-home mortgage: belongs to box 1 with the house",
}


def box3_accounts(con, conv, peil, profile: dict | None = None) -> pd.DataFrame:
    """One row per account (two for a mixed plan) with its value on the peildatum and where it goes in box 3.

    If an account has no value on or before the peildatum, its earliest later value is used and marked
    as estimated, so an investment account first imported in October still counts."""
    from .analysis import balance_history
    inc_roth, inc_trad = box3_settings(profile or {})
    peil = pd.Timestamp(peil)
    accts = pd.read_sql("SELECT id, name, kind, currency, tax_treatment, roth_share FROM accounts ORDER BY name", con)
    hist = balance_history(con, conv)
    rows = []
    for _, a in accts.iterrows():
        h = hist[hist["account_id"] == a["id"]] if not hist.empty else hist
        before = h[h["date"] <= peil] if len(h) else h
        if len(before):
            r = before.sort_values("date").iloc[-1]
            src, dt = "on peildatum", r["date"]
        elif len(h):
            r = h.sort_values("date").iloc[0]
            src, dt = "estimated", r["date"]
        else:
            r, src, dt = None, "no value", None
        val = 0.0 if r is None else float(r["balance"]) / conv.per_eur(a["currency"], peil if src == "on peildatum" else dt)
        t, guessed = taxtreat.effective(a)
        for part, amt in taxtreat.portions(a, val):
            bucket = taxtreat.box3_bucket(part, inc_roth, inc_trad)
            reason = ""
            if a["kind"] == "mortgage":
                bucket, reason = "excluded", REASON_OUT["mortgage"]
            elif bucket == "excluded":
                reason = REASON_OUT.get(part, "Left out")
            rows.append({"account_id": int(a["id"]), "part": part, "account": a["name"] + (f" ({taxtreat.label(part).split(':')[0]} part)" if t == "mixed" else ""),
                         "treatment": taxtreat.label(part) + (" (guessed)" if guessed and t not in ("cash", "debt") else ""),
                         "guessed": guessed and a["kind"] == "retirement", "value": amt, "date": dt, "source": src,
                         "bucket": {"bank": "Savings", "other": "Investments", "debt": "Debt"}.get(bucket, "Left out"),
                         "reason": reason})
    return pd.DataFrame(rows, columns=["account_id", "part", "account", "treatment", "guessed", "value", "date", "source",
                                       "bucket", "reason"])


def box3_totals(con, conv, year: int, profile: dict) -> tuple[float, float, float]:
    """(bank, other, debts) for the peildatum of `year`, using values saved on the Dutch tax page where set."""
    from . import db
    acc = box3_accounts(con, conv, pd.Timestamp(year, 1, 1), profile)
    saved_all = db.get_setting(con, "box3_values", {}) or {}
    saved = saved_all.get(str(year), {})
    ex = saved_all.get(f"{year}_extra", {})
    if len(acc):
        keys = [f"{i}:{t}" for i, t in zip(acc["account_id"], acc["part"])]
        acc["use"] = [float(saved.get(k, v)) for k, v in zip(keys, acc["value"])]
    else:
        acc["use"] = []
    bank = acc.loc[acc["bucket"] == "Savings", "use"].sum()
    other = acc.loc[acc["bucket"] == "Investments", "use"].sum() + float(ex.get("assets", 0.0))
    debts = abs(acc.loc[acc["bucket"] == "Debt", "use"].clip(upper=0).sum()) + float(ex.get("debts", 0.0))
    return float(bank), float(other), float(debts)


def partial_non_resident_option(profile: dict, year: int) -> bool:
    """30% ruling holders who had it in December 2023 can still opt for partial non-resident status up to 2026,
    which leaves box 3 out except Dutch real estate."""
    er = profile.get("expat_ruling") or {}
    return bool(er.get("has")) and int(er.get("first_applied_year", 2025)) <= 2023 and year <= 2026


def retirement_by_treatment(con, conv) -> pd.DataFrame:
    """Latest value of every account, split into pre-tax / Roth / taxable / other, in USD."""
    last = balance_on(con, conv, pd.Timestamp.today().normalize())
    rows = []
    if last.empty:
        return pd.DataFrame(columns=["account", "treatment", "as_of", "usd", "pre_tax", "roth", "taxable", "other"])
    usd = conv.usd_per_eur()
    for _, r in last.iterrows():
        t, guessed = taxtreat.effective(r)
        if t in ("cash", "debt", "options"):
            continue
        row = {"account": r["name"], "treatment": taxtreat.label(t) + (" (guessed)" if guessed else ""),
               "as_of": r["date"].date(), "usd": float(r["eur"]) * usd, "pre_tax": 0.0, "roth": 0.0, "taxable": 0.0, "other": 0.0}
        if t == "mixed" and pd.notna(r.get("roth_share")):
            row["treatment"] += f" · {float(r['roth_share']):.0%} Roth"
        for part, amt in taxtreat.portions(r, float(r["eur"]) * usd):
            key = {"traditional": "pre_tax", "roth": "roth", "taxable": "taxable"}.get(part, "other")
            row[key] += amt
        rows.append(row)
    return pd.DataFrame(rows)


def box3(bank: float, other: float, debts: float, partners: bool, year: int) -> dict:
    p = rules.get(year)["box3"]
    n = 2 if partners else 1
    debt_net = max(0.0, debts - p["debt_threshold_per_person"] * n)
    base = bank + other - debt_net                                  # rendementsgrondslag
    ret = bank * p["rate_bank"] + other * p["rate_other"] - debt_net * p["rate_debt"]
    eff = ret / base if base > 0 else 0.0
    taxable = max(0.0, base - p["exemption_per_person"] * n)        # grondslag sparen en beleggen
    benefit = taxable * eff
    tax = max(0.0, benefit * p["tax_rate"])
    # what-if: same wealth all in savings
    return {
        "base": base, "notional_return": ret, "effective_rate": eff, "exemption": p["exemption_per_person"] * n,
        "taxable_base": taxable, "taxable_benefit": benefit, "tax": tax, "params": p,
        "headroom": p["exemption_per_person"] * n - base,
    }


# ------------------------------------------------------------------ NL: child benefits

def kinderbijslag_expected(kids: list, year: int) -> dict:
    p = rules.get(year)["kinderbijslag_quarter"]
    total = 0.0
    per_child = {}
    for k in kids:
        if not k.get("birth"):
            continue
        amt = 0.0
        for q, (m, half) in enumerate([(1, "H1"), (4, "H1"), (7, "H2"), (10, "H2")], start=1):
            b = band(age_on(k["birth"], date(year, m, 1)))
            # benefit starts the quarter after birth
            if b and pd.Timestamp(k["birth"]) < pd.Timestamp(year, m, 1):
                amt += p[half][b]
        per_child[k.get("name", "child")] = amt
        total += amt
    return {"total": total, "per_child": per_child, "verify": p.get("verify")}


def kindgebonden_budget(kids: list, income: float, assets: float, partner: bool, year: int) -> dict:
    p = rules.get(year)["kindgebonden_budget"]
    on = date(year, 1, 1)
    max_assets = p["max_assets_partner"] if partner else p["max_assets_single"]
    under18 = [k for k in kids if k.get("birth") and 0 <= age_on(k["birth"], on) < 18]
    if not under18:
        return {"amount": 0.0, "reason": "No children under 18"}
    if assets > max_assets:
        return {"amount": 0.0, "reason": f"Assets on 1 Jan above €{max_assets:,.0f}"}
    mx = 0.0
    for k in under18:
        a = age_on(k["birth"], on)
        mx += p["per_child"] + (p["extra_12_15"] if 12 <= a <= 15 else p["extra_16_17"] if a >= 16 else 0)
    if not partner:
        mx += p["alo_kop_single_parent"]
    thr = p["income_threshold_partner"] if partner else p["income_threshold_single"]
    amount = max(0.0, mx - p["taper"] * max(0.0, income - thr))
    zero_point = thr + mx / p["taper"]
    return {"amount": amount, "max": mx, "threshold": thr, "zero_point": zero_point, "reason": "", "verify": True}


# ------------------------------------------------------------------ NL: expat (30%) ruling

def expat_ruling_check(profile: dict, year: int) -> dict:
    er = profile.get("expat_ruling", {})
    if not er.get("has"):
        return {}
    p = rules.get(year)["expat_ruling"]
    gross = profile.get("me_salary_eur", 0) if er.get("who", "me") == "me" else profile.get("partner_salary_eur", 0)
    gross = gross or 0
    norm = p["salary_norm_u30_master"] if er.get("under30_master") else p["salary_norm"]
    allowance = min(gross * p["rate"], p["max_allowance"], max(0.0, gross - norm))
    taxable = gross - allowance
    first = er.get("first_applied_year", 2025)
    rate_2027 = p["rate"] if first <= 2023 else p["rate_2027"]
    norm_2027 = p["salary_norm_2027_new"] if first >= 2025 and not er.get("under30_master") else norm
    allowance_2027 = min(gross * rate_2027, max(0.0, gross - norm_2027))
    return {
        "gross": gross, "norm": norm, "allowance": allowance, "taxable": taxable,
        "capped_by_norm": allowance < gross * p["rate"],
        "margin": taxable - norm,
        "rate_2027": rate_2027, "norm_2027": norm_2027, "allowance_2027": allowance_2027,
        "first_applied_year": first,
    }


# ------------------------------------------------------------------ action list

def opportunities(con, conv, profile: dict, tx: pd.DataFrame, year: int) -> list[dict]:
    """Plain-language checklist of things worth acting on. severity: act / check / info."""
    out = []
    add = lambda sev, area, title, body: out.append({"severity": sev, "area": area, "title": title, "body": body})
    partner = profile.get("partner", True)
    kids = profile.get("kids", [])
    fs = profile.get("filing_status", "MFJ")
    yr_tx = tx[tx["date"].dt.year == year] if not tx.empty else tx

    # ---- US filing
    f = fbar_8938(con, conv, year, fs, profile.get("treasury_usd_per_eur"))
    if f["fbar_required"]:
        add("act", "US", "FBAR (FinCEN 114) required",
            f"Your non-US accounts peaked at about ${f['aggregate_max_usd']:,.0f} combined, above the $10,000 threshold. "
            "File on the BSA E-Filing site by 15 April (automatic extension to 15 October). List every foreign account, "
            "including ones you can sign on but don't own.")
    if f["f8938_required"]:
        add("act", "US", "Form 8938 (FATCA) likely required",
            f"Foreign assets exceed the abroad thresholds for {fs} (${f['f8938_thresholds'][0]:,.0f} year-end / "
            f"${f['f8938_thresholds'][1]:,.0f} any time). Dutch pensions and investment accounts can count here too.")
    if f["missing_balances"]:
        add("check", "US", "Some foreign accounts have no balance history",
            "FBAR needs the *maximum* balance of each account during the year. Add balance snapshots on the Accounts page.")

    pf = pfic_flags(con)
    if pf:
        add("act", "US", "Possible PFIC holdings",
            " ".join(pf) + " EU-domiciled funds and ETFs are usually PFICs for US tax purposes, which means Form 8621 per fund and "
            "punitive default taxation. Common fixes: hold individual stocks, use a US broker that serves expats, or "
            "make a QEF/mark-to-market election with an adviser. Dutch pension schemes are generally not PFICs.")

    cr = us_family_credits(profile, conv, year)
    if cr["eligible_children"]:
        add("check", "US", "Foreign Tax Credit vs. FEIE: the Child Tax Credit tips it",
            f"With the Foreign Tax Credit, the refundable Child Tax Credit could pay about ${cr['actc_with_ftc']:,.0f} "
            f"for {len(cr['eligible_children'])} child(ren). Claiming the FEIE (Form 2555) blocks the refundable part entirely. "
            "Because Dutch rates are higher than US rates, FTC usually leaves you owing no US tax anyway, and the excess credit carries forward 10 years.")
    if cr["children_without_ssn"]:
        add("act", "US", "Children without an SSN",
            "No Child Tax Credit without an SSN issued before the return due date: " + ", ".join(cr["children_without_ssn"]) +
            ". Apply via a Consular Report of Birth Abroad and SS-5 at the US consulate in Amsterdam.")
    if cr["ruling_allowance_usd"]:
        add("check", "US", "Expat-ruling allowance is US-taxable",
            f"About ${cr['ruling_allowance_usd']:,.0f} paid tax-free in NL is still income on your US return, with no Dutch tax "
            "to credit against it. Run your FTC numbers with that in mind; it can create a small US liability.")
    add("info", "US", "IRAs need US-taxable compensation",
        "Income excluded with the FEIE can't fund an IRA. Under the FTC route it can, but check Dutch Box 3 treatment of a Roth IRA "
        "before contributing, and remember the US-NL treaty pension article.")

    # ---- NL Box 3
    bank, other, debts = box3_totals(con, conv, year, profile)
    b3 = box3(bank, other, debts, partner, year)
    if partial_non_resident_option(profile, year) and b3["tax"] > 0:
        add("check", "NL", f"Partial non-resident status could make box 3 €0 for {year}",
            "Your expat ruling already applied in December 2023, so you can still choose partial non-resident taxpayer "
            f"status in the {year} return (the last year it's allowed). Box 3 would then only cover Dutch real estate "
            "other than your home. See Dutch tax & benefits, and confirm with your adviser.")
    if b3["tax"] > 0:
        add("check", "NL", f"Box 3 tax about €{b3['tax']:,.0f} for {year}",
            f"Based on assets on 1 January (€{b3['base']:,.0f} after debts). If your real return was lower than the notional "
            f"{b3['effective_rate']:.2%}, file an Opgaaf Werkelijk Rendement (tegenbewijsregeling). Fiscal partners can split box 3 "
            "assets between them in whatever ratio works best.")
    elif b3["headroom"] < 15_000 and b3["base"] > 0:
        add("info", "NL", "Close to the box 3 exemption",
            f"You're €{b3['headroom']:,.0f} below the tax-free amount. What counts is the balance on 1 January, so large purchases "
            "or debt repayments planned for early January can be worth doing in late December instead.")

    # ---- NL child benefits
    if kids:
        kb = kinderbijslag_expected(kids, year)
        got = yr_tx.loc[yr_tx["category"] == "Income: Kinderbijslag", "eur"].sum() if not yr_tx.empty else 0
        if kb["total"] and got < kb["total"] * 0.2 and date.today().year == year and date.today().month > 4:
            add("act", "NL", "Kinderbijslag not showing up",
                f"Expected about €{kb['total']:,.0f} this year; your imported statements show €{got:,.0f}. "
                "Check Mijn SVB, or recategorise the SVB payments if they're there under another label.")

        income = (profile.get("me_salary_eur", 0) or 0) + ((profile.get("partner_salary_eur", 0) or 0) if partner else 0)
        er = expat_ruling_check(profile, year)
        if er:
            income -= er["allowance"]  # toetsingsinkomen excludes the tax-free allowance
        kgb = kindgebonden_budget(kids, income, b3["base"], partner, year)
        toeslag = yr_tx.loc[yr_tx["category"] == "Income: Toeslagen", "eur"].sum() if not yr_tx.empty else 0
        if kgb.get("amount", 0) > 100 and toeslag <= 0:
            add("act", "NL", f"You may qualify for kindgebonden budget (about €{kgb['amount']:,.0f}/yr)",
                "Rough estimate from your household income and 1 January assets. Do a proefberekening at toeslagen.nl. "
                "You can still apply for this year until 31 December next year. Note this is a benefit, not a tax refund, so it doesn't affect your US return.")

        childcare = -yr_tx.loc[yr_tx["category"] == "Family: Childcare", "eur"].sum() if not yr_tx.empty else 0
        if childcare > 500 and toeslag <= 0:
            add("act", "NL", "Childcare costs but no kinderopvangtoeslag received",
                f"You spent about €{childcare:,.0f} on childcare this year. If both partners work and the provider is LRK-registered, "
                "kinderopvangtoeslag can cover a large share. Apply within 3 months after the month care starts.")

    # ---- NL expat ruling
    er = expat_ruling_check(profile, year)
    if er:
        if er["margin"] < 0.05 * er["norm"]:
            add("act", "NL", "Expat ruling: close to the salary norm",
                f"Taxable salary is €{er['taxable']:,.0f} against a norm of €{er['norm']:,.0f}. Dropping below it, e.g. by cutting hours, ends the ruling.")
        add("info", "NL", "Expat ruling changes in 2027",
            f"From 2027 your tax-free allowance is {er['rate_2027']:.0%} and the salary norm about €{er['norm_2027']:,.0f} "
            f"(before indexation). Estimated allowance falls from €{er['allowance']:,.0f} to €{er['allowance_2027']:,.0f}.")

    order = {"act": 0, "check": 1, "info": 2}
    return sorted(out, key=lambda x: order[x["severity"]])
