import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import type { Job } from "./types";
import { JOB_STATUS_LABEL, PAUSE_REASON, jobTone, relTime } from "./format";

/* ------------------------------------------------------------------ icons */

const paths: Record<string, string> = {
  home: "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z",
  library: "M4 4h4v16H4zM10 4h4v16h-4zM16.5 4.5l3.8-1 3.2 15.5-3.8 1z",
  plus: "M12 5v14M5 12h14",
  queue: "M12 3v12m0 0-4.5-4.5M12 15l4.5-4.5M4 17v3h16v-3",
  settings:
    "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zm7.4-2.5.1-1-.1-1 2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-1.7-1L15 3h-4l-.3 2.5a7.5 7.5 0 0 0-1.7 1l-2.4-1-2 3.4 2 1.6-.1 1 .1 1-2 1.6 2 3.4 2.4-1a7.5 7.5 0 0 0 1.7 1L11 21h4l.3-2.5a7.5 7.5 0 0 0 1.7-1l2.4 1 2-3.4z",
  review: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  pause: "M8 5v14M16 5v14",
  play: "M7 4l13 8-13 8z",
  x: "M6 6l12 12M18 6 6 18",
  external: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  logout: "M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 16l4-4-4-4M14 12H3",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v6M12 7.5v.01",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM21 21l-4.3-4.3",
  inbox: "M4 13h4l2 3h4l2-3h4M4 13l2.5-8h11L20 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20H2",
};

export function Icon({ name, title }: { name: keyof typeof paths | string; title?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      <path d={paths[name] ?? ""} />
    </svg>
  );
}

/* ------------------------------------------------------------------ toasts */

interface ToastState {
  message: string;
  tone?: "bad";
}
const ToastCtx = createContext<(m: string, tone?: "bad") => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<ToastState | null>(null);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), toast.tone === "bad" ? 6000 : 3000);
    return () => clearTimeout(t);
  }, [toast]);
  const show = useCallback((message: string, tone?: "bad") => setToast({ message, tone }), []);
  return (
    <ToastCtx.Provider value={show}>
      {children}
      {toast && (
        <div className={`toast ${toast.tone ?? ""}`} role="status" aria-live="polite" onClick={() => setToast(null)}>
          {toast.message}
        </div>
      )}
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

/* ------------------------------------------------------------------ bits */

export function Badge({ tone = "", children }: { tone?: string; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

const TIER_HINT: Record<number, string> = {
  1: "Level 1 – active: new chapters within the last ~3 months; checked at the normal interval",
  2: "Level 2 – slow: no new chapters for 3–12 months; checked less often",
  3: "Level 3 – dormant or complete: no new chapters for over a year; checked rarely",
};

/** Small coloured square with the adaptive monitoring level (1–3). */
export function TierBadge({ tier }: { tier: number | null | undefined }) {
  if (!tier) return null;
  return (
    <span className={`tier tier-${tier}`} title={TIER_HINT[tier]} aria-label={TIER_HINT[tier]}>
      {tier}
    </span>
  );
}

/** Manga title that truncates (ellipsis / line clamp) while the level square stays visible. */
export function MangaTitle({ title, tier, lines = 1, className = "" }: { title: string | undefined; tier?: number | null; lines?: 1 | 2; className?: string }) {
  return (
    <div className={`title-row ${className}`} title={title}>
      <span className={lines === 2 ? "title-text clamp2" : "title-text"}>{title}</span>
      <TierBadge tier={tier} />
    </div>
  );
}

/** Level tab hanging from the top edge of a card (top right corner). */
export function TierTab({ tier }: { tier: number | null | undefined }) {
  if (!tier) return null;
  return (
    <span className={`tier-tab tier-${tier}`} title={TIER_HINT[tier]} aria-label={TIER_HINT[tier]}>
      <span className="lv">Lv</span>
      {tier}
    </span>
  );
}

export function CompletedBadge({ show }: { show: boolean }) {
  if (!show) return null;
  return <span className="badge done" title="Every chapter up to the final one listed on MangaDex has been uploaded (removed chapters included)">Completed</span>;
}

export function Cover({ src, alt, size }: { src: string | null; alt: string; size?: "lg" }) {
  if (!src) return <div className={`cover ${size ?? ""}`} aria-hidden="true" />;
  return <img className={`cover ${size ?? ""}`} src={src} alt={alt} loading="lazy" />;
}

export function Switch({ label, checked, onChange, hint, disabled }: { label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string; disabled?: boolean }) {
  return (
    <label className="switch">
      <span>
        <span className="label">{label}</span>
        {hint && <span className="hint small muted" style={{ display: "block" }}>{hint}</span>}
      </span>
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
    </label>
  );
}

/** Progress is only shown as a percentage when the page count is actually known. */
export function JobProgress({ job }: { job: Job }) {
  if (job.status !== "downloading") return null;
  if (!job.pages_total) {
    return (
      <div className="progress indeterminate" role="progressbar" aria-label="Downloading">
        <span />
      </div>
    );
  }
  const done = job.pages_done ?? 0;
  const pct = Math.min(100, Math.round((done / job.pages_total) * 100));
  return (
    <div>
      <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={job.pages_total} aria-valuenow={done} aria-label="Pages downloaded">
        <span style={{ width: `${pct}%` }} />
      </div>
      <div className="small muted" style={{ marginTop: 4 }}>
        {done} / {job.pages_total} pages
      </div>
    </div>
  );
}

export function JobStatusBadge({ job }: { job: Job }) {
  let extra = "";
  if (job.status === "paused" && job.pause_reason) extra = ` · ${PAUSE_REASON[job.pause_reason] ?? job.pause_reason}`;
  if (job.status === "retry_scheduled" && job.next_attempt_at) extra = ` · ${relTime(job.next_attempt_at)}`;
  return <Badge tone={jobTone(job.status)}>{JOB_STATUS_LABEL[job.status] + extra}</Badge>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="big">{title}</div>
      {children}
    </div>
  );
}

export function Loading() {
  return <div className="empty" aria-busy="true">Loading…</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="alert bad" role="alert">
      <div className="body">{msg}</div>
    </div>
  );
}

export function Tabs<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: { value: T; label: string; count?: number }[] }) {
  return (
    <div className="tabs" role="tablist">
      {items.map((it) => (
        <button key={it.value} role="tab" className="tab" aria-selected={value === it.value} onClick={() => onChange(it.value)}>
          {it.label}
          {it.count !== undefined && <span className="count">{it.count}</span>}
        </button>
      ))}
    </div>
  );
}
