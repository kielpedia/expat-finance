import pytest

from finapp import expat, salary


def test_box3_couple():
    # 2026: exemption 59,357 pp, debt threshold 3,800 pp, bank 1.28%, other 6.00%, tax 36%
    r = expat.box3(bank=100_000, other=100_000, debts=10_000, partners=True, year=2026)
    debt_net = 10_000 - 7_600
    base = 200_000 - debt_net
    ret = 100_000 * 0.0128 + 100_000 * 0.06 - debt_net * 0.027
    assert r["base"] == pytest.approx(base)
    assert r["tax"] == pytest.approx((base - 2 * 59_357) * ret / base * 0.36)


def test_box3_below_exemption_is_zero():
    assert expat.box3(50_000, 0, 0, True, 2026)["tax"] == 0


def test_net_gross_round_trip():
    net = salary.net_from_gross(70_000, 2026)["net"]
    assert salary.gross_from_net(net, 2026)["gross"] == pytest.approx(70_000, abs=1)


def test_expat_ruling_capped_by_norm():
    p = {"me_salary_eur": 60_000, "expat_ruling": {"has": True, "who": "me", "first_applied_year": 2024}}
    r = expat.expat_ruling_check(p, 2026)
    assert r["allowance"] == pytest.approx(60_000 - 48_013)   # 30% would leave taxable pay below the norm
    assert r["capped_by_norm"]


def kid(age_years, ssn):
    return {"name": f"kid{age_years}", "birth": f"{2026 - age_years}-01-01", "has_ssn": ssn}


def test_ctc_counts_only_confirmed_ssn(conv):
    p = {"filing_status": "MFJ", "me_salary_eur": 80_000, "kids": [kid(5, True), kid(7, False), kid(3, None)]}
    r = expat.us_family_credits(p, conv, 2026)
    assert r["eligible_children"] == ["kid5"]
    assert r["children_without_ssn"] == ["kid7"]
    assert r["children_ssn_unknown"] == ["kid3"]
    assert r["actc_with_ftc"] == pytest.approx(1_700)
    assert r["actc_with_feie"] == 0


def test_fbar_sums_each_accounts_maximum(con, conv):
    from finapp import db
    a = db.upsert_account(con, "NL checking", "ING", "NL", "EUR", "checking")
    b = db.upsert_account(con, "NL savings", "Rabo", "NL", "EUR", "savings")
    db.upsert_account(con, "US checking", "Chase", "US", "USD", "checking")
    for aid, d, bal in [(a, "2026-02-01", 6000), (a, "2026-06-01", 1000), (b, "2026-03-01", 2000), (b, "2026-11-01", 3500)]:
        con.execute("INSERT INTO balances(account_id, date, balance) VALUES(?,?,?)", (aid, d, bal))
    con.commit()
    f = expat.fbar_8938(con, conv, 2026, "MFJ")
    assert f["aggregate_max_usd"] == pytest.approx((6000 + 3500) * 1.10)
    assert f["fbar_required"] and not f["f8938_required"]


def test_toeslagen_checks_are_separate(loaded, conv):
    """Receiving zorgtoeslag must not hide a missing kinderopvangtoeslag."""
    from finapp import analysis
    con = loaded
    aid = con.execute("SELECT id FROM accounts WHERE name='ING joint'").fetchone()["id"]
    con.execute("INSERT INTO transactions(account_id, date, amount, description, counterparty, category, hash) "
                "VALUES(?, '2026-03-20', 120, 'ZORGTOESLAG 2026', 'Belastingdienst Toeslagen', 'Income: Zorgtoeslag', 'z1')", (aid,))
    con.commit()
    tx = analysis.transactions(con, conv)
    prof = {"partner": True, "kids": [kid(4, True)], "me_salary_eur": 60_000, "partner_salary_eur": 30_000}
    titles = [o["title"] for o in expat.opportunities(con, conv, prof, tx, 2026)]
    assert any("kinderopvangtoeslag" in t for t in titles)


def test_kinderbijslag_starts_quarter_after_birth():
    r = expat.kinderbijslag_expected([{"name": "b", "birth": "2026-02-10"}], 2026)
    assert r["per_child"]["b"] == pytest.approx(0 + 295.07 + 298.40 + 298.40)
