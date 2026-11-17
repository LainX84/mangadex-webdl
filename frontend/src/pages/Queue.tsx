import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "../api";
import { Badge, Cover, Empty, ErrorBox, JobProgress, JobStatusBadge, Loading, MangaTitle, Tabs, useToast } from "../components";
import { absTime, chapterLabel, relTime } from "../format";
import type { Job } from "../types";

type Group = "active" | "pending" | "paused" | "failed" | "completed" | "cancelled";

type View = "chapters" | "series";
export type MangaType = "manga" | "manhwa" | "manhua" | "other";
export const MANGA_TYPES: { value: MangaType; label: string }[] = [
  { value: "manga", label: "Manga" },
  { value: "manhwa", label: "Manhwa" },
  { value: "manhua", label: "Manhua" },
  { value: "other", label: "Other" },
];

/** The `type` URL parameter ("manga,manhwa") as a list of known types, in a stable order. */
export function parseTypes(raw: string | null): MangaType[] {
  const set = new Set((raw ?? "").split(",").map((t) => t.trim()));
  return MANGA_TYPES.map((t) => t.value).filter((t) => set.has(t));
}

export function typeQuery(types: MangaType[]): string {
  return types.map((t) => `&type=${t}`).join("");
}

interface QueueResponse {
  items: Job[];
  counts: Record<Group, number>;
  queue_paused: boolean;
  manga?: { id: string; title: string | null };
}

export interface SeriesRow {
  manga: { id: string; title: string; cover_url: string | null; update_tier?: number | null };
  in_group: number;
  queued: number;
  downloading: number;
  paused: number;
  failed: number;
  downloaded: number;
  last_finished_at: string | null;
}

interface SeriesResponse {
  items: SeriesRow[];
  counts: Record<Group, number>;
  queue_paused: boolean;
}

