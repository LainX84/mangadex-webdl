import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ArchiveHint } from "../archive";
import type { ArchiveSuggestion } from "../types";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function mount(data: ArchiveSuggestion, props: Partial<Parameters<typeof ArchiveHint>[0]> = {}) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } })));
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onUse = vi.fn();
  const onUpToDate = vi.fn();
  render(<QueryClientProvider client={qc}><ArchiveHint mangaId="m1" languages={["en"]} onUse={onUse} onUpToDate={onUpToDate} {...props} /></QueryClientProvider>);
  return { onUse, onUpToDate };
}

const base = { folder: "Manhua/Inglese/Non ufficiale/In Progress/Blue Wings", title: "Blue Wings", language: "en",
  language_match: true, custom_only: false, archives: 442, errors: [] };

it("offers to continue after the newest archived chapter", async () => {
  const best = { ...base, last_chapter: 442, source: "file" as const, evidence: "Blue Wings - Chapter 442.cbz",
    next_chapter: 443, after_count: 10, after_last: 452, up_to_date: false };
  const { onUse } = mount({ configured: true, candidates: [best], best });
  const btn = await screen.findByRole("button", { name: "Continue from Ch. 443" });
  expect(screen.getByText(/10 chapters on MangaDex after it \(Ch\. 443–452\)/)).toBeTruthy();
  await userEvent.setup().click(btn);
  expect(onUse).toHaveBeenCalledWith("443");
});

it("suggests 'new chapters only' when the archive is up to date", async () => {
  const best = { ...base, last_chapter: 452, source: "inside" as const, evidence: "Blue Wings v40 - Custom.cbz",
    next_chapter: null, after_count: 0, after_last: null, up_to_date: true, custom_only: true };
  const { onUpToDate } = mount({ configured: true, candidates: [best], best });
  await userEvent.setup().click(await screen.findByRole("button", { name: "New chapters only" }));
  expect(onUpToDate).toHaveBeenCalled();
  expect(screen.getByText("Custom volumes")).toBeTruthy();
  expect(screen.getByText(/read inside Blue Wings v40 - Custom\.cbz/)).toBeTruthy();
});

it("renders nothing when no archive is mounted", async () => {
  const { container } = (() => { mount({ configured: false, candidates: [], best: null }); return { container: document.body }; })();
  await new Promise((r) => setTimeout(r, 20));
  expect(container.textContent).not.toMatch(/Your archive/);
});

it("re-reads one archive folder on request and refreshes the suggestion", async () => {
  const best = { ...base, last_chapter: 41, source: "inside" as const, evidence: "Blue Wings v11 - Custom.cbz",
    next_chapter: 42, after_count: 11, after_last: 52.1, up_to_date: false };
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    const body = init?.method === "POST"
      ? { status: "success", changed: true, chapter: 52.1 }
      : { configured: true, candidates: [best], best };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={qc}><ArchiveHint mangaId="m1" languages={["en"]} onUse={vi.fn()} /></QueryClientProvider>);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Rescan folder" }));
  await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([u]) => String(u).startsWith("/api/archive/suggest")).length).toBe(2));
  const post = fetchMock.mock.calls.find(([, i]) => i?.method === "POST")!;
  expect(post[0]).toBe("/api/archive/folder/rescan");
  expect(JSON.parse(String(post[1]!.body))).toEqual({ folder: base.folder });
});
