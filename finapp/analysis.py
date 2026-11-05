"""Budget, cash-flow, net-worth and financial-health calculations."""
import pandas as pd

from .categorize import group_of, is_budget

LIABILITY_KINDS = ("credit", "loan", "mortgage")
LIQUID_KINDS = ("checking", "savings")


def accounts(con) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM accounts ORDER BY name", con)


def transactions(con, conv, start=None, end=None) -> pd.DataFrame:
    q = ("SELECT t.*, a.name AS account, a.currency, a.country, a.kind, a.owner "
         "FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE 1=1")
    params = []
    if start:
        q += " AND t.date >= ?"
        params.append(str(start))
    if end:
        q += " AND t.date <= ?"
        params.append(str(end))
    df = pd.read_sql(q + " ORDER BY t.date", con, params=params, parse_dates=["date"])
    if df.empty:
        for c in ("eur", "usd", "group", "budget", "income", "month", "spread_months", "spread_dir", "spread", "payee"):
            df[c] = pd.Series(dtype=float if c in ("eur", "usd") else object)
        return df
    df["category"] = df["category"].fillna("Uncategorized")
    df["eur"] = conv.to_eur_series(df)
    df["usd"] = df["eur"] * df["date"].map(lambda d: conv.per_eur("USD", d))
    df["group"] = df["category"].map(group_of)
    df["note"] = (df["note"] if "note" in df else pd.Series("", index=df.index)).fillna("")
    sm = df["spread_months"] if "spread_months" in df else pd.Series(1, index=df.index)
    df["spread_months"] = pd.to_numeric(sm, errors="coerce").fillna(1).clip(lower=1).astype(int)
    sd = df["spread_dir"] if "spread_dir" in df else pd.Series("next", index=df.index)
    df["spread_dir"] = sd.fillna("next").where(sd.isin(["next", "past"]), "next")
    df["spread"] = spread_labels(df)
    df["budget"] = df["category"].map(is_budget)
    df["income"] = df["group"] == "Income"
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    df["payee"] = df.apply(lambda r: payee_key(r["counterparty"], r["description"]), axis=1)
    return df


