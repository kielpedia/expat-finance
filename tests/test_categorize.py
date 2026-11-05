import re

import pytest

from finapp import categorize

RULES = [(re.compile(p, re.I), c) for p, c in categorize.DEFAULT_RULES]


@pytest.mark.parametrize("text, cat", [
    # regressions from the review
    ("Saloon Bar", "Uncategorized"),
    ("Disney Plus", "Subscriptions"),
    ("Wise Payment to J. de Vries", "Uncategorized"),       # paying a person is spending, not a transfer
    ("Wise Europe SA Top-up", "Transfer"),
    ("Converted 100.00 EUR to 108.20 USD", "Transfer"),
    ("Vattenfall InCharge", "Transport: Fuel & charging"),
    ("Schwab Bank interest", "Income: Interest & dividends"),
    ("Belastingdienst Toeslagen ZORGTOESLAG 2026", "Income: Zorgtoeslag"),
    ("Belastingdienst KINDGEBONDEN BUDGET", "Income: Kindgebonden budget"),
    ("Belastingdienst kinderopvangtoeslag", "Income: Kinderopvangtoeslag"),
    ("Belastingdienst huurtoeslag", "Income: Huurtoeslag"),
    # everyday cases
    ("ACME BV Salaris oktober", "Income: Salary"),
    ("Albert Heijn 1432", "Food: Groceries"),
    ("Plus Supermarkt", "Food: Groceries"),
    ("Thuisbezorgd.nl", "Food: Dining & takeaway"),
    ("Uber Eats", "Food: Dining & takeaway"),
    ("Uber", "Transport: Public & taxi"),
    ("Vattenfall termijnbedrag", "Housing: Utilities"),
    ("Woonstichting Voorbeeld Huur 10-2026", "Housing: Rent"),
    ("Sociale Verzekeringsbank Kinderbijslag", "Income: Kinderbijslag"),
    ("AMEX EPAYMENT ACH PMT", "Transfer: Card payment"),
    ("DEGIRO Storting", "Transfer: Investing"),
    ("Amazon Prime", "Subscriptions"),
    ("Amazon.com", "Shopping"),
])
def test_default_rules(text, cat):
    assert categorize.categorize_text(text, RULES) == cat


@pytest.mark.parametrize("pattern, ok", [("albert heijn|jumbo", True), ("(unclosed", False), ("", False), ("x" * 400, False)])
def test_check_pattern(pattern, ok):
    assert (categorize.check_pattern(pattern) is None) == ok


def test_broken_saved_rule_is_skipped(con):
    con.execute("INSERT INTO cat_rules(pattern, category) VALUES('(broken', 'X')")
    con.execute("INSERT INTO cat_rules(pattern, category) VALUES('my gym', 'Health: Sport')")
    rules = categorize.load_rules(con)
    assert categorize.categorize_text("MY GYM Utrecht", rules) == "Health: Sport"


def test_hand_set_category_is_kept(loaded):
    con = loaded
    tid = con.execute("SELECT id FROM transactions WHERE counterparty='Picnic' LIMIT 1").fetchone()["id"]
    con.execute("UPDATE transactions SET category='Family: Kids & household', category_locked=1 WHERE id=?", (tid,))
    categorize.apply(con)
    assert con.execute("SELECT category FROM transactions WHERE id=?", (tid,)).fetchone()["category"] == "Family: Kids & household"
