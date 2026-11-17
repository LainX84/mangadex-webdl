import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { GroupStat, Language, TitleChoice } from "./types";

export function useLanguages() {
  return useQuery({ queryKey: ["languages"], queryFn: () => api<Language[]>("/api/languages"), staleTime: 60_000 });
}

/** Multi-select language chips. Languages available for the manga are listed first. */
export function LanguagePicker({ value, onChange, available }: { value: string[]; onChange: (v: string[]) => void; available?: string[] }) {
  const { data: langs } = useLanguages();
  const [showAll, setShowAll] = useState(false);
  const byCode = useMemo(() => new Map((langs ?? []).map((l) => [l.code, l])), [langs]);
  const shown = useMemo(() => {
    if (!langs) return [];
    if (showAll || !available || available.length === 0) return langs;
    const set = new Set([...available, ...value]);
    return langs.filter((l) => set.has(l.code));
  }, [langs, available, showAll, value]);
  const toggle = (code: string) => onChange(value.includes(code) ? value.filter((c) => c !== code) : [...value, code]);

  return (
    <div>
      <div className="chips" role="group" aria-label="Languages">
        {shown.map((l) => (
          <button type="button" key={l.code} className="chip" aria-pressed={value.includes(l.code)} onClick={() => toggle(l.code)}>
            {l.name} <span className="count">{l.folder_code}</span>
          </button>
        ))}
      </div>
      {available && available.length > 0 && (
        <button type="button" className="btn ghost sm" style={{ marginTop: 6 }} onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show only languages available for this manga" : "Show all languages"}
        </button>
      )}
      {value.length > 0 && (
        <div className="small muted" style={{ marginTop: 4 }}>
          Selected: {value.map((c) => byCode.get(c)?.name ?? c).join(", ")}
        </div>
      )}
    </div>
  );
}

export interface GroupChoice {
  mode: "all" | "selected";
  ids: string[];
  includeUnknown: boolean;
}

export function GroupPicker({ groups, unknown, value, onChange, loading }: {
  groups: GroupStat[];
  unknown?: Record<string, number>;
  value: GroupChoice;
  onChange: (v: GroupChoice) => void;
  loading?: boolean;
}) {
  const toggle = (id: string) =>
    onChange({ ...value, ids: value.ids.includes(id) ? value.ids.filter((x) => x !== id) : [...value.ids, id] });
  const unknownCount = Object.values(unknown ?? {}).reduce((a, b) => a + b, 0);
  return (
    <div>
      <div className="chips" role="radiogroup" aria-label="Scanlation group mode">
        <button type="button" className="chip" role="radio" aria-checked={value.mode === "all"} aria-pressed={value.mode === "all"} onClick={() => onChange({ ...value, mode: "all" })}>
          All groups
        </button>
        <button type="button" className="chip" role="radio" aria-checked={value.mode === "selected"} aria-pressed={value.mode === "selected"} onClick={() => onChange({ ...value, mode: "selected" })}>
          Only selected groups
        </button>
      </div>
      {value.mode === "selected" && (
        <div style={{ marginTop: 12 }}>
          {loading && <div className="small muted">Loading groups from MangaDex…</div>}
          {!loading && groups.length === 0 && <div className="small muted">No groups found for the selected languages yet.</div>}
          <div className="chips" role="group" aria-label="Scanlation groups">
            {groups.map((g) => {
              const n = Object.values(g.languages).reduce((a, b) => a + b, 0);
              return (
                <button type="button" key={g.id} className="chip" aria-pressed={value.ids.includes(g.id)} onClick={() => toggle(g.id)}>
                  {g.name} <span className="count">{n} ch · {Object.keys(g.languages).join("/").toUpperCase()}</span>
                </button>
              );
            })}
          </div>
          <label className="switch" style={{ marginTop: 8 }}>
            <span className="small">Also include releases without a group (“Unknown Group”{unknownCount ? `, ${unknownCount}` : ""})</span>
            <input type="checkbox" role="switch" checked={value.includeUnknown} onChange={(e) => onChange({ ...value, includeUnknown: e.target.checked })} />
          </label>
        </div>
      )}
    </div>
  );
}

/** Marker value for "type your own title" in TitlePicker. */
export const CUSTOM_TITLE = "\u0000custom";

function titleRank(t: TitleChoice, langs: string[]): [number, number] {
  const l = t.language.toLowerCase();
  const i = langs.indexOf(l);
  if (i >= 0) return [0, i];
  const j = langs.indexOf(l.split("-")[0]);
  if (j >= 0) return [0, j + 0.5];
  if (t.source === "main") return [1, 0];
  return [2, 0];
}

/** Choose one of the MangaDex titles (titles in `languages` first) or type a custom one. */
export function TitlePicker({ titles, languages, value, custom, onChange, current, disabled }: {
  titles: TitleChoice[];
  languages: string[];
  value: string;
  custom: string;
  onChange: (value: string, custom: string) => void;
  current?: string;
  disabled?: boolean;
}) {
  const [showAll, setShowAll] = useState(false);
  const langs = languages.map((l) => l.toLowerCase());
  const ranked = titles
    .map((t, idx) => ({ t, idx, r: titleRank(t, langs) }))
    .sort((a, b) => a.r[0] - b.r[0] || a.r[1] - b.r[1] || a.idx - b.idx);
  const visible = ranked.filter(({ t, r }) => showAll || r[0] < 2 || t.title === value || t.title === current);
  const hidden = ranked.length - visible.length;
  const extra = current && !titles.some((t) => t.title === current) ? current : null;
  return (
    <div className="title-options" role="radiogroup" aria-label="Title">
      {extra && (
        <label className="title-option">
          <input type="radio" name="title" disabled={disabled} checked={value === extra} onChange={() => onChange(extra, custom)} />
          <span className="t">{extra}</span>
          <span className="lang">current</span>
        </label>
      )}
      {visible.map(({ t }) => (
        <label className="title-option" key={t.title}>
          <input type="radio" name="title" disabled={disabled} checked={value === t.title} onChange={() => onChange(t.title, custom)} />
          <span className="t">{t.title}{t.source === "main" && <span className="small muted"> · MangaDex main title</span>}</span>
          <span className="lang">{t.language}</span>
        </label>
      ))}
      {hidden > 0 && (
        <button type="button" className="btn sm" onClick={() => setShowAll(true)} disabled={disabled}>
          Show {hidden} title(s) in other languages
        </button>
      )}
      <label className="title-option">
        <input type="radio" name="title" disabled={disabled} checked={value === CUSTOM_TITLE} onChange={() => onChange(CUSTOM_TITLE, custom)} />
        <input
          className="input"
          placeholder="Type your own title"
          aria-label="Custom title"
          maxLength={300}
          disabled={disabled}
          value={custom}
          onFocus={() => onChange(CUSTOM_TITLE, custom)}
          onChange={(e) => onChange(CUSTOM_TITLE, e.target.value)}
        />
      </label>
    </div>
  );
}
