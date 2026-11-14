"""Gate 1, Who: demo login for the merchant app and a signed session token.

Demo only: pick one of the demo shops and enter the demo OTP. A real app would use the merchant's existing
app login (or an OTP to the registered phone). Either way the server, not the chat, decides which shop this is.

Token = base64url(payload JSON) "." base64url(HMAC-SHA256(payload)). Payload: shop id, role, expiry.
The key comes from MUNIM_SESSION_SECRET, or is random per process (tokens then end on restart).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from fastapi import Header, HTTPException

DEMO_OTP = "123456"
DEMO_SHOPS = ("M1001", "M1002", "M1003", "M1004", "M1005", "M1006", "M1007", "M1008", "M1009")
ROLES = ("owner",)                      # staff (cashier) would get a narrower lens; not in the demo
TOKEN_TTL_S = 8 * 60 * 60

_SECRET = (os.environ.get("MUNIM_SESSION_SECRET") or "").encode() or secrets.token_bytes(32)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())


def issue(merchant_id: str, role: str = "owner", ttl_s: int = TOKEN_TTL_S) -> str:
    body = _b64(json.dumps({"mid": merchant_id, "role": role, "exp": int(time.time()) + ttl_s}, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def verify(token: str | None) -> dict[str, Any] | None:
    """Payload of a valid, unexpired token; None for anything else."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        data = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("mid") not in DEMO_SHOPS or data.get("role") not in ROLES:
        return None
    if int(data.get("exp") or 0) < time.time():
        return None
    return data


def current_merchant(authorization: str | None = Header(None)) -> dict[str, Any]:
    """FastAPI dependency: the logged-in shop, from the Bearer token only."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    data = verify(token)
    if data is None:
        raise HTTPException(401, "Please log in again.")
    return {"merchant_id": data["mid"], "role": data["role"]}
