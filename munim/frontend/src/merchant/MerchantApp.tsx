// Merchant app (?view=merchant): the shop owner's side of Munim, in a phone frame.
// Same engine as the team view; the server decides what this shop may see, do and be told (four gates, see backend/munim/access).
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ChevronRight,
  CircleCheck,
  ExternalLink,
  Eye,
  House,
  KeyRound,
  ListChecks,
  LoaderCircle,
  LogOut,
  MessageCircle,
  MessageSquareText,
  Mic,
  Send,
  ShieldCheck,
  Square,
  Truck,
  Wallet,
  Wrench,
} from "lucide-react";
import { api, ApiError } from "../api";
import { SpeakButton, useVoiceInput } from "../components/voice";
import { me, session, type CaseCard, type Home, type Msg, type Shop, type Step } from "./api";
import "./merchant.css";

type Tab = "home" | "chat" | "issues";
type Item = { k: "me"; text: string } | { k: "munim"; msg: Msg } | { k: "case"; id: string };

const firstName = (s: string | undefined) => (s ?? "").split(" ")[0] || "there";
const isEn = (lang: string | undefined) => !lang || lang === "en";

export default function MerchantApp() {
  const [shop, setShop] = useState<Shop | null>(null);
  const [tab, setTab] = useState<Tab>("home");
  const [home, setHome] = useState<Home | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [voice, setVoice] = useState(false);
  // chat lives here so it survives tab switches
  const [items, setItems] = useState<Item[]>([]);
  const [cards, setCards] = useState<Record<string, CaseCard>>({});

  const logout = useCallback((why?: string) => {
    session.clear();
    setShop(null);
    setHome(null);
    setItems([]);
    setCards({});
    setTab("home");
    setNotice(why ?? null);
  }, []);

  const guard = useCallback(
    async <T,>(p: Promise<T>): Promise<T | null> => {
      try {
        return await p;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) logout("Please log in again.");
        else setNotice((e as Error).message);
        return null;
      }
    },
    [logout],
  );

  const loadHome = useCallback(async () => {
    const h = await guard(me.home());
    if (h) {
      setHome(h);
      setShop(h.shop);
    }
  }, [guard]);

  useEffect(() => {
    if (session.get()) void loadHome();
    api
      .health()
      .then((h) => setVoice(h.providers?.voice?.live === true))
      .catch(() => setVoice(false));
  }, [loadHome]);

  // poll the issues opened from chat until each is done
  const pending = useMemo(() => items.filter((i): i is { k: "case"; id: string } => i.k === "case" && !cards[i.id]?.done).map((i) => i.id), [items, cards]);
  useEffect(() => {
    if (!pending.length) return;
    const t = setInterval(async () => {
      for (const id of pending) {
        const c = await guard(me.case(id));
        if (c) setCards((m) => ({ ...m, [id]: c }));
        if (c?.done) void loadHome();
      }
    }, 1200);
    return () => clearInterval(t);
  }, [pending, guard, loadHome]);

  const ask = async (text: string) => {
    setItems((x) => [...x, { k: "me", text }]);
    setTab("chat");
    const r = await guard(me.ask(text));
    if (!r) {
      setItems((x) => [...x, { k: "munim", msg: { text: "Sorry, that didn't go through. Please try again.", en: "", language: "en" } }]);
      return;
    }
    if (r.kind === "case") {
      setCards((m) => ({ ...m, [r.case.id]: r.case }));
      setItems((x) => [...x, { k: "munim", msg: r.message }, { k: "case", id: r.case.id }]);
    } else {
      setItems((x) => [...x, { k: "munim", msg: r.message }]);
    }
  };

  return (
    <div className="mx scroll">
      <div className="phone">
        {!shop ? (
          <Login
            notice={notice}
            onIn={(s) => {
              setNotice(null);
              setShop(s);
              void loadHome();
            }}
          />
        ) : (
          <>
            <header className="ph-head">
              <span className="mlogo">M</span>
              <div className="who">
                <b>{shop.business_name}</b>
                <span>
                  {shop.owner_name} · {shop.city}
                </span>
              </div>
              <button type="button" className="ph-icon" onClick={() => logout()} aria-label="Log out" title="Log out">
                <LogOut className="ico" aria-hidden />
              </button>
            </header>
            {notice && (
              <div className="ph-notice" role="status">
                {notice}
                <button type="button" onClick={() => setNotice(null)} aria-label="Dismiss">
                  ×
                </button>
              </div>
            )}
            <main className="ph-body scroll">
              {tab === "home" && <HomeScreen home={home} onAsk={ask} onTab={setTab} />}
              {tab === "chat" && <Chat shop={shop} home={home} items={items} cards={cards} voice={voice} onAsk={ask} />}
              {tab === "issues" && <Issues guard={guard} voice={voice} />}
            </main>
            <nav className="ph-tabs" aria-label="Sections">
              <TabBtn on={tab === "home"} onClick={() => setTab("home")} icon={<House className="ico" aria-hidden />} label="Home" />
              <TabBtn on={tab === "chat"} onClick={() => setTab("chat")} icon={<MessageCircle className="ico" aria-hidden />} label="Ask Munim" />
              <TabBtn
                on={tab === "issues"}
                onClick={() => setTab("issues")}
                icon={<ListChecks className="ico" aria-hidden />}
                label="My issues"
                badge={home?.issues.filter((i) => !["RESOLVED", "BLOCKED"].includes(i.status)).length}
              />
            </nav>
          </>
        )}
      </div>
      <SidePanel />
    </div>
  );
}

