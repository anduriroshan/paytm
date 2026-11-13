// Owns the current case: a pure reducer fed either by the live SSE stream or by the offline replay player.
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { subscribeCase } from "../api";
import { loadFixture } from "../replay";
import type { MunimEvent } from "../types";
import { applyEvents, buildCaseView, initialCaseView, type CaseView } from "./caseReducer";

type SessionAction =
  | { type: "reset" }
  | { type: "events"; events: MunimEvent[] }
  | { type: "rebuild"; events: MunimEvent[] };

/** React reducer wrapper around the pure case reducer. */
export function sessionReducer(state: CaseView, action: SessionAction): CaseView {
  switch (action.type) {
    case "reset":
      return initialCaseView();
    case "events":
      return applyEvents(state, action.events);
    case "rebuild": {
      // Rebuild from the authoritative history, but keep events newer than it (if any).
      const rebuilt = buildCaseView(action.events);
      return rebuilt.lastSeq >= state.lastSeq ? rebuilt : state;
    }
    default:
      return state;
  }
}

export type StreamStatus = "idle" | "connecting" | "live" | "reconnecting" | "closed";

/** A follow-up chat turn recorded after the case finished; replayed when the user clicks its chip. */
export interface RecordedTurn {
  question: string;
  en: string | null;
  language: string | null;
  events: MunimEvent[];
}

/** Split a recording at `done`: the investigation plays on its own; later chat turns wait for a click. */
export function splitRecording(events: MunimEvent[]): { main: MunimEvent[]; turns: RecordedTurn[] } {
  const cut = events.findIndex((e) => e.type === "done");
  if (cut < 0) return { main: events, turns: [] };
  const main = events.slice(0, cut + 1);
  const turns: RecordedTurn[] = [];
  for (const ev of events.slice(cut + 1)) {
    const d = (ev.data ?? {}) as Record<string, unknown>;
    if (ev.type === "message" && d.role !== "munim" && d.kind === "chat") {
      turns.push({
        question: typeof d.text === "string" ? d.text : "",
        en: typeof d.en === "string" ? d.en : null,
        language: typeof d.language === "string" ? d.language : null,
        events: [ev],
      });
    } else if (turns.length) turns[turns.length - 1].events.push(ev);
    else main.push(ev); // e.g. an approval recorded right after done
  }
  return { main, turns: turns.filter((t) => t.question) };
}

export interface ReplayControls {
  name: string;
  total: number;
  index: number;
  playing: boolean;
  finished: boolean;
  speed: number;
  error: string | null;
}

/** Longest pause the player will honour between two recorded events (ms, before speed). */
const GAP_CAP_MS = 1400;
/** Events closer than this are applied in one batch. */
const BATCH_MS = 12;

function gapMs(prev: MunimEvent | undefined, next: MunimEvent | undefined): number {
  if (!prev || !next) return 0;
  const a = Date.parse(prev.at);
  const b = Date.parse(next.at);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return 25;
  return Math.min(GAP_CAP_MS, Math.max(0, b - a));
}

