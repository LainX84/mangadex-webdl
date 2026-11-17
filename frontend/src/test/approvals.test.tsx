import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "../App";
import { ToastProvider } from "../components";
import ApprovalsPage from "../pages/Approvals";
import { countTags } from "../tags";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); try { localStorage.clear(); } catch { /* none */ } });

const item = (id: string, title: string, tags: string[], reading_status: string | null = "plan_to_read", english_title: string | null = null) => ({
  id, title, english_title, canonical_title: title, cover_url: null, description: "", status: "ongoing", year: 2024,
  content_rating: "safe", original_language: "ko", last_chapter: null, available_languages: ["en"], languages: ["en"],
  tags, pending_since: "2026-11-01T10:00:00Z", reading_status, followed: true, monitoring_enabled: false,
  mangadex_url: `https://mangadex.org/title/${id}`,
});

const ITEMS = [
  item("a", "Villain Story", ["Villainess", "Romance", "Isekai"], "reading"),
  item("b", "Swap Story", ["Genderswap", "Comedy"]),
  item("c", "Both Story", ["Villainess", "Genderswap"]),
  item("d", "Plain Story", ["Action"]),
  item("e", "Eigo Monogatari", ["Drama"], "on_hold", "English Story"),
];

let location = "";
function Where() {
  location = useLocation().search;
  return null;
}

function setup(initial = "/approvals", highlighted: { name: string; color: string }[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const body = url.includes("/api/approvals") ? { items: ITEMS, total: ITEMS.length, dismissed: [] }
      : url.includes("/api/settings") ? { settings: { highlighted_tags: highlighted }, defaults: {} }
      : { configured: true, username: "me" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  render(
    <QueryClientProvider client={makeQueryClient()}><ToastProvider>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/approvals" element={<><ApprovalsPage /><Where /></>} />
        </Routes>
      </MemoryRouter>
    </ToastProvider></QueryClientProvider>,
  );
}

const titles = () => screen.queryAllByText(/ Story$/).map((e) => e.textContent);

describe("countTags", () => {
  it("counts titles per tag, case-insensitively, once per title", () => {
    const c = countTags([{ tags: ["Romance", "romance"] }, { tags: ["ROMANCE", "Isekai"] }, {}]);
    expect(c.get("romance")).toEqual({ name: "Romance", count: 2 });
    expect(c.get("isekai")?.count).toBe(1);
  });
});

describe("Approvals tag filter", () => {
  it("filters by a tag picked from the menu and puts it in the URL", async () => {
    setup();
    const user = userEvent.setup();
    expect(await screen.findByText("Plain Story")).toBeTruthy();
    const menu = screen.getByRole("combobox", { name: "Filter by tag" });
    expect(within(menu).getByRole("option", { name: "Villainess (2)" })).toBeTruthy();
    await user.selectOptions(menu, "Villainess");
    expect(titles()).toEqual(["Villain Story", "Both Story"]);
    expect(location).toContain("tag=Villainess");
    expect(screen.getByText("Showing 2 of 5 titles.")).toBeTruthy();
  });

  it("combines tags with any / all", async () => {
    setup("/approvals?tag=Villainess&tag=Genderswap");
    const user = userEvent.setup();
    expect(await screen.findByText("Swap Story")).toBeTruthy();
    expect(titles()).toEqual(["Villain Story", "Swap Story", "Both Story"]);
    await user.click(screen.getByRole("button", { name: "All of these" }));
    expect(titles()).toEqual(["Both Story"]);
    await user.click(screen.getByRole("button", { name: "Clear tags" }));
    expect(titles()).toHaveLength(5);
    expect(location).toBe("");
  });

  it("has no fixed tag chips: highlighted tags are in the menu like the others", async () => {
    setup("/approvals", [{ name: "Genderswap", color: "#7048e8" }]);
    const user = userEvent.setup();
    expect(await screen.findByText("Plain Story")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Genderswap/ })).toBeNull();
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by tag" }), "Genderswap");
    expect(titles()).toEqual(["Swap Story", "Both Story"]);
    // The picked tag becomes a removable chip; status chips count only what the tag filter leaves
    expect(screen.getByRole("button", { name: /^Genderswap\s*2/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^All\s*2$/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Reading/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: /^Plan to read\s*2$/ }));
    expect(titles()).toEqual(["Swap Story", "Both Story"]);
  });

  it("shows the English title first, with the MangaDex title below, and searches both", async () => {
    setup();
    const user = userEvent.setup();
    expect(await screen.findByText("English Story")).toBeTruthy();
    expect(screen.getByText("Eigo Monogatari")).toBeTruthy();
    await user.type(screen.getByRole("searchbox"), "english");
    expect(titles()).toEqual(["English Story"]);
    await user.clear(screen.getByRole("searchbox"));
    await user.type(screen.getByRole("searchbox"), "monogatari");
    expect(titles()).toEqual(["English Story"]);
  });

  it("shows the filtered tag on the card even when it is not highlighted", async () => {
    setup("/approvals?tag=Isekai");
    expect(await screen.findByText("Villain Story")).toBeTruthy();
    expect(within(screen.getByLabelText("Tags")).getByText("Isekai")).toBeTruthy();
  });
});
