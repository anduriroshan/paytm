# Munim (मुनीम): an AI teammate that fixes merchant money errors, safely

A merchant writes in Hindi, Telugu, Hinglish or English, for example *"I returned the Soundbox in September, ₹499 is still being deducted"*.
Munim then works the case:

1. **Investigates like a detective**:
   - It checks the complaint for fake instructions (Sarvam Prism).
   - It follows the money trail through the ledger, mandates, subscriptions, devices, pickups and payments.
   - It checks the merchant's statements against the evidence.
   - It lines up known failure patterns. Each one is **matched** (smoking gun) or **eliminated** (alibi).
   - When nothing fits, it improvises read-only SQL probes.
2. **Proposes** a fix with an LLM that has no authority and no write tools.
3. **Decides** with a deterministic **Action Firewall**. It recomputes every rupee from the ledger, binds the action to the evidence, and checks idempotency, preconditions, ₹ limits and hard denies. Each action ends as ALLOW / CORRECTED / NEEDS_HUMAN / BLOCK.
4. **Executes** or queues the action for a human, and replies in the merchant's language.
5. **Sweeps the fleet.** One complaint finds every other merchant hit by the same bug, and the fixes go to a human for batch approval.

> *LLM proposes. Rules dispose.*

## Quick start

```powershell
cd munim
.\run.ps1 -Reset          # seed demo DB, build UI if needed, serve on http://127.0.0.1:8000
.\run.ps1 -Mock           # offline scripted brain: no keys, no credits, works without Wi-Fi
```
- Backup on stage, with no backend at all: `http://127.0.0.1:8000/?replay=s1_zombie_rent`. It replays a recorded run.
- To develop the UI with hot reload, run `cd frontend; npm run dev`. That serves `http://localhost:5173`, which proxies `/api` to `:8000`.

### Going live (keys)
Copy `backend/.env.example` to `backend/.env`, then fill in:

| key | used for | where |
|---|---|---|
| `GEMINI_API_KEY` | reasoner: intake, propose, improvise (JSON) | Google AI Studio. Cloud credits apply when the key's project is linked to billing |
| `SARVAM_API_KEY` | writer (vernacular replies), decider (Prism fake-instruction score), voice | dashboard.sarvam.ai |

Check them with `backend\.venv\Scripts\python.exe backend\scripts\check_providers.py`. Add `--ping` for one tiny call per role.
Any role without a key falls back to the mock brain. Any live failure (network, 402 no credits, 429, bad JSON) also falls back for that call, and the UI shows a `fallback` badge, so the demo never crashes.

Change the routing in `.env` without touching code. Each role is set as `provider:model`:
```
MUNIM_REASONER=gemini:gemini-flash-latest
MUNIM_WRITER=sarvam:sarvam-105b
MUNIM_DECIDER=sarvam:sarvam-prism      # or: llm | mock
MUNIM_REASONING_EFFORT=low             # reasoner (JSON tasks)
MUNIM_WRITER_REASONING_EFFORT=none     # replies: sarvam-105b otherwise thinks ~25 s and can run out of tokens
```
Verified live on 13 Nov 2026:
- Gemini handles intake and propose in about 3 s.
- Prism returns the fake-instruction score in about 0.3 s.
- Sarvam writes the Hindi reply in about 1 s.
- TTS uses `bulbul:v4-flash` and STT uses `saaras:v4`.

`?replay=s1_live_gemini_sarvam` replays that real run offline.

## Architecture

```
START ─┬─ intake (LLM) ──┐
       └─ hunch (Prism) ─┴─ trail ─ statements ─ lineup ─┬────────────┬─ verdict ─ propose (LLM)
                                                          └─ improvise ┘  (only if no suspect matched)
  ─ firewall (code) ─ execute (code) ─ respond (LLM, vernacular) ─┬─ sweep ─ END  (fleet-wide fault found)
                                                                   └───────── END
```
- **Backend:** Python 3.14, FastAPI, LangGraph (custom stream writer → SSE), SQLite (WAL), Pydantic.
- **Frontend:** React, Vite, TypeScript and Tailwind v4. The UI is a pure event reducer, so live and replay modes are the same code.
- **Safety in the database:**
  - `ledger_entries.refund_of UNIQUE` means no row can ever be refunded twice.
  - `actions.idempotency_key UNIQUE` stops the same action executing twice.
  - Probes run on a read-only connection with an authorizer allowlist, are scoped to the merchant, and have time and row caps.
- **Where Munim looks:** `backend/sources/*.yaml`. Each file names a system, what it holds, when to reach it (topic, claim or words), and the fixes it carries out. Add a file to connect a new system, or set `enabled: false` to disconnect one: no restart needed. The app's **Sources** page lists them.
- **No MCP servers needed.** Everything runs locally, and the LLM providers use plain HTTPS.

| path | what |
|---|---|
| `backend/munim/graph/` | LangGraph nodes, graph and the runner (event buffer, SSE fan-out) |
| `backend/munim/forensics/` | trail, statements, suspect catalogue (lineup), verdict, guarded SQL probes |
| `backend/munim/llm/` | provider-agnostic router (Gemini / Sarvam / OpenAI-compatible / mock), prompts, Prism fake-instruction check (`hunch` node) |
| `backend/munim/firewall/` | Action Firewall policy (11 checks) |
| `backend/munim/actions/` | executor, approvals, fleet sweep |
| `backend/munim/db/` | schema, demo seed (9 scenarios + 180 background merchants) |
| `backend/sources/` | **connected sources**: one YAML file per system Munim can reach (database, CSV/JSON file, API), and when. See its README |
| `backend/munim/sources/` | loads that folder, routes a complaint to sources, reads files and APIs |
| `frontend/` | the live investigation UI |
| `docs/CONTRACTS.md` | the module contracts · `docs/DEMO.md`: the stage script |

## Tests
```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q
```
The tests make no network calls and spend no credits. They cover every scenario end to end through the real graph, the firewall rules (correction, double refund, RISK-hold hard deny, injection), the probe sandbox, connected sources (routing, broken files, API keys, a turned-off source), and the LLM fallbacks.

All demo data is fictional.
