// Merchant app client (/api/me/*). The shop comes from the signed token, never from what the page sends.
import { ApiError } from "../api";

export interface Shop {
  id: string;
  business_name: string;
  owner_name: string;
  city: string;
  language: string;
  category: string;
  phone?: string;
}

export interface Step {
  label: string;
  date?: string;
  state: "done" | "now" | "next";
}

export interface Msg {
  text: string;
  en: string;
  language: string;
  kind?: string;
  guarded?: boolean;
}

export interface CaseCard {
  id: string;
  status: string;
  label: string;
  tone: "run" | "ok" | "hum" | "esc";
  opened: string;
  text: string;
  from_app: boolean;
  done: boolean;
  steps: Step[];
  progress?: Step[];
  reply?: Msg | null;
}

export interface Hold {
  id: string;
  kind: string;
  reason: string;
  since: string;
  payouts_held: number;
  amount: string;
  fix: string;
}

export interface Payout {
  id: string;
  date: string;
  amount: string;
  deductions: string;
  status: string;
  bank_ref: string;
}

export interface Device {
  id: string;
  serial: string;
  model: string;
  status: string;
  events: { what: string; date: string }[];
  pickups: { id: string; status: string; date: string; done: string; attempts: number; why_failed: string }[];
}

export interface Home {
  today: string;
  shop: Shop;
  payments: { date: string; label: string; count: number; total: string };
  payouts: Payout[];
  holds: Hold[];
  devices: Device[];
  plans: { plan: string; fee: string; status: string; since: string; ended: string }[];
  loans: { lender: string; daily_instalment: string; status: string; since: string }[];
  chargebacks: { id: string; amount: string; reason: string; status: string; raised: string; debited: string }[];
  passbook: { date: string; what: string; direction: string; amount: string; status: string; note: string; ref: string }[];
  issues: CaseCard[];
  suggestions: { label: string; text: string }[];
}

export type AskResult = { kind: "answer"; message: Msg; live: boolean } | { kind: "case"; case: CaseCard; message: Msg; live: boolean };

const TOKEN_KEY = "munim.merchant.token";
export const session = {
  get: () => sessionStorage.getItem(TOKEN_KEY),
  set: (t: string) => sessionStorage.setItem(TOKEN_KEY, t),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
};

async function call<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), init?.timeoutMs ?? 20000);
  const token = session.get();
  try {
    const res = await fetch(path, {
      ...init,
      signal: ctrl.signal,
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(init?.headers ?? {}) },
    });
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try {
        const j = await res.json();
        if (j && typeof j.detail === "string") msg = j.detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(res.status, msg);
    }
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

export const me = {
  shops: () => call<Shop[]>("/api/me/shops"),
  login: (merchant_id: string, otp: string) =>
    call<{ token: string; shop: Shop }>("/api/me/login", { method: "POST", body: JSON.stringify({ merchant_id, otp }) }),
  home: () => call<Home>("/api/me/home"),
  cases: () => call<CaseCard[]>("/api/me/cases"),
  case: (id: string) => call<CaseCard>(`/api/me/cases/${encodeURIComponent(id)}`),
  ask: (text: string) => call<AskResult>("/api/me/ask", { method: "POST", body: JSON.stringify({ text }), timeoutMs: 60000 }),
};
