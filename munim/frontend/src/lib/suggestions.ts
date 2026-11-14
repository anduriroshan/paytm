// Short English labels for the demo complaints (the complaint itself stays in the merchant's language),
// and suggested questions for a finished case. The teammate asks in English, or pastes the merchant's own words.

/** Start-screen chips, in display order. The first four show; the rest sit behind "More". */
export const START_CHIPS: ReadonlyArray<{ scenario: string; label: string }> = [
  { scenario: "zombie_rent", label: "Returned the Soundbox, still charged" },
  { scenario: "double_billing", label: "Charged for 2 devices" },
  { scenario: "duplicate_edi", label: "Loan EDI cut twice" },
  { scenario: "risk_hold", label: "Payment on hold" },
  { scenario: "legit_rent", label: "Charged after returning in Oct" },
  { scenario: "stuck_return", label: "Nobody came to collect the Soundbox" },
  { scenario: "kyc_hold", label: "Settlement not received" },
  { scenario: "prompt_injection", label: "Fake admin asks ₹50,000" },
  { scenario: "cold_case", label: "₹2,340 missing from settlement" },
  { scenario: "bus_turned_back", label: "Bus turned back, no refund" },
  { scenario: "flight_refund", label: "Airline refund never arrived" },
];
export const START_CHIPS_SHOWN = 4;

export interface FollowUp {
  /** What gets sent: the teammate's English question, or the merchant's words in their language. */
  text: string;
  /** English label on the chip. */
  label: string;
  /** Set only for the merchant's own words (the chip shows a language tag). */
  language: string | null;
}

/** The merchant asking about their money, as they would write it. */
const WHEN_MONEY: Record<string, string> = {
  hi: "पैसा कब तक आएगा?",
  te: "డబ్బులు ఎప్పుడు వస్తాయి?",
  "hi-Latn": "Paisa kab tak aayega?",
  en: "When will I get my money?",
};
const whenMoneyLabel = (who: string) => `${who}: when's my money?`;

/** The teammate's own questions (English). Keep in sync with backend/scripts/record_fixtures.py FOLLOWUPS. */
const PER_SCENARIO: Record<string, FollowUp[]> = {
  zombie_rent: [{ text: "Why wasn't the 1 Sep ₹499 refunded?", label: "Why not the 1 Sep ₹499?", language: null }],
  duplicate_edi: [{ text: "When will the money reach them?", label: "When will the money reach them?", language: null }],
  legit_rent: [{ text: "Can we refund them anyway?", label: "Can we refund anyway?", language: null }],
  risk_hold: [{ text: "When will the hold be released?", label: "When is the hold released?", language: null }],
  kyc_hold: [{ text: "What does the merchant need to do?", label: "What must the merchant do?", language: null }],
};

/** Suggested questions for a case, minus the ones already asked. */
export function followUps(scenarioId: string | null | undefined, language: string | null | undefined, asked: string[], opts: { whenMoney: boolean; who?: string }): FollowUp[] {
  const lang = language && WHEN_MONEY[language] ? language : "en";
  const list = [...(PER_SCENARIO[scenarioId ?? ""] ?? [])];
  if (opts.whenMoney) list.push({ text: WHEN_MONEY[lang], label: whenMoneyLabel(opts.who ?? "Merchant"), language: lang });
  const seen = new Set(asked.map((a) => a.trim()));
  return list.filter((f) => !seen.has(f.text.trim()));
}

/** Chip label for a recorded question (replays): the known label, else its English. */
export function followUpLabel(question: string, en: string | null, language: string | null, who = "Merchant"): string {
  const q = question.trim();
  const known = Object.values(PER_SCENARIO).flat().find((f) => f.text.trim() === q);
  if (known) return known.label;
  if (Object.values(WHEN_MONEY).some((t) => t.trim() === q)) return whenMoneyLabel(who);
  return en && language && !language.toLowerCase().startsWith("en") ? `${who}: ${en}` : (en ?? question);
}
