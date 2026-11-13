"""Provider clients over httpx: OpenAI-compatible streaming chat (Gemini, Sarvam, any /chat/completions
server) and the Sarvam Prism decision API. No network at import time. Every failure surfaces as
ProviderError with a short, UI-safe `reason`; the router turns that into a mock fallback.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio
import httpx

from munim import config

OnDelta = Callable[[str, str], None]

CHAT_TIMEOUT_S = 45.0
CONNECT_TIMEOUT_S = 10.0
PRISM_TIMEOUT_S = 30.0
LIST_TIMEOUT_S = 15.0

# Optional request params a provider may reject with a 400. Dropped together on the single retry.
OPTIONAL_PARAMS = ("response_format", "reasoning_effort", "temperature")

KNOWN_PROVIDERS = ("gemini", "sarvam", "openai", "mock")


class ProviderError(Exception):
    def __init__(self, reason: str, *, status: int | None = None, body: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.body = body


@dataclass
class ChatOutput:
    text: str
    reasoning: str = ""
    usage: dict | None = None
    model: str = ""
    finish_reason: str | None = None
    dropped_params: list[str] = field(default_factory=list)


def make_client(timeout: float = CHAT_TIMEOUT_S) -> httpx.AsyncClient:
    """Factory so tests can swap in httpx.MockTransport (monkeypatch providers.make_client)."""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S))


# ───────────────────────────── provider config ─────────────────────────────

def split_spec(spec: str) -> tuple[str, str]:
    """'gemini:gemini-flash-latest' -> ('gemini', 'gemini-flash-latest'). Splits on the first ':' only,
    so Ollama tags like 'openai:llama3.1:8b' survive."""
    spec = (spec or "").strip()
    if ":" in spec:
        p, m = spec.split(":", 1)
        return p.strip().lower(), m.strip()
    return spec.lower(), ""


def provider_key(provider: str) -> str:
    return {
        "gemini": config.GEMINI_API_KEY,
        "sarvam": config.SARVAM_API_KEY,
        "openai": config.OPENAI_COMPAT_API_KEY,
    }.get(provider, "")


def provider_ready(provider: str) -> tuple[bool, str]:
    """(usable, note). openai-compatible servers (Ollama, vLLM) may not need a key, only a base URL."""
    if provider == "mock":
        return False, "mock by configuration"
    if provider not in KNOWN_PROVIDERS:
        return False, f"unknown provider '{provider}'"
    if provider == "gemini" and not config.GEMINI_API_KEY:
        return False, "no GEMINI_API_KEY"
    if provider == "sarvam" and not config.SARVAM_API_KEY:
        return False, "no SARVAM_API_KEY"
    if provider == "openai" and not config.OPENAI_COMPAT_BASE_URL:
        return False, "no OPENAI_COMPAT_BASE_URL"
    return True, "live"


def chat_url(provider: str) -> str:
    if provider == "gemini":
        return config.GEMINI_BASE_URL.rstrip("/") + "/chat/completions"
    if provider == "sarvam":
        return config.SARVAM_BASE_URL.rstrip("/") + "/v2/chat/completions"
    if provider == "openai":
        return config.OPENAI_COMPAT_BASE_URL.rstrip("/") + "/chat/completions"
    raise ProviderError(f"unknown provider '{provider}'")


def models_url(provider: str) -> str:
    if provider == "gemini":
        return config.GEMINI_BASE_URL.rstrip("/") + "/models"
    if provider == "sarvam":
        return config.SARVAM_BASE_URL.rstrip("/") + "/v2/models"
    if provider == "openai":
        return config.OPENAI_COMPAT_BASE_URL.rstrip("/") + "/models"
    raise ProviderError(f"unknown provider '{provider}'")


def auth_headers(provider: str) -> dict[str, str]:
    key = provider_key(provider)
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    if provider == "sarvam" and key:
        h["api-subscription-key"] = key          # Sarvam's native header; Bearer is also accepted
    return h


# ───────────────────────────── errors ─────────────────────────────

_STATUS_HINT = {
    401: "auth failed (check API key)",
    402: "no credits",
    403: "forbidden (check key / model access)",
    404: "model or endpoint not found",
    408: "upstream timeout",
    413: "request too large",
    429: "rate limited",
    500: "provider error",
    502: "bad gateway",
    503: "provider overloaded",
    504: "gateway timeout",
}


def _error_reason(provider: str, status: int, body: str) -> str:
    """Short, UI-safe reason, e.g. 'sarvam: HTTP 402 no credits'."""
    if status in (400, 422):
        msg = _error_message(body)
        return f"{provider}: HTTP {status} {msg[:90]}".rstrip()
    return f"{provider}: HTTP {status} {_STATUS_HINT.get(status, 'error')}"


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.strip()[:200]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err)[:200]
        return str(err)[:200]
    return str(data)[:200]


def _mentions_optional(body: str, payload: dict) -> list[str]:
    low = body.lower()
    hits = [p for p in OPTIONAL_PARAMS if p in payload and (p in low or p.replace("_", " ") in low)]
    if not hits and "json_object" in low and "response_format" in payload:
        hits = ["response_format"]
    if not hits and "reasoning" in low and "reasoning_effort" in payload:
        hits = ["reasoning_effort"]
    return [p for p in OPTIONAL_PARAMS if p in payload] if hits else []


# ───────────────────────────── SSE parsing ─────────────────────────────

def parse_sse_line(line: str) -> dict | str | None:
    """One SSE line -> parsed JSON dict, the string '[DONE]', or None (comment/blank/other field/garbage)."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data:
        return None
    if data == "[DONE]":
        return "[DONE]"
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def chunk_parts(obj: dict) -> tuple[str, str, dict | None, str | None]:
    """(content, reasoning, usage, finish_reason) from one chat.completion.chunk. Tolerates chunks with
    no `choices` (usage-only), null deltas and provider-specific reasoning field names."""
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
    choices = obj.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return "", "", usage, None
    ch = choices[0]
    delta = ch.get("delta") or ch.get("message") or {}
    content = delta.get("content") or ""
    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
    if not isinstance(content, str):                 # some servers send content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict)) if isinstance(content, list) else ""
    if not isinstance(reasoning, str):
        reasoning = ""
    return content, reasoning, usage, ch.get("finish_reason")


