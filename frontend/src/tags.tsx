import { useMemo, type CSSProperties } from "react";
import { Link, useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { AppSettings } from "./types";

/** A MangaDex tag the user wants to stand out, with its colour (#rrggbb). */
export interface HighlightedTag {
  name: string;
  color: string;
}

export type TagMatch = "any" | "all";

/** Colours offered to new highlighted tags, in order. */
export const TAG_PALETTE = ["#d6336c", "#7048e8", "#1c7ed6", "#0ca678", "#f08c00", "#e8590c", "#ae3ec9", "#2f9e44", "#c92a2a", "#1098ad"];

export const tagKey = (name: string) => name.trim().toLowerCase();

export function nextTagColor(used: HighlightedTag[]): string {
  const taken = new Set(used.map((t) => t.color.toLowerCase()));
  return TAG_PALETTE.find((c) => !taken.has(c)) ?? TAG_PALETTE[used.length % TAG_PALETTE.length];
}

/** Black or white text, whichever reads better on the given background. */
export function inkFor(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return "#ffffff";
  const n = parseInt(m[1], 16);
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const l = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
  // Contrast with white vs. with black
  return (1.05 / (l + 0.05)) >= ((l + 0.05) / 0.05) ? "#ffffff" : "#111111";
}

export function tagStyle(color: string): CSSProperties {
  return { background: color, borderColor: color, color: inkFor(color) };
}

/** True when the title's tags satisfy the filter (case-insensitive). An empty filter matches everything. */
export function matchesTags(tags: string[] | undefined, wanted: string[], match: TagMatch): boolean {
  if (wanted.length === 0) return true;
  const have = new Set((tags ?? []).map(tagKey));
  const keys = wanted.map(tagKey);
  return match === "all" ? keys.every((k) => have.has(k)) : keys.some((k) => have.has(k));
}

/** Highlighted tags of a title first (in the order chosen in Settings), then the others alphabetically. */
export function sortTags(tags: string[], highlights: Map<string, HighlightedTag>, order: HighlightedTag[]): { name: string; hl?: HighlightedTag }[] {
  const pos = new Map(order.map((t, i) => [tagKey(t.name), i]));
  return [...tags]
    .map((name) => ({ name, hl: highlights.get(tagKey(name)) }))
    .sort((a, b) => {
      if (a.hl && b.hl) return (pos.get(tagKey(a.name)) ?? 0) - (pos.get(tagKey(b.name)) ?? 0);
      if (a.hl) return -1;
      if (b.hl) return 1;
      return a.name.localeCompare(b.name);
    });
}

export function useHighlightedTags(): { list: HighlightedTag[]; map: Map<string, HighlightedTag> } {
  const { data } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<{ settings: AppSettings; defaults: AppSettings }>("/api/settings"),
    staleTime: 60_000,
  });
  return useMemo(() => {
    const list = ((data?.settings.highlighted_tags as HighlightedTag[] | undefined) ?? []).filter((t) => t && t.name);
    return { list, map: new Map(list.map((t) => [tagKey(t.name), t])) };
  }, [data]);
}

export const libraryTagLink = (name: string) => `/library?tag=${encodeURIComponent(name)}`;

export function TagChip({ name, color, to, small }: { name: string; color?: string; to?: string; small?: boolean }) {
  const cls = `tag${color ? " hl" : ""}${small ? " sm" : ""}`;
  const style = color ? tagStyle(color) : undefined;
  const content = (
    <>
      {color && <span aria-hidden="true">★</span>}
      {name}
    </>
  );
  return to ? (
    <Link className={cls} style={style} to={to} title={`Library titles tagged “${name}”`}>{content}</Link>
  ) : (
    <span className={cls} style={style}>{content}</span>
  );
}

/**
 * Tags of a title. "all": every tag, highlighted ones first and coloured, each linking to the
 * library filtered by it. "highlighted": only the highlighted ones, as small coloured badges
 * (safe inside a card that is itself a link).
 */
export function TagList({ tags, mode = "all", linked = mode === "all", also = [] }: {
  tags: string[] | undefined;
  mode?: "all" | "highlighted";
  linked?: boolean;
  /** In "highlighted" mode, tags shown even when not highlighted (e.g. the ones being filtered by). */
  also?: string[];
}) {
  const { list, map } = useHighlightedTags();
  const extra = new Set(also.map(tagKey));
  const items = sortTags(tags ?? [], map, list).filter((t) => mode === "all" || t.hl || extra.has(tagKey(t.name)));
  if (items.length === 0) return null;
  return (
    <div className={`tags${mode === "highlighted" ? " compact" : ""}`} aria-label="Tags">
      {items.map((t) => (
        <TagChip key={t.name} name={t.name} color={t.hl?.color} small={mode === "highlighted"} to={linked ? libraryTagLink(t.name) : undefined} />
      ))}
    </div>
  );
}

