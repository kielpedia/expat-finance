"""
Tax treatment of each account, and how a value is split between treatments.

A bank export never says whether money is pre-tax, Roth or taxable, so this is set per
account. Mixed workplace plans (one 401(k) with both pre-tax and Roth money) carry a
Roth share, taken from the provider's 'balances by source' page and applied to each new value.
"""
import re

import pandas as pd

# key: (label, box3 bucket)
#   box3 bucket: "bank" | "other" | "pension" (left out by default) | "roth" (setting decides) | "none"
TREATMENTS = {
    "cash":        ("Cash / savings", "bank"),
    "taxable":     ("Taxable investments", "other"),
    "traditional": ("Pre-tax: 401(k), 403(b), Traditional IRA", "pension"),
    "roth":        ("Roth: Roth 401(k) or Roth IRA", "roth"),
    "mixed":       ("Mixed: pre-tax + Roth in one plan", None),
    "hsa":         ("HSA", "other"),
    "529":         ("529 education plan", "other"),
    "nl_pension":  ("Dutch pension product (lijfrente, banksparen)", "none"),
    "options":     ("Employee stock options (taxed in box 1 when exercised)", "none"),
    "debt":        ("Debt", "debt"),
}
ORDER = list(TREATMENTS)


def label(key) -> str:
    return TREATMENTS.get(key or "", ("Not set", None))[0]


def guess(kind: str, name: str = "") -> str:
    """Best guess from account type and name, used until you set it yourself."""
    n = (name or "").lower()
    if kind in ("credit", "loan", "mortgage"):
        return "debt"
    if kind in ("checking", "savings"):
        return "cash"
    if kind == "options":
        return "options"
    if re.search(r"\broth\b", n):
        return "roth"
    if re.search(r"\bhsa\b|health savings", n):
        return "hsa"
    if re.search(r"\b529\b", n):
        return "529"
    if re.search(r"lijfrente|banksparen|pensioen", n):
        return "nl_pension"
    if kind == "retirement" or re.search(r"\bira\b|401|403|457|rollover|\bsep\b", n):
        return "traditional"
    return "taxable"


def effective(row) -> tuple[str, bool]:
    """(treatment key, is_guess) for an accounts row (dict or Series)."""
    t = row.get("tax_treatment") if hasattr(row, "get") else None
    if isinstance(t, str) and t in TREATMENTS:
        return t, False
    return guess(row.get("kind", ""), row.get("name", "")), True


def portions(row, amount: float) -> list[tuple[str, float]]:
    """Split an account value into (treatment, amount) parts."""
    t, _ = effective(row)
    if t != "mixed":
        return [(t, amount)]
    share = row.get("roth_share")
    share = float(share) if share is not None and pd.notna(share) else 0.0
    share = min(max(share, 0.0), 1.0)
    return [("roth", amount * share), ("traditional", amount * (1 - share))]


def box3_bucket(treatment: str, include_roth: bool, include_traditional: bool) -> str:
    b = TREATMENTS.get(treatment, ("", "other"))[1]
    if b == "roth":
        return "other" if include_roth else "excluded"
    if b == "pension":
        return "other" if include_traditional else "excluded"
    if b in ("none", None):
        return "excluded"
    return b