export function useCaseSession() {
  const [view, dispatch] = useReducer(sessionReducer, undefined, initialCaseView);
  const [mode, setMode] = useState<"idle" | "live" | "replay">("idle");
  const [stream, setStream] = useState<StreamStatus>("idle");
  const [replay, setReplay] = useState<ReplayControls | null>(null);
  const [turns, setTurns] = useState<RecordedTurn[]>([]);
  const [usedTurns, setUsedTurns] = useState<number[]>([]);
  const turnTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const unsubRef = useRef<(() => void) | null>(null);
  const eventsRef = useRef<MunimEvent[]>([]);
  const idxRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** When the pending tick is due (performance.now()); lets a late tick catch up instead of drifting. */
  const dueRef = useRef(0);
  const speedRef = useRef(1);
  const playingRef = useRef(false);
  /** Bumped on every stop/start so a slow fixture load can't start a second ticking chain. */
  const genRef = useRef(0);

  const stopTimer = () => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
  };

  const stopAll = useCallback(() => {
    genRef.current += 1;
    stopTimer();
    if (turnTimerRef.current) clearTimeout(turnTimerRef.current);
    turnTimerRef.current = null;
    playingRef.current = false;
    unsubRef.current?.();
    unsubRef.current = null;
  }, []);

  useEffect(() => stopAll, [stopAll]);

  // ── live ──
  const openLive = useCallback(
    (caseId: string) => {
      stopAll();
      setReplay(null);
      setTurns([]);
      setUsedTurns([]);
      setMode("live");
      dispatch({ type: "reset" });
      unsubRef.current = subscribeCase(caseId, {
        onEvents: (events) => dispatch({ type: "events", events }),
        onRebuild: (events) => dispatch({ type: "rebuild", events }),
        onStatus: (s) => setStream(s),
      });
    },
    [stopAll],
  );

  /** Append events fetched out-of-band (e.g. after an approval) — deduped by seq in the reducer. */
  const ingest = useCallback((events: MunimEvent[]) => dispatch({ type: "events", events }), []);

  const clear = useCallback(() => {
    stopAll();
    setReplay(null);
    setTurns([]);
    setUsedTurns([]);
    setMode("idle");
    setStream("idle");
    dispatch({ type: "reset" });
  }, [stopAll]);

  // ── replay ──
  const tick = useCallback(() => {
    timerRef.current = null;
    if (!playingRef.current) return;
    const evs = eventsRef.current;
    let i = idxRef.current;
    if (i >= evs.length) return;
    const batch: MunimEvent[] = [evs[i]];
    i += 1;
    let acc = 0;
    // Browsers throttle timers in background tabs (about 1/s); whatever fell due meanwhile goes in this batch.
    const late = Math.max(0, performance.now() - dueRef.current);
    while (i < evs.length) {
      const g = gapMs(evs[i - 1], evs[i]) / speedRef.current;
      if (acc + g > BATCH_MS + late) break;
      acc += g;
      batch.push(evs[i]);
      i += 1;
    }
    idxRef.current = i;
    dispatch({ type: "events", events: batch });
    const finished = i >= evs.length;
    setReplay((r) => (r ? { ...r, index: i, finished, playing: !finished } : r));
    if (finished) {
      playingRef.current = false;
      return;
    }
    // The batch was due at the first event's moment; the next event is `acc + gap` after that.
    const wait = Math.max(0, acc + gapMs(evs[i - 1], evs[i]) / speedRef.current - late);
    dueRef.current = performance.now() + wait;
    timerRef.current = setTimeout(tick, wait);
  }, []);

  const play = useCallback(() => {
    if (idxRef.current >= eventsRef.current.length) return;
    playingRef.current = true;
    setReplay((r) => (r ? { ...r, playing: true } : r));
    stopTimer();
    dueRef.current = performance.now() + 0;
    timerRef.current = setTimeout(tick, 0);
  }, [tick]);

  const pause = useCallback(() => {
    playingRef.current = false;
    stopTimer();
    setReplay((r) => (r ? { ...r, playing: false } : r));
  }, []);

  const restart = useCallback(() => {
    stopTimer();
    if (turnTimerRef.current) clearTimeout(turnTimerRef.current);
    turnTimerRef.current = null;
    setUsedTurns([]);
    idxRef.current = 0;
    dispatch({ type: "reset" });
    setReplay((r) => (r ? { ...r, index: 0, finished: false } : r));
    playingRef.current = true;
    setReplay((r) => (r ? { ...r, playing: true } : r));
    dueRef.current = performance.now() + 400;
    timerRef.current = setTimeout(tick, 400);
  }, [tick]);

  const skipToEnd = useCallback(() => {
    stopTimer();
    playingRef.current = false;
    const rest = eventsRef.current.slice(idxRef.current);
    idxRef.current = eventsRef.current.length;
    if (rest.length) dispatch({ type: "events", events: rest });
    setReplay((r) => (r ? { ...r, index: eventsRef.current.length, finished: true, playing: false } : r));
  }, []);

  const setSpeed = useCallback((speed: number) => {
    speedRef.current = speed > 0 ? speed : 1;
    setReplay((r) => (r ? { ...r, speed: speedRef.current } : r));
  }, []);

  const startReplay = useCallback(
    async (name: string, opts: { speed?: number; autoplay?: boolean } = {}) => {
      stopAll();
      const gen = genRef.current;
      setMode("replay");
      setStream("idle");
      dispatch({ type: "reset" });
      setTurns([]);
      setUsedTurns([]);
      speedRef.current = opts.speed && opts.speed > 0 ? opts.speed : speedRef.current;
      setReplay({ name, total: 0, index: 0, playing: false, finished: false, speed: speedRef.current, error: null });
      try {
        const all = await loadFixture(name);
        if (gen !== genRef.current) return; // superseded by a newer start/stop
        const { main: events, turns: recorded } = splitRecording(all);
        setTurns(recorded);
        eventsRef.current = events;
        idxRef.current = 0;
        setReplay({ name, total: events.length, index: 0, playing: false, finished: events.length === 0, speed: speedRef.current, error: null });
        if (opts.autoplay !== false && events.length) {
          playingRef.current = true;
          setReplay((r) => (r ? { ...r, playing: true } : r));
          dueRef.current = performance.now() + 500;
          timerRef.current = setTimeout(tick, 500);
        }
      } catch (e) {
        if (gen !== genRef.current) return;
        setReplay({ name, total: 0, index: 0, playing: false, finished: true, speed: speedRef.current, error: (e as Error).message });
      }
    },
    [stopAll, tick],
  );

  /** Replay a recorded follow-up: the question appears at once, the answer after a short pause. */
  const playTurn = useCallback((i: number) => {
    const t = turns[i];
    if (!t || usedTurns.includes(i)) return;
    setUsedTurns((u) => [...u, i]);
    const [q, ...rest] = t.events;
    dispatch({ type: "events", events: [q] });
    if (turnTimerRef.current) clearTimeout(turnTimerRef.current);
    turnTimerRef.current = setTimeout(() => {
      turnTimerRef.current = null;
      dispatch({ type: "events", events: rest });
    }, 1300 / Math.max(1, speedRef.current));
  }, [turns, usedTurns]);

  const replayTurns = turns.map((t, i) => ({ ...t, index: i, used: usedTurns.includes(i) }));

  return {
    view,
    mode,
    stream,
    replay,
    replayTurns,
    playTurn,
    openLive,
    ingest,
    clear,
    startReplay,
    replayControls: { play, pause, restart, skipToEnd, setSpeed },
  };
}

export type CaseSession = ReturnType<typeof useCaseSession>;
