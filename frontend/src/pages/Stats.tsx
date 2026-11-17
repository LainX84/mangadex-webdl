import { useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Cover, ErrorBox, Icon, Loading, MangaTitle, TierBadge } from "../components";
import { absTime, bytes, relTime } from "../format";
import { BarList, Donut, Heatmap, Seg, TimeChart, compact, num, type BarItem, type TimePoint } from "../charts";

/* ------------------------------------------------------------------ API types */

interface Breakdown { name: string; series: number; chapters: number; pages: number }
interface StatsOptions {
  since: string | null;
  series: { id: string; title: string; chapters: number }[];
  tags: { name: string; group: string; series: number }[];
  languages: { code: string; name: string; chapters: number }[];
  groups: { name: string; chapters: number }[];
  types: { value: string; series: number }[];
  ratings: { value: string; series: number }[];
  statuses: { value: string; series: number }[];
  demographics: { value: string; series: number }[];
}
interface Stats {
  generated_at: string;
  since: string | null;
  range: { from: string; to: string; days: number; bucket: "day" | "week" | "month" };
  filtered: { titles: boolean; releases: boolean; series_selected: number };
  summary: {
    chapters: number; pages: number; bytes: number; series: number; volumes: number; chapters_without_volume: number;
    groups: number; languages: number; active_days: number; per_active_day: number; avg_pages: number; avg_bytes: number;
    avg_page_bytes: number; redownloads: number; reuploads: number; refreshes: number; download_seconds: number;
    pages_per_minute: number | null; median_arrival_seconds: number | null; arrival_samples: number; series_added: number;
    scans: number; scan_failures: number; requests: number; new_releases_found: number; retries: number; cancelled: number;
    recovered: number;
  };
  previous: {
    from: string; to: string; available: boolean; chapters: number; pages: number; bytes: number; series: number;
    delta_pct: { chapters: number | null; pages: number | null; bytes: number | null };
  };
  now: {
    series_followed: number; monitored: number; auto_download: number; pending_approval: number; upload_complete: number;
    tiers: Record<"1" | "2" | "3", number>;
    library: { chapters: number; pages: number; bytes: number; volumes: number; tbd: number; moved_out: number; owned_elsewhere: number; skipped: number };
    queue: { active: number; pending: number; paused: number; failed: number };
  };
  timeline: { t: string; chapters: number; pages: number; bytes: number; series_added: number; scans: number; requests: number; cumulative: number }[];
  heatmap: number[][];
  top_series: { id: string; title: string; cover_url: string | null; tier: number | null; chapters: number; pages: number; bytes: number; last_at: string }[];
  series_with_downloads: number;
  by_tag: Record<string, Breakdown[]>;
  by_type: Breakdown[];
  by_rating: Breakdown[];
  by_status: Breakdown[];
  by_demographic: Breakdown[];
  by_language: { code: string; name: string; chapters: number; pages: number }[];
  by_group: { name: string; series: number; chapters: number; pages: number }[];
  groups_total: number;
  by_trigger: { name: string; chapters: number }[];
  records: {
    busiest_day: { date: string; chapters: number } | null;
    biggest_chapter: { manga_id: string; title: string; volume: string | null; chapter: string | null; chapter_title: string | null; pages: number; bytes: number; at: string } | null;
    longest_streak: number;
    current_streak: number;
  };
}

/* ------------------------------------------------------------------ helpers */

const TZ = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
})();

const LIST_KEYS = ["manga", "tag", "type", "rating", "status", "demographic", "lang", "group"] as const;
type ListKey = (typeof LIST_KEYS)[number];

const PRESETS = [
  { value: "7", label: "7D" },
  { value: "30", label: "30D" },
  { value: "90", label: "90D" },
  { value: "365", label: "1Y" },
  { value: "all", label: "All" },
] as const;

