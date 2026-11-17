import { expect, it } from "vitest";
import { groupByVolume } from "../pages/Manga";
import type { Release } from "../types";

const rel = (chapter: string | null, volume: string | null, id = `${volume}-${chapter}`): Release => ({
  id, chapter, volume, title: null, language: "en", groups: ["G"], group_ids: [], pages: 10, publish_at: null,
  external: false, unavailable: false, local_status: "missing", folder: null, local_page_count: null,
  downloaded_at: null, wanted: true, sort: chapter && !Number.isNaN(Number(chapter)) ? Number(chapter) : null,
  job: null, mangadex_url: "",
});

const items = [rel("21", null), rel("17", "4"), rel("1", "1"), rel("2.1", "1"), rel("20", null), rel("18", "4"), rel(null, null, "oneshot")];

it("groups by volume in ascending order", () => {
  const v = groupByVolume(items, "asc");
  expect(v.map((x) => [x.label, x.range, x.items.map((r) => r.chapter)])).toEqual([
    ["Volume 1", "Ch. 1 – 2.1", ["1", "2.1"]],
    ["Volume 4", "Ch. 17 – 18", ["17", "18"]],
    ["No volume", "Ch. 20 – 21", ["20", "21", null]],
  ]);
});

it("puts the newest (not yet in a volume) first when descending", () => {
  const v = groupByVolume(items, "desc");
  expect(v.map((x) => [x.label, x.range, x.items.map((r) => r.chapter)])).toEqual([
    ["No volume", "Ch. 21 – 20", [null, "21", "20"]],
    ["Volume 4", "Ch. 18 – 17", ["18", "17"]],
    ["Volume 1", "Ch. 2.1 – 1", ["2.1", "1"]],
  ]);
});
