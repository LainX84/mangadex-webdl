import type { JobStatus, ReleaseBrief } from "./types";

export function relTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = Math.round((t - now) / 1000);
  const abs = Math.abs(diff);
  let text: string;
  if (abs < 45) text = `${abs}s`;
  else if (abs < 3600) text = `${Math.round(abs / 60)}m`;
  else if (abs < 86400 * 2) text = `${Math.round(abs / 3600)}h`;
  else text = `${Math.round(abs / 86400)}d`;
  if (abs < 10) return "just now";
  return diff > 0 ? `in ${text}` : `${text} ago`;
}

export function absTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

export function chapterLabel(r: Pick<ReleaseBrief, "volume" | "chapter" | "title">): string {
  const parts: string[] = [];
  if (r.volume) parts.push(`Vol. ${r.volume}`);
  parts.push(r.chapter ? `Ch. ${r.chapter}` : "Oneshot");
  return parts.join(" ");
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  pending: "Pending",
  queued: "Queued",
  downloading: "Downloading",
  paused: "Paused",
  retry_scheduled: "Retry scheduled",
  completed: "Completed",
  failed: "Failed",
  needs_attention: "Needs attention",
  cancelled: "Cancelled",
};

export function jobTone(s: JobStatus): string {
  switch (s) {
    case "completed":
      return "ok";
    case "downloading":
    case "queued":
      return "info";
    case "failed":
    case "needs_attention":
      return "bad";
    case "paused":
    case "retry_scheduled":
      return "warn";
    default:
      return "";
  }
}

export const PAUSE_REASON: Record<string, string> = {
  user: "paused by you",
  queue: "queue paused",
  storage: "library unavailable",
  blocked: "MangaDex access restricted",
};

export const LOCAL_STATUS: Record<string, { label: string; tone: string }> = {
  missing: { label: "Missing", tone: "" },
  downloaded: { label: "Downloaded", tone: "ok" },
  imported: { label: "Imported", tone: "ok" },
  modified: { label: "Edited locally", tone: "info" },
  missing_on_disk: { label: "Folder deleted", tone: "warn" },
  skipped: { label: "Skipped", tone: "" },
  external: { label: "Already owned", tone: "ok" },
  replaced: { label: "Replaced by re-upload", tone: "" },
  in_replaced: { label: "Revision in replaced folder", tone: "info" },
};

/** MangaDex descriptions are Markdown: turn them into readable plain text (tables, links, emphasis, rules). */
export function plainDescription(md: string | null | undefined): string {
  if (!md) return "";
  const out: string[] = [];
  for (const raw of md.replace(/\r\n?/g, "\n").split("\n")) {
    let line = raw.trim();
    if (/^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/.test(line)) continue; // table separator
    if (/^([-*_]\s*){3,}$/.test(line)) continue; // horizontal rule
    if (line.startsWith("|") || / \| /.test(line)) {
      line = line.split("|").map((c) => c.trim()).filter(Boolean).join(" · ");
    }
    line = line
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/^#{1,6}\s+/, "")
      .replace(/^>\s?/, "")
      .replace(/(\*\*|__|~~)(.+?)\1/g, "$2")
      .replace(/(^|\W)[*_](\S.*?\S|\S)[*_](?=\W|$)/g, "$1$2")
      .replace(/`([^`]*)`/g, "$1")
      .trim();
    if (line) out.push(line);
  }
  return out.join("\n");
}
