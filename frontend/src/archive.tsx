import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "./api";
import { Badge, useToast } from "./components";
import { relTime } from "./format";
import type { ArchiveCandidate, ArchiveSuggestion } from "./types";

const fmt = (n: number) => String(n);
type FolderRescan = { status: "success" | "removed"; changed?: boolean; chapter?: number | null };

const LANG: Record<string, string> = { en: "EN", it: "IT", ja: "JA", es: "ES", fr: "FR", de: "DE" };

function evidence(c: ArchiveCandidate): string {
  if (!c.evidence) return "";
  if (c.source === "inside") return ` · read inside ${c.evidence}`;
  if (c.source === "volume") return ` · ${c.evidence} = volume ${c.last_volume}, whose last chapter on MangaDex is ${fmt(c.last_chapter!)}`;
  return ` · ${c.evidence}`;
}

function Candidate({ c, onUse, onUpToDate, primary }: {
  c: ArchiveCandidate;
  onUse: (start: string) => void;
  onUpToDate?: () => void;
  primary: boolean;
}) {
  const btn = primary ? "btn sm primary" : "btn sm";
  const qc = useQueryClient();
  const toast = useToast();
  const rescan = useMutation({
    mutationFn: () => post<FolderRescan>("/api/archive/folder/rescan", { folder: c.folder }),
    onSuccess: (r) => {
      if (r.status === "removed") toast("No archives left in this folder: removed from the index");
      else if (r.chapter != null && r.chapter !== c.last_chapter) toast(`Folder re-read: up to Ch. ${fmt(r.chapter)}`);
      else toast(r.changed ? "Folder re-read" : "Folder re-read: nothing new");
      return qc.invalidateQueries({ queryKey: ["archive-suggest"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  return (
    <div className="archive-cand">
      <div className="row between" style={{ gap: 8, alignItems: "flex-start" }}>
        <div className="small">
          <span className="mono">{c.folder}</span>{" "}
          {c.language && <Badge>{LANG[c.language] ?? c.language.toUpperCase()}</Badge>}{" "}
          {c.custom_only && <Badge tone="info">Custom volumes</Badge>}
        </div>
        <button type="button" className="btn sm" disabled={rescan.isPending} onClick={() => rescan.mutate()}
          title="Read this folder again now (after adding or replacing archives)">
          {rescan.isPending ? "Rescanning…" : "Rescan folder"}
        </button>
      </div>
      {c.last_chapter != null ? (
        <div className="small">
          You have up to <strong>Ch. {fmt(c.last_chapter)}</strong>
          <span className="muted">{evidence(c)}</span>
        </div>
      ) : c.source === "volume_unmapped" ? (
        <div className="small">
          You have up to <strong>volume {c.last_volume}</strong>
          <span className="muted"> · its pages carry no chapter numbers and MangaDex lists no volumes for this title, so the chapter can't be worked out</span>
        </div>
      ) : (
        <div className="small muted">No chapter or volume number recognised in {c.archives} archive{c.archives === 1 ? "" : "s"}.</div>
      )}
      {c.last_chapter != null && (
        <div className="row" style={{ gap: 8, marginTop: 6, alignItems: "center" }}>
          {c.next_chapter != null ? (
            <>
              <button type="button" className={btn} onClick={() => onUse(fmt(c.next_chapter!))}>Continue from Ch. {fmt(c.next_chapter)}</button>
              <span className="small muted">
                {c.after_count} chapter{c.after_count === 1 ? "" : "s"} on MangaDex after it
                {c.after_count! > 1 ? ` (Ch. ${fmt(c.next_chapter)}–${fmt(c.after_last!)})` : ""}
              </span>
            </>
          ) : (
            <>
              {onUpToDate ? (
                <button type="button" className={btn} onClick={onUpToDate}>New chapters only</button>
              ) : (
                <button type="button" className={btn} onClick={() => onUse(fmt(Math.floor(c.last_chapter!) + 1))}>
                  Download from Ch. {fmt(Math.floor(c.last_chapter) + 1)}
                </button>
              )}
              <span className="small muted">You already have everything MangaDex lists.</span>
            </>
          )}
        </div>
      )}
      {c.errors.length > 0 && <div className="small muted">Unreadable: {c.errors.join(", ")}</div>}
    </div>
  );
}

/** "Continue from your archive": where the read-only CBZ collection ends for this title. */
export function ArchiveHint({ mangaId, languages, onUse, onUpToDate }: {
  mangaId: string;
  languages: string[];
  onUse: (start: string) => void;
  onUpToDate?: () => void;
}) {
  const langs = [...languages].sort().join(",");
  const q = useQuery({
    queryKey: ["archive-suggest", mangaId, langs],
    queryFn: () => api<ArchiveSuggestion>(`/api/archive/suggest/${mangaId}?languages=${encodeURIComponent(langs)}`),
    enabled: languages.length > 0,
    staleTime: 5 * 60_000,
    retry: false,
  });
  if (!languages.length) return null;
  if (q.isLoading) return <div className="small muted archive-hint">Looking in your archive…</div>;
  if (q.error) return <div className="small muted archive-hint">Archive check unavailable: {(q.error as Error).message}</div>;
  const d = q.data;
  if (!d || !d.configured) return null;
  const others = d.candidates.filter((c) => c !== d.best && c.folder !== d.best?.folder);
  return (
    <div className="card archive-hint">
      <div className="row between">
        <strong>Your archive</strong>
        {d.indexed_at && <span className="small muted">indexed {relTime(d.indexed_at)}</span>}
      </div>
      {d.candidates.length === 0 && (
        <div className="small muted">No folder with this title in the archive (searched by every MangaDex title, main and alternative).</div>
      )}
      {d.best && <Candidate c={d.best} onUse={onUse} onUpToDate={onUpToDate} primary />}
      {!d.best && d.candidates.length > 0 && (
        <div className="small muted">Found only in other languages or without a recognisable chapter:</div>
      )}
      {others.length > 0 && (
        d.best ? (
          <details style={{ marginTop: 8 }}>
            <summary className="small">Other folders ({others.length})</summary>
            {others.map((c) => <Candidate key={c.folder} c={c} onUse={onUse} onUpToDate={onUpToDate} primary={false} />)}
          </details>
        ) : others.map((c) => <Candidate key={c.folder} c={c} onUse={onUse} onUpToDate={onUpToDate} primary={false} />)
      )}
    </div>
  );
}
