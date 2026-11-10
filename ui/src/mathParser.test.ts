// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { createEffect, createSignal } from "solid-js";
import { render } from "solid-js/web";
import { EquationPopover } from "./EquationPopover";
import { MarkdownView } from "./MarkdownView";
import { applyMathEdit, extractMathSpans } from "./mathParser";
import type { MathSpan } from "./types";

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

  it("renders headings, lists, callouts, code blocks, and KaTeX equations in MarkdownView without throwing", () => {
    const md = [
      "# Heading One",
      "",
      "> [!tip] Invariant",
      "> Keep inline math $\\pi_0(\\theta)$ crisp.",
      "",
      "$$",
      "E = mc^2",
      "$$",
      "",
      "- [x] First task",
      "- [ ] Second task",
      "",
      "1. Ordered one",
      "2. Ordered two",
      "",
      "```lean",
      "theorem t : True := trivial",
      "```",
    ].join("\n");

    const host = document.createElement("div");
    const dispose = render(
      () =>
        MarkdownView({
          rawMarkdown: md,
          macros: { "\\R": "\\mathbb{R}" },
          themeMode: "dark",
          activeMathIndex: null,
          onSelectMath: () => {},
          onOpenWikilink: () => {},
          onExternalUrl: () => {},
        }),
      host,
    );

    expect(host.querySelector("h1.md-h1")?.textContent).toBe("Heading One");
    expect(host.querySelectorAll("ul.md-list li")).toHaveLength(2);
    expect(host.querySelectorAll("ol.md-list li")).toHaveLength(2);
    expect(host.querySelectorAll(".katex").length).toBeGreaterThanOrEqual(2);
    expect(host.querySelector(".callout-card")).not.toBeNull();
    expect(host.querySelector(".code-card")).not.toBeNull();
    dispose();
  });

  it("renders EquationPopover and synchronizes selectedMath offsets without reactive loops", () => {
    const [raw, setRaw] = createSignal("Intro $\\pi_0(\\theta)$ and $$E = mc^2$$.");
    const [selected, setSelected] = createSignal<MathSpan | null>(extractMathSpans(raw())[1]);
    let runs = 0;

    const host = document.createElement("div");
    const dispose = render(() => {
      createEffect(() => {
        runs += 1;
        const currentSpan = selected();
        if (!currentSpan) return;
        const spans = extractMathSpans(raw());
        const updatedSpan = spans[currentSpan.index];
        if (
          updatedSpan &&
          updatedSpan.tex === currentSpan.tex &&
          (updatedSpan.rawStart !== currentSpan.rawStart ||
            updatedSpan.rawEnd !== currentSpan.rawEnd ||
            updatedSpan.delimiter !== currentSpan.delimiter)
        ) {
          setSelected(updatedSpan);
        }
      });
      const span = selected();
      return span
        ? EquationPopover({
            span,
            readonly: false,
            macros: { "\\R": "\\mathbb{R}" },
            onApply: () => {},
            onSaveMacros: () => {},
            onClose: () => setSelected(null),
          })
        : null;
    }, host);

    expect(runs).toBeLessThanOrEqual(2);
    expect(host.querySelector('[data-testid="equation-popover"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="equation-live-preview"] .katex')).not.toBeNull();

    setRaw("Updated prefix text: $\\pi_0(\\theta)$ and $$E = mc^2$$.");
    expect(runs).toBeLessThanOrEqual(4);
    expect(selected()?.rawStart).toBeGreaterThan(20);
    dispose();
  });
});
