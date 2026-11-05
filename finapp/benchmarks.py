"""
Spending check: your own trend, Dutch benchmarks, and your municipality's fixed local charges.

Benchmarks (as of Nov 2026; update yearly):
- CBS Budgetonderzoek 2020, spending shares by household type (5 groups), as published by ABN AMRO
  ("Weet jij wat je elke maand uitgeeft?"). CBS runs this survey every 5 years; 2020 is the latest.
- Groceries: ABN AMRO median €585/month (bank data of ~150,000 salaried households, Sep 2024-Aug 2025);
  Nibud minimum for healthy home-cooked food (single €260-290, two adults ~€500, 2+2 children from ~€630).
- Municipal charges per municipality (MUNICIPAL_CHARGES below), each with its source. Anyone can add their
  own municipality there, or enter their rates on Household & settings.
Shares compare *how you split* spending, so they work at any income level. They are a conversation
starter, not a verdict: a family with childcare or a long commute will differ for good reasons.
"""
import re

import pandas as pd

from .categorize import group_of

SOURCES = {
    "cbs_shares": ("CBS 2020 via ABN AMRO",
                   "https://www.abnamro.nl/nl/prive/themas/hulp-bij-geldzaken/inzicht-inkomsten-uitgaven/weet-jij-wat-je-elke-maand-uitgeeft.html"),
    "cbs_amounts": ("CBS StatLine 85419NED", "https://www.cbs.nl/nl-nl/cijfers/detail/85419NED"),
    "groceries": ("Knab, citing ABN AMRO and Nibud", "https://bieb.knab.nl/inkomsten-uitgaven/boodschappen-per-maand-wat-is-normaal"),
}

# CBS 2020 shares of total spending by household type
GROUPS = ["Housing, water & energy", "Food & drink", "Transport, leisure & eating out",
          "Clothing, furnishing & appliances", "Other"]
CBS_SHARES = {
    "all":                  [33.2, 16.0, 24.4, 10.4, 16.0],
    "single":               [41.05, 16.2, 19.75, 8.6, 14.95],   # average of single man / single woman
    "single_parent":        [33.7, 15.9, 22.3, 11.2, 17.1],
    "couple_children":      [27.3, 15.9, 27.6, 11.9, 17.1],
    "couple_no_children":   [33.3, 15.6, 25.4, 10.1, 15.5],
}
HOUSEHOLD_LABEL = {"all": "all Dutch households", "single": "single people", "single_parent": "single-parent families",
                   "couple_children": "couples with children", "couple_no_children": "couples without children"}

# app category group -> CBS group (sub-category overrides first)
CAT_MAP = {
    "Housing: Rent": 0, "Housing: Mortgage": 0, "Housing: Utilities": 0, "Housing: HOA / VvE": 0,
    "Housing: Internet & phone": 4, "Housing: Home & DIY": 3,
    "Food: Groceries": 1, "Food: Dining & takeaway": 2,
    "Family: Kids & household": 3, "Family: Childcare": 4, "Family: School": 4, "Family: Pets": 4,
}
GROUP_MAP = {"Housing": 0, "Food": 1, "Transport": 2, "Travel": 2, "Subscriptions": 2, "Shopping": 3,
             "Family": 4, "Insurance": 4, "Health": 4, "Taxes": 4, "Fees": 4, "Cash": 4}

# Fixed local charges by municipality. To add one: copy a block, fill in the year's tariffs from the
# municipality's (or its tax collector's) tariff page, and cite it in "source".
MUNICIPAL_CHARGES = {
    "Utrecht": {
        "year": 2026,
        "collector": "BghU",
        "afval": {1: 346.25, 2: 443.30, 3: 527.80, 4: 600.05},   # waste charge by registered residents; 4 = 4 or more
        "riool_owner": 247.27,                                   # sewer charge, owners only
        "ozb_owner_pct": 0.0008060,                              # property tax, fraction of the WOZ value
        "payee": r"bghu|gemeente utrecht",
        "water_board": "HDSR",
        "source": ("BghU tarieven 2026 Utrecht", "https://bghu.nl/Alles-over/Belastingen/Tarieven-2026/Utrecht-2026"),
    },
}
OTHER_MUNICIPALITY = "Other (enter your own rates)"


def municipality_options() -> list[str]:
    return sorted(MUNICIPAL_CHARGES) + [OTHER_MUNICIPALITY]


GROCERY_MEDIAN = 585.0
NIBUD_FOOD_MIN = {(1, 0): 275.0, (2, 0): 500.0, (2, 2): 630.0}


def household_type(profile: dict) -> str:
    kids = [k for k in profile.get("kids", []) if k.get("birth")]
    partner = bool(profile.get("partner", True))
    if kids:
        return "couple_children" if partner else "single_parent"
    return "couple_no_children" if partner else "single"


def cbs_group(category: str) -> int:
    if category in CAT_MAP:
        return CAT_MAP[category]
    return GROUP_MAP.get(group_of(category), 4)


