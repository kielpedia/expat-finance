"""
Bank connections through Enable Banking: linking a bank, matching its accounts to yours, and syncing transactions
and balances. Kept apart from the UI so a scheduled job can call `sync_all()` later.

What's stored (in the database, like everything else):
- `bank_links`: one row per bank approval: Enable Banking's session ID, the bank, and when the approval ends.
  A session ID is useless without the private key, which is never in the database.
- `bank_accounts`: the accounts that approval covers (IBAN, name) and which of your accounts each one fills.
- `bank_auth_states`: short-lived, single-use `state` values that tie the bank's redirect back to a request
  this app started.

Synced transactions go through `importers.save`, like a CSV import. They are matched on the bank's own
reference, so syncing the same days twice adds nothing. Each account only syncs from its start date on, which
defaults to the day after the last transaction you already have, so CSV history isn't counted twice.
"""
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC

import pandas as pd

from . import categorize, importers
from .enablebanking import Client, EBError

STATE_TTL = timedelta(hours=1)
DEFAULT_VALIDITY = timedelta(days=90)
MAX_VALIDITY = timedelta(days=180)
HISTORY_DAYS = 89          # banks usually give about 90 days of history once the first hour after approval has passed
OVERLAP_DAYS = 7           # re-read the last week on each sync, for transactions the bank books late
BALANCE_TYPES = ["CLBD", "ITBD", "XPCD", "ITAV", "CLAV", "OPBD"]   # closing booked first, then interim/expected
FMT = "enablebanking"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_links (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    aspsp_name TEXT NOT NULL,
    aspsp_country TEXT NOT NULL,
    valid_until TEXT,
    status TEXT DEFAULT 'AUTHORIZED',        -- AUTHORIZED, EXPIRED
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bank_accounts (
    uid TEXT PRIMARY KEY,                    -- Enable Banking's account ID within the session
    link_id INTEGER NOT NULL REFERENCES bank_links(id) ON DELETE CASCADE,
    iban TEXT,
    name TEXT,
    currency TEXT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,   -- NULL = not synced
    start_date TEXT,                         -- never fetch before this (the CSV cut-over)
    last_synced TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS bank_auth_states (
    state TEXT PRIMARY KEY,
    aspsp_name TEXT NOT NULL,
    aspsp_country TEXT NOT NULL,
    created TEXT NOT NULL
);
"""


def setup(con):
    con.executescript(SCHEMA)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ---------------------------------------------------------------- linking

def consent_validity(aspsp: dict | None) -> timedelta:
    secs = (aspsp or {}).get("maximum_consent_validity")
    try:
        v = timedelta(seconds=int(secs))
    except (TypeError, ValueError):
        return DEFAULT_VALIDITY
    return min(v, MAX_VALIDITY)


def begin_link(con, client: Client, redirect_url: str, aspsp: dict, now: datetime | None = None) -> str:
    """Start a bank approval. Returns the URL to send the browser to."""
    now = now or _now()
    con.execute("DELETE FROM bank_auth_states WHERE created < ?", ((now - STATE_TTL).isoformat(),))
    state = secrets.token_urlsafe(24)
    # A minute short of the bank's maximum, so the request is never rejected for asking a second too long.
    valid_until = (now + consent_validity(aspsp) - timedelta(minutes=1)).isoformat(timespec="seconds")
    resp = client.start_auth(aspsp["name"], aspsp["country"], redirect_url, state, valid_until)
    con.execute("INSERT INTO bank_auth_states(state, aspsp_name, aspsp_country, created) VALUES(?,?,?,?)",
                (state, aspsp["name"], aspsp["country"], now.isoformat()))
    con.commit()
    return resp["url"]


def finish_link(con, client: Client, code: str, state: str, now: datetime | None = None) -> tuple[int, int]:
    """Turn the code from the bank's redirect into a session. Returns (link id, number of accounts).
    The state must be one this app issued in the last hour, and it can be used once."""
    now = now or _now()
    row = con.execute("SELECT * FROM bank_auth_states WHERE state=?", (state or "",)).fetchone()
    if row is None or (_parse_dt(row["created"]) or now) < now - STATE_TTL:
        raise EBError("This bank approval wasn't started here, has expired or was already used. Start again from "
                      "*Connect a bank*.")
    con.execute("DELETE FROM bank_auth_states WHERE state=?", (state,))
    con.commit()
    sess = client.create_session(code)
    accounts = sess.get("accounts") or []
    if not accounts:
        try:
            client.delete_session(sess["session_id"])
        except EBError:
            pass
        raise EBError("The bank approved access, but no accounts came back. In Enable Banking's free (restricted) "
                      "mode only accounts you linked in its Control Panel are shared: link them there, then connect again.")
    valid_until = (sess.get("access") or {}).get("valid_until")
    cur = con.execute("INSERT INTO bank_links(session_id, aspsp_name, aspsp_country, valid_until) VALUES(?,?,?,?)",
                      (sess["session_id"], row["aspsp_name"], row["aspsp_country"], valid_until))
    link_id = cur.lastrowid
    for a in accounts:
        iban = ((a.get("account_id") or {}).get("iban") or "").replace(" ", "").upper() or None
        prev = con.execute("SELECT account_id, start_date, last_synced FROM bank_accounts WHERE iban=? AND link_id<>? "
                           "ORDER BY link_id DESC LIMIT 1", (iban, link_id)).fetchone() if iban else None
        con.execute("INSERT OR REPLACE INTO bank_accounts(uid, link_id, iban, name, currency, account_id, start_date, last_synced) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (a["uid"], link_id, iban, a.get("name") or a.get("product") or "", a.get("currency") or "",
                     prev["account_id"] if prev else None, prev["start_date"] if prev else None,
                     prev["last_synced"] if prev else None))
        if prev:   # a renewed approval for the same account replaces the old one
            con.execute("DELETE FROM bank_accounts WHERE iban=? AND link_id<>?", (iban, link_id))
    con.commit()
    for old in con.execute("SELECT id, session_id FROM bank_links l WHERE id<>? AND NOT EXISTS "
                           "(SELECT 1 FROM bank_accounts b WHERE b.link_id=l.id)", (link_id,)).fetchall():
        remove_link(con, client, old["id"])
    return link_id, len(accounts)


def remove_link(con, client: Client | None, link_id: int) -> None:
    """Forget a bank approval here, and close it at Enable Banking (and so at the bank) when possible.
    Transactions already synced stay."""
    row = con.execute("SELECT session_id FROM bank_links WHERE id=?", (link_id,)).fetchone()
    if row is None:
        return
    if client is not None:
        try:
            client.delete_session(row["session_id"])
        except EBError:
            pass   # already closed or expired at the bank; removing it here is what matters
    con.execute("DELETE FROM bank_accounts WHERE link_id=?", (link_id,))
    con.execute("DELETE FROM bank_links WHERE id=?", (link_id,))
    con.commit()


def default_start(con, account_id: int, today: date | None = None) -> date:
    """The day after the last transaction you already have in that account, or about 90 days back."""
    today = today or date.today()
    last = con.execute("SELECT MAX(date) FROM transactions WHERE account_id=?", (account_id,)).fetchone()[0]
    if last:
        return date.fromisoformat(last) + timedelta(days=1)
    return today - timedelta(days=HISTORY_DAYS)


def map_account(con, uid: str, account_id: int | None, start: date | None) -> None:
    con.execute("UPDATE bank_accounts SET account_id=?, start_date=?, last_error=NULL WHERE uid=?",
                (account_id, start.isoformat() if (start and account_id) else None, uid))
    con.commit()


def links(con) -> list[dict]:
    out = []
    for link in con.execute("SELECT * FROM bank_links ORDER BY aspsp_name, id").fetchall():
        d = dict(link)
        d["accounts"] = [dict(r) for r in con.execute(
            "SELECT b.*, a.name AS account_name FROM bank_accounts b LEFT JOIN accounts a ON a.id=b.account_id "
            "WHERE b.link_id=? ORDER BY b.name, b.iban", (link["id"],))]
        out.append(d)
    return out


def days_left(link: dict, now: datetime | None = None) -> int | None:
    until = _parse_dt(link.get("valid_until"))
    if until is None:
        return None
    return (until - (now or _now())).days


# ---------------------------------------------------------------- converting

def _amount(t: dict) -> float:
    raw = float((t.get("transaction_amount") or {})["amount"])
    ind = t.get("credit_debit_indicator")
    if ind == "DBIT":
        return -abs(raw)
    if ind == "CRDT":
        return abs(raw)
    return raw


def _text(v) -> str:
    if isinstance(v, list):
        return " ".join(str(x) for x in v if x)
    return str(v or "")


def to_frame(txs: list[dict], currency: str = "") -> tuple[pd.DataFrame, int]:
    """API transactions -> the importers' DataFrame (plus `ext_ref`). Returns (frame, rows skipped).
    Pending rows and rows in another currency than the account are skipped."""
    rows, skipped = [], 0
    for t in txs:
        if t.get("status") not in (None, "", "BOOK"):
            skipped += 1
            continue
        ccy = (t.get("transaction_amount") or {}).get("currency") or currency
        when = t.get("booking_date") or t.get("value_date") or t.get("transaction_date")
        try:
            amount = _amount(t)
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if not when or (currency and ccy and ccy != currency):
            skipped += 1
            continue
        creditor = (t.get("creditor") or {}).get("name") or ""
        debtor = (t.get("debtor") or {}).get("name") or ""
        counterparty = (creditor or debtor) if amount < 0 else (debtor or creditor)
        desc = (_text(t.get("remittance_information")) or _text(t.get("note"))
                or (t.get("bank_transaction_code") or {}).get("description") or counterparty)
        bal = (t.get("balance_after_transaction") or {}).get("amount")
        rows.append({"date": when, "amount": amount, "description": desc, "counterparty": counterparty,
                     "balance_after": bal, "currency": ccy, "account_hint": None,
                     "ext_ref": t.get("entry_reference") or None})
    df = pd.DataFrame(rows, columns=importers.OUT_COLS + ["ext_ref"])
    if df.empty:
        return df, skipped
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["balance_after"] = pd.to_numeric(df["balance_after"], errors="coerce")
    df["description"] = df["description"].fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    df["counterparty"] = df["counterparty"].fillna("").astype(str).str.strip()
    bad = df["date"].isna() | df["amount"].isna()
    df = df[~bad].sort_values("date", kind="stable").reset_index(drop=True)
    return df, skipped + int(bad.sum())


def pick_balance(balances: list[dict], currency: str = "") -> tuple[float, str | None] | None:
    usable = [b for b in balances if not currency or (b.get("balance_amount") or {}).get("currency") in (None, currency)]
    usable.sort(key=lambda b: BALANCE_TYPES.index(b.get("balance_type")) if b.get("balance_type") in BALANCE_TYPES else 99)
    for b in usable:
        try:
            return float(b["balance_amount"]["amount"]), b.get("reference_date")
        except (KeyError, TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------- syncing

@dataclass
class Result:
    bank: str
    account: str
    fetched: int = 0
    added: int = 0
    skipped: int = 0
    balance: float | None = None
    error: str = ""


def _friendly(e: EBError) -> str:
    if e.code == "EXPIRED_SESSION" or e.status == 401:
        return "The bank approval has ended. Click *Renew* to approve again."
    if e.code == "ASPSP_RATE_LIMIT_EXCEEDED" or e.status == 429:
        return "The bank allows only a few updates a day. Try again in about 6 hours."
    if e.code == "WRONG_TRANSACTIONS_PERIOD":
        return ("The bank doesn't give transactions that far back (usually about 90 days). Set a later start date, "
                "and import a CSV for the gap.")
    if e.code == "ASPSP_ERROR":
        return "The bank had a problem. Try again in a minute or two."
    return str(e)


def sync_account(con, client: Client, link: dict, acct: dict, today: date | None = None) -> Result:
    today = today or date.today()
    res = Result(link["aspsp_name"], acct.get("account_name") or acct.get("name") or acct.get("iban") or acct["uid"])
    start = date.fromisoformat(acct["start_date"]) if acct.get("start_date") else default_start(con, acct["account_id"], today)
    date_from = start
    if acct.get("last_synced"):
        date_from = max(start, date.fromisoformat(acct["last_synced"]) - timedelta(days=OVERLAP_DAYS))
    try:
        txs = client.transactions(acct["uid"], date_from.isoformat())
        df, res.skipped = to_frame(txs, acct.get("currency") or "")
        res.fetched = len(df)
        df = df[df["date"].dt.date >= start] if not df.empty else df
        if not df.empty:
            _, res.added = importers.save(con, acct["account_id"], df.reset_index(drop=True),
                                          f"Enable Banking · {link['aspsp_name']}", FMT)
        bal = pick_balance(client.balances(acct["uid"]), acct.get("currency") or "")
        if bal is not None:
            res.balance, ref = bal
            on = ref[:10] if ref else today.isoformat()
            con.execute("INSERT INTO balances(account_id, date, balance, source) VALUES(?,?,?,'import') "
                        "ON CONFLICT(account_id, date) DO UPDATE SET balance=excluded.balance WHERE balances.source='import'",
                        (acct["account_id"], on, res.balance))
        con.execute("UPDATE bank_accounts SET last_synced=?, last_error=NULL, start_date=COALESCE(start_date, ?) WHERE uid=?",
                    (today.isoformat(), start.isoformat(), acct["uid"]))
    except EBError as e:
        res.error = _friendly(e)
        con.execute("UPDATE bank_accounts SET last_error=? WHERE uid=?", (res.error, acct["uid"]))
        if e.code == "EXPIRED_SESSION" or e.status == 401:
            con.execute("UPDATE bank_links SET status='EXPIRED' WHERE id=?", (link["id"],))
    con.commit()
    return res


def sync_all(con, client: Client, today: date | None = None, now: datetime | None = None) -> list[Result]:
    """Sync every mapped account of every approval that hasn't ended. Safe to call from a scheduled job."""
    results = []
    for link in links(con):
        left = days_left(link, now)
        if link["status"] == "EXPIRED" or (left is not None and left < 0):
            con.execute("UPDATE bank_links SET status='EXPIRED' WHERE id=?", (link["id"],))
            con.commit()
            results += [Result(link["aspsp_name"], a.get("account_name") or a.get("name") or "",
                               error="The bank approval has ended. Click *Renew* to approve again.")
                        for a in link["accounts"] if a["account_id"]]
            continue
        for acct in link["accounts"]:
            if acct["account_id"]:
                results.append(sync_account(con, client, link, acct, today))
    if any(r.added for r in results):
        categorize.apply(con, only_uncategorized=True)
    return results