def _emit(on_delta: OnDelta | None, kind: str, text: str) -> None:
    if on_delta and text:
        try:
            on_delta(kind, text)
        except Exception:                            # a UI callback must never kill the LLM call
            pass


# ───────────────────────────── chat ─────────────────────────────

async def stream_chat(
    provider: str,
    model: str,
    messages: list[dict],
    *,
    json_mode: bool = False,
    max_tokens: int = 1500,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    on_delta: OnDelta | None = None,
    timeout: float = CHAT_TIMEOUT_S,
) -> ChatOutput:
    """Streamed /chat/completions. Retries once without optional params on a 400 that names them."""
    ok, note = provider_ready(provider)
    if not ok:
        raise ProviderError(f"{provider}: {note}")
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    dropped: list[str] = []
    for attempt in (1, 2):
        try:
            out = await _stream_once(provider, payload, on_delta, timeout)
            out.dropped_params = dropped
            return out
        except ProviderError as e:
            if attempt == 1 and e.status == 400:
                drop = _mentions_optional(e.body, payload)
                if drop:
                    for p in drop:
                        payload.pop(p, None)
                    dropped = drop
                    continue
            raise
    raise ProviderError(f"{provider}: retry exhausted")    # pragma: no cover


async def _stream_once(provider: str, payload: dict, on_delta: OnDelta | None, timeout: float) -> ChatOutput:
    url = chat_url(provider)
    headers = auth_headers(provider)
    headers["Accept"] = "text/event-stream"
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict | None = None
    finish: str | None = None
    model = payload.get("model", "")
    try:
        with anyio.fail_after(timeout):
            async with make_client(timeout) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise ProviderError(_error_reason(provider, resp.status_code, body),
                                            status=resp.status_code, body=body)
                    ctype = resp.headers.get("content-type", "")
                    if "text/event-stream" not in ctype and "json" in ctype:
                        # server ignored stream=true: one JSON completion
                        body = (await resp.aread()).decode("utf-8", "replace")
                        try:
                            obj = json.loads(body)
                        except (json.JSONDecodeError, ValueError):
                            raise ProviderError(f"{provider}: unreadable response")
                        if isinstance(obj, dict) and obj.get("error"):
                            raise ProviderError(f"{provider}: {_error_message(body)[:90]}", body=body)
                        c, r, u, f = chunk_parts(obj)
                        _emit(on_delta, "reasoning", r)
                        _emit(on_delta, "content", c)
                        content.append(c); reasoning.append(r)
                        usage, finish = u, f
                        model = obj.get("model") or model
                    else:
                        async for line in resp.aiter_lines():
                            item = parse_sse_line(line)
                            if item is None:
                                continue
                            if item == "[DONE]":
                                break
                            if item.get("error"):
                                raise ProviderError(f"{provider}: stream error {_error_message(json.dumps(item))[:90]}",
                                                    body=json.dumps(item))
                            c, r, u, f = chunk_parts(item)
                            if r:
                                reasoning.append(r)
                                _emit(on_delta, "reasoning", r)
                            if c:
                                content.append(c)
                                _emit(on_delta, "content", c)
                            if u:
                                usage = u
                            if f:
                                finish = f
                            model = item.get("model") or model
    except ProviderError:
        raise
    except TimeoutError:
        raise ProviderError(f"{provider}: timeout after {int(timeout)}s")
    except httpx.TimeoutException:
        raise ProviderError(f"{provider}: timeout")
    except httpx.HTTPError as e:
        raise ProviderError(f"{provider}: network error ({type(e).__name__})")
    text = "".join(content)
    if not text.strip():
        why = "max_tokens used up by reasoning" if finish == "length" else "empty reply"
        raise ProviderError(f"{provider}: {why}")
    return ChatOutput(text=text, reasoning="".join(reasoning), usage=usage, model=model, finish_reason=finish)


