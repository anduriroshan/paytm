"""Gate 4, Say: the last check before any text reaches the merchant app. Plain code, no LLM.

A reply is refused (and a safe line sent instead) when it contains:
  - a risk-hold reason or code, from any shop (fraud signals are never explained to a merchant);
  - another shop's id or business name;
  - internal words: how Munim decides, its limits, its tools.
"""
from __future__ import annotations

import re
from typing import Any

from munim import config
from munim.db.conn import audit, rows
from munim.forensics.facts import human_code
from munim.llm.mock import reply_language
from munim.models import rupees

INTERNAL_WORDS = (
    "firewall", "probe", "suspect", "sweep", "velocity", "bin", "card volume", "high-value card", "fraud",
    "auto-approval", "auto approval", "approval limit", "30-day cap", "30 day cap", "risk score", "injection",
    "prompt", "llm", "sql", "case file", "system prompt", "ops.lead",
)
SAFE = {
    "en": "I have checked your account. A teammate will send you the details soon.",
    "hi": "मैंने आपका खाता देख लिया है। हमारी टीम जल्द ही आपको पूरी जानकारी भेजेगी।",
    "hi-Latn": "Maine aapka account check kar liya hai. Hamari team jaldi aapko poori jaankari bhejegi.",
    "te": "మీ ఖాతాను పరిశీలించాను. మా బృందం త్వరలో మీకు పూర్తి వివరాలు పంపుతుంది.",
}
_ID_RX = re.compile(r"\bM\d{4}\b")
# Team words that are fine in substance but say too much: reworded before the check (same meaning, no hint of why).
SOFTEN = (
    (re.compile(r"\brisk team\b", re.I), "specialist team"),
    (re.compile(r"\brisk review\b", re.I), "safety review"),
    (re.compile("रिस्क टीम"), "विशेषज्ञ टीम"),
    (re.compile("రిస్క్ టీమ్‌కు"), "నిపుణుల బృందానికి"),
    (re.compile("రిస్క్ టీమ్"), "నిపుణుల బృందం"),
)


def soften(text: str) -> str:
    for rx, sub in SOFTEN:
        text = rx.sub(sub, text)
    return text


def _terms(conn, merchant_id: str) -> tuple[list[str], list[str]]:
    risk: list[str] = []
    for h in rows(conn, "SELECT reason_code, reason_text FROM holds WHERE hold_type='RISK'"):
        risk += [h["reason_code"], human_code(h["reason_code"]), h["reason_text"]]
    names = [r["business_name"] for r in rows(conn, "SELECT business_name FROM merchants WHERE id != ?", (merchant_id,))]
    own = rows(conn, "SELECT business_name FROM merchants WHERE id = ?", (merchant_id,))
    own_name = (own[0]["business_name"] if own else "").lower()
    # a shorter name inside our own name (e.g. a shop called "Sharma Kirana") is not a leak
    names = [n for n in names if n and n.lower() not in own_name]
    return [t for t in risk if t], names


def check(conn, merchant_id: str, text: str) -> list[str]:
    """What in `text` must not reach this merchant. Empty list = safe to send."""
    t = (text or "").lower()
    hits: list[str] = []
    risk, names = _terms(conn, merchant_id)
    hits += [f"risk reason: {r}" for r in risk if r.lower() in t]
    hits += [f"other shop id: {m}" for m in set(_ID_RX.findall(text or "")) if m != merchant_id]
    hits += [f"other shop: {n}" for n in names if n.lower() in t]
    hits += [f"internal word: {w}" for w in INTERNAL_WORDS if re.search(r"\b" + re.escape(w) + r"\b", t)]
    limit_words = re.search(r"\b(limit|cap)\b", t)
    for amt in (config.AUTO_LIMIT_PAISE, config.MERCHANT_30D_AUTO_LIMIT_PAISE):
        if limit_words and rupees(amt).lower() in t:
            hits.append(f"internal limit: {rupees(amt)}")
    return hits


def safe_text(language: str | None) -> str:
    return SAFE[reply_language({"language": language})]


def guard_reply(conn, merchant_id: str, msg: dict[str, Any], *, case_id: str | None = None) -> dict[str, Any]:
    """Pass a {text, en, language} message through the check; swap in the safe line on any hit (both copies)."""
    msg = {**msg, "text": soften(msg.get("text") or ""), "en": soften(msg.get("en") or "")}
    hits = check(conn, merchant_id, msg.get("text") or "") + check(conn, merchant_id, msg.get("en") or "")
    if not hits:
        return {**msg, "guarded": False}
    lang = msg.get("language") or "en"
    try:
        audit(conn, case_id, "guard", "merchant_reply_held", {"merchant_id": merchant_id, "hits": sorted(set(hits))[:10]})
    except Exception:  # noqa: BLE001 - the audit must never break the answer
        pass
    return {**msg, "text": safe_text(lang), "en": SAFE["en"], "guarded": True}
