import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "../api";
import { Badge, Cover, ErrorBox, Switch, useToast } from "../components";
import { GroupPicker, LanguagePicker, type GroupChoice, CUSTOM_TITLE, TitlePicker } from "../pickers";
import type { ChapterPreview, Dashboard, GroupStat, MangaDetail, SearchResult, TitleChoice } from "../types";
import { plainDescription, relTime } from "../format";
import { ArchiveHint } from "../archive";
import { TagList } from "../tags";

const UUID_OR_URL = /mangadex\.org|^[0-9a-f]{8}-[0-9a-f]{4}-/i;

export default function AddPage() {
  const [params] = useSearchParams();
  const [q, setQ] = useState(params.get("q") ?? "");
  const [submitted, setSubmitted] = useState(params.get("q") ?? "");
  const [picked, setPicked] = useState<SearchResult | null>(null);
  const existingFolder = params.get("folder");
  const approveId = params.get("approve");
  const nav = useNavigate();
  const dash = useQuery({ queryKey: ["dashboard"], queryFn: () => api<Dashboard>("/api/dashboard"), staleTime: 15_000 });
  const waiting = dash.data?.pending_approvals ?? 0;

  const search = useQuery({
    queryKey: ["search", submitted],
    queryFn: () => api<{ items: SearchResult[]; total: number }>(`/api/mangadex/search?q=${encodeURIComponent(submitted)}`),
    enabled: submitted.trim().length > 0,
    staleTime: 60_000,
  });

  useEffect(() => {
    // A pasted URL/UUID resolves to exactly one manga: select it directly.
    // Already followed: never open the form, point to the existing entry instead.
    if (search.data?.items.length === 1 && UUID_OR_URL.test(submitted) && !search.data.items[0].followed) setPicked(search.data.items[0]);
  }, [search.data, submitted]);

  function onSearch(e: FormEvent) {
    e.preventDefault();
    setPicked(null);
    setSubmitted(q.trim());
  }

  if (approveId && !picked) return <ApproveLoader id={approveId} existingFolder={existingFolder} />;

  return (
    <>
      <div className="topbar">
        <h1 className="page-title">
          <small>MangaDex</small>Add manga
        </h1>
      </div>
      {waiting > 0 && !existingFolder && (
        <div className="alert info">
          <div className="body">
            <strong>{waiting} title{waiting === 1 ? "" : "s"} from your MangaDex follows waiting for approval</strong>
            <Link to="/approvals">Review and approve them</Link> to choose languages, groups and the first chapter.
          </div>
        </div>
      )}
      {existingFolder && (
        <div className="alert info">
          <div className="body">
            <strong>Importing existing folder “{existingFolder}”</strong>
            Pick the matching manga; its existing chapters will be recognised and only missing releases downloaded.
          </div>
        </div>
      )}
      <form onSubmit={onSearch} className="row" role="search">
        <label htmlFor="q" className="sr-only">Search title or paste MangaDex URL</label>
        <input id="q" className="input grow" placeholder="Search a title, or paste a MangaDex URL / UUID" value={q} onChange={(e) => setQ(e.target.value)} autoCapitalize="none" enterKeyHint="search" />
        <button className="btn dark">Search</button>
      </form>

      {search.isFetching && <div className="muted small" style={{ marginTop: 12 }}>Searching…</div>}
      {search.error && <div style={{ marginTop: 12 }}><ErrorBox error={search.error} /></div>}

      {!picked && search.data?.items.length === 1 && search.data.items[0].followed && UUID_OR_URL.test(submitted) && (
        <div className="alert info" style={{ marginTop: 14 }}>
          <div className="body">
            <strong>Already in your library</strong>
            This MangaDex link is a manga you already follow, so it can't be added twice.{" "}
            <Link to={`/manga/${search.data.items[0].id}`}>Open it</Link>
          </div>
        </div>
      )}
      {!picked && search.data && (
        <div className="list" style={{ marginTop: 14 }}>
          {search.data.items.length === 0 && <div className="muted">No results.</div>}
          {search.data.items.map((r) => (
            <button key={r.id} className="item search-result" aria-pressed={false}
              onClick={() => (r.followed ? nav(`/manga/${r.id}`) : setPicked(r))}
              title={r.followed ? "Already in your library — open it" : undefined}>
              <Cover src={r.cover_url} alt="" />
              <div style={{ minWidth: 0 }}>
                <div className="title">{r.title}</div>
                <div className="meta">
                  {r.year && <span>{r.year}</span>}
                  {r.status && <span>{r.status}</span>}
                  {r.content_rating && <span>{r.content_rating}</span>}
                  <span>{r.available_languages.length} languages</span>
                </div>
                {r.followed ? <Badge tone="ok">Already in library · open ›</Badge>
                  : r.pending ? <Badge tone="accent">From your MangaDex follows · waiting for approval</Badge>
                  : <div className="small muted ellipsis">{r.alt_titles.join(" · ")}</div>}
              </div>
            </button>
          ))}
        </div>
      )}

      {picked && <ConfigureForm manga={picked} existingFolder={existingFolder} onBack={() => setPicked(null)} approving={!!picked.pending} />}
    </>
  );
}

