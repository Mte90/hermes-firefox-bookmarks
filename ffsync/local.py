"""Local-mode bookmark source: read ``places.sqlite`` (copy) from a Firefox profile.

Used when Firefox Sync is unavailable, or as an offline test fixture.
Copies the DB + WAL to a temp dir before opening (immutable), so a live
browser never blocks the read.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from .bookmarks import Node

# moz_bookmarks.type integer constants
_MOZ_TYPES = {1: "bookmark", 2: "folder", 3: "separator", 5: "query", 6: "livemark"}


def find_profile_dir() -> Path | None:
    """Best-effort detection of a Firefox profile dir on this host."""
    base = Path.home() / ".mozilla" / "firefox"
    if not base.is_dir():
        return None
    candidates = [p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")]
    for cand in sorted(candidates):
        if (cand / "places.sqlite").exists():
            return cand
    return None


def load_from_places(places: str | Path) -> list[Node]:
    src = Path(places)
    if not src.exists():
        raise FileNotFoundError(f"places.sqlite not found: {src}")
    tmp = Path(tempfile.mkdtemp(prefix="ffb-places-"))
    try:
        dst = tmp / "places.sqlite"
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            extra = src.parent / (src.name + suffix)
            if extra.exists():
                shutil.copy2(extra, tmp / (dst.name + suffix))
        con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT b.guid AS guid, b.title AS title, b.type AS type,
                       p.url AS url, b.parent AS parent, b.dateAdded AS date_added,
 b.lastModified AS date_modified, b.fkIndex AS idx
                FROM moz_bookmarks b LEFT JOIN moz_places p ON b.fk = p.id
                ORDER BY b.fkIndex
                """
            ).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    nodes: list[Node] = []
    for r in rows:
        nodes.append(Node(
            guid=r["guid"] or "",
            type=_MOZ_TYPES.get(int(r["type"] or 0), "unknown"),
            title=r["title"] or "",
            url=r["url"] or "",
            parent=r["parent"] or "",
            date_added=int(r["date_added"] or 0),
            date_modified=int(r["date_modified"] or 0),
            index=int(r["idx"] or 0),
        ))
    return nodes
