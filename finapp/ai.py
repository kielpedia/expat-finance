"""
Category suggestions from Claude, with data minimisation.

What is sent: a list of cleaned payee names (numbers, dates, card refs, IBANs and e-mail
addresses removed), whether each is money in or out, and the category names you use.
Optionally a rounded typical amount. Nothing else: no dates, balances, account names,
notes, or household details. The caller shows the exact payload before anything is sent.
"""
import json
import re
from datetime import datetime

import requests

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"    # Anthropic's model for high-volume classification
BATCH = 120

IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{6,}\b", re.I)
EMAIL = re.compile(r"\S+@\S+")
LONGNUM = re.compile(r"\d{3,}")
COMPANY = re.compile(r"\b(b\.?v\.?|n\.?v\.?|v\.?o\.?f\.?|ltd|inc|llc|gmbh|corp|co)\b|"
                     r"store|shop|winkel|bank|verzeker|gemeente|belasting|stichting|vereniging|club|supermarkt|"
                     r"apotheek|school|opvang|markt|cafe|restaurant|hotel|garage|\.com|\.nl", re.I)
PERSONLIKE = re.compile(r"^(?:(?i:dhr|mevr|mw|hr|mr|mrs|ms)\.?\s+)?(?:[A-Z]\.\s*){1,3}[A-Za-z]|"
                        r"^[A-Z][a-z]+(?:\s+(?:van|de|der|den|ter|ten|het|la|le))*\s+[A-Z][a-z]+$")


def scrub(text: str) -> str:
    s = IBAN.sub(" ", text or "")
    s = EMAIL.sub(" ", s)
    s = LONGNUM.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()[:60]


def looks_like_person(name: str) -> bool:
    return bool(PERSONLIKE.search(name.strip())) and not COMPANY.search(name)


def build_items(tx, review_categories=("Uncategorized", "Income: Other"), include_amounts=False):
    """One row per payee that needs a category. Returns a list of dicts for preview and sending."""
    need = tx[tx["category"].isin(review_categories)]
    if need.empty:
        return []
    rows = []
    for payee, g in need.groupby("payee"):
        name = scrub(payee)
        if not name or name == "(no description)":
            continue
        out = (g["eur"] < 0).sum() >= (g["eur"] > 0).sum()
        item = {
            "payee": payee,
            "send_name": name,
            "direction": "out" if out else "in",
            "n": int(len(g)),
            "typical": float(g["eur"].median()),
            "send": not looks_like_person(name),
            "flag": "looks like a person" if looks_like_person(name) else "",
        }
        if include_amounts:
            item["send_amount"] = int(round(abs(item["typical"]), -1)) or int(round(abs(item["typical"])))
        rows.append(item)
    return sorted(rows, key=lambda r: abs(r["typical"]) * r["n"], reverse=True)


def payload_preview(items, include_amounts=False):
    """Exactly the per-payee records that would leave the computer."""
    out = []
    for i, it in enumerate([x for x in items if x["send"]]):
        rec = {"id": i, "payee": it["send_name"], "direction": it["direction"]}
        if include_amounts and "send_amount" in it:
            rec["typical_eur"] = it["send_amount"]
        out.append(rec)
    return out


SYSTEM = """You categorise bank transactions for a household that lives in the Netherlands and also has US accounts.
Payees can be Dutch or American merchants, Dutch government bodies, utilities, insurers or employers.
For each payee, pick the single best category from the list provided. Prefer an existing category.
Only if nothing fits, propose a new one in the form "Group: Sub", reusing an existing Group where possible.
If you genuinely cannot tell what the payee is, answer "Uncategorized" with confidence "low".
Treat payments to the household's own savings, brokerage or card accounts as "Transfer..." categories.
Keep each reason under 12 words."""

SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "category": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "category", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


class AIError(Exception):
    pass


