import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "../api";
import { Badge, Empty, ErrorBox, Loading, useToast } from "../components";
import { chapterLabel, relTime } from "../format";
import type { ReviewItem } from "../types";

const KIND: Record<ReviewItem["kind"], string> = {
  unmatched_folder: "No matching release",
  ambiguous_match: "Several possible releases",
  unknown_manga_folder: "Folder not linked to a manga",
  destination_conflict: "Download blocked by existing folder",
  revision_after_move: "Newer revision of a chapter you moved out",
};

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

function RevisionDetails({ d }: { d: Record<string, unknown> }) {
  const groups = Array.isArray(d.groups) ? (d.groups as string[]).join(", ") : "";
  return (
    <div className="small" style={{ marginTop: 4 }}>
      <div>
        <strong>{chapterLabel({ volume: str(d.volume), chapter: str(d.chapter), title: str(d.title) })}</strong>
        {" · "}{(str(d.language) ?? "").toUpperCase()} · {groups || "Unknown Group"}
        {typeof d.pages === "number" ? ` · ${d.pages} pages` : ""}
        {" · "}{d.revision === "version" ? `new MangaDex version ${String(d.version ?? "")}` : "re-uploaded by the same group"}
      </div>
      <div className="muted">
        First downloaded {relTime(str(d.first_downloaded_at))}; you had moved it out of <span className="mono">{str(d.library_path) ?? "—"}</span>.
      </div>
      {str(d.mangadex_url) && (
        <div><a href={str(d.mangadex_url)!} target="_blank" rel="noreferrer noopener">Open on MangaDex</a></div>
      )}
    </div>
  );
}

export default function ReviewPage() {
  const toast = useToast();
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({ queryKey: ["review"], queryFn: () => api<ReviewItem[]>("/api/review") });
  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => post(path, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      toast("Done");
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  return (
    <>
      <div className="topbar">
        <h1 className="page-title">
          <small>Library import</small>Review
        </h1>
      </div>
      <p className="small muted">
        Library folders that could not be matched with certainty (nothing was guessed: linking a folder only adds a hidden <span className="mono">.mdm-release.json</span> marker, your images are never changed), and newer revisions of chapters you had already moved out, saved in the replaced folder at the path shown.
      </p>
      {isLoading && <Loading />}
      {error && <ErrorBox error={error} />}
      {data && data.length === 0 && <Empty title="Nothing to review" />}
      <div className="list">
        {data?.map((it) => (
          <div className="item" key={it.id}>
            <div className="row between">
              <Badge tone={it.kind === "destination_conflict" ? "bad" : it.kind === "revision_after_move" ? "info" : "warn"}>{KIND[it.kind] ?? it.kind}</Badge>
              <span className="small muted">{relTime(it.created_at)}</span>
            </div>
            <div className="mono" style={{ marginTop: 6 }}>{it.path}</div>
            {it.manga && <div className="small"><Link to={`/manga/${it.manga.id}`}>{it.manga.title}</Link></div>}
            {typeof it.details.reason === "string" && <div className="small muted">{it.details.reason}</div>}
            {it.kind === "revision_after_move" ? (
              <RevisionDetails d={it.details} />
            ) : (
              typeof it.details.message === "string" && <div className="small muted">{it.details.message}</div>
            )}

            {it.candidates.length > 0 && it.kind !== "destination_conflict" && (
              <div className="list" style={{ marginTop: 8 }}>
                {it.candidates.map((c) => (
                  <div className="row between card" key={c.release_id} style={{ padding: 10 }}>
                    <div className="grow small">
                      <strong>{chapterLabel({ volume: c.volume ?? null, chapter: c.chapter ?? null, title: null })}</strong>
                      {c.title ? ` · ${c.title}` : ""} · {c.language?.toUpperCase()} · {(c.groups ?? []).join(", ") || "Unknown Group"} · {c.pages} pages
                    </div>
                    <button className="btn sm dark" onClick={() => act.mutate({ path: `/api/review/${it.id}/link`, body: { release_id: c.release_id } })}>
                      This one
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="actions">
              {it.kind === "destination_conflict" && (
                <>
                  <button className="btn sm dark" onClick={() => act.mutate({ path: `/api/review/${it.id}/link`, body: { release_id: it.candidates[0]?.release_id } })}>
                    Folder is this release
                  </button>
                  <button className="btn sm" onClick={() => act.mutate({ path: `/api/review/${it.id}/download-separately` })}>
                    Download to a separate folder
                  </button>
                </>
              )}
              {it.kind === "unknown_manga_folder" && (
                <Link className="btn sm dark" to={`/add?q=${encodeURIComponent(it.path)}&folder=${encodeURIComponent(it.path)}`}>
                  Find on MangaDex & import
                </Link>
              )}
              <button className="btn sm ghost" onClick={() => act.mutate({ path: `/api/review/${it.id}/dismiss` })}>
                {it.kind === "revision_after_move" ? "Seen" : "Dismiss"}
              </button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
