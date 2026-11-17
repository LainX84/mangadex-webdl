import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Badge, Cover, Empty, ErrorBox, JobProgress, Loading, MangaTitle, TierTab } from "../components";
import { absTime, chapterLabel, relTime } from "../format";
import type { Dashboard } from "../types";

export function SystemAlerts({ d }: { d: Dashboard }) {
  return (
    <>
      {d.access_block && (
        <div className="alert bad" role="alert">
          <div className="body">
            <strong>MangaDex access restricted (HTTP {d.access_block.status})</strong>
            All MangaDex requests are stopped. Check the cause (e.g. IP restriction), then acknowledge it in{" "}
            <Link to="/settings#system">Settings → System</Link>. The app never tries to bypass a restriction.
          </div>
        </div>
      )}
      {d.library && !d.library.ok && (
        <div className="alert bad" role="alert">
          <div className="body">
            <strong>Library unavailable — downloads paused</strong>
            {d.library.error}
          </div>
        </div>
      )}
      {d.cooldown && d.cooldown.kind === "maintenance" && (
        <div className="alert warn" role="status">
          <div className="body">
            <strong>MangaDex is down for maintenance · next check {relTime(d.cooldown.until)}</strong>
            Scans and downloads are on hold and resume automatically when MangaDex is back.
          </div>
        </div>
      )}
      {d.cooldown && d.cooldown.kind !== "maintenance" && (
        <div className="alert warn" role="status">
          <div className="body">
            <strong>Rate-limit cooldown · resumes {relTime(d.cooldown.until)}</strong>
            {d.cooldown.reason}. Requests are delayed, not retried faster.
          </div>
        </div>
      )}
      {d.queue_paused && (
        <div className="alert info" role="status">
          <div className="body">
            <strong>Download queue paused</strong>
            <Link to="/queue">Resume it from the queue</Link> when you're ready.
          </div>
        </div>
      )}
      {(!d.components.worker.alive || !d.components.scheduler.alive) && (
        <div className="alert warn" role="status">
          <div className="body">
            <strong>Background service not responding</strong>
            {!d.components.worker.alive && "Worker "}
            {!d.components.scheduler.alive && "Scheduler "}
            heartbeat missing. Check the containers.
          </div>
        </div>
      )}
      {(d.pending_approvals ?? 0) > 0 && (
        <div className="alert info" role="status">
          <div className="body">
            <strong>{d.pending_approvals} title(s) from your MangaDex follows waiting for approval</strong>
            <Link to="/approvals">Approve them</Link> to choose languages, groups and the first chapter.
          </div>
        </div>
      )}
      {d.mangadex_account?.auth_failed && (
        <div className="alert warn" role="status">
          <div className="body">
            <strong>MangaDex account login failed</strong>
            Fast updates through the followed feed are paused; the per-title scans keep running. <Link to="/settings#mangadex">Check the account</Link>.
          </div>
        </div>
      )}
      {(d.open_revisions ?? 0) > 0 && (
        <div className="alert warn" role="status">
          <div className="body">
            <strong>{d.open_revisions} newer revision(s) of chapters you already moved out</strong>
            A group re-uploaded or edited them after you moved them; the new copies are in the replaced folder.{" "}
            <Link to="/review">See which ones</Link>.
          </div>
        </div>
      )}
      {d.open_reviews - (d.open_revisions ?? 0) > 0 && (
        <div className="alert info" role="status">
          <div className="body">
            <strong>{d.open_reviews - (d.open_revisions ?? 0)} library item(s) need review</strong>
            <Link to="/review">Open the review list</Link> to confirm ambiguous folders.
          </div>
        </div>
      )}
    </>
  );
}

