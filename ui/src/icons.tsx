import type { JSX } from "solid-js";

type IconProps = { size?: number; class?: string; sw?: number };

const I = (p: IconProps & { children: JSX.Element }) => (
  <svg
    class={p.class}
    width={p.size ?? 16}
    height={p.size ?? 16}
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    stroke-width={p.sw ?? 1.15}
    stroke-linecap="round"
    stroke-linejoin="round"
  >
    {p.children}
  </svg>
);

export const SidebarIcon = (p: IconProps) => (
  <I {...p}>
    <rect x="2" y="3" width="12" height="10" rx="2" />
    <path d="M6 3v10" />
  </I>
);

export const SearchIcon = (p: IconProps) => (
  <I {...p}>
    <circle cx="7.2" cy="7.2" r="4.2" />
    <path d="M10.4 10.4 13.5 13.5" />
  </I>
);

export const NewNoteIcon = (p: IconProps) => (
  <I {...p}>
    <path d="M9.2 2.4H4.4A1.4 1.4 0 0 0 3 3.8v8.4a1.4 1.4 0 0 0 1.4 1.4h7.2a1.4 1.4 0 0 0 1.4-1.4V6.2z" />
    <path d="M9 2.5v3.8h3.9" />
    <path d="M8 8.3v3.2M6.4 9.9h3.2" />
  </I>
);

export const PlusIcon = (p: IconProps) => (
  <I {...p}>
    <path d="M8 3.3v9.4M3.3 8h9.4" />
  </I>
);

export const FolderIcon = (p: IconProps) => (
  <I {...p}>
    <path d="M2.2 4.4a1.2 1.2 0 0 1 1.2-1.2h2.8l1.3 1.5h5.1a1.2 1.2 0 0 1 1.2 1.2v5.5a1.2 1.2 0 0 1-1.2 1.2H3.4a1.2 1.2 0 0 1-1.2-1.2z" />
  </I>
);

export const DocIcon = (p: IconProps) => (
  <I {...p}>
    <path d="M4.4 2.4h4.9L12.6 5.7v6.5a1.4 1.4 0 0 1-1.4 1.4H4.4A1.4 1.4 0 0 1 3 12.2V3.8a1.4 1.4 0 0 1 1.4-1.4z" />
    <path d="M9.1 2.5v3.3h3.4" />
  </I>
);

export const KnowledgeIcon = (p: IconProps) => (
  <I {...p}>
    <path d="M8 2.3 13.2 8 8 13.7 2.8 8z" />
  </I>
);

export const ChevronRight = (p: IconProps) => (
  <I size={12} sw={1.3} {...p}>
    <path d="M6 3.8 10 8l-4 4.2" />
  </I>
);

export const ChevronDown = (p: IconProps) => (
  <I size={12} sw={1.3} {...p}>
    <path d="M3.8 6 8 10l4.2-4" />
  </I>
);

export const SunIcon = (p: IconProps) => (
  <I {...p}>
    <circle cx="8" cy="8" r="2.8" />
    <path d="M8 1.9v1.5M8 12.6v1.5M1.9 8h1.5M12.6 8h1.5M3.7 3.7l1.1 1.1M11.2 11.2l1.1 1.1M12.3 3.7l-1.1 1.1M4.8 11.2l-1.1 1.1" />
  </I>
);

export const MoonIcon = (p: IconProps) => (
  <I {...p}>
    <path d="M12.8 9.8A5.2 5.2 0 0 1 6.2 3.2a5.3 5.3 0 1 0 6.6 6.6z" />
  </I>
);

export const CloseIcon = (p: IconProps) => (
  <I size={14} {...p}>
    <path d="M4.2 4.2 11.8 11.8M11.8 4.2 4.2 11.8" />
  </I>
);

export const ArchiveUpIcon = (p: IconProps) => (
  <I size={14} {...p}>
    <path d="M8 10.8V3.4M5.2 6.1 8 3.3l2.8 2.8" />
    <path d="M3 10.6v1.6a1.2 1.2 0 0 0 1.2 1.2h7.6a1.2 1.2 0 0 0 1.2-1.2v-1.6" />
  </I>
);

export const ArrowLeft = (p: IconProps) => (
  <I {...p}>
    <path d="M13 8H3M7 4 3 8l4 4" />
  </I>
);

export const ArrowRight = (p: IconProps) => (
  <I {...p}>
    <path d="M3 8h10M9 4l4 4-4 4" />
  </I>
);

export const CaseSensitiveIcon = (p: IconProps) => (
  <I {...p} sw={1.25}>
    <path d="M1.9 11.8 4.8 4.2l2.9 7.6M2.8 9.6h4" />
    <path d="M10.1 7.6c.5-.5 1.1-.7 1.9-.7 1.1 0 1.8.6 1.8 1.6v3.3" />
    <path d="M13.8 9.4h-1.5c-1.2 0-2.1.5-2.1 1.3 0 .7.6 1.2 1.4 1.2.9 0 1.7-.4 2.2-1" />
  </I>
);

export const WholeWordIcon = (p: IconProps) => (
  <I {...p} sw={1.25}>
    <path d="M3.4 6.6c.4-.4 1-.6 1.6-.6 1 0 1.6.5 1.6 1.4v3" />
    <path d="M6.6 8.2H5.3c-1.1 0-1.8.4-1.8 1.1 0 .6.5 1.1 1.2 1.1.8 0 1.4-.3 1.9-.9" />
    <path d="M9.1 3.9v6.5" />
    <path d="M9.1 7.1a1.8 1.7 0 1 1 0 2.4" />
    <path d="M1.8 12v1.7h12.4V12" />
  </I>
);

export const FindPrevIcon = (p: IconProps) => (
  <I {...p} sw={1.25}>
    <path d="M8 12.6V3.4M4.4 7 8 3.4 11.6 7" />
  </I>
);

export const FindNextIcon = (p: IconProps) => (
  <I {...p} sw={1.25}>
    <path d="M8 3.4v9.2M4.4 9 8 12.6 11.6 9" />
  </I>
);