/** Approval of a title added from the MangaDex followed list: the add form, pre-filled. */
function ApproveLoader({ id, existingFolder }: { id: string; existingFolder: string | null }) {
  const nav = useNavigate();
  const detail = useQuery({ queryKey: ["manga", id], queryFn: () => api<MangaDetail>(`/api/manga/${id}`) });
  const preview = useQuery({
    queryKey: ["md-preview", id],
    queryFn: () => api<SearchResult & { titles?: TitleChoice[] }>(`/api/mangadex/manga/${id}`),
    staleTime: 5 * 60_000,
    retry: false,
  });
  if (detail.isLoading || (preview.isLoading && !preview.error)) return <div className="muted small">Loading…</div>;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const d = detail.data!;
  if (!d.pending_approval) {
    return (
      <div className="alert info">
        <div className="body">
          <strong>{d.title} is already approved</strong>
          <Link to={`/manga/${d.id}`}>Open it</Link>
        </div>
      </div>
    );
  }
  const p = preview.data;
  const manga: SearchResult = {
    id: d.id,
    title: d.canonical_title,
    alt_titles: [],
    year: d.year,
    status: d.status,
    content_rating: d.content_rating,
    description: d.description ?? "",
    available_languages: p?.available_languages?.length ? p.available_languages : d.available_languages,
    cover_url: d.cover_url,
    followed: false,
    pending: true,
    titles: p?.titles ?? [],
  };
  const initial = d.languages.filter((l) => manga.available_languages.includes(l));
  return (
    <>
      <div className="topbar">
        <h1 className="page-title">
          <small>From your MangaDex follows</small>Approve
        </h1>
      </div>
      {preview.error && <div className="small muted">MangaDex is not reachable right now; alternative titles are not listed.</div>}
      <ConfigureForm manga={manga} existingFolder={existingFolder} approving initialLanguages={initial.length ? initial : undefined}
        onBack={() => nav("/approvals")} backLabel="← Back to approvals" />
    </>
  );
}

