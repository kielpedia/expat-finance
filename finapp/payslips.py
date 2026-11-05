"""
Payslip (loonstrook) import from PDF.

Reads the PDF's text locally (no OCR, nothing sent anywhere) and looks for the usual lines in Dutch
and English, with Workday's layout in mind: a summary with 'Huidig' (this period) and 'Lopend jaar'
(year to date) columns, then earnings and deduction tables. Every value found is shown with the line
it came from, for you to confirm or correct before it's saved.
"""
import io
import re

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS payslips (
    id INTEGER PRIMARY KEY,
    person TEXT DEFAULT 'me',            -- me / partner
    employer TEXT,
    period TEXT NOT NULL,                -- YYYY-MM
    gross REAL, base_salary REAL, holiday REAL, pension REAL, wage_tax REAL,
    et_allowance REAL, taxable REAL, net REAL,
    ytd_gross REAL, ytd_pension REAL, ytd_wage_tax REAL, ytd_et_allowance REAL, ytd_taxable REAL, ytd_net REAL,
    filename TEXT,
    added TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person, employer, period)
);
"""

FIELDS = {
    # key: (label shown, patterns tried in order; earlier = preferred)
    "gross":        ("Gross pay (total)", [r"bruto\s*loon\b", r"brutoloon", r"totaal\s+bruto", r"gross\s+pay", r"total\s+gross",
                                           r"gross\s+earnings", r"bruto\s+salaris\s+totaal"]),
    "base_salary":  ("Base salary", [r"^\s*salaris\b", r"maandsalaris", r"basis\s*salaris", r"base\s+salary", r"monthly\s+salary",
                                     r"regular\s+salary", r"^\s*salary\b"]),
    "holiday":      ("Holiday pay (vakantiegeld)", [r"vakantiegeld", r"vakantietoeslag", r"holiday\s+(allowance|pay)",
                                                    r"vacation\s+(allowance|pay)"]),
    "pension":      ("Pension contribution (employee)", [r"pensioen\w*\s*(premie)?\s*(werknemer|wn)", r"pensioenpremie", r"^\s*pensioen\b",
                                                         r"pension\s+(contribution|premium)?\s*(employee|ee)?", r"pension"]),
    "wage_tax":     ("Wage tax withheld (loonheffing)", [r"loonheffing", r"loonbelasting", r"wage\s+tax", r"payroll\s+tax",
                                                         r"income\s+tax"]),
    "et_allowance": ("Expat-ruling allowance (tax-free)", [r"30\s*%[\s-]*(regeling|ruling|vergoeding)?", r"et[\s-]*vergoeding",
                                                           r"extraterritori\w*", r"expat\s+(ruling|allowance)", r"onbelaste\s+vergoeding",
                                                           r"tax[\s-]*free\s+allowance"]),
    "taxable":      ("Taxable wage (loon voor de loonheffing)", [r"loon\s+voor\s+de\s+loonheffing", r"fiscaal\s+loon", r"loon\s*lb\s*/?\s*pv",
                                                                 r"belastbaar\s+loon", r"heffingsloon", r"tabelloon", r"taxable\s+(wage|income|pay)"]),
    "net":          ("Net pay", [r"netto\s*loon", r"nettoloon", r"netto\s+uitbetaling", r"uit\s+te\s+betalen", r"te\s+betalen",
                                 r"net\s+pay", r"net\s+salary", r"take[\s-]*home"]),
}
YTD_KEYS = ["gross", "pension", "wage_tax", "et_allowance", "taxable", "net"]

MONTHS = {m: i for i, ms in enumerate([
    ("januari", "january", "jan"), ("februari", "february", "feb"), ("maart", "march", "mrt", "mar"),
    ("april", "apr"), ("mei", "may"), ("juni", "june", "jun"), ("juli", "july", "jul"), ("augustus", "august", "aug"),
    ("september", "sep", "sept"), ("oktober", "october", "okt", "oct"), ("november", "nov"), ("december", "dec")], 1)
    for m in ms}

MONEY = re.compile(r"(?<![\d.,])-?\s?€?\s?\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})-?(?![\d])|(?<![\d.,])-?\s?€?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})-?(?![\d])"
                   r"|(?<![\d.,])-?\s?€?\s?\d+[.,]\d{2}-?(?![\d])")
# dates, including ones glued to the next number in extracted text ("31-10-2026173,33")
DATE = re.compile(r"(?<!\d)\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|(?<!\d)\d{4}-\d{2}-\d{2}|(?<!\d)\d{1,2}[-/.]\d{1,2}[-/.]\d{2}(?!\d)")


def setup(con):
    con.executescript(SCHEMA)
    con.commit()


def to_number(tok: str) -> float:
    t = tok.replace("€", "").replace(" ", "").strip()
    neg = t.startswith("-") or t.endswith("-")
    t = t.strip("-")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{2}$", t) else t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return float("nan")
    return -v if neg else v


def amounts(line: str) -> list[float]:
    clean = DATE.sub(" ", line)
    clean = re.sub(r"\b\d{1,3}(?:[.,]\d{1,2})?\s*%", " ", clean)   # drop percentages like 30% or 4,5%
    return [v for v in (to_number(m.group(0)) for m in MONEY.finditer(clean)) if pd.notna(v)]


def pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:  # noqa: BLE001
            raise ValueError("This PDF is password-protected. Open it, save an unprotected copy, and import that.") from e
    pages = []
    for p in reader.pages:
        try:   # layout mode keeps a table row (label + amounts) on one line
            pages.append(p.extract_text(extraction_mode="layout") or "")
        except Exception:  # noqa: BLE001
            pages.append(p.extract_text() or "")
    text = "\n".join(pages)
    if len(text.strip()) < 40:
        raise ValueError("No text found in this PDF; it may be a scanned image. A text PDF from Workday works best.")
    return text


def find_period(text: str):
    """YYYY-MM of the pay period: period end / payment date / a month name with a year."""
    low = text.lower()
    for label in (r"periode\s*(eind|einde|tot|t/m)\w*", r"pay\s+period\s+end", r"period\s+end", r"betaaldatum", r"uitbetalingsdatum",
                  r"check\s+date", r"payment\s+date", r"pay\s+date", r"datum"):
        m = re.search(label + r"[^\n\d]{0,20}(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2})", low)
        if m:
            d = pd.to_datetime(m.group(m.lastindex), dayfirst=not re.match(r"\d{4}-", m.group(m.lastindex)), errors="coerce")
            if pd.notna(d):
                return f"{d:%Y-%m}"
    m = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(20\d{2})\b", low)
    if m:
        return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}"
    m = re.search(r"periode\s*:?\s*(\d{1,2})\s*[-/]\s*(20\d{2})", low)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    d = DATE.search(text)
    if d:
        dd = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
        if pd.notna(dd):
            return f"{dd:%Y-%m}"
    return None


def find_employer(text: str) -> str:
    low = text.lower()
    m = re.search(r"(werkgever|employer|company|bedrijf)\s*[:\-]?\s*([^\n]{2,60})", low)
    if m:
        return text[m.start(2):m.end(2)].strip().title()
    first = next((ln.strip() for ln in text.splitlines() if ln.strip() and not re.search(r"\d", ln)), "")
    return first[:60]


def parse(text: str) -> dict:
    """Extract fields. Each value comes with the line it was read from, so it can be checked."""
    lines = [ln for ln in (x.strip() for x in text.splitlines()) if ln]
    found = {}
    for key, (_label, patterns) in FIELDS.items():
        for pat in patterns:
            rx = re.compile(pat, re.I)
            for line in lines:
                if not rx.search(line):
                    continue
                # skip header-ish lines and percent-only lines
                nums = amounts(rx.split(line, maxsplit=1)[-1] if rx.split(line, maxsplit=1)[-1].strip() else line)
                if not nums:
                    continue
                cur = nums[-2] if len(nums) >= 2 else nums[0]
                ytd = nums[-1] if len(nums) >= 2 else None
                found[key] = {"value": abs(cur), "ytd": abs(ytd) if ytd is not None else None, "line": line[:140]}
                break
            if key in found:
                break
    # gross missing but base salary there: use the base salary plus holiday pay as gross for the period
    if "gross" not in found and "base_salary" in found:
        found["gross"] = {"value": found["base_salary"]["value"] + found.get("holiday", {}).get("value", 0),
                          "ytd": None, "line": "(base salary + holiday pay)"}
    return {"period": find_period(text), "employer": find_employer(text), "fields": found}


def check(parsed: dict) -> list[str]:
    """Sanity checks shown next to the review form."""
    f = {k: v["value"] for k, v in parsed["fields"].items()}
    notes = []
    if "gross" in f and "net" in f:
        if f["net"] > f["gross"] + f.get("et_allowance", 0) + 1:
            notes.append("Net pay is higher than gross pay, so one of the two was probably read from the wrong line.")
        implied = f["gross"] - f.get("pension", 0) - f.get("wage_tax", 0)
        if "wage_tax" in f and abs(implied - f["net"]) > max(50, 0.03 * f["gross"]):
            notes.append(f"Gross − pension − wage tax = €{implied:,.2f}, but net pay is €{f['net']:,.2f}. Other deductions "
                         "or allowances on the payslip can explain a gap; otherwise check the lines below.")
    if not parsed.get("period"):
        notes.append("Couldn't find the pay period; set the month below.")
    return notes


def save(con, person, employer, period, values: dict, filename=""):
    cols = ["gross", "base_salary", "holiday", "pension", "wage_tax", "et_allowance", "taxable", "net",
            "ytd_gross", "ytd_pension", "ytd_wage_tax", "ytd_et_allowance", "ytd_taxable", "ytd_net"]
    vals = [values.get(c) for c in cols]
    con.execute(f"INSERT INTO payslips(person, employer, period, {', '.join(cols)}, filename) VALUES(?,?,?,{','.join('?' * len(cols))},?) "
                f"ON CONFLICT(person, employer, period) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}, filename=excluded.filename",
                (person, employer, period, *vals, filename))
    con.commit()


def all_payslips(con) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM payslips ORDER BY period DESC, person", con)


def yearly(con, person: str, year: int) -> dict | None:
    """Best yearly figures for a person: from the latest payslip's year-to-date totals if present,
    else from the months on file. 'projected' scales the year so far to 12 months."""
    p = pd.read_sql("SELECT * FROM payslips WHERE person=? AND substr(period,1,4)=? ORDER BY period", con,
                    params=(person, str(year)))
    if p.empty:
        return None
    last = p.iloc[-1]
    month = int(last["period"][5:7])
    out = {"months_on_file": len(p), "latest": last["period"], "source": ""}
    if pd.notna(last.get("ytd_gross")) and last["ytd_gross"]:
        base = {k: (last.get(f"ytd_{k}") if pd.notna(last.get(f"ytd_{k}")) else None) for k in YTD_KEYS}
        out["source"] = f"year-to-date totals on the {last['period']} payslip ({month} months)"
    else:
        base = {k: p[k].sum() if p[k].notna().any() else None for k in YTD_KEYS}
        month = len(p)
        out["source"] = f"{len(p)} payslip(s) on file"
    out["ytd"] = base
    out["months"] = month
    # project to a full year; holiday pay is often reserved monthly and paid in May, so a projection made
    # before May adds the reserve: 8% of the year's regular pay, unless it already shows up on the slips
    factor = 12 / month if month else 1
    proj = {k: (v * factor if v is not None else None) for k, v in base.items()}
    paid_holiday = p["holiday"].fillna(0).max() > (p["base_salary"].fillna(0).max() * 0.3)
    if month < 5 and not paid_holiday and proj.get("gross"):
        proj["gross"] *= 1.08
        out["holiday_added"] = True
    out["projected"] = proj
    if proj.get("gross") and base.get("pension"):
        out["pension_pct"] = (base["pension"] / base["gross"]) if base.get("gross") else None
    return out
