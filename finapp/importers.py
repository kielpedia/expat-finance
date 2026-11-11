"""
CSV importers for Dutch and US bank exports.

Each parser returns a DataFrame with columns:
    date (datetime64), amount (float, negative = out), description, counterparty,
    balance_after (float or NaN), currency, account_hint (IBAN/account no. if present)

detect_format() sniffs the header so the user rarely has to pick a format.
"""
import csv
import hashlib
import io
import re
import sqlite3

import pandas as pd

OUT_COLS = ["date", "amount", "description", "counterparty", "balance_after", "currency", "account_hint"]


# ---------------------------------------------------------------- helpers

def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _sniff_delim(text: str) -> str:
    first = text.splitlines()[0] if text else ""
    counts = {d: first.count(d) for d in [";", ",", "\t", "|"]}
    return max(counts, key=counts.get)


def parse_number(val, decimal=None) -> float:
    """Parse '1.234,56', '1,234.56', '-12,3', '+5', '$1,200.00', '(45.00)'."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return float("nan")
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(" ", "").replace(" ", "")
    if not s:
        return float("nan")
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^\d,.\-+]", "", s)
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".")
    elif decimal == ".":
        s = s.replace(",", "")
    else:
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            # a single comma followed by 1-2 digits is a decimal separator
            s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return float("nan")
    return -abs(f) if neg else f


def _read(text: str, delim=None, header="infer") -> pd.DataFrame:
    delim = delim or _sniff_delim(text)
    return pd.read_csv(io.StringIO(text), sep=delim, dtype=str, header=header,
                       keep_default_na=False, quoting=csv.QUOTE_MINIMAL, engine="python")


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    for c in OUT_COLS:
        if c not in df:
            df[c] = None
    df = df[OUT_COLS].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["balance_after"] = pd.to_numeric(df["balance_after"], errors="coerce")
    df["description"] = df["description"].fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    df["counterparty"] = df["counterparty"].fillna("").astype(str).str.strip()
    return df.dropna(subset=["date", "amount"]).reset_index(drop=True)


def _cols(df):
    return {c.strip().lower(): c for c in df.columns}


# ---------------------------------------------------------------- Dutch banks

def parse_ing(text: str) -> pd.DataFrame:
    df = _read(text)
    c = _cols(df)
    amt_col = c.get("bedrag (eur)") or c.get("bedrag")
    sign = df[c["af bij"]].str.strip().str.lower().map(lambda x: -1 if x.startswith("af") else 1)
    out = pd.DataFrame({
        "date": pd.to_datetime(df[c["datum"]].str.strip(), format="%Y%m%d", errors="coerce"),
        "amount": df[amt_col].map(lambda v: parse_number(v, ",")) * sign,
        "counterparty": df[c["naam / omschrijving"]],
        "description": df[c.get("mededelingen", c["naam / omschrijving"])],
        "balance_after": df[c["saldo na mutatie"]].map(lambda v: parse_number(v, ",")) if "saldo na mutatie" in c else None,
        "currency": "EUR",
        "account_hint": df[c["rekening"]] if "rekening" in c else None,
    })
    return _norm(out)


def parse_rabobank(text: str) -> pd.DataFrame:
    df = _read(text)
    c = _cols(df)
    desc = df[[col for k, col in c.items() if k.startswith("omschrijving")]].agg(" ".join, axis=1)
    out = pd.DataFrame({
        "date": pd.to_datetime(df[c["datum"]], errors="coerce"),
        "amount": df[c["bedrag"]].map(lambda v: parse_number(v, ",")),
        "counterparty": df[c.get("naam tegenpartij", c["datum"])] if "naam tegenpartij" in c else "",
        "description": desc,
        "balance_after": df[c["saldo na trn"]].map(lambda v: parse_number(v, ",")) if "saldo na trn" in c else None,
        "currency": df[c["munt"]] if "munt" in c else "EUR",
        "account_hint": df[c["iban/bban"]] if "iban/bban" in c else None,
    })
    return _norm(out)


def parse_abnamro_tab(text: str) -> pd.DataFrame:
    # TXT export: account, currency, date, start bal, end bal, value date, amount, description (no header)
    df = _read(text, delim="\t", header=None)
    out = pd.DataFrame({
        "account_hint": df[0],
        "currency": df[1],
        "date": pd.to_datetime(df[2].str.strip(), format="%Y%m%d", errors="coerce"),
        "balance_after": df[4].map(lambda v: parse_number(v, ",")),
        "amount": df[6].map(lambda v: parse_number(v, ",")),
        "description": df[7],
        "counterparty": df[7].map(_abn_payee),
    })
    return _norm(out)


def _abn_payee(desc: str) -> str:
    """Pull the payee out of ABN AMRO's three description styles."""
    d = str(desc)
    m = re.search(r"/NAME/([^/]+)", d)                                  # SEPA, slash-delimited
    if m:
        return m.group(1).strip()
    m = re.search(r"Naam:\s*(.+?)(?=\s+(?:Machtiging|Omschrijving|IBAN|Kenmerk|BIC|Incassant):|\s{2,}|$)", d)  # SEPA, labelled
    if m:
        return m.group(1).strip()
    m = re.match(r"\s*(?:BEA|GEA|ePos|eCom)\b[^ ]*\s*,?\s*(?:Betaalpas|Apple Pay|Google Pay)?\s+(.+?)(?:,PAS\d+|\s{2,}|$)", d)  # card
    if m:
        return m.group(1).strip(" ,")
    return ""


