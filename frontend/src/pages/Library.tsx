import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, post } from "../api";
import { Badge, CompletedBadge, Cover, Empty, ErrorBox, Loading, MangaTitle, TierTab, useToast } from "../components";
import { relTime } from "../format";
import type { MangaSummary } from "../types";
import { TagFilter, TagList, countTags, matchesTags, useTagFilter } from "../tags";

type View = "grid" | "list";
const VIEW_KEY = "mdm.library.view";
function loadView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === "list" ? "list" : "grid";
  } catch {
    return "grid";
  }
}

function saveView(v: View) {
  try {
    localStorage.setItem(VIEW_KEY, v);
  } catch {
    /* storage unavailable: the choice just isn't remembered */
  }
}

export default function LibraryPage() {
  const toast = useToast();
  const [q, setQ] = useState("");
  const [level, setLevel] = useState<string>("all");
  const [view, setView] = useState<View>(loadView);
  const tagFilter = useTagFilter("mdm.library.tagMatch");
  const { data, error, isLoading } = useQuery({
    queryKey: ["manga"],
    queryFn: () => api<MangaSummary[]>("/api/manga"),
    refetchInterval: 20000,
  });
  const scanAll = useMutation({
    mutationFn: () => post<{ queued: boolean }>("/api/library/scan"),
    onSuccess: (r) => toast(r.queued ? "Library scan queued" : "A library scan is already running"),
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const active = useMemo(() => (data ?? []).filter((m) => !m.pending_approval), [data]);
  const pending = useMemo(() => (data ?? []).filter((m) => m.pending_approval), [data]);
  const tagFilterKey = tagFilter.tags.join("\u0000");
  const match = tagFilter.match;
  const tagCounts = useMemo(() => countTags(level === "pending" ? pending : active), [active, pending, level]);
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const wanted = tagFilterKey ? tagFilterKey.split("\u0000") : [];
    return (level === "pending" ? pending : active).filter(
      (m) =>
        matchesTags(m.tags, wanted, match) &&
        (!needle || m.title.toLowerCase().includes(needle) || m.canonical_title.toLowerCase().includes(needle)) &&
        (level === "all" || level === "pending" || (level === "done" ? m.upload_complete : String(m.update_tier) === level)),
    );
  }, [active, pending, q, level, tagFilterKey, match]);
  const levelCount = (k: string) =>
    active.filter((m) => (k === "done" ? m.upload_complete : String(m.update_tier) === k)).length;
  const linkTo = (m: MangaSummary) => (m.pending_approval ? `/add?approve=${m.id}` : `/manga/${m.id}`);
  const changeView = (v: View) => {
    setView(v);
    saveView(v);
  };

  return (
    <>
      <div className="topbar">
        <h1 className="page-title">
          <small>{active.length} followed{pending.length ? ` · ${pending.length} to approve` : ""}</small>Library
        </h1>
        <button className="btn sm" onClick={() => scanAll.mutate()} disabled={scanAll.isPending} title="Reconcile the library folder with the database">
          Scan library
        </button>
      </div>
      <div className="field">
        <label htmlFor="libfilter" className="sr-only">Filter</label>
        <input id="libfilter" className="input" type="search" placeholder="Filter your library…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {data && data.length > 0 && (
        <div className="row between" style={{ marginBottom: 12, gap: 10, alignItems: "center" }}>
          <div className="chips" role="group" aria-label="Filter by monitoring level">
            {[["all", "All"], ["1", "Lv.1"], ["2", "Lv.2"], ["3", "Lv.3"], ["done", "Completed"]].map(([k, label]) => (
              <button type="button" key={k} className="chip" aria-pressed={level === k} onClick={() => setLevel(k)} aria-label={label}>
                {k !== "all" && k !== "done" && <span className={`tier tier-${k}`} style={{ marginLeft: 0 }}>{k}</span>}
                {k === "all" || k === "done" ? label : ""}
                <span className="count">{k === "all" ? active.length : levelCount(k)}</span>
              </button>
            ))}
            {pending.length > 0 && (
              <button type="button" className="chip" aria-pressed={level === "pending"} onClick={() => setLevel("pending")} title="Titles from your MangaDex follows waiting for approval">
                To approve<span className="count">{pending.length}</span>
              </button>
            )}
          </div>
          <div className="view-toggle" role="group" aria-label="Library view">
            <button type="button" aria-pressed={view === "grid"} onClick={() => changeView("grid")} title="Grid view">
              <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path fill="currentColor" d="M0 0h6v6H0zM8 0h6v6H8zM0 8h6v6H0zM8 8h6v6H8z" /></svg>
              Grid
            </button>
            <button type="button" aria-pressed={view === "list"} onClick={() => changeView("list")} title="List view">
              <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path fill="currentColor" d="M0 1h14v2H0zM0 6h14v2H0zM0 11h14v2H0z" /></svg>
              List
            </button>
          </div>
        </div>
      )}
      {data && data.length > 0 && <TagFilter filter={tagFilter} counts={tagCounts} />}
      {isLoading && <Loading />}
      {error && <ErrorBox error={error} />}
      {level !== "pending" && pending.length > 0 && (
        <div className="alert info">
          <div className="body">
            <strong>{pending.length} title{pending.length === 1 ? "" : "s"} from your MangaDex follows waiting for approval</strong>
            <Link to="/approvals">Open Approvals</Link> to choose languages, groups and the first chapter.
          </div>
        </div>
      )}
      {data && data.length === 0 && (
        <Empty title="Your library is empty">
          <p>
            <Link className="btn primary" to="/add">Add your first manga</Link>
          </p>
        </Empty>
      )}
      {data && data.length > 0 && filtered.length === 0 && <div className="card muted small">No titles match this filter.</div>}

      {view === "grid" ? (
        <div className="manga-grid">
          {filtered.map((m) => (
            <Link key={m.id} to={linkTo(m)} className="manga-card">
              <TierTab tier={m.update_tier} />
              <Cover src={m.cover_url} alt={`Cover of ${m.title}`} />
              <div style={{ minWidth: 0 }}>
                <MangaTitle title={m.title} lines={2} className={m.update_tier ? "has-tab" : ""} />
                <div className="row" style={{ gap: 6, marginTop: 6 }}>
                  {m.languages.map((l) => (
                    <Badge key={l}>{l.toUpperCase()}</Badge>
                  ))}
                  {m.pending_approval ? <Badge tone="accent">Waiting for approval</Badge>
                    : m.monitoring_enabled ? <Badge tone="ok">Monitored</Badge> : <Badge tone="warn">Paused</Badge>}
                  {!m.auto_download && <Badge>Manual DL</Badge>}
                  <CompletedBadge show={m.upload_complete} />
                </div>
                <TagList tags={m.tags} mode="highlighted" />
                <div className="small muted ellipsis" style={{ marginTop: 6 }}>
                  {m.group_mode === "all" ? "All groups" : m.groups.map((g) => g.name).join(", ")}
                </div>
                <div className="counts">
                  <span><strong>{m.downloaded}</strong> <span className="muted">downloaded</span></span>
                  <span><strong>{m.missing}</strong> <span className="muted">missing</span></span>
                  {m.external > 0 && <span><strong>{m.external}</strong> <span className="muted">owned</span></span>}
                  {m.failed_jobs > 0 && <span style={{ color: "var(--bad)" }}><strong>{m.failed_jobs}</strong> failed</span>}
                </div>
                <div className="small muted" style={{ marginTop: 4 }}>
                  Scanned {relTime(m.last_scan_at)}
                  {m.last_error && <span style={{ color: "var(--bad)" }}> · last scan failed</span>}
                </div>
              </div>
            </Link>
          ))}
        </div>
      ) : (
        filtered.length > 0 && (
          <div className="manga-list">
            {filtered.map((m) => (
              <Link key={m.id} to={linkTo(m)} className="manga-row">
                <Cover src={m.cover_url} alt="" />
                <div style={{ minWidth: 0 }}>
                  <MangaTitle title={m.title} tier={m.update_tier} />
                  <div className="sub">
                    {m.languages.map((l) => (
                      <Badge key={l}>{l.toUpperCase()}</Badge>
                    ))}
                    {m.pending_approval ? <Badge tone="accent">Waiting for approval</Badge> : !m.monitoring_enabled && <Badge tone="warn">Paused</Badge>}
                    {!m.auto_download && <Badge>Manual DL</Badge>}
                    <CompletedBadge show={m.upload_complete} />
                    <span>Scanned {relTime(m.last_scan_at)}</span>
                    {m.last_error && <span style={{ color: "var(--bad)" }}>last scan failed</span>}
                  </div>
                  <TagList tags={m.tags} mode="highlighted" />
                </div>
                <div className="nums">
                  <span><strong>{m.downloaded}</strong> <span className="muted">downloaded</span></span>
                  <span><strong>{m.missing}</strong> <span className="muted">missing</span></span>
                  {m.external > 0 && <span><strong>{m.external}</strong> <span className="muted">owned</span></span>}
                  {m.failed_jobs > 0 && <span style={{ color: "var(--bad)" }}><strong>{m.failed_jobs}</strong> <span>failed</span></span>}
                </div>
              </Link>
            ))}
          </div>
        )
      )}
    </>
  );
}
