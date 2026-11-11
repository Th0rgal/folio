export type ViewMode = "preview" | "split" | "source";
export type ThemeMode = "dark" | "light";
export type CopilotProfileId =
  | "builtin/fast"
  | "builtin/smart"
  | "builtin/private"
  | "builtin/obliterated";
export type CopilotScope = "note" | "vault" | "all";

export interface ProvenanceInfo {
  author: string;
  source_type: string;
  is_agent_summary: boolean;
  source_id: string | null;
  document_id: string | null;
  bank_id: string | null;
  original_date: string | null;
  updated_at: string | null;
  related_sources: string[];
  related_pages: string[];
  tags: string[];
}

export interface BacklinkItem {
  uri: string;
  title: string;
  provider: string;
  external_id?: string;
  is_agent_summary?: boolean;
}

export interface NoteSyncInfo {
  state: "synced" | "modified" | "unsynced" | "private" | "remote";
  target_bank?: string | null;
  document_id?: string | null;
  source_id?: string | null;
  synced_at?: string | null;
  reason?: string | null;
  last_error?: string | null;
}

export interface SyncSummary {
  sync_bank: string;
  synced: number;
  modified: number;
  unsynced: number;
  private: number;
  pending_sync: number;
}

export interface DocumentRecord {
  uri: string;
  provider: string;
  external_id: string;
  title: string;
  body: string;
  raw_markdown: string;
  folder_path: string;
  frontmatter: Record<string, unknown>;
  tags: string[];
  wikilinks: string[];
  provenance: ProvenanceInfo;
  readonly: boolean;
  content_hash: string;
  updated_at: string;
  backlinks?: BacklinkItem[];
  sync?: NoteSyncInfo;
}

export interface TreeNode {
  id: string;
  name: string;
  kind: "folder" | "page" | "source";
  uri: string | null;
  provider: string;
  badge: string | null;
  is_agent_summary: boolean;
  children: TreeNode[];
}

export interface SearchHit {
  uri: string;
  provider: string;
  external_id?: string;
  scope?: string;
  title: string;
  folder_path?: string;
  snippet: string;
  tags: string[];
  is_agent_summary: boolean;
  author: string;
  match_kind?: string;
  score: number;
}

export interface LinkCandidate {
  uri: string;
  provider: string;
  external_id: string;
  title: string;
  folder_path: string;
  is_agent_summary: boolean;
}

export interface CopilotProfile {
  id: CopilotProfileId;
  label: string;
  short: string;
  description: string;
  resolved_model: string;
  sovereign_local: boolean;
}

export interface CopilotStatus {
  configured: boolean;
  connected: boolean;
  base_url: string;
  auth_source: string;
  profiles: CopilotProfile[];
  default_profile: CopilotProfileId;
}

export interface CopilotTrace {
  tool: string;
  label: string;
  items: Array<{ uri: string; title: string; provider: string }>;
}

export interface CopilotProposal {
  proposal_id: string;
  request_id: string;
  kind: "edit" | "create";
  summary: string;
  target_uri: string | null;
  title: string;
  folder: string;
  tags: string[];
  before_markdown: string;
  after_markdown: string;
  unified_diff: string;
  added_lines: number;
  removed_lines: number;
  applied: boolean;
}

export interface ProviderStatus {
  provider_id: string;
  connected: boolean;
  readonly?: boolean;
  api_url?: string;
  default_bank?: string;
  sync_bank?: string;
  credential_source?: string;
  mounts?: Array<{ mount_id: string; root_path: string; label: string }>;
  folders?: string[];
  sync_summary?: SyncSummary;
  error?: string | null;
}

export interface BootstrapPayload {
  providers: {
    local_fs: ProviderStatus;
    hindsight: ProviderStatus;
    notion: ProviderStatus;
    copilot?: CopilotStatus;
  };
  banks: Array<{ bank_id: string; name?: string; description?: string }>;
  sync_bank?: string;
  active_bank: string;
  local_tree: TreeNode[];
  macros: Record<string, string>;
  omarchy_mode: ThemeMode | null;
}

export interface MathSpan {
  index: number;
  display: boolean;
  delimiter: "$$" | "\\[" | "$" | "\\(";
  tex: string;
  raw: string;
  rawStart: number;
  rawEnd: number;
}