def payee_key(counterparty, description) -> str:
    """A stable, readable payee name used to group similar payments for review."""
    import re
    s = (counterparty or "").strip() or (description or "").strip()
    s = re.sub(r"\b(BEA|GEA|ePos|eCom|SEPA|Betaalpas|Apple Pay|Google Pay|Betaalautomaat|POS|DEBIT|PURCHASE|CARD)\b", " ", s, flags=re.I)
    s = re.sub(r"[#*]?\d{3,}|,PAS\w*|\b\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?\b|\d{1,2}:\d{2}", " ", s)
    s = re.sub(r"[^\w&'.\- ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return s[:40] or "(no description)"


# ---------------------------------------------------------------- spreading a payment over months
NO_SPREAD = "—"
SPREAD_NEXT = (2, 3, 4, 6, 9, 10, 12, 18, 24, 36, 48, 60)
SPREAD_PAST = (2, 3, 6, 12)


def spread_label(n, direction="next") -> str:
    n = int(n or 1)
    if n <= 1:
        return NO_SPREAD
    return f"{'Next' if direction != 'past' else 'Past'} {n} months"


def spread_options(extra=()) -> list:
    """Choices for the 'Spread' column; `extra` adds labels already in use (e.g. a custom number of months)."""
    opts = [NO_SPREAD] + [spread_label(n, "next") for n in SPREAD_NEXT] + [spread_label(n, "past") for n in SPREAD_PAST]
    return opts + [e for e in extra if e not in opts]


def parse_spread(label) -> tuple:
    """'Next 12 months' -> (12, 'next'); anything else -> (1, 'next')."""
    import re
    m = re.match(r"(Next|Past)\s+(\d+)", str(label or ""))
    if not m or int(m.group(2)) <= 1:
        return 1, "next"
    return int(m.group(2)), m.group(1).lower()


def spread_labels(df) -> pd.Series:
    return pd.Series([spread_label(n, d) for n, d in zip(df["spread_months"], df["spread_dir"])], index=df.index, dtype=object)


def spread_view(tx: pd.DataFrame) -> pd.DataFrame:
    """Budget view: a payment spread over N months becomes N equal parts, one per month.

    'Next N months' starts in the month it was paid; 'Past N months' ends in it. Each part keeps the
    original id, so edits still go to the real transaction. Columns added: part (e.g. "3/12"),
    paid_date and paid_eur (the real payment)."""
    if tx.empty:
        return tx.assign(part="", paid_date=tx["date"], paid_eur=tx["eur"]) if "date" in tx else tx
    base = tx.assign(part="", paid_date=tx["date"], paid_eur=tx["eur"])
    multi = base["spread_months"] > 1
    if not multi.any():
        return base
    pieces = []
    for _, r in base[multi].iterrows():
        n = int(r["spread_months"])
        first = -(n - 1) if r["spread_dir"] == "past" else 0
        for i in range(n):
            p = r.copy()
            p["month"] = r["month"] + pd.DateOffset(months=first + i)
            p["date"] = r["date"] if first + i == 0 else p["month"]
            for c in ("eur", "usd", "amount"):
                p[c] = r[c] / n
            p["part"] = f"{i + 1}/{n}"
            pieces.append(p)
    parts = pd.DataFrame(pieces).astype({c: base[c].dtype for c in base.columns if c in ("id", "eur", "usd", "amount",
                                                                                         "budget", "spread_months")})
    out = pd.concat([base[~multi], parts], ignore_index=True)
    out["month"] = pd.to_datetime(out["month"])
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values("date").reset_index(drop=True)


def spread_costs(tx: pd.DataFrame) -> pd.DataFrame:
    """One row per spread payment: what was paid, the monthly share and the months it covers."""
    s = tx[(tx["spread_months"] > 1)].copy()
    if s.empty:
        return s
    s["per_month"] = s["eur"] / s["spread_months"]
    start = [m + pd.DateOffset(months=-(n - 1) if d == "past" else 0)
             for m, n, d in zip(s["month"], s["spread_months"], s["spread_dir"])]
    s["covers_from"] = pd.to_datetime(start)
    s["covers_to"] = [a + pd.DateOffset(months=int(n) - 1) for a, n in zip(s["covers_from"], s["spread_months"])]
    return s.sort_values("date", ascending=False)


def income_mask(tx: pd.DataFrame) -> pd.Series:
    """True for income rows (category group 'Income'). Money coming back in any other category (a refund,
    a reimbursement) is not income: it reduces spending in that category."""
    if "income" in tx:
        return tx["income"].astype(bool)
    return tx["category"].fillna("").map(group_of) == "Income"


def spending_rows(tx: pd.DataFrame) -> pd.DataFrame:
    """Budget rows that count as spending: everything that isn't income or a transfer, refunds included
    (as positive amounts that offset the spending)."""
    return tx[tx["budget"] & ~income_mask(tx)]


def monthly_summary(tx: pd.DataFrame) -> pd.DataFrame:
    b = tx[tx["budget"]]
    if b.empty:
        return pd.DataFrame(columns=["month", "income", "expenses", "net", "savings_rate"])
    inc = income_mask(b)
    months = sorted(b["month"].unique())
    out = pd.DataFrame({
        "income": b[inc].groupby("month")["eur"].sum().reindex(months, fill_value=0.0),
        "expenses": -b[~inc].groupby("month")["eur"].sum().reindex(months, fill_value=0.0),
    })
    out.index.name = "month"
    out["net"] = out["income"] - out["expenses"]
    out["savings_rate"] = (out["net"] / out["income"]).where(out["income"] > 0)
    return out.reset_index()


def by_category(tx: pd.DataFrame, expenses_only=True) -> pd.DataFrame:
    """Spending per category, net of refunds in that category (expenses_only), or every budget category."""
    b = spending_rows(tx) if expenses_only else tx[tx["budget"]]
    if b.empty:
        return pd.DataFrame(columns=["group", "category", "eur", "n"])
    out = b.groupby(["group", "category"]).agg(eur=("eur", "sum"), n=("eur", "size")).reset_index()
    if expenses_only:
        out = out[out["eur"] < 0]
        out["eur"] = out["eur"].abs()
    return out.sort_values("eur", ascending=False)


def recurring(tx: pd.DataFrame, min_months=3) -> pd.DataFrame:
    """Payees that appear in at least `min_months` distinct months with a similar amount."""
    out = tx[(tx["budget"]) & (tx["eur"] < 0)].copy()
    if out.empty:
        return pd.DataFrame()
    out["payee"] = out["counterparty"].where(out["counterparty"].str.len() > 0, out["description"].str[:40])
    out["payee"] = out["payee"].str.lower().str.replace(r"\d{4,}", "", regex=True).str.strip()
    g = out.groupby("payee").agg(months=("month", "nunique"), avg=("eur", "mean"), std=("eur", "std"),
                                 last=("date", "max"), category=("category", "first"))
    g = g[(g["months"] >= min_months) & ((g["std"].fillna(0) / g["avg"].abs()) < 0.25)]
    g["avg"] = g["avg"].abs()
    g["annual"] = g["avg"] * 12
    return g.sort_values("avg", ascending=False).reset_index()


def balance_history(con, conv) -> pd.DataFrame:
    """Daily-forward-filled balance per account, in EUR. Liabilities are stored as negative balances."""
    b = pd.read_sql(
        "SELECT b.account_id, b.date, b.balance, a.name, a.currency, a.kind, a.country, a.tax_treatment, a.roth_share "
        "FROM balances b JOIN accounts a ON a.id=b.account_id ORDER BY b.date", con, parse_dates=["date"])
    if b.empty:
        return b
    b["eur"] = [bal / conv.per_eur(c, d) for bal, c, d in zip(b["balance"], b["currency"], b["date"])]
    return b


def balance_on(con, conv, on, kinds=None) -> pd.DataFrame:
    """Last known balance of each account on or before `on`."""
    b = balance_history(con, conv)
    if b.empty:
        return b
    b = b[b["date"] <= pd.Timestamp(on)]
    if kinds:
        b = b[b["kind"].isin(kinds)]
    last = b.sort_values("date").groupby("account_id").tail(1).copy()
    # re-value at `on` so FX moves after the last statement are reflected
    last["eur"] = [bal / conv.per_eur(c, on) for bal, c in zip(last["balance"], last["currency"])]
    return last


def net_worth_series(con, conv, freq="ME") -> pd.DataFrame:
    b = balance_history(con, conv)
    if b.empty:
        return pd.DataFrame(columns=["date", "assets", "liabilities", "net_worth"])
    today = pd.Timestamp.today().normalize()
    end = max(b["date"].max(), today)
    # month-end points, plus the latest date so this month's values (e.g. a Positions import today) count
    dates = pd.date_range(b["date"].min(), end, freq=freq).union(pd.DatetimeIndex([end]))
    rows = []
    for d in dates:
        last = balance_on(con, conv, d)
        assets = last.loc[last["eur"] > 0, "eur"].sum()
        liab = abs(last.loc[last["eur"] < 0, "eur"].sum())
        rows.append({"date": d, "assets": assets, "liabilities": liab, "net_worth": assets - liab})
    return pd.DataFrame(rows)


def health(con, conv, tx: pd.DataFrame, months=6) -> dict:
    """Key ratios over the last `months` complete months of data. Yearly bills set to spread count as their
    monthly share, and the current (unfinished) month is left out so it doesn't drag the averages down."""
    this_month = pd.Timestamp.today().normalize().replace(day=1)
    ms = monthly_summary(spread_view(tx)) if not tx.empty else monthly_summary(tx)
    if len(ms) and not tx.empty:
        first = tx["month"].min()
        ms = ms[(ms["month"] >= first) & (ms["month"] < this_month) & (ms["month"] <= tx["month"].max())]
    recent = ms.tail(months)
    avg_exp = recent["expenses"].mean() if len(recent) else 0
    avg_inc = recent["income"].mean() if len(recent) else 0
    today = pd.Timestamp.today()
    bal = balance_on(con, conv, today)
    liquid = bal.loc[bal["kind"].isin(LIQUID_KINDS), "eur"].clip(lower=0).sum() if not bal.empty else 0
    debts = abs(bal.loc[bal["kind"].isin(("credit", "loan")), "eur"].clip(upper=0).sum()) if not bal.empty else 0
    debt_payments = 0
    if not tx.empty:
        rec = tx[(tx["date"] >= today - pd.DateOffset(months=months)) & (tx["category"].str.contains("Mortgage|Loan", na=False))]
        debt_payments = -rec["eur"].sum() / months if len(rec) else 0
    return {
        "avg_income": avg_inc,
        "avg_expenses": avg_exp,
        "savings_rate": (avg_inc - avg_exp) / avg_inc if avg_inc else None,
        "liquid": liquid,
        "emergency_months": liquid / avg_exp if avg_exp else None,
        "consumer_debt": debts,
        "debt_to_income": debt_payments / avg_inc if avg_inc else None,
        "months_of_data": len(ms),
        "months_used": len(recent),
    }


def coverage(con, conv, stale_days=45) -> pd.DataFrame:
    """Per account: whether and how recently it has a value, so net worth gaps are visible."""
    a = pd.read_sql("SELECT a.id, a.name, a.kind, a.currency, "
                    "(SELECT COUNT(*) FROM transactions t WHERE t.account_id=a.id) AS txns, "
                    "(SELECT MAX(date) FROM balances b WHERE b.account_id=a.id) AS last_value "
                    "FROM accounts a ORDER BY a.name", con, parse_dates=["last_value"])
    today = pd.Timestamp.today().normalize()
    a["status"] = "ok"
    a.loc[a["last_value"].isna(), "status"] = "missing"
    a.loc[a["last_value"].notna() & ((today - a["last_value"]).dt.days > stale_days), "status"] = "stale"
    return a