def parse_bunq(text: str) -> pd.DataFrame:
    df = _read(text)
    c = _cols(df)
    out = pd.DataFrame({
        "date": pd.to_datetime(df[c["date"]], errors="coerce", dayfirst=False),
        "amount": df[c["amount"]].map(parse_number),
        "counterparty": df[c["name"]] if "name" in c else "",
        "description": df[c["description"]] if "description" in c else "",
        "currency": "EUR",
        "account_hint": df[c["account"]] if "account" in c else None,
    })
    return _norm(out)


def parse_wise(text: str) -> pd.DataFrame:
    df = _read(text)
    c = _cols(df)
    out = pd.DataFrame({
        "date": pd.to_datetime(df[c["date"]], errors="coerce", dayfirst=True),
        "amount": df[c["amount"]].map(parse_number),
        "currency": df[c["currency"]],
        "description": df[c["description"]],
        "counterparty": df[c["payee name"]] if "payee name" in c else (df[c["merchant"]] if "merchant" in c else ""),
        "balance_after": df[c["running balance"]].map(parse_number) if "running balance" in c else None,
    })
    return _norm(out)


# ---------------------------------------------------------------- US / generic

DATE_KEYS = ["transaction date", "date", "posting date", "post date", "posted date", "trade date", "run date"]
DESC_KEYS = ["description", "details", "payee", "name", "memo", "action", "transaction description"]
AMT_KEYS = ["amount", "amount ($)", "transaction amount", "net amount"]
DEBIT_KEYS = ["debit", "withdrawal", "withdrawals", "debit amount"]
CREDIT_KEYS = ["credit", "deposit", "deposits", "credit amount"]
BAL_KEYS = ["balance", "running bal.", "running balance", "runningbalance", "available balance"]


def _first(c, keys):
    for k in keys:
        if k in c:
            return c[k]
    return None


def guess_mapping(df: pd.DataFrame) -> dict:
    c = _cols(df)
    return {
        "date": _first(c, DATE_KEYS),
        "description": _first(c, DESC_KEYS),
        "amount": _first(c, AMT_KEYS),
        "debit": _first(c, DEBIT_KEYS),
        "credit": _first(c, CREDIT_KEYS),
        "balance": _first(c, BAL_KEYS),
    }


def parse_generic(text: str, mapping: dict | None = None, invert: bool = False,
                  currency: str = "USD", dayfirst: bool = False) -> pd.DataFrame:
    df = _read(text)
    # some US banks put summary lines before the real header; find it
    if not guess_mapping(df)["date"]:
        lines = text.splitlines()
        for i, line in enumerate(lines[:30]):
            if re.search(r"\bdate\b", line, re.I):
                df = _read("\n".join(lines[i:]))
                break
    m = {**guess_mapping(df), **(mapping or {})}
    if not m.get("date"):
        raise ValueError("Couldn't find a date column. Use the column mapper.")
    if m.get("amount"):
        amt = df[m["amount"]].map(lambda v: parse_number(v, "."))
    elif m.get("debit") or m.get("credit"):
        zero = pd.Series(0.0, index=df.index)
        deb = df[m["debit"]].map(lambda v: parse_number(v, ".")).fillna(0) if m.get("debit") else zero
        cre = df[m["credit"]].map(lambda v: parse_number(v, ".")).fillna(0) if m.get("credit") else zero
        amt = cre.abs() - deb.abs()
    else:
        raise ValueError("Couldn't find an amount column. Use the column mapper.")
    if invert:
        amt = -amt
    out = pd.DataFrame({
        # Schwab writes "10/28/2026 as of 10/27/2026"; keep the first date
        "date": pd.to_datetime(df[m["date"]].astype(str).str.split(" as of ").str[0].str.strip(),
                               errors="coerce", dayfirst=dayfirst),
        "amount": amt,
        "description": df[m["description"]] if m.get("description") else "",
        "counterparty": "",
        "balance_after": df[m["balance"]].map(lambda v: parse_number(v, ".")) if m.get("balance") else None,
        "currency": currency,
    })
    return _norm(out)


