"""Parse decrypted bookmark records into a flat node list + folder paths.

Sync ``bookmarks`` collection records carry:
  id, type, title, url, tags (csv), children (csv of guids), parent (guid),
  dateAdded / dateModified (microseconds epoch), annot, index.

We build a guid -> node map, resolve parent chains into ``folder_path``,
and normalize URLs for duplicate detection.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote


@dataclass
class Node:
    guid: str
    type: str = ""               # bookmark | folder | query | livemark | separator
    title: str = ""
    url: str = ""
    tags: list[str] = field(default_factory=list)
    parent: str = ""
    children: list[str] = field(default_factory=list)
    date_added: int = 0          # microseconds epoch
    date_modified: int = 0
    index: int = 0

    @property
    def is_bookmark(self) -> bool:
        return self.type in ("bookmark", "query", "livemark") and bool(self.url)


def _csv(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def records_to_nodes(records: list[dict]) -> list[Node]:
    nodes: list[Node] = []
    for rec in records:
        guid = str(rec.get("id") or rec.get("_id") or "").strip()
        if not guid:
            continue
        nodes.append(Node(
            guid=guid,
            type=str(rec.get("type", "")),
            title=str(rec.get("title", "") or ""),
            url=str(rec.get("url", "") or ""),
            tags=_csv(rec.get("tags")),
            parent=str(rec.get("parent", "") or ""),
            children=_csv(rec.get("children")),
            date_added=int(rec.get("dateAdded", 0) or 0),
            date_modified=int(rec.get("dateModified", 0) or 0),
            index=int(rec.get("index", 0) or 0),
        ))
    return nodes


def build_folder_paths(nodes: list[Node]) -> dict[str, str]:
    """Return {guid: 'Root/Parent/...'} walking the parent chain (cycle-safe)."""
    by_guid = {n.guid: n for n in nodes}
    paths: dict[str, str] = {}

    def walk(guid: str) -> str:
        seen: set[str] = set()
        parts: list[str] = []
        cur = guid
        while cur and cur not in by_guid:
            break
        # walk up
        cur = guid
        while cur:
            if cur in seen:
                break
            seen.add(cur)
            n = by_guid.get(cur)
            if n is None:
                break
            if n.type == "folder" and n.title:
                parts.append(n.title)
            cur = n.parent
        parts.reverse()
        return "/".join(parts)

    for n in nodes:
        paths[n.guid] = walk(n.guid)
    return paths


def normalize_url(url: str) -> str:
    """Strip tracking params + trailing slash + lowercase host for dedupe."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = {k.lower(): v for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid", "mc_"))}
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                           parts.path.rstrip("/"), urlencode(query), ""))
    except ValueError:
        return url
