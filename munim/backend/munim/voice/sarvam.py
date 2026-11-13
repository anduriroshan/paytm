"""Sarvam speech: STT with Saaras, TTS with Bulbul. Never raises: problems come back as {"error": ...}.

Verified against docs.sarvam.ai:
  STT  POST {SARVAM_BASE_URL}/speech-to-text   multipart: file, model (saaras:v4 default/recommended | saaras:v3),
       mode=transcribe, language_code=unknown (auto-detect) -> {transcript, language_code, ...}
       https://docs.sarvam.ai/api-reference/speech-to-text/transcribe
  TTS  POST {SARVAM_BASE_URL}/text-to-speech   JSON: text (≤2500 chars), language_code (hi-IN, te-IN, en-IN, …),
       model bulbul:v4-flash (newest, ₹1 / 1K chars) | bulbul:v3, speaker, output_audio_codec -> {audios: [base64]}
       https://docs.sarvam.ai/api-reference/text-to-speech/convert
       voices: https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/voices
Auth header: api-subscription-key.

Optional env overrides (read at call time; config.py is shared): MUNIM_STT_MODEL, MUNIM_TTS_MODEL, MUNIM_TTS_SPEAKER.
"""
from __future__ import annotations

import base64
import logging
import os

import httpx

from munim import config
from munim.llm import providers

log = logging.getLogger("munim.voice")

DEFAULT_STT_MODEL = "saaras:v4"
FALLBACK_STT_MODEL = "saaras:v3"
DEFAULT_TTS_MODEL = "bulbul:v4-flash"
FALLBACK_TTS_MODEL = "bulbul:v3"
TTS_MAX_CHARS = 2500
STT_TIMEOUT_S = 60.0
TTS_TIMEOUT_S = 45.0

# bulbul:v4-flash persona voices (<name>_<lang>_<style>); v3 short names are NOT valid on v4 and vice versa.
V4_SPEAKERS = {"hi-IN": "ritu_hi_customer_warm", "te-IN": "kavitha_te_conversation", "en-IN": "simran_en_customer"}
V3_SPEAKERS = {"hi-IN": "ritu", "te-IN": "kavitha", "en-IN": "simran"}
V4_UNSUPPORTED = {"ml-IN", "od-IN"}                       # "Malayalam and Odia are not yet offered on v4 Flash"
TTS_LANGS = {"bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN"}
_SHORT_TO_BCP = {"hi": "hi-IN", "hi-latn": "hi-IN", "hinglish": "hi-IN", "te": "te-IN", "en": "en-IN", "bn": "bn-IN",
                 "gu": "gu-IN", "kn": "kn-IN", "ml": "ml-IN", "mr": "mr-IN", "od": "od-IN", "or": "od-IN", "pa": "pa-IN",
                 "ta": "ta-IN"}
_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "aac": "audio/aac", "flac": "audio/flac"}


def stt_model() -> str:
    return (os.getenv("MUNIM_STT_MODEL") or DEFAULT_STT_MODEL).strip()


def tts_model() -> str:
    return (os.getenv("MUNIM_TTS_MODEL") or DEFAULT_TTS_MODEL).strip()


def _base() -> str:
    return config.SARVAM_BASE_URL.rstrip("/")


def _unavailable() -> str | None:
    if (config.VOICE or "").strip().lower() != "sarvam":
        return f"voice is disabled (MUNIM_VOICE={config.VOICE or 'off'})"
    if not config.SARVAM_API_KEY:
        return "SARVAM_API_KEY is not set; voice is unavailable (text chat still works)"
    return None


def _stt_error(msg: str, provider: str = "sarvam") -> dict:
    return {"text": None, "language_code": None, "language": None, "provider": provider, "error": msg}


def _tts_error(msg: str, provider: str = "sarvam") -> dict:
    return {"audio_base64": None, "mime": None, "provider": provider, "error": msg}


def _http_reason(resp: httpx.Response) -> str:
    msg = ""
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        msg = (err.get("message") if isinstance(err, dict) else err) or (data.get("message") if isinstance(data, dict) else "")
    except ValueError:
        msg = resp.text[:120]
    hint = {401: "invalid API key", 402: "no credits", 403: "forbidden / no access", 429: "rate limited"}.get(resp.status_code, "")
    return f"HTTP {resp.status_code}" + (f" {hint}" if hint else "") + (f": {str(msg)[:120]}" if msg else "")


def short_language(language_code: str | None, text: str = "") -> str | None:
    """Sarvam BCP-47 code + transcript -> Munim language code ('hi', 'hi-Latn', 'te', 'en', …)."""
    if text and text.strip():
        from munim.llm.mock import detect_language          # script + Hinglish word heuristic
        guess = detect_language(text)
        if guess != "en" or not language_code:
            return guess
    if not language_code:
        return None
    return language_code.split("-")[0].lower()


