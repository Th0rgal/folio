import { For, Show, createEffect, createSignal } from "solid-js";
import { renderKatexSafe } from "./katexRender";
import type { MathSpan } from "./types";

export function EquationPopover(props: {
  span: MathSpan;
  readonly: boolean;
  macros: Record<string, string>;
  onApply: (span: MathSpan, nextTex: string) => void;
  onSaveMacros: (nextMacros: Record<string, string>) => void;
  onClose: () => void;
}) {
  const [draftTex, setDraftTex] = createSignal(props.span.tex);
  const [showMacros, setShowMacros] = createSignal(false);
  const [newMacroKey, setNewMacroKey] = createSignal("\\Prob");
  const [newMacroVal, setNewMacroVal] = createSignal("\\mathbb{P}");
  const [copied, setCopied] = createSignal(false);
  let textareaRef: HTMLTextAreaElement | undefined;

  createEffect(() => {
    setDraftTex(props.span.tex);
    queueMicrotask(() => {
      textareaRef?.focus();
    });
  });

  const preview = () => renderKatexSafe(draftTex(), props.span.display, props.macros);

  const handleCopyLatex = () => {
    void navigator.clipboard?.writeText(draftTex());
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div
      class="equation-popover"
      data-testid="equation-popover"
      onClick={(e) => e.stopPropagation()}
    >
      <div class="eq-popover-head">
        <div class="eq-popover-title">
          <span class="eq-kind-pill">
            {props.span.display ? "Display LaTeX" : "Inline LaTeX"}
          </span>
          <span class="eq-delim-code">{props.span.delimiter}</span>
          <Show when={props.readonly}>
            <span class="eq-readonly-tag">Read-only source</span>
          </Show>
        </div>
        <div class="eq-popover-actions">
          <button
            type="button"
            class="pill-btn"
            onClick={() => setShowMacros((v) => !v)}
          >
            Macros ({Object.keys(props.macros).length})
          </button>
          <button type="button" class="pill-btn" onClick={handleCopyLatex}>
            {copied() ? "Copied TeX" : "Copy LaTeX"}
          </button>
          <button
            type="button"
            class="icon-close-btn"
            aria-label="Close equation inspector"
            onClick={props.onClose}
          >
            ✕
          </button>
        </div>
      </div>

      <div class="eq-editor-grid">
        <div class="eq-source-col">
          <label class="eq-label">TeX Source</label>
          <textarea
            ref={textareaRef}
            class="eq-textarea"
            data-testid="equation-textarea"
            spellcheck={false}
            value={draftTex()}
            readOnly={props.readonly}
            onInput={(e) => setDraftTex(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.isComposing) return;
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !props.readonly) {
                e.preventDefault();
                props.onApply(props.span, draftTex());
              } else if (e.key === "Escape") {
                e.preventDefault();
                props.onClose();
              }
            }}
          />
        </div>

        <div class="eq-preview-col">
          <label class="eq-label">Live KaTeX Preview (Offline Fonts)</label>
          <div
            class={`eq-live-preview ${preview().error ? "has-error" : ""}`}
            data-testid="equation-live-preview"
            innerHTML={preview().html}
          />
          <Show when={preview().error}>
            {(err) => <div class="eq-parse-error">{err()}</div>}
          </Show>
        </div>
      </div>

      <Show when={showMacros()}>
        <div class="eq-macros-drawer">
          <div class="eq-macros-list">
            <For each={Object.entries(props.macros)}>
              {([k, v]) => (
                <div class="macro-chip">
                  <code>{k}</code>
                  <span>→</span>
                  <code>{v}</code>
                  <button
                    type="button"
                    class="macro-del"
                    onClick={() => {
                      const next = { ...props.macros };
                      delete next[k];
                      props.onSaveMacros(next);
                    }}
                  >
                    ×
                  </button>
                </div>
              )}
            </For>
          </div>
          <div class="eq-macro-add">
            <input
              type="text"
              class="s-input mono"
              placeholder="\\Macro"
              value={newMacroKey()}
              onInput={(e) => setNewMacroKey(e.currentTarget.value)}
            />
            <input
              type="text"
              class="s-input mono"
              placeholder="\\mathbb{M}"
              value={newMacroVal()}
              onInput={(e) => setNewMacroVal(e.currentTarget.value)}
            />
            <button
              type="button"
              class="pill-btn primary"
              onClick={() => {
                const k = newMacroKey().trim();
                const v = newMacroVal().trim();
                if (!k.startsWith("\\") || !v) return;
                props.onSaveMacros({ ...props.macros, [k]: v });
              }}
            >
              Save Macro
            </button>
          </div>
        </div>
      </Show>

      <div class="eq-popover-foot">
        <span class="eq-hint">
          {props.readonly
            ? "Inspecting equation from read-only Hindsight / remote document"
            : "⌘+Enter to write TeX back into local Markdown · Esc to close"}
        </span>
        <Show when={!props.readonly}>
          <button
            type="button"
            class="pill-btn primary"
            data-testid="equation-apply-btn"
            disabled={Boolean(preview().error)}
            onClick={() => props.onApply(props.span, draftTex())}
          >
            Apply to Markdown
          </button>
        </Show>
      </div>
    </div>
  );
}