function TabBtn({ on, onClick, icon, label, badge }: { on: boolean; onClick: () => void; icon: ReactNode; label: string; badge?: number }) {
  return (
    <button type="button" className={`ph-tab${on ? " on" : ""}`} onClick={onClick} aria-current={on ? "page" : undefined}>
      {icon}
      <span>{label}</span>
      {!!badge && <i className="ph-badge">{badge}</i>}
    </button>
  );
}

// ───────────────────────── login ─────────────────────────

function Login({ notice, onIn }: { notice: string | null; onIn: (s: Shop) => void }) {
  const [shops, setShops] = useState<Shop[]>([]);
  const [pick, setPick] = useState<Shop | null>(null);
  const [otp, setOtp] = useState("");
  const [err, setErr] = useState<string | null>(notice);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    me.shops()
      .then(setShops)
      .catch((e) => setErr(`Can't reach Munim: ${(e as Error).message}`));
  }, []);

  const submit = async () => {
    if (!pick) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await me.login(pick.id, otp);
      session.set(r.token);
      onIn(r.shop);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="login-top">
        <span className="mlogo big">M</span>
        <h1>Munim</h1>
        <p>Your shop's payments, payouts and issues</p>
      </div>
      <h2>Pick your shop</h2>
      <div className="shoplist scroll" role="radiogroup" aria-label="Demo shops">
        {shops.map((s) => (
          <button
            key={s.id}
            type="button"
            role="radio"
            aria-checked={pick?.id === s.id}
            className={`shoprow${pick?.id === s.id ? " on" : ""}`}
            onClick={() => {
              setPick(s);
              setOtp("");
            }}
          >
            <b>{s.business_name}</b>
            <span>
              {s.owner_name} · {s.city}
            </span>
          </button>
        ))}
      </div>
      <div className="otp">
        {pick ? (
          <>
            <label htmlFor="otp">
              OTP sent to <b>{pick.phone}</b>
            </label>
            <div className="otprow">
              <input
                id="otp"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={otp}
                onChange={(e) => setOtp(e.target.value.replace(/\D/g, ""))}
                onKeyDown={(e) => e.key === "Enter" && otp.length === 6 && void submit()}
                placeholder="6 digits"
              />
              <button type="button" className="btn pri" disabled={otp.length !== 6 || busy} onClick={submit}>
                {busy ? <LoaderCircle className="ico spin" aria-hidden /> : <KeyRound className="ico" aria-hidden />}
                Log in
              </button>
            </div>
            <small>Demo OTP: 123456</small>
          </>
        ) : (
          <small>Demo: 8 fictional shops</small>
        )}
        {err && <div className="merr">{err}</div>}
      </div>
    </div>
  );
}