function ConfigureForm({ manga, existingFolder, onBack, approving = false, initialLanguages, backLabel }: {
  manga: SearchResult;
  existingFolder: string | null;
  onBack: () => void;
  approving?: boolean;
  initialLanguages?: string[];
  backLabel?: string;
}) {
  const nav = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();
  const defaultLang = initialLanguages ?? (manga.available_languages.includes("en") ? ["en"] : manga.available_languages.slice(0, 1));
  const [languages, setLanguages] = useState<string[]>(defaultLang);
  const [groups, setGroups] = useState<GroupChoice>({ mode: "all", ids: [], includeUnknown: true });
  const [monitoring, setMonitoring] = useState(true);
  const [autoDownload, setAutoDownload] = useState(true);
  const [titleChoice, setTitleChoice] = useState<string>(() => defaultTitle(manga));
  const [customTitle, setCustomTitle] = useState("");
  const chosenTitle = titleChoice === CUSTOM_TITLE ? customTitle.trim() : titleChoice;
  const [startChapter, setStartChapter] = useState("");
  const [onlyNew, setOnlyNew] = useState(false);
  const pickStart = (v: string) => { setOnlyNew(false); setStartChapter(v); };
  const pickOnlyNew = () => { setOnlyNew(true); setStartChapter(""); };

  const groupQuery = useQuery({
    queryKey: ["preview-groups", manga.id, [...languages].sort().join(",")],
    queryFn: () => api<{ groups: GroupStat[]; unknown_group_releases: Record<string, number>; chapters?: ChapterPreview[] }>(
      `/api/mangadex/manga/${manga.id}/groups?languages=${encodeURIComponent(languages.join(","))}`),
    enabled: languages.length > 0,
    staleTime: 5 * 60_000,
  });

  const add = useMutation({
    mutationFn: () =>
      post<MangaDetail>("/api/manga", {
        reference: manga.id,
        languages,
        group_mode: groups.mode,
        group_ids: groups.mode === "selected" ? groups.ids : [],
        include_unknown_group: groups.includeUnknown,
        monitoring_enabled: monitoring,
        auto_download: autoDownload,
        title_override: existingFolder || !chosenTitle || chosenTitle === manga.title ? null : chosenTitle,
        existing_folder: existingFolder,
        start_chapter: !onlyNew && startChapter.trim() ? Number(startChapter.replace(",", ".")) : null,
        only_new: onlyNew,
      }),
    onSuccess: (m) => {
      qc.invalidateQueries({ queryKey: ["manga"] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      const verb = approving ? "Approved" : "Added";
      toast(existingFolder ? `${verb} — scanning existing folder` : `${verb} — first scan queued`);
      nav(`/manga/${m.id}`);
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  const invalid = languages.length === 0 || (groups.mode === "selected" && groups.ids.length === 0);

  return (
    <form
      className="card"
      style={{ marginTop: 14 }}
      onSubmit={(e) => {
        e.preventDefault();
        if (!invalid) add.mutate();
      }}
    >
      <div className="hero">
        <Cover src={manga.cover_url} alt={`Cover of ${manga.title}`} />
        <div>
          <h1>{manga.title}</h1>
          <TagList tags={(manga as { tags?: string[] }).tags} linked={false} />
          <div className="small muted clamp2" style={{ whiteSpace: "pre-line" }}>{plainDescription(manga.description)}</div>
          <a className="small" href={`https://mangadex.org/title/${manga.id}`} target="_blank" rel="noreferrer noopener">Open on MangaDex ↗</a>
          {approving && <div style={{ marginTop: 6 }}><Badge tone="accent">From your MangaDex follows</Badge></div>}
          <button type="button" className="btn ghost sm" onClick={onBack} style={{ marginTop: 6, paddingLeft: 0 }}>
            {backLabel ?? "← Choose another"}
          </button>
        </div>
      </div>

      <h2 className="section-title">Languages</h2>
      <LanguagePicker value={languages} onChange={setLanguages} available={manga.available_languages} />

      <h2 className="section-title">Scanlation groups</h2>
      <GroupPicker
        groups={groupQuery.data?.groups ?? []}
        unknown={groupQuery.data?.unknown_group_releases}
        value={groups}
        onChange={setGroups}
        loading={groupQuery.isFetching}
      />
      {groupQuery.error && <ErrorBox error={groupQuery.error} />}

      <h2 className="section-title">Monitoring</h2>
      <Switch label="Monitor for new chapters" checked={monitoring} onChange={setMonitoring} />
      <Switch label="Download new releases automatically" hint="If off, new releases are listed as missing until you download them." checked={autoDownload} onChange={setAutoDownload} />

      <h2 className="section-title">Chapters on MangaDex</h2>
      <ArchiveHint mangaId={manga.id} languages={languages} onUse={pickStart} onUpToDate={pickOnlyNew} />
      <ChapterSummary
        chapters={groupQuery.data?.chapters}
        loading={groupQuery.isFetching}
        groups={groups}
        languages={languages}
        startChapter={startChapter}
        onStart={pickStart}
        onlyNew={onlyNew}
        onOnlyNew={pickOnlyNew}
        autoDownload={autoDownload}
      />
      <div className="field" style={{ marginTop: 8 }}>
        <label htmlFor="startch">Download from chapter (optional)</label>
        <input id="startch" className="input" inputMode="decimal" list="chapter-numbers" placeholder={onlyNew ? "New chapters only" : "All chapters"} value={startChapter} onChange={(e) => pickStart(e.target.value)} />
        <span className="hint">
          Only chapters with this number or higher are downloaded. Extras without a number are always included,
          and so are lower chapter numbers that first appear on MangaDex from now on (late extras like 12.5).
          Already have everything up to Ch. N? Enter N+1, or pick <em>New chapters only</em> if you have all of them.
        </span>
      </div>

      <h2 className="section-title">Title and folder name</h2>
      <div className="field">
        {existingFolder ? (
          <span className="hint">Using the existing folder “{existingFolder}”.</span>
        ) : (
          <>
            <TitlePicker
              titles={manga.titles ?? []}
              languages={languages}
              value={titleChoice}
              custom={customTitle}
              current={manga.title}
              onChange={(v, c) => { setTitleChoice(v); setCustomTitle(c); }}
            />
            <span className="hint">Shown in the library and used as the folder name. Titles in the selected languages are listed first; you can change it later from the manga page.</span>
          </>
        )}
      </div>

      <div className="sticky-actions">
        <button className="btn primary block" disabled={invalid || add.isPending}>
          {add.isPending ? (approving ? "Approving…" : "Adding…") : approving ? "Approve and add to library" : "Add to library"}
        </button>
        {invalid && <div className="small muted" style={{ textAlign: "center", marginTop: 6 }}>Choose at least one language{groups.mode === "selected" ? " and one group" : ""}.</div>}
      </div>
    </form>
  );
}

const isNum = (v: string | null): v is string => v !== null && v.trim() !== "" && !Number.isNaN(Number(v));

export function ChapterSummary({ chapters, loading, groups, languages, startChapter, onStart, autoDownload = true, onlyNew = false, onOnlyNew }: {
  chapters: ChapterPreview[] | undefined;
  loading: boolean;
  groups: GroupChoice;
  languages: string[];
  startChapter: string;
  onStart: (v: string) => void;
  autoDownload?: boolean;
  onlyNew?: boolean;
  onOnlyNew?: () => void;
}) {
  if (!chapters) {
    return <div className="small muted">{loading ? "Reading the chapter list from MangaDex…" : languages.length ? "" : "Choose a language to see the chapters."}</div>;
  }
  const shown = chapters.filter((c) =>
    groups.mode === "all" || c.groups.some((g) => (g === "" ? groups.includeUnknown : groups.ids.includes(g))));
  const numbered = shown.filter((c) => isNum(c.chapter));
  const nums = [...new Set(numbered.map((c) => Number(c.chapter)))].sort((a, b) => a - b);
  const extras = shown.length - numbered.length;
  if (shown.length === 0) {
    return <div className="small muted">{loading ? "Reading the chapter list from MangaDex…" : "No downloadable chapters in the selected languages and groups."}</div>;
  }
  const first = nums[0];
  const last = nums[nums.length - 1];
  const latest = numbered.filter((c) => Number(c.chapter) === last).map((c) => c.published).filter(Boolean).sort().pop() ?? null;
  const ints = new Set(nums.map((n) => Math.floor(n)));
  let gaps = 0;
  if (nums.length) for (let i = Math.max(1, Math.floor(first)); i <= Math.floor(last); i++) if (!ints.has(i)) gaps++;
  const perLang = languages.length > 1
    ? languages.map((l) => `${l.toUpperCase()} ${new Set(numbered.filter((c) => c.language === l).map((c) => Number(c.chapter))).size}`).join(" · ")
    : null;
  const start = startChapter.trim() ? Number(startChapter.replace(",", ".")) : null;
  const fromStart = start === null || Number.isNaN(start) ? nums.length : nums.filter((n) => n >= start).length;
  const fmt = (n: number) => String(n);
  return (
    <div className="chapter-summary">
      <div>
        <strong>{nums.length} chapter{nums.length === 1 ? "" : "s"}</strong>
        {nums.length > 0 && <> · Ch. {fmt(first)} → {fmt(last)}</>}
        {extras > 0 && <> · {extras} without a number (one-shots, extras)</>}
      </div>
      {nums.length > 0 && <div className="small muted">Latest: Ch. {fmt(last)}{latest ? `, ${relTime(latest)}` : ""}{perLang ? ` · ${perLang}` : ""}</div>}
      {gaps > 0 && <div className="small muted">{gaps} chapter number{gaps === 1 ? " is" : "s are"} missing in this range (often removed from MangaDex).</div>}
      {nums.length > 0 && (
        <>
          <div className="row" style={{ marginTop: 8, gap: 6 }}>
            <button type="button" className="chip" aria-pressed={!onlyNew && !startChapter.trim()} onClick={() => onStart("")}>All chapters</button>
            {nums.length > 10 && (
              <button type="button" className="chip" aria-pressed={start === nums[nums.length - 10]} onClick={() => onStart(fmt(nums[nums.length - 10]))}>
                Last 10 (from Ch. {fmt(nums[nums.length - 10])})
              </button>
            )}
            <button type="button" className="chip" aria-pressed={!onlyNew && start === last} onClick={() => onStart(fmt(last))}>Only the latest (Ch. {fmt(last)})</button>
            {onOnlyNew && (
              <button type="button" className="chip" aria-pressed={onlyNew} onClick={onOnlyNew}
                title="You already have every chapter listed: download only chapters released from now on">
                New chapters only (after Ch. {fmt(last)})
              </button>
            )}
          </div>
          <div className="small" style={{ marginTop: 6 }}>
            {onlyNew ? (
              <>Nothing listed now is downloaded: the {nums.length} chapter{nums.length === 1 ? "" : "s"}{extras > 0 ? ` and ${extras} extra${extras === 1 ? "" : "s"}` : ""} on
                MangaDex are marked as <em>already owned</em>. Only chapters released from now on (Ch. {fmt(last)}.5, {fmt(Math.floor(last) + 1)}, …) {autoDownload ? "will be downloaded." : "will be listed as missing (automatic downloads are off)."}</>
            ) : <>
            {start === null ? `All ${nums.length} numbered chapters` : `${fromStart} numbered chapter${fromStart === 1 ? "" : "s"}`}
            {extras > 0 ? ` and ${extras} extra${extras === 1 ? "" : "s"}` : ""}{" "}
            {autoDownload ? "will be downloaded after you add it." : "will be listed as missing (automatic downloads are off)."}
            </>}
          </div>
          <datalist id="chapter-numbers">{nums.map((n) => <option key={n} value={fmt(n)} />)}</datalist>
        </>
      )}
    </div>
  );
}

const normTitle = (t: string) => t.normalize("NFKC").toLowerCase().replace(/[\W_]+/gu, " ").trim();

/** The only English title MangaDex lists, if there is exactly one; otherwise the main title. */
export function defaultTitle(manga: Pick<SearchResult, "title" | "titles">): string {
  const en = new Map<string, string>();
  for (const t of manga.titles ?? []) if (t.language === "en" && !en.has(normTitle(t.title))) en.set(normTitle(t.title), t.title);
  return en.size === 1 ? [...en.values()][0] : manga.title;
}
