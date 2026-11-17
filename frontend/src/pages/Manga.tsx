import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, del, patch, post, put } from "../api";
import { Badge, CompletedBadge, Cover, Empty, ErrorBox, JobProgress, JobStatusBadge, Loading, Switch, Tabs, TierBadge, useToast } from "../components";
import { LOCAL_STATUS, absTime, chapterLabel, plainDescription, relTime } from "../format";
import { CUSTOM_TITLE, GroupPicker, LanguagePicker, TitlePicker, type GroupChoice } from "../pickers";
import { ArchiveHint } from "../archive";
import { TagList } from "../tags";
import type { GroupStat, MangaDetail, MangaTitles, Release, RenameReport, ScanRun } from "../types";

type Tab = "chapters" | "settings" | "history";

export default function MangaPage() {
  const { id = "" } = useParams();
  const [tab, setTab] = useState<Tab>("chapters");
  const [renaming, setRenaming] = useState(false);
  const toast = useToast();
  const qc = useQueryClient();
  const nav = useNavigate();
  const manga = useQuery({ queryKey: ["manga", id], queryFn: () => api<MangaDetail>(`/api/manga/${id}`), refetchInterval: 15000 });

  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => post<Record<string, unknown>>(path, body),
    onSuccess: (_r, v) => {
      qc.invalidateQueries({ queryKey: ["manga", id] });
      qc.invalidateQueries({ queryKey: ["chapters", id] });
      const msg: Record<string, string> = { scan: "Scan queued", "download-missing": "Missing releases queued", "library-scan": "Folder scan queued" };
      toast(msg[v.path.split("/").pop() ?? ""] ?? "Done");
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const [owning, setOwning] = useState(false);
  const [ownedUpTo, setOwnedUpTo] = useState("");
  const markOwned = useMutation({
    mutationFn: (upTo: number | null) =>
      post<{ owned: number; cancelled: number }>(`/api/manga/${id}/mark-owned`, upTo === null ? {} : { up_to: upTo }),
    onSuccess: (r) => {
      setOwning(false);
      setOwnedUpTo("");
      qc.invalidateQueries({ queryKey: ["manga", id] });
      qc.invalidateQueries({ queryKey: ["chapters", id] });
      qc.invalidateQueries({ queryKey: ["queue"] });
      toast(`${r.owned} chapter(s) marked as owned, ${r.cancelled} download(s) cancelled`);
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const toggle = useMutation({
    mutationFn: (body: Record<string, unknown>) => patch<MangaDetail>(`/api/manga/${id}`, body),
    onSuccess: (m) => qc.setQueryData(["manga", id], m),
    onError: (e: Error) => toast(e.message, "bad"),
  });

  const remove = useMutation({
    mutationFn: () => del(`/api/manga/${id}`, { confirm: true }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manga"] });
      toast("Removed from library (files kept)");
      nav("/library");
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  if (manga.isLoading) return <Loading />;
  if (manga.error || !manga.data) return <ErrorBox error={manga.error} />;
  const m = manga.data;

  if (m.pending_approval) {
    return (
      <>
        <div className="hero" style={{ marginTop: 4 }}>
          <Cover src={m.cover_url} alt={`Cover of ${m.title}`} size="lg" />
          <div style={{ minWidth: 0 }}>
            <h1>{m.title}</h1>
            <div className="row" style={{ gap: 6, margin: "6px 0" }}>
              <Badge tone="accent">Waiting for approval</Badge>
              {m.status && <Badge>{m.status}</Badge>}
              {m.year && <Badge>{m.year}</Badge>}
            </div>
            <TagList tags={m.tags} />
            {m.description && <div className="small muted clamp2">{plainDescription(m.description)}</div>}
            <a className="small" href={m.mangadex_url} target="_blank" rel="noreferrer noopener">Open on MangaDex ↗</a>
          </div>
        </div>
        <div className="alert info">
          <div className="body">
            <strong>Added from your MangaDex follows {relTime(m.pending_since)}</strong>
            Nothing is scanned or downloaded until you approve it and choose languages, groups and the first chapter.
          </div>
        </div>
        <div className="row">
          <Link className="btn primary" to={`/add?approve=${m.id}`}>Configure & approve</Link>
          <Link className="btn ghost" to="/approvals">All approvals</Link>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="hero" style={{ marginTop: 4 }}>
        <Cover src={m.cover_url} alt={`Cover of ${m.title}`} size="lg" />
        <div style={{ minWidth: 0 }}>
          <h1>
            {m.title}
            <TierBadge tier={m.update_tier} />
          </h1>
          {m.title_override && <div className="small muted">MangaDex: {m.canonical_title}</div>}
          <div className="row" style={{ gap: 6, margin: "6px 0" }}>
            <CompletedBadge show={m.upload_complete} />
            {m.status && <Badge>{m.status}</Badge>}
            {m.year && <Badge>{m.year}</Badge>}
            {m.content_rating && <Badge>{m.content_rating}</Badge>}
            {m.languages.map((l) => <Badge key={l} tone="info">{l.toUpperCase()}</Badge>)}
          </div>
          <TagList tags={m.tags} />
          <div className="small muted">
            {m.downloaded} downloaded · {m.missing} missing{m.active_jobs ? ` · ${m.active_jobs} in queue` : ""}
            {m.failed_jobs ? ` · ${m.failed_jobs} failed` : ""}
          </div>
          <div className="small muted">
            Last scan {relTime(m.last_scan_at)} · next {m.monitoring_enabled ? relTime(m.next_scan_at) : "— (paused)"}
          </div>
          {m.update_tier && (
            <div className="small muted">
              Level {m.update_tier}
              {m.upload_complete ? " (complete)" : ""} · last chapter {relTime(m.last_chapter_at)}
              {m.prev_chapter_at ? `, previous ${relTime(m.prev_chapter_at)}` : ""} · checked every {intervalLabel(m.effective_interval_minutes)}
            </div>
          )}
          <a className="small" href={m.mangadex_url} target="_blank" rel="noreferrer noopener">Open on MangaDex ↗</a>
        </div>
      </div>

      {m.last_error && (
        <div className="alert warn" style={{ marginTop: 12 }}>
          <div className="body"><strong>Last scan failed</strong>{m.last_error}</div>
        </div>
      )}

      <div className="card" style={{ marginTop: 14 }}>
        <Switch label="Monitoring" hint="Check MangaDex for new releases on a schedule" checked={m.monitoring_enabled} onChange={(v) => toggle.mutate({ monitoring_enabled: v })} />
        <Switch label="Automatic downloads" hint="Queue new releases as soon as they are found" checked={m.auto_download} onChange={(v) => toggle.mutate({ auto_download: v })} />
        <Switch label="One release per chapter" hint="Download each chapter number only once, from the first group that releases it" checked={m.one_release_per_chapter} onChange={(v) => toggle.mutate({ one_release_per_chapter: v })} />
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn sm dark" onClick={() => action.mutate({ path: `/api/manga/${id}/scan` })} disabled={action.isPending}>Scan now</button>
          <button className="btn sm" onClick={() => action.mutate({ path: `/api/manga/${id}/download-missing` })} disabled={action.isPending || m.missing === 0}>
            Download missing ({m.missing})
          </button>
          <button
            className="btn sm"
            aria-expanded={owning}
            title="Chapters you already have elsewhere: they are never downloaded"
            onClick={() => setOwning((v) => !v)}
          >
            I have these already
          </button>
          <button className="btn sm" onClick={() => action.mutate({ path: `/api/manga/${id}/library-scan` })} disabled={action.isPending} title="Re-check this manga's folder on disk">
            Check folder
          </button>
          <button className="btn sm" onClick={() => setRenaming((v) => !v)} aria-expanded={renaming}>
            Change title
          </button>
          <button
            className="btn sm danger"
            disabled={remove.isPending}
            onClick={() => {
              if (window.confirm(`Stop following “${m.title}”?\n\nThe database records are removed. Downloaded files on the NAS are NOT deleted.`)) remove.mutate();
            }}
          >
            Stop following
          </button>
        </div>
      </div>
      {owning && (
        <div className="card" style={{ marginTop: 14 }} role="region" aria-label="Chapters you already have">
          <h3 style={{ marginTop: 0 }}>Chapters you already have</h3>
          <p className="small muted" style={{ marginTop: 0 }}>
            Owned chapters are never downloaded and are removed from the queue. Monitoring stays on.
          </p>
          <form
            className="row"
            style={{ gap: 8, alignItems: "flex-end" }}
            onSubmit={(e) => {
              e.preventDefault();
              const v = ownedUpTo.trim().replace(",", ".");
              if (v === "") {
                if (window.confirm("Mark every chapter MangaDex lists now as already owned? Only chapters published from now on will be downloaded.")) markOwned.mutate(null);
                return;
              }
              const n = Number(v);
              if (!Number.isFinite(n) || n < 0) return toast("Enter a chapter number", "bad");
              markOwned.mutate(n);
            }}
          >
            {!m.chapters_per_volume && (
              <label className="small" style={{ display: "grid", gap: 4 }}>
                Up to chapter
                <input
                  className="input"
                  inputMode="decimal"
                  style={{ width: 140 }}
                  placeholder="everything"
                  value={ownedUpTo}
                  onChange={(e) => setOwnedUpTo(e.target.value)}
                  aria-label="Up to chapter"
                />
              </label>
            )}
            <button className="btn sm dark" type="submit" disabled={markOwned.isPending}>
              {ownedUpTo.trim() ? `I have up to ${ownedUpTo.trim()}` : "I have everything listed now"}
            </button>
            <button className="btn sm" type="button" onClick={() => setOwning(false)}>Cancel</button>
          </form>
          <p className="small muted" style={{ marginBottom: 0 }}>
            With a number, chapters up to it are owned (also the ones MangaDex does not list) and higher chapters stay in
            the queue. Empty: everything MangaDex lists now is owned.
          </p>
        </div>
      )}
      {renaming && <TitleEditor manga={m} onDone={() => setRenaming(false)} />}

      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        items={[
          { value: "chapters", label: "Chapters" },
          { value: "settings", label: "Languages & groups" },
          { value: "history", label: "History" },
        ]}
      />
      {tab === "chapters" && <Chapters mangaId={id} />}
      {tab === "settings" && <MangaSettings manga={m} />}
      {tab === "history" && <History mangaId={id} />}
    </>
  );
}

function Chapters({ mangaId }: { mangaId: string }) {
  const [scope, setScope] = useState<"wanted" | "all">("wanted");
  const [filter, setFilter] = useState<"all" | "missing" | "have" | "problems">("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const toast = useToast();
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["chapters", mangaId, scope],
    queryFn: () => api<{ items: Release[] }>(`/api/manga/${mangaId}/chapters?scope=${scope}`),
    refetchInterval: 8000,
  });
  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => post(path, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["chapters", mangaId] });
      qc.invalidateQueries({ queryKey: ["manga", mangaId] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const bulk = useMutation({
    mutationFn: (ids: string[]) => post<{ queued: number; already: number; not_downloadable: number }>("/api/releases/download", { release_ids: ids }),
    onSuccess: (r) => {
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["chapters", mangaId] });
      qc.invalidateQueries({ queryKey: ["manga", mangaId] });
      toast(`${r.queued} chapter(s) queued${r.already ? `, ${r.already} already queued or in library` : ""}`);
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  const items = useMemo(() => {
    const all = data?.items ?? [];
    switch (filter) {
      case "missing":
        return all.filter((r) => r.local_status === "missing");
      case "have":
        return all.filter((r) => ["downloaded", "imported", "modified"].includes(r.local_status));
      case "problems":
        return all.filter((r) => r.job && ["failed", "needs_attention"].includes(r.job.status));
      default:
        return all;
    }
  }, [data, filter]);

  const selectable = (r: Release) =>
    ["missing", "skipped"].includes(r.local_status) && !r.external && !r.unavailable &&
    !(r.job && !["completed", "cancelled"].includes(r.job.status));
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  // "From here": this chapter and every chapter with a higher number, whatever the display order.
  const selectFrom = (from: Release) =>
    setSelected(new Set(items.filter((r) => selectable(r) && (r.id === from.id ||
      (from.sort !== null && r.sort !== null && r.sort >= from.sort))).map((r) => r.id)));
  const order = useChapterOrder();
  const volumes = useMemo(() => groupByVolume(items, order.value), [items, order.value]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const selectableCount = items.filter(selectable).length;

  if (isLoading) return <Loading />;
  if (error) return <ErrorBox error={error} />;

  return (
    <>
      {selectableCount > 0 && (
        <div className="card row between" style={{ marginBottom: 10, padding: "10px 12px" }}>
          <span className="small">
            {selected.size > 0 ? <strong>{selected.size} selected</strong> : "Tick chapters to download them, or use “From here”."}
          </span>
          <div className="row" style={{ gap: 6 }}>
            <button className="btn sm ghost" onClick={() => setSelected(new Set(items.filter(selectable).map((r) => r.id)))}>Select all</button>
            {selected.size > 0 && <button className="btn sm ghost" onClick={() => setSelected(new Set())}>Clear</button>}
            <button className="btn sm primary" disabled={selected.size === 0 || bulk.isPending} onClick={() => bulk.mutate([...selected])}>
              Download selected{selected.size ? ` (${selected.size})` : ""}
            </button>
          </div>
        </div>
      )}
      <div className="row between" style={{ marginBottom: 10 }}>
        <select className="input" style={{ width: "auto" }} value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)} aria-label="Filter chapters">
          <option value="all">All ({data?.items.length ?? 0})</option>
          <option value="missing">Missing</option>
          <option value="have">In library</option>
          <option value="problems">Problems</option>
        </select>
        <button type="button" className="btn sm" onClick={order.toggle} title="Applies to every manga"
          aria-label={order.value === "asc" ? "Sorted oldest first; switch to newest first" : "Sorted newest first; switch to oldest first"}>
          {order.value === "asc" ? "↑ Oldest first" : "↓ Newest first"}
        </button>
        <label className="switch" style={{ gap: 8 }}>
          <span className="small">Show filtered-out releases</span>
          <input type="checkbox" role="switch" checked={scope === "all"} onChange={(e) => setScope(e.target.checked ? "all" : "wanted")} />
        </label>
      </div>
      {items.length === 0 && <Empty title="No releases">Run a scan to fetch chapters from MangaDex.</Empty>}
      {volumes.map((v) => {
        const isClosed = collapsed.has(v.key);
        return (
          <section className="vol-group" key={v.key}>
            <button type="button" className="vol-head" aria-expanded={!isClosed}
              onClick={() => setCollapsed((prev) => { const n = new Set(prev); if (n.has(v.key)) n.delete(v.key); else n.add(v.key); return n; })}>
              <span className="vol-name">{v.label}</span>
              <span className="vol-range">{v.range}</span>
              <span className="vol-count">{v.items.length} <span aria-hidden="true">{isClosed ? "▾" : "▴"}</span></span>
            </button>
            {!isClosed && (
              <div className="ch-list">
                {v.items.map((r) => (
                  <ChapterRow key={r.id} r={r} selectable={selectable(r)} selected={selected.has(r.id)}
                    onToggle={() => toggle(r.id)} onFromHere={() => selectFrom(r)}
                    onAction={(path, body) => act.mutate({ path, body })} />
                ))}
              </div>
            )}
          </section>
        );
      })}
    </>
  );
}

const VOLUME_FACTOR = 1000;
const fmtNum = (n: number) => String(Math.round(n * 10000) / 10000);

function MangaSettings({ manga }: { manga: MangaDetail }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [languages, setLanguages] = useState(manga.languages);
  const [groups, setGroups] = useState<GroupChoice>({ mode: manga.group_mode, ids: manga.groups.map((g) => g.id), includeUnknown: manga.include_unknown_group });
  const [interval, setIntervalMin] = useState<string>(manga.interval_minutes ? String(manga.interval_minutes / 60) : "");
  const perVolume = manga.chapters_per_volume;
  const [startChapter, setStartChapter] = useState<string>(
    manga.start_chapter == null ? "" : perVolume ? fmtNum(manga.start_chapter % VOLUME_FACTOR) : String(manga.start_chapter),
  );
  const [startVolume, setStartVolume] = useState<string>(
    perVolume && manga.start_chapter != null ? String(Math.floor(manga.start_chapter / VOLUME_FACTOR)) : "",
  );
  const startValue = (): number | null => {
    if (!startChapter.trim()) return null;
    const ch = Number(startChapter.replace(",", "."));
    if (!perVolume) return ch;
    const vol = startVolume.trim() ? Number(startVolume.replace(",", ".")) : 0;
    return Math.floor(vol) * VOLUME_FACTOR + ch;
  };
  useEffect(() => setLanguages(manga.languages), [manga.languages]);

  const known = useQuery({
    queryKey: ["manga-groups", manga.id],
    queryFn: () => api<{ groups: GroupStat[]; unknown_group_releases: Record<string, number> }>(`/api/manga/${manga.id}/groups`),
  });
  // Groups in the selected languages (from releases already discovered).
  const groupList = useMemo(
    () => (known.data?.groups ?? []).filter((g) => g.subscribed || Object.keys(g.languages).some((l) => languages.includes(l))),
    [known.data, languages],
  );

  const save = useMutation({
    mutationFn: () =>
      patch<MangaDetail>(`/api/manga/${manga.id}`, {
        languages,
        group_mode: groups.mode,
        group_ids: groups.mode === "selected" ? groups.ids : [],
        include_unknown_group: groups.includeUnknown,
        ...(interval.trim() ? { interval_minutes: Math.round(Number(interval) * 60) } : { use_global_interval: true }),
        ...(startValue() != null ? { start_chapter: startValue() } : { clear_start_chapter: true }),
      }),
    onSuccess: (m) => {
      qc.setQueryData(["manga", manga.id], m);
      qc.invalidateQueries({ queryKey: ["chapters", manga.id] });
      toast("Saved. Filter changes trigger a rescan; nothing is re-downloaded.");
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  return (
    <div className="card">
      <h2 className="section-title" style={{ marginTop: 0 }}>Languages</h2>
      <LanguagePicker value={languages} onChange={setLanguages} available={manga.available_languages} />
      <h2 className="section-title">Scanlation groups</h2>
      <GroupPicker groups={groupList} unknown={known.data?.unknown_group_releases} value={groups} onChange={setGroups} loading={known.isLoading} />
      {groups.mode === "selected" && groupList.length === 0 && (
        <div className="small muted">Groups appear here after the first scan in the selected languages.</div>
      )}
      <h2 className="section-title">Start from chapter</h2>
      {!perVolume && <ArchiveHint mangaId={manga.id} languages={languages} onUse={setStartChapter} />}
      {perVolume ? (
        <div className="field">
          <div className="row" style={{ gap: 8 }}>
            <div style={{ flex: 1 }}>
              <label htmlFor="startvol">From volume</label>
              <input id="startvol" className="input" inputMode="numeric" placeholder="Volume" value={startVolume} onChange={(e) => setStartVolume(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label htmlFor="startch">Chapter</label>
              <input id="startch" className="input" inputMode="decimal" placeholder="All chapters" value={startChapter} onChange={(e) => setStartChapter(e.target.value)} />
            </div>
          </div>
          <span className="hint">Chapter numbering restarts in every volume for this title, so the start is a volume and a chapter in it (e.g. volume 15, chapter 2). Leave the chapter empty for all chapters.</span>
        </div>
      ) : (
        <div className="field">
          <label htmlFor="startch">Download from chapter</label>
          <input id="startch" className="input" inputMode="decimal" placeholder="All chapters" value={startChapter} onChange={(e) => setStartChapter(e.target.value)} />
          <span className="hint">Only chapters with this number or higher are downloaded automatically (e.g. 25 or 12.5). One-shots and extras without a number are not affected. Lower chapter numbers that appear on MangaDex after you set the start (late extras like 12.5, skipped chapters uploaded later) are downloaded too; new uploads of numbers that already existed are not. Leave empty for all chapters.</span>
        </div>
      )}
      <h2 className="section-title">Monitoring interval</h2>
      <div className="field">
        <label htmlFor="interval">Hours between scans</label>
        <input id="interval" className="input" inputMode="decimal" placeholder="Use global default" value={interval} onChange={(e) => setIntervalMin(e.target.value)} />
        <span className="hint">Leave empty to use the global setting. Minimum 0.5 hours. Titles at level 2 or 3 (no recent chapters) are checked less often than this; see Settings → Monitoring levels.</span>
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <button className="btn primary" onClick={() => save.mutate()} disabled={save.isPending || languages.length === 0 || (groups.mode === "selected" && groups.ids.length === 0)}>
          Save changes
        </button>
      </div>
    </div>
  );
}

interface HistoryEvent {
  id: number;
  event: string;
  message: string | null;
  created_at: string;
  release: { chapter: string | null; volume: string | null; language: string; groups: string[] } | null;
}

function History({ mangaId }: { mangaId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["history", mangaId],
    queryFn: () => api<{ events: HistoryEvent[]; scans: ScanRun[] }>(`/api/manga/${mangaId}/history`),
    refetchInterval: 15000,
  });
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorBox error={error} />;
  return (
    <>
      <h2 className="section-title" style={{ marginTop: 0 }}>Scans</h2>
      <div className="list">
        {data.scans.length === 0 && <div className="muted small">No scans yet.</div>}
        {data.scans.map((s) => (
          <div className="item" key={s.id}>
            <div className="row between">
              <span>{relTime(s.started_at)} · {s.trigger.replace("_", " ")}</span>
              <Badge tone={s.status === "success" ? "ok" : s.status === "failed" ? "bad" : "info"}>{s.status}</Badge>
            </div>
            <div className="meta">
              <span>{s.full_scan ? "full" : "incremental"}</span>
              <span>{s.releases_seen} seen</span>
              <span>{s.new_releases} new</span>
              <span>{s.queued} queued</span>
              <span>{s.requests_made} requests</span>
            </div>
            {s.error && <div className="error">{s.error}</div>}
          </div>
        ))}
      </div>
      <h2 className="section-title">Download history</h2>
      <div className="list">
        {data.events.length === 0 && <div className="muted small">No downloads yet.</div>}
        {data.events.map((e) => (
          <div className="item" key={e.id}>
            <div className="row between">
              <strong>{e.release ? `${chapterLabel(e.release as never)} · ${e.release.language.toUpperCase()} · ${e.release.groups.join(", ") || "Unknown Group"}` : e.event}</strong>
              <Badge tone={e.event === "completed" ? "ok" : ["failed", "needs_attention"].includes(e.event) ? "bad" : ""}>{e.event.replace("_", " ")}</Badge>
            </div>
            <div className="meta">
              <span title={absTime(e.created_at)}>{relTime(e.created_at)}</span>
              {e.message && <span className="ellipsis">{e.message}</span>}
            </div>
          </div>
        ))}
      </div>
      <p className="small muted"><Link to="/queue">Open download queue →</Link></p>
    </>
  );
}

export function TitleEditor({ manga, onDone }: { manga: MangaDetail; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [choice, setChoice] = useState<string>(manga.title);
  const [custom, setCustom] = useState("");
  const titles = useQuery({
    queryKey: ["manga-titles", manga.id],
    queryFn: () => api<MangaTitles>(`/api/manga/${manga.id}/titles`),
  });
  const target = choice === CUSTOM_TITLE ? custom.trim() : choice;

  const rename = useMutation({
    mutationFn: async () => {
      const title = target || null; // empty = back to the MangaDex main title
      const preview = (await post<{ report: RenameReport }>(`/api/manga/${manga.id}/rename`, { title, dry_run: true })).report;
      if (preview.title === manga.title && !preview.folder_changes) return null;
      if (preview.busy_downloads > 0 && preview.folder_changes) {
        throw new Error(`${preview.busy_downloads} chapter(s) are downloading right now. Wait for them to finish (or pause them), then try again.`);
      }
      if (preview.folder_changes) {
        const files = preview.chapters_on_disk
          ? `The folder with ${preview.chapters_on_disk} downloaded chapter(s) will be renamed:`
          : "No chapters downloaded yet; new downloads will use:";
        const ok = window.confirm(`Change title to “${preview.title}”?\n\n${files}\n“${preview.folder_from}” → “${preview.folder_to}”`);
        if (!ok) return null;
      }
      return post<{ report: RenameReport; manga: MangaDetail }>(`/api/manga/${manga.id}/rename`, { title });
    },
    onSuccess: (r) => {
      if (!r) {
        onDone();
        return;
      }
      qc.setQueryData(["manga", manga.id], r.manga);
      qc.invalidateQueries({ queryKey: ["manga"] });
      qc.invalidateQueries({ queryKey: ["chapters", manga.id] });
      qc.invalidateQueries({ queryKey: ["manga-titles", manga.id] });
      toast(r.report.moved ? `Title changed. Folder renamed to “${r.report.folder_to}”.` : "Title changed.");
      onDone();
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  return (
    <div className="card" style={{ marginTop: 14 }}>
      <h2 className="section-title" style={{ marginTop: 0 }}>Change title</h2>
      {titles.isLoading && <Loading />}
      {titles.error && <ErrorBox error={titles.error} />}
      {titles.data && !titles.data.live && <div className="small muted">MangaDex is not reachable right now; showing the titles saved earlier.</div>}
      {titles.data && (
        <TitlePicker
          titles={titles.data.titles}
          languages={titles.data.preferred_languages}
          value={choice}
          custom={custom}
          current={manga.title}
          onChange={(v, c) => { setChoice(v); setCustom(c); }}
          disabled={rename.isPending}
        />
      )}
      <div className="small muted" style={{ marginTop: 6 }}>The library folder is renamed to match, with every chapter already downloaded inside it. Current folder: <span className="mono">{manga.folder_name}</span></div>
      <div className="row" style={{ marginTop: 10 }}>
        <button type="button" className="btn primary" disabled={rename.isPending || !titles.data || (choice === CUSTOM_TITLE && !custom.trim())} onClick={() => rename.mutate()}>
          {rename.isPending ? "Renaming…" : "Apply title"}
        </button>
        <button type="button" className="btn" onClick={onDone} disabled={rename.isPending}>Cancel</button>
      </div>
    </div>
  );
}

type Volume = { key: string; label: string; range: string; items: Release[] };

const SORT_LAST = Number.POSITIVE_INFINITY;

/** Group releases by volume (MangaDex style). Groups are ordered by their chapters, so
 * chapters not yet collected in a volume sit where they belong (usually the newest). */
export function groupByVolume(items: Release[], order: "asc" | "desc"): Volume[] {
  const dir = order === "asc" ? 1 : -1;
  const num = (r: Release) => (r.sort ?? SORT_LAST);
  const sorted = [...items].sort((a, b) =>
    dir * (num(a) - num(b) || 0) || a.language.localeCompare(b.language) || a.groups.join().localeCompare(b.groups.join()));
  const map = new Map<string, Release[]>();
  for (const r of sorted) {
    const key = r.volume ? `v:${r.volume}` : "none";
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(r);
  }
  const vols: Volume[] = [...map.entries()].map(([key, rs]) => {
    const nums = rs.map((r) => r.chapter).filter((c): c is string => !!c && !Number.isNaN(Number(c)));
    const vals = nums.map(Number);
    const lo = vals.length ? nums[vals.indexOf(Math.min(...vals))] : null;
    const hi = vals.length ? nums[vals.indexOf(Math.max(...vals))] : null;
    const range = lo === null ? "" : lo === hi ? `Ch. ${lo}` : order === "asc" ? `Ch. ${lo} – ${hi}` : `Ch. ${hi} – ${lo}`;
    return { key, label: key === "none" ? "No volume" : `Volume ${key.slice(2)}`, range, items: rs };
  });
  const anchor = (v: Volume) => {
    const vals = v.items.map(num).filter((n) => n !== SORT_LAST);
    if (!vals.length) return SORT_LAST;
    return order === "asc" ? Math.min(...vals) : Math.max(...vals);
  };
  const volNum = (v: Volume) => (v.key === "none" ? SORT_LAST : Number(v.key.slice(2)) || 0);
  return vols.sort((a, b) => dir * (anchor(a) - anchor(b) || volNum(a) - volNum(b) || 0));
}

/** Chapter list order, stored in the app settings so it applies to every manga and device. */
function useChapterOrder() {
  const qc = useQueryClient();
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<{ settings: Record<string, unknown>; defaults: Record<string, unknown> }>("/api/settings"),
    staleTime: 60_000,
  });
  const value = (settings.data?.settings.chapter_sort_order as "asc" | "desc" | undefined) ?? "asc";
  const save = useMutation({
    mutationFn: (v: "asc" | "desc") => put("/api/settings", { chapter_sort_order: v }),
    onMutate: (v) => {
      const prev = qc.getQueryData(["settings"]);
      qc.setQueryData(["settings"], (old: { settings: Record<string, unknown> } | undefined) =>
        old ? { ...old, settings: { ...old.settings, chapter_sort_order: v } } : old);
      return { prev };
    },
    onError: (_e, _v, ctx) => ctx?.prev && qc.setQueryData(["settings"], ctx.prev),
  });
  return { value, toggle: () => save.mutate(value === "asc" ? "desc" : "asc") };
}

function statusClass(r: Release, job: Release["job"]): string {
  if (job && ["failed", "needs_attention"].includes(job.status)) return "st-bad";
  if (job) return "st-active";
  if (["downloaded", "imported", "modified", "external"].includes(r.local_status)) return "st-have";
  if (r.local_status === "missing_on_disk") return "st-warn";
  if (r.local_status === "missing" && r.wanted && !r.external && !r.unavailable) return "st-missing";
  return "st-off";
}

function ChapterRow({ r, selectable, selected, onToggle, onFromHere, onAction }: {
  r: Release;
  selectable: boolean;
  selected: boolean;
  onToggle: () => void;
  onFromHere: () => void;
  onAction: (path: string, body?: unknown) => void;
}) {
  const ls = LOCAL_STATUS[r.local_status];
  const job = r.job && r.job.status !== "completed" && r.job.status !== "cancelled" ? r.job : null;
  const name = `${r.chapter ? `Ch. ${r.chapter}` : "Oneshot"}${r.title ? ` - ${r.title}` : ""}`;
  return (
    <div className={`ch-row ${statusClass(r, job)}`} style={{ opacity: r.wanted || r.local_status !== "missing" ? 1 : 0.6 }}>
      <div className="ch-pick">
        {selectable && (
          <input type="checkbox" className="pick" checked={selected} onChange={onToggle} aria-label={`Select ${chapterLabel(r)} ${r.groups.join(", ")}`} />
        )}
      </div>
      <div className="ch-title">
        <span className="lang-chip">{r.language.toUpperCase()}</span>
        <a href={r.mangadex_url} target="_blank" rel="noreferrer noopener" title={`Open ${chapterLabel(r)} on MangaDex`}>{name}</a>
      </div>
      <div className="ch-time" title={r.publish_at ? absTime(r.publish_at) : undefined}>
        {r.publish_at ? relTime(r.publish_at) : ""}
      </div>
      <div className="ch-meta">
        <span title="Scanlation group">👥 {r.groups.join(" & ") || "Unknown Group"}</span>
        <span>{r.pages} p.</span>
        {!r.wanted && r.local_status === "missing" && <span>filtered out</span>}
        {r.folder && <span className="mono ch-folder" title={r.folder}>{r.folder}</span>}
      </div>
      <div className="ch-side">
        {r.external ? <Badge>External</Badge> : r.unavailable ? <Badge tone="warn">Unavailable</Badge> : null}
        {job ? <JobStatusBadge job={job} /> : <Badge tone={ls.tone}>{ls.label}</Badge>}
      </div>
      <div className="ch-actions">
        {r.local_status === "missing" && !job && !r.external && !r.unavailable && (
          <button className="btn xs dark" onClick={() => onAction(`/api/releases/${r.id}/download`)}>Download</button>
        )}
        {selectable && (
          <button className="btn xs ghost" onClick={onFromHere} title="Select this chapter and every chapter with a higher number">From here</button>
        )}
        {r.local_status === "missing" && !job && (
          <button className="btn xs ghost" onClick={() => onAction(`/api/releases/${r.id}/skip`)}>Skip</button>
        )}
        {r.local_status === "skipped" && (
          <button className="btn xs ghost" onClick={() => onAction(`/api/releases/${r.id}/unskip`)}>Un-skip</button>
        )}
        {job && ["failed", "needs_attention", "paused", "retry_scheduled"].includes(job.status) && (
          <button className="btn xs" onClick={() => onAction(`/api/queue/${job.id}/retry`)}>Retry now</button>
        )}
        {["downloaded", "imported", "modified", "missing_on_disk"].includes(r.local_status) && !job && (
          <button
            className="btn xs ghost"
            onClick={() => {
              if (window.confirm(`Redownload ${chapterLabel(r)} (${r.groups.join(", ") || "Unknown Group"})?\n\nThe current folder will be moved out of the library to the replaced folder (not deleted) and replaced by a fresh copy.`)) {
                onAction(`/api/releases/${r.id}/redownload`, { confirm: true });
              }
            }}
          >
            Redownload…
          </button>
        )}
        <a className="btn xs ghost" href={r.mangadex_url} target="_blank" rel="noreferrer noopener">MangaDex ↗</a>
      </div>
      {job && <div className="ch-wide"><JobProgress job={job} /></div>}
      {job?.last_error && ["failed", "needs_attention", "retry_scheduled", "paused"].includes(job.status) && (
        <div className="ch-wide error">{job.last_error}</div>
      )}
    </div>
  );
}

function intervalLabel(minutes: number): string {
  if (minutes >= 1440 && minutes % 1440 === 0) return `${minutes / 1440} d`;
  if (minutes >= 60) return `${Math.round((minutes / 60) * 10) / 10} h`;
  return `${minutes} min`;
}