# ---------------------------------------------------------------- detection

FORMATS = {
    "ing": ("ING (NL)", "EUR", "NL"),
    "rabobank": ("Rabobank (NL)", "EUR", "NL"),
    "abnamro": ("ABN AMRO TXT (NL)", "EUR", "NL"),
    "bunq": ("bunq (NL)", "EUR", "NL"),
    "wise": ("Wise (multi-currency)", "EUR", "BE"),
    "amex": ("American Express (US)", "USD", "US"),
    "schwab_brokerage": ("Charles Schwab brokerage (US)", "USD", "US"),
    "schwab_bank": ("Charles Schwab Bank checking (US)", "USD", "US"),
    "schwab_positions": ("Charles Schwab positions: account values (US)", "USD", "US"),
    "generic_us": ("US bank / card / brokerage (auto-mapped)", "USD", "US"),
}


def detect_format(text: str) -> str:
    head = text[:2000].lower()
    first = head.splitlines()[0] if head else ""
    if "af bij" in first and "naam / omschrijving" in first:
        return "ing"
    if "iban/bban" in first and "volgnr" in first:
        return "rabobank"
    if "\t" in first and re.match(r"^\s*\d{6,12}\t[a-z]{3}\t\d{8}\t", first):
        return "abnamro"
    if "interest date" in first and "counterparty" in first:
        return "bunq"
    if "transferwise id" in first or ("running balance" in first and "exchange rate" in first):
        return "wise"
    if first.strip().startswith("date,description,") and ("card member" in first or "extended details" in first):
        return "amex"
    if "action" in first and "symbol" in first and ("fees & comm" in first or "quantity" in first):
        return "schwab_brokerage"
    if "checknumber" in first and "runningbalance" in first:
        return "schwab_bank"
    return "generic_us"


def parse(text: str, fmt: str, **kw) -> pd.DataFrame:
    if fmt == "ing":
        return parse_ing(text)
    if fmt == "rabobank":
        return parse_rabobank(text)
    if fmt == "abnamro":
        return parse_abnamro_tab(text)
    if fmt == "bunq":
        return parse_bunq(text)
    if fmt == "wise":
        return parse_wise(text)
    if fmt == "schwab_brokerage":
        return parse_schwab_brokerage(text)
    if fmt == "schwab_bank":
        return parse_generic(text, currency="USD")
    if fmt == "amex":
        # Amex lists charges as positive numbers
        return parse_generic(text, invert=True, currency=kw.get("currency", "USD"))
    return parse_generic(text, **kw)


def read_upload(raw: bytes) -> str:
    return _skip_preamble(_decode(raw))


def _skip_preamble(text: str) -> str:
    """Drop title lines some banks put above the header (Schwab: "Transactions for account ...")."""
    lines = text.splitlines()

    def width(line):
        return max(line.count(d) for d in (",", ";", "\t", "|"))

    head = lines[:15]
    if not head or width(head[0]) >= 2:
        return text
    for i, line in enumerate(head):
        if width(line) >= 2:
            return "\n".join(lines[i:])
    return text


def row_hash(account_id: int, row, seq: int) -> str:
    key = f"{account_id}|{row['date']:%Y-%m-%d}|{row['amount']:.2f}|{row['description'][:120]}|{seq}"
    return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()


def ref_hash(account_id: int, row, ref: str) -> str:
    """For rows that carry the bank's own reference (bank sync): stable even if the bank rewords the description."""
    key = f"{account_id}|ref|{ref}|{row['date']:%Y-%m-%d}|{row['amount']:.2f}"
    return hashlib.sha256(key.encode()).hexdigest()


