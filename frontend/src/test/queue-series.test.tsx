import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";
import { parseTypes, SeriesList, type SeriesRow, typeQuery, TypeFilter } from "../pages/Queue";

afterEach(cleanup);

const row = (id: string, title: string, queued: number, downloaded: number, extra: Partial<SeriesRow> = {}): SeriesRow => ({
  manga: { id, title, cover_url: null, update_tier: 1 },
  in_group: queued, queued, downloading: 0, paused: 0, failed: 0, downloaded, last_finished_at: null, ...extra,
});

it("shows one card per series with queued and downloaded counts and opens its chapters", async () => {
  const onOpen = vi.fn();
  const data = {
    items: [row("a", "Manga A", 30, 0, { downloading: 1 }), row("b", "Manga B", 20, 2), row("c", "Manga C", 10, 9, { failed: 1 })],
    counts: { active: 1, pending: 59, paused: 0, failed: 1, completed: 11, cancelled: 0 },
    queue_paused: false,
  };
  render(<MemoryRouter><SeriesList group="pending" query={{ data, error: null, isLoading: false }} onOpen={onOpen} /></MemoryRouter>);
  expect(screen.getByText("3 series · 60 pending chapters")).toBeTruthy();
  expect(screen.getAllByRole("progressbar")).toHaveLength(3);
  expect(screen.getByText("Downloading")).toBeTruthy();
  expect(screen.getByText("1 failed")).toBeTruthy();
  await userEvent.click(screen.getAllByRole("button", { name: "Show chapters" })[1]);
  expect(onOpen).toHaveBeenCalledWith("b");
});

it("shows an empty state when no series has chapters in the tab", () => {
  render(<MemoryRouter><SeriesList group="failed" query={{ data: { items: [], counts: {} as never, queue_paused: false }, error: null, isLoading: false }} onOpen={() => {}} /></MemoryRouter>);
  expect(screen.getByText("Nothing here")).toBeTruthy();
});


it("parses and serialises the type filter", () => {
  expect(parseTypes("manhwa,manga,bogus")).toEqual(["manga", "manhwa"]);
  expect(parseTypes(null)).toEqual([]);
  expect(typeQuery(["manga", "manhua"])).toBe("&type=manga&type=manhua");
});

it("toggles types and clears them with All", async () => {
  const onChange = vi.fn();
  render(<TypeFilter value={["manga"]} onChange={onChange} />);
  await userEvent.click(screen.getByRole("button", { name: "Manhwa" }));
  expect(onChange).toHaveBeenLastCalledWith(["manga", "manhwa"]);
  await userEvent.click(screen.getByRole("button", { name: "Manga" }));
  expect(onChange).toHaveBeenLastCalledWith([]);
  await userEvent.click(screen.getByRole("button", { name: "All" }));
  expect(onChange).toHaveBeenLastCalledWith([]);
  expect(screen.getByRole("button", { name: "Manga" }).getAttribute("aria-pressed")).toBe("true");
});
