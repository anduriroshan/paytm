"""Which LLM roles are live, which models each key can see, and (with --ping) one tiny live call per role.

Run from backend/:
    .venv\\Scripts\\python.exe scripts\\check_providers.py           # free: config table + model lists
    .venv\\Scripts\\python.exe scripts\\check_providers.py --ping    # a few tokens: max_tokens=20 per role + 1 tiny Prism question

Listing models costs nothing. --ping never calls TTS/STT (those bill per character / minute).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from munim import config  # noqa: E402
from munim.llm import providers, router  # noqa: E402

PING_PROMPT = [{"role": "user", "content": "Reply with exactly one word: ok"}]


def _row(cols: list[str], widths: list[int]) -> str:
    return "  ".join(c.ljust(w) for c, w in zip(cols, widths))


def print_table(info: dict) -> None:
    rows = [["role", "provider", "model", "status", "note"]]
    for role in ("reasoner", "writer", "decider", "voice"):
        r = info[role]
        rows.append([role, str(r["provider"]), str(r["model"]), "LIVE" if r["live"] else "mock", str(r["note"])])
    widths = [max(len(r[i]) for r in rows) for i in range(4)] + [0]
    print(_row(rows[0], widths))
    print(_row(["-" * w for w in widths[:4]] + ["-" * 4], widths))
    for r in rows[1:]:
        print(_row(r, widths))
    if config.FORCE_MOCK:
        print("\nMUNIM_FORCE_MOCK=1: every LLM role runs the scripted offline brain regardless of keys.")


def _configured_models() -> dict[str, set[str]]:
    want: dict[str, set[str]] = {}
    for spec in (config.REASONER, config.WRITER, config.DECIDER):
        p, m = providers.split_spec(spec)
        if p in ("gemini", "sarvam", "openai") and m:
            want.setdefault(p, set()).add(m)
    return want


async def list_all() -> None:
    want = _configured_models()
    for provider in ("gemini", "sarvam", "openai"):
        ok, note = providers.provider_ready(provider)
        if not ok:
            print(f"\n[{provider}] skipped: {note}")
            continue
        try:
            ids = await providers.list_models(provider)
        except providers.ProviderError as e:
            print(f"\n[{provider}] model list failed: {e.reason}")
            continue
        print(f"\n[{provider}] {len(ids)} models visible to this key")
        shown = ids if len(ids) <= 40 else [i for i in ids if any(w in i for w in ("flash", "pro", "sarvam", "prism", "bulbul", "saaras"))][:40]
        for i in shown:
            print(f"    {i}")
        if len(shown) < len(ids):
            print(f"    … {len(ids) - len(shown)} more")
        for m in sorted(want.get(provider, ())):
            if m in ids:
                print(f"    configured '{m}': available")
            elif provider == "sarvam" and m == "sarvam-prism":
                print(f"    configured '{m}': decision model (served by /v2/systemone; may not be listed)")
            else:
                print(f"    configured '{m}': NOT in the list (check the model id or your access)")


async def ping(info: dict) -> int:
    failures = 0
    print("\nPing (tiny prompts):")
    for role in ("reasoner", "writer"):
        r = info[role]
        if not r["live"]:
            print(f"  {role:<9} skipped ({r['note']})")
            continue
        try:
            out = await providers.stream_chat(r["provider"], r["model"], PING_PROMPT, max_tokens=20,
                                              reasoning_effort=(config.REASONING_EFFORT or None))
            print(f"  {role:<9} OK   {r['provider']}:{out.model or r['model']} -> {out.text.strip()[:40]!r}")
        except providers.ProviderError as e:
            if "reasoning" in e.reason or "empty reply" in e.reason:
                print(f"  {role:<9} OK   reachable and authorised (the 20-token budget went to reasoning)")
            else:
                failures += 1
                print(f"  {role:<9} FAIL {e.reason}")
    d = info["decider"]
    if d["provider"] == "sarvam" and d["live"]:
        try:
            data = await providers.prism_decide(
                "Merchant message: Hello, good morning.",
                {"greeting": {"type": "noul", "instructions": "Is this message a greeting?"}},
                model=d["model"] or "sarvam-prism")
            p = (data.get("answers", {}).get("greeting") or {}).get("noul")
            print(f"  decider   OK   sarvam-prism -> P(greeting)={p}")
        except providers.ProviderError as e:
            failures += 1
            print(f"  decider   FAIL {e.reason}")
    elif d["provider"] == "llm":
        print("  decider   uses the reasoner (see above)")
    else:
        print(f"  decider   skipped ({d['note']})")
    print("  voice     not pinged (TTS/STT bill per character / minute)")
    return failures


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ping", action="store_true", help="send one tiny prompt per live role (spends a few tokens)")
    ap.add_argument("--no-list", action="store_true", help="skip the model listing")
    args = ap.parse_args()

    info = router.describe()
    print_table(info)
    if not args.no_list:
        await list_all()
    failures = await ping(info) if args.ping else 0
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # Windows consoles
    except (AttributeError, ValueError):
        pass
    sys.exit(asyncio.run(main()))