const TYPE_LABEL: Record<string, string> = { manga: "Manga", manhwa: "Manhwa", manhua: "Manhua", other: "Other" };
const DEMO_LABEL: Record<string, string> = { shounen: "Shounen", shoujo: "Shoujo", seinen: "Seinen", josei: "Josei", none: "None" };
const RATING_LABEL: Record<string, string> = { safe: "Safe", suggestive: "Suggestive", erotica: "Erotica", pornographic: "Pornographic", unknown: "Unknown" };
const STATUS_LABEL: Record<string, string> = { ongoing: "Ongoing", completed: "Completed", hiatus: "Hiatus", cancelled: "Cancelled", unknown: "Unknown" };
const TRIGGER_LABEL: Record<string, string> = {
  monitor: "Per-title scans",
  follow_feed: "MangaDex followed feed",
  manual: "Manual",
  manual_redownload: "Manual redownload",
};
const TAG_GROUP_LABEL: Record<string, string> = { genre: "Genres", theme: "Themes", format: "Format", content: "Content", other: "Other" };

const label = (map: Record<string, string>, v: string) => map[v] ?? v.charAt(0).toUpperCase() + v.slice(1);
const isoDay = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parseDay = (s: string) => {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
};

function duration(sec: number | null | undefined): string {
  if (sec == null) return "—";
  if (sec < 90) return `${Math.round(sec)} s`;
  if (sec < 5400) return `${Math.round(sec / 60)} min`;
  if (sec < 172800) return `${(sec / 3600).toFixed(1)} h`;
  return `${(sec / 86400).toFixed(1)} days`;
}

function fmtDay(s: string, opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" }) {
  return parseDay(s).toLocaleDateString("en", opts);
}

type Metric = "chapters" | "pages" | "bytes" | "series_added" | "scans" | "requests";
const METRICS: { value: Metric; label: string }[] = [
  { value: "chapters", label: "Chapters" },
  { value: "pages", label: "Pages" },
  { value: "bytes", label: "Data" },
  { value: "series_added", label: "Series added" },
  { value: "scans", label: "Scans" },
  { value: "requests", label: "Requests" },
];
const fmtMetric = (m: Metric) => (m === "bytes" ? (n: number) => bytes(n) : (n: number) => num(n));

/* ------------------------------------------------------------------ small bits */

function Delta({ pct, show }: { pct: number | null; show: boolean }) {
  if (!show || pct == null) return null;
  const up = pct >= 0;
  return (
    <span className={`delta ${up ? "up" : "down"}`} title="Compared with the previous period of the same length">
      {up ? "▲" : "▼"} {Math.abs(pct)}%
    </span>
  );
}

function Kpi({ value, label: lbl, sub, delta, tone }: { value: ReactNode; label: string; sub?: ReactNode; delta?: ReactNode; tone?: string }) {
  return (
    <div className={`stat kpi ${tone ?? ""}`}>
      <div className="num">{value}</div>
      <div className="lbl">{lbl}</div>
      {(sub || delta) && <div className="kpi-sub">{delta}{sub && <span className="muted">{sub}</span>}</div>}
    </div>
  );
}