// ───────────────────────── home ─────────────────────────

function HomeScreen({ home, onAsk, onTab }: { home: Home | null; onAsk: (t: string) => void; onTab: (t: Tab) => void }) {
  if (!home) return <div className="loading"><LoaderCircle className="ico spin" aria-hidden /> Loading</div>;
  const last = home.payouts.find((p) => p.status === "Paid");
  const heldPayouts = home.payouts.filter((p) => p.status === "On hold");
  const dev = home.devices[0];
  const pickup = dev?.pickups[0];
  const open = home.issues.filter((i) => !["RESOLVED", "BLOCKED"].includes(i.status));
  return (
    <div className="home">
      <section className="hero">
        <span>Payments · {home.payments.label}</span>
        <b>{home.payments.total}</b>
        <small>
          {home.payments.count} payment{home.payments.count === 1 ? "" : "s"}
        </small>
      </section>

      {home.holds.map((h) => (
        <section key={h.id} className="card warn">
          <div className="card-h">
            <Wallet className="ico" aria-hidden />
            On hold · {h.amount}
          </div>
          <p>
            {h.payouts_held} payout{h.payouts_held === 1 ? "" : "s"} · {h.kind} since {h.since}
          </p>
          <p className="muted">{h.fix}</p>
          <button type="button" className="link" onClick={() => onAsk("My payout has not come. Where is my money?")}>
            Ask Munim <ChevronRight className="ico" aria-hidden />
          </button>
        </section>
      ))}

      {last && (
        <section className="card">
          <div className="card-h">
            <Wallet className="ico" aria-hidden />
            Last payout
          </div>
          <div className="kv">
            <b>{last.amount}</b>
            <span>{last.date}</span>
          </div>
          {last.deductions !== "₹0" && <p className="muted">After {last.deductions} cut (rent, instalments, chargebacks)</p>}
          {!!heldPayouts.length && !home.holds.length && <p className="muted">{heldPayouts.length} payout(s) on hold</p>}
        </section>
      )}

      {dev && (
        <section className="card">
          <div className="card-h">
            <Truck className="ico" aria-hidden />
            {dev.model} <span className="muted">{dev.serial}</span>
          </div>
          <p>
            {dev.status}
            {dev.events.length ? ` · ${dev.events[0].what} ${dev.events[0].date}` : ""}
          </p>
          {pickup && (
            <p className="muted">
              Pickup: {pickup.status}
              {pickup.attempts ? ` · ${pickup.attempts} attempt${pickup.attempts === 1 ? "" : "s"}` : ""}
              {pickup.why_failed ? ` · ${pickup.why_failed}` : ""}
            </p>
          )}
        </section>
      )}

      <section className="card">
        <div className="card-h">
          <ListChecks className="ico" aria-hidden />
          My issues
          {open.length > 0 && <span className="muted"> · {open.length} open</span>}
        </div>
        {home.issues.length === 0 && <p className="muted">None. Ask Munim if something looks wrong.</p>}
        {home.issues.slice(0, 3).map((c) => (
          <button key={c.id} type="button" className="issuerow" onClick={() => onTab("issues")}>
            <i className={`dot ${c.tone}`} aria-hidden />
            <span>{c.text}</span>
            <em className={`pill ${toneCls(c.tone)}`}>{c.label}</em>
          </button>
        ))}
      </section>

      <button type="button" className="btn pri wide" onClick={() => onTab("chat")}>
        <MessageCircle className="ico" aria-hidden />
        Ask Munim
      </button>
    </div>
  );
}

const toneCls = (t: CaseCard["tone"]) => (t === "ok" ? "ok" : t === "hum" ? "hum" : t === "esc" ? "warn" : "run");

// ───────────────────────── chat ─────────────────────────

