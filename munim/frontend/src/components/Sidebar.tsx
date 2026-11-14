// Left sidebar: new chat, the chat list, "Needs you", and the footer (which brain is running, reset, replay controls).
import { Inbox, Pause, Play, Plug, Plus, RotateCcw, Smartphone, UserRound, X } from "lucide-react";
import type { Tone } from "../lib/explain";
import { fixtureTitle } from "../replay";
import type { ReplayControls } from "../state/useCaseSession";

export interface SideItem {
  id: string;
  name: string;
  sub: string;
  tone: Tone;
  /** opened by the merchant in the merchant app */
  app?: boolean;
}

interface Props {
  items: SideItem[];
  activeId: string | null;
  onOpen: (id: string) => void;
  onNewChat: () => void;
  page: "inbox" | "chat" | "needs" | "sources";
  onInbox: () => void;
  /** new complaints + replies waiting for Send */
  inboxCount: number;
  onNeeds: () => void;
  onSources: () => void;
  needsCount: number;
  brain: { label: string; live: boolean };
  onReset: (() => void) | null;
  replay: ReplayControls | null;
  controls: { play: () => void; pause: () => void; restart: () => void; setSpeed: (n: number) => void } | null;
  onExitReplay: () => void;
}

const SPEEDS = [1, 2, 4];

export function Sidebar({ items, activeId, onOpen, onNewChat, page, onInbox, inboxCount, onNeeds, onSources, needsCount, brain, onReset, replay, controls, onExitReplay }: Props) {
  return (
    <aside className="side" aria-label="Chats">
      <div className="logo">
        <span className="m">M</span>Munim
      </div>
      <button type="button" className="newchat" onClick={onNewChat}>
        <Plus className="ico" aria-hidden />
        New chat
      </button>

      <div className="navsec grow">
        <h6>Chats</h6>
        <div className="chatlist scroll">
          {items.length === 0 && <div className="emptynav">No chats yet</div>}
          {items.map((c) => (
            <button key={c.id} type="button" className={`casei${page === "chat" && c.id === activeId ? " on" : ""}`} onClick={() => onOpen(c.id)}>
              <i className={`dot ${c.tone}`} aria-hidden />
              <div>
                <b>
                  {c.name}
                  {c.app && (
                    <span className="flag key apptag" title="Opened by the merchant in the merchant app">
                      App
                    </span>
                  )}
                </b>
                <span>{c.sub}</span>
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="navsec">
        <button type="button" className={`navi${page === "inbox" ? " on" : ""}`} onClick={onInbox} title="Every complaint, by source and status">
          <Inbox className="ico" aria-hidden />
          Inbox
          <span className={`badge${inboxCount ? "" : " zero"}`}>{inboxCount}</span>
        </button>
        <button type="button" className={`navi${page === "needs" ? " on" : ""}`} onClick={onNeeds}>
          <UserRound className="ico" aria-hidden />
          Needs you
          <span className={`badge${needsCount ? "" : " zero"}`}>{needsCount}</span>
        </button>
        <button type="button" className={`navi${page === "sources" ? " on" : ""}`} onClick={onSources} title="What Munim can reach, and when">
          <Plug className="ico" aria-hidden />
          Sources
        </button>
        <a className="navi" href="?view=merchant" target="_blank" rel="noreferrer" title="The shop owner's side of Munim (opens in a new tab)">
          <Smartphone className="ico" aria-hidden />
          Merchant app
        </a>
      </div>

      <div className="sidefoot">
        <span className={`live${brain.live ? "" : " gray"}`} title={brain.label}>
          <i />
          <span className="lbl">{brain.label}</span>
        </span>
        {replay && controls ? (
          <div className="rp">
            <button
              type="button"
              className="btn icon"
              onClick={() => (replay.finished ? controls.restart() : replay.playing ? controls.pause() : controls.play())}
              aria-label={replay.playing ? "Pause" : "Play"}
              title={replay.playing ? "Pause (Space)" : "Play (Space)"}
            >
              {replay.playing ? <Pause className="ico" aria-hidden /> : <Play className="ico" aria-hidden />}
            </button>
            <button type="button" className="btn icon" onClick={controls.restart} aria-label="Restart" title="Restart">
              <RotateCcw className="ico" aria-hidden />
            </button>
            <button
              type="button"
              className="btn spd"
              onClick={() => controls.setSpeed(SPEEDS[(SPEEDS.indexOf(replay.speed) + 1) % SPEEDS.length] ?? 1)}
              title="Playback speed"
            >
              {replay.speed}×
            </button>
            <span className="name" title={replay.name}>
              {fixtureTitle(replay.name)}
            </span>
            <span className="spacer" />
            <button type="button" className="textbtn" onClick={onExitReplay} aria-label="Leave the recording" title="Leave the recording">
              <X className="ico" aria-hidden />
            </button>
          </div>
        ) : (
          onReset && (
            <div className="footrow">
              <button type="button" className="textbtn" onClick={onReset} title="Wipe all cases and reseed the demo data">
                <RotateCcw className="ico" aria-hidden />
                Reset demo
              </button>
            </div>
          )
        )}
        {replay?.error && <div className="footrow bad">{replay.error}</div>}
      </div>
    </aside>
  );
}
