import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { makeQueryClient } from "../App";
import { ToastProvider } from "../components";
import { TitleEditor } from "../pages/Manga";
import { CUSTOM_TITLE, TitlePicker } from "../pickers";
import type { MangaDetail } from "../types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const manga = {
  id: "m1",
  title: "Ore no Imouto",
  canonical_title: "Ore no Imouto",
  title_override: null,
  folder_name: "Ore no Imouto",
} as unknown as MangaDetail;

it("lets the user pick another MangaDex title and confirms the folder move", async () => {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, body });
    let out: unknown;
    if (url.endsWith("/titles")) {
      out = {
        title: "Ore no Imouto", canonical_title: "Ore no Imouto", title_override: null, folder_name: "Ore no Imouto",
        preferred_languages: ["en", "it"], live: true,
        titles: [
          { title: "My Little Sister", language: "en", source: "alt", preferred: true },
          { title: "My Little Sister Can't Be This Cute", language: "en", source: "alt", preferred: true },
          { title: "La mia sorellina", language: "it", source: "alt", preferred: true },
          { title: "Ore no Imouto", language: "ja-ro", source: "main", preferred: false },
          { title: "俺の妹", language: "ja", source: "alt", preferred: false },
        ],
      };
    } else {
      const report = { title: body.title, title_override: body.title, folder_from: "Ore no Imouto", folder_to: body.title,
        folder_changes: true, chapters_on_disk: 12, busy_downloads: 0, moved: !body.dry_run, dry_run: !!body.dry_run };
      out = body.dry_run ? { report } : { report, manga: { ...manga, title: body.title, folder_name: body.title } };
    }
    return new Response(JSON.stringify(out), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const user = userEvent.setup();
  render(<QueryClientProvider client={makeQueryClient()}><ToastProvider><TitleEditor manga={manga} onDone={() => {}} /></ToastProvider></QueryClientProvider>);

  await screen.findByText("La mia sorellina");
  expect(screen.queryByText("俺の妹")).toBeNull(); // other languages hidden at first
  await user.click(screen.getByRole("button", { name: /show 1 title/i }));
  expect(screen.getByText("俺の妹")).toBeTruthy();

  await user.click(screen.getByLabelText(/My Little Sister Can't Be This Cute/));
  await user.click(screen.getByRole("button", { name: /apply title/i }));
  await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/rename"))).toHaveLength(2));
  const renames = calls.filter((c) => c.url.endsWith("/rename")).map((c) => c.body);
  expect(renames).toEqual([
    { title: "My Little Sister Can't Be This Cute", dry_run: true },
    { title: "My Little Sister Can't Be This Cute" },
  ]);
  expect(confirm.mock.calls[0][0]).toContain("12 downloaded chapter(s)");
  expect(confirm.mock.calls[0][0]).toContain("“Ore no Imouto” → “My Little Sister Can't Be This Cute”");
});

it("accepts a custom title", async () => {
  const calls: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    if (body) calls.push(body);
    const out = url.endsWith("/titles")
      ? { title: "X", canonical_title: "X", title_override: null, folder_name: "X", preferred_languages: ["en"], live: true, titles: [] }
      : { report: { title: body.title, folder_from: "X", folder_to: body.title, folder_changes: true, chapters_on_disk: 0, busy_downloads: 0, moved: false, dry_run: !!body.dry_run }, manga };
    return new Response(JSON.stringify(out), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const user = userEvent.setup();
  render(<QueryClientProvider client={makeQueryClient()}><ToastProvider><TitleEditor manga={{ ...manga, title: "X" } as MangaDetail} onDone={() => {}} /></ToastProvider></QueryClientProvider>);
  await user.type(await screen.findByLabelText("Custom title"), "Il mio titolo");
  await user.click(screen.getByRole("button", { name: /apply title/i }));
  await waitFor(() => expect(calls).toEqual([{ title: "Il mio titolo", dry_run: true }, { title: "Il mio titolo" }]));
});

it("lists titles in the chosen languages first and hides the others", async () => {
  const onChange = vi.fn();
  const user = userEvent.setup();
  const titles = [
    { title: "Kara no Kyoukai", language: "ja-ro", source: "main" as const, preferred: false },
    { title: "空の境界", language: "ja", source: "alt" as const, preferred: false },
    { title: "Il giardino dei peccatori", language: "it", source: "alt" as const, preferred: false },
    { title: "The Garden of Sinners", language: "en", source: "alt" as const, preferred: false },
  ];
  render(<TitlePicker titles={titles} languages={["en", "it"]} value="Kara no Kyoukai" custom="" current="Kara no Kyoukai" onChange={onChange} />);
  const labels = screen.getAllByRole("radio").map((r) => r.closest("label")?.textContent ?? "");
  expect(labels.slice(0, 3)).toEqual([
    expect.stringContaining("The Garden of Sinners"),
    expect.stringContaining("Il giardino dei peccatori"),
    expect.stringContaining("Kara no Kyoukai"),
  ]);
  expect(screen.queryByText("空の境界")).toBeNull();
  await user.click(screen.getByLabelText(/The Garden of Sinners/));
  expect(onChange).toHaveBeenLastCalledWith("The Garden of Sinners", "");
  await user.type(screen.getByLabelText("Custom title"), "Z");
  expect(onChange).toHaveBeenLastCalledWith(CUSTOM_TITLE, "Z");
});