def save(con, account_id: int, df: pd.DataFrame, filename: str, fmt: str) -> tuple[int, int]:
    """Insert rows, skipping duplicates. Same-day identical rows are kept apart by a sequence number.
    Rows with an `ext_ref` (the bank's reference, from bank sync) are matched on that instead."""
    cur = con.execute("INSERT INTO imports(filename, account_id, fmt, rows, added) VALUES(?,?,?,?,0)",
                      (filename, account_id, fmt, len(df)))
    import_id = cur.lastrowid
    # Many banks export newest-first. Process oldest-first so the last row of each day
    # carries that day's closing balance.
    if len(df) > 1 and df["date"].iloc[0] > df["date"].iloc[-1]:
        df = df.iloc[::-1].reset_index(drop=True)
    seen = {}
    added = 0
    for _, r in df.iterrows():
        base = (r["date"].date(), round(r["amount"], 2), r["description"][:120])
        seq = seen.get(base, 0)
        seen[base] = seq + 1
        ref = r.get("ext_ref") if "ext_ref" in df.columns else None
        h = ref_hash(account_id, r, ref) if isinstance(ref, str) and ref else row_hash(account_id, r, seq)
        bal = None if pd.isna(r["balance_after"]) else float(r["balance_after"])
        try:
            con.execute(
                "INSERT INTO transactions(account_id, date, amount, description, counterparty, balance_after, import_id, hash) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (account_id, r["date"].strftime("%Y-%m-%d"), float(r["amount"]), r["description"],
                 r["counterparty"], bal, import_id, h),
            )
            added += 1
        except sqlite3.IntegrityError:  # duplicate hash: already imported
            pass
        if bal is not None:
            con.execute(
                "INSERT INTO balances(account_id, date, balance, source) VALUES(?,?,?,'import') "
                "ON CONFLICT(account_id, date) DO UPDATE SET balance=excluded.balance WHERE balances.source='import'",
                (account_id, r["date"].strftime("%Y-%m-%d"), bal),
            )
    con.execute("UPDATE imports SET added=? WHERE id=?", (added, import_id))
    con.commit()
    return len(df), added


def parse_schwab_brokerage(text: str) -> pd.DataFrame:
    """Schwab brokerage history: Date, Action, Symbol, Description, Quantity, Price, Fees & Comm, Amount.
    Action and symbol go into the description so trades and dividends categorise correctly."""
    df = _read(text)
    if "action" not in _cols(df):
        lines = text.splitlines()
        for i, line in enumerate(lines[:10]):
            if re.search(r"\baction\b", line, re.I):
                df = _read("\n".join(lines[i:]))
                break
    c = _cols(df)
    desc = (df[c["action"]].str.strip() + " " + df[c["symbol"]].str.strip() + " " +
            (df[c["description"]].str.strip() if "description" in c else ""))
    amount = df[c["amount"]].map(lambda v: parse_number(v, "."))
    # Shares moved in or out without cash (ACATS transfers, journals, rollovers in kind) have no amount.
    # Keep them at 0 with the share count in the description so the history is complete;
    # their value reaches net worth through a Positions import or a balance snapshot.
    qty = df[c["quantity"]].map(lambda v: parse_number(v, ".")) if "quantity" in c else pd.Series(float("nan"), index=df.index)
    in_kind = amount.isna() & qty.notna()
    desc = desc.where(~in_kind, desc + " (in kind: " + qty.map(lambda q: f"{q:,.4g}") + " shares, no cash)")
    amount = amount.where(~in_kind, 0.0)
    out = pd.DataFrame({
        "date": pd.to_datetime(df[c["date"]].astype(str).str.split(" as of ").str[0].str.strip(), errors="coerce"),
        "amount": amount,
        "description": desc,
        "counterparty": "Schwab: " + df[c["action"]].str.strip(),
        "currency": "USD",
    })
    return _norm(out)


# ---------------------------------------------------------------- Schwab positions (account values)

def is_schwab_positions(raw_text: str) -> bool:
    head = raw_text[:3000].lower()
    return ("symbol" in head and ("mkt val" in head or "market value" in head)
            and ("account total" in raw_text.lower() or "positions for" in head))


