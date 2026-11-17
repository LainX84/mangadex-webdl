import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, patch, post, put } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, Loading, Switch, useToast } from "../components";
import { bytes, relTime } from "../format";
import { LanguagePicker, useLanguages } from "../pickers";
import type { AccessBlock, AccountStatus, AppSettings, ArchiveStatus, Components, Cooldown, LibraryStatus, MangaSummary, User } from "../types";
import { SKINS, useSkin } from "../skin";
import { TagChip, nextTagColor, tagKey, useHighlightedTags, type HighlightedTag } from "../tags";

interface FieldDef {
  key: string;
  label: string;
  hint?: string;
  unit?: string;
  scale?: number; // display = stored / scale
  step?: number;
}

const SECTIONS: { title: string; note?: string; fields: FieldDef[] }[] = [
  {
    title: "Monitoring",
    fields: [
      { key: "monitoring_interval_minutes", label: "Monitoring interval", unit: "hours", scale: 60, step: 0.5, hint: "Default 6 h. Each scan is usually one request (incremental)." },
      { key: "full_scan_interval_hours", label: "Full re-scan every", unit: "hours", hint: "Full scans also detect removed chapters." },
      { key: "scan_spacing_seconds", label: "Minimum gap between scans", unit: "seconds", hint: "Spreads monitoring over time instead of bursts." },
    ],
  },
  {
    title: "Monitoring levels",
    note: "Each title gets a level from how often it receives chapters (any group, removed chapters included): the larger of the time since the newest chapter and the gap between the two newest. Lv.1 uses the monitoring interval above; Lv.2 and Lv.3 are checked less often. Titles whose upload is complete are always Lv.3.",
    fields: [
      { key: "tier1_max_days", label: "Lv.1 if updated within", unit: "days", hint: "Default 90 (about 3 months)." },
      { key: "tier2_max_days", label: "Lv.2 if updated within", unit: "days", hint: "Default 365. Beyond this: Lv.3." },
      { key: "tier2_interval_minutes", label: "Lv.2 check interval", unit: "hours", scale: 60, step: 0.5, hint: "Default 24 h." },
      { key: "tier3_interval_minutes", label: "Lv.3 check interval", unit: "days", scale: 1440, step: 0.5, hint: "Default 7 days." },
    ],
  },
  {
    title: "Concurrency & request rate",
    note: "Faster settings download quicker but send more requests to MangaDex and increase the chance of rate limiting. No setting can guarantee an IP will never be restricted; the defaults are deliberately conservative.",
    fields: [
      { key: "max_concurrent_chapters", label: "Max concurrent chapter downloads", hint: "1–4" },
      { key: "max_concurrent_pages", label: "Max concurrent page downloads", hint: "Across all chapters, 1–8" },
      { key: "api_requests_per_second", label: "API requests per second", step: 0.1, hint: "MangaDex allows ~5/s per IP; default 1." },
      { key: "api_min_interval_ms", label: "Minimum interval between API requests", unit: "ms" },
      { key: "image_requests_per_second", label: "Image requests per second", step: 0.1 },
      { key: "image_min_interval_ms", label: "Minimum interval between image requests", unit: "ms" },
      { key: "at_home_requests_per_minute", label: "Chapter server lookups per minute", hint: "MangaDex limit is 40/min; default 20." },
    ],
  },
  {
    title: "Retries & cooldown",
    fields: [
      { key: "max_retries", label: "Retry limit", hint: "Automatic retries for temporary errors before a job is marked failed." },
      { key: "initial_retry_delay_seconds", label: "Initial retry delay", unit: "seconds" },
      { key: "max_retry_delay_seconds", label: "Maximum retry delay", unit: "minutes", scale: 60 },
      { key: "page_retry_limit", label: "Attempts per page (within one run)" },
      { key: "rate_limit_base_cooldown_seconds", label: "Base cooldown after HTTP 429", unit: "seconds", hint: "Doubles with each repeated 429; Retry-After is always honoured." },
      { key: "rate_limit_max_cooldown_seconds", label: "Maximum cooldown", unit: "minutes", scale: 60 },
    ],
  },
  {
    title: "Revisions of chapters you moved out",
    note: "When a group re-uploads a chapter, or MangaDex publishes a new version with different pages, after you already moved that chapter out of the library, the new copy is saved in the replaced folder (never in the library) and listed in Review. Chapters downloaded longer ago are ignored. Chapters still in the library are simply replaced.",
    fields: [
      { key: "revision_after_move_days", label: "Keep revisions for chapters downloaded within", unit: "days", hint: "Default 15. 0 = never download revisions of chapters moved out." },
    ],
  },
  {
    title: "Weekly library check",
    note: "Marks the chapters you moved out of the library by hand as \u201cFolder deleted\u201d (and edited ones as \u201cEdited locally\u201d), so counts, statistics and revisions stay correct. Nothing is downloaded and no file is touched. It can also be run at any time from System \u2192 Library check.",
    fields: [
      { key: "library_check_weekday", label: "Day of the week", hint: "0 = Monday \u2026 6 = Sunday (default), -1 = off." },
      { key: "library_check_hour", label: "Hour (local time)", hint: "0\u201323, default 5." },
    ],
  },
  {
    title: "Folder naming",
    fields: [
      { key: "volume_padding", label: "Volume number digits", hint: "2 → Volume 01" },
      { key: "chapter_padding_with_volume", label: "Chapter digits (with volume)", hint: "2 → Volume 01 Chapter 01" },
      { key: "chapter_padding_without_volume", label: "Chapter digits (no volume)", hint: "3 → Volume TBD Chapter 012" },
    ],
  },
];

