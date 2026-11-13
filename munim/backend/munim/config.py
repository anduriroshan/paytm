"""Settings from backend/.env (see .env.example). Import-safe: no network, no side effects beyond reading env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


DB_PATH = Path(_env("MUNIM_DB", str(BACKEND_DIR / "data" / "munim.db")))
DEMO_TODAY = _env("MUNIM_TODAY", "2026-11-14")          # the demo's "today"; all seed data is relative to it

# ── provider credentials ──
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_BASE_URL = _env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
SARVAM_API_KEY = _env("SARVAM_API_KEY")
SARVAM_BASE_URL = _env("SARVAM_BASE_URL", "https://api.sarvam.ai")
OPENAI_COMPAT_BASE_URL = _env("OPENAI_COMPAT_BASE_URL")
OPENAI_COMPAT_API_KEY = _env("OPENAI_COMPAT_API_KEY")

# ── role routing: "provider:model" ──
REASONER = _env("MUNIM_REASONER", "gemini:gemini-flash-latest")   # intake, propose, improvise (JSON out)
WRITER = _env("MUNIM_WRITER", "sarvam:sarvam-105b")               # merchant-facing replies (vernacular)
DECIDER = _env("MUNIM_DECIDER", "sarvam:sarvam-prism")            # hunch probabilities: sarvam:sarvam-prism | llm | mock
VOICE = _env("MUNIM_VOICE", "sarvam")                             # sarvam | off
REASONING_EFFORT = _env("MUNIM_REASONING_EFFORT", "low")                 # reasoner / JSON tasks
WRITER_REASONING_EFFORT = _env("MUNIM_WRITER_REASONING_EFFORT", "none")  # replies only restate settled facts;
#   sarvam-105b thinks at "medium" by default and can spend the whole token budget (and credits) on it
FORCE_MOCK = _env("MUNIM_FORCE_MOCK", "0") == "1"                  # run the scripted offline demo regardless of keys
MOCK_STREAM_DELAY_MS = int(_env("MUNIM_MOCK_STREAM_DELAY_MS", "12"))  # mock token pacing so the UI looks live

# ── Action Firewall policy (deterministic) ──
AUTO_LIMIT_PAISE = int(_env("MUNIM_AUTO_LIMIT_PAISE", "200000"))            # ₹2,000 per action without a human
MERCHANT_30D_AUTO_LIMIT_PAISE = int(_env("MUNIM_MERCHANT_30D_LIMIT_PAISE", "500000"))  # ₹5,000 per merchant / 30 days
INJECTION_THRESHOLD = float(_env("MUNIM_INJECTION_THRESHOLD", "0.8"))
MAX_PROBES = int(_env("MUNIM_MAX_PROBES", "3"))                              # improvise: max SQL probes per case
PROBE_ROW_LIMIT = 50

FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "dist"
