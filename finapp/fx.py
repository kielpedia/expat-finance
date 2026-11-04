"""
Exchange rates from the European Central Bank reference series (free, no key).

Rates are stored as "units of currency per 1 EUR". The only network call the app
makes is this download; everything else stays on your machine.
"""
import io
import zipfile
from datetime import date, datetime

import pandas as pd
import requests

ECB_HIST = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"


def refresh(con, timeout=30) -> int:
    r = requests.get(ECB_HIST, timeout=timeout)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        df = pd.read_csv(z.open(name))
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    long = df.melt(id_vars="Date", var_name="currency", value_name="per_eur").dropna()
    long["currency"] = long["currency"].str.strip()
    long = long[long["per_eur"].astype(str).str.strip() != "N/A"]
    long["per_eur"] = long["per_eur"].astype(float)
    rows = list(long[["Date", "currency", "per_eur"]].itertuples(index=False, name=None))
    con.executemany("INSERT OR REPLACE INTO fx_rates(date, currency, per_eur) VALUES(?,?,?)", rows)
    con.commit()
    return len(rows)


def latest_date(con):
    row = con.execute("SELECT MAX(date) d FROM fx_rates").fetchone()
    return row["d"] if row else None


def table(con) -> pd.DataFrame:
    """Wide table: index=date, columns=currency, values=per_eur."""
    df = pd.read_sql("SELECT date, currency, per_eur FROM fx_rates", con, parse_dates=["date"])
    if df.empty:
        return df
    return df.pivot(index="date", columns="currency", values="per_eur").sort_index()


class Converter:
    """Converts between currencies using the nearest earlier ECB rate (weekends/holidays roll back)."""

    def __init__(self, con, fallback_usd_per_eur: float = 1.10):
        self.t = table(con)
        self.fallback = {"USD": fallback_usd_per_eur}

    def per_eur(self, ccy: str, on) -> float:
        if ccy == "EUR":
            return 1.0
        on = pd.Timestamp(on)
        if not self.t.empty and ccy in self.t:
            s = self.t[ccy].dropna()
            s = s[s.index <= on]
            if len(s):
                return float(s.iloc[-1])
            if len(self.t[ccy].dropna()):
                return float(self.t[ccy].dropna().iloc[0])
        if ccy in self.fallback:
            return self.fallback[ccy]
        raise KeyError(f"No rate for {ccy}. Refresh rates on the Settings page.")

    def convert(self, amount: float, src: str, dst: str, on) -> float:
        if src == dst:
            return amount
        eur = amount / self.per_eur(src, on)
        return eur * self.per_eur(dst, on)

    def to_eur_series(self, df: pd.DataFrame, amount_col="amount", ccy_col="currency", date_col="date") -> pd.Series:
        """Vectorised conversion for a frame of transactions."""
        out = pd.Series(index=df.index, dtype=float)
        for ccy, grp in df.groupby(ccy_col):
            if ccy == "EUR":
                out.loc[grp.index] = grp[amount_col]
                continue
            rates = grp[date_col].map(lambda d: self.per_eur(ccy, d))
            out.loc[grp.index] = grp[amount_col] / rates
        return out

    def usd_per_eur(self, on=None) -> float:
        return self.per_eur("USD", on or date.today())

    def year_end_usd_per_eur(self, year: int) -> float:
        return self.per_eur("USD", datetime(year, 12, 31))

    def year_avg_usd_per_eur(self, year: int) -> float:
        if self.t.empty or "USD" not in self.t:
            return self.fallback["USD"]
        s = self.t["USD"].dropna()
        s = s[s.index.year == year]
        return float(s.mean()) if len(s) else self.usd_per_eur()
