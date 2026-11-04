"""
Employee stock options, for any number of companies.

Each grant is tracked on its own: company and ticker (read from the plan's export), grant date, strike,
quantity, expiry, how many are vested, exercised or cancelled. Value is intrinsic value
(price - strike) x vested outstanding options, at a share price per ticker that you enter or fetch
(only the ticker is sent, to Yahoo Finance or Stooq). Prices and strikes are in USD.

Tax facts used (as of 2026; confirm with an adviser):
- US: exercising a non-qualified option creates ordinary wage income equal to the spread.
- NL: employee options are taxed in box 1 when exercised (or later, when the shares become
  tradable), on the spread. Where work was done in several countries during the vesting period,
  the income is split pro rata. Unexercised options are not box 3 assets.
"""
import csv
import hashlib
import io
import re
from datetime import date

import pandas as pd

from .importers import parse_number

SCHEMA = """
CREATE TABLE IF NOT EXISTS option_grants (
    id INTEGER PRIMARY KEY,
    grant_ref TEXT UNIQUE,              -- the plan's grant ID, or a hash of date+strike+qty
    company TEXT,
    symbol TEXT NOT NULL,
    plan_type TEXT DEFAULT 'NSO',       -- NSO (non-qualified) or ISO
    grant_date TEXT NOT NULL,
    expiration TEXT,
    strike REAL NOT NULL,               -- USD per share, split-adjusted
    quantity REAL NOT NULL,             -- options granted, split-adjusted
    vested REAL,                        -- NULL = all vested
    exercised REAL DEFAULT 0,
    cancelled REAL DEFAULT 0,
    work_country TEXT DEFAULT 'NL',     -- where you worked when the grant was earned (for tax sourcing)
    split_factor REAL DEFAULT 1,        -- splits already applied to this row
    notes TEXT
);
CREATE TABLE IF NOT EXISTS option_exercises (
    id INTEGER PRIMARY KEY,
    grant_id INTEGER REFERENCES option_grants(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    quantity REAL NOT NULL,
    fmv REAL NOT NULL,                  -- share price at exercise
    method TEXT DEFAULT 'same-day sale', -- same-day sale, sell to cover, exercise and hold
    notes TEXT
);
CREATE TABLE IF NOT EXISTS stock_splits (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,                 -- first trading day on the split-adjusted basis
    ratio REAL NOT NULL,                -- new shares per old share, e.g. 10 for a 10-for-1 split
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS option_prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""

# Recent forward splits of large US companies, loaded into the stock_splits table on first run.
# Add your own company's splits on the Stock options page; nothing else depends on this list.
KNOWN_SPLITS = [
    ("AAPL", "2020-08-31", 4.0), ("TSLA", "2020-08-31", 5.0), ("TSLA", "2022-08-25", 3.0),
    ("NVDA", "2021-07-20", 4.0), ("NVDA", "2024-06-10", 10.0), ("AMZN", "2022-06-06", 20.0),
    ("GOOGL", "2022-07-18", 20.0), ("GOOG", "2022-07-18", 20.0), ("SHOP", "2022-06-29", 10.0),
    ("WMT", "2024-02-26", 3.0), ("CMG", "2024-06-26", 50.0), ("AVGO", "2024-07-15", 10.0),
    ("NFLX", "2025-11-17", 10.0),
]

NETWORTH_ACCOUNT = "Stock options (in the money)"


def setup(con):
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM stock_splits LIMIT 1").fetchone():
        con.executemany("INSERT OR IGNORE INTO stock_splits(symbol, date, ratio) VALUES(?,?,?)", KNOWN_SPLITS)
    con.commit()


def splits(con) -> pd.DataFrame:
    return pd.read_sql("SELECT symbol, date, ratio FROM stock_splits ORDER BY symbol, date", con, parse_dates=["date"])


def add_split(con, symbol: str, on, ratio: float):
    con.execute("INSERT OR REPLACE INTO stock_splits(symbol, date, ratio) VALUES(?,?,?)",
                (clean_symbol(symbol), pd.Timestamp(on).strftime("%Y-%m-%d"), float(ratio)))
    con.commit()


def delete_split(con, symbol: str, on):
    con.execute("DELETE FROM stock_splits WHERE symbol=? AND date=?", (symbol, pd.Timestamp(on).strftime("%Y-%m-%d")))
    con.commit()


def expected_factor(sp: pd.DataFrame, symbol: str, grant_date) -> float:
    """Product of all known split ratios for `symbol` that happened after `grant_date`."""
    if sp.empty:
        return 1.0
    m = (sp["symbol"] == symbol) & (sp["date"] > pd.Timestamp(grant_date))
    f = 1.0
    for r in sp.loc[m, "ratio"]:
        f *= float(r)
    return f


SYMBOL_OK = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")


def clean_symbol(sym) -> str:
    s = str(sym or "").strip().upper()
    return s if SYMBOL_OK.match(s) else ""


# ------------------------------------------------------------------ data access

def grants(con) -> pd.DataFrame:
    g = pd.read_sql("SELECT * FROM option_grants ORDER BY grant_date, id", con, parse_dates=["grant_date", "expiration"])
    if g.empty:
        return g
    g["vested_n"] = g["vested"].where(g["vested"].notna(), g["quantity"])
    g["outstanding"] = (g["quantity"] - g["exercised"].fillna(0) - g["cancelled"].fillna(0)).clip(lower=0)
    g["vested_out"] = (g["vested_n"] - g["exercised"].fillna(0) - g["cancelled"].fillna(0)).clip(lower=0)
    g["vested_out"] = g[["vested_out", "outstanding"]].min(axis=1)
    g["unvested_out"] = (g["outstanding"] - g["vested_out"]).clip(lower=0)
    return g


def exercises(con) -> pd.DataFrame:
    return pd.read_sql("SELECT e.*, g.strike, g.symbol, g.grant_date, g.grant_ref, g.work_country, g.plan_type "
                       "FROM option_exercises e JOIN option_grants g ON g.id=e.grant_id ORDER BY e.date",
                       con, parse_dates=["date", "grant_date"])


def symbols(con) -> list[str]:
    return [r["symbol"] for r in con.execute("SELECT DISTINCT symbol FROM option_grants WHERE symbol IS NOT NULL ORDER BY symbol")]


def latest_price(con, symbol):
    r = con.execute("SELECT date, price FROM option_prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
    return (float(r["price"]), r["date"]) if r else (None, None)


def latest_prices(con) -> dict:
    """{symbol: (price, date)} for every symbol that has grants."""
    return {s: latest_price(con, s) for s in symbols(con)}


def set_price(con, symbol, price, on=None):
    con.execute("INSERT OR REPLACE INTO option_prices(symbol, date, price) VALUES(?,?,?)",
                (symbol, (on or date.today()).isoformat(), float(price)))
    con.commit()


# ------------------------------------------------------------------ valuation

def value(g: pd.DataFrame, prices, on=None) -> pd.DataFrame:
    """Adds price, spread, intrinsic value, status and days to expiry for each grant.
    `prices` is {symbol: price} (or one number used for every grant). Grants without a price get status 'no price'."""
    if g.empty:
        return g
    on = pd.Timestamp(on or date.today())
    v = g.copy()
    if isinstance(prices, dict):
        v["price"] = v["symbol"].map(lambda s: prices.get(s)).astype(float)
    else:
        v["price"] = float(prices) if prices is not None else float("nan")
    v["spread"] = (v["price"] - v["strike"]).clip(lower=0).fillna(0.0)
    v["expired"] = v["expiration"].notna() & (v["expiration"] < on)
    live = ~v["expired"]
    v["intrinsic"] = (v["spread"] * v["vested_out"]).where(live, 0.0)
    v["intrinsic_unvested"] = (v["spread"] * v["unvested_out"]).where(live, 0.0)
    v["days_left"] = (v["expiration"] - on).dt.days
    v["status"] = "in the money"
    v.loc[v["strike"] >= v["price"], "status"] = "underwater"
    v.loc[v["price"].isna() | (v["price"] <= 0), "status"] = "no price"
    v.loc[v["outstanding"] <= 0, "status"] = "fully exercised"
    v.loc[v["expired"] & (v["outstanding"] > 0), "status"] = "expired"
    return v


LIVE = ("in the money", "underwater", "no price")


def summary(v: pd.DataFrame, tax_rate: float) -> dict:
    if v.empty:
        return {}
    live = v[v["status"].isin(LIVE)]
    itm = live[live["status"] == "in the money"]
    soon = itm[itm["days_left"] <= 365]
    return {
        "grants": int(len(live)),
        "options": float(live["outstanding"].sum()),
        "vested": float(live["vested_out"].sum()),
        "intrinsic": float(live["intrinsic"].sum()),
        "after_tax": float(live["intrinsic"].sum()) * (1 - tax_rate),
        "unvested_value": float(live["intrinsic_unvested"].sum()),
        "underwater": int((live["status"] == "underwater").sum()),
        "unpriced": int((live["status"] == "no price").sum()),
        "avg_strike": float((live["strike"] * live["outstanding"]).sum() / live["outstanding"].sum()) if live["outstanding"].sum() else None,
        "breakeven": float(live["strike"].min()) if len(live) else None,
        "expiring_12m": soon,
    }


def by_company(v: pd.DataFrame) -> pd.DataFrame:
    """One row per ticker: price, outstanding options, value."""
    live = v[v["status"].isin(LIVE)]
    if live.empty:
        return pd.DataFrame(columns=["symbol", "company", "price", "grants", "options", "intrinsic", "underwater"])
    return (live.assign(company=live["company"].fillna(live["symbol"]))
            .groupby("symbol")
            .agg(company=("company", "first"), price=("price", "first"), grants=("id", "size"),
                 options=("outstanding", "sum"), intrinsic=("intrinsic", "sum"),
                 underwater=("status", lambda s: int((s == "underwater").sum())))
            .reset_index().sort_values("intrinsic", ascending=False))


def what_if(v: pd.DataFrame, moves=(-0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3)) -> pd.DataFrame:
    """Value if every share price moved by the same percentage."""
    live = v[v["status"].isin(("in the money", "underwater"))]
    rows = []
    for m in moves:
        p = live["price"] * (1 + m)
        rows.append({"move": m, "value": float(((p - live["strike"]).clip(lower=0) * live["vested_out"]).sum())})
    return pd.DataFrame(rows)


def exercise_income(ex: pd.DataFrame, year: int) -> pd.DataFrame:
    """Spread income per exercise in a calendar year (wage income in both the US and NL)."""
    e = ex[ex["date"].dt.year == year].copy()
    if e.empty:
        return e
    e["spread_income"] = (e["fmv"] - e["strike"]).clip(lower=0) * e["quantity"]
    return e


# ------------------------------------------------------------------ splits

def split_candidates(con) -> pd.DataFrame:
    """Grants that predate a known split of their ticker but haven't been adjusted for it.
    `ratio` is the factor still to apply (strike ÷ ratio, options × ratio)."""
    g = grants(con)
    if g.empty:
        return g
    sp = splits(con)
    out = []
    for _, r in g.iterrows():
        exp = expected_factor(sp, r["symbol"], r["grant_date"])
        have = float(r["split_factor"]) if pd.notna(r["split_factor"]) else 1.0
        if exp > have * 1.0001:
            out.append({**r.to_dict(), "ratio": exp / have})
    return pd.DataFrame(out)


def apply_split(con, grant_ids, ratio):
    for gid in grant_ids:
        con.execute("UPDATE option_grants SET quantity=quantity*?, strike=strike/?, vested=CASE WHEN vested IS NULL "
                    "THEN NULL ELSE vested*? END, exercised=exercised*?, cancelled=cancelled*?, split_factor=split_factor*? "
                    "WHERE id=?", (ratio, ratio, ratio, ratio, ratio, ratio, int(gid)))
    con.commit()


# ------------------------------------------------------------------ import

COLS = {
    "grant_ref": ["grant id", "grant number", "grant #", "award id", "award number", "grant reference", "grant name"],
    "grant_date": ["grant date", "award date", "date granted", "grant dt"],
    "strike": ["grant price", "exercise price", "strike price", "strike", "option price", "award price"],
    "quantity": ["granted qty", "granted", "quantity granted", "shares granted", "options granted", "total granted", "original quantity",
                 "number of options", "quantity", "shares"],
    "outstanding": ["outstanding", "available", "remaining", "options outstanding", "unexercised", "exercisable qty",
                    "exercisable quantity", "exercisable", "sellable qty"],
    "cancelled": ["expired qty", "cancelled", "canceled", "forfeited", "expired"],
    "vested": ["vested", "vested quantity", "vested qty", "vested shares"],
    "exercised": ["exercised", "exercised quantity", "quantity exercised"],
    "expiration": ["expiration date", "expiration", "expiry date", "expires", "expire date"],
    "plan_type": ["grant type", "award type", "plan type", "option type", "type"],
    "symbol": ["symbol", "ticker", "ticker symbol", "stock symbol", "trading symbol"],
    "company": ["company name", "company", "issuer", "employer", "plan name"],
}


def guess_columns(header: list[str]) -> dict:
    low = [h.strip().lower() for h in header]
    out = {}
    for field, names in COLS.items():
        for n in names:
            hit = next((header[i] for i, h in enumerate(low) if h == n), None) or \
                  next((header[i] for i, h in enumerate(low) if h.startswith(n)), None)
            if hit and hit not in out.values():
                out[field] = hit
                break
    return out


def read_upload(raw: bytes, filename: str = "") -> pd.DataFrame:
    """CSV or Excel. For Excel (E*TRADE's ByBenefitType export) every sheet is searched for the grants table."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")) or raw[:2] == b"PK":
        return read_excel(raw)
    from .importers import _decode
    return read_table(_decode(raw))


def dedupe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Exports like E*TRADE's repeat headings (one 'Date' per section). For each repeated name keep the
    column with the most filled-in values for the rows we kept, so every name points to one column."""
    if not df.columns.duplicated().any():
        return df
    keep = []
    for name in dict.fromkeys(df.columns):
        idx = [i for i, c in enumerate(df.columns) if c == name]
        if len(idx) == 1:
            keep.append(idx[0])
            continue
        filled = [df.iloc[:, i].astype(str).str.strip().replace({"nan": "", "None": ""}).ne("").sum() for i in idx]
        keep.append(idx[max(range(len(idx)), key=lambda j: (filled[j], -j))])
    return df.iloc[:, sorted(keep)]


def _is_header(cells) -> bool:
    low = [str(c).strip().lower() for c in cells if c is not None and str(c).strip() and str(c) != "nan"]
    j = " | ".join(low)
    return bool(re.search(r"grant date|award date|date granted", j)) and bool(re.search(r"price|strike", j))


def read_excel(raw: bytes) -> pd.DataFrame:
    sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None, dtype=object)
    # prefer a sheet called Options / Stock Options, then any sheet with a grants header
    order = sorted(sheets, key=lambda n: (0 if re.search(r"option", n, re.I) else 1, n))
    for sname in order:
        raw_df = sheets[sname]
        for i in range(min(40, len(raw_df))):
            if _is_header(raw_df.iloc[i].tolist()):
                header = [str(c).strip() if c is not None and str(c) != "nan" else f"col{j}"
                          for j, c in enumerate(raw_df.iloc[i].tolist())]
                body = raw_df.iloc[i + 1:].copy()
                body.columns = header
                body = body.dropna(how="all")
                # E*TRADE mixes record types (Grant, Event, Vest Schedule, totals); keep the grant rows
                rt_i = next((j for j, c in enumerate(body.columns) if c.lower() == "record type"), None)
                if rt_i is not None:
                    body = body[body.iloc[:, rt_i].astype(str).str.strip().str.lower() == "grant"]
                # only stock options, not RSUs/ESPP, when a plan type column says so
                pt_i = next((j for j, c in enumerate(body.columns) if c.lower() in ("plan type", "benefit type", "award type")), None)
                if pt_i is not None:
                    keep = ~body.iloc[:, pt_i].astype(str).str.upper().str.contains(r"\bRS|RSU|RESTRICTED|ESPP|PERFORMANCE")
                    body = body[keep]
                body = body.loc[:, [not c.startswith("col") for c in body.columns]]
                body = dedupe_columns(body)
                for c in body.columns:   # everything as text, like the CSV path
                    body[c] = body[c].map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v))
                                          else (v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)))
                return body.reset_index(drop=True)
    raise ValueError("Couldn't find a sheet with a grant date and a price column. "
                     "In E*TRADE use Stock Plan → Holdings → Download → Download Expanded.")


def read_table(raw_text: str) -> pd.DataFrame:
    """Find the header row (the first row with a date-ish and a price-ish column) and read from there."""
    lines = raw_text.splitlines()
    for i, line in enumerate(lines[:40]):
        l = line.lower()
        if re.search(r"grant|award", l) and re.search(r"price|strike", l):
            text = "\n".join(lines[i:])
            delim = max([",", ";", "\t"], key=lambda d: line.count(d))
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
            header = [h.strip() for h in rows[0]]
            width = len(header)
            body = [r + [""] * (width - len(r)) if len(r) < width else r[:width] for r in rows[1:] if any(x.strip() for x in r)]
            df = pd.DataFrame(body, columns=header)
            df = df.loc[:, [bool(c) for c in df.columns]]
            return dedupe_columns(df)
    raise ValueError("Couldn't find a header row with a grant date and a price column.")


def parse_grants(df: pd.DataFrame, mapping: dict, symbol: str = "", years_to_expiry=10) -> pd.DataFrame:
    """Grants from an export. Ticker and company come from the file's own columns where it has them;
    `symbol` fills rows that have none (asked for on the import screen, never assumed)."""
    get = lambda f: df[mapping[f]] if mapping.get(f) in df else pd.Series([""] * len(df), index=df.index)
    fallback = clean_symbol(symbol)
    sym = get("symbol").astype(str).map(clean_symbol)
    out = pd.DataFrame({
        "grant_date": pd.to_datetime(get("grant_date"), errors="coerce"),
        "strike": get("strike").map(lambda v: parse_number(v, ".")),
        "quantity": get("quantity").map(lambda v: parse_number(v, ".")),
        "outstanding": get("outstanding").map(lambda v: parse_number(v, ".")),
        "vested": get("vested").map(lambda v: parse_number(v, ".")),
        "exercised": get("exercised").map(lambda v: parse_number(v, ".")),
        "cancelled": get("cancelled").map(lambda v: parse_number(v, ".")),
        "expiration": pd.to_datetime(get("expiration"), errors="coerce"),
        "plan_type": get("plan_type").astype(str).str.upper().map(lambda s: "ISO" if "ISO" in s or "INCENTIVE" in s else "NSO"),
        "symbol": sym.where(sym != "", fallback),
        "company": get("company").astype(str).str.strip(),
        "grant_ref": get("grant_ref").astype(str).str.strip(),
    })
    out["company"] = out["company"].where(out["company"] != "", out["symbol"])
    out = out.dropna(subset=["grant_date", "strike"])
    # quantity: granted if present, else outstanding (+ exercised + cancelled)
    q = out["quantity"]
    q = q.where(q.notna(), out["outstanding"] + out["exercised"].fillna(0) + out["cancelled"].fillna(0))
    out["quantity"] = q
    out = out.dropna(subset=["quantity"])
    # exercised: explicit, or granted - outstanding
    ex = out["exercised"]
    ex = ex.where(ex.notna(), (out["quantity"] - out["outstanding"] - out["cancelled"].fillna(0)).where(out["outstanding"].notna(), 0))
    out["exercised"] = ex.clip(lower=0).fillna(0)
    out["cancelled"] = out["cancelled"].fillna(0)
    out["expiration"] = out["expiration"].where(out["expiration"].notna(),
                                                out["grant_date"] + pd.DateOffset(years=years_to_expiry))
    out.loc[out["grant_ref"] == "", "grant_ref"] = None
    return out.drop(columns=["outstanding"]).reset_index(drop=True)


def missing_symbol(parsed: pd.DataFrame) -> bool:
    return bool(len(parsed)) and bool((parsed["symbol"] == "").any())


def grant_key(r) -> str:
    if r.get("grant_ref"):
        return str(r["grant_ref"])
    base = f"{r['symbol']}|{pd.Timestamp(r['grant_date']):%Y-%m-%d}|{float(r['strike']):.4f}|{float(r['quantity']):.4f}"
    return "h:" + hashlib.sha1(base.encode()).hexdigest()[:16]


def presplit_in_file(con, parsed: pd.DataFrame) -> dict:
    """{symbol: [(split date, ratio), ...]} for tickers in the file that have grants older than a known split."""
    sp = splits(con)
    out = {}
    if parsed.empty or sp.empty:
        return out
    for sym, grp in parsed.groupby("symbol"):
        s = sp[sp["symbol"] == sym]
        hits = [(d, float(r)) for d, r in zip(s["date"], s["ratio"]) if (grp["grant_date"] < d).any()]
        if hits:
            out[sym] = hits
    return out


def save_grants(con, parsed: pd.DataFrame, work_country="NL", adjusted=True) -> tuple[int, int]:
    """Insert new grants; update quantity/exercised/vested on grants already known (same grant ID).

    `adjusted` says whether the file's numbers already reflect later stock splits: True/False for every
    ticker, or {symbol: bool}. Unadjusted grants are stored as they are and then adjusted for the splits."""
    sp = splits(con)
    is_adj = (lambda s: bool(adjusted.get(s, True))) if isinstance(adjusted, dict) else (lambda s: bool(adjusted))
    added = updated = 0
    new_unadjusted = []
    for _, r in parsed.iterrows():
        d = r.to_dict()
        if not d.get("symbol"):
            raise ValueError("Every grant needs a ticker symbol.")
        key = grant_key(d)
        split = expected_factor(sp, d["symbol"], d["grant_date"]) if is_adj(d["symbol"]) else 1.0
        vals = (d["symbol"], d["plan_type"], f"{pd.Timestamp(d['grant_date']):%Y-%m-%d}",
                f"{pd.Timestamp(d['expiration']):%Y-%m-%d}" if pd.notna(d["expiration"]) else None,
                float(d["strike"]), float(d["quantity"]),
                None if pd.isna(d.get("vested")) else float(d["vested"]), float(d.get("exercised") or 0),
                float(d.get("cancelled") or 0))
        cur = con.execute("SELECT id, split_factor FROM option_grants WHERE grant_ref=?", (key,)).fetchone()
        if cur:
            # bring the file's numbers onto the stored row's split basis before updating
            scale = float(cur["split_factor"] or 1) / split
            sym_, pt, gd, ex, strike, qty, vest, exer, canc = vals
            vals_s = (sym_, pt, gd, ex, strike / scale, qty * scale, None if vest is None else vest * scale,
                      exer * scale, canc * scale)
            con.execute("UPDATE option_grants SET symbol=?, plan_type=?, grant_date=?, expiration=?, strike=?, quantity=?, "
                        "vested=?, exercised=MAX(exercised, ?), cancelled=MAX(cancelled, ?) WHERE id=?", (*vals_s, cur["id"]))
            updated += 1
        else:
            c = con.execute("INSERT INTO option_grants(symbol, plan_type, grant_date, expiration, strike, quantity, vested, "
                            "exercised, cancelled, grant_ref, work_country, split_factor, company) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (*vals, key, work_country, split, d.get("company") or d["symbol"]))
            added += 1
            exp = expected_factor(sp, d["symbol"], d["grant_date"])
            if exp > split * 1.0001:
                new_unadjusted.append((c.lastrowid, exp / split))
    for gid, ratio in new_unadjusted:
        apply_split(con, [gid], ratio)
    con.commit()
    return added, updated


def looks_unadjusted(con, parsed: pd.DataFrame, symbol: str):
    """Best guess whether the file's grants for `symbol` predating a known split are still on the pre-split basis.
    True / False, or None when there's nothing to go on (then the user is asked without a pre-selected answer)."""
    sp = splits(con)
    s = sp[sp["symbol"] == symbol]
    g = parsed[parsed["symbol"] == symbol]
    if s.empty or g.empty:
        return None
    first, ratio = s["date"].max(), float(s.loc[s["date"].idxmax(), "ratio"])
    pre, post = g[g["grant_date"] < first], g[g["grant_date"] >= first]
    if pre.empty:
        return None
    ref, _ = latest_price(con, symbol)
    if not ref and len(post):
        ref = float(post["strike"].median())
    if not ref:
        return None
    # which basis puts the old strikes nearer the reference price (log scale)? Only answer when it's clear-cut.
    import math
    med = float(pre["strike"].median())
    if med <= 0:
        return None
    d_adj = abs(math.log(med / ref))
    d_raw = abs(math.log(med / ratio / ref))
    if abs(d_adj - d_raw) < math.log(2):
        return None
    return d_raw < d_adj


# ------------------------------------------------------------------ net worth

def sync_networth(con, prices: dict, tax_rate: float, after_tax: bool, on=None):
    """Keep one account holding all options' in-the-money value (every company, USD) so net worth includes them."""
    v = value(grants(con), prices, on)
    total = float(v["intrinsic"].sum()) if not v.empty else 0.0
    if after_tax:
        total *= (1 - tax_rate)
    row = con.execute("SELECT id FROM accounts WHERE name=?", (NETWORTH_ACCOUNT,)).fetchone()
    if row:
        aid = row["id"]
    else:
        cur = con.execute("INSERT INTO accounts(name, institution, country, currency, kind, owner, tax_treatment) "
                          "VALUES(?,?,?,?,?,?,?)", (NETWORTH_ACCOUNT, "Employee stock plans", "US", "USD", "options", "me", "options"))
        aid = cur.lastrowid
    con.execute("INSERT INTO balances(account_id, date, balance, source) VALUES(?,?,?,'options') "
                "ON CONFLICT(account_id, date) DO UPDATE SET balance=excluded.balance, source='options'",
                (aid, (on or date.today()).isoformat(), total))
    con.commit()
    return total


def presplit_summary(con) -> pd.DataFrame:
    """Per known split of a ticker you hold: how many grants predate it and how many are marked adjusted."""
    g = grants(con)
    sp = splits(con)
    rows = []
    if g.empty or sp.empty:
        return pd.DataFrame(rows)
    for _, s in sp[sp["symbol"].isin(g["symbol"].unique())].iterrows():
        pre = g[(g["symbol"] == s["symbol"]) & (g["grant_date"] < s["date"])]
        if pre.empty:
            continue
        exp = pre.apply(lambda r: expected_factor(sp, r["symbol"], r["grant_date"]), axis=1)
        rows.append({"symbol": s["symbol"], "split_date": f"{s['date']:%Y-%m-%d}", "ratio": float(s["ratio"]),
                     "grants_before": int(len(pre)),
                     "marked_adjusted": int((pre["split_factor"].fillna(1) >= exp * 0.9999).sum()),
                     "strike_min": float(pre["strike"].min()), "strike_max": float(pre["strike"].max()),
                     "ids": pre["id"].tolist()})
    return pd.DataFrame(rows)


def force_split(con, grant_ids, ratio):
    """Apply a split to these grants regardless of how they're marked (fixes a wrong answer at import)."""
    apply_split(con, grant_ids, ratio)


def undo_split(con, grant_ids, ratio):
    """Reverse a split adjustment (if grants were adjusted twice or by mistake)."""
    for gid in grant_ids:
        con.execute("UPDATE option_grants SET quantity=quantity/?, strike=strike*?, vested=CASE WHEN vested IS NULL "
                    "THEN NULL ELSE vested/? END, exercised=exercised/?, cancelled=cancelled/?, split_factor=split_factor/? "
                    "WHERE id=?", (ratio, ratio, ratio, ratio, ratio, ratio, int(gid)))
    con.commit()


# ------------------------------------------------------------------ public price lookup (only the ticker is sent)

class PriceError(Exception):
    pass


def fetch_price(symbol: str, timeout: int = 10) -> dict:
    """Latest price from Yahoo Finance, falling back to Stooq. Returns {price, as_of, source}."""
    import requests
    from urllib.parse import quote
    symbol = clean_symbol(symbol)
    if not symbol:
        raise PriceError("That doesn't look like a ticker symbol.")
    headers = {"User-Agent": "Mozilla/5.0 (HouseholdFinance local app)"}
    errors = []
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
                         params={"range": "5d", "interval": "1d"}, headers=headers, timeout=timeout)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or meta["previousClose"])
        when = pd.to_datetime(meta.get("regularMarketTime"), unit="s", utc=True) if meta.get("regularMarketTime") else None
        if price > 0:
            return {"price": price, "as_of": when.tz_convert("Europe/Amsterdam").strftime("%d %b %Y %H:%M") if when is not None else "",
                    "source": "Yahoo Finance"}
    except Exception as e:  # noqa: BLE001
        errors.append(f"Yahoo Finance: {e}")
    try:
        r = requests.get("https://stooq.com/q/l/", params={"s": f"{symbol.lower()}.us", "f": "sd2t2c", "h": "", "e": "csv"},
                         headers=headers, timeout=timeout)
        r.raise_for_status()
        row = list(csv.DictReader(io.StringIO(r.text)))[0]
        price = float(row["Close"])
        if price > 0:
            return {"price": price, "as_of": f"{row.get('Date', '')} {row.get('Time', '')}".strip(), "source": "Stooq"}
        errors.append("Stooq: no price")
    except Exception as e:  # noqa: BLE001
        errors.append(f"Stooq: {e}")
    raise PriceError("Couldn't get a price. " + " · ".join(errors))
