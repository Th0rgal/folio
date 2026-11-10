import katex from "katex";
import "katex/dist/katex.min.css";
import "katex/contrib/copy-tex";

export interface KatexRenderOutcome {
  html: string;
  error: string | null;
}

export function renderKatexSafe(
  tex: string,
  displayMode: boolean,
  macros: Record<string, string> = {},
): KatexRenderOutcome {
  try {
    const clonedMacros: Record<string, string> = { ...macros };
    const html = katex.renderToString(tex, {
      displayMode,
      throwOnError: true,
      trust: false,
      strict: "ignore",
      maxExpand: 1000,
      macros: clonedMacros,
    });
    return { html, error: null };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    const fallback = katex.renderToString(tex, {
      displayMode,
      throwOnError: false,
      trust: false,
      strict: "ignore",
      maxExpand: 1000,
      macros: { ...macros },
    });
    return { html: fallback, error: message };
  }
}
