# expat-finance

[![CI](https://github.com/kielpedia/expat-finance/actions/workflows/ci.yml/badge.svg)](https://github.com/kielpedia/expat-finance/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Household Finance** is a private, local app for a US family living in the Netherlands. Import bank statements from both countries, see your budget and net worth in EUR with USD alongside, and get a checklist of US and Dutch tax and benefit items that apply to you.

Everything stays on your computer. The app only goes online when you click for it: to download ECB exchange rates, to fetch a share price for your stock options (only the ticker is sent), or, if you opt in, to ask Claude to suggest categories (see below).

## Start it

You need Python 3.11 or newer.

- **macOS / Linux:** double-click or run `./run.sh`
- **Windows:** double-click `run.bat`

The first start creates a virtual environment and installs the packages in `requirements.txt` (Streamlit, pandas, Plotly, requests, openpyxl, pypdf, keyring, defusedxml). After that it opens at http://localhost:8501, reachable only from your machine.

**Don't expose it to a network.** The app has no login: anyone who can reach it sees all your data. It refuses to open when it's listening on a network address or reached through a reverse proxy. See [SECURITY.md](SECURITY.md).

## First-time setup

1. **Household & settings:** where you live, filing status, salaries, children (with dates of birth and whether they have an SSN), expat ruling. Then click **Download / refresh ECB rates**.
2. **Import statements:** drop in CSV exports. The format is detected automatically; pick or create the account to import into.
3. **Accounts & net worth:** set each account's type and the bank's country. Add balance snapshots for brokerage, 401(k)/IRA, pension, mortgage and loans.
4. **Transactions:** fix any categories. Hand-edited categories are kept, and you can add rules so future imports sort themselves.

## Supported imports

| Source | Format |
|---|---|
| ING | CSV (comma or semicolon) |
| Rabobank | CSV |
| ABN AMRO | TXT (tab-separated) |
| bunq | CSV |
| Wise | CSV, one per currency balance |
| American Express | CSV |
| Chase, BofA, Citi, Capital One, Schwab, Fidelity and most others | CSV, columns auto-mapped with a manual mapper as backup |

Re-importing overlapping periods is safe: duplicates are skipped.

Try it with fake data: `python sample_data/make_samples.py`, then import the four CSVs in `sample_data/`.

## What it checks

**Budget & health:** monthly cash flow, spending by category, budget targets, recurring payments, savings rate, emergency-fund months, net worth over time, currency exposure.

**US:** FBAR and Form 8938 thresholds from each foreign account's maximum balance, refundable Child Tax Credit under FTC vs. FEIE, missing SSNs, PFIC exposure (EU funds/ETFs), expat-ruling allowance being US-taxable.

**Netherlands:** Box 3 estimate from 1 January balances, kinderbijslag expected vs. received, kindgebonden budget estimate, childcare costs without kinderopvangtoeslag, expat-ruling salary norm and the 2027 change to 27%, and your municipality's charges (waste, sewer, OZB) against what you paid. Utrecht's tariffs are built in; for another municipality, enter the rates from your assessment, or add a block to `MUNICIPAL_CHARGES` in `finapp/benchmarks.py`.

**Payslips:** read Dutch and English payslip PDFs (Workday layout in mind) locally, to check gross, wage tax and expat-ruling allowance against what arrived in your account.

**Stock options:** import your grant history (E*TRADE's Excel export or any CSV); the ticker and company are read from the file, and grants from several companies sit side by side. Values each grant at today's price, handles stock splits (a list of recent large splits is built in, and you can add your own), and records exercises for the tax year.

## Spreading yearly costs

A payment that covers several months (a yearly school fee, annual insurance, an energy settlement) can be set to **Spread** over N months on the Transactions page, the Budget category panel or the Overview month detail. *Next N months* starts in the month it was paid; *Past N months* ends in it. The Budget then counts an equal share each month, and the "Spread payments" box shows the total to set aside per month. Balances and net worth always use the real payment, and the Overview cash-flow chart has a *Spread costs* switch to see actual cash instead.

## What if…

At the bottom of the Budget page you can try changes against your average month: remove or change an income source (per payer), cut or drop a spending category or a single payee, or add a new income or expense. It shows the new monthly net, savings rate, emergency-fund months (or how long savings last if you'd run a shortfall) and a step-by-step chart. Your scenario is kept until you clear it; your data isn't changed.

## Claude category suggestions (optional)

On **Review & categorise**, *Suggest categories with Claude* sends the payees that still need a category to the Claude API and brings back suggestions for you to accept or change. You need your own API key from console.anthropic.com. Enter it on Household & settings: it's kept in your operating system's credential store (Windows Credential Manager, macOS Keychain or the Linux keyring), never in the database. Or set the `ANTHROPIC_API_KEY` environment variable.

Only cleaned payee names, money in or out, and your category names are sent. Numbers, dates, card references, IBANs and e-mail addresses are removed first; amounts are off unless you switch them on. Payees that look like a person's name are unticked by default. You see the exact request before sending, and every request is logged under Household & settings.

## Keeping it current

All tax figures live in `finapp/rules.py`, one block per year, with sources. Add a new year's block each January. Some 2026 figures are provisional (Box 3 savings/debt rates) or only from secondary sources (kindgebonden budget age supplements); they're marked `verify`.

## Your data

One SQLite file at `~/FinanceApp/finance.db` (set `FINAPP_DB` to move it). It's not encrypted, so keep it on an encrypted disk (FileVault/BitLocker) and back it up from the Settings page.

## Not tax advice

The app estimates and flags. A cross-border US/NL tax adviser should confirm anything you file.

## Development

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/pytest            # tests
.venv/bin/ruff check .      # lint
```

Tests use the fake data in `sample_data/` and never touch your own database. See [CONTRIBUTING.md](CONTRIBUTING.md) for adding a bank format or a new tax year.

## License

[MIT](LICENSE)
