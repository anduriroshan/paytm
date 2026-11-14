"""Merchant-facing access: the same Munim engine behind four gates.

    Who  (session.py)   the login decides the shop; an id typed in chat is ignored.
    See  (lens.py)      this shop's rows only, from explicit column allowlists, with internal fields redacted.
    Do   (api/merchant) a merchant can ask, open an issue, and get fixes that move no money (pickup, KYC link).
                        Money actions on a merchant-opened case always wait for a teammate (firewall `limits`).
    Say  (guard.py)     every answer is checked for internal text and other shops before it leaves.
"""
