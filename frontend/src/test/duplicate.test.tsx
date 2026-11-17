import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, expect, it, vi } from "vitest";
import { makeQueryClient } from "../App";
import { ToastProvider } from "../components";
import AddPage from "../pages/Add";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const ID = "6d4e768d-d68a-449f-8983-f7acbc160d9c";
const result = (followed: boolean) => ({
  total: 1,
  items: [{ id: ID, title: "Kara no Kyoukai", alt_titles: [], year: 2008, status: "completed", content_rating: "safe",
    description: "", available_languages: ["en"], cover_url: null, followed, titles: [] }],
});

function setup(followed: boolean) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const body = url.includes("/api/mangadex/search") ? result(followed)
      : url.includes("/api/languages") ? [{ code: "en", name: "English", folder_code: "ENG" }]
      : { groups: [], unknown_group_releases: {}, chapters: [] };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  render(
    <QueryClientProvider client={makeQueryClient()}><ToastProvider>
      <MemoryRouter initialEntries={["/add"]}>
        <Routes>
          <Route path="/add" element={<AddPage />} />
          <Route path="/manga/:id" element={<div>manga page</div>} />
        </Routes>
      </MemoryRouter>
    </ToastProvider></QueryClientProvider>,
  );
}

it("does not open the add form for a pasted link of a manga already in the library", async () => {
  setup(true);
  const user = userEvent.setup();
  await user.type(screen.getByPlaceholderText(/paste a MangaDex URL/), `https://mangadex.org/title/${ID}/kara{Enter}`);
  expect(await screen.findByText("Already in your library")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /add to library/i })).toBeNull();
  await user.click(screen.getByRole("link", { name: "Open it" }));
  expect(await screen.findByText("manga page")).toBeTruthy();
});

it("opens the add form for a new manga", async () => {
  setup(false);
  const user = userEvent.setup();
  await user.type(screen.getByPlaceholderText(/paste a MangaDex URL/), `https://mangadex.org/title/${ID}{Enter}`);
  expect(await screen.findByRole("button", { name: /add to library/i })).toBeTruthy();
});
