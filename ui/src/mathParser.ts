import type { MathSpan } from "./types";

export function splitFrontmatterOffset(rawMarkdown: string): {
  frontmatterOffset: number;
  body: string;
} {
  if (!rawMarkdown.startsWith("---")) {
    return { frontmatterOffset: 0, body: rawMarkdown };
  }
  const m = /^---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/.exec(rawMarkdown);
  if (!m) {
    return { frontmatterOffset: 0, body: rawMarkdown };
  }
  return {
    frontmatterOffset: m[0].length,
    body: rawMarkdown.slice(m[0].length),
  };
}

function isUnescapedAt(src: string, idx: number): boolean {
  let backslashes = 0;
  let k = idx - 1;
  while (k >= 0 && src[k] === "\\") {
    backslashes += 1;
    k -= 1;
  }
  return backslashes % 2 === 0;
}

function findFenceClose(src: string, afterOpenLineIdx: number, fenceChar: string, fenceLen: number): number {
  let pos = afterOpenLineIdx;
  while (pos < src.length) {
    const lineEnd = src.indexOf("\n", pos);
    const end = lineEnd === -1 ? src.length : lineEnd;
    const line = src.slice(pos, end);
    const trimmed = line.replace(/^[ \t]{0,3}/, "");
    let run = 0;
    while (run < trimmed.length && trimmed[run] === fenceChar) run += 1;
    if (run >= fenceLen && trimmed.slice(run).trim() === "") {
      return lineEnd === -1 ? src.length : lineEnd + 1;
    }
    if (lineEnd === -1) break;
    pos = lineEnd + 1;
  }
  return src.length;
}

function isLikelyInlineDollarMath(inner: string, charAfterClose: string): boolean {
  if (!inner || inner.includes("\n")) return false;
  if (/^\s|\s$/.test(inner)) return false;
  if (/[0-9a-zA-Z$]/.test(charAfterClose)) return false;
  if (/^\d[\d,.]*(?:\s*(?:[-–—]|to)\s*)$/.test(inner)) return false;
  if (/^\d+(?:[.,]\d+)?(?:\s*(?:k|m|b|usd|eur|gbp|cad|ms|s|%))?$/i.test(inner)) {
    // Pure standalone number like `$5$` is valid math if small/single variable?Wait:
    // Currency only has one `$` unless there are two prices on the same line (`$50 and $100`).
    // In `$50 and $100`, `inner` is `50 and ` which ends with space -> already rejected by `\s$`!
    // What about `$50–$100` or `$50-$100` or `$50/$100`?
    // In `$50–$100`, `inner` is `50–`, which matches the range regex above!
    // In `$50/$100`, `charAfterClose` is `1` (`[0-9]`) -> already rejected by `charAfterClose`!
    return true;
  }
  if (/^\d[\d,.]*\s+.*\s*$/.test(inner) && !/[\\^_{}=<>+\-*/()]/.test(inner)) {
    return false;
  }
  return true;
}