/** How many titles carry each tag (keyed by tagKey, keeping the first spelling seen). */
export function countTags(titles: { tags?: string[] }[]): Map<string, { name: string; count: number }> {
  const counts = new Map<string, { name: string; count: number }>();
  for (const m of titles) {
    const seen = new Set<string>();
    for (const t of m.tags ?? []) {
      if (seen.has(tagKey(t))) continue;
      seen.add(tagKey(t));
      const e = counts.get(tagKey(t));
      if (e) e.count += 1;
      else counts.set(tagKey(t), { name: t, count: 1 });
    }
  }
  return counts;
}

export interface TagFilterState {
  tags: string[];
  match: TagMatch;
  isSelected: (name: string) => boolean;
  toggle: (name: string) => void;
  setMatch: (m: TagMatch) => void;
  clear: () => void;
}

/**
 * Tag filter kept in the URL (?tag=A&tag=B&match=all), so a filtered view can be bookmarked or
 * shared. The last any/all choice is remembered per page in localStorage.
 */
export function useTagFilter(storageKey: string): TagFilterState {
  const [params, setParams] = useSearchParams();
  const tags = params.getAll("tag");
  const stored = (): TagMatch => {
    try {
      return localStorage.getItem(storageKey) === "all" ? "all" : "any";
    } catch {
      return "any";
    }
  };
  const match: TagMatch = params.get("match") === "all" ? "all" : params.get("match") === "any" ? "any" : stored();
  const isSelected = (name: string) => tags.some((t) => tagKey(t) === tagKey(name));
  const set = (next: string[], m: TagMatch = match) => {
    const p = new URLSearchParams(params);
    p.delete("tag");
    next.forEach((t) => p.append("tag", t));
    p.set("match", m);
    if (next.length === 0) p.delete("match");
    try {
      localStorage.setItem(storageKey, m);
    } catch {
      /* not remembered */
    }
    setParams(p, { replace: true });
  };
  return {
    tags,
    match,
    isSelected,
    toggle: (name) => set(isSelected(name) ? tags.filter((t) => tagKey(t) !== tagKey(name)) : [...tags, name]),
    setMatch: (m) => set(tags, m),
    clear: () => set([]),
  };
}

/**
 * Tags picked from a menu (with how many titles carry each), shown as removable chips — coloured
 * when highlighted in Settings — plus any/all when two or more are selected. `counts` are the titles
 * carrying each tag among those the other filters leave.
 */
export function TagFilter({ filter, counts }: { filter: TagFilterState; counts: Map<string, { name: string; count: number }> }) {
  const { map: hlMap } = useHighlightedTags();
  const options = [...counts.values()].filter((t) => !filter.isSelected(t.name)).sort((a, b) => a.name.localeCompare(b.name));
  if (options.length === 0 && filter.tags.length === 0) return null;
  return (
    <div className="tag-filter">
      {filter.tags.length > 0 && (
        <div className="chips" role="group" aria-label="Tags in the filter">
          {filter.tags.map((t) => {
            const hl = hlMap.get(tagKey(t));
            return (
              <button type="button" key={t} className="chip" aria-pressed={true} style={hl ? tagStyle(hl.color) : undefined}
                onClick={() => filter.toggle(t)} title="Remove this tag from the filter">
                {t}<span className="count">{counts.get(tagKey(t))?.count ?? 0}</span><span aria-hidden="true">✕</span>
              </button>
            );
          })}
        </div>
      )}
      <div className="row">
        {options.length > 0 && (
          <select className="input" value="" aria-label="Filter by tag" onChange={(e) => e.target.value && filter.toggle(e.target.value)}>
            <option value="">+ Filter by tag…</option>
            {options.map((t) => (
              <option key={t.name} value={t.name}>{t.name} ({t.count})</option>
            ))}
          </select>
        )}
        {filter.tags.length >= 2 && (
          <div className="seg" role="group" aria-label="How to combine the tags">
            <button type="button" aria-pressed={filter.match === "any"} onClick={() => filter.setMatch("any")}>Any of these</button>
            <button type="button" aria-pressed={filter.match === "all"} onClick={() => filter.setMatch("all")}>All of these</button>
          </div>
        )}
        {filter.tags.length > 0 && (
          <button type="button" className="btn sm ghost" onClick={filter.clear}>Clear tags</button>
        )}
      </div>
    </div>
  );
}