# ───────────────────────────── Sarvam Prism ─────────────────────────────

async def prism_decide(state: Any, questions: dict[str, dict], *, model: str = "sarvam-prism",
                       timeout: float = PRISM_TIMEOUT_S) -> dict:
    """POST {SARVAM_BASE_URL}/v2/systemone. Returns the parsed JSON response ({model, answers, usage})."""
    if not config.SARVAM_API_KEY:
        raise ProviderError("sarvam: no SARVAM_API_KEY")
    url = config.SARVAM_BASE_URL.rstrip("/") + "/v2/systemone"
    body = {"model": model or "sarvam-prism", "state": state, "questions": questions}
    try:
        with anyio.fail_after(timeout):
            async with make_client(timeout) as client:
                resp = await client.post(url, json=body, headers=auth_headers("sarvam"))
    except TimeoutError:
        raise ProviderError(f"sarvam-prism: timeout after {int(timeout)}s")
    except httpx.TimeoutException:
        raise ProviderError("sarvam-prism: timeout")
    except httpx.HTTPError as e:
        raise ProviderError(f"sarvam-prism: network error ({type(e).__name__})")
    if resp.status_code >= 400:
        raise ProviderError(_error_reason("sarvam-prism", resp.status_code, resp.text),
                            status=resp.status_code, body=resp.text)
    try:
        data = resp.json()
    except ValueError:
        raise ProviderError("sarvam-prism: unreadable response")
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise ProviderError("sarvam-prism: response has no answers")
    return data


# ───────────────────────────── model listing (free) ─────────────────────────────

async def list_models(provider: str, timeout: float = LIST_TIMEOUT_S) -> list[str]:
    ok, note = provider_ready(provider)
    if not ok:
        raise ProviderError(f"{provider}: {note}")
    try:
        async with make_client(timeout) as client:
            resp = await client.get(models_url(provider), headers=auth_headers(provider))
    except httpx.HTTPError as e:
        raise ProviderError(f"{provider}: network error ({type(e).__name__})")
    if resp.status_code >= 400:
        raise ProviderError(_error_reason(provider, resp.status_code, resp.text), status=resp.status_code, body=resp.text)
    try:
        data = resp.json()
    except ValueError:
        raise ProviderError(f"{provider}: unreadable model list")
    items = (data.get("data") or data.get("models") or []) if isinstance(data, dict) else data
    ids = []
    for it in items or []:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or ""
            ids.append(mid.removeprefix("models/"))
        elif isinstance(it, str):
            ids.append(it)
    return sorted(set(i for i in ids if i))


def elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
