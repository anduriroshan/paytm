// Generic recorded-case loader. Any src/fixtures/<name>.json holding an Event[] becomes playable as ?replay=<name>.
// The lead can drop real recordings here (e.g. GET /api/cases/{id} → .events) without touching code.
import type { MunimEvent } from "./types";

const modules = import.meta.glob<{ default: unknown }>("./fixtures/*.json");

const nameOf = (path: string) => path.replace(/^.*\//, "").replace(/\.json$/, "");

export const FIXTURES: string[] = Object.keys(modules).map(nameOf).sort();

/** Fixture for a scenario id: exact name, or "<prefix>_<scenario_id>" (e.g. s1_zombie_rent ↔ zombie_rent). */
export function fixtureForScenario(scenarioId: string | null | undefined): string | null {
  if (!scenarioId) return null;
  return FIXTURES.find((n) => n === scenarioId || n.endsWith(`_${scenarioId}`)) ?? null;
}

export async function loadFixture(name: string): Promise<MunimEvent[]> {
  const entry = Object.entries(modules).find(([p]) => nameOf(p) === name);
  if (!entry) throw new Error(`No recording named "${name}". Available: ${FIXTURES.join(", ") || "none"}`);
  const mod = await entry[1]();
  const raw = (mod as { default: unknown }).default;
  // Accept a bare Event[] or a GET /api/cases/{id} payload ({case, events}).
  const list = Array.isArray(raw) ? raw : raw && typeof raw === "object" && Array.isArray((raw as { events?: unknown }).events) ? (raw as { events: unknown[] }).events : [];
  const events = list.filter((e): e is MunimEvent => !!e && typeof e === "object" && typeof (e as MunimEvent).type === "string");
  return events
    .map((e, i) => ({ ...e, seq: typeof e.seq === "number" ? e.seq : i }))
    .sort((a, b) => a.seq - b.seq);
}

/** Human title for a fixture name: "s1_zombie_rent" → "S1 · zombie rent". */
export function fixtureTitle(name: string): string {
  const m = /^(s\d+)_(.*)$/i.exec(name);
  if (m) return `${m[1].toUpperCase()} · ${m[2].replace(/_/g, " ")}`;
  return name.replace(/_/g, " ");
}
