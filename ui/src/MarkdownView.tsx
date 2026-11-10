import { For, Show, createEffect, createSignal } from "solid-js";
import { Dynamic } from "solid-js/web";
import { highlightCodeSafe } from "./codeHighlight";
import { renderKatexSafe } from "./katexRender";
import { extractMathSpans, splitFrontmatterOffset } from "./mathParser";
import { renderMermaidDiagram } from "./mermaidRender";
import type { MathSpan, ThemeMode } from "./types";

export interface InlineNodeText {
  kind: "text";
  text: string;
}
export interface InlineNodeCode {
  kind: "code";
  code: string;
}
export interface InlineNodeMath {
  kind: "math";
  span: MathSpan;
}
export interface InlineNodeWikilink {
  kind: "wikilink";
  target: string;
  label: string;
}
export interface InlineNodeLink {
  kind: "link";
  href: string;
  label: string;
}
export interface InlineNodeStrong {
  kind: "strong";
  children: InlineNode[];
}
export interface InlineNodeEm {
  kind: "em";
  children: InlineNode[];
}
export interface InlineNodeRefChip {
  kind: "refchip";
  refId: string;
}

export type InlineNode =
  | InlineNodeText
  | InlineNodeCode
  | InlineNodeMath
  | InlineNodeWikilink
  | InlineNodeLink
  | InlineNodeStrong
  | InlineNodeEm
  | InlineNodeRefChip;

type BlockNode =
  | { kind: "heading"; level: 1 | 2 | 3 | 4; text: string; id: string; inlines: InlineNode[] }
  | { kind: "paragraph"; inlines: InlineNode[] }
  | { kind: "math_block"; span: MathSpan }
  | { kind: "code_block"; lang: string; code: string }
  | { kind: "mermaid_block"; source: string }
  | { kind: "callout"; calloutType: string; title: string; bodyLines: InlineNode[][] }
  | { kind: "blockquote"; lines: InlineNode[][] }
  | { kind: "list"; ordered: boolean; items: Array<{ checked: boolean | null; inlines: InlineNode[] }> }
  | { kind: "table"; headers: InlineNode[][]; aligns: Array<"left" | "center" | "right">; rows: InlineNode[][][] }
  | { kind: "hr" };

const MATH_TOKEN_RE = /\uE000MATH(\d+)\uE001/;

function slugifyHeading(raw: string): string {
  return (
    raw
      .toLowerCase()
      .replace(/[^\w\s-]/g, "")
      .trim()
      .replace(/\s+/g, "-") || "section"
  );
}

function replaceMathWithPlaceholders(rawMarkdown: string): {
  tokenizedBody: string;
  spans: MathSpan[];
} {
  const { frontmatterOffset, body } = splitFrontmatterOffset(rawMarkdown);
  const spans = extractMathSpans(rawMarkdown);
  if (spans.length === 0) {
    return { tokenizedBody: body, spans };
  }
  let out = "";
  let cursor = 0;
  for (const span of spans) {
    const relStart = span.rawStart - frontmatterOffset;
    const relEnd = span.rawEnd - frontmatterOffset;
    if (relStart < cursor) continue;
    out += body.slice(cursor, relStart);
    out += `\uE000MATH${span.index}\uE001`;
    cursor = relEnd;
  }
  out += body.slice(cursor);
  return { tokenizedBody: out, spans };
}