export default function QueuePage() {
  const [params, setParams] = useSearchParams();
  const group = (params.get("group") as Group) || "active";
  const view: View = params.get("view") === "series" ? "series" : "chapters";
  const manga = view === "chapters" ? params.get("manga") : null;
  const types = parseTypes(params.get("type"));
  const tq = typeQuery(types);
  const toast = useToast();
  const qc = useQueryClient();
  const [limit, setLimit] = useState(100);
  useEffect(() => setLimit(100), [group, manga, tq]);
  const live = group === "active" || group === "pending";
  const setQuery = (next: { group?: Group; view?: View; manga?: string | null; types?: MangaType[] }) => {
    const g = next.group ?? group;
    const v = next.view ?? view;
    const m = next.manga === undefined ? manga : next.manga;
    const t = next.types ?? types;
    const q: Record<string, string> = {};
    if (g !== "active") q.group = g;
    if (t.length) q.type = t.join(",");
    if (v === "series") q.view = v;
    else if (m) q.manga = m;
    setParams(q);
  };
  const chapters = useQuery({
    queryKey: ["queue", group, limit, manga, tq],
    queryFn: () => api<QueueResponse>(`/api/queue?group=${group}&limit=${limit}${manga ? `&manga=${manga}` : ""}${tq}`),
    placeholderData: (prev, prevQuery) =>
      prevQuery?.queryKey[1] === group && prevQuery?.queryKey[3] === manga && prevQuery?.queryKey[4] === tq ? prev : undefined,
    refetchInterval: live ? 3000 : 10000,
    enabled: view === "chapters",
  });
  const series = useQuery({
    queryKey: ["queue", "series", group, tq],
    queryFn: () => api<SeriesResponse>(`/api/queue/series?group=${group}${tq}`),
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[2] === group && prevQuery?.queryKey[3] === tq ? prev : undefined),
    refetchInterval: live ? 5000 : 15000,
    enabled: view === "series",
  });
  const { data, error, isLoading, isFetching } = chapters;
  const head = view === "series" ? series.data : data;
  const act = useMutation({
    mutationFn: (path: string) => post(path),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["queue"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  return (
    <>
      <div className="topbar">
        <h1 className="page-title">
          <small>Downloads</small>Queue
        </h1>
        {head &&
          (head.queue_paused ? (
            <button className="btn primary" onClick={() => act.mutate("/api/queue/resume")}>Resume queue</button>
          ) : (
            <button className="btn" onClick={() => act.mutate("/api/queue/pause")}>Pause queue</button>
          ))}
      </div>
      {head?.queue_paused && (
        <div className="alert info"><div className="body"><strong>Queue paused</strong>Running downloads stop after their current page; nothing new starts.</div></div>
      )}
      <div className="row" role="group" aria-label="Queue view" style={{ gap: 6, marginTop: 14 }}>
        <span className="small muted">View</span>
        <button className="chip" aria-pressed={view === "chapters"} onClick={() => setQuery({ view: "chapters", manga: null })}>By chapter</button>
        <button className="chip" aria-pressed={view === "series"} onClick={() => setQuery({ view: "series" })}>By series</button>
      </div>
      <TypeFilter value={types} onChange={(t) => setQuery({ types: t })} />
      <Tabs<Group>
        value={group}
        onChange={(g) => setQuery({ group: g })}
        items={[
          { value: "active", label: "Active", count: head?.counts.active },
          { value: "pending", label: "Pending", count: head?.counts.pending },
          { value: "paused", label: "Paused", count: head?.counts.paused },
          { value: "failed", label: "Failed", count: head?.counts.failed },
          { value: "completed", label: "Completed", count: head?.counts.completed },
          { value: "cancelled", label: "Cancelled", count: head?.counts.cancelled },
        ]}
      />
      {group === "failed" && !manga && (head?.counts.failed ?? 0) > 0 && (
        <button className="btn sm" style={{ marginBottom: 10 }} onClick={() => act.mutate("/api/queue/retry-failed")}>Retry all failed</button>
      )}
      {view === "series" ? (
        <SeriesList group={group} query={series} onOpen={(id) => setQuery({ view: "chapters", manga: id })} />
      ) : (
        <>
      {manga && (
        <div className="row" style={{ gap: 6, marginBottom: 10 }}>
          <span className="small muted">Only</span>
          <button className="chip" aria-pressed="true" onClick={() => setQuery({ manga: null })} title="Show every series">
            {data?.manga?.title ?? "this series"} ✕
          </button>
          <Link className="small" to={`/manga/${manga}`}>Open series</Link>
        </div>
      )}
      {isLoading && <Loading />}
      {error && <ErrorBox error={error} />}
      {data && data.items.length === 0 && <Empty title="Nothing here" />}
      <div className="list">
        {data?.items.map((j) => (
          <JobCard key={j.id} job={j} onAction={(a) => act.mutate(`/api/queue/${j.id}/${a}`)} />
        ))}
      </div>
      {data && data.items.length < (data.counts[group] ?? 0) && limit < 1000 && (
        <div className="row" style={{ justifyContent: "center", marginTop: 12 }}>
          <button className="btn" disabled={isFetching} onClick={() => setLimit((l) => Math.min(l + 100, 1000))}>
            {isFetching ? "Loading…" : `Show more (${data.items.length} of ${data.counts[group]})`}
          </button>
        </div>
      )}
        </>
      )}
    </>
  );
}

export function TypeFilter({ value, onChange }: { value: MangaType[]; onChange: (types: MangaType[]) => void }) {
  const toggle = (t: MangaType) => {
    const next = value.includes(t) ? value.filter((x) => x !== t) : [...value, t];
    onChange(MANGA_TYPES.map((x) => x.value).filter((x) => next.includes(x)));
  };
  return (
    <div className="row" role="group" aria-label="Type" style={{ gap: 6, marginTop: 8 }}>
      <span className="small muted">Type</span>
      <button className="chip" aria-pressed={value.length === 0} onClick={() => onChange([])}>All</button>
      {MANGA_TYPES.map((t) => (
        <button key={t.value} className="chip" aria-pressed={value.includes(t.value)} onClick={() => toggle(t.value)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

const GROUP_WORD: Record<Group, string> = {
  active: "active", pending: "pending", paused: "paused", failed: "failed", completed: "completed", cancelled: "cancelled",
};

export function SeriesList({ group, query, onOpen }: {
  group: Group;
  query: { data?: SeriesResponse; error: unknown; isLoading: boolean };
  onOpen: (mangaId: string) => void;
}) {
  const { data, error, isLoading } = query;
  if (isLoading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  if (!data || data.items.length === 0) return <Empty title="Nothing here" />;
  const total = data.items.reduce((a, s) => a + s.in_group, 0);
  return (
    <>
      <div className="small muted" style={{ marginBottom: 8 }}>
        {data.items.length} series · {total} {GROUP_WORD[group]} chapter{total === 1 ? "" : "s"}
      </div>
      <div className="list">
        {data.items.map((s) => <SeriesCard key={s.manga.id} row={s} group={group} onOpen={() => onOpen(s.manga.id)} />)}
      </div>
    </>
  );
}

function SeriesCard({ row: s, group, onOpen }: { row: SeriesRow; group: Group; onOpen: () => void }) {
  const planned = s.downloaded + s.queued;
  const pct = planned > 0 ? Math.round((s.downloaded / planned) * 100) : 0;
  const showInGroup = !["active", "pending", "completed"].includes(group);
  return (
    <div className="item">
      <div className="row" style={{ alignItems: "flex-start" }}>
        <Cover src={s.manga.cover_url} alt="" />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row between" style={{ flexWrap: "nowrap" }}>
            <Link to={`/manga/${s.manga.id}`} className="grow" style={{ textDecoration: "none", color: "inherit", minWidth: 0 }}>
              <MangaTitle className="title" title={s.manga.title} tier={s.manga.update_tier} />
            </Link>
            {s.downloading > 0 && <Badge tone="info">Downloading</Badge>}
          </div>
          <div className="meta">
            <span><strong>{s.queued}</strong> in queue</span>
            <span><strong>{s.downloaded}</strong> downloaded</span>
            {s.paused > 0 && <span>{s.paused} paused</span>}
            {s.failed > 0 && <span className="bad-text">{s.failed} failed</span>}
            {showInGroup && <span>{s.in_group} {GROUP_WORD[group]}</span>}
            {s.last_finished_at && <span title={absTime(s.last_finished_at)}>last {relTime(s.last_finished_at)}</span>}
          </div>
          {planned > 0 && (
            <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={planned} aria-valuenow={s.downloaded}
                 aria-label="Chapters downloaded" style={{ marginTop: 6 }}>
              <span style={{ width: `${pct}%` }} />
            </div>
          )}
          <div className="actions">
            <button className="btn sm" onClick={onOpen}>Show chapters</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function JobCard({ job: j, onAction }: { job: Job; onAction: (a: "retry" | "pause" | "resume" | "cancel") => void }) {
  const canPause = ["pending", "queued", "downloading", "retry_scheduled"].includes(j.status);
  const canResume = j.status === "paused";
  const canRetry = ["failed", "needs_attention", "retry_scheduled"].includes(j.status) || (j.status === "paused" && j.pause_reason !== "user");
  const canCancel = !["completed", "cancelled"].includes(j.status);
  return (
    <div className="item">
      <div className="row" style={{ alignItems: "flex-start" }}>
        <Cover src={j.manga?.cover_url ?? null} alt="" />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row between" style={{ flexWrap: "nowrap" }}>
            <Link to={`/manga/${j.manga?.id}`} className="grow" style={{ textDecoration: "none", color: "inherit", minWidth: 0 }}>
              <MangaTitle className="title" title={j.manga?.title} tier={j.manga?.update_tier} />
            </Link>
            <JobStatusBadge job={j} />
          </div>
          {j.release && (
            <div className="meta">
              <span>{chapterLabel(j.release)}{j.release.title ? ` · ${j.release.title}` : ""}</span>
              <span>{j.release.language.toUpperCase()}</span>
              <span>{j.release.groups.join(" & ") || "Unknown Group"}</span>
            </div>
          )}
          <div className="meta">
            {j.attempts > 0 && <span>attempt {j.attempts}</span>}
            {j.status === "retry_scheduled" && j.next_attempt_at && <span title={absTime(j.next_attempt_at)}>next try {relTime(j.next_attempt_at)}</span>}
            {j.pages_total != null && j.status !== "downloading" && <span>{j.pages_done ?? 0}/{j.pages_total} pages</span>}
            {j.finished_at && <span>{relTime(j.finished_at)}</span>}
            {j.is_redownload && <span>redownload</span>}
          </div>
          <div style={{ marginTop: 6 }}><JobProgress job={j} /></div>
          {j.last_error && j.status !== "completed" && <div className="error">{j.last_error}</div>}
          {j.target && j.status === "completed" && <div className="mono muted" style={{ marginTop: 4 }}>{j.target}</div>}
          <div className="actions">
            {canRetry && <button className="btn sm dark" onClick={() => onAction("retry")}>Retry now</button>}
            {canResume && <button className="btn sm" onClick={() => onAction("resume")}>Resume</button>}
            {canPause && <button className="btn sm" onClick={() => onAction("pause")}>Pause</button>}
            {canCancel && (
              <button className="btn sm ghost" onClick={() => window.confirm("Cancel this download? The release will be marked as skipped.") && onAction("cancel")}>
                Cancel
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
