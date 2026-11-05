import pytest

from finapp import options


def grants_csv(text):
    df = options.read_table(text)
    return options.parse_grants(df, options.guess_columns(list(df.columns)), "", 10)


def test_ticker_and_company_come_from_the_file():
    p = grants_csv("Grant ID,Symbol,Company Name,Grant Date,Grant Price,Granted Qty\nA1,AVGO,Broadcom,2025-03-01,180,500\n"
                   "M1,MSFT,Microsoft,2024-01-10,380,50\n")
    assert list(p["symbol"]) == ["AVGO", "MSFT"] and list(p["company"]) == ["Broadcom", "Microsoft"]
    assert not options.missing_symbol(p)


def test_missing_ticker_is_not_guessed(con):
    p = grants_csv("Grant Date,Exercise Price,Options Granted\n2022-05-01,25.00,1000\n")
    assert options.missing_symbol(p)
    with pytest.raises(ValueError):
        options.save_grants(con, p)


def test_unadjusted_file_is_adjusted_for_split(con):
    # AVGO split 10-for-1 on 2024-07-15 (in the built-in list)
    p = grants_csv("Symbol,Grant Date,Grant Price,Granted Qty\nAVGO,2023-03-01,600,100\nAVGO,2025-03-01,180,500\n")
    options.save_grants(con, p, adjusted={"AVGO": False})
    g = options.grants(con).set_index("grant_date")
    assert g.loc["2023-03-01", "strike"] == pytest.approx(60) and g.loc["2023-03-01", "quantity"] == pytest.approx(1000)
    assert g.loc["2025-03-01", "strike"] == pytest.approx(180)
    assert options.split_candidates(con).empty
    # importing the same file again doesn't double-adjust
    options.save_grants(con, p, adjusted={"AVGO": False})
    assert options.grants(con).set_index("grant_date").loc["2023-03-01", "strike"] == pytest.approx(60)


def test_value_per_company(con):
    p = grants_csv("Grant ID,Symbol,Grant Date,Grant Price,Granted Qty,Exercised\nA,AVGO,2025-03-01,180,500,0\n"
                   "B,MSFT,2024-01-10,380,50,10\nC,ACME,2024-01-10,5,100,0\n")
    options.save_grants(con, p)
    v = options.value(options.grants(con), {"AVGO": 300.0, "MSFT": 420.0})
    s = options.summary(v, 0.5)
    assert s["intrinsic"] == pytest.approx(120 * 500 + 40 * 40)
    assert s["unpriced"] == 1                                    # ACME has no price yet: counted as zero
    assert options.sync_networth(con, {"AVGO": 300.0, "MSFT": 420.0}, 0.5, True) == pytest.approx(s["intrinsic"] / 2)


@pytest.mark.parametrize("raw, want", [("msft", "MSFT"), ("BRK.B", "BRK.B"), ("../etc", ""), ("AAPL?x=1", ""), ("", "")])
def test_clean_symbol(raw, want):
    assert options.clean_symbol(raw) == want
