import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";
import { makeQueryClient } from "../App";
import { ToastProvider } from "../components";
import ReviewPage from "../pages/Review";

afterEach(() => vi.unstubAllGlobals());

const revision = {
  id: 7,
  kind: "revision_after_move",
  path: "Replaced/Some Manga/Volume 02 Chapter 11 20261114-105710",
  status: "open",
  candidates: [],
  resolution: null,
  created_at: new Date().toISOString(),
  manga: { id: "m1", title: "Some Manga" },
  details: {
    revision: "reupload",
    volume: "2",
    chapter: "11",
    title: "Fireworks",
    language: "en",
    groups: ["Team X"],
    pages: 24,
    library_path: "Some Manga/Volume 02 Chapter 11",
    first_downloaded_at: new Date(Date.now() - 3 * 86400_000).toISOString(),
    mangadex_url: "https://mangadex.org/chapter/abc",
  },
};

it("lists a revision of a chapter moved out and closes it with Seen", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? "GET"} ${url}`);
    const body = url === "/api/review" ? [revision] : { ok: true };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  render(
    <QueryClientProvider client={makeQueryClient()}><ToastProvider>
      <MemoryRouter><ReviewPage /></MemoryRouter>
    </ToastProvider></QueryClientProvider>,
  );
  expect(await screen.findByText("Newer revision of a chapter you moved out")).toBeInTheDocument();
  expect(screen.getByText(revision.path)).toBeInTheDocument();
  expect(screen.getByText(/re-uploaded by the same group/)).toBeInTheDocument();
  expect(screen.getByText("Some Manga/Volume 02 Chapter 11")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open on MangaDex" })).toHaveAttribute("href", "https://mangadex.org/chapter/abc");
  expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Seen" }));
  expect(calls).toContain("POST /api/review/7/dismiss");
});
