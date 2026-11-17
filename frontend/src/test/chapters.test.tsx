import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ChapterSummary } from "../pages/Add";
import type { ChapterPreview } from "../types";

afterEach(cleanup);

const ch = (chapter: string | null, language = "en", groups = ["g1"], published = "2026-10-01T00:00:00Z"): ChapterPreview =>
  ({ chapter, volume: null, language, title: null, groups, published });

const chapters = [
  ...Array.from({ length: 20 }, (_, i) => ch(String(i + 1))),
  ch("12.5"), ch("25", "en", ["g2"]), ch(null), ch("3", "it", [""]),
];

it("summarises the chapters and sets the start chapter", async () => {
  const onStart = vi.fn();
  const user = userEvent.setup();
  render(<ChapterSummary chapters={chapters} loading={false} groups={{ mode: "all", ids: [], includeUnknown: true }}
    languages={["en", "it"]} startChapter="" onStart={onStart} />);
  expect(screen.getByText("22 chapters")).toBeTruthy(); // 1–20, 12.5, 25
  expect(screen.getByText(/Ch\. 1 → 25/)).toBeTruthy();
  expect(screen.getByText(/1 without a number/)).toBeTruthy();
  expect(screen.getByText(/4 chapter numbers are missing/)).toBeTruthy(); // 21–24
  expect(screen.getByText(/EN 22 · IT 1/)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: /only the latest/i }));
  expect(onStart).toHaveBeenLastCalledWith("25");
  await user.click(screen.getByRole("button", { name: /last 10/i }));
  expect(onStart).toHaveBeenLastCalledWith("12.5");
});

it("follows the group selection and the start chapter", () => {
  render(<ChapterSummary chapters={chapters} loading={false} groups={{ mode: "selected", ids: ["g2"], includeUnknown: false }}
    languages={["en"]} startChapter="" onStart={() => {}} autoDownload={false} />);
  expect(screen.getByText("1 chapter")).toBeTruthy();
  cleanup();
  render(<ChapterSummary chapters={chapters} loading={false} groups={{ mode: "all", ids: [], includeUnknown: true }}
    languages={["en"]} startChapter="18" onStart={() => {}} />);
  expect(screen.getByText(/4 numbered chapters and 1 extra will be downloaded/)).toBeTruthy(); // 18,19,20,25
});

it("pre-selects the only English title", async () => {
  const { defaultTitle } = await import("../pages/Add");
  const t = (title: string, language: string, source: "main" | "alt" = "alt") => ({ title, language, source, preferred: false });
  expect(defaultTitle({ title: "Mahou Shoujo ni Akogarete", titles: [t("Mahou Shoujo ni Akogarete", "ja-ro", "main"), t("Gushing over Magical Girls", "en"), t("魔法少女にあこがれて", "ja")] }))
    .toBe("Gushing over Magical Girls");
  expect(defaultTitle({ title: "X", titles: [t("X", "ja-ro", "main"), t("One", "en"), t("Two", "en")] })).toBe("X");
  expect(defaultTitle({ title: "X", titles: [t("X", "ja-ro", "main"), t("Same Title", "en"), t("same  title!", "en")] })).toBe("Same Title");
  expect(defaultTitle({ title: "X", titles: [] })).toBe("X");
});

it("offers 'new chapters only' after the latest chapter", async () => {
  const onOnlyNew = vi.fn();
  const user = userEvent.setup();
  render(<ChapterSummary chapters={chapters} loading={false} groups={{ mode: "all", ids: [], includeUnknown: true }}
    languages={["en"]} startChapter="" onStart={() => {}} onOnlyNew={onOnlyNew} />);
  await user.click(screen.getByRole("button", { name: /new chapters only \(after ch\. 25\)/i }));
  expect(onOnlyNew).toHaveBeenCalled();
  cleanup();
  render(<ChapterSummary chapters={chapters} loading={false} groups={{ mode: "all", ids: [], includeUnknown: true }}
    languages={["en"]} startChapter="" onStart={() => {}} onlyNew onOnlyNew={() => {}} />);
  expect(screen.getByRole("button", { name: /new chapters only/i }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: /all chapters/i }).getAttribute("aria-pressed")).toBe("false");
  expect(screen.getByText(/Nothing listed now is downloaded/)).toBeTruthy();
});
