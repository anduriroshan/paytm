"""Merchant app API (/api/me/*). Every route but login takes the shop from the signed token, never from the request."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from munim.access import assistant, lens, session
from munim.db.conn import rows, tx

router = APIRouter(prefix="/api/me", tags=["merchant app"])


class Login(BaseModel):
    merchant_id: str
    otp: str


class Ask(BaseModel):
    text: str


@router.get("/shops")
def demo_shops():
    """Demo login picker: the demo shops, with the masked phone a real OTP would go to."""
    with tx() as conn:
        shops = rows(conn, f"""SELECT id, business_name, owner_name, city, language, category, phone FROM merchants
                               WHERE id IN ({','.join('?' * len(session.DEMO_SHOPS))}) ORDER BY id""", session.DEMO_SHOPS)
    return [{**s, "phone": lens.mask(s["phone"])} for s in shops]


@router.post("/login")
def login(body: Login):
    if body.merchant_id not in session.DEMO_SHOPS:
        raise HTTPException(404, "Shop not found.")
    if body.otp.strip() != session.DEMO_OTP:
        raise HTTPException(401, "Wrong OTP.")
    with tx() as conn:
        shop = lens.shop(conn, body.merchant_id)
    if not shop:
        raise HTTPException(404, "Shop not found.")
    return {"token": session.issue(body.merchant_id), "shop": shop}


@router.get("/home")
def home(me: dict = Depends(session.current_merchant)):
    with tx() as conn:
        snap = lens.snapshot(conn, me["merchant_id"])
    return {**snap, "suggestions": lens.suggestions(snap)}


@router.get("/cases")
def my_cases(me: dict = Depends(session.current_merchant)):
    with tx() as conn:
        return [lens.case_card(conn, c, with_reply=True) for c in lens.own_cases(conn, me["merchant_id"])]


@router.get("/cases/{case_id}")
def my_case(case_id: str, me: dict = Depends(session.current_merchant)):
    with tx() as conn:
        case = lens.own_case(conn, me["merchant_id"], case_id)
        if not case:
            raise HTTPException(404, "Issue not found.")
        return lens.case_card(conn, case, with_reply=True)


@router.post("/ask")
async def ask(body: Ask, me: dict = Depends(session.current_merchant)):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Message is empty.")
    if len(text) > assistant.MAX_TEXT:
        raise HTTPException(400, f"Message is too long (max {assistant.MAX_TEXT} characters).")
    try:
        return await assistant.ask(me["merchant_id"], text)
    except assistant.TooMany as e:
        raise HTTPException(429, str(e))
