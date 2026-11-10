export type ViewMode = "preview" | "split" | "source";
export type ThemeMode = "dark" | "light";

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
  title: string;
  snippet: string;
  tags: string[];
  is_agent_summary: boolean;
  author: string;
  score: number;
}

export interface ProviderStatus {
  provider_id: string;
  connected: boolean;
  readonly?: boolean;
  api_url?: string;
  default_bank?: string;
  credential_source?: string;
  mounts?: Array<{ mount_id: string; root_path: string; label: string }>;
  error?: string | null;
}

export interface BootstrapPayload {
  providers: {
    local_fs: ProviderStatus;
    hindsight: ProviderStatus;
    notion: ProviderStatus;
  };
  banks: Array<{ bank_id: string; name?: string; description?: string }>;
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