function parseInlines(raw: string, spans: MathSpan[]): InlineNode[] {
  const nodes: InlineNode[] = [];
  let i = 0;

  const pushText = (txt: string) => {
    if (!txt) return;
    const unescaped = txt.replace(/\\([\\`$[\](){}*_#+-])/g, "$1");
    const last = nodes[nodes.length - 1];
    if (last && last.kind === "text") {
      last.text += unescaped;
    } else {
      nodes.push({ kind: "text", text: unescaped });
    }
  };

  while (i < raw.length) {
    // Math token
    if (raw[i] === "\uE000" && raw.startsWith("\uE000MATH", i)) {
      const end = raw.indexOf("\uE001", i + 5);
      if (end !== -1) {
        const idx = Number(raw.slice(i + 5, end));
        const span = spans[idx];
        if (span) {
          nodes.push({ kind: "math", span });
          i = end + 1;
          continue;
        }
      }
    }

    // Escaped char
    if (raw[i] === "\\" && i + 1 < raw.length) {
      pushText(raw.slice(i, i + 2));
      i += 2;
      continue;
    }

    // Inline code
    if (raw[i] === "`") {
      let ticks = 0;
      while (i + ticks < raw.length && raw[i + ticks] === "`") ticks += 1;
      const delim = "`".repeat(ticks);
      const close = raw.indexOf(delim, i + ticks);
      if (close !== -1) {
        const codeContent = raw.slice(i + ticks, close);
        // Restore any accidental math tokens if any existed (mathParser skips inline code so none exist)
        if (/^(?:kp|src)-[a-zA-Z0-9_-]{6,}$/.test(codeContent.trim())) {
          nodes.push({ kind: "refchip", refId: codeContent.trim() });
        } else {
          nodes.push({ kind: "code", code: codeContent });
        }
        i = close + ticks;
        continue;
      }
    }

    // Wikilink [[target|label]] or [[target]]
    if (raw[i] === "[" && raw[i + 1] === "[") {
      const close = raw.indexOf("]]", i + 2);
      if (close !== -1) {
        const inner = raw.slice(i + 2, close).trim();
        const pipe = inner.indexOf("|");
        const target = (pipe >= 0 ? inner.slice(0, pipe) : inner).trim();
        const label = (pipe >= 0 ? inner.slice(pipe + 1) : inner).trim();
        if (target) {
          nodes.push({ kind: "wikilink", target, label: label || target });
          i = close + 2;
          continue;
        }
      }
    }

    // Markdown link [label](href)
    if (raw[i] === "[") {
      const closeBracket = raw.indexOf("]", i + 1);
      if (closeBracket !== -1 && raw[closeBracket + 1] === "(") {
        const closeParen = raw.indexOf(")", closeBracket + 2);
        if (closeParen !== -1) {
          const label = raw.slice(i + 1, closeBracket);
          const href = raw.slice(closeBracket + 2, closeParen).trim();
          nodes.push({ kind: "link", href, label });
          i = closeParen + 1;
          continue;
        }
      }
    }

    // Strong **...**
    if (raw[i] === "*" && raw[i + 1] === "*") {
      const close = raw.indexOf("**", i + 2);
      if (close > i + 2) {
        nodes.push({
          kind: "strong",
          children: parseInlines(raw.slice(i + 2, close), spans),
        });
        i = close + 2;
        continue;
      }
    }

    // Emphasis *...*
    if (raw[i] === "*" && raw[i + 1] !== " ") {
      const close = raw.indexOf("*", i + 1);
      if (close > i + 1) {
        nodes.push({
          kind: "em",
          children: parseInlines(raw.slice(i + 1, close), spans),
        });
        i = close + 1;
        continue;
      }
    }

    // Bare Hindsight IDs in prose (`kp-xxxx` or `src-xxxx`)
    if ((raw.startsWith("kp-", i) || raw.startsWith("src-", i)) && (i === 0 || /[\s([,;:]/.test(raw[i - 1]))) {
      const m = /^(?:kp|src)-[a-f0-9]{8,40}/i.exec(raw.slice(i));
      if (m) {
        nodes.push({ kind: "refchip", refId: m[0] });
        i += m[0].length;
        continue;
      }
    }

    pushText(raw[i]);
    i += 1;
  }

  return nodes;
}

function splitTableCells(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells: string[] = [];
  let cur = "";
  for (let i = 0; i < trimmed.length; i++) {
    if (trimmed[i] === "\\" && trimmed[i + 1] === "|") {
      cur += "|";
      i += 1;
    } else if (trimmed[i] === "|") {
      cells.push(cur.trim());
      cur = "";
    } else {
      cur += trimmed[i];
    }
  }
  cells.push(cur.trim());
  return cells;
}

function parseBlocks(rawMarkdown: string): { blocks: BlockNode[]; spans: MathSpan[]; headings: Array<{ level: number; text: string; id: string }> } {
  const { tokenizedBody, spans } = replaceMathWithPlaceholders(rawMarkdown);
  const lines = tokenizedBody.replace(/\r\n/g, "\n").split("\n");
  const blocks: BlockNode[] = [];
  const headings: Array<{ level: number; text: string; id: string }> = [];

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    // Fenced code / mermaid block
    const fenceMatch = /^[ \t]{0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fenceMatch) {
      const fence = fenceMatch[1];
      const lang = fenceMatch[2].trim();
      const buf: string[] = [];
      i += 1;
      while (i < lines.length) {
        const cl = lines[i].replace(/^[ \t]{0,3}/, "");
        if (cl.startsWith(fence) && cl.slice(fence.length).trim() === "") {
          i += 1;
          break;
        }
        buf.push(lines[i]);
        i += 1;
      }
      const code = buf.join("\n");
      if (lang.toLowerCase() === "mermaid") {
        blocks.push({ kind: "mermaid_block", source: code });
      } else {
        blocks.push({ kind: "code_block", lang, code });
      }
      continue;
    }

    // Standalone display math token line
    const mathLineMatch = /^\uE000MATH(\d+)\uE001$/.exec(trimmed);
    if (mathLineMatch) {
      const span = spans[Number(mathLineMatch[1])];
      if (span && span.display) {
        blocks.push({ kind: "math_block", span });
        i += 1;
        continue;
      }
    }

    // Horizontal rule
    if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      blocks.push({ kind: "hr" });
      i += 1;
      continue;
    }

    // Heading
    const headingMatch = /^(#{1,4})\s+(.+)$/.exec(trimmed);
    if (headingMatch) {
      const level = Math.min(4, headingMatch[1].length) as 1 | 2 | 3 | 4;
      const rawHeading = headingMatch[2].trim();
      const cleanHeading = rawHeading.replace(/\uE000MATH(\d+)\uE001/g, (_, idx) => spans[Number(idx)]?.tex || "");
      const id = slugifyHeading(cleanHeading);
      headings.push({ level, text: cleanHeading, id });
      blocks.push({
        kind: "heading",
        level,
        text: cleanHeading,
        id,
        inlines: parseInlines(rawHeading, spans),
      });
      i += 1;
      continue;
    }

    // Blockquote / Obsidian-style Callout > [!tip] Title
    if (trimmed.startsWith(">")) {
      const quoteLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ""));
        i += 1;
      }
      const calloutHeader = /^\[!(\w+)\]\s*(.*)$/.exec(quoteLines[0] || "");
      if (calloutHeader) {
        const calloutType = calloutHeader[1].toLowerCase();
        const title = calloutHeader[2].trim() || calloutType.toUpperCase();
        const bodyLines = quoteLines
          .slice(1)
          .filter((l) => l.trim().length > 0)
          .map((l) => parseInlines(l, spans));
        blocks.push({ kind: "callout", calloutType, title, bodyLines });
      } else {
        blocks.push({
          kind: "blockquote",
          lines: quoteLines.filter((l) => l.trim().length > 0).map((l) => parseInlines(l, spans)),
        });
      }
      continue;
    }

    // Table
    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/.test(lines[i + 1])
    ) {
      const headerCells = splitTableCells(lines[i]).map((c) => parseInlines(c, spans));
      const alignCells = splitTableCells(lines[i + 1]).map((a) => {
        if (a.startsWith(":") && a.endsWith(":")) return "center" as const;
        if (a.endsWith(":")) return "right" as const;
        return "left" as const;
      });
      i += 2;
      const rows: InlineNode[][][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        rows.push(splitTableCells(lines[i]).map((c) => parseInlines(c, spans)));
        i += 1;
      }
      blocks.push({ kind: "table", headers: headerCells, aligns: alignCells, rows });
      continue;
    }

    // List (unordered / task / ordered)
    const listMatch = /^([ \t]*)(?:([-*+])|(\d+\.))\s+(.*)$/.exec(line);
    if (listMatch) {
      const ordered = Boolean(listMatch[3]);
      const items: Array<{ checked: boolean | null; inlines: InlineNode[] }> = [];
      while (i < lines.length) {
        const m = /^([ \t]*)(?:([-*+])|(\d+\.))\s+(.*)$/.exec(lines[i]);
        if (!m) break;
        let itemBody = m[4];
        let checked: boolean | null = null;
        const taskMatch = /^\[([ xX])\]\s+(.*)$/.exec(itemBody);
        if (taskMatch) {
          checked = taskMatch[1].toLowerCase() === "x";
          itemBody = taskMatch[2];
        }
        items.push({ checked, inlines: parseInlines(itemBody, spans) });
        i += 1;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }

    // Paragraph (coalesce consecutive plain lines, stopping before special blocks)
    const paraLines: string[] = [];
    while (i < lines.length) {
      const l = lines[i];
      const t = l.trim();
      if (!t) break;
      if (
        /^[ \t]{0,3}(`{3,}|~{3,})/.test(l) ||
        /^(#{1,4})\s+/.test(t) ||
        t.startsWith(">") ||
        /^(?:-{3,}|\*{3,}|_{3,})$/.test(t) ||
        /^([ \t]*)(?:[-*+]|\d+\.)\s+/.test(l) ||
        (/^\uE000MATH(\d+)\uE001$/.test(t) && spans[Number(MATH_TOKEN_RE.exec(t)?.[1] ?? -1)]?.display)
      ) {
        break;
      }
      paraLines.push(t);
      i += 1;
    }
    if (paraLines.length > 0) {
      blocks.push({
        kind: "paragraph",
        inlines: parseInlines(paraLines.join(" "), spans),
      });
    } else {
      i += 1;
    }
  }

  return { blocks, spans, headings };
}

function MermaidCard(props: { source: string; themeMode: ThemeMode }) {
  const [svgUrl, setSvgUrl] = createSignal<string | null>(null);
  const [err, setErr] = createSignal<string | null>(null);

  createEffect(() => {
    const src = props.source;
    const mode = props.themeMode;
    let cancelled = false;
    setErr(null);
    void renderMermaidDiagram(src, mode).then((res) => {
      if (cancelled) return;
      if (res.ok) {
        setSvgUrl(res.svgDataUrl);
      } else {
        setErr(res.error);
      }
    });
    return () => {
      cancelled = true;
    };
  });

  return (
    <div class="mermaid-card">
      <Show
        when={!err()}
        fallback={
          <pre class="code-fence mermaid-fallback">
            <code>{props.source}</code>
          </pre>
        }
      >
        <Show
          when={svgUrl()}
          fallback={<div class="mermaid-loading">Rendering diagram…</div>}
        >
          {(url) => <img class="mermaid-svg" src={url()} alt="Mermaid diagram" />}
        </Show>
      </Show>
    </div>
  );
}

function InlineRun(props: {
  nodes: InlineNode[];
  macros: Record<string, string>;
  activeMathIndex: number | null;
  onSelectMath: (span: MathSpan, anchor: HTMLElement) => void;
  onOpenWikilink: (target: string) => void;
  onExternalUrl: (url: string) => void;
}) {
  return (
    <For each={props.nodes}>
      {(node) => {
        if (node.kind === "text") return <>{node.text}</>;
        if (node.kind === "code") return <code class="inline-code">{node.code}</code>;
        if (node.kind === "refchip") {
          const isKp = node.refId.startsWith("kp-");
          return (
            <button
              type="button"
              class={`ref-chip ${isKp ? "kp" : "src"}`}
              title={`Open Hindsight ${isKp ? "Knowledge Page" : "Raw Source"} ${node.refId}`}
              onClick={() => props.onOpenWikilink(node.refId)}
            >
              <span class="ref-dot" />
              {node.refId.slice(0, 15)}
            </button>
          );
        }
        if (node.kind === "wikilink") {
          const isHindsight = node.target.startsWith("kp-") || node.target.startsWith("src-");
          return (
            <button
              type="button"
              class={`wikilink-pill ${isHindsight ? "hindsight" : ""}`}
              onClick={() => props.onOpenWikilink(node.target)}
            >
              <span class="wikilink-bracket">[[</span>
              {node.label}
              <span class="wikilink-bracket">]]</span>
            </button>
          );
        }
        if (node.kind === "link") {
          return (
            <a
              href={node.href}
              class="md-link"
              onClick={(e) => {
                e.preventDefault();
                if (node.href.startsWith("http://") || node.href.startsWith("https://")) {
                  props.onExternalUrl(node.href);
                } else {
                  props.onOpenWikilink(node.href);
                }
              }}
            >
              {node.label}
            </a>
          );
        }
        if (node.kind === "strong") {
          return (
            <strong>
              <InlineRun {...props} nodes={node.children} />
            </strong>
          );
        }
        if (node.kind === "em") {
          return (
            <em>
              <InlineRun {...props} nodes={node.children} />
            </em>
          );
        }
        if (node.kind === "math") {
          const rendered = () => renderKatexSafe(node.span.tex, node.span.display, props.macros);
          return (
            <span
              class={`math-interactive ${node.span.display ? "math-display-inline" : "math-inline"} ${
                props.activeMathIndex === node.span.index ? "selected" : ""
              } ${rendered().error ? "has-error" : ""}`}
              data-math-index={node.span.index}
              title="Click to inspect or edit LaTeX equation"
              onClick={(e) => {
                e.stopPropagation();
                props.onSelectMath(node.span, e.currentTarget);
              }}
              innerHTML={rendered().html}
            />
          );
        }
        return null;
      }}
    </For>
  );
}

export function MarkdownView(props: {
  rawMarkdown: string;
  macros: Record<string, string>;
  themeMode: ThemeMode;
  activeMathIndex: number | null;
  onSelectMath: (span: MathSpan, anchor: HTMLElement) => void;
  onOpenWikilink: (target: string) => void;
  onExternalUrl: (url: string) => void;
}) {
  const parsed = () => parseBlocks(props.rawMarkdown);
  const [copiedIdx, setCopiedIdx] = createSignal<number | null>(null);

  return (
    <div class="markdown-body" data-testid="markdown-body">
      <For each={parsed().blocks}>
        {(block, idx) => {
          if (block.kind === "heading") {
            const tag = `h${block.level}` as "h1" | "h2" | "h3" | "h4";
            return (
              <Dynamic component={tag} id={block.id} class={`md-h md-h${block.level}`}>
                <InlineRun
                  nodes={block.inlines}
                  macros={props.macros}
                  activeMathIndex={props.activeMathIndex}
                  onSelectMath={props.onSelectMath}
                  onOpenWikilink={props.onOpenWikilink}
                  onExternalUrl={props.onExternalUrl}
                />
              </Dynamic>
            );
          }

          if (block.kind === "paragraph") {
            return (
              <p class="md-p">
                <InlineRun
                  nodes={block.inlines}
                  macros={props.macros}
                  activeMathIndex={props.activeMathIndex}
                  onSelectMath={props.onSelectMath}
                  onOpenWikilink={props.onOpenWikilink}
                  onExternalUrl={props.onExternalUrl}
                />
              </p>
            );
          }

          if (block.kind === "math_block") {
            const rendered = () => renderKatexSafe(block.span.tex, true, props.macros);
            return (
              <div
                class={`math-block-card math-interactive ${
                  props.activeMathIndex === block.span.index ? "selected" : ""
                } ${rendered().error ? "has-error" : ""}`}
                data-testid={`math-block-${block.span.index}`}
                data-math-index={block.span.index}
                onClick={(e) => {
                  e.stopPropagation();
                  props.onSelectMath(block.span, e.currentTarget);
                }}
              >
                <div class="math-block-badge">
                  <span>{block.span.delimiter === "\\[" ? "\\[ … \\]" : "$$ … $$"}</span>
                  <span class="math-edit-hint">Click to edit TeX</span>
                </div>
                <div class="math-block-render" innerHTML={rendered().html} />
                <Show when={rendered().error}>
                  {(errMsg) => <div class="math-error-line">{errMsg()}</div>}
                </Show>
              </div>
            );
          }

          if (block.kind === "mermaid_block") {
            return <MermaidCard source={block.source} themeMode={props.themeMode} />;
          }

          if (block.kind === "code_block") {
            const highlighted = () => highlightCodeSafe(block.code, block.lang);
            return (
              <div class="code-card">
                <div class="code-card-bar">
                  <span class="code-lang">{block.lang || "text"}</span>
                  <button
                    type="button"
                    class="code-copy-btn"
                    onClick={() => {
                      void navigator.clipboard?.writeText(block.code);
                      setCopiedIdx(idx());
                      window.setTimeout(() => {
                        if (copiedIdx() === idx()) setCopiedIdx(null);
                      }, 1400);
                    }}
                  >
                    {copiedIdx() === idx() ? "Copied" : "Copy"}
                  </button>
                </div>
                <pre class="code-fence">
                  <code innerHTML={highlighted()} />
                </pre>
              </div>
            );
          }

          if (block.kind === "callout") {
            return (
              <div class={`callout-card callout-${block.calloutType}`}>
                <div class="callout-title">
                  <span class="callout-pill">{block.calloutType}</span>
                  <span>{block.title}</span>
                </div>
                <div class="callout-body">
                  <For each={block.bodyLines}>
                    {(lineNodes) => (
                      <p>
                        <InlineRun
                          nodes={lineNodes}
                          macros={props.macros}
                          activeMathIndex={props.activeMathIndex}
                          onSelectMath={props.onSelectMath}
                          onOpenWikilink={props.onOpenWikilink}
                          onExternalUrl={props.onExternalUrl}
                        />
                      </p>
                    )}
                  </For>
                </div>
              </div>
            );
          }

          if (block.kind === "blockquote") {
            return (
              <blockquote class="md-quote">
                <For each={block.lines}>
                  {(lineNodes) => (
                    <p>
                      <InlineRun
                        nodes={lineNodes}
                        macros={props.macros}
                        activeMathIndex={props.activeMathIndex}
                        onSelectMath={props.onSelectMath}
                        onOpenWikilink={props.onOpenWikilink}
                        onExternalUrl={props.onExternalUrl}
                      />
                    </p>
                  )}
                </For>
              </blockquote>
            );
          }

          if (block.kind === "list") {
            const listTag = block.ordered ? "ol" : "ul";
            return (
              <Dynamic component={listTag} class="md-list">
                <For each={block.items}>
                  {(item) => (
                    <li class={item.checked !== null ? "md-task-item" : ""}>
                      <Show when={item.checked !== null}>
                        <span class={`task-checkbox ${item.checked ? "checked" : ""}`}>
                          {item.checked ? "✓" : ""}
                        </span>
                      </Show>
                      <span class={item.checked ? "task-done-text" : ""}>
                        <InlineRun
                          nodes={item.inlines}
                          macros={props.macros}
                          activeMathIndex={props.activeMathIndex}
                          onSelectMath={props.onSelectMath}
                          onOpenWikilink={props.onOpenWikilink}
                          onExternalUrl={props.onExternalUrl}
                        />
                      </span>
                    </li>
                  )}
                </For>
              </Dynamic>
            );
          }

          if (block.kind === "table") {
            return (
              <div class="table-wrap">
                <table class="md-table">
                  <thead>
                    <tr>
                      <For each={block.headers}>
                        {(cell, colIdx) => (
                          <th style={{ "text-align": block.aligns[colIdx()] || "left" }}>
                            <InlineRun
                              nodes={cell}
                              macros={props.macros}
                              activeMathIndex={props.activeMathIndex}
                              onSelectMath={props.onSelectMath}
                              onOpenWikilink={props.onOpenWikilink}
                              onExternalUrl={props.onExternalUrl}
                            />
                          </th>
                        )}
                      </For>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={block.rows}>
                      {(row) => (
                        <tr>
                          <For each={row}>
                            {(cell, colIdx) => (
                              <td style={{ "text-align": block.aligns[colIdx()] || "left" }}>
                                <InlineRun
                                  nodes={cell}
                                  macros={props.macros}
                                  activeMathIndex={props.activeMathIndex}
                                  onSelectMath={props.onSelectMath}
                                  onOpenWikilink={props.onOpenWikilink}
                                  onExternalUrl={props.onExternalUrl}
                                />
                              </td>
                            )}
                          </For>
                        </tr>
                      )}
                    </For>
                  </tbody>
                </table>
              </div>
            );
          }

          if (block.kind === "hr") {
            return <hr class="md-hr" />;
          }

          return null;
        }}
      </For>
    </div>
  );
}
