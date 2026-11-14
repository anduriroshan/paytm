// What Munim can reach, and when: one card per file in backend/sources/. Read-only; edit the YAML files to change it.
import { Database, FilePlus, LoaderCircle, RotateCcw, TriangleExclamationPoint } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { actionLabel } from "../format";
import type { SourceListing, SourcesList } from "../types";
import { KIND_ICON } from "./WorkPanel";

const KIND_LABEL: Record<string, string> = { database: "Database", file: "File", api: "API" };
const WORDS_SHOWN = 4;

const lower = (code: string) => code.toLowerCase().replace(/_/g, " ");

function whenParts(w: SourceListing["when"]): string[] {
  if (w.always) return ["every case"];
  const out = [
    ...w.topics.map((t) => `${lower(t)} complaint`),
    ...w.claims.map((c) => `claim: ${lower(c)}`),
  ];
  if (w.words.length) {
    const more = w.words.length - WORDS_SHOWN;
    out.push(`words: ${w.words.slice(0, WORDS_SHOWN).join(", ")}${more > 0 ? ` +${more}` : ""}`);
  }
  return out;
}

function reads(s: SourceListing): string {
  if (s.kind === "database") return s.tables.join(" · ");
  if (s.kind === "file") return s.detail.path ?? "";
  const h = s.detail.headers?.length ? ` · headers: ${s.detail.headers.join(", ")}` : "";
  return `GET ${s.detail.url ?? ""}${h}`;
}

function Card({ s }: { s: SourceListing }) {
  const Icon = KIND_ICON[s.kind] ?? Database;
  const state = !s.enabled ? { cls: "gray", text: "Off" } : s.warnings.length ? { cls: "warn", text: "Check" } : { cls: "ok", text: "On" };
  return (
    <div className={`ncard scard${s.enabled ? "" : " dim"}`}>
      <div className="nhead">
        <span className="ic code">
          <Icon className="ico" aria-hidden />
        </span>
        <div>
          <b>{s.name}</b>
          <small>{s.holds}</small>
        </div>
        <span className="spacer" />
        <span className="chip" title={`backend/sources/${s.file}`}>
          {s.file}
        </span>
        <span className={`pill ${state.cls}`}>{state.text}</span>
      </div>
      <dl className="sgrid">
        <dt>Reached when</dt>
        <dd>{whenParts(s.when).join(" · ") || "never (no rule set)"}</dd>
        <dt>{KIND_LABEL[s.kind] ?? s.kind}</dt>
        <dd className="mono">
          {reads(s)}
          {s.kind !== "database" && <span className="muted"> · max {s.max_rows} rows</span>}
        </dd>
        {s.actions.length > 0 && (
          <>
            <dt>Fixes</dt>
            <dd>
              {s.actions.map(actionLabel).join(" · ")}
              {!s.enabled && <span className="flag bad">refused while off</span>}
            </dd>
          </>
        )}
      </dl>
      {s.warnings.map((w) => (
        <div className="swarn" key={w}>
          <TriangleExclamationPoint className="ico" aria-hidden />
          {w}
        </div>
      ))}
    </div>
  );
}

export function Sources({ live }: { live: boolean }) {
  const [data, setData] = useState<SourcesList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    if (!live) return;
    setLoading(true);
    setError(null);
    api
      .sources()
      .then(setData)
      .catch((e: unknown) =>
        setError(
          e instanceof ApiError && e.status === 404
            ? "The backend is older than this page. Restart it to see the sources."
            : e instanceof Error
              ? e.message
              : "Could not load the sources",
        ),
      )
      .finally(() => setLoading(false));
  }, [live]);

  useEffect(load, [load]);

  const on = data?.sources.filter((s) => s.enabled).length ?? 0;
  return (
    <div className="needswrap scroll">
      <div className="needs">
        <div className="shead">
          <div>
            <h1>Sources</h1>
            <p>What Munim can reach, and when. One file per system.</p>
          </div>
          <span className="spacer" />
          {live && (
            <button type="button" className="btn" onClick={load} disabled={loading} title="Read the folder again">
              {loading ? <LoaderCircle className="ico spin" aria-hidden /> : <RotateCcw className="ico" aria-hidden />}
              Refresh
            </button>
          )}
        </div>

        {!live && <div className="empty">Needs the live backend.</div>}
        {error && <div className="ok-line bad">{error}</div>}

        {data && (
          <>
            {data.problems.map((p) => (
              <div className="ok-line bad" key={p}>
                <TriangleExclamationPoint className="ico" aria-hidden />
                {p}
              </div>
            ))}
            <h3>
              {on} on · {data.sources.length - on} off
            </h3>
            {data.sources.map((s) => (
              <Card key={s.id} s={s} />
            ))}
            <div className="ncard dim sadd">
              <FilePlus className="ico" aria-hidden />
              <span>
                Add a system: copy <code>_template.yaml</code> in <code>{data.dir}</code> and fill it in. No restart needed.
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