def parse_schwab_positions(raw_text: str) -> list[dict]:
    """
    Schwab Positions export -> one entry per account section:
        {"section": "Roth Contributory IRA ...456", "date": Timestamp, "total": float,
         "cash": float|None, "holdings": DataFrame(symbol, description, quantity, market_value, cost_basis, security_type)}
    Handles single-account exports and the 'All accounts' export with several sections.
    Pass the raw decoded text (not read_upload's output) so the title line with the date is kept.
    """
    rows = list(csv.reader(io.StringIO(raw_text)))
    title = next((",".join(r) for r in rows[:3] if r), "")
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", title)
    as_of = pd.to_datetime(m.group(1), format="%m/%d/%Y") if m else pd.Timestamp.today().normalize()
    m = re.search(r"positions for (?:account )?(.+?)(?: as of|$)", title, re.I)
    default_section = m.group(1).strip() if m else "Schwab account"

    sections, cur, header, last_label = [], None, None, default_section
    for r in rows:
        cells = [x.strip() for x in r]
        nonempty = [x for x in cells if x]
        if not nonempty:
            continue
        low = [x.lower() for x in cells]
        if len(nonempty) == 1 and not header_like(low):
            # a lone cell is a section label in the all-accounts export ("Individual ...123")
            if not nonempty[0].lower().startswith("positions for"):
                last_label = nonempty[0]
            continue
        if header_like(low):
            header = {name: i for i, name in enumerate(low)}
            cur = {"section": last_label, "date": as_of, "total": None, "cash": None, "holdings": [], "audit": []}
            sections.append(cur)
            continue
        if header is None or cur is None:
            continue
        get = lambda *keys: next((cells[i] for k, i in header.items() for kk in keys if k.startswith(kk) and i < len(cells)), "")
        sym = get("symbol")
        mv = parse_number(get("mkt val", "market value"), ".")
        if TOTAL_ROW.match(sym):
            cur["audit"].append({"row": sym, "used_as": "account total", "market_value": mv})
            if cur["total"] is None:
                cur["total"] = mv
            header = None   # the next section starts with its own header
            continue
        cur["audit"].append({"row": sym or "(blank)", "used_as": "cash" if CASH_ROW.match(sym) else
                             ("holding" if sym else "skipped"), "market_value": mv})
        if CASH_ROW.match(sym):
            cur["cash"] = mv
            cur["holdings"].append({"symbol": "CASH", "description": sym, "quantity": None, "market_value": mv,
                                    "cost_basis": None, "security_type": "Cash"})
        elif sym:
            cur["holdings"].append({
                "symbol": sym, "description": get("description"),
                "quantity": parse_number(get("qty", "quantity"), "."), "market_value": mv,
                "cost_basis": parse_number(get("cost basis"), "."), "security_type": get("security type", "asset type"),
            })
    out = []
    for sct in sections:
        h = pd.DataFrame(sct["holdings"], columns=["symbol", "description", "quantity", "market_value", "cost_basis", "security_type"])
        sct["sum_of_rows"] = float(h["market_value"].fillna(0).sum())
        sct["unpriced_rows"] = int(h["market_value"].isna().sum())
        if sct["total"] is None or pd.isna(sct["total"]):
            sct["total"] = sct["sum_of_rows"]
            sct["total_source"] = "sum of holdings + cash (no total row found)"
        else:
            sct["total_source"] = "the file's total row"
        sct["mismatch"] = abs(sct["total"] - sct["sum_of_rows"]) > 1
        sct["holdings"] = h
        sct["audit"] = pd.DataFrame(sct["audit"], columns=["row", "used_as", "market_value"])
        out.append(sct)
    return out


TOTAL_ROW = re.compile(r"^\s*(account|positions|portfolio|grand)?\s*totals?\b", re.I)
CASH_ROW = re.compile(r"^\s*cash\b", re.I)


def header_like(low_cells) -> bool:
    return any(x == "symbol" for x in low_cells) and any(x.startswith(("mkt val", "market value")) for x in low_cells)


def guess_kind(section: str) -> str:
    s = section.lower()
    if re.search(r"\bira\b|401|403|457|roth|rollover|sep\b|simple|pension|retirement|\btsp\b|netbenefits", s):
        return "retirement"
    return "brokerage"


# ---------------------------------------------------------------- Fidelity positions (all accounts in one file)

