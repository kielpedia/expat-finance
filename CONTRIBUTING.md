# Contributing

Thanks for helping. Issues and pull requests are welcome, especially new bank formats and tax-year updates.

**Never include real financial data** (statements, payslips, grant exports, screenshots with real numbers) in an issue, a pull request or a test. Make a fake file in the same format; `sample_data/make_samples.py` shows how.

## Setup

```
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt      # Windows: .venv\Scripts\pip
.venv/bin/pytest
.venv/bin/ruff check .
```

## Project layout

```
app.py                  entry point: page setup, the localhost guard, sidebar navigation
finapp/                 calculations, no Streamlit: importers, categorisation, analysis, tax rules, FX, options
finapp/ui/common.py     shared per-run state (database, rates, profile) and small UI helpers
finapp/ui/pages/        one module per page, each with a render() function
finapp/ui/*.py          larger page sections (box 3, payslips, what-if)
tests/                  pytest suite, including a test that renders every page on the sample data
```

## Adding a bank format

1. Add a `parse_<bank>(text)` function to `finapp/importers.py` that returns the standard columns (see the module docstring).
2. Teach `detect_format()` to recognise its header, and add it to `FORMATS` and `parse()`.
3. Add a small fake export to `tests/fixtures/` and a test in `tests/test_importers.py` that checks dates, amounts (sign!) and balances.
4. Add the bank to the table in the README.

## Adding a tax year

All tax and benefit figures live in `finapp/rules.py`, one block per year. Copy the latest block, update each figure from an official source, keep the `source` notes, and mark anything provisional with `verify: True`. Add or update tests in `tests/test_tax.py`.

## Adding a municipality

Copy the Utrecht block in `MUNICIPAL_CHARGES` (`finapp/benchmarks.py`), fill in that year's tariffs from the municipality's or tax collector's tariff page, and cite it in `source`.

## Style

- `ruff` must pass (it runs in CI).
- Keep money logic in `finapp/` modules with tests, and Streamlit code in `finapp/ui/`.
- A new page: add a module with `render()` to `finapp/ui/pages/`, register it in `PAGES` in `app.py`, and add its name to `tests/test_pages.py`.
- Plain, specific wording in the UI: say what the number is and where it came from.
