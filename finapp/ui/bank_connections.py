"""Bank connections (Enable Banking) on the Import page: set-up, connecting a bank, matching accounts, Sync now."""
import os
from datetime import date, timedelta

import streamlit as st

from finapp import analysis, banksync, db, hosting
from finapp import enablebanking as eb

COUNTRIES = {"NL": "Netherlands", "BE": "Belgium", "DE": "Germany", "FR": "France", "LU": "Luxembourg", "IE": "Ireland",
             "ES": "Spain", "PT": "Portugal", "IT": "Italy", "AT": "Austria", "FI": "Finland", "SE": "Sweden",
             "DK": "Denmark", "NO": "Norway", "PL": "Poland", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania"}
NEW = "➕ New account"
SKIP = "Don't sync"
WARN_DAYS = 14


def _cfg(con) -> eb.Config:
    return eb.config(os.environ, db.get_setting(con, "enablebanking", {}) or {})


def _client(cfg: eb.Config):
    if not cfg.app_id:
        return None, "no application ID is set"
    try:
        return eb.Client.from_config(cfg), None
    except eb.EBError as e:
        return None, str(e)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _banks(_client: eb.Client, app_id: str, country: str) -> list[dict]:
    return sorted(_client.aspsps(country), key=lambda b: b["name"].lower())


def _flash(kind, msg):
    st.session_state["eb_flash"] = (kind, msg)


def _handle_redirect(con, client):
    """The bank sends the browser back to this page with ?code=…&state=… (or ?error=…)."""
    qp = st.query_params
    state = qp.get("state")
    if not state or not ("code" in qp or "error" in qp):
        return
    code, err, desc = qp.get("code"), qp.get("error"), qp.get("error_description")
    st.query_params.clear()                      # the code is single-use; don't leave it in the address bar
    if err:
        con.execute("DELETE FROM bank_auth_states WHERE state=?", (state,))
        con.commit()
        _flash("error", f"The bank approval didn't go through: {desc or err}.")
        return
    if client is None:
        _flash("error", "Came back from the bank, but Enable Banking isn't set up here, so the approval can't be used.")
        return
    try:
        with st.spinner("Finishing the connection…"):
            _, n = banksync.finish_link(con, client, code, state)
        _flash("success", f"Connected {n} account(s). Choose which of your accounts each one fills, then click **Sync now**.")
    except eb.EBError as e:
        _flash("error", str(e))


def _setup(con, cfg, problem, expanded):
    with st.expander("Set-up" + (" (needed)" if problem else ""), expanded=expanded):
        if problem:
            st.warning(f"Bank sync isn't ready: {problem}.")
        st.markdown(f"""
1. Create a free account at [enablebanking.com](https://enablebanking.com) and, in its Control Panel, register a
   **Production** application. Add this exact redirect URL: `{cfg.redirect_url}`. Your browser downloads the
   app's private key as `<app id>.pem`.
2. In the Control Panel, choose **Activate by linking accounts** and link **every** account you want to sync. In this free,
   restricted mode only linked accounts are shared, and it's for your own, non-commercial use.
3. Put the key file at `{cfg.key_file}`, readable only by you (on Linux/macOS: `chmod 600`). Self-hosted, mount it as the
   `enablebanking.pem` secret (see the README). It's never stored in the database or its backups.
4. Enter the application ID below (self-hosted: set `FINAPP_EB_APP_ID` instead).
""")
        saved = db.get_setting(con, "enablebanking", {}) or {}
        from_env = bool(os.environ.get("FINAPP_EB_APP_ID"))
        c = st.columns([3, 3, 1], vertical_alignment="bottom")
        app_id = c[0].text_input("Application ID", cfg.app_id, disabled=from_env,
                                 help="Set by FINAPP_EB_APP_ID" if from_env else "From Enable Banking's Control Panel")
        redir_env = bool(os.environ.get("FINAPP_EB_REDIRECT_URL"))
        redirect = c[1].text_input("Redirect URL", cfg.redirect_url, disabled=redir_env,
                                   help="Set by FINAPP_EB_REDIRECT_URL" if redir_env else
                                   "This app's Import page as your browser reaches it. Must match the one registered.")
        if c[2].button("Save", key="eb_save_cfg", disabled=from_env and redir_env):
            db.set_setting(con, "enablebanking", {**saved, "app_id": app_id.strip(), "redirect_url": redirect.strip()})
            st.rerun()
        if hosting.mode(os.environ) == "server" and "localhost" in cfg.redirect_url:
            st.warning("Self-hosted: set `FINAPP_EB_REDIRECT_URL` to this app's address through your proxy, "
                       "e.g. `https://finance.example.org/import`.")


def _start(con, client, cfg, aspsp):
    try:
        url = banksync.begin_link(con, client, cfg.redirect_url, aspsp)
    except eb.EBError as e:
        st.error(str(e))
        return
    st.session_state["eb_auth"] = (aspsp["name"], url)


def _account_row(con, link, acct, accts):
    names = [SKIP, NEW] + accts["name"].tolist()
    tail = (acct.get("iban") or "")[-4:]
    if acct["account_id"] and acct.get("account_name") in names:
        pre = names.index(acct["account_name"])
    else:
        pre = next((i + 2 for i, n in enumerate(accts["name"]) if tail and tail in n), 0)
    c = st.columns([3, 3, 2, 3])
    c[0].markdown(f"{acct.get('name') or 'Account'}  \n`•••• {tail or '?'}` · {acct.get('currency') or ''}")
    target = c[1].selectbox("Your account", names, index=pre, key=f"eb_t_{acct['uid']}", label_visibility="collapsed")
    new_name = None
    if target == NEW:
        new_name = c[1].text_input("Name", f"{link['aspsp_name']} {tail}".strip(), key=f"eb_n_{acct['uid']}")
    start = None
    if target != SKIP:
        if acct.get("start_date") and target == acct.get("account_name"):
            default = date.fromisoformat(acct["start_date"])
        elif target == NEW:
            default = date.today() - timedelta(days=banksync.HISTORY_DAYS)
        else:
            default = banksync.default_start(con, int(accts.loc[accts["name"] == target, "id"].iloc[0]))
        start = c[2].date_input("Sync from", default, key=f"eb_s_{acct['uid']}_{target}", label_visibility="collapsed",
                                help="Nothing before this date is fetched. Defaults to the day after your last "
                                     "transaction in that account, so CSV history isn't counted twice.")
    status = []
    if acct.get("last_synced"):
        status.append(f"Synced {acct['last_synced']}")
    if acct.get("last_error"):
        status.append(f"⚠️ {acct['last_error']}")
    c[3].caption(" · ".join(status) or ("Not synced yet" if acct["account_id"] else "Not synced"))
    return acct["uid"], target, new_name, start


def _link_box(con, client, cfg, link, accts):
    left = banksync.days_left(link)
    expired = link["status"] == "EXPIRED" or (left is not None and left < 0)
    with st.container(border=True):
        h = st.columns([5, 1, 1])
        until = (link.get("valid_until") or "")[:10]
        when = "approval has ended" if expired else (f"approval until {until} ({left} days)" if until else "")
        h[0].markdown(f"**{link['aspsp_name']}** · {COUNTRIES.get(link['aspsp_country'], link['aspsp_country'])} · {when}")
        if h[1].button("Renew", key=f"eb_renew_{link['id']}", help="Approve again at the bank; account choices are kept."):
            try:
                banks = _banks(client, cfg.app_id, link["aspsp_country"])
            except eb.EBError:
                banks = []
            aspsp = next((b for b in banks if b["name"] == link["aspsp_name"]),
                         {"name": link["aspsp_name"], "country": link["aspsp_country"]})
            _start(con, client, cfg, aspsp)
        with h[2].popover("Remove"):
            st.caption("Ends this approval at the bank and forgets it here. Transactions already synced stay.")
            if st.button("Remove connection", key=f"eb_rm_{link['id']}", type="primary"):
                banksync.remove_link(con, client, link["id"])
                st.rerun()
        if expired:
            st.warning("This bank approval has ended. Click **Renew** to approve again.")
        elif left is not None and left < WARN_DAYS:
            st.info(f"This approval ends in {left} days. Click **Renew** to keep syncing.")
        rows = [_account_row(con, link, a, accts) for a in link["accounts"]]
        if st.button("Save account choices", key=f"eb_map_{link['id']}"):
            for uid, target, new_name, start in rows:
                acct = next(a for a in link["accounts"] if a["uid"] == uid)
                if target == SKIP:
                    banksync.map_account(con, uid, None, None)
                    continue
                if target == NEW:
                    kind = "savings" if "spaar" in (acct.get("name") or "").lower() or "saving" in (acct.get("name") or "").lower() else "checking"
                    aid = db.upsert_account(con, new_name or acct.get("iban") or uid, link["aspsp_name"],
                                            link["aspsp_country"], acct.get("currency") or "EUR", kind)
                else:
                    aid = int(accts.loc[accts["name"] == target, "id"].iloc[0])
                banksync.map_account(con, uid, aid, start)
            _flash("success", "Saved. Click **Sync now** to fetch transactions.")
            st.rerun()


def render(con):
    cfg = _cfg(con)
    client, problem = _client(cfg)
    _handle_redirect(con, client)
    st.subheader("Bank connections (EU)")
    st.caption("Sync EU bank accounts directly through Enable Banking (PSD2, read-only) instead of exporting CSVs. "
               "Nothing is fetched until you click **Sync now**.")
    flash = st.session_state.pop("eb_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    links = banksync.links(con)
    _setup(con, cfg, problem, expanded=client is None)
    if client is None:
        return

    accts = analysis.accounts(con)
    for link in links:
        _link_box(con, client, cfg, link, accts)

    # The bank list is fetched only after a click: the app doesn't go online just because a page was opened.
    if not st.session_state.get("eb_pick"):
        if st.button("Connect another bank" if links else "Connect a bank", key="eb_pick_btn"):
            st.session_state["eb_pick"] = True
            st.rerun()
    else:
        c = st.columns([2, 4, 1, 1], vertical_alignment="bottom")
        country = c[0].selectbox("Country", list(COUNTRIES), format_func=lambda k: COUNTRIES[k], key="eb_country")
        try:
            banks = _banks(client, cfg.app_id, country)
        except eb.EBError as e:
            st.error(f"Couldn't load the list of banks: {e}")
            banks = []
        names = [b["name"] for b in banks]
        bank = c[1].selectbox("Bank", names, index=None, placeholder="Choose your bank", key="eb_bank")
        if c[2].button("Connect", disabled=not bank, key="eb_connect"):
            _start(con, client, cfg, next(b for b in banks if b["name"] == bank))
        if c[3].button("Cancel", key="eb_pick_cancel"):
            st.session_state.pop("eb_pick", None)
            st.session_state.pop("eb_auth", None)
            st.rerun()
    if "eb_auth" in st.session_state:
        name, url = st.session_state["eb_auth"]
        st.link_button(f"Continue to {name} to approve ↗", url, type="primary")
        st.caption("Your bank's approval page opens in a new tab and sends you back here afterwards. Finish within an hour, "
                   "and on a device that can reach this app (self-hosted on your home network: be on that network).")

    mapped = sum(1 for link in links for a in link["accounts"] if a["account_id"])
    if links and st.button("Sync now", type="primary", disabled=not mapped, key="eb_sync",
                           help=None if mapped else "Choose which of your accounts each bank account fills first."):
        with st.spinner("Fetching from your banks…"):
            results = banksync.sync_all(con, client)
        st.session_state.pop("eb_auth", None)
        for r in results:
            if r.error:
                st.error(f"**{r.bank} · {r.account}:** {r.error}")
            else:
                bal = f" Balance {r.balance:,.2f}." if r.balance is not None else ""
                st.success(f"**{r.bank} · {r.account}:** {r.added} new of {r.fetched} transactions.{bal}")
