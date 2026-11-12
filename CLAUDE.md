# expat-finance

Household Finance: a Streamlit app for a US family in the Netherlands (budget, net worth, US and Dutch tax checks). Public repo, MIT. Runs as a desktop app by default, or self-hosted behind a login proxy (`FINAPP_MODE=server`). The owner's own copy runs on a home server and deploys automatically after each merge to `main`, so `main` must always work.

Read `CONTRIBUTING.md` for the layout, how to add a bank format, a tax year or a page, and style. Read `SECURITY.md` before touching anything about access, hosting, keys or network calls.

## Check your work

```
pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
```

Both must pass before you push. CI runs the tests on Linux and Windows with Python 3.11-3.13, plus `pip-audit` and a Docker build with a smoke test. Add or update tests for every behaviour change; money logic is tested in `finapp/`, and `tests/test_pages.py` renders every page on sample data.

## Rules

- **No real financial data, ever**: not in code, tests, fixtures, issues, PRs or screenshots. Make fake data (`sample_data/make_samples.py` shows how).
- **Don't weaken the access guard** (`finapp/hosting.py`, the guard in `app.py`): desktop mode stays localhost-only, server mode refuses requests without the proxy secret. Never add a way around either, and never suggest `FINAPP_ALLOW_REMOTE`.
- **The app goes online only when the user clicks.** No new background or automatic network calls without an explicit opt-in, and update README and SECURITY.md if you add one.
- **Secrets stay out of the database, backups and logs**: the Anthropic key lives in the OS credential store (`finapp/keystore.py`) or the container environment, the Enable Banking key in a file the user places.
- Tax and benefit figures live only in `finapp/rules.py` with a `source` note; mark anything unverified `verify: True`. Don't invent figures.
- Don't change the database schema without a step in `_migrate()` in `finapp/db.py` that upgrades existing databases in place; users' data must survive an upgrade.
- Keep Streamlit code in `finapp/ui/`, calculations in `finapp/` modules.
- Dependencies: add to `requirements.txt` and regenerate `requirements.lock` with the `uv pip compile` command at the top of that file; keep them few.

## Working from an issue

- One issue, one focused PR. Don't refactor unrelated code.
- If the issue is unclear or would need one of the rules above bent, comment with your question instead of guessing.
- PR description: what changed, how you tested it, and anything the reviewer should check by hand (a page to open, a figure to verify).
- Add a line under "Unreleased" in `CHANGELOG.md` for user-visible changes.
