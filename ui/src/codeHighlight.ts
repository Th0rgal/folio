import "./prismSetup";
import Prism from "prismjs";
import "prismjs/components/prism-python";
import "prismjs/components/prism-typescript";
import "prismjs/components/prism-rust";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-json";
import "prismjs/components/prism-yaml";
import "prismjs/components/prism-sql";
import "prismjs/components/prism-go";
import "prismjs/components/prism-c";
import "prismjs/components/prism-cpp";
import "prismjs/components/prism-toml";
import "prismjs/components/prism-diff";
import "prismjs/components/prism-nix";

const leanGrammar: Prism.Grammar = {
  comment: [
    { pattern: /\/-(?:[^/-]|\/(?!-)|-(?!\/)|\/-[\s\S]*?-\/)*-\//, greedy: true },
    { pattern: /--[^\r\n]*/, greedy: true },
  ],
  string: { pattern: /"(?:\\[\s\S]|[^"\\])*"/, greedy: true },
  keyword:
    /\b(?:abbrev|axiom|by|calc|class|def|deriving|do|else|end|example|export|extends|for|forall|from|fun|have|if|import|in|inductive|infix|infixl|infixr|instance|let|macro|match|mutual|namespace|noncomputable|notation|opaque|open|partial|private|protected|return|section|set_option|show|structure|syntax|termination_by|then|theorem|universe|unsafe|variable|variables|where|with)\b|[∀∃λ]/,
  builtin:
    /\b(?:apply|assumption|cases|constructor|decide|exact|ext|intro|intros|rcases|refine|rfl|rw|simp|simpa|sorry|subst|unfold|use|omega|linarith|nlinarith|norm_num|ring|aesop)\b/,
  "class-name":
    /\b(?:Bool|Char|Fin|Float|Int|List|Nat|Option|Prop|Sort|String|Type|UIntN|UInt8|UInt16|UInt32|UInt64|Unit|SpecState|ValidInvariant|PreservesSafety)\b/,
  boolean: /\b(?:true|false|True|False)\b/,
  number: /\b(?:0[xX][\da-fA-F]+|0[bB][01]+|\d+(?:\.\d+)?)\b/,
  operator: /:=|=>|->|<-|[→←↔≤≥≠∧∨¬∈∉⊆⊂∪∩+*/%=<>!^|&~:-]/,
  punctuation: /[{}[\]();,.⟨⟩]/,
};

Prism.languages.lean = leanGrammar;
Prism.languages.lean4 = leanGrammar;

function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function highlightCodeSafe(code: string, langLabel: string): string {
  const normalized = langLabel.trim().toLowerCase().split(/\s+/)[0] || "";
  const aliasMap: Record<string, string> = {
    py: "python",
    ts: "typescript",
    tsx: "typescript",
    js: "javascript",
    sh: "bash",
    shell: "bash",
    zsh: "bash",
    yml: "yaml",
    rs: "rust",
  };
  const lang = aliasMap[normalized] || normalized;
  const grammar = Prism.languages[lang];
  if (!grammar || code.length > 100_000) {
    return escapeHtml(code);
  }
  return Prism.highlight(code, grammar, lang);
}
