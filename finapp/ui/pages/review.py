"""Review & categorise page."""

import pandas as pd
import streamlit as st

from finapp import ai, categorize, db, keystore
from finapp.ui import common
from finapp.ui.common import add_category_box, category_options, edited_rows, load_tx, rule_pattern, save_tx_edits, spread_column


def render():
    con, conv, profile, YEAR, USD_NOW = common.state()
    st.header("Review & categorise")
    st.markdown("Payments are grouped by payee, so you set the category once per payee instead of once per transaction. "
                "Tick **Rule** to apply it to future imports too.")
    tx = load_tx()
    if tx.empty:
        st.info("Nothing imported yet.")
        st.stop()
    REVIEW = ("Uncategorized", "Income: Other")
    f = st.columns([2, 2, 3])
    scope = f[0].radio("Show", ["Needs a category", "All payees"], horizontal=True)
    direction = f[1].radio("Money", ["Out", "In", "Both"], horizontal=True)
    q = f[2].text_input("Search payee")
    v = tx
    if direction == "Out":
        v = v[v["eur"] < 0]
    elif direction == "In":
        v = v[v["eur"] > 0]
    if q:
        v = v[(v["payee"] + " " + v["description"]).str.contains(q, case=False, regex=False)]

    g = (v.groupby("payee")
          .agg(n=("id", "size"), total=("eur", "sum"), typical=("eur", "median"),
               first=("date", "min"), last=("date", "max"),
               category=("category", lambda s: s.mode().iloc[0] if len(s.mode()) else "Uncategorized"),
               mixed=("category", "nunique"), needs=("category", lambda s: s.isin(REVIEW).sum()),
               example=("description", "first"))
          .reset_index())
    if scope == "Needs a category":
        g = g[g["needs"] > 0]
    g = g.sort_values("total", key=lambda s: s.abs(), ascending=False)
    if g.empty:
        st.success("Everything has a category. 🎉" if scope == "Needs a category" else "No matching payees.")
        st.stop()
    left = int(tx["category"].isin(REVIEW).sum())
    st.caption(f"{len(g)} payees shown · {left} transactions still need a category overall. Biggest amounts first.")

    # ---------------------------------------------------------- Claude suggestions
    ai_cfg = db.get_setting(con, "ai", {}) or {}
    api_key = keystore.get_key()
    with st.expander("✨ Suggest categories with Claude", expanded=bool(st.session_state.get("ai_sugg"))):
        if True:
            incl = bool(ai_cfg.get("include_amounts", False))
            items = ai.build_items(tx, REVIEW, incl)
            if not items:
                st.caption("Nothing needs a category right now.")
            else:
                st.markdown("**What will be sent:** only the payee names below (numbers, dates, card and account "
                            "references already removed), whether each is money in or out"
                            + (", a rounded typical amount" if incl else "")
                            + ", and your list of category names. No dates, balances, IBANs, account names, notes "
                            "or household details. Untick anything you'd rather not send.")
                prev = pd.DataFrame(items)
                cols = ["send", "send_name", "direction"] + (["send_amount"] if incl else []) + ["n", "flag", "payee"]
                ped = st.data_editor(prev[cols], hide_index=True, width="stretch", height=300, key="ai_prev",
                                     disabled=[c for c in cols if c != "send"],
                                     column_config={"send": st.column_config.CheckboxColumn("Send"),
                                                    "send_name": st.column_config.TextColumn("Sent as"),
                                                    "direction": "In/out",
                                                    "send_amount": st.column_config.NumberColumn("Amount sent (€, rounded)"),
                                                    "n": st.column_config.NumberColumn("# txns", help="Stays on your computer"),
                                                    "flag": "Note", "payee": None})
                for it, s in zip(items, ped["send"]):
                    it["send"] = bool(s)
                n_send = sum(it["send"] for it in items)
                chat_mode, api_mode = st.tabs(["Copy & paste (uses your Claude subscription)", "Claude API (automatic)"])
                with chat_mode:
                    st.markdown(f"**1.** Copy this text (copy icon, top right of the box) and paste it into a new chat at "
                                f"claude.ai. It holds the {n_send} ticked payees above and nothing else.")
                    st.code(ai.chat_prompt(items, category_options(), incl), language=None, height=180, wrap_lines=True)
                    st.markdown("**2.** Copy Claude's whole reply and paste it here.")
                    reply = st.text_area("Claude's reply", height=140, key="ai_reply",
                                         placeholder='```json\n{"suggestions": [ … ]}\n```', label_visibility="collapsed")
                    if st.button("Read suggestions", type="primary", disabled=not reply.strip()):
                        try:
                            st.session_state["ai_sugg"] = ai.parse_chat_reply(reply, items, category_options(), incl)
                        except ai.AIError as e:
                            st.error(str(e))
                            st.stop()
                        log = db.get_setting(con, "ai_log", []) or []
                        log.append(ai.log_entry(ai.payload_preview(items, incl), "claude.ai chat (copy & paste)", {}))
                        db.set_setting(con, "ai_log", log[-50:])
                        st.rerun()
                    st.caption("Tip: in claude.ai, Settings → Privacy controls whether chats can be used to improve "
                               "Anthropic's models. Turn it off if you'd rather they weren't.")
                with api_mode:
                    if not api_key:
                        st.markdown("Add an Anthropic API key on **Household & settings** to send straight from the app. "
                                    "API usage is billed separately from a Claude subscription.")
                with api_mode, st.popover("See the exact request text", disabled=not api_key):
                    st.json({"categories": category_options(), "payees": ai.payload_preview(items, incl)})
                if api_key and api_mode.button(f"Send {n_send} payee names to Claude", type="primary", disabled=n_send == 0):
                    model = ai_cfg.get("model") or ai.DEFAULT_MODEL
                    with st.spinner("Asking Claude…"):
                        try:
                            sugg, usage = ai.suggest(api_key, items, category_options(), incl, model)
                        except ai.AIError as e:
                            st.error(str(e))
                            st.stop()
                    log = db.get_setting(con, "ai_log", []) or []
                    log.append(ai.log_entry(ai.payload_preview(items, incl), model, usage))
                    db.set_setting(con, "ai_log", log[-50:])
                    st.session_state["ai_sugg"] = sugg
                    st.rerun()

        sugg = st.session_state.get("ai_sugg") or {}
        if sugg:
            st.divider()
            st.markdown("**Claude's suggestions.** Nothing is saved until you click Apply. Change any category you disagree with.")
            sdf = pd.DataFrame([{"payee": p, **s} for p, s in sugg.items()])
            sdf = sdf.merge(g[["payee", "n", "total"]], on="payee", how="left") if len(g) else sdf.assign(n=None, total=None)
            sdf["accept"] = sdf["confidence"].isin(["high", "medium"]) & (sdf["category"] != "Uncategorized")
            sdf["rule"] = True
            sdf["new"] = sdf["new"].map(lambda x: "new category" if x else "")
            sopts = sorted(set(category_options()) | set(sdf["category"]))
            sed = st.data_editor(sdf[["accept", "payee", "category", "confidence", "reason", "new", "rule", "n", "total"]],
                                 hide_index=True, width="stretch", key="ai_res",
                                 disabled=["payee", "confidence", "reason", "new", "n", "total"],
                                 column_config={"accept": st.column_config.CheckboxColumn("Apply"),
                                                "category": st.column_config.SelectboxColumn("Category", options=sopts, required=True),
                                                "rule": st.column_config.CheckboxColumn("Rule"),
                                                "total": st.column_config.NumberColumn("Total", format="euro"),
                                                "n": "#", "new": ""})
            acc = sed[sed["accept"]]
            b = st.columns([2, 1, 3])
            if b[0].button(f"Apply {len(acc)} suggestions", type="primary", disabled=acc.empty):
                ids_by_payee = tx.groupby("payee")["id"].apply(list)
                custom = set(db.get_setting(con, "custom_categories", []) or [])
                for _, r in acc.iterrows():
                    ids = [int(i) for i in ids_by_payee.get(r["payee"], [])]
                    con.executemany("UPDATE transactions SET category=?, category_locked=1 WHERE id=? "
                                    "AND category IN ('Uncategorized','Income: Other')", [(r["category"], i) for i in ids])
                    if r["rule"]:
                        pat = rule_pattern(r["payee"])
                        con.execute("DELETE FROM cat_rules WHERE pattern=?", (pat,))
                        con.execute("INSERT INTO cat_rules(pattern, category, priority) VALUES(?,?,50)", (pat, r["category"]))
                    custom.add(r["category"])
                con.commit()
                db.set_setting(con, "custom_categories", sorted(custom - set(categorize.all_categories(con))))
                st.session_state.pop("ai_sugg", None)
                st.toast(f"Applied {len(acc)} suggestions")
                st.rerun()
            if b[1].button("Discard"):
                st.session_state.pop("ai_sugg", None)
                st.rerun()

    g["rule"] = True
    g["category_new"] = g["category"]
    opts = category_options()
    ed = st.data_editor(
        g[["payee", "n", "total", "typical", "last", "category_new", "rule", "example", "category"]],
        hide_index=True, width="stretch", height=520, key="review_ed",
        disabled=["payee", "n", "total", "typical", "last", "example", "category"],
        column_config={
            "payee": st.column_config.TextColumn("Payee", width="medium"),
            "n": st.column_config.NumberColumn("#", help="Number of transactions"),
            "total": st.column_config.NumberColumn("Total", format="euro"),
            "typical": st.column_config.NumberColumn("Typical", format="euro"),
            "last": st.column_config.DateColumn("Last"),
            "category_new": st.column_config.SelectboxColumn("Category", options=opts, required=True, width="medium"),
            "rule": st.column_config.CheckboxColumn("Rule", help="Also categorise this payee automatically in future imports"),
            "example": st.column_config.TextColumn("Example description", width="large"),
            "category": None,
        })
    changed = ed[ed["category_new"] != ed["category"]]
    c = st.columns([2, 3])
    if c[0].button(f"Save {len(changed)} payee{'s' if len(changed) != 1 else ''}", type="primary", disabled=changed.empty):
        n_tx = n_rules = 0
        ids_by_payee = v.groupby("payee")["id"].apply(list)
        for _, r in changed.iterrows():
            ids = ids_by_payee.get(r["payee"], [])
            con.executemany("UPDATE transactions SET category=?, category_locked=1 WHERE id=?",
                            [(r["category_new"], int(i)) for i in ids])
            n_tx += len(ids)
            if r["rule"]:
                pat = rule_pattern(r["payee"])
                con.execute("DELETE FROM cat_rules WHERE pattern=?", (pat,))
                con.execute("INSERT INTO cat_rules(pattern, category, priority) VALUES(?,?,50)", (pat, r["category_new"]))
                n_rules += 1
        con.commit()
        st.toast(f"Updated {n_tx} transactions · saved {n_rules} rules")
        st.rerun()
    c[1].caption("Saved categories are locked, so re-running the automatic rules never overwrites them.")
    add_category_box("review")

    with st.expander("Payee has a mix of things (e.g. Bol.com, Amazon)? Categorise single transactions"):
        pick = st.selectbox("Payee", g["payee"].tolist())
        sub = v[v["payee"] == pick][["id", "date", "description", "eur", "category", "spread"]].copy()
        sub["date"] = sub["date"].dt.date
        sed = st.data_editor(sub, hide_index=True, width="stretch", key="review_sub",
                             disabled=["date", "description", "eur"],
                             column_config={"id": None, "eur": st.column_config.NumberColumn("EUR", format="euro"),
                                            "category": st.column_config.SelectboxColumn("Category", options=opts, required=True),
                                            "spread": spread_column(sub)})
        diff = edited_rows(sed, sub)
        if st.button(f"Save {len(diff)} changes", key="save_sub", disabled=diff.empty):
            save_tx_edits(diff, sub.set_index("id").assign(category=None))   # categories set here are locked
            st.rerun()