def bcp47(language: str | None) -> str:
    raw = (language or "en").strip()
    if raw in TTS_LANGS:
        return raw
    low = raw.lower()
    if low in _SHORT_TO_BCP:
        return _SHORT_TO_BCP[low]
    if "-" in low and f"{low.split('-')[0]}-IN" in TTS_LANGS and "latn" not in low:
        return f"{low.split('-')[0]}-IN"
    return _SHORT_TO_BCP.get(low.split("-")[0], "en-IN")


async def stt(audio: bytes, filename: str, content_type: str) -> dict:
    """Speech -> text. Returns {"text","language_code","provider"} (+ "language" short code), or the same keys
    set to None plus "error"."""
    why = _unavailable()
    if why:
        return _stt_error(why)
    if not audio:
        return _stt_error("empty audio")
    models = [stt_model()]
    if FALLBACK_STT_MODEL not in models:
        models.append(FALLBACK_STT_MODEL)
    headers = {"api-subscription-key": config.SARVAM_API_KEY}
    last = "unknown error"
    for i, model in enumerate(models):
        form = {"model": model, "mode": "transcribe", "language_code": "unknown"}
        files = {"file": (filename or "speech.webm", audio, content_type or "application/octet-stream")}
        try:
            async with providers.make_client(STT_TIMEOUT_S) as client:
                resp = await client.post(f"{_base()}/speech-to-text", data=form, files=files, headers=headers)
        except httpx.HTTPError as e:
            return _stt_error(f"network error ({type(e).__name__})", f"sarvam:{model}")
        if resp.status_code in (400, 404, 422) and i + 1 < len(models):
            last = _http_reason(resp)
            log.warning("STT %s rejected (%s); retrying with %s", model, last, models[i + 1])
            continue
        if resp.status_code >= 400:
            return _stt_error(_http_reason(resp), f"sarvam:{model}")
        try:
            data = resp.json()
        except ValueError:
            return _stt_error("unreadable STT response", f"sarvam:{model}")
        text = str(data.get("transcript") or "").strip()
        code = data.get("language_code")
        return {"text": text, "language_code": code, "language": short_language(code, text),
                "provider": f"sarvam:{model}"}
    return _stt_error(last)


def _tts_plan(lang_code: str) -> list[tuple[str, str | None, str]]:
    """[(model, speaker, codec), …] in order of preference."""
    primary = tts_model()
    override = (os.getenv("MUNIM_TTS_SPEAKER") or "").strip() or None
    plan: list[tuple[str, str | None, str]] = []
    if primary.startswith("bulbul:v4") and lang_code in V4_UNSUPPORTED:
        primary = FALLBACK_TTS_MODEL
    if primary.startswith("bulbul:v4"):
        speaker = override or V4_SPEAKERS.get(lang_code)
        if speaker:                                      # no verified v4 persona for this language -> go to v3
            plan.append((primary, speaker, "mp3"))
    else:
        plan.append((primary, override or V3_SPEAKERS.get(lang_code), "mp3"))
    fb = (FALLBACK_TTS_MODEL, V3_SPEAKERS.get(lang_code), "wav")
    if fb not in plan:
        plan.append(fb)
    return plan


async def tts(text: str, language: str) -> dict:
    """Text -> speech. Returns {"audio_base64","mime","provider"}, or those keys set to None plus "error"."""
    why = _unavailable()
    if why:
        return _tts_error(why)
    text = (text or "").strip()
    if not text:
        return _tts_error("empty text")
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]
    lang_code = bcp47(language)
    headers = {"api-subscription-key": config.SARVAM_API_KEY, "Content-Type": "application/json"}
    plan = _tts_plan(lang_code)
    last = "unknown error"
    for i, (model, speaker, codec) in enumerate(plan):
        body: dict = {"text": text, "language_code": lang_code, "model": model, "output_audio_codec": codec}
        if speaker:
            body["speaker"] = speaker
        try:
            async with providers.make_client(TTS_TIMEOUT_S) as client:
                resp = await client.post(f"{_base()}/text-to-speech", json=body, headers=headers)
        except httpx.HTTPError as e:
            return _tts_error(f"network error ({type(e).__name__})", f"sarvam:{model}")
        if resp.status_code in (400, 404, 422) and i + 1 < len(plan):
            last = _http_reason(resp)
            log.warning("TTS %s/%s rejected (%s); retrying with %s", model, speaker, last, plan[i + 1][0])
            continue
        if resp.status_code >= 400:
            return _tts_error(_http_reason(resp), f"sarvam:{model}")
        try:
            audios = [a for a in (resp.json().get("audios") or []) if a]
        except (ValueError, AttributeError):
            return _tts_error("unreadable TTS response", f"sarvam:{model}")
        if not audios:
            return _tts_error("TTS returned no audio", f"sarvam:{model}")
        b64 = audios[0] if (len(audios) == 1 or codec != "mp3") else \
            base64.b64encode(b"".join(base64.b64decode(a) for a in audios)).decode("ascii")
        return {"audio_base64": b64, "mime": _MIME.get(codec, "audio/wav"), "provider": f"sarvam:{model}",
                "speaker": speaker, "language_code": lang_code}
    return _tts_error(last)