function Bubble({ msg, voice }: { msg: Msg; voice: boolean }) {
  const en = !isEn(msg.language) && msg.en && msg.en !== msg.text;
  return (
    <div className="mrow mu">
      <span className="mav">M</span>
      <div className="mbub">
        <div className={isEn(msg.language) ? "" : "ind"}>{msg.text}</div>
        {en && <div className="en">{msg.en}</div>}
        {voice && !isEn(msg.language) && (
          <div className="speak">
            <SpeakButton text={msg.text} language={msg.language} />
          </div>
        )}
      </div>
    </div>
  );
}

function Steps({ steps, live }: { steps: Step[]; live?: boolean }) {
  return (
    <ol className={`steps${live ? " live" : ""}`}>
      {steps.map((s, i) => (
        <li key={i} className={s.state}>
          <i aria-hidden>{s.state === "done" ? <CircleCheck className="ico" /> : s.state === "now" ? <LoaderCircle className={`ico${live ? " spin" : ""}`} /> : null}</i>
          <span>{s.label}</span>
          {s.date && <em>{s.date}</em>}
        </li>
      ))}
    </ol>
  );
}

function CaseBubble({ card, voice }: { card: CaseCard | undefined; voice: boolean }) {
  if (!card) return null;
  return (
    <>
      <div className="mrow mu">
        <span className="mav ghost" />
        <div className="casebub">
          <div className="casebub-h">
            <b>Issue {card.id}</b>
            <em className={`pill ${toneCls(card.tone)}`}>{card.label}</em>
          </div>
          <Steps steps={card.done ? card.steps : (card.progress ?? card.steps)} live={!card.done} />
        </div>
      </div>
      {card.done && card.reply && <Bubble msg={card.reply} voice={voice} />}
    </>
  );
}

function Chat({ shop, home, items, cards, voice, onAsk }: { shop: Shop; home: Home | null; items: Item[]; cards: Record<string, CaseCard>; voice: boolean; onAsk: (t: string) => Promise<void> }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);
  const send = async (t: string) => {
    const q = t.trim();
    if (!q || busy) return;
    setText("");
    setBusy(true);
    try {
      await onAsk(q);
    } finally {
      setBusy(false);
    }
  };
  const mic = useVoiceInput((t) => setText(t));
  useEffect(() => {
    // scroll the phone body (not the window) so the sticky composer never covers the newest message
    const body = endRef.current?.closest(".ph-body");
    if (body) body.scrollTop = body.scrollHeight;
  }, [items, cards, busy]);

  return (
    <div className="mchat">
      <div className="msgs-m">
        <div className="mrow mu">
          <span className="mav">M</span>
          <div className="mbub">
            Hi {firstName(shop.owner_name)}. Ask me about payments, payouts, your Soundbox or an issue. I only see your shop's account.
          </div>
        </div>
        {items.map((it, i) =>
          it.k === "me" ? (
            <div key={i} className="mrow me">
              <div className="mbub">{it.text}</div>
            </div>
          ) : it.k === "munim" ? (
            <Bubble key={i} msg={it.msg} voice={voice} />
          ) : (
            <CaseBubble key={i} card={cards[it.id]} voice={voice} />
          ),
        )}
        {busy && (
          <div className="mrow mu">
            <span className="mav">M</span>
            <div className="mbub mtyping">
              <i />
              <i />
              <i />
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>
      <div className="composer-m">
        {!!home?.suggestions.length && (
          <div className="chips-m">
            {home.suggestions.map((s) => (
              <button key={s.label} type="button" disabled={busy} onClick={() => void send(s.text)}>
                {s.label}
              </button>
            ))}
          </div>
        )}
        <div className="inrow">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void send(text)}
            placeholder="Type in any language"
            maxLength={500}
            aria-label="Message"
          />
          {voice && (
            <button type="button" className={`ph-icon${mic.state === "recording" ? " rec" : ""}`} onClick={mic.toggle} aria-label={mic.state === "recording" ? "Stop" : "Speak"} disabled={mic.state === "transcribing"}>
              {mic.state === "recording" ? <Square className="ico" aria-hidden /> : mic.state === "transcribing" ? <LoaderCircle className="ico spin" aria-hidden /> : <Mic className="ico" aria-hidden />}
            </button>
          )}
          <button type="button" className="ph-send" onClick={() => void send(text)} disabled={!text.trim() || busy} aria-label="Send">
            <Send className="ico" aria-hidden />
          </button>
        </div>
        {mic.error && <div className="merr">{mic.error}</div>}
      </div>
    </div>
  );
}