def is_fidelity_positions(raw_text: str) -> bool:
    first = next((ln for ln in raw_text.splitlines() if ln.strip()), "").lower()
    return "account number" in first and "account name" in first and "current value" in first


def parse_fidelity_positions(raw_text: str, filename: str = "") -> list[dict]:
    """
    Fidelity 'Portfolio Positions' export: one table, every row tagged with Account Number + Account Name.
    Returns one section per account in the same shape as parse_schwab_positions.
    Money market sweep (SPAXX** etc.) counts as cash; 'Pending Activity' is included, as on Fidelity's site.
    """
    rows = list(csv.reader(io.StringIO(raw_text)))
    hdr_i = next(i for i, r in enumerate(rows) if r and "account number" in ",".join(r).lower())
    header = [h.strip().lower() for h in rows[hdr_i]]
    col = lambda *names: next((i for i, h in enumerate(header) for n in names if h.startswith(n)), None)
    i_num, i_name, i_sym, i_desc = col("account number"), col("account name"), col("symbol"), col("description")
    i_qty, i_val, i_cost, i_type = col("quantity"), col("current value"), col("cost basis total"), col("type")

    # date: "Date downloaded Oct-30-2026 ..." in the footer, else from a filename like Portfolio_Positions_Oct-30-2026.csv
    as_of = None
    m = re.search(r"date downloaded\s+([A-Za-z]{3}-\d{1,2}-\d{4})", raw_text, re.I) or \
        re.search(r"([A-Za-z]{3}-\d{1,2}-\d{4})", filename or "")
    if m:
        as_of = pd.to_datetime(m.group(1), format="%b-%d-%Y", errors="coerce")
    if as_of is None or pd.isna(as_of):
        as_of = pd.Timestamp.today().normalize()

    cell = lambda r, i: r[i].strip() if i is not None and i < len(r) else ""
    accounts = {}
    for r in rows[hdr_i + 1:]:
        num = cell(r, i_num)
        if not num or len(r) < 5 or num.lower().startswith(("the data", "brokerage services", "date downloaded")):
            continue
        name = cell(r, i_name)
        sym = cell(r, i_sym)
        val = parse_number(cell(r, i_val), ".")
        key = (num, name)
        sec = accounts.setdefault(key, {"section": f"{name} ...{num[-4:]}", "date": as_of, "total": None, "cash": 0.0,
                                        "holdings": [], "audit": []})
        is_pending = sym.lower().startswith("pending")
        is_cash = sym.endswith("**") or "money market" in cell(r, i_desc).lower() or sym.upper() == "CORE"
        used = "pending activity" if is_pending else "cash" if is_cash else "holding"
        sec["audit"].append({"row": sym or cell(r, i_desc) or "(blank)", "used_as": used, "market_value": val})
        if is_cash and pd.notna(val):
            sec["cash"] += val
        sec["holdings"].append({"symbol": sym.rstrip("*") or "(none)", "description": cell(r, i_desc),
                                "quantity": parse_number(cell(r, i_qty), "."), "market_value": val,
                                "cost_basis": parse_number(cell(r, i_cost), "."), "security_type": used if used != "holding" else cell(r, i_type)})
    out = []
    for sec in accounts.values():
        h = pd.DataFrame(sec["holdings"], columns=["symbol", "description", "quantity", "market_value", "cost_basis", "security_type"])
        sec["sum_of_rows"] = float(h["market_value"].fillna(0).sum())
        sec["unpriced_rows"] = int(h["market_value"].isna().sum())
        sec["total"] = sec["sum_of_rows"]
        sec["total_source"] = "the sum of this account's rows (Fidelity's file has no total row)"
        sec["mismatch"] = False
        sec["holdings"] = h
        sec["audit"] = pd.DataFrame(sec["audit"], columns=["row", "used_as", "market_value"])
        out.append(sec)
    return out


def detect_positions(raw_text: str):
    """Return (broker, institution label) if the file is a positions/holdings export, else None."""
    if is_fidelity_positions(raw_text):
        return "fidelity", "Fidelity"
    if is_schwab_positions(raw_text):
        return "schwab", "Charles Schwab"
    return None


def parse_positions(raw_text: str, broker: str, filename: str = "") -> list[dict]:
    return parse_fidelity_positions(raw_text, filename) if broker == "fidelity" else parse_schwab_positions(raw_text)
