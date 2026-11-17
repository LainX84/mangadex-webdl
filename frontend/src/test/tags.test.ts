import { describe, expect, it } from "vitest";
import { inkFor, matchesTags, nextTagColor, sortTags, tagKey, TAG_PALETTE, type HighlightedTag } from "../tags";

describe("matchesTags", () => {
  const tags = ["Romance", "Girls' Love", "School Life"];
  it("matches everything without a filter", () => {
    expect(matchesTags(tags, [], "any")).toBe(true);
    expect(matchesTags(undefined, [], "all")).toBe(true);
  });
  it("any: at least one tag, case-insensitive", () => {
    expect(matchesTags(tags, ["genderswap", "girls' love"], "any")).toBe(true);
    expect(matchesTags(tags, ["Genderswap"], "any")).toBe(false);
  });
  it("all: every tag", () => {
    expect(matchesTags(tags, ["Romance", "GIRLS' LOVE"], "all")).toBe(true);
    expect(matchesTags(tags, ["Romance", "Genderswap"], "all")).toBe(false);
    expect(matchesTags(undefined, ["Romance"], "all")).toBe(false);
  });
});

describe("sortTags", () => {
  it("puts highlighted tags first in the chosen order, others alphabetically", () => {
    const order: HighlightedTag[] = [{ name: "Gyaru", color: "#111111" }, { name: "Girls' Love", color: "#222222" }];
    const map = new Map(order.map((t) => [tagKey(t.name), t]));
    const out = sortTags(["Romance", "Girls' Love", "Award Winning", "Gyaru"], map, order);
    expect(out.map((t) => t.name)).toEqual(["Gyaru", "Girls' Love", "Award Winning", "Romance"]);
    expect(out[0].hl?.color).toBe("#111111");
    expect(out[3].hl).toBeUndefined();
  });
});

describe("colours", () => {
  it("picks readable text", () => {
    expect(inkFor("#ffff00")).toBe("#111111");
    expect(inkFor("#1c1c1c")).toBe("#ffffff");
    expect(inkFor("#7048e8")).toBe("#ffffff");
  });
  it("offers an unused palette colour", () => {
    expect(nextTagColor([])).toBe(TAG_PALETTE[0]);
    expect(nextTagColor([{ name: "A", color: TAG_PALETTE[0].toUpperCase() }])).toBe(TAG_PALETTE[1]);
  });
});
