"""
Estimate gross salary from the net salary payments in your statements.

Works backwards from what actually arrived in your account over the last 12 months, using 2026
Dutch wage tax (box 1 brackets, algemene heffingskorting, arbeidskorting), an employee pension
contribution, and the expat ruling if it applies. It's an estimate: payslip extras like a lease car,
bonuses paid through payroll, or deductions for benefits will move the real figure.
"""
import pandas as pd

from . import rules

# Built-in 2026 figures, used if rules.py has no wage-tax block for the year
# (e.g. an older rules.py still loaded in a running app).
FALLBACK = {
    "brackets": [(38_883, 0.3575), (78_426, 0.3756), (None, 0.4950)],
    "ahk": {"max": 3_115, "phase_start": 29_736, "phase_rate": 0.06398},
    "arbeidskorting": [(11_965, 0.0, 0.08324, 0), (25_845, 996.0, 0.31009, 11_965),
                       (45_592, 5_300.0, 0.01950, 25_845), (132_920, 5_685.0, -0.06510, 45_592)],
    "holiday_allowance": 0.08,
}


def params(year: int) -> dict:
    return rules.get(year).get("nl_wage_tax") or FALLBACK


def _brackets(taxable, p):
    tax, lower = 0.0, 0.0
    for upper, rate in p["brackets"]:
        top = taxable if upper is None else min(taxable, upper)
        if top > lower:
            tax += (top - lower) * rate
        if upper is None or taxable <= upper:
            break
        lower = upper
    return tax


def _ahk(income, p):
    a = p["ahk"]
    return max(0.0, a["max"] - max(0.0, income - a["phase_start"]) * a["phase_rate"])


def _arbeidskorting(work_income, p):
    for end, base, rate, start in p["arbeidskorting"]:
        if work_income <= end:
            return max(0.0, base + rate * (work_income - start))
    return 0.0


def net_from_gross(gross: float, year: int, pension_pct: float = 0.0, ruling_allowance=None) -> dict:
    """Yearly net pay for a yearly gross salary (including holiday allowance)."""
    p = params(year)
    pension = gross * pension_pct
    allowance = ruling_allowance(gross) if ruling_allowance else 0.0
    taxable = max(0.0, gross - pension - allowance)
    tax = _brackets(taxable, p)
    credits = _ahk(taxable, p) + _arbeidskorting(taxable, p)
    wage_tax = max(0.0, tax - credits)
    return {"gross": gross, "pension": pension, "allowance": allowance, "taxable": taxable,
            "tax_before_credits": tax, "credits": min(credits, tax), "wage_tax": wage_tax,
            "net": gross - pension - wage_tax}


def gross_from_net(net: float, year: int, pension_pct: float = 0.0, ruling_allowance=None) -> dict:
    lo, hi = 0.0, max(10_000.0, net * 4)
    for _ in range(80):
        mid = (lo + hi) / 2
        if net_from_gross(mid, year, pension_pct, ruling_allowance)["net"] < net:
            lo = mid
        else:
            hi = mid
    return net_from_gross(round((lo + hi) / 2, 2), year, pension_pct, ruling_allowance)


def ruling_allowance_fn(year: int, under30_master=False, first_applied_year=2025):
    """Allowance as a function of gross: 30% (27% from 2027 for newer rulings), capped so the taxable
    salary stays above the norm and at the yearly maximum."""
    r = rules.get(year)["expat_ruling"]
    norm = r["salary_norm_u30_master"] if under30_master else r["salary_norm"]
    rate = r["rate"] if year < 2027 or first_applied_year <= 2023 else r["rate_2027"]
    return lambda g: max(0.0, min(g * rate, r["max_allowance"], g - norm))


def detect(tx: pd.DataFrame, months: int = 12) -> pd.DataFrame:
    """Salary payers in the statements: net received over the last `months` months, per payer."""
    s = tx[(tx["category"] == "Income: Salary") & (tx["eur"] > 0)].copy()
    if s.empty:
        return pd.DataFrame(columns=["payer", "first", "last", "months", "net_period", "monthly_median", "net_year", "annualized"])
    end = s["date"].max()
    start = end - pd.DateOffset(months=months) + pd.Timedelta(days=1)
    s = s[s["date"] >= start]
    s["payer"] = s["counterparty"].where(s["counterparty"].str.strip() != "", s["payee"])
    out = []
    for payer, g in s.groupby("payer"):
        per_month = g.groupby(g["date"].dt.to_period("M"))["eur"].sum()
        n = len(per_month)
        med = float(per_month.median())
        total = float(per_month.sum())
        # under a full year: scale the typical month to 12 and add holiday pay (8% of a year of monthly pay)
        hol = params(int(end.year))["holiday_allowance"]
        annual = total if n >= 12 else med * 12 * (1 + hol)
        out.append({"payer": payer, "first": g["date"].min().date(), "last": g["date"].max().date(), "months": n,
                    "net_period": total, "monthly_median": med, "net_year": annual, "annualized": n < 12})
    return pd.DataFrame(out).sort_values("net_year", ascending=False).reset_index(drop=True)