export function extractMathSpans(rawMarkdown: string): MathSpan[] {
  const { frontmatterOffset, body } = splitFrontmatterOffset(rawMarkdown);
  const spans: MathSpan[] = [];
  let i = 0;
  let atLineStart = true;

  while (i < body.length) {
    if (atLineStart) {
      let indent = i;
      while (indent < body.length && (body[indent] === " " || body[indent] === "\t") && indent - i <= 3) {
        indent += 1;
      }
      const ch = body[indent];
      if (ch === "`" || ch === "~") {
        let run = 0;
        while (indent + run < body.length && body[indent + run] === ch) run += 1;
        if (run >= 3) {
          const lineEnd = body.indexOf("\n", indent + run);
          if (lineEnd === -1) break;
          const info = body.slice(indent + run, lineEnd);
          if (ch !== "`" || !info.includes("`")) {
            i = findFenceClose(body, lineEnd + 1, ch, run);
            atLineStart = true;
            continue;
          }
        }
      }
    }

    const c = body[i];
    if (c === "\n") {
      atLineStart = true;
      i += 1;
      continue;
    }
    if (c !== " " && c !== "\t") {
      atLineStart = false;
    }

    // Inline code span: `...` or ``...``
    if (c === "`" && isUnescapedAt(body, i)) {
      let ticks = 0;
      while (i + ticks < body.length && body[i + ticks] === "`") ticks += 1;
      const needle = "`".repeat(ticks);
      let searchFrom = i + ticks;
      let closed = -1;
      while (searchFrom < body.length) {
        const candidate = body.indexOf(needle, searchFrom);
        if (candidate === -1) break;
        if (body[candidate - 1] !== "`" && body[candidate + ticks] !== "`") {
          closed = candidate + ticks;
          break;
        }
        searchFrom = candidate + 1;
      }
      if (closed !== -1) {
        i = closed;
        atLineStart = false;
        continue;
      }
      i += ticks;
      continue;
    }

    // Escaped character like \$ or \[ or \(
    if (c === "\\") {
      const next = body[i + 1];
      if (next === "[") {
        const close = findUnescapedDelimiter(body, i + 2, "\\]");
        if (close !== -1) {
          const raw = body.slice(i, close + 2);
          const tex = body.slice(i + 2, close).trim();
          if (tex.length > 0) {
            spans.push({
              index: spans.length,
              display: true,
              delimiter: "\\[",
              tex,
              raw,
              rawStart: frontmatterOffset + i,
              rawEnd: frontmatterOffset + close + 2,
            });
            i = close + 2;
            atLineStart = false;
            continue;
          }
        }
      } else if (next === "(") {
        const close = findUnescapedInlineParenClose(body, i + 2);
        if (close !== -1) {
          const raw = body.slice(i, close + 2);
          const tex = body.slice(i + 2, close).trim();
          if (tex.length > 0) {
            spans.push({
              index: spans.length,
              display: false,
              delimiter: "\\(",
              tex,
              raw,
              rawStart: frontmatterOffset + i,
              rawEnd: frontmatterOffset + close + 2,
            });
            i = close + 2;
            atLineStart = false;
            continue;
          }
        }
      } else if (next !== undefined) {
        i += 2;
        atLineStart = next === "\n";
        continue;
      }
    }

    if (c === "$" && isUnescapedAt(body, i)) {
      if (body[i + 1] === "$") {
        const close = findDoubleDollarClose(body, i + 2);
        if (close !== -1) {
          const raw = body.slice(i, close + 2);
          const tex = body.slice(i + 2, close).trim();
          if (tex.length > 0) {
            spans.push({
              index: spans.length,
              display: true,
              delimiter: "$$",
              tex,
              raw,
              rawStart: frontmatterOffset + i,
              rawEnd: frontmatterOffset + close + 2,
            });
            i = close + 2;
            atLineStart = false;
            continue;
          }
        }
        i += 2;
        continue;
      }

      const prevChar = i > 0 ? body[i - 1] : "";
      if (!/[0-9a-zA-Z]/.test(prevChar)) {
        const close = findSingleDollarClose(body, i + 1);
        if (close !== -1) {
          const inner = body.slice(i + 1, close);
          const after = close + 1 < body.length ? body[close + 1] : "";
          if (isLikelyInlineDollarMath(inner, after)) {
            const raw = body.slice(i, close + 1);
            spans.push({
              index: spans.length,
              display: false,
              delimiter: "$",
              tex: inner,
              raw,
              rawStart: frontmatterOffset + i,
              rawEnd: frontmatterOffset + close + 1,
            });
            i = close + 1;
            atLineStart = false;
            continue;
          }
        }
      }
    }

    i += 1;
  }

  return spans;
}

function findUnescapedDelimiter(src: string, start: number, delim: string): number {
  let pos = start;
  while (pos < src.length) {
    const idx = src.indexOf(delim, pos);
    if (idx === -1) return -1;
    if (isUnescapedAt(src, idx)) return idx;
    pos = idx + 1;
  }
  return -1;
}

function findUnescapedInlineParenClose(src: string, start: number): number {
  let pos = start;
  while (pos < src.length) {
    if (src[pos] === "\n" && src[pos + 1] === "\n") return -1;
    if (src[pos] === "\\" && src[pos + 1] === ")" && isUnescapedAt(src, pos)) {
      return pos;
    }
    pos += 1;
  }
  return -1;
}

function findDoubleDollarClose(src: string, start: number): number {
  let pos = start;
  while (pos < src.length) {
    const idx = src.indexOf("$$", pos);
    if (idx === -1) return -1;
    if (isUnescapedAt(src, idx)) return idx;
    pos = idx + 1;
  }
  return -1;
}

function findSingleDollarClose(src: string, start: number): number {
  let pos = start;
  while (pos < src.length) {
    const ch = src[pos];
    if (ch === "\n") return -1;
    if (ch === "`") return -1;
    if (ch === "$" && isUnescapedAt(src, pos)) {
      if (src[pos + 1] === "$") return -1;
      if (src[pos - 1] !== " " && src[pos - 1] !== "\t") {
        return pos;
      }
    }
    pos += 1;
  }
  return -1;
}

export function formatMathReplacement(span: MathSpan, nextTex: string): string {
  const trimmed = nextTex.trim();
  if (span.delimiter === "$$") {
    const multiline = span.raw.includes("\n") || trimmed.includes("\n");
    return multiline ? `$$\n${trimmed}\n$$` : `$$${trimmed}$$`;
  }
  if (span.delimiter === "\\[") {
    const multiline = span.raw.includes("\n") || trimmed.includes("\n");
    return multiline ? `\\[\n${trimmed}\n\\]` : `\\[ ${trimmed} \\]`;
  }
  if (span.delimiter === "\\(") {
    return `\\(${trimmed}\\)`;
  }
  return `$${trimmed.replace(/\r?\n/g, " ")}$`;
}

export function applyMathEdit(rawMarkdown: string, span: MathSpan, nextTex: string): string {
  const replacement = formatMathReplacement(span, nextTex);
  return rawMarkdown.slice(0, span.rawStart) + replacement + rawMarkdown.slice(span.rawEnd);
}
