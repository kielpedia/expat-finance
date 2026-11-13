import json
import time

import pytest

from finapp import ai

ITEMS = [
    {"payee": "ALBERT HEIJN 1234", "send_name": "Albert Heijn", "send": True},
    {"payee": "NS GROEP", "send_name": "NS Groep", "send": True},
]
CATEGORIES = ["Groceries", "Transport"]
SUGG = {"suggestions": [
    {"id": 0, "payee": "Albert Heijn", "category": "groceries", "confidence": "high", "reason": "supermarket"},
    {"id": 1, "payee": "NS Groep", "category": "Transport", "confidence": "medium", "reason": "trains"},
]}
EXPECTED = {
    "ALBERT HEIJN 1234": {"category": "Groceries", "confidence": "high", "reason": "supermarket", "new": False},
    "NS GROEP": {"category": "Transport", "confidence": "medium", "reason": "trains", "new": False},
}


@pytest.mark.parametrize("reply", [
    "Here you go:\n```json\n" + json.dumps(SUGG, indent=2) + "\n```\nLet me know!",
    "Here you go:\n```\n" + json.dumps(SUGG) + "\n```",
    "Sure. " + json.dumps(SUGG) + " Hope that helps.",
], ids=["json-fence", "bare-fence", "no-fence"])
def test_parse_chat_reply_formats(reply):
    assert ai.parse_chat_reply(reply, ITEMS, CATEGORIES) == EXPECTED


def test_parse_chat_reply_skips_fence_without_json_object():
    reply = "```text\nnot this\n```\n```json\n" + json.dumps(SUGG) + "\n```"
    assert ai.parse_chat_reply(reply, ITEMS, CATEGORIES) == EXPECTED


@pytest.mark.parametrize("text, expected", [
    ("```json {\"a\": 1} ```", "{\"a\": 1}"),
    ("```{\"a\": {\"b\": 2}}```", "{\"a\": {\"b\": 2}}"),
    ("```json\n{} ``` {\"x\": 1}\n```", "{}"),
    ("```json {\"a\": 1}", None),
    ("```python\nx = {}\n```", None),
    ("no fences {}", None),
])
def test_fenced_json(text, expected):
    assert ai._fenced_json(text) == expected


@pytest.mark.parametrize("text", [
    "```json {" * 15000,
    "```json {} `" * 15000,
    "```{" + " ```" * 30000,
])
def test_parse_chat_reply_large_malformed_input_is_fast(text):
    start = time.perf_counter()
    with pytest.raises(ai.AIError):
        ai.parse_chat_reply(text, ITEMS, CATEGORIES)
    assert time.perf_counter() - start < 0.5