// ───────────────────────── my issues ─────────────────────────

function Issues({ guard, voice }: { guard: <T>(p: Promise<T>) => Promise<T | null>; voice: boolean }) {
  const [list, setList] = useState<CaseCard[] | null>(null);
  useEffect(() => {
    let stop = false;
    const load = async () => {
      const r = await guard(me.cases());
      if (!stop && r) setList(r);
    };
    void load();
    const t = setInterval(load, 3000);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [guard]);
  if (!list) return <div className="loading"><LoaderCircle className="ico spin" aria-hidden /> Loading</div>;
  if (!list.length) return <div className="empty-m">No issues yet. Ask Munim if something looks wrong.</div>;
  return (
    <div className="issues">
      {list.map((c) => (
        <section key={c.id} className="card">
          <div className="casebub-h">
            <b>{c.id}</b>
            <span className="muted">{c.opened}</span>
            <em className={`pill ${toneCls(c.tone)}`}>{c.label}</em>
          </div>
          <p className="quote">“{c.text}”</p>
          <Steps steps={c.done ? c.steps : (c.progress ?? c.steps)} live={!c.done} />
          {c.reply && (
            <div className="lastmsg">
              <div className={isEn(c.reply.language) ? "" : "ind"}>{c.reply.text}</div>
              {!isEn(c.reply.language) && c.reply.en && c.reply.en !== c.reply.text && <div className="en">{c.reply.en}</div>}
              {voice && !isEn(c.reply.language) && <SpeakButton text={c.reply.text} language={c.reply.language} />}
            </div>
          )}
        </section>
      ))}
    </div>
  );
}

// ───────────────────────── side panel (for the demo audience) ─────────────────────────

const GATES = [
  { icon: KeyRound, name: "Who", text: "The login decides the shop. A shop id typed in chat is ignored." },
  { icon: Eye, name: "See", text: "Only this shop's data. Safety-review reasons hidden, refs masked." },
  { icon: Wrench, name: "Do", text: "Books pickups and sends KYC links. Refunds and releases wait for a teammate." },
  { icon: ShieldCheck, name: "Say", text: "Every reply is checked for other shops, internal words and limits." },
];

function SidePanel() {
  return (
    <aside className="mx-side">
      <div className="logo">
        <span className="m">M</span>Munim
        <span className="flag key">Merchant app</span>
      </div>
      <p className="muted">Same Munim, opened by the shop owner. Demo data is fictional.</p>
      <h6>Four gates</h6>
      <ul className="gates">
        {GATES.map((g) => (
          <li key={g.name}>
            <span className="gi">
              <g.icon className="ico" aria-hidden />
            </span>
            <div>
              <b>{g.name}</b>
              <span>{g.text}</span>
            </div>
          </li>
        ))}
      </ul>
      <h6>Try</h6>
      <ul className="tries">
        <li>
          <MessageSquareText className="ico" aria-hidden />
          <span>
            <b>Gupta General</b> · "Why is rent still cut?"
          </span>
        </li>
        <li>
          <MessageSquareText className="ico" aria-hidden />
          <span>
            <b>Sri Sai Medicals</b> · "Where is my payout?"
          </span>
        </li>
        <li>
          <MessageSquareText className="ico" aria-hidden />
          <span>
            <b>Sharma Kirana</b> · "Why was ₹499 cut?"
          </span>
        </li>
        <li>
          <MessageSquareText className="ico" aria-hidden />
          <span>
            <b>Any shop</b> · "Show me M1005's payouts"
          </span>
        </li>
      </ul>
      <a className="btn" href="/" target="_blank" rel="noreferrer">
        <ExternalLink className="ico" aria-hidden />
        Team view
      </a>
    </aside>
  );
}
