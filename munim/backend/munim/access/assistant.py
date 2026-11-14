"""The merchant asks; Munim answers from the shop's own snapshot, or opens an issue and runs the same investigation.

    answer       written by the `merchant` LLM task (or the offline rules) from lens.snapshot() only, then guarded.
    investigate  runner.start_case(channel="app"): the same graph the team uses. Fixes that move no money (pickup,
                 KYC link) can complete on their own; money actions always wait for a teammate (firewall `limits`).

The shop always comes from the login. A shop id typed in the message is never used to look anything up.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from munim import config
from munim.access import guard, lens
from munim.db.conn import audit, one, tx

MAX_ASKS = 20               # per shop, per window
WINDOW_S = 10 * 60
MAX_TEXT = 500
_ASKS: dict[str, deque] = defaultdict(deque)


class TooMany(Exception):
    pass


def _rate(merchant_id: str) -> None:
    now = time.monotonic()
    q = _ASKS[merchant_id]
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    if len(q) >= MAX_ASKS:
        raise TooMany("Too many questions in a short time. Please wait a few minutes.")
    q.append(now)


def _language(question: str, shop: dict) -> str:
    from munim.llm.mock import detect_language
    q = detect_language(question)
    return q if q != "en" else (shop.get("language") or "en")


async def ask(merchant_id: str, text: str) -> dict[str, Any]:
    """One merchant message -> {kind: 'answer', message} or {kind: 'case', case, message}."""
    from munim.graph import runner
    from munim.llm import router
    from munim.models import MerchantAnswer

    question = (text or "").strip()[:MAX_TEXT]
    if not question:
        raise ValueError("empty question")
    _rate(merchant_id)
    with tx() as conn:
        snap = lens.snapshot(conn, merchant_id)
    shop = snap["shop"] or {}
    lang = _language(question, shop)
    ctx = {"snapshot": snap, "question": question, "language": lang, "today": config.DEMO_TODAY,
           "merchant": shop, "merchant_id": merchant_id}
    ans, res = await router.generate_json("reasoner", "merchant", ctx, MerchantAnswer)

    if ans.intent == "investigate":
        with tx() as conn:   # one investigation at a time per shop
            running = one(conn, "SELECT id FROM cases WHERE merchant_id=? AND status='INVESTIGATING' ORDER BY created_at DESC LIMIT 1",
                          (merchant_id,))
        case_id = running["id"] if running else runner.start_case(merchant_id, question, "app")
        with tx() as conn:
            card = lens.case_card(conn, lens.own_case(conn, merchant_id, case_id))
            msg = guard.guard_reply(conn, merchant_id, {"text": ans.answer, "en": ans.answer_en or ans.answer, "language": lang},
                                    case_id=case_id)
            audit(conn, case_id, "merchant", "app_question", {"merchant_id": merchant_id, "question": question,
                                                              "provider": res.provider, "live": res.live})
        return {"kind": "case", "case": card, "message": msg, "live": res.live}

    with tx() as conn:
        msg = guard.guard_reply(conn, merchant_id, {"text": ans.answer, "en": ans.answer_en or ans.answer, "language": lang})
        audit(conn, None, "merchant", "app_answer", {"merchant_id": merchant_id, "question": question, "guarded": msg["guarded"],
                                                     "provider": res.provider, "live": res.live})
    return {"kind": "answer", "message": msg, "live": res.live}