def _call(api_key, model, records, categories, timeout=90):
    body = {
        "model": model,
        "max_tokens": 8000,
        "system": SYSTEM,
        "messages": [{
            "role": "user",
            "content": "Categories in use:\n" + "\n".join(categories) +
                       "\n\nPayees (JSON):\n" + json.dumps(records, ensure_ascii=False),
        }],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        r = requests.post(API_URL, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as e:
        raise AIError(f"Couldn't reach the Claude API: {e}") from e
    if r.status_code == 401:
        raise AIError("The API key was rejected. Check it on Household & settings.")
    if r.status_code == 429:
        raise AIError("Rate limited by the API. Wait a minute and try again.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            msg = r.text
        raise AIError(f"API error {r.status_code}: {msg[:300]}")
    data = r.json()
    if data.get("stop_reason") in ("refusal", "max_tokens"):
        raise AIError(f"The response was incomplete ({data['stop_reason']}). Try fewer payees at once.")
    text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), "")
    try:
        return json.loads(text)["suggestions"], data.get("usage", {})
    except (ValueError, KeyError) as e:
        raise AIError("Couldn't read the API response.") from e


def suggest(api_key, items, categories, include_amounts=False, model=DEFAULT_MODEL):
    """Send the ticked items in batches; return suggestions keyed by payee and token usage."""
    sendable = [x for x in items if x["send"]]
    records = payload_preview(items, include_amounts)
    by_id = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    for start in range(0, len(records), BATCH):
        chunk = records[start:start + BATCH]
        sugg, u = _call(api_key, model, chunk, categories)
        for k in usage:
            usage[k] += u.get(k, 0)
        for s in sugg:
            by_id[s["id"]] = s
    canon = {c.lower(): c for c in categories}
    out = {}
    for rec, it in zip(records, sendable):
        s = by_id.get(rec["id"])
        if not s:
            continue
        cat = canon.get(s["category"].strip().lower(), s["category"].strip())
        out[it["payee"]] = {"category": cat, "confidence": s["confidence"], "reason": s["reason"],
                            "new": cat.lower() not in canon}
    return out, usage


def log_entry(records, model, usage):
    return {"at": datetime.now().isoformat(timespec="seconds"), "model": model, "count": len(records),
            "payees": [r["payee"] for r in records], "usage": usage}


# ------------------------------------------------------------------ copy & paste mode (uses a Claude subscription)

def chat_prompt(items, categories, include_amounts=False) -> str:
    """Text to paste into a Claude chat. Contains exactly the same data the API mode would send."""
    records = payload_preview(items, include_amounts)
    return (
        SYSTEM
        + "\n\nReply with ONLY one JSON code block in this exact shape, one entry per payee, nothing else:\n"
        + '```json\n{"suggestions": [{"id": 0, "payee": "…", "category": "…", "confidence": "high|medium|low", "reason": "…"}]}\n```'
        + "\n\nCategories in use:\n" + "\n".join(categories)
        + "\n\nPayees (JSON):\n" + json.dumps(records, ensure_ascii=False)
    )


def parse_chat_reply(text, items, categories, include_amounts=False):
    """Parse Claude's pasted reply back into suggestions keyed by local payee. Matches by name, then by id."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    blob = m.group(1) if m else (text or "")
    if not m:
        start, end = blob.find("{"), blob.rfind("}")
        blob = blob[start:end + 1] if start >= 0 and end > start else ""
    try:
        sugg = json.loads(blob)["suggestions"]
    except (ValueError, KeyError, TypeError) as e:
        raise AIError("Couldn't find the suggestions in what you pasted. Copy Claude's whole reply, "
                      "including the ```json block.") from e
    sendable = [x for x in items if x["send"]]
    by_name = {}
    for it in sendable:
        by_name.setdefault(it["send_name"].lower(), it)
    canon = {c.lower(): c for c in categories}
    out = {}
    for s in sugg:
        if not isinstance(s, dict) or not s.get("category"):
            continue
        it = by_name.get(str(s.get("payee", "")).strip().lower())
        if it is None and isinstance(s.get("id"), int) and 0 <= s["id"] < len(sendable):
            it = sendable[s["id"]]
        if it is None:
            continue
        cat = canon.get(str(s["category"]).strip().lower(), str(s["category"]).strip())
        conf = str(s.get("confidence", "medium")).lower()
        out[it["payee"]] = {"category": cat, "confidence": conf if conf in ("high", "medium", "low") else "medium",
                            "reason": str(s.get("reason", ""))[:120], "new": cat.lower() not in canon}
    if not out:
        raise AIError("The reply didn't match any of the payees in the current list. "
                      "Make sure the ticks haven't changed since you copied the prompt.")
    return out