export default function DashboardPage() {
  const { data: d, error, isLoading } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/api/dashboard"),
    refetchInterval: 5000,
  });
  if (isLoading) return <Loading />;
  if (error || !d) return <ErrorBox error={error} />;

  return (
    <>
      <div className="topbar">
        <h1 className="page-title">
          <small>Overview</small>Dashboard
        </h1>
        <div className="row">
          <Link className="btn" to="/stats">
            Statistics
          </Link>
          <Link className="btn primary" to="/add">
            Add manga
          </Link>
        </div>
      </div>

      <SystemAlerts d={d} />

      <div className="stats">
        <Link className="stat accent" to="/queue">
          <div className="num">{d.jobs.active}</div>
          <div className="lbl">Downloading</div>
        </Link>
        <Link className="stat" to="/queue?group=pending">
          <div className="num">{d.jobs.pending}</div>
          <div className="lbl">Pending</div>
        </Link>
        <Link className={`stat ${d.jobs.failed ? "bad" : ""}`} to="/queue?group=failed">
          <div className="num">{d.jobs.failed}</div>
          <div className="lbl">Failed</div>
        </Link>
        <Link className="stat" to="/library">
          <div className="num">{d.followed_manga}</div>
          <div className="lbl">Followed · {d.monitored_manga} monitored</div>
        </Link>
      </div>

      <div className="card" style={{ marginTop: 12 }}>
        <div className="row between">
          <div>
            <div className="small muted">Last successful scan</div>
            <div title={absTime(d.last_successful_scan)}>{relTime(d.last_successful_scan)}</div>
          </div>
          <div>
            <div className="small muted">Next scheduled scan</div>
            <div title={absTime(d.next_scheduled_scan)}>{d.next_scheduled_scan ? relTime(d.next_scheduled_scan) : "—"}</div>
          </div>
          <div>
            <div className="small muted">Completed releases</div>
            <div>{d.completed_releases}</div>
          </div>
          <div>
            <div className="small muted">Rate limit</div>
            <div>{d.cooldown ? <Badge tone="warn">{d.cooldown.kind === "maintenance" ? "MangaDex maintenance" : "Cooling down"}</Badge> : <Badge tone="ok">Normal</Badge>}</div>
          </div>
        </div>
      </div>

      <h2 className="section-title">Active downloads</h2>
      {d.active_downloads.length === 0 ? (
        <div className="card muted small">Nothing downloading right now.</div>
      ) : (
        <div className="list">
          {d.active_downloads.map((j) => (
            <div className="item tabbed" key={j.id}>
              <TierTab tier={j.manga?.update_tier} />
              <div className="row">
                <Cover src={j.manga?.cover_url ?? null} alt="" />
                <div className="grow" style={{ minWidth: 0 }}>
                  <MangaTitle className="title has-tab" title={j.manga?.title} />
                  <div className="meta">
                    {j.release && <span>{chapterLabel(j.release)}</span>}
                    {j.release && <span>{j.release.language.toUpperCase()}</span>}
                    {j.release && <span>{j.release.groups.join(", ") || "Unknown Group"}</span>}
                  </div>
                  <div style={{ marginTop: 8 }}>
                    {j.status === "queued" ? <span className="small muted">Starting…</span> : <JobProgress job={j} />}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="row between section-title-row">
        <h2 className="section-title">Recent downloads</h2>
        <Link className="btn sm ghost" to="/queue?group=completed">More ›</Link>
      </div>
      {!d.recent_downloads?.length ? (
        <div className="card muted small">No chapters downloaded yet.</div>
      ) : (
        <div className="list recent-dl">
          {d.recent_downloads.map((j) => (
            <Link className="item tabbed" key={j.id} to={`/manga/${j.manga?.id}`} style={{ textDecoration: "none" }}>
              <TierTab tier={j.manga?.update_tier} />
              <div className="row">
                <Cover src={j.manga?.cover_url ?? null} alt="" />
                <div className="grow" style={{ minWidth: 0 }}>
                  <MangaTitle className="title has-tab" title={j.manga?.title} />
                  <div className="meta">
                    {j.release && <span>{chapterLabel(j.release)}{j.release.title ? ` · ${j.release.title}` : ""}</span>}
                    {j.release && <span>{j.release.language.toUpperCase()}</span>}
                    {j.release && <span>{j.release.groups.join(" & ") || "Unknown Group"}</span>}
                    {j.pages_total ? <span>{j.pages_total} p.</span> : null}
                    {j.is_redownload && <span>redownload</span>}
                    <span className="when" title={absTime(j.finished_at)}>{relTime(j.finished_at)}</span>
                  </div>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}

      <h2 className="section-title">Recent monitoring activity</h2>
      {d.recent_scans.length === 0 ? (
        <Empty title="No scans yet">Add a manga to start monitoring.</Empty>
      ) : (
        <div className="list">
          {d.recent_scans.map((s) => (
            <Link className="item" key={s.id} to={`/manga/${s.manga_id}`} style={{ textDecoration: "none" }}>
              <div className="row between">
                <div className="title ellipsis grow">{s.manga_title}</div>
                <Badge tone={s.status === "success" ? "ok" : s.status === "failed" ? "bad" : "info"}>{s.status}</Badge>
              </div>
              <div className="meta">
                <span>{relTime(s.started_at)}</span>
                <span>{s.full_scan ? "full" : "incremental"}</span>
                <span>{s.new_releases} new</span>
                <span>{s.queued} queued</span>
                <span>{s.requests_made} requests</span>
              </div>
              {s.error && <div className="error">{s.error}</div>}
            </Link>
          ))}
        </div>
      )}
    </>
  );
}
