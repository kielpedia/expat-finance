# Security

## How the app is meant to run

Household Finance is a **single-user app on your own computer**. It has no login and no user accounts. That's safe only because nobody else can reach it:

- It listens on `localhost` only (`.streamlit/config.toml`), and refuses to show anything if it's started on a network address or reached through a reverse proxy.
- Your data is one SQLite file in your home folder (`~/FinanceApp/finance.db`), outside the code folder. It is **not encrypted**: keep it on an encrypted disk (BitLocker, FileVault, LUKS).
- An Anthropic API key, if you add one, is kept in your operating system's credential store, never in the database or its backups.
- The app goes online only when you click for it: ECB exchange rates, a share price (only the ticker is sent), and optional Claude categorisation (cleaned payee names only, shown to you before sending).

**Do not** host it on a server, put it behind a reverse proxy, or open its port, even on your home network. `FINAPP_ALLOW_REMOTE=1` turns the guard off; only do that with real authentication in front of it.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → *Report a vulnerability*) rather than a public issue. Include what you found, how to reproduce it, and what an attacker could do with it. You'll get a reply within a week.

Never attach real bank statements, payslips or a database to a report or an issue. Use the generator in `sample_data/` to make fake data that shows the problem.