def shares_vs_cbs(spend: pd.DataFrame, htype: str, months: int) -> pd.DataFrame:
    """spend: spending rows (budget, not income; refunds offset). Returns per-group comparison."""
    s = spend.assign(g=spend["category"].map(cbs_group), amt=-spend["eur"])
    tot = s["amt"].sum()
    rows = []
    for i, name in enumerate(GROUPS):
        mine = s.loc[s["g"] == i, "amt"].sum()
        share = mine / tot * 100 if tot else 0
        bench = CBS_SHARES[htype][i]
        rows.append({"group": name, "you_month": mine / months, "you_share": share, "cbs_share": bench,
                     "diff_pts": share - bench, "diff_month": (share - bench) / 100 * tot / months})
    return pd.DataFrame(rows)


def trends(tx: pd.DataFrame, recent: int = 3, base: int = 9) -> pd.DataFrame:
    """Per category: average of the last `recent` full months vs the `base` months before them."""
    from .analysis import spending_rows
    e = spending_rows(tx).copy()
    if e.empty:
        return pd.DataFrame()
    months = sorted(e["month"].unique())
    if len(months) < recent + 3:
        return pd.DataFrame()
    last = months[-recent:]
    prev = months[-(recent + base):-recent]
    e["amt"] = -e["eur"]
    pm = e.pivot_table(index="category", columns="month", values="amt", aggfunc="sum", fill_value=0).clip(lower=0)
    out = pd.DataFrame({"recent": pm[last].mean(axis=1), "before": pm[[m for m in prev if m in pm]].mean(axis=1)})
    out["change"] = out["recent"] - out["before"]
    out["pct"] = (out["change"] / out["before"]).where(out["before"] > 0)
    # what's driving it: payees with the biggest increase in that category
    drivers = {}
    for cat in out.index:
        sub = e[e["category"] == cat]
        pr = sub[sub["month"].isin(last)].groupby("payee")["amt"].sum() / recent
        pb = sub[sub["month"].isin(prev)].groupby("payee")["amt"].sum() / max(1, len(prev))
        d = pr.subtract(pb, fill_value=0).sort_values(ascending=False)
        d = d[d > 5].head(3)
        drivers[cat] = ", ".join(f"{p} (+€{v:,.0f})" for p, v in d.items())
    out["drivers"] = pd.Series(drivers)
    out["months_recent"] = f"{pd.Timestamp(last[0]):%b}–{pd.Timestamp(last[-1]):%b %Y}"
    return out.reset_index().sort_values("change", ascending=False)


def groceries_check(tx: pd.DataFrame, profile: dict, months: int) -> dict:
    g = -tx.loc[(tx["category"] == "Food: Groceries") & (tx["budget"]), "eur"].sum() / max(1, months)
    adults = 2 if profile.get("partner", True) else 1
    kids = len([k for k in profile.get("kids", []) if k.get("birth")])
    key = (adults, min(kids, 2)) if (adults, min(kids, 2)) in NIBUD_FOOD_MIN else (adults, 0)
    nibud = NIBUD_FOOD_MIN.get(key, 500.0)
    if kids > 2:
        nibud += (kids - 2) * 65.0   # rough per extra child; Nibud doesn't publish a single figure
    return {"you": g, "median": GROCERY_MEDIAN, "nibud_min": nibud, "adults": adults, "kids": kids}


def municipal_charges(profile: dict) -> dict | None:
    """Expected yearly local charges from the household settings, or None if no municipality is set.

    Uses the built-in tariffs for a listed municipality, or the rates entered by hand for 'Other'."""
    name = profile.get("municipality")
    if not name:
        return None
    residents = int(profile.get("residents") or 1)
    owner = bool(profile.get("home_owner", False))
    woz = float(profile.get("woz") or 0)
    if name in MUNICIPAL_CHARGES:
        m = MUNICIPAL_CHARGES[name]
        afval = m["afval"][min(max(residents, 1), max(m["afval"]))]
        riool_owner, ozb_pct = m["riool_owner"], m["ozb_owner_pct"]
        label, payee = name, m["payee"]
        info = {"collector": m.get("collector") or f"Gemeente {name}", "water_board": m.get("water_board"),
                "source": m.get("source"), "year": m.get("year")}
    else:
        own = profile.get("local_rates") or {}
        label = (own.get("name") or "").strip() or "your municipality"
        afval = float(own.get("afval") or 0)
        riool_owner = float(own.get("riool") or 0)
        ozb_pct = float(own.get("ozb_pct") or 0) / 100
        payee = ("gemeente " + re.escape(label.lower())) if own.get("name") else r"gemeente"
        if own.get("collector"):
            payee += "|" + re.escape(own["collector"].lower())
        info = {"collector": own.get("collector") or f"Gemeente {label}", "water_board": None, "source": None, "year": None}
    riool = riool_owner if owner else 0.0
    ozb = woz * ozb_pct if owner and woz else 0.0
    return {"municipality": label, "residents": residents, "owner": owner, "woz": woz, "ozb_pct": ozb_pct,
            "afval": afval, "riool": riool, "ozb": ozb, "total": afval + riool + ozb, "payee": payee, **info}
