import hashlib
import re
from typing import Any


WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|([^\]]+))?\]\]")
TAG_INLINE_RE = re.compile(r"(?<![&\w`/])#([a-zA-Z][\w/-]{1,48})")
HEADING_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SRC_REF_RE = re.compile(r"\b(src-[a-z0-9]{8,32})\b")
KP_REF_RE = re.compile(r"\b(kp-[a-z0-9-]{4,64})\b")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Lightweight, zero-dependency YAML frontmatter parser supporting scalars,
    quoted strings, booleans, inline `[a, b]` arrays, and multiline `- item` lists."""
    if not raw.startswith("---\n") and not raw.startswith("---\r\n"):
        return {}, raw
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    end_idx = -1
    for i in range(1, min(len(lines), 160)):
        if lines[i].strip() in ("---", "..."):
            end_idx = i
            break
    if end_idx < 0:
        return {}, raw

    fm: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in lines[1:end_idx]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_list_key:
            item_val = _unquote(stripped[2:].strip())
            existing = fm.get(current_list_key)
            if isinstance(existing, list):
                existing.append(item_val)
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            key, val_raw = line.split(":", 1)
            key = key.strip()
            val = val_raw.strip()
            current_list_key = None
            if val == "":
                fm[key] = []
                current_list_key = key
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                if not inner:
                    fm[key] = []
                else:
                    fm[key] = [_unquote(part.strip()) for part in inner.split(",") if part.strip()]
            elif val.lower() in ("true", "yes"):
                fm[key] = True
            elif val.lower() in ("false", "no"):
                fm[key] = False
            else:
                fm[key] = _unquote(val)

    body = "\n".join(lines[end_idx + 1 :]).lstrip("\r\n")
    return fm, body


def serialize_frontmatter(fm: dict[str, Any], body: str) -> str:
    if not fm:
        return body
    out = ["---"]
    for k, v in fm.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, list):
            if not v:
                out.append(f"{k}: []")
            else:
                quoted = ", ".join(_quote_scalar(str(item)) for item in v)
                out.append(f"{k}: [{quoted}]")
        else:
            out.append(f"{k}: {_quote_scalar(str(v))}")
    out.append("---")
    out.append("")
    out.append(body)
    return "\n".join(out)


def extract_title(fm: dict[str, Any], body: str, fallback: str) -> str:
    for key in ("title", "name"):
        val = fm.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    m = HEADING_H1_RE.search(body)
    if m:
        return m.group(1).strip()
    return fallback


def extract_wikilinks(markdown_text: str) -> list[str]:
    cleaned = _strip_code_regions(markdown_text)
    seen: list[str] = []
    for match in WIKILINK_RE.finditer(cleaned):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def extract_tags(fm: dict[str, Any], body: str) -> list[str]:
    tags: list[str] = []
    fm_tags = fm.get("tags")
    if isinstance(fm_tags, list):
        for t in fm_tags:
            ts = str(t).strip().lstrip("#")
            if ts and ts not in tags:
                tags.append(ts)
    elif isinstance(fm_tags, str) and fm_tags.strip():
        for part in re.split(r"[,\s]+", fm_tags.strip()):
            ts = part.strip().lstrip("#")
            if ts and ts not in tags:
                tags.append(ts)
    cleaned = _strip_code_regions(body)
    for m in TAG_INLINE_RE.finditer(cleaned):
        t = m.group(1)
        if t not in tags:
            tags.append(t)
    return tags


def extract_hindsight_refs(text: str) -> tuple[list[str], list[str]]:
    sources = list(dict.fromkeys(SRC_REF_RE.findall(text)))
    pages = list(dict.fromkeys(KP_REF_RE.findall(text)))
    return sources, pages


def strip_provenance_headers(raw_text: str) -> tuple[dict[str, str], str]:
    lines = raw_text.splitlines()
    meta: dict[str, str] = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if line.startswith("[PROVENANCE ") and line.endswith("]"):
            inner = line[len("[PROVENANCE ") : -1]
            for token in inner.split():
                if "=" in token:
                    k, v = token.split("=", 1)
                    meta[k.strip()] = v.strip()
            idx += 1
        elif line.startswith("[ATTRIBUTION") and line.endswith("]"):
            meta["attribution"] = line[1:-1]
            idx += 1
        elif meta and line == "":
            idx += 1
            break
        else:
            break
    return meta, "\n".join(lines[idx:])


def _strip_code_regions(text: str) -> str:
    without_fences = re.sub(r"```[\s\S]*?```", " ", text)
    return re.sub(r"`[^`\n]+`", " ", without_fences)


def _unquote(val: str) -> str:
    if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
        inner = val[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return val


def _quote_scalar(val: str) -> str:
    if any(ch in val for ch in (":", "#", "[", "]", "{", "}", ",", '"', "\n")) or val.strip() != val:
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return val
