"""SQLite cache for Firefox bookmarks (cache-first design).

Single file under the Hermes profile:
  <HERMES_HOME>/plugin-data/firefox-bookmarks/cache.sqlite3
(override with env FFB_CACHE_PATH).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ffsync.bookmarks import Node, build_folder_paths, normalize_url

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
  guid TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  title TEXT,
  uri TEXT,
  parent_guid TEXT,
  folder_path TEXT,
  tags TEXT,
  date_added INTEGER,
  date_modified INTEGER,
  position INTEGER,
  deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nodes_uri    ON nodes(uri);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_guid);
CREATE INDEX IF NOT EXISTS idx_nodes_type   ON nodes(type);
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
  title, uri, folder_path, tags, content='nodes', content_rowid='rowid'
);
CREATE TABLE IF NOT EXISTS categories (
  node_guid TEXT PRIMARY KEY,
  category TEXT,
  confidence REAL,
  categorized_at INTEGER
);
CREATE TABLE IF NOT EXISTS link_status (
  node_guid TEXT PRIMARY KEY,
  http_status INTEGER,
  ok INTEGER,
  checked_at INTEGER
);
"""

BOOKMARK_TYPES = ("bookmark", "query", "livemark")


def cache_path(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("FFB_CACHE_PATH")
    if env:
        return Path(env)
    home = Path(os.environ.get("FFB_HOME") or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / "plugin-data" / "firefox-bookmarks" / "cache.sqlite3"


class BookmarksCache:
    def __init__(self, path: str | Path | None = None):
        self.path = cache_path(str(path)) if path is not None else cache_path(None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.path))
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        if self._get_meta("schema_version") != str(SCHEMA_VERSION):
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            self._con.commit()

    # -- meta -------------------------------------------------------------

    def _get_meta(self, key: str) -> str | None:
        row = self._con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- ingest -----------------------------------------------------------

    def replace(self, nodes: list[Node], mode: str) -> dict:
        """Replace the whole node set (full sync). Rebuilds FTS atomically."""
        t0 = time.time()
        paths = build_folder_paths(nodes)
        now = int(time.time())
        con = self._con
        with con:
            con.execute("DELETE FROM nodes")
            con.execute("DELETE FROM nodes_fts")
            con.executemany(
                """INSERT INTO nodes(guid,type,title,uri,parent_guid,folder_path,tags,
                       date_added,date_modified,position)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [
                    (n.guid, n.type, n.title, n.url, n.parent,
                     paths.get(n.guid, ""), json.dumps(n.tags),
                     n.date_added, n.date_modified, n.index)
                    for n in nodes
                ],
            )
            con.execute(
                "INSERT INTO nodes_fts(rowid,title,uri,folder_path,tags) "
                "SELECT rowid,title,uri,folder_path,tags FROM nodes")
            self._set_meta("last_sync", str(now))
            self._set_meta("mode", mode)
            self._set_meta("total", str(len(nodes)))
        n_bm = con.execute(
            "SELECT COUNT(*) c FROM nodes WHERE type IN (%s)" %
            ",".join("?" * len(BOOKMARK_TYPES)), BOOKMARK_TYPES).fetchone()["c"]
        return {
            "success": True, "added": len(nodes), "removed": 0,
            "total_bookmarks": n_bm, "total_nodes": len(nodes),
            "last_sync": now, "mode": mode,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    def stats(self) -> dict:
        total = int(self._get_meta("total") or 0)
        last = int(self._get_meta("last_sync") or 0)
        bookmarks = int(self._con.execute(
            "SELECT COUNT(*) c FROM nodes WHERE type IN (%s)" %
            ",".join("?" * len(BOOKMARK_TYPES)), BOOKMARK_TYPES).fetchone()["c"])
        folders = int(self._con.execute(
            "SELECT COUNT(*) c FROM nodes WHERE type='folder'").fetchone()["c"])
        return {
            "total_bookmarks": bookmarks, "total_nodes": total, "folders": folders,
            "last_sync": last, "mode": self._get_meta("mode"),
            "stale": (time.time() - last) > 7 * 86400 if last else None,
        }

    # -- queries ------------------------------------------------------------

    def search(self, query: str | None = None, domain: str | None = None,
               folder: str | None = None, tag: str | None = None,
               limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        where: list[str] = ["deleted=0", f"type IN ({','.join('?'*len(BOOKMARK_TYPES))})"]
        args: list[Any] = list(BOOKMARK_TYPES)
        use_fts = bool(query)
        if query:
            where.append("nodes_fts MATCH ?")
            args.append(_fts_query(query))
        if domain:
            where.append("uri LIKE ?"); args.append(f"%{domain}%")
        if folder:
            where.append("folder_path LIKE ?"); args.append(f"%{folder}%")
        if tag:
            where.append("tags LIKE ?"); args.append(f'%"{tag}"%')
        join = "JOIN nodes_fts ON nodes_fts.rowid=nodes.rowid " if use_fts else ""
        sql = (f"SELECT guid,title,uri,folder_path,tags,date_added FROM nodes {join}"
               f"WHERE {' AND '.join(where)} ORDER BY date_added DESC LIMIT ?")
        args.append(limit)
        try:
            rows = self._con.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            rows = self._like_search(query, domain, folder, tag, limit)
        results = []
        for r in rows:
            try:
                tags = json.loads(r["tags"] or "[]")
            except json.JSONDecodeError:
                tags = []
            results.append({
                "id": r["guid"], "title": r["title"], "url": r["uri"],
                "folder_path": r["folder_path"], "tags": tags,
                "added": _iso(r["date_added"]),
            })
        return results

    def _like_search(self, query, domain, folder, tag, limit) -> list:
        where = ["deleted=0", f"type IN ({','.join('?'*len(BOOKMARK_TYPES))})"]
        args: list[Any] = list(BOOKMARK_TYPES)
        if query:
            for tok in query.split():
                where.append("(title LIKE ? OR uri LIKE ?)")
                like = f"%{tok}%"
                args += [like, like]
        if domain:
            where.append("uri LIKE ?"); args.append(f"%{domain}%")
        if folder:
            where.append("folder_path LIKE ?"); args.append(f"%{folder}%")
        if tag:
            where.append("tags LIKE ?"); args.append(f'%"{tag}"%')
        sql = ("SELECT guid,title,uri,folder_path,tags,date_added FROM nodes "
               f"WHERE {' AND '.join(where)} ORDER BY date_added DESC LIMIT ?")
        args.append(limit)
        return self._con.execute(sql, args).fetchall()

    def tree(self, folder: str | None = None, depth: int = 2,
             include_counts: bool = True) -> list[dict]:
        """Flat list of folder paths with direct bookmark counts (most useful for the model)."""
        rows = self._con.execute(
            f"SELECT folder_path fp, COUNT(*) c FROM nodes WHERE deleted=0 "
            f"AND type IN ({','.join('?'*len(BOOKMARK_TYPES))}) GROUP BY folder_path",
            BOOKMARK_TYPES).fetchall()
        # also include empty folders
        folders = self._con.execute(
            "SELECT folder_path FROM nodes WHERE deleted=0 AND type='folder'").fetchall()
        seen: dict[str, int] = {r["fp"] or "(no folder)": int(r["c"]) for r in rows}
        for r in folders:
            seen.setdefault(r["folder_path"] or "(no folder)", 0)
        out = []
        for path, cnt in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0])):
            if folder and folder.lower() not in path.lower():
                continue
            out.append({"folder_path": path, "bookmark_count": cnt})
        return out

    def top_domains(self, limit: int = 20) -> list[dict]:
        rows = self._con.execute(
            f"""SELECT uri, COUNT(*) c FROM nodes WHERE deleted=0
                AND type IN ({','.join('?'*len(BOOKMARK_TYPES))}) AND uri != ''
                GROUP BY uri ORDER BY c DESC LIMIT ?""",
            (*BOOKMARK_TYPES, limit)).fetchall()
        out = []
        for r in rows:
            try:
                host = r["uri"].split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            except IndexError:
                host = r["uri"]
            out.append({"domain": host, "url": r["uri"], "count": int(r["c"])})
        return out

    def duplicates(self, limit: int = 50) -> list[dict]:
        """Groups of bookmarks sharing the same normalized URL: [{'count','variants':[...]}]."""
        rows = self._con.execute(
            f"SELECT uri, title FROM nodes WHERE deleted=0 "
            f"AND type IN ({','.join('?'*len(BOOKMARK_TYPES))}) AND uri != ''",
            BOOKMARK_TYPES).fetchall()
        by_norm: dict[str, list[dict]] = {}
        for r in rows:
            by_norm.setdefault(normalize_url(r["uri"]), []).append(
                {"url": r["uri"], "title": r["title"]})
        dups = [{"count": len(v), "variants": v} for v in by_norm.values() if len(v) > 1]
        dups.sort(key=lambda d: d["count"], reverse=True)
        return dups[:limit]

    def close(self) -> None:
        self._con.close()


def _fts_query(q: str) -> str:
    toks = [t for t in q.replace('"', " ").split() if t]
    if not toks:
        return '""'
    return " OR ".join(f'"{t}"' for t in toks)


def _iso(micros: int | None) -> str:
    if not micros:
        return ""
    secs = micros / 1_000_000 if micros > 10_000_000_000 else micros / 1000.0
    try:
        return datetime.fromtimestamp(secs, timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""