function Panel({ title, actions, children, wide }: { title: string; actions?: ReactNode; children: ReactNode; wide?: boolean }) {
  return (
    <section className={`card panel${wide ? " wide" : ""}`}>
      <div className="panel-head">
        <h2 className="section-title">{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

/* ------------------------------------------------------------------ filter UI */

function ChipFilter({ title, options, selected, onToggle, labelOf }: {
  title: string;
  options: { value: string; series: number }[];
  selected: string[];
  onToggle: (v: string) => void;
  labelOf: (v: string) => string;
}) {
  if (options.length < 2 && selected.length === 0) return null;
  return (
    <div className="fgroup">
      <div className="flabel">{title}</div>
      <div className="chips">
        {options.map((o) => (
          <button key={o.value} type="button" className="chip" aria-pressed={selected.includes(o.value)} onClick={() => onToggle(o.value)}>
            {labelOf(o.value)} <span className="count">{o.series}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function SearchAdd({ placeholder, items, onPick }: {
  placeholder: string;
  items: { key: string; label: string; hint?: string }[];
  onPick: (key: string) => void;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const hits = useMemo(() => {
    const k = q.trim().toLowerCase();
    if (!k) return items.slice(0, 12);
    return items.filter((i) => i.label.toLowerCase().includes(k)).slice(0, 12);
  }, [q, items]);
  return (
    <div className="search-add">
      <input
        className="input" type="search" placeholder={placeholder} value={q}
        onChange={(e) => { setQ(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && hits[0]) { onPick(hits[0].key); setQ(""); }
          if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && hits.length > 0 && (
        <div className="search-pop" role="listbox">
          {hits.map((h) => (
            <button key={h.key} type="button" role="option" aria-selected={false}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => { onPick(h.key); setQ(""); setOpen(false); }}>
              <span className="ellipsis">{h.label}</span>
              {h.hint && <span className="muted small">{h.hint}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ page */

export default function StatsPage() {
  const [params, setParams] = useSearchParams();
  const [showFilters, setShowFilters] = useState(false);
  const [live, setLive] = useState(true);
  const [metric, setMetric] = useState<Metric>("chapters");
  const [cumulative, setCumulative] = useState(true);
  const [topMetric, setTopMetric] = useState<"chapters" | "pages" | "bytes">("chapters");
  const [tagGroup, setTagGroup] = useState<string>("genre");
  const [tagMetric, setTagMetric] = useState<"chapters" | "series">("chapters");
  const [pieMetric, setPieMetric] = useState<"series" | "chapters">("series");

  const list = (k: ListKey) => params.getAll(k);
  const preset = params.get("range") ?? (params.get("from") || params.get("to") ? "custom" : "all");
  const bucket = params.get("bucket") ?? "auto";
  const tagMatch = params.get("tag_match") === "all" ? "all" : "any";

  const update = (fn: (p: URLSearchParams) => void) => {
    const p = new URLSearchParams(params);
    fn(p);
    setParams(p, { replace: true });
  };
  const toggle = (k: ListKey, v: string) =>
    update((p) => {
      const cur = p.getAll(k);
      p.delete(k);
      (cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]).forEach((x) => p.append(k, x));
    });
  const setPreset = (v: string) =>
    update((p) => {
      p.delete("from");
      p.delete("to");
      if (v === "all") p.delete("range");
      else p.set("range", v);
    });
  const setDates = (from: string, to: string) =>
    update((p) => {
      p.delete("range");
      if (from) p.set("from", from); else p.delete("from");
      if (to) p.set("to", to); else p.delete("to");
    });
  const clearAll = () =>
    update((p) => {
      LIST_KEYS.forEach((k) => p.delete(k));
      p.delete("tag_match");
    });

  // Query string sent to the API
  const query = useMemo(() => {
    const q = new URLSearchParams();
    q.set("tz", TZ);
    if (preset !== "all" && preset !== "custom") {
      const to = new Date();
      const from = new Date();
      from.setDate(to.getDate() - (Number(preset) - 1));
      q.set("from", isoDay(from));
      q.set("to", isoDay(to));
    } else if (preset === "custom") {
      const f = params.get("from");
      const t = params.get("to");
      if (f) q.set("from", f);
      if (t) q.set("to", t);
    }
    q.set("bucket", bucket);
    LIST_KEYS.forEach((k) => params.getAll(k).forEach((v) => q.append(k, v)));
    if (params.getAll("tag").length > 1) q.set("tag_match", tagMatch);
    return q.toString();
  }, [params, preset, bucket, tagMatch]);

  const { data: opts } = useQuery({
    queryKey: ["stats-options"],
    queryFn: () => api<StatsOptions>("/api/stats/options"),
    staleTime: 60_000,
    refetchInterval: live ? 120_000 : false,
  });
  const { data: d, error, isLoading, isFetching, dataUpdatedAt, refetch } = useQuery({
    queryKey: ["stats", query],
    queryFn: () => api<Stats>(`/api/stats?${query}`),
    placeholderData: keepPreviousData,
    refetchInterval: live ? 30_000 : false,
  });

  const seriesTitle = useMemo(() => new Map((opts?.series ?? []).map((s) => [s.id, s.title])), [opts]);
  const activeFilters: { k: ListKey; v: string; text: string }[] = LIST_KEYS.flatMap((k) =>
    list(k).map((v) => ({
      k,
      v,
      text:
        k === "manga" ? seriesTitle.get(v) ?? "Series"
        : k === "type" ? label(TYPE_LABEL, v)
        : k === "rating" ? label(RATING_LABEL, v)
        : k === "status" ? label(STATUS_LABEL, v)
        : k === "demographic" ? label(DEMO_LABEL, v)
        : k === "lang" ? (opts?.languages.find((l) => l.code === v)?.name ?? v.toUpperCase())
        : v,
    })),
  );

  if (isLoading && !d) return <Loading />;
  if (error && !d) return <ErrorBox error={error} />;
  if (!d) return null;

  const s = d.summary;
  const showDelta = d.previous.available && preset !== "all";
  const fm = fmtMetric(metric);
  const tlLabel = (t: string) =>
    d.range.bucket === "month" ? fmtDay(t, { month: "short", year: "2-digit" }) : fmtDay(t);
  const tlLong = (t: string) => {
    if (d.range.bucket === "month") return fmtDay(t, { month: "long", year: "numeric" });
    if (d.range.bucket === "week") return `Week of ${fmtDay(t, { day: "numeric", month: "long", year: "numeric" })}`;
    return fmtDay(t, { weekday: "short", day: "numeric", month: "long", year: "numeric" });
  };
  const points: TimePoint[] = d.timeline.map((p) => ({
    key: p.t,
    label: tlLabel(p.t),
    long: tlLong(p.t),
    value: p[metric],
    line: cumulative && metric === "chapters" ? p.cumulative : undefined,
  }));
  // Click on a bar: narrow the period to it (a day shows just that day's activity).
  const zoom = (key: string) => {
    const start = parseDay(key);
    let end = new Date(start);
    if (d.range.bucket === "week") end.setDate(start.getDate() + 6);
    else if (d.range.bucket === "month") end = new Date(start.getFullYear(), start.getMonth() + 1, 0);
    update((p) => {
      p.delete("range");
      p.set("from", isoDay(start));
      p.set("to", isoDay(end));
      if (d.range.bucket !== "day") p.set("bucket", "day");
    });
  };

  const tagItems: BarItem[] = (d.by_tag[tagGroup] ?? [])
    .filter((t) => t[tagMetric] > 0)
    .sort((a, b) => b[tagMetric] - a[tagMetric])
    .map((t) => ({
      key: t.name,
      label: t.name,
      value: t[tagMetric],
      sub: tagMetric === "chapters" ? `${t.series} series` : `${num(t.chapters)} ch.`,
      active: list("tag").some((x) => x.toLowerCase() === t.name.toLowerCase()),
    }));
  const pie = (rows: Breakdown[], k: ListKey, map: Record<string, string>): BarItem[] =>
    rows
      .filter((r) => r[pieMetric] > 0)
      .sort((a, b) => b[pieMetric] - a[pieMetric])
      .map((r) => ({ key: r.name, label: label(map, r.name), value: r[pieMetric], active: list(k).includes(r.name) }));
  const topFmt = topMetric === "bytes" ? (n: number) => bytes(n) : num;
  const top = [...d.top_series].sort((a, b) => b[topMetric] - a[topMetric]);
  const topMax = Math.max(1, ...top.map((t) => t[topMetric]));
  const tiers = d.now.tiers;
  const tierTotal = tiers["1"] + tiers["2"] + tiers["3"] || 1;
  const updated = dataUpdatedAt ? new Date(dataUpdatedAt).toISOString() : d.generated_at;

  return (
    <div className="stats-page">
      <div className="topbar">
        <h1 className="page-title">
          <small>Since {d.since ? new Date(d.since).toLocaleDateString("en", { day: "numeric", month: "long", year: "numeric" }) : "—"}</small>
          Statistics
        </h1>
        <div className="row live-row">
          <button type="button" className={`live ${live ? "on" : ""}`} onClick={() => setLive(!live)} aria-pressed={live}
            title={live ? "Auto-refresh every 30 s — click to pause" : "Auto-refresh paused — click to resume"}>
            <span className="pulse" aria-hidden="true" />
            {live ? "Live" : "Paused"}
          </button>
          <span className="small muted" title={absTime(updated)}>{isFetching ? "updating…" : `updated ${relTime(updated)}`}</span>
          <button type="button" className="btn sm" onClick={() => refetch()} aria-label="Refresh now"><Icon name="refresh" /></button>
        </div>
      </div>

      {/* ------------------------------------------------ filters */}
      <div className="card filters">
        <div className="row filter-top">
          <Seg label="Period" value={PRESETS.some((p) => p.value === preset) ? preset : "custom"}
            onChange={setPreset} items={[...PRESETS]} />
          <div className="row dates">
            <input className="input sm" type="date" aria-label="From" value={preset === "custom" ? params.get("from") ?? "" : d.range.from}
              min={d.since?.slice(0, 10)} max={isoDay(new Date())}
              onChange={(e) => setDates(e.target.value, preset === "custom" ? params.get("to") ?? "" : d.range.to)} />
            <span className="muted">→</span>
            <input className="input sm" type="date" aria-label="To" value={preset === "custom" ? params.get("to") ?? "" : d.range.to}
              max={isoDay(new Date())}
              onChange={(e) => setDates(preset === "custom" ? params.get("from") ?? "" : d.range.from, e.target.value)} />
          </div>
          <Seg label="Group by" value={bucket} onChange={(v) => update((p) => (v === "auto" ? p.delete("bucket") : p.set("bucket", v)))}
            items={[{ value: "auto", label: "Auto" }, { value: "day", label: "Day" }, { value: "week", label: "Week" }, { value: "month", label: "Month" }]} />
          <button type="button" className={`btn sm ${showFilters ? "dark" : ""}`} onClick={() => setShowFilters(!showFilters)} aria-expanded={showFilters}>
            Filters{activeFilters.length > 0 && <span className="badge accent">{activeFilters.length}</span>}
          </button>
        </div>

        {activeFilters.length > 0 && (
          <div className="active-filters">
            {activeFilters.map((f) => (
              <button key={`${f.k}:${f.v}`} type="button" className="fchip" onClick={() => toggle(f.k, f.v)} title="Remove filter">
                <span className="muted">{f.k === "manga" ? "series" : f.k === "lang" ? "language" : f.k}:</span> {f.text} <Icon name="x" />
              </button>
            ))}
            {list("tag").length > 1 && (
              <Seg label="Tag match" value={tagMatch} onChange={(v) => update((p) => p.set("tag_match", v))}
                items={[{ value: "any", label: "Any tag" }, { value: "all", label: "All tags" }]} />
            )}
            <button type="button" className="btn sm ghost" onClick={clearAll}>Clear all</button>
          </div>
        )}

        {showFilters && opts && (
          <div className="filter-body">
            <div className="fgrid">
              <div className="fgroup">
                <div className="flabel">Series</div>
                <SearchAdd placeholder="Search a title…"
                  items={opts.series.filter((x) => !list("manga").includes(x.id)).map((x) => ({ key: x.id, label: x.title, hint: x.chapters ? `${x.chapters} ch.` : undefined }))}
                  onPick={(k) => toggle("manga", k)} />
              </div>
              <div className="fgroup">
                <div className="flabel">Genres, themes &amp; tags</div>
                <SearchAdd placeholder="Search a tag…"
                  items={opts.tags.filter((t) => !list("tag").includes(t.name)).map((t) => ({ key: t.name, label: t.name, hint: `${TAG_GROUP_LABEL[t.group] ?? t.group} · ${t.series}` }))}
                  onPick={(k) => toggle("tag", k)} />
              </div>
              <div className="fgroup">
                <div className="flabel">Scanlation group</div>
                <SearchAdd placeholder="Search a group…"
                  items={opts.groups.filter((g) => !list("group").includes(g.name)).map((g) => ({ key: g.name, label: g.name, hint: `${g.chapters} ch.` }))}
                  onPick={(k) => toggle("group", k)} />
              </div>
            </div>
            <div className="fgroup">
              <div className="flabel">Genres</div>
              <div className="chips">
                {opts.tags.filter((t) => t.group === "genre").map((t) => (
                  <button key={t.name} type="button" className="chip" aria-pressed={list("tag").includes(t.name)} onClick={() => toggle("tag", t.name)}>
                    {t.name} <span className="count">{t.series}</span>
                  </button>
                ))}
              </div>
            </div>
            <ChipFilter title="Type" options={opts.types} selected={list("type")} onToggle={(v) => toggle("type", v)} labelOf={(v) => label(TYPE_LABEL, v)} />
            <ChipFilter title="Demographic" options={opts.demographics} selected={list("demographic")} onToggle={(v) => toggle("demographic", v)} labelOf={(v) => label(DEMO_LABEL, v)} />
            <ChipFilter title="Content rating" options={opts.ratings} selected={list("rating")} onToggle={(v) => toggle("rating", v)} labelOf={(v) => label(RATING_LABEL, v)} />
            <ChipFilter title="Publication status" options={opts.statuses} selected={list("status")} onToggle={(v) => toggle("status", v)} labelOf={(v) => label(STATUS_LABEL, v)} />
            <ChipFilter title="Chapter language" options={opts.languages.map((l) => ({ value: l.code, series: l.chapters }))} selected={list("lang")}
              onToggle={(v) => toggle("lang", v)} labelOf={(v) => opts.languages.find((l) => l.code === v)?.name ?? v} />
          </div>
        )}
      </div>

      <div className="small muted range-line">
        {fmtDay(d.range.from, { day: "numeric", month: "short", year: "numeric" })} – {fmtDay(d.range.to, { day: "numeric", month: "short", year: "numeric" })}
        {" · "}{d.range.days} day{d.range.days === 1 ? "" : "s"} · by {d.range.bucket}
        {d.filtered.titles && <> · {num(d.filtered.series_selected)} matching series</>}
        {showDelta && <> · compared with {fmtDay(d.previous.from)} – {fmtDay(d.previous.to)}</>}
      </div>

      {/* ------------------------------------------------ KPIs */}
      <div className="stats kpis">
        <Kpi value={compact(s.chapters)} label="Chapters" delta={<Delta pct={d.previous.delta_pct.chapters} show={showDelta} />}
          sub={`${s.per_active_day}/active day`} tone="accent" />
        <Kpi value={compact(s.pages)} label="Pages" delta={<Delta pct={d.previous.delta_pct.pages} show={showDelta} />}
          sub={`${s.avg_pages}/chapter`} />
        <Kpi value={bytes(s.bytes)} label="Downloaded" delta={<Delta pct={d.previous.delta_pct.bytes} show={showDelta} />}
          sub={`${bytes(s.avg_bytes)}/chapter`} />
        <Kpi value={num(s.series)} label="Series" sub={`${num(s.series_added)} added`} />
        <Kpi value={num(s.volumes)} label="Volumes" sub={`+${num(s.chapters_without_volume)} ch. w/o volume`} />
        <Kpi value={duration(s.median_arrival_seconds)} label="Arrival time"
          sub={s.arrival_samples ? `median, ${num(s.arrival_samples)} new ch.` : "no new chapters"} />
      </div>

      {/* ------------------------------------------------ timeline */}
      <Panel title="Activity" wide actions={
        <div className="row">
          <Seg label="Metric" value={metric} onChange={setMetric} items={METRICS} />
          {metric === "chapters" && (
            <label className="mini-check"><input type="checkbox" checked={cumulative} onChange={(e) => setCumulative(e.target.checked)} /> Running total</label>
          )}
        </div>
      }>
        <TimeChart points={points} format={fm} lineFormat={num} lineLabel="Total so far" onSelect={zoom} />
      </Panel>

      <div className="panels">
        {/* ------------------------------------------------ now */}
        <Panel title="Right now">
          <div className="now-grid">
            <div><div className="big-n">{num(d.now.series_followed)}</div><div className="small muted">series followed</div></div>
            <div><div className="big-n">{num(d.now.library.chapters)}</div><div className="small muted">chapters in library</div></div>
            <div><div className="big-n">{bytes(d.now.library.bytes)}</div><div className="small muted">on the NAS</div></div>
            <div><div className="big-n">{num(d.now.library.volumes)}</div><div className="small muted">volumes in library</div></div>
          </div>
          <dl className="kv">
            <dt>Pages in library</dt><dd>{num(d.now.library.pages)}</dd>
            <dt>Chapters moved out</dt><dd title="Folders removed from the library (e.g. packed into CBZ)">{num(d.now.library.moved_out)}</dd>
            <dt>Owned elsewhere</dt><dd>{num(d.now.library.owned_elsewhere)}</dd>
            <dt>Skipped</dt><dd>{num(d.now.library.skipped)}</dd>
            <dt>Monitored · auto-download</dt><dd>{num(d.now.monitored)} · {num(d.now.auto_download)}</dd>
            <dt>Fully uploaded on MangaDex</dt><dd>{num(d.now.upload_complete)}</dd>
            <dt>Waiting for approval</dt><dd>{d.now.pending_approval ? <Link to="/approvals">{num(d.now.pending_approval)}</Link> : 0}</dd>
            <dt>Queue</dt>
            <dd>
              <Link to="/queue">{d.now.queue.active} active · {d.now.queue.pending} pending</Link>
              {d.now.queue.failed > 0 && <span className="bad-text"> · {d.now.queue.failed} failed</span>}
            </dd>
          </dl>
          <div className="flabel" style={{ marginTop: 12 }}>Monitoring levels</div>
          <div className="tierbar" role="img" aria-label="Monitoring levels">
            {(["1", "2", "3"] as const).map((t) => tiers[t] > 0 && (
              <span key={t} className={`tier-${t}`} style={{ width: `${(tiers[t] / tierTotal) * 100}%` }}
                title={`Level ${t}: ${tiers[t]} series`}>{tiers[t]}</span>
            ))}
          </div>
          <div className="small muted">1 active · 2 slow · 3 dormant/complete</div>
        </Panel>

        {/* ------------------------------------------------ when */}
        <Panel title="When chapters arrive">
          <Heatmap data={d.heatmap} />
          <dl className="kv" style={{ marginTop: 14 }}>
            <dt>Busiest day</dt>
            <dd>{d.records.busiest_day ? <>{fmtDay(d.records.busiest_day.date, { day: "numeric", month: "short", year: "numeric" })} · {d.records.busiest_day.chapters} ch.</> : "—"}</dd>
            <dt>Active days</dt><dd>{num(s.active_days)} of {num(d.range.days)}</dd>
            <dt>Longest streak · current</dt><dd>{d.records.longest_streak} · {d.records.current_streak} days</dd>
            <dt>Download speed</dt><dd>{s.pages_per_minute != null ? `${s.pages_per_minute} pages/min` : "—"}</dd>
            <dt>Time spent downloading</dt><dd>{duration(s.download_seconds)}</dd>
            <dt>Biggest chapter</dt>
            <dd>
              {d.records.biggest_chapter ? (
                <Link className="kv-link" to={`/manga/${d.records.biggest_chapter.manga_id}`} title={d.records.biggest_chapter.title}>
                  <span className="kv-title">{d.records.biggest_chapter.title}</span>
                  <span className="kv-meta">
                    {d.records.biggest_chapter.volume ? `Vol. ${d.records.biggest_chapter.volume} ` : ""}
                    {d.records.biggest_chapter.chapter ? `Ch. ${d.records.biggest_chapter.chapter}` : "Oneshot"} · {d.records.biggest_chapter.pages} p. · {bytes(d.records.biggest_chapter.bytes)}
                  </span>
                </Link>
              ) : "—"}
            </dd>
          </dl>
        </Panel>
      </div>

      {/* ------------------------------------------------ top series */}
      <Panel title={`Top series · ${num(d.series_with_downloads)} with downloads`} wide actions={
        <Seg label="Rank by" value={topMetric} onChange={setTopMetric}
          items={[{ value: "chapters", label: "Chapters" }, { value: "pages", label: "Pages" }, { value: "bytes", label: "Data" }]} />
      }>
        {top.length === 0 ? <div className="muted small">Nothing downloaded in this period.</div> : (
          <div className="top-series">
            {top.map((t, i) => {
              const active = list("manga").includes(t.id);
              return (
                <div className={`ts-row${active ? " active" : ""}`} key={t.id}>
                  <span className="ts-rank">{i + 1}</span>
                  <Cover src={t.cover_url} alt="" />
                  <button type="button" className="ts-main" onClick={() => toggle("manga", t.id)} aria-pressed={active}
                    title={active ? "Remove series filter" : "Show only this series"}>
                    <div className="row" style={{ gap: 4, flexWrap: "nowrap" }}>
                      <MangaTitle className="title" title={t.title} />
                      <TierBadge tier={t.tier} />
                    </div>
                    <div className="bl-track"><span style={{ width: `${(t[topMetric] / topMax) * 100}%` }} /></div>
                    <div className="meta">
                      <span>{topFmt(t[topMetric])}</span>
                      {topMetric !== "chapters" && <span>{t.chapters} ch.</span>}
                      {topMetric !== "pages" && <span>{num(t.pages)} p.</span>}
                      {topMetric !== "bytes" && <span>{bytes(t.bytes)}</span>}
                      <span title={absTime(t.last_at)}>last {relTime(t.last_at)}</span>
                    </div>
                  </button>
                  <Link className="btn sm ghost" to={`/manga/${t.id}`} aria-label={`Open ${t.title}`}><Icon name="external" /></Link>
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      <div className="panels">
        {/* ------------------------------------------------ tags */}
        <Panel title="Genres & tags" actions={
          <Seg label="Measure" value={tagMetric} onChange={setTagMetric} items={[{ value: "chapters", label: "Chapters" }, { value: "series", label: "Series" }]} />
        }>
          <div className="tabs compact-tabs" role="tablist">
            {Object.keys(TAG_GROUP_LABEL).filter((g) => (d.by_tag[g] ?? []).length > 0).map((g) => (
              <button key={g} role="tab" type="button" className="tab" aria-selected={tagGroup === g} onClick={() => setTagGroup(g)}>
                {TAG_GROUP_LABEL[g]}<span className="count">{d.by_tag[g].length}</span>
              </button>
            ))}
          </div>
          <BarList items={tagItems} format={num} onToggle={(k) => toggle("tag", k)} />
        </Panel>

        {/* ------------------------------------------------ groups & languages */}
        <Panel title={`Scanlation groups · ${num(d.groups_total)}`}>
          <BarList
            items={d.by_group.map((g) => ({ key: g.name, label: g.name, value: g.chapters, sub: `${g.series} series`, active: list("group").includes(g.name) }))}
            format={num} onToggle={(k) => toggle("group", k)} limit={10} />
          {d.by_language.length > 0 && (
            <>
              <div className="flabel" style={{ marginTop: 16 }}>Languages</div>
              <BarList
                items={d.by_language.map((l) => ({ key: l.code, label: l.name, value: l.chapters, sub: `${num(l.pages)} p.`, active: list("lang").includes(l.code), color: "var(--info)" }))}
                format={num} onToggle={(k) => toggle("lang", k)} />
            </>
          )}
        </Panel>
      </div>

      {/* ------------------------------------------------ composition */}
      <Panel title="Collection mix" wide actions={
        <Seg label="Measure" value={pieMetric} onChange={setPieMetric} items={[{ value: "series", label: "Series" }, { value: "chapters", label: "Chapters" }]} />
      }>
        <div className="donuts">
          <div><div className="flabel">Type</div><Donut items={pie(d.by_type, "type", TYPE_LABEL)} format={num} onToggle={(k) => toggle("type", k)} /></div>
          <div><div className="flabel">Demographic</div><Donut items={pie(d.by_demographic, "demographic", DEMO_LABEL)} format={num} onToggle={(k) => toggle("demographic", k)} /></div>
          <div><div className="flabel">Content rating</div><Donut items={pie(d.by_rating, "rating", RATING_LABEL)} format={num} onToggle={(k) => toggle("rating", k)} /></div>
          <div><div className="flabel">Publication status</div><Donut items={pie(d.by_status, "status", STATUS_LABEL)} format={num} onToggle={(k) => toggle("status", k)} /></div>
        </div>
      </Panel>

      <div className="panels">
        {/* ------------------------------------------------ sources */}
        <Panel title="How chapters were found">
          <Donut items={d.by_trigger.map((t) => ({ key: t.name, label: label(TRIGGER_LABEL, t.name), value: t.chapters }))} format={num} />
          <dl className="kv" style={{ marginTop: 12 }}>
            <dt>Redownloads</dt><dd>{num(s.redownloads)}</dd>
            <dt>Same-group re-uploads</dt><dd>{num(s.reuploads)}</dd>
            <dt>Refreshed (new MangaDex version)</dt><dd>{num(s.refreshes)}</dd>
          </dl>
        </Panel>

        {/* ------------------------------------------------ monitoring */}
        <Panel title="Monitoring & reliability">
          <dl className="kv">
            <dt>Per-title scans</dt><dd>{num(s.scans)}</dd>
            <dt>Failed scans</dt><dd className={s.scan_failures ? "warn-text" : ""}>{num(s.scan_failures)}{s.scans ? ` (${((s.scan_failures / s.scans) * 100).toFixed(1)}%)` : ""}</dd>
            <dt>MangaDex API requests (scans)</dt><dd>{num(s.requests)}</dd>
            <dt>Releases discovered</dt><dd>{num(s.new_releases_found)}</dd>
            <dt>Download retries</dt><dd>{num(s.retries)}</dd>
            <dt>Recovered after a crash</dt><dd>{num(s.recovered)}</dd>
            <dt>Cancelled</dt><dd>{num(s.cancelled)}</dd>
            <dt>Avg page size</dt><dd>{bytes(s.avg_page_bytes)}</dd>
          </dl>
        </Panel>
      </div>
    </div>
  );
}
