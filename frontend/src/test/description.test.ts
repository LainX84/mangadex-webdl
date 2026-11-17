import { describe, expect, it } from "vitest";
import { plainDescription } from "../format";

describe("plainDescription", () => {
  it("flattens a Markdown table and links", () => {
    const md = "| Namespace | Tags |\n| :--- | :--- |\n| female | females only, kissing, yuri |\n\n[Source](https://e-hentai.org/g/1/x/)";
    expect(plainDescription(md)).toBe("Namespace · Tags\nfemale · females only, kissing, yuri\nSource");
  });
  it("strips emphasis, headings and rules", () => {
    expect(plainDescription("## Title\n**Bold** and *italic*\n---\nsnake_case_word")).toBe("Title\nBold and italic\nsnake_case_word");
  });
  it("handles empty", () => {
    expect(plainDescription(null)).toBe("");
  });
});
