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

### Security
- The app refuses to run on a network address or behind a reverse proxy.
- The Anthropic API key moved from the database to the OS credential store; older keys are migrated and removed from the database.
- Backups use SQLite's backup API and never contain an API key.
- New databases are created readable by the owner only (macOS/Linux).
- `defusedxml` protects Excel parsing; share-price lookups only accept ticker-shaped input.

### Changed
- Stock options work for any company and several at once; the ticker and company come from the import.
- Where you live (municipality) is set on Household & settings; enter your own rates if your municipality isn't built in.
