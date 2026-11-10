import type { ThemeMode } from "./types";

type MermaidApi = typeof import("mermaid").default;
type PurifyApi = typeof import("dompurify").default;

let loaded: Promise<{ mermaid: MermaidApi; purify: PurifyApi }> | null = null;
let renderQueue: Promise<unknown> = Promise.resolve();
let seq = 0;
const svgCache = new Map<string, string>();

function ensureLoaded() {
  if (!loaded) {
    loaded = Promise.all([import("mermaid"), import("dompurify")]).then(([m, p]) => ({
      mermaid: m.default,
      purify: p.default,
    }));
  }
  return loaded;
}

export async function renderMermaidDiagram(
  source: string,
  themeMode: ThemeMode,
): Promise<{ ok: true; svgDataUrl: string } | { ok: false; error: string }> {
  const cleanSource = source.replace(/%%\{[\s\S]*?\}%%/g, "").trim();
  if (!cleanSource) return { ok: false, error: "Empty diagram" };
  const key = `${themeMode}::${cleanSource}`;
  const hit = svgCache.get(key);
  if (hit) return { ok: true, svgDataUrl: hit };

  const task = renderQueue.then(async () => {
    const { mermaid, purify } = await ensureLoaded();
    const isLight = themeMode === "light";
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      theme: "base",
      themeVariables: {
        darkMode: !isLight,
        background: "transparent",
        primaryColor: isLight ? "#f1f0ed" : "#212121",
        primaryTextColor: isLight ? "#242423" : "#d8d8d7",
        primaryBorderColor: isLight ? "#d4d3cd" : "#3a3a3a",
        lineColor: isLight ? "#7b7a76" : "#8a8a8a",
        secondaryColor: isLight ? "#eae9e5" : "#1c1c1c",
        tertiaryColor: isLight ? "#ffffff" : "#141414",
        fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", sans-serif',
        fontSize: "12.5px",
      },
      flowchart: { useMaxWidth: true, padding: 12, curve: "basis" },
    });

    const id = `folio-mermaid-${++seq}`;
    try {
      const { svg } = await mermaid.render(id, cleanSource);
      const frag = purify.sanitize(svg, {
        RETURN_DOM_FRAGMENT: true,
        USE_PROFILES: { svg: true, svgFilters: true, html: true },
        ADD_TAGS: ["foreignobject"],
        HTML_INTEGRATION_POINTS: { foreignobject: true },
        FORBID_TAGS: ["script", "iframe", "object", "embed", "a", "image", "img"],
        FORBID_ATTR: ["href", "xlink:href", "src", "srcset"],
      });
      const root = Array.from(frag.childNodes).find(
        (n): n is Element => n.nodeType === 1 && (n as Element).localName === "svg",
      );
      if (!root) return { ok: false as const, error: "Mermaid produced empty SVG" };
      for (const el of [root, ...Array.from(root.querySelectorAll("*"))]) {
        if (el.hasAttribute("xmlns")) el.removeAttribute("xmlns");
      }
      const xml = new XMLSerializer().serializeToString(root);
      const dataUrl = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`;
      svgCache.set(key, dataUrl);
      return { ok: true as const, svgDataUrl: dataUrl };
    } catch (err) {
      return {
        ok: false as const,
        error: err instanceof Error ? err.message : String(err),
      };
    } finally {
      document.getElementById(id)?.remove();
      document.getElementById(`d${id}`)?.remove();
    }
  });

  renderQueue = task.catch(() => undefined);
  return task;
}