export default function SettingsPage() {
  return (
    <>
      <div className="topbar">
        <h1 className="page-title">
          <small>Configuration</small>Settings
        </h1>
        <div style={{ display: "flex", gap: 8 }}>
          <Link className="btn sm" to="/review">Review</Link>
          <Link className="btn sm ghost" to="/about">Info</Link>
        </div>
      </div>
      <AppearanceCard />
      <SystemCard />
      <MangaDexAccountCard />
      <ArchiveCard />
      <HighlightedTagsCard />
      <AppSettingsForm />
      <LanguageCodes />
      <AccountCard />
    </>
  );
}

interface SystemStatus {
  library: LibraryStatus;
  library_root: string;
  staging: WorkDir;
  replaced: WorkDir;
  sentinel: string;
  expected_fs_types: string[];
  cooldown: Cooldown | null;
  access_block: AccessBlock | null;
  queue_paused: boolean;
  components: Components;
  version: string;
}

interface WorkDir {
  path: string;
  exists: boolean;
  fs_type: string | null;
  entries: number | null;
  error: string | null;
}

function WorkDirLine({ label, dir, empty }: { label: string; dir: WorkDir; empty: string }) {
  return (
    <div className="small muted" style={{ marginTop: 4 }}>
      {label}: <span className="mono">{dir.path}</span>
      {dir.error ? ` · ${dir.error}` : !dir.exists ? " · created on first use" : ` · ${dir.entries ? `${dir.entries} folder(s)` : empty}`}
    </div>
  );
}

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

interface LibraryCheckStatus {
  enabled: boolean;
  weekday: number;
  hour: number;
  next_run: string | null;
  last_run: null | {
    status: string;
    started_at: string;
    finished_at: string | null;
    error: string | null;
    moved_out: number;
    edited: number;
    restored: number;
    review: number;
  };
}

