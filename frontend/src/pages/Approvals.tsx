import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "../api";
import { Badge, Cover, Empty, ErrorBox, Loading, useToast } from "../components";
import { plainDescription, relTime } from "../format";
import type { AccountStatus, Approvals, PendingManga, ReadingStatus } from "../types";
import { TagFilter, TagList, countTags, matchesTags, useTagFilter } from "../tags";

export const READING_LABEL: Record<ReadingStatus, string> = {
  reading: "Reading",
  plan_to_read: "Plan to read",
  on_hold: "On hold",
  re_reading: "Re-reading",
  completed: "Completed",
  dropped: "Dropped",
};
const READING_TONE: Partial<Record<ReadingStatus, string>> = { reading: "ok", re_reading: "ok", plan_to_read: "info", dropped: "bad" };

/** English title first (see PendingManga.english_title), else the MangaDex title. */
const shownTitle = (it: PendingManga) => it.english_title || it.title;

export default function ApprovalsPage() {
  const toast = useToast();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<string>("all");
  const [showDismissed, setShowDismissed] = useState(false);
  const tagFilter = useTagFilter("mdm.approvals.tagMatch");
  const { data, error, isLoading } = useQuery({ queryKey: ["approvals"], queryFn: () => api<Approvals>("/api/approvals"), refetchInterval: 30000 });
  const account = useQuery({ queryKey: ["md-account"], queryFn: () => api<AccountStatus>("/api/mangadex-account"), refetchInterval: 30000 });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["approvals"] });
    qc.invalidateQueries({ queryKey: ["dashboard"] });
    qc.invalidateQueries({ queryKey: ["manga"] });
  };
  const dismiss = useMutation({
    mutationFn: (id: string) => post(`/api/approvals/${id}/dismiss`),
    onSuccess: () => { refresh(); toast("Dismissed — it won't be proposed again"); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const restore = useMutation({
    mutationFn: (id: string) => post(`/api/approvals/${id}/restore`),
    onSuccess: () => { refresh(); toast("Back in the approval list"); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const sync = useMutation({
    mutationFn: () => post<{ queued: Record<string, boolean> }>("/api/mangadex-account/sync", { kind: "list" }),
    onSuccess: (r) => toast(r.queued.list ? "Reading your MangaDex follows…" : "A sync is already running"),
    onError: (e: Error) => toast(e.message, "bad"),
  });

  const items = data?.items ?? [];
  const tagFilterKey = tagFilter.tags.join("\u0000");
  const tagMatch = tagFilter.match;
  // Each group of chips counts the titles the *other* filters leave, so the numbers say what a click shows.
  const { counts, tagCounts, filtered, shown } = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const wanted = tagFilterKey ? tagFilterKey.split("\u0000") : [];
    const textOk = (it: (typeof items)[number]) =>
      !needle || [it.english_title, it.title, it.canonical_title].some((t) => t?.toLowerCase().includes(needle));
    const statusOk = (it: (typeof items)[number]) => status === "all" || (it.reading_status ?? "none") === status;
    const tagOk = (it: (typeof items)[number]) => matchesTags(it.tags, wanted, tagMatch);
    const c: Record<string, number> = {};
    let shown = 0;
    for (const it of items) {
      if (!textOk(it) || !tagOk(it)) continue;
      shown += 1;
      c[it.reading_status ?? "none"] = (c[it.reading_status ?? "none"] ?? 0) + 1;
    }
    return {
      counts: c,
      shown,
      tagCounts: countTags(items.filter((it) => textOk(it) && statusOk(it))),
      filtered: items.filter((it) => textOk(it) && statusOk(it) && tagOk(it)),
    };
  }, [items, q, status, tagFilterKey, tagMatch]);
  const acc = account.data;

  return (
    <>
      <div className="topbar">
        <h1 className="page-title">
          <small>From your MangaDex follows</small>Approvals
        </h1>
        {acc?.configured && (
          <button className="btn sm" onClick={() => sync.mutate()} disabled={sync.isPending} title="Read the followed list from MangaDex now">
            Check follows now
          </button>
        )}
      </div>

      {acc && !acc.configured && (
        <div className="alert info">
          <div className="body">
            <strong>MangaDex account not connected</strong>
            Titles you follow on MangaDex appear here once the account is set up. See <Link to="/settings#mangadex">Settings → MangaDex account</Link>.
          </div>
        </div>
      )}
      {acc?.last_error && (
        <div className={`alert ${acc.auth_failed ? "bad" : "warn"}`}>
          <div className="body">
            <strong>{acc.auth_failed ? "MangaDex login failed" : "Last MangaDex sync failed"} · {relTime(acc.last_error_at)}</strong>
            {acc.last_error}
          </div>
        </div>
      )}
      <p className="small muted">
        Titles you follow on MangaDex that are not in the library yet. Nothing is scanned or downloaded until you approve them: choose languages,
        groups and the first chapter to download. Dismissed titles are never proposed again.
        {acc?.last_list_sync_at && <> Follows last read {relTime(acc.last_list_sync_at)}{acc.username ? ` (account ${acc.username})` : ""}.</>}
      </p>

      {isLoading && <Loading />}
      {error && <ErrorBox error={error} />}
      {data && items.length === 0 && (
        <Empty title="Nothing to approve">
          <p className="small">New follows on MangaDex will show up here.</p>
        </Empty>
      )}

      {items.length > 0 && (
        <>
          <div className="field">
            <label htmlFor="apfilter" className="sr-only">Filter</label>
            <input id="apfilter" className="input" type="search" placeholder={`Filter ${items.length} title${items.length === 1 ? "" : "s"}…`} value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="chips" role="group" aria-label="Filter by MangaDex reading status" style={{ marginBottom: 12 }}>
            <button type="button" className="chip" aria-pressed={status === "all"} onClick={() => setStatus("all")}>All<span className="count">{shown}</span></button>
            {(Object.keys(READING_LABEL) as ReadingStatus[]).filter((k) => counts[k] || status === k).map((k) => (
              <button type="button" key={k} className="chip" aria-pressed={status === k} onClick={() => setStatus(k)}>
                {READING_LABEL[k]}<span className="count">{counts[k] ?? 0}</span>
              </button>
            ))}
            {(counts.none > 0 || status === "none") && (
              <button type="button" className="chip" aria-pressed={status === "none"} onClick={() => setStatus("none")}>No status<span className="count">{counts.none ?? 0}</span></button>
            )}
          </div>
          <TagFilter filter={tagFilter} counts={tagCounts} />
          {filtered.length !== items.length && (
            <p className="small muted" aria-live="polite">Showing {filtered.length} of {items.length} titles.</p>
          )}
        </>
      )}

      <div className="list">
        {filtered.map((it) => (
          <div className="item approval" key={it.id}>
            <Cover src={it.cover_url} alt={`Cover of ${shownTitle(it)}`} />
            <div style={{ minWidth: 0 }}>
              <div className="title">{shownTitle(it)}</div>
              {shownTitle(it) !== it.title && <div className="small muted">{it.title}</div>}
              <div className="meta">
                {it.reading_status && <Badge tone={READING_TONE[it.reading_status] ?? ""}>{READING_LABEL[it.reading_status]}</Badge>}
                {it.year && <span>{it.year}</span>}
                {it.status && <span>{it.status}</span>}
                {it.last_chapter && <span>last ch. {it.last_chapter}</span>}
                <span>{it.available_languages.length} languages</span>
                {it.pending_since && <span>added {relTime(it.pending_since)}</span>}
              </div>
              <TagList tags={it.tags} mode="highlighted" also={tagFilter.tags} />
              {it.description && <div className="small muted clamp2" style={{ marginTop: 4 }}>{plainDescription(it.description)}</div>}
              <div className="actions">
                <Link className="btn sm primary" to={`/add?approve=${it.id}`}>Configure & approve</Link>
                <button className="btn sm ghost" disabled={dismiss.isPending} onClick={() => window.confirm(`Dismiss “${shownTitle(it)}”? It stays followed on MangaDex but won't be proposed again.`) && dismiss.mutate(it.id)}>
                  Dismiss
                </button>
                <a className="btn sm ghost" href={it.mangadex_url} target="_blank" rel="noreferrer noopener">MangaDex ↗</a>
              </div>
            </div>
          </div>
        ))}
      </div>
      {items.length > 0 && filtered.length === 0 && <div className="card muted small">No titles match this filter.</div>}

      {(data?.dismissed.length ?? 0) > 0 && (
        <section>
          <h2 className="section-title">
            <button type="button" className="btn ghost sm" style={{ paddingLeft: 0 }} onClick={() => setShowDismissed(!showDismissed)} aria-expanded={showDismissed}>
              {showDismissed ? "▾" : "▸"} Dismissed ({data!.dismissed.length})
            </button>
          </h2>
          {showDismissed && (
            <div className="list">
              {data!.dismissed.map((d) => (
                <div className="item row between" key={d.id}>
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="ellipsis">{d.title}</div>
                    <div className="small muted">
                      {d.reading_status ? `${READING_LABEL[d.reading_status]} · ` : ""}dismissed {relTime(d.dismissed_at)}
                    </div>
                  </div>
                  <button className="btn sm" disabled={restore.isPending} onClick={() => restore.mutate(d.id)}>Restore</button>
                </div>
              ))}
            </div>
          )}
        </section>
      )}
    </>
  );
}
