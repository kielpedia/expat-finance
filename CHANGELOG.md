# Changelog

## Unreleased

### Fixed
- Refunds and reimbursements no longer count as income; they reduce spending in their category.
- Overview averages count spread payments as their monthly share and leave out the unfinished current month.
- Toeslagen are split into zorgtoeslag, huurtoeslag, kindgebonden budget and kinderopvangtoeslag, so a missing benefit is flagged even when another one arrives.
- Categorisation: Wise payments to other people are spending (only top-ups and conversions are transfers); "Disney Plus" is a subscription; "loon" only matches as a word; bank interest is income; EV charging isn't a utility bill.
- US checks warn when exchange rates aren't loaded or are out of date, instead of silently using a fixed rate.
- A child's SSN starts as "Not sure" and only a confirmed SSN counts towards the Child Tax Credit estimate.
- Import errors other than duplicates are no longer swallowed.
- An invalid categorisation rule can't be saved, and a broken saved rule no longer stops the app.

### Added
- EU bank sync through Enable Banking (PSD2, read-only): connect a bank on the Import page, choose which account each bank account fills, and click **Sync now**. Syncs start after your last imported transaction and never add a row twice; approvals can be renewed without losing account choices. Manual only for now.
- Restore from a backup in Settings: upload a `.db` file, see what's in it, and replace your data. The file is checked first, and a copy of your current data is kept next to the database.
- Optional self-hosted mode: a container image (`ghcr.io/kielpedia/expat-finance`) that only opens behind a reverse proxy that does the login and sends a shared secret header. Desktop mode is unchanged and still the default.

### Security
- The Enable Banking private key is read from a file or mounted secret, never stored in the database or backups, and refused on macOS/Linux if other users can read it. Bank approvals use a single-use `state` that expires after an hour.
- The app refuses to run on a network address or behind a reverse proxy (desktop mode).
- Self-hosted mode refuses every request that doesn't carry `FINAPP_PROXY_SECRET`, and won't start without a 32+ character secret.
- The Anthropic API key moved from the database to the OS credential store; older keys are migrated and removed from the database.
- Backups use SQLite's backup API and never contain an API key.
- New databases are created readable by the owner only (macOS/Linux).
- `defusedxml` protects Excel parsing; share-price lookups only accept ticker-shaped input.

### Changed
- README has screenshots and a short demo GIF, all from the sample data.
- The average line's label on the cash-flow chart has a background so it stays readable over a bar.
- Each page has its own address (`/budget`, `/settings`, …), so reloading or bookmarking keeps you on the same page.
- Overview cash flow has a period picker: current month, last month, year to date, 1 year, 3 years, all time (remembered between visits).
- The Overview month picker lists the newest month first.
- The 1,800-line `app.py` is split into one module per page under `finapp/ui/pages/`, with shared state in `finapp/ui/common.py`. Every page renders exactly as before (checked element by element), and a new test renders each page in CI.
- Stock options work for any company and several at once; the ticker and company come from the import.
- Where you live (municipality) is set on Household & settings; enter your own rates if your municipality isn't built in.
