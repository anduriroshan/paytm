"""Extract and repair JSON from model text.

Models wrap JSON in code fences, add prose around it, leave trailing commas, use Python literals or
single quotes, or get cut off by max_tokens. `extract_json` tries progressively more forgiving readings
and raises JSONFixError when nothing parses. `parse_model` adds Pydantic validation (with a couple of
harmless unwrapping tricks) on top.
"""
from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json|JSON|json5)?\s*\n?(.*?)```", re.DOTALL)
_SMART = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


class JSONFixError(ValueError):
    """No JSON value could be recovered from the text."""


def strip_think(text: str) -> str:
    """Drop <think>…</think> blocks (and a dangling unclosed <think> prefix) some models emit inline."""
    text = _THINK_RE.sub("", text or "")
    if "<think>" in text.lower() and "</think>" not in text.lower():
        text = text[: text.lower().index("<think>")]
    return text


def _balanced(s: str, start: int) -> tuple[str, bool]:
    """From s[start] ('{' or '['), return the balanced JSON-ish slice and whether it closed.
    Double-quote aware; single quotes are not tracked (they are legal inside JSON strings)."""
    stack: list[str] = []
    in_str = esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]":
            if stack and stack[-1] == c:
                stack.pop()
            if not stack:
                return s[start : i + 1], True
    return s[start:], False


def _close_truncated(s: str) -> str:
    """Best effort for output cut off by max_tokens: close an open string, drop a dangling key/comma,
    then close every open bracket."""
    stack: list[str] = []
    in_str = esc = False
    for c in s:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]" and stack:
            stack.pop()
    out = s + ('"' if in_str else "")
    out = re.sub(r'[,:]\s*$', "", out.rstrip())
    out = re.sub(r',\s*"[^"]*"\s*$', "", out)          # dangling key without value
    return out + "".join(reversed(stack))


def repair(s: str) -> str:
    """String-aware cleanup: comments, trailing commas, Python literals, single-quoted strings,
    raw newlines/tabs inside strings."""
    out: list[str] = []
    i, n = 0, len(s)
    in_str = esc = False
    while i < n:
        c = s[i]
        if in_str:
            if esc:
                esc = False
                out.append(c)
            elif c == "\\":
                esc = True
                out.append(c)
            elif c == '"':
                in_str = False
                out.append(c)
            elif c == "\n":
                out.append("\\n")
            elif c == "\t":
                out.append("\\t")
            elif c == "\r":
                pass
            else:
                out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "}]":
            while out and out[-1].isspace():
                out.pop()
            if out and out[-1] == ",":
                out.pop()
            out.append(c)
            i += 1
        elif c == "'":
            # single-quoted string: convert to a double-quoted one
            j = i + 1
            buf: list[str] = []
            while j < n and s[j] != "'":
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j : j + 2])
                    j += 2
                    continue
                buf.append('\\"' if s[j] == '"' else s[j])
                j += 1
            out.append('"' + "".join(buf) + '"')
            i = j + 1
        elif c.isalpha():
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            word = s[i:j]
            out.append({"True": "true", "False": "false", "None": "null"}.get(word, word))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _candidates(text: str) -> list[str]:
    s = strip_think(text).strip().lstrip("﻿")
    cands: list[str] = [s]
    cands += [m.strip() for m in _FENCE_RE.findall(s)]
    if "```" in s:                                     # unterminated fence
        tail = s.split("```", 1)[1]
        cands.append(re.sub(r"^(json|JSON)?\s*", "", tail).strip())
    for opener in "{[":
        k = s.find(opener)
        while k >= 0:
            frag, closed = _balanced(s, k)
            cands.append(frag if closed else _close_truncated(frag))
            if closed:
                break
            k = s.find(opener, k + 1)
    seen: set[str] = set()
    return [c for c in cands if c and not (c in seen or seen.add(c))]


def extract_json(text: str) -> Any:
    """Return the first JSON value recoverable from `text`, or raise JSONFixError."""
    if not text or not text.strip():
        raise JSONFixError("empty reply")
    for cand in _candidates(text):
        for variant in (cand, repair(cand), repair(cand.translate(_SMART)), repair(_close_truncated(cand))):
            try:
                val = json.loads(variant)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(val, (dict, list)):
                return val
    raise JSONFixError(f"no JSON object found in reply: {text.strip()[:120]!r}")


def parse_model(text: str, schema: type[T]) -> T:
    """extract_json + schema validation. Unwraps {"result": {...}} style wrappers and bare lists
    for single-list models (e.g. Proposal.actions). Raises JSONFixError or ValidationError."""
    data = extract_json(text)
    try:
        return schema.model_validate(data)
    except ValidationError as first:
        alts: list[Any] = []
        if isinstance(data, dict) and len(data) == 1:
            inner = next(iter(data.values()))
            if isinstance(inner, dict):
                alts.append(inner)
        if isinstance(data, list):
            alts += [{name: data} for name in schema.model_fields]
            alts += [d for d in data if isinstance(d, dict)][:1]
        for alt in alts:
            try:
                return schema.model_validate(alt)
            except ValidationError:
                continue
        raise first