function LibraryCheck({ libraryOk }: { libraryOk: boolean }) {
  const toast = useToast();
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["library-check"],
    queryFn: () => api<LibraryCheckStatus>("/api/library/check"),
    refetchInterval: 20000,
  });
  const run = useMutation({
    mutationFn: () => post<{ queued: boolean }>("/api/library/scan"),
    onSuccess: (r) => {
      toast(r.queued ? "Library check queued" : "A library check is already queued or running");
      qc.invalidateQueries({ queryKey: ["library-check"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const last = data?.last_run;
  const running = last?.status === "running";
  return (
    <>
      <div className="row between">
        <strong>Library check</strong>
        {running ? <Badge tone="info">Running</Badge> : last?.status === "failed" ? <Badge tone="bad">Last run failed</Badge> : null}
      </div>
      <div className="small muted" style={{ marginTop: 4 }}>
        Marks chapters moved out of the library by hand. {data?.enabled
          ? <>Every {WEEKDAYS[data.weekday]} at {String(data.hour).padStart(2, "0")}:00 · next {relTime(data.next_run)}.</>
          : <>Weekly run is off (Settings → Weekly library check).</>}
      </div>
      {last && (
        <div className="small muted">
          Last run {relTime(last.started_at)}
          {last.finished_at && !running
            ? ` · ${last.moved_out} moved out, ${last.edited} edited, ${last.restored} back in place, ${last.review} to review`
            : ""}
          {last.error ? ` · ${last.error}` : ""}
        </div>
      )}
      <div className="actions">
        <button className="btn sm" disabled={!libraryOk || running || run.isPending} onClick={() => run.mutate()}>
          Check library now
        </button>
      </div>
    </>
  );
}

function SystemCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({ queryKey: ["system"], queryFn: () => api<SystemStatus>("/api/system/status"), refetchInterval: 20000 });
  const init = useMutation({
    mutationFn: () => post("/api/system/library/initialize", { confirm: true }),
    onSuccess: () => {
      toast("Library initialised");
      qc.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const clearBlock = useMutation({
    mutationFn: () => post<{ resumed_jobs: number }>("/api/system/access-block/clear", { confirm: true }),
    onSuccess: (r) => {
      toast(`Acknowledged; ${r.resumed_jobs} job(s) resumed`);
      qc.invalidateQueries({ queryKey: ["system"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorBox error={error} />;
  const lib = data.library;
  return (
    <section id="system">
      <h2 className="section-title">System</h2>
      <div className="card">
        <div className="row between">
          <strong>Library</strong>
          {lib.ok ? <Badge tone="ok">Available</Badge> : <Badge tone="bad">Unavailable</Badge>}
        </div>
        <div className="small muted" style={{ marginTop: 4 }}>
          <span className="mono">{data.library_root}</span> · filesystem {lib.fs_type ?? "?"} · free {bytes(lib.free_bytes)}
        </div>
        <WorkDirLine label="Downloads in progress" dir={data.staging} empty="empty" />
        <WorkDirLine label="Replaced copies" dir={data.replaced} empty="empty" />
        <div className="small muted" style={{ marginTop: 4 }}>
          Sentinel: <span className="mono">{data.sentinel}</span>
        </div>
        {!lib.ok && <div className="error">{lib.error}</div>}
        {!lib.sentinel_present && lib.is_dir && (
          <div style={{ marginTop: 10 }}>
            <p className="small">
              The library has not been initialised (or the NAS is not mounted). Only initialise it when you are sure the NAS share is mounted: this creates <span className="mono">{data.sentinel}</span>, which the app checks before every download.
            </p>
            <button
              className="btn sm danger"
              onClick={() => window.confirm("Is the NAS share mounted right now? Initialising an unmounted folder would let downloads fill the local disk.") && init.mutate()}
            >
              Initialise library…
            </button>
          </div>
        )}
        <hr style={{ border: 0, borderTop: "1px solid var(--rule)", margin: "14px 0" }} />
        <LibraryCheck libraryOk={lib.ok} />
        <hr style={{ border: 0, borderTop: "1px solid var(--rule)", margin: "14px 0" }} />
        <div className="row between">
          <strong>MangaDex access</strong>
          {data.access_block ? <Badge tone="bad">Restricted</Badge> : data.cooldown ? <Badge tone="warn">{data.cooldown.kind === "maintenance" ? "MangaDex maintenance" : "Cooling down"}</Badge> : <Badge tone="ok">Normal</Badge>}
        </div>
        {data.cooldown && (data.cooldown.kind === "maintenance"
          ? <div className="small muted">MangaDex is down for maintenance — next check {relTime(data.cooldown.until)}.</div>
          : <div className="small muted">Cooldown ends {relTime(data.cooldown.until)} — {data.cooldown.reason} (strike {data.cooldown.strikes}).</div>)}
        {data.access_block && (
          <div style={{ marginTop: 8 }}>
            <div className="error">{data.access_block.message}</div>
            <p className="small">All MangaDex traffic is stopped. Resolve the cause first (wait out an IP restriction, check MangaDex status). The app will not try to circumvent it.</p>
            <button className="btn sm" onClick={() => window.confirm("Resume MangaDex requests now?") && clearBlock.mutate()}>I've resolved it — resume</button>
          </div>
        )}
        <hr style={{ border: 0, borderTop: "1px solid var(--rule)", margin: "14px 0" }} />
        <div className="row">
          <span>Worker {data.components.worker.alive ? <Badge tone="ok">alive</Badge> : <Badge tone="bad">down</Badge>}</span>
          <span>Scheduler {data.components.scheduler.alive ? <Badge tone="ok">alive</Badge> : <Badge tone="bad">down</Badge>}</span>
          <span className="small muted">v{data.version}</span>
        </div>
      </div>
    </section>
  );
}

const SECRET_FILES: { key: keyof AccountStatus["secrets"]; file: string; label: string }[] = [
  { key: "client_id", file: "md_client_id", label: "Client ID" },
  { key: "client_secret", file: "md_client_secret", label: "Client secret" },
  { key: "username", file: "md_username", label: "Username" },
  { key: "password", file: "md_password", label: "Password" },
];

function MangaDexAccountCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({ queryKey: ["md-account"], queryFn: () => api<AccountStatus>("/api/mangadex-account"), refetchInterval: 20000 });
  const test = useMutation({
    mutationFn: () => post<{ username: string }>("/api/mangadex-account/test"),
    onSuccess: (r) => toast(`Connected as ${r.username}`),
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const sync = useMutation({
    mutationFn: () => post<{ queued: Record<string, boolean> }>("/api/mangadex-account/sync", { kind: "all" }),
    onSuccess: () => {
      toast("Sync queued");
      setTimeout(() => qc.invalidateQueries({ queryKey: ["md-account"] }), 4000);
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorBox error={error} />;
  const feed = data.last_feed_result;
  return (
    <section id="mangadex">
      <h2 className="section-title">MangaDex account</h2>
      <div className="card">
        <div className="row between">
          <strong>{data.username ? `Account ${data.username}` : "Followed list & fast updates"}</strong>
          {!data.configured ? <Badge>Not configured</Badge>
            : data.auth_failed ? <Badge tone="bad">Login failed</Badge>
            : !data.enabled ? <Badge tone="warn">Disabled</Badge>
            : <Badge tone="ok">Active</Badge>}
        </div>
        <p className="small muted" style={{ marginTop: 6 }}>
          With your account, one request every {data.feed_interval_minutes} min checks new chapters of <em>every</em> manga you follow on MangaDex, so
          downloads start minutes after release. Titles you follow but haven't added go to <Link to="/approvals">Approvals</Link>. The per-title scans keep running as before.
        </p>
        {!data.configured && (
          <div className="alert info small">
            <div className="body">
              <strong>Credentials are read from Docker secrets</strong>
              Fill in the missing files under the stack's secrets folder (one value per file), then restart the stack. Values are never shown or stored in the database.
            </div>
          </div>
        )}
        <div className="secret-list">
          {SECRET_FILES.map((f) => (
            <span key={f.key}>
              {data.secrets[f.key] ? <Badge tone="ok">✓</Badge> : <Badge tone="warn">missing</Badge>} {f.label} <span className="mono muted">{f.file}</span>
            </span>
          ))}
        </div>
        {data.last_error && (
          <div className="error" style={{ marginTop: 10 }}>
            {data.last_error} <span className="muted">({relTime(data.last_error_at)})</span>
            {data.auth_failed && <div className="small">Login is retried once an hour; check the username/password secrets and that the API client is approved on MangaDex.</div>}
          </div>
        )}
        {data.configured && (
          <>
            <div className="small" style={{ marginTop: 10 }}>
              Followed on MangaDex: <strong>{data.followed}</strong> · waiting for approval: <strong>{data.pending_approval}</strong>
            </div>
            <div className="small muted">
              Followed list read {relTime(data.last_list_sync_at)} · feed checked {relTime(data.last_feed_sync_at)}
              {feed ? ` · last check: ${feed.new} new, ${feed.queued} queued` : ""}
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              <button className="btn sm" onClick={() => test.mutate()} disabled={test.isPending}>Test login</button>
              <button className="btn sm" onClick={() => sync.mutate()} disabled={sync.isPending || !data.enabled}>Sync now</button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

function ArchiveCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["archive-status"], queryFn: () => api<ArchiveStatus>("/api/archive"), refetchInterval: 30000 });
  const rescan = useMutation({
    mutationFn: () => post<{ queued: boolean }>("/api/archive/rescan"),
    onSuccess: (r) => {
      toast(r.queued ? "Archive scan queued (a few minutes)" : "An archive scan is already running");
      setTimeout(() => qc.invalidateQueries({ queryKey: ["archive-status"] }), 5000);
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  if (!data) return null;
  return (
    <section id="archive">
      <h2 className="section-title">Comics archive</h2>
      <div className="card">
        <div className="row between">
          <strong>Read-only CBZ collection</strong>
          {!data.configured ? <Badge>Not mounted</Badge> : !data.enabled ? <Badge tone="warn">Disabled</Badge> : <Badge tone="ok">Active</Badge>}
        </div>
        <p className="small muted" style={{ marginTop: 6 }}>
          When you add or approve a title, the app looks for it in your archive (by every MangaDex title), finds the newest chapter you have —
          from the file names, or inside the newest volume — and offers to continue from the next one. Nothing in the archive is ever changed.
        </p>
        {data.configured ? (
          <>
            <div className="small">
              <span className="mono">{data.root}</span> · <strong>{data.folders.toLocaleString()}</strong> title folders indexed
              {data.indexed_at ? `, ${relTime(data.indexed_at)}` : " (not indexed yet)"} · rescanned every {data.rescan_hours} h
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              <button className="btn sm" onClick={() => rescan.mutate()} disabled={rescan.isPending}>Rescan archive now</button>
            </div>
          </>
        ) : (
          <div className="small muted">Mount the collection read-only at <span className="mono">/archive</span> (MDM_ARCHIVE_ROOT) to enable it.</div>
        )}
      </div>
    </section>
  );
}

const COMMON_LANGS = ["en", "it", "es", "es-la", "fr", "de", "pt-br", "ja"];

function HighlightedTagsCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const { list } = useHighlightedTags();
  const library = useQuery({ queryKey: ["manga"], queryFn: () => api<MangaSummary[]>("/api/manga") });
  const [draft, setDraft] = useState<HighlightedTag[] | null>(null);
  const [q, setQ] = useState("");
  const rows = draft ?? list;
  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(list);

  const counts = useMemo(() => {
    const c = new Map<string, { name: string; count: number }>();
    for (const m of library.data ?? []) {
      for (const t of m.tags ?? []) {
        const e = c.get(tagKey(t));
        if (e) e.count += 1;
        else c.set(tagKey(t), { name: t, count: 1 });
      }
    }
    return c;
  }, [library.data]);
  const pool = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return [...counts.values()].filter((t) => !needle || t.name.toLowerCase().includes(needle)).sort((a, b) => a.name.localeCompare(b.name));
  }, [counts, q]);
  const has = (name: string) => rows.some((t) => tagKey(t.name) === tagKey(name));
  const add = (name: string) => {
    const n = name.trim().replace(/\s+/g, " ");
    if (!n || has(n)) return;
    setDraft([...rows, { name: n, color: nextTagColor(rows) }]);
  };
  const typedIsNew = q.trim() !== "" && !has(q) && !counts.has(tagKey(q));

  const save = useMutation({
    mutationFn: (tags: HighlightedTag[]) => put<{ settings: AppSettings }>("/api/settings", { highlighted_tags: tags }),
    onSuccess: (r) => {
      qc.setQueryData(["settings"], (old: { settings: AppSettings; defaults: AppSettings } | undefined) =>
        old ? { ...old, settings: { ...old.settings, highlighted_tags: r.settings.highlighted_tags } } : old);
      setDraft(null);
      toast("Highlighted tags saved");
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  return (
    <section id="tags">
      <h2 className="section-title">Highlighted tags</h2>
      <div className="card">
        <p className="small muted" style={{ marginTop: 0 }}>
          These MangaDex tags are shown first and in their colour on every title, as badges on the Library and Approvals cards, and as quick filters in the Library.
        </p>
        {rows.length === 0 && <div className="small muted">No highlighted tags yet: pick some below.</div>}
        {rows.map((t, i) => (
          <div className="hl-tag-row" key={tagKey(t.name)}>
            <div style={{ minWidth: 0 }}>
              <TagChip name={t.name} color={t.color} />
              <span className="small muted" style={{ marginLeft: 8 }}>{counts.get(tagKey(t.name))?.count ?? 0} titles</span>
            </div>
            <input type="color" aria-label={`Colour of ${t.name}`} value={t.color}
              onChange={(e) => setDraft(rows.map((x, j) => (j === i ? { ...x, color: e.target.value } : x)))} />
            <div className="row" style={{ gap: 2, flexWrap: "nowrap" }}>
              <button type="button" className="btn sm ghost" aria-label={`Move ${t.name} up`} disabled={i === 0}
                onClick={() => setDraft(rows.map((x, j) => (j === i - 1 ? rows[i] : j === i ? rows[i - 1] : x)))}>↑</button>
              <button type="button" className="btn sm ghost" aria-label={`Move ${t.name} down`} disabled={i === rows.length - 1}
                onClick={() => setDraft(rows.map((x, j) => (j === i + 1 ? rows[i] : j === i ? rows[i + 1] : x)))}>↓</button>
            </div>
            <button type="button" className="btn sm ghost" aria-label={`Remove ${t.name}`} onClick={() => setDraft(rows.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}

        <div className="field" style={{ marginTop: 14 }}>
          <label htmlFor="tagsearch">Add a tag</label>
          <input id="tagsearch" className="input" type="search" placeholder="Search the tags in your library…" value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                if (pool.length === 1 && !has(pool[0].name)) add(pool[0].name);
                else if (typedIsNew) add(q);
              }
            }} />
          <span className="hint">Tap a tag to highlight it. Names are the English MangaDex ones, e.g. Genderswap, Girls' Love.</span>
        </div>
        <div className="tag-pool">
          {pool.map((t) => (
            <button type="button" key={t.name} className="tag" disabled={has(t.name)} onClick={() => add(t.name)}>
              {t.name} <span className="muted">{t.count}</span>
            </button>
          ))}
          {typedIsNew && (
            <button type="button" className="tag" onClick={() => add(q)}>+ Add “{q.trim()}”</button>
          )}
          {library.isLoading && <span className="small muted">Loading tags…</span>}
        </div>

        <div className="row" style={{ marginTop: 14 }}>
          <button type="button" className="btn primary" disabled={!dirty || save.isPending} onClick={() => draft && save.mutate(draft)}>Save tags</button>
          {dirty && <button type="button" className="btn ghost" onClick={() => setDraft(null)}>Discard changes</button>}
        </div>
      </div>
    </section>
  );
}

function AppSettingsForm() {
  const toast = useToast();
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({ queryKey: ["settings"], queryFn: () => api<{ settings: AppSettings; defaults: AppSettings }>("/api/settings") });
  const [form, setForm] = useState<AppSettings | null>(null);
  useEffect(() => {
    if (data) setForm(data.settings);
  }, [data]);
  const save = useMutation({
    // Highlighted tags have their own card and save; never overwrite them from this form.
    mutationFn: ({ highlighted_tags: _tags, ...s }: AppSettings) =>
      put<{ settings: AppSettings; group_exclusions?: { cancelled: number; queued_instead: number } }>("/api/settings", s),
    onSuccess: (r) => {
      qc.setQueryData(["settings"], { settings: r.settings, defaults: data?.defaults });
      const g = r.group_exclusions;
      toast(g ? `Settings saved · ${g.cancelled} job(s) cancelled, ${g.queued_instead} queued from other groups` : "Settings saved");
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  if (isLoading || !form) return error ? <ErrorBox error={error} /> : <Loading />;

  const num = (f: FieldDef) => {
    const raw = form[f.key] as number;
    return f.scale ? raw / f.scale : raw;
  };
  const setNum = (f: FieldDef, v: string) => {
    const n = Number(v);
    if (Number.isNaN(n)) return;
    setForm({ ...form, [f.key]: f.scale ? Math.round(n * f.scale) : n });
  };
  const ratings = (form.content_ratings as string[]) ?? [];

  return (
    <form
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        save.mutate(form);
      }}
    >
      {SECTIONS.map((sec) => (
        <section key={sec.title}>
          <h2 className="section-title">{sec.title}</h2>
          <div className="card">
            {sec.note && <div className="alert info small"><div className="body">{sec.note}</div></div>}
            <div className="settings-grid">
              {sec.fields.map((f) => (
                <div className="field" key={f.key}>
                  <label htmlFor={f.key}>
                    {f.label}
                    {f.unit && <span className="muted"> ({f.unit})</span>}
                  </label>
                  <input id={f.key} className="input" type="number" inputMode="decimal" step={f.step ?? 1} value={num(f)} onChange={(e) => setNum(f, e.target.value)} />
                  {f.hint && <span className="hint">{f.hint}</span>}
                </div>
              ))}
            </div>
          </div>
        </section>
      ))}
      <div className="card" style={{ marginTop: 12 }}>
        <Switch label="Adaptive monitoring levels" hint="Check titles without recent chapters less often (Lv.2 / Lv.3). Off = every title uses its normal interval." checked={form.adaptive_monitoring !== false} onChange={(v) => setForm({ ...form, adaptive_monitoring: v })} />
      </div>
      <h2 className="section-title">Chapter folders</h2>
      <div className="card">
        <Switch label="Scanlation group in the chapter folder name" hint="Chapters of every group in one folder, instead of a folder per group. The group is added at the end (“Volume 03 Chapter 12 - Title - Team X”) only when the same chapter is there from two groups; the copy already downloaded is renamed too." checked={form.group_in_chapter_name !== false} onChange={(v) => setForm({ ...form, group_in_chapter_name: v })} />
        <div className="field" style={{ marginTop: 12 }}>
          <label htmlFor="volume_placeholder">Volume name for chapters without a volume</label>
          <input id="volume_placeholder" className="input" maxLength={20} value={(form.volume_placeholder as string) ?? ""} onChange={(e) => setForm({ ...form, volume_placeholder: e.target.value })} />
          <span className="hint">“TBD” → <span className="mono">Volume TBD Chapter 012</span>, listed after the numbered volumes. Empty = <span className="mono">Chapter 012</span>.</span>
        </div>
        <Switch label="Rename when MangaDex assigns the volume" hint="After each scan, chapter folders still in the library move from “Volume TBD …” to “Volume 07 …”. Folders you already moved away are not affected." checked={form.rename_on_volume_change !== false} onChange={(v) => setForm({ ...form, rename_on_volume_change: v })} />
      </div>
      <h2 className="section-title">MangaDex account sync</h2>
      <div className="card">
        <Switch label="Use the MangaDex account" hint="Fast checks through the followed feed and the followed list. Needs the md_* secrets." checked={form.mangadex_account_enabled !== false} onChange={(v) => setForm({ ...form, mangadex_account_enabled: v })} />
        <div className="settings-grid" style={{ marginTop: 10 }}>
          <div className="field">
            <label htmlFor="follow_feed_interval_minutes">Check followed feed every <span className="muted">(minutes)</span></label>
            <input id="follow_feed_interval_minutes" className="input" type="number" min={5} value={form.follow_feed_interval_minutes as number} onChange={(e) => setForm({ ...form, follow_feed_interval_minutes: Number(e.target.value) })} />
            <span className="hint">One request for every followed manga. Default 15, minimum 5.</span>
          </div>
          <div className="field">
            <label htmlFor="follow_list_interval_minutes">Read followed list every <span className="muted">(hours)</span></label>
            <input id="follow_list_interval_minutes" className="input" type="number" step={0.5} min={0.5} value={(form.follow_list_interval_minutes as number) / 60} onChange={(e) => setForm({ ...form, follow_list_interval_minutes: Math.round(Number(e.target.value) * 60) })} />
            <span className="hint">New follows become titles waiting for approval. Default 6 h.</span>
          </div>
        </div>
        <Switch label="Add new follows for approval" hint="Off = the followed list is read (for the feed) but no new titles are proposed." checked={form.follow_add_pending !== false} onChange={(v) => setForm({ ...form, follow_add_pending: v })} />
        <div className="label" style={{ margin: "12px 0 6px" }}>Languages preselected when approving</div>
        <LanguagePicker value={(form.follow_default_languages as string[]) ?? []} onChange={(v) => setForm({ ...form, follow_default_languages: v })} available={COMMON_LANGS} />
      </div>
      <h2 className="section-title">Comics archive</h2>
      <div className="card">
        <Switch label="Suggest where to start from the archive" hint="Looks up the newest chapter you already have when you add or approve a title." checked={form.archive_enabled !== false} onChange={(v) => setForm({ ...form, archive_enabled: v })} />
        <Switch label="Approve titles already in the archive" hint="Followed titles whose romaji or English title is a folder of the archive are approved automatically — in that folder's language, all groups, continuing after the newest chapter you have (or new chapters only). Unclear cases stay in Approvals. Checked after each archive scan and each followed-list sync." checked={form.archive_auto_approve === true} onChange={(v) => setForm({ ...form, archive_auto_approve: v })} />
        <div className="field" style={{ marginTop: 12 }}>
          <label htmlFor="archive_dirs">Folders to search (all archives)</label>
          <textarea id="archive_dirs" className="input" rows={6} value={((form.archive_dirs as string[]) ?? []).join("\n")}
            onChange={(e) => setForm({ ...form, archive_dirs: e.target.value.split("\n") })} />
          <span className="hint">One folder per line, relative to the archive root; <span className="mono">.</span> searches the whole archive. The language comes from the path (English / Inglese → EN, Italian / Italiano → IT…). Title folders below (Completed, In Progress…) are found automatically; sub-folders of a title (Extra, Previous version…) are ignored.</span>
        </div>
        <div className="field">
          <label htmlFor="archive_custom_dirs">Folders to search for "- Custom" volumes only</label>
          <textarea id="archive_custom_dirs" className="input" rows={4} value={((form.archive_custom_dirs as string[]) ?? []).join("\n")}
            onChange={(e) => setForm({ ...form, archive_custom_dirs: e.target.value.split("\n") })} />
          <span className="hint">Official releases are ignored here; only volumes named <span className="mono">… - Custom.cbz</span> (built from MangaDex chapters) count.</span>
        </div>
        <div className="field">
          <label htmlFor="archive_rescan_hours">Rescan the archive every <span className="muted">(hours)</span></label>
          <input id="archive_rescan_hours" className="input" type="number" min={1} value={form.archive_rescan_hours as number}
            onChange={(e) => setForm({ ...form, archive_rescan_hours: Number(e.target.value) })} />
        </div>
      </div>
      <h2 className="section-title">Content</h2>
      <div className="card">
        <div className="label" style={{ marginBottom: 8 }}>Content ratings to include</div>
        <div className="chips">
          {["safe", "suggestive", "erotica", "pornographic"].map((r) => (
            <button type="button" key={r} className="chip" aria-pressed={ratings.includes(r)} onClick={() => setForm({ ...form, content_ratings: ratings.includes(r) ? ratings.filter((x) => x !== r) : [...ratings, r] })}>
              {r}
            </button>
          ))}
        </div>
        <Switch label="Use data-saver images" hint="Smaller, recompressed images from MangaDex. Off = original quality." checked={!!form.use_data_saver} onChange={(v) => setForm({ ...form, use_data_saver: v })} />
        <div className="field" style={{ marginTop: 12 }}>
          <label htmlFor="excluded_group_names">Never download from these groups</label>
          <textarea
            id="excluded_group_names"
            className="input"
            rows={6}
            value={((form.excluded_group_names as string[]) ?? []).join("\n")}
            onChange={(e) => setForm({ ...form, excluded_group_names: e.target.value.split("\n") })}
          />
          <span className="hint">One MangaDex group name per line (not case-sensitive). Use it for official publishers whose releases only link to a paid site; another group's release of the same chapter is downloaded instead.</span>
        </div>
        <Switch label="Report MangaDex@Home node health" hint="Recommended by MangaDex; helps them route around broken nodes." checked={!!form.report_to_mangadex_at_home} onChange={(v) => setForm({ ...form, report_to_mangadex_at_home: v })} />
      </div>
      <div className="sticky-actions row">
        <button className="btn primary grow" disabled={save.isPending}>Save settings</button>
        <button type="button" className="btn" onClick={() => data && setForm(data.defaults)}>Defaults</button>
      </div>
    </form>
  );
}

function LanguageCodes() {
  const { data } = useLanguages();
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState("");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: ({ code, folder_code }: { code: string; folder_code: string }) => patch(`/api/languages/${code}`, { folder_code }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["languages"] });
      toast("Folder code saved (applies to new downloads)");
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const rows = useMemo(
    () => (data ?? []).filter((l) => !q || l.name.toLowerCase().includes(q.toLowerCase()) || l.code.includes(q.toLowerCase())),
    [data, q],
  );
  return (
    <section>
      <h2 className="section-title">Language folder codes</h2>
      <div className="card">
        <input className="input" type="search" placeholder="Filter languages…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter languages" />
        <div className="list" style={{ marginTop: 10, maxHeight: 360, overflowY: "auto" }}>
          {rows.map((l) => (
            <div className="row" key={l.code}>
              <span className="grow">{l.name} <span className="muted small">{l.code}</span></span>
              <input className="input" style={{ width: 110 }} aria-label={`Folder code for ${l.name}`} value={edits[l.code] ?? l.folder_code} onChange={(e) => setEdits({ ...edits, [l.code]: e.target.value })} />
              <button className="btn sm" disabled={(edits[l.code] ?? l.folder_code) === l.folder_code} onClick={() => save.mutate({ code: l.code, folder_code: edits[l.code] })}>Save</button>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function AccountCard() {
  const { user, logout, setUser } = useAuth();
  const toast = useToast();
  const [cur, setCur] = useState("");
  const [pw, setPw] = useState("");
  const change = useMutation({
    mutationFn: () => post<User>("/api/auth/change-password", { current_password: cur, new_password: pw }),
    onSuccess: (u) => {
      setUser(u);
      setCur("");
      setPw("");
      toast("Password changed; other sessions signed out");
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const logoutAll = useMutation({ mutationFn: () => post("/api/auth/logout-all"), onSuccess: () => setUser(null) });
  return (
    <section>
      <h2 className="section-title">Account</h2>
      <div className="card">
        <div className="row between">
          <span>Signed in as <strong>{user?.username}</strong></span>
          <button className="btn sm" onClick={logout}>Sign out</button>
        </div>
        <form
          style={{ marginTop: 14 }}
          onSubmit={(e) => {
            e.preventDefault();
            change.mutate();
          }}
        >
          <div className="settings-grid">
            <div className="field">
              <label htmlFor="cur">Current password</label>
              <input id="cur" className="input" type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required />
            </div>
            <div className="field">
              <label htmlFor="newpw">New password</label>
              <input id="newpw" className="input" type="password" autoComplete="new-password" minLength={12} value={pw} onChange={(e) => setPw(e.target.value)} required />
            </div>
          </div>
          <div className="row">
            <button className="btn dark" disabled={change.isPending}>Change password</button>
            <button type="button" className="btn ghost" onClick={() => window.confirm("Sign out on every device?") && logoutAll.mutate()}>Sign out everywhere</button>
          </div>
        </form>
      </div>
    </section>
  );
}

function AppearanceCard() {
  const [skin, setSkin] = useSkin();
  const current = SKINS.find((s) => s.id === skin) ?? SKINS[0];
  return (
    <section id="appearance">
      <h2 className="section-title">Appearance</h2>
      <div className="card">
        <div className="row between">
          <div className="grow">
            <strong>Theme</strong>
            <div className="small muted">{current.hint}. Saved in this browser.</div>
          </div>
          <div className="seg" role="group" aria-label="Theme">
            {SKINS.map((s) => (
              <button key={s.id} type="button" aria-pressed={skin === s.id} onClick={() => setSkin(s.id)}>
                {s.label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
