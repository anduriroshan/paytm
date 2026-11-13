"""Record real event streams of the demo scenarios into frontend/src/fixtures/*.json (the UI's offline replay mode).

    .venv\\Scripts\\python.exe scripts\\record_fixtures.py            # uses whatever .env configures (live if keys are set)
    .venv\\Scripts\\python.exe scripts\\record_fixtures.py --mock     # scripted offline brain, no credits
    .venv\\Scripts\\python.exe scripts\\record_fixtures.py --only zombie_rent

Runs against a throw-away copy of the demo DB, so your working database is untouched.
Then open  http://127.0.0.1:8000/?replay=s1_zombie_rent  (after `npm run build`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from munim import config  # noqa: E402

# One recorded follow-up question per scenario, as the support teammate would ask it: in English, or the merchant's own
# words pasted in (then Munim also drafts a reply in that language). Replay holds these back after `done` and plays one
# only when the presenter clicks its suggestion chip. Keep in sync with frontend/src/lib/suggestions.ts.
FOLLOWUPS = {
    "zombie_rent": "Why wasn't the 1 Sep ₹499 refunded?",
    "double_billing": "డబ్బులు ఎప్పుడు వస్తాయి?",
    "duplicate_edi": "When will the money reach them?",
    "legit_rent": "Can we refund them anyway?",
    "risk_hold": "When will the hold be released?",
    "kyc_hold": "What does the merchant need to do?",
    "cold_case": "Paisa kab tak aayega?",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="force the offline mock brain")
    ap.add_argument("--only", help="record one scenario id")
    ap.add_argument("--no-chat", action="store_true", help="don't record a follow-up question")
    ap.add_argument("--out", default=str(config.BACKEND_DIR.parent / "frontend" / "src" / "fixtures"))
    args = ap.parse_args()

    if args.mock:
        config.FORCE_MOCK = True
    tmp = Path(tempfile.mkdtemp(prefix="munim_rec_")) / "rec.db"
    config.DB_PATH = tmp

    from munim.db.scenarios import SCENARIOS
    from munim.db.seed import reset_db
    from munim.graph import runner
    from munim.llm import router

    reset_db(tmp)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print("providers:", json.dumps(router.describe(), indent=None))

    async def go():
        for i, sc in enumerate(SCENARIOS, start=1):
            if args.only and sc.id != args.only:
                continue
            case_id, merchant = runner.create_case(sc.merchant_id, sc.complaint_text, sc.channel, sc.id)
            await runner.run_case(case_id, merchant, sc.complaint_text, sc.channel, sc.id)
            if not args.no_chat and sc.id in FOLLOWUPS:
                await runner.chat(case_id, FOLLOWUPS[sc.id])
            evs = [e.model_dump(mode="json") for e in runner.RUNS[case_id].events]
            name = f"s{i}_{sc.id}"
            (out / f"{name}.json").write_text(json.dumps(evs, ensure_ascii=False, indent=1), encoding="utf-8")
            done = next(e["data"] for e in evs if e["type"] == "done")
            print(f"{name:24s} {len(evs):4d} events  {done.get('outcome')!s:15s} {done.get('decision')!s:12s} {done.get('status')}")

    asyncio.run(go())


if __name__ == "__main__":
    main()
