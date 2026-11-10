import { describe, expect, it } from "vitest";
import { applyMathEdit, extractMathSpans } from "./mathParser";

describe("extractMathSpans", () => {
  it("parses inline $, \\(...\\), display $$, and \\[...\\] with exact raw offsets", () => {
    const md = [
      "---",
      'title: "Spec $not_math$"',
      "---",
      "",
      "Inline $\\pi_0(\\theta)$ and \\(x^2 + y^2 = 1\\).",
      "",
      "$$",
      "E = mc^2",
      "$$",
      "",
      "\\[",
      "\\int_0^1 x\\,\\mathrm{d}x = \\frac{1}{2}",
      "\\]",
    ].join("\n");

    const spans = extractMathSpans(md);
    expect(spans.map((s) => ({ d: s.delimiter, display: s.display, tex: s.tex }))).toEqual([
      { d: "$", display: false, tex: "\\pi_0(\\theta)" },
      { d: "\\(", display: false, tex: "x^2 + y^2 = 1" },
      { d: "$$", display: true, tex: "E = mc^2" },
      { d: "\\[", display: true, tex: "\\int_0^1 x\\,\\mathrm{d}x = \\frac{1}{2}" },
    ]);

    for (const s of spans) {
      expect(md.slice(s.rawStart, s.rawEnd)).toBe(s.raw);
    }
  });

  it("ignores escaped \\$100, currency ranges $50–$100, inline code, and fenced code blocks", () => {
    const md = [
      "Price is \\$100 or between $50 and $100, or $50–$100, or $50/$100.",
      "Inline code `const x = \"$HOME/$USER\";` stays untouched.",
      "",
      "```bash",
      "echo \"$FOO\" \"$BAR\"",
      "$$not_display_math$$",
      "```",
      "",
      "Actual math $\\alpha + \\beta$ works.",
    ].join("\n");

    const spans = extractMathSpans(md);
    expect(spans).toHaveLength(1);
    expect(spans[0].tex).toBe("\\alpha + \\beta");
  });

  it("rewrites an edited equation back into the exact Markdown span", () => {
    const md = "Prior $\\pi_0(\\theta)$ updates to $$p(\\theta \\mid y)$$ cleanly.";
    const spans = extractMathSpans(md);
    expect(spans).toHaveLength(2);
    const updated = applyMathEdit(md, spans[0], "\\pi_1(\\theta)");
    expect(updated).toBe("Prior $\\pi_1(\\theta)$ updates to $$p(\\theta \\mid y)$$ cleanly.");
  });
});
