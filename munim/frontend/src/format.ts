// Formatting helpers. rupees() matches backend munim.models.rupees() exactly.

/** ₹1,23,456 style (Indian grouping), no decimals when whole. null/undefined → "—". */
export function rupees(paise: number | null | undefined): string {
  if (paise === null || paise === undefined || !Number.isFinite(paise)) return "—";
  const neg = paise < 0;
  const p = Math.abs(Math.trunc(paise));
  const r = Math.floor(p / 100);
  const ps = p % 100;
  let s = String(r);
  if (s.length > 3) {
    let head = s.slice(0, -3);
    const tail = s.slice(-3);
    const groups: string[] = [];
    while (head.length > 2) {
      groups.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) groups.unshift(head);
    s = [...groups, tail].join(",");
  }
  const out = `₹${s}` + (ps ? `.${String(ps).padStart(2, "0")}` : "");
  return neg ? `-${out}` : out;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-09-12" → "12 Sep 2026" ("01 Oct 2026", matching the backend's fmt_date); "2026-09" → "Sep 2026"; datetimes → "12 Sep 2026, 06:00". Parsed textually (no TZ shift). */
export function fmtDate(iso: string | null | undefined, opts: { time?: boolean } = {}): string {
  if (!iso || typeof iso !== "string") return "—";
  const m = /^(\d{4})-(\d{2})(?:-(\d{2}))?(?:[T ](\d{2}):(\d{2}))?/.exec(iso);
  if (!m) return iso;
  const [, y, mo, d, hh, mm] = m;
  const mon = MONTHS[Number(mo) - 1] ?? mo;
  if (!d) return `${mon} ${y}`;
  const base = `${d} ${mon} ${y}`;
  if (opts.time && hh !== undefined) return `${base}, ${hh}:${mm}`;
  return base;
}

/** "12 Sep" short form for dense places. */
export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return fmtDate(iso);
  return `${m[3]} ${MONTHS[Number(m[2]) - 1]}`;
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

export function pct(p: number | null | undefined, digits = 0): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return "—";
  return `${(p * 100).toFixed(digits)}%`;
}

export const LANGUAGES: Record<string, { label: string; native: string }> = {
  hi: { label: "Hindi", native: "हिन्दी" },
  "hi-Latn": { label: "Hinglish", native: "Hinglish" },
  te: { label: "Telugu", native: "తెలుగు" },
  en: { label: "English", native: "English" },
  ta: { label: "Tamil", native: "தமிழ்" },
  mr: { label: "Marathi", native: "मराठी" },
  bn: { label: "Bengali", native: "বাংলা" },
  kn: { label: "Kannada", native: "ಕನ್ನಡ" },
  gu: { label: "Gujarati", native: "ગુજરાતી" },
};

export function langLabel(code: string | null | undefined): string {
  if (!code) return "—";
  return LANGUAGES[code]?.label ?? code;
}

export const SUSPECT_TITLES: Record<string, string> = {
  ZOMBIE_BILLING: "Billing continued after device return",
  DUPLICATE_SUBSCRIPTION: "Two subscriptions on one device",
  DUPLICATE_EDI: "Loan instalment debited twice",
  PICKUP_NOT_COMPLETED: "Pickup never completed",
  ALREADY_REFUNDED: "Already refunded",
  RISK_HOLD: "Settlement held by risk team",
  KYC_HOLD: "Settlement held for KYC",
};

export function suspectTitle(id: string | null | undefined): string {
  if (!id) return "—";
  return SUSPECT_TITLES[id] ?? humanize(id);
}

export const ACTION_LABELS: Record<string, string> = {
  REFUND_DEBITS: "Refund debits",
  CANCEL_SUBSCRIPTION: "Cancel subscription",
  SCHEDULE_PICKUP: "Schedule pickup",
  SEND_KYC_LINK: "Send re-KYC link",
  RELEASE_HOLD: "Release hold",
  ESCALATE: "Escalate to team",
  NO_ACTION: "No action",
};

export function actionLabel(t: string | null | undefined): string {
  if (!t) return "—";
  return ACTION_LABELS[t] ?? humanize(t);
}

export const OUTCOME_LABELS: Record<string, string> = {
  REFUND_DUE: "Refund due",
  NO_FAULT: "No fault — charge is legitimate",
  HOLD_EXPLAINED: "Hold explained",
  IMPROVISED: "Improvised finding",
  NO_EVIDENCE: "No evidence",
  UNSOLVED: "Unsolved",
};

export function outcomeLabel(o: string | null | undefined): string {
  if (!o) return "—";
  return OUTCOME_LABELS[o] ?? humanize(o);
}

export const INTENT_LABELS: Record<string, string> = {
  RENT_DEDUCTION: "Device rent deduction",
  LOAN_DEDUCTION: "Loan EDI deduction",
  SETTLEMENT_ISSUE: "Settlement issue",
  OTHER: "Other",
};

export function humanize(s: string | null | undefined): string {
  if (!s) return "";
  const t = s.replace(/_/g, " ").toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/** Sort key for ISO-ish timestamps; missing → end. */
export function timeKey(iso: string | null | undefined): number {
  if (!iso) return Number.POSITIVE_INFINITY;
  const t = Date.parse(iso.length === 7 ? `${iso}-01` : iso);
  return Number.isFinite(t) ? t : Number.POSITIVE_INFINITY;
}

export function asArray<T>(v: T[] | null | undefined): T[] {
  return Array.isArray(v) ? v : [];
}

export function claimValue(c: { date?: string | null; amount_paise?: number | null; count?: number | null }): string {
  const parts: string[] = [];
  if (c.amount_paise !== null && c.amount_paise !== undefined) parts.push(rupees(c.amount_paise));
  if (c.count !== null && c.count !== undefined) parts.push(`× ${c.count}`);
  if (c.date) parts.push(fmtDate(c.date));
  return parts.join(" · ");
}
