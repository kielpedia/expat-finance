"""Enable Banking client and bank sync, against a fake API (no network, a throwaway key)."""
import base64
import json
import os
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from finapp import banksync, db
from finapp import enablebanking as eb

APP_ID = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


@pytest.fixture(scope="module")
def key():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return k, pem


class Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.content = b"x" if data is not None else b""
        self.reason = "error"

    def json(self):
        return self._data


class FakeAPI:
    """Answers like Enable Banking. `routes` maps (METHOD, path) to a response dict, a list of them (one per call),
    or (status, body) for an error."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.replace(eb.API, "")
        self.calls.append({"method": method, "path": path, "params": dict(params or {}), "json": json, "headers": headers})
        r = self.routes.get((method, path), {})
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, tuple):
            return Resp(*r)
        return Resp(200, r)


def client(key, routes=None):
    api = FakeAPI(routes)
    return eb.Client(APP_ID, key[1], http=api), api


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- client

def test_jwt_is_signed_rs256_with_the_documented_claims(key):
    tok = eb.make_jwt(APP_ID, key[1], now=1_000_000)
    h, b, s = tok.split(".")
    assert json.loads(b64d(h)) == {"typ": "JWT", "alg": "RS256", "kid": APP_ID}
    body = json.loads(b64d(b))
    assert body["iss"] == "enablebanking.com" and body["aud"] == "api.enablebanking.com"
    assert 0 < body["exp"] - body["iat"] <= 86400
    key[0].public_key().verify(b64d(s), f"{h}.{b}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_every_request_is_authorised(key):
    c, api = client(key, {("GET", "/aspsps"): {"aspsps": [{"name": "ING", "country": "NL"}]}})
    assert c.aspsps("NL")[0]["name"] == "ING"
    assert api.calls[0]["headers"]["Authorization"].startswith("Bearer ey")
    assert api.calls[0]["params"] == {"country": "NL", "psu_type": "personal"}


def test_auth_asks_for_read_access_only(key):
    c, api = client(key, {("POST", "/auth"): {"url": "https://bank.example/approve"}})
    c.start_auth("ING", "NL", "https://finance.example/import", "st", "2027-05-01T00:00:00+00:00")
    access = api.calls[0]["json"]["access"]
    assert access == {"valid_until": "2027-05-01T00:00:00+00:00", "balances": True, "transactions": True}


def test_errors_carry_the_api_code(key):
    c, _ = client(key, {("GET", "/accounts/u1/balances"): (401, {"error": "EXPIRED_SESSION", "message": "Session expired"})})
    with pytest.raises(eb.EBError) as e:
        c.balances("u1")
    assert e.value.code == "EXPIRED_SESSION" and e.value.status == 401


def test_transactions_follow_continuation_keys_through_empty_pages(key):
    pages = [{"transactions": [{"entry_reference": "a"}], "continuation_key": "k1"},
             {"transactions": [], "continuation_key": "k2"},
             {"transactions": [{"entry_reference": "b"}], "continuation_key": None}]
    c, api = client(key, {("GET", "/accounts/u1/transactions"): pages})
    got = c.transactions("u1", "2026-10-01")
    assert [t["entry_reference"] for t in got] == ["a", "b"]
    assert [x["params"].get("continuation_key") for x in api.calls] == [None, "k1", "k2"]
    assert all(x["params"]["date_from"] == "2026-10-01" and x["params"]["transaction_status"] == "BOOK" for x in api.calls)


def test_key_file_problems(tmp_path, key):
    missing = tmp_path / "nope.pem"
    assert "no private key" in eb.key_problem(missing)
    with pytest.raises(eb.EBError, match="no private key"):
        eb.Client.from_config(eb.Config(APP_ID, missing, ""))
    p = tmp_path / "k.pem"
    p.write_bytes(key[1])
    if os.name != "nt":
        p.chmod(0o644)
        assert "other users" in eb.key_problem(p)
        p.chmod(0o600)
    assert eb.key_problem(p) is None
    assert eb.Client.from_config(eb.Config(APP_ID, p, "")).app_id == APP_ID


def test_config_prefers_environment_and_never_reads_a_key_from_settings(tmp_path):
    env = {"FINAPP_EB_APP_ID": "env-id", "FINAPP_EB_KEY_FILE": str(tmp_path / "k.pem"),
           "FINAPP_EB_REDIRECT_URL": "https://finance.example/import"}
    cfg = eb.config(env, {"app_id": "saved-id", "private_key": "-----BEGIN"})
    assert (cfg.app_id, cfg.key_file, cfg.redirect_url) == ("env-id", Path(tmp_path / "k.pem"), "https://finance.example/import")
    assert eb.config({}, {"app_id": "saved-id"}).app_id == "saved-id"


# ---------------------------------------------------------------- converting

def tx(ref, day, amount, ind, **kw):
    return {"entry_reference": ref, "booking_date": day, "status": "BOOK", "credit_debit_indicator": ind,
            "transaction_amount": {"currency": "EUR", "amount": str(amount)}, **kw}


def test_to_frame_signs_payees_and_skips():
    df, skipped = banksync.to_frame([
        tx("r1", "2026-11-02", "12.50", "DBIT", creditor={"name": "Albert Heijn"}, remittance_information=["AH 1234", "Utrecht"]),
        tx("r2", "2026-11-01", "3000", "CRDT", debtor={"name": "ACME BV"}, remittance_information=["Salaris"]),
        {**tx("r3", "2026-11-03", "5", "DBIT"), "status": "PDNG"},
        {**tx("r4", "2026-11-03", "5", "DBIT"), "transaction_amount": {"currency": "USD", "amount": "5"}},
    ], "EUR")
    assert skipped == 2
    assert list(df["ext_ref"]) == ["r2", "r1"]                      # oldest first
    assert list(df["amount"]) == [3000.0, -12.5]
    assert list(df["counterparty"]) == ["ACME BV", "Albert Heijn"]
    assert df["description"].iloc[1] == "AH 1234 Utrecht"


def test_pick_balance_prefers_closing_booked():
    got = banksync.pick_balance([
        {"balance_type": "ITAV", "balance_amount": {"currency": "EUR", "amount": "90"}},
        {"balance_type": "CLBD", "balance_amount": {"currency": "EUR", "amount": "100"}, "reference_date": "2026-11-10"},
    ], "EUR")
    assert got == (100.0, "2026-11-10")


# ---------------------------------------------------------------- linking

def session(sid, *accounts):
    return {"session_id": sid, "access": {"valid_until": "2027-05-10T12:00:00+00:00"},
            "accounts": [{"uid": uid, "account_id": {"iban": iban}, "name": name, "currency": "EUR"}
                         for uid, iban, name in accounts]}


def link(con, key, sess, now=NOW):
    c, api = client(key, {("POST", "/auth"): {"url": "https://bank.example/approve"}, ("POST", "/sessions"): sess})
    url = banksync.begin_link(con, c, "https://finance.example/import", {"name": "ING", "country": "NL",
                                                                         "maximum_consent_validity": 15552000}, now=now)
    assert url == "https://bank.example/approve"
    state = api.calls[0]["json"]["state"]
    return c, api, state


def test_link_flow_uses_a_single_use_state(con, key):
    c, api, state = link(con, key, session("s1", ("u1", "NL01INGB0001234567", "Betaalrekening")))
    valid_until = datetime.fromisoformat(api.calls[0]["json"]["access"]["valid_until"])
    assert timedelta(days=179) < valid_until - NOW <= timedelta(days=180)
    with pytest.raises(eb.EBError, match="wasn't started here"):
        banksync.finish_link(con, c, "code", "forged-state", now=NOW)
    link_id, n = banksync.finish_link(con, c, "code", state, now=NOW)
    assert n == 1
    assert api.calls[-1]["json"] == {"code": "code"}
    [lk] = banksync.links(con)
    assert lk["session_id"] == "s1" and lk["accounts"][0]["iban"] == "NL01INGB0001234567"
    with pytest.raises(eb.EBError, match="already used"):
        banksync.finish_link(con, c, "code", state, now=NOW)


def test_state_expires_after_an_hour(con, key):
    c, _, state = link(con, key, session("s1", ("u1", "NL01", "x")))
    with pytest.raises(eb.EBError):
        banksync.finish_link(con, c, "code", state, now=NOW + timedelta(hours=2))


def test_no_accounts_means_restricted_mode_and_closes_the_session(con, key):
    c, api, state = link(con, key, session("s1"))
    with pytest.raises(eb.EBError, match="Control Panel"):
        banksync.finish_link(con, c, "code", state, now=NOW)
    assert api.calls[-1]["method"] == "DELETE" and api.calls[-1]["path"] == "/sessions/s1"
    assert banksync.links(con) == []


def test_renewal_keeps_account_choices_and_closes_the_old_approval(con, key):
    aid = db.upsert_account(con, "ING joint")
    c, _, state = link(con, key, session("old", ("u1", "NL01INGB0001234567", "Betaal")))
    banksync.finish_link(con, c, "code", state, now=NOW)
    banksync.map_account(con, "u1", aid, date(2026, 10, 1))
    c, api, state = link(con, key, session("new", ("u9", "NL01INGB0001234567", "Betaal")))
    banksync.finish_link(con, c, "code2", state, now=NOW)
    [lk] = banksync.links(con)
    assert lk["session_id"] == "new"
    assert lk["accounts"][0]["account_id"] == aid and lk["accounts"][0]["start_date"] == "2026-10-01"
    assert {"method": "DELETE", "path": "/sessions/old"}.items() <= api.calls[-1].items()


# ---------------------------------------------------------------- syncing

def linked(loaded, key, routes):
    """The sample ING account, linked and mapped with its default start (the day after its last CSV row)."""
    aid = int(loaded.execute("SELECT id FROM accounts WHERE name='ING joint'").fetchone()[0])
    c, _, state = link(loaded, key, session("s1", ("u1", "NL01INGB0001234567", "Betaal")))
    banksync.finish_link(loaded, c, "code", state, now=NOW)
    start = banksync.default_start(loaded, aid, TODAY)
    banksync.map_account(loaded, "u1", aid, start)
    c, api = client(key, routes)
    return c, api, aid, start


def test_sync_starts_after_csv_history_and_is_idempotent(loaded, key):
    aid = int(loaded.execute("SELECT id FROM accounts WHERE name='ING joint'").fetchone()[0])
    last = date.fromisoformat(loaded.execute("SELECT MAX(date) FROM transactions WHERE account_id=?", (aid,)).fetchone()[0])
    d = lambda n: (last + timedelta(days=n)).isoformat()
    txs = {"transactions": [tx("old", d(0), "9.99", "DBIT", remittance_information=["already in the CSV"]),
                            tx("a", d(1), "25", "DBIT", creditor={"name": "Picnic"}, remittance_information=["Picnic order"]),
                            tx("b", d(2), "100", "CRDT", debtor={"name": "Friend"})], "continuation_key": None}
    bal = {"balances": [{"balance_type": "CLBD", "balance_amount": {"currency": "EUR", "amount": "1234.56"},
                         "reference_date": d(2)}]}
    routes = {("GET", "/accounts/u1/transactions"): txs, ("GET", "/accounts/u1/balances"): bal}
    c, api, aid, start = linked(loaded, key, routes)
    assert start == last + timedelta(days=1)
    before = loaded.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    [r] = banksync.sync_all(loaded, c, today=TODAY, now=NOW)
    assert (r.error, r.added) == ("", 2)                            # the row on the CSV's last day is left out
    assert api.calls[0]["params"]["date_from"] == start.isoformat()
    assert loaded.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before + 2
    assert loaded.execute("SELECT balance FROM balances WHERE account_id=? AND date=?", (aid, d(2))).fetchone()[0] == 1234.56

    [r] = banksync.sync_all(loaded, c, today=TODAY, now=NOW)        # same days again: nothing new
    assert r.added == 0
    assert loaded.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before + 2
    # later syncs re-read the last week (for late bookings), but never before the start date
    assert api.calls[-2]["params"]["date_from"] == max(start, TODAY - timedelta(days=banksync.OVERLAP_DAYS)).isoformat()


def test_rate_limit_is_explained_and_kept_on_the_account(loaded, key):
    c, _, _, _ = linked(loaded, key, {("GET", "/accounts/u1/transactions"): (429, {"error": "ASPSP_RATE_LIMIT_EXCEEDED"})})
    [r] = banksync.sync_all(loaded, c, today=TODAY, now=NOW)
    assert "6 hours" in r.error
    assert "6 hours" in banksync.links(loaded)[0]["accounts"][0]["last_error"]


def test_expired_approval_is_marked_and_not_called(loaded, key):
    c, api, _, _ = linked(loaded, key, {("GET", "/accounts/u1/transactions"): (401, {"error": "EXPIRED_SESSION"})})
    [r] = banksync.sync_all(loaded, c, today=TODAY, now=NOW)
    assert "Renew" in r.error and banksync.links(loaded)[0]["status"] == "EXPIRED"
    n = len(api.calls)
    banksync.sync_all(loaded, c, today=TODAY, now=NOW)
    assert len(api.calls) == n


def test_removing_a_connection_keeps_transactions(loaded, key):
    routes = {("GET", "/accounts/u1/transactions"): {"transactions": [tx("z", TODAY.isoformat(), "1", "DBIT")]},
              ("GET", "/accounts/u1/balances"): {"balances": []}}
    c, api, aid, _ = linked(loaded, key, routes)
    banksync.sync_all(loaded, c, today=TODAY, now=NOW)
    n = loaded.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (aid,)).fetchone()[0]
    banksync.remove_link(loaded, c, banksync.links(loaded)[0]["id"])
    assert api.calls[-1]["path"] == "/sessions/s1" and api.calls[-1]["method"] == "DELETE"
    assert banksync.links(loaded) == []
    assert loaded.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (aid,)).fetchone()[0] == n


def test_import_page_shows_connections_without_going_online(loaded, key, tmp_path, monkeypatch):
    import requests
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    now = datetime.now(UTC)
    c, _, state = link(loaded, key, session("s1", ("u1", "NL01INGB0001234567", "Betaalrekening")), now=now)
    banksync.finish_link(loaded, c, "code", state, now=now)
    p = tmp_path / "eb.pem"
    p.write_bytes(key[1])
    p.chmod(0o600)
    monkeypatch.setenv("FINAPP_EB_APP_ID", APP_ID)
    monkeypatch.setenv("FINAPP_EB_KEY_FILE", str(p))

    def offline(*a, **k):
        raise AssertionError("the Import page must not call the network just by opening")
    monkeypatch.setattr(requests.Session, "request", offline)
    st.cache_resource.clear()
    at = AppTest.from_string("from finapp.ui import common\nfrom finapp.ui.pages import import_statements\n"
                             "common.refresh()\nimport_statements.render()\n", default_timeout=120)
    at.run()
    assert not at.exception, at.exception[0].message if at.exception else ""
    assert any("ING" in m.value for m in at.markdown)
    assert any(b.label == "Sync now" for b in at.button)
