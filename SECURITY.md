# Security

## How the app is meant to run

Household Finance has no login and no user accounts. That's safe only because nobody else can reach it. There are two supported ways to run it.

### Desktop (default)

A **single-user app on your own computer**:

- It listens on `localhost` only (`.streamlit/config.toml`), and refuses to show anything if it's started on a network address or reached through a reverse proxy.
- Your data is one SQLite file in your home folder (`~/FinanceApp/finance.db`), outside the code folder. It is **not encrypted**: keep it on an encrypted disk (BitLocker, FileVault, LUKS).
- An Anthropic API key, if you add one, is kept in your operating system's credential store, never in the database or its backups.
- The app goes online only when you click for it: ECB exchange rates, a share price (only the ticker is sent), and optional Claude categorisation (cleaned payee names only, shown to you before sending).

**Do not** open its port or put a desktop install behind a reverse proxy.

### Self-hosted (optional)

The container image runs with `FINAPP_MODE=server`, for a home server behind a reverse proxy that **does the login** (see *Self-hosting* in the README):

- The app won't open at all until `FINAPP_PROXY_SECRET` is set to 32 or more characters.
- The proxy sends that secret in the `X-Finapp-Proxy-Secret` header. Any request without it, such as one going straight to the server's port, is refused, so the proxy's login can't be skipped.
- Keep it on your home network or a VPN. The login is the proxy's, so its strength is the strength of that password.
- The database is still unencrypted, now on the server's disk and in its backups. The API key comes from the container's environment.
- Anyone who can read the server's environment or the proxy's configuration has the secret; protect both like a password.

`FINAPP_ALLOW_REMOTE=1` turns every check off. Don't use it; self-hosted mode replaces it.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → *Report a vulnerability*) rather than a public issue. Include what you found, how to reproduce it, and what an attacker could do with it. You'll get a reply within a week.

Never attach real bank statements, payslips or a database to a report or an issue. Use the generator in `sample_data/` to make fake data that shows the problem.
