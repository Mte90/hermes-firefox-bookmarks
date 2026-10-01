"""Tool schemas + handlers for the firefox-bookmarks plugin.

Handlers follow the Hermes plugin contract: ``fn(args: dict, **kw) -> str``
returning ``tool_result(payload)`` or ``tool_error(message)``.
The host context is available as ``kw["ctx"]`` (see registry.dispatch).
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from tools.registry import tool_error, tool_result

from .cache import BookmarksCache

if TYPE_CHECKING:
    from .ffsync.auth import CredentialBundle


# --- schemas ----------------------------------------------------------------

def _s(name: str, description: str, properties: dict, required: list | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


_S = lambda d: {"type": "string", "description": d}
_B = lambda d: {"type": "boolean", "description": d}
_I = lambda d: {"type": "integer", "description": d}

TOOL_SCHEMAS: dict[str, dict] = {
    "firefox_bookmarks_sync": _s(
        "firefox_bookmarks_sync",
        "Sync the user's Firefox bookmarks (cloud Firefox Sync, or local profile) into the local cache. "
        "Run this before search/analyze when the cache is empty or stale, or when the user asks to refresh. "
        "If not configured yet, returns NOT_CONFIGURED (interactive probe setup, or FFB_EMAIL/FFB_PASSWORD env vars).",
        {
            "force": _B("Re-download the full collection even if the server timestamp is unchanged. Default false."),
            "totp_code": _S("Current 6-digit TOTP code, only for the first login when 2FA is enabled: "
                            "ask the user for the live code and retry. Never stored."),
        },
    ),
    "firefox_bookmarks_search": _s(
        "firefox_bookmarks_search",
        "Search the user's Firefox bookmarks by text (title/URL), domain, folder or tag. "
        "Use SHORT topical queries (1-3 keywords), not full sentences.",
        {
            "query": _S("Free-text keywords, e.g. 'systemd cron'."),
            "domain": _S("Restrict to URLs containing this domain/substring, e.g. 'github.com'."),
            "folder": _S("Restrict to folder path containing this text."),
            "tag": _S("Restrict to bookmarks with this tag."),
            "limit": _I("Max results, default 20, max 100."),
        },
    ),
    "firefox_bookmarks_tree": _s(
        "firefox_bookmarks_tree",
        "Show how the bookmark collection is organized: folder paths with bookmark counts.",
        {
            "folder": _S("Only folders whose path contains this text."),
        },
    ),
    "firefox_bookmarks_analyze": _s(
        "firefox_bookmarks_analyze",
        "Analyze the bookmark collection: overview, top domains, duplicates, categories, and (opt-in) dead links. "
        "Dead-link checking contacts the websites — only enable check_links with explicit user consent.",
        {
            "sections": {
                "type": "array", "items": {"type": "string"},
                "description": "Which sections to include: overview, domains, duplicates, categories, dead_links. Default all except dead_links.",
            },
            "use_llm_categorization": _B(
                "Assign/refresh thematic categories using the configured LLM (titles+URLs are sent to it). "
                "Only when the user explicitly asks for thematic grouping. Default false."),
            "check_links": _B("Check up to 200 links for dead/alive status. Contacts external sites. Default false."),
        },
    ),
    "firefox_bookmarks_recheck": _s(
        "firefox_bookmarks_recheck",
        "Re-run dead-link checks for a subset (by domain or folder) without a full sync. Contacts the websites.",
        {
            "domain": _S("Only URLs containing this domain."),
            "folder": _S("Only bookmarks in folders whose path contains this text."),
            "limit": _I("Max URLs to check, default 50, max 200."),
        },
    ),
}


# --- shared helpers ----------------------------------------------------------

_CTX = None  # PluginContext, set by register() in __init__.py


def _set_ctx(ctx):
    global _CTX
    _CTX = ctx


def _cache(kw) -> BookmarksCache:
    return BookmarksCache()


def _hint_stale(stats: dict) -> str:
    if not stats["last_sync"]:
        return "cache is empty — run firefox_bookmarks_sync first"
    if stats["stale"]:
        return "data may be stale (>7 days old) — consider firefox_bookmarks_sync"
    return ""


def _mode_env() -> str:
    import os
    m = (os.environ.get("FFB_MODE") or "").strip().lower()
    return m if m in ("sync", "local") else "sync"


def _env_login(totp_code: str | None = None) -> CredentialBundle | None:
    """Headless login from env vars (FFB_EMAIL, FFB_PASSWORD).

    The TOTP code comes from the tool argument (agent asks the user in chat);
    it is used once, never stored. Returns None if email/password are not
    set; FxAError propagates.
    """
    import os
    from .ffsync.auth import FxAAuth, save_credentials, creds_path

    email = (os.environ.get("FFB_EMAIL") or "").strip()
    password = (os.environ.get("FFB_PASSWORD") or "").strip()
    totp = (totp_code or "").strip() or None

    if not email or not password:
        return None

    creds = FxAAuth().login(email, password, totp_code=totp)
    save_credentials(creds, creds_path())
    return creds


# --- handlers -----------------------------------------------------------------

def handle_sync(args: dict, **kw) -> str:
    force = bool(args.get("force"))
    totp_code = str(args.get("totp_code") or "").strip() or None
    mode = _mode_env()
    cache = _cache(kw)
    stats = cache.stats()
    if not force and stats["last_sync"] and (time.time() - stats["last_sync"]) < 3600:
        cache.close()
        return tool_result({**stats, "skipped": True,
                            "note": "synced within the last hour; use force=true to re-sync"})

    try:
        if mode == "local":
            from .ffsync.local import find_profile_dir, load_from_places
            prof = find_profile_dir()
            if not prof:
                return tool_error("NOT_CONFIGURED: no Firefox profile with places.sqlite found on this host. "
                                  "Set FFB_MODE=sync and configure Firefox Sync, or provide the profile path.")
            nodes = load_from_places(prof / "places.sqlite")
        else:
            from .ffsync.auth import FxAAuth, acquire_storage_session, creds_path, load_credentials
            from .ffsync.storage import SyncStorage
            path = creds_path()
            creds = load_credentials(path)
            if creds is None:
                creds = _env_login(totp_code)
            if creds is None:
                return tool_error("NOT_CONFIGURED: no Firefox Sync credentials. Use interactive setup "
                                  "`python -m ffsync.probe setup` OR set env vars FFB_EMAIL/FFB_PASSWORD "
                                  "for headless/Docker deployments.")
            try:
                session = acquire_storage_session(creds)
            except Exception:
                # try a transparent refresh of the refresh token is not possible without password;
                # report a human error instead
                raise
            storage = SyncStorage(session)
            storage.collections_info()  # validates session + endpoint
            records = storage.list_records("bookmarks")
            from .ffsync.bookmarks import records_to_nodes
            nodes = records_to_nodes(records)
        res = cache.replace(nodes, mode=mode)
        res["hint"] = ""
        cache.close()
        return tool_result(res)
    except Exception as e:
        cache.close()
        msg = str(e)
        code = getattr(e, "code", "ERROR")
        if "NOT_CONFIGURED" in code:
            return tool_error(msg)
        if code == "TOTP_REQUIRED":
            return tool_error("TOTP_REQUIRED: Firefox Account requires 2FA. Ask the user for the current "
                              "6-digit code and retry firefox_bookmarks_sync with totp_code. "
                              "The code is used once and never stored.")
        if code in ("AUTH_EXPIRED",):
            return tool_error("AUTH_EXPIRED: saved Firefox Sync session expired. Log in again "
                              "(python -m ffsync.probe setup, or FFB_EMAIL/FFB_PASSWORD env vars).")
        return tool_error(f"SYNC_FAILED ({code}): {msg}")


def handle_search(args: dict, **kw) -> str:
    cache = _cache(kw)
    query = str(args.get("query") or "").strip() or None
    domain = str(args.get("domain") or "").strip() or None
    folder = str(args.get("folder") or "").strip() or None
    tag = str(args.get("tag") or "").strip() or None
    limit = int(args.get("limit") or 20)
    if not any((query, domain, folder, tag)):
        cache.close()
        return tool_error("Provide at least one of: query, domain, folder, tag")
    results = cache.search(query=query, domain=domain, folder=folder, tag=tag, limit=limit)
    stats = cache.stats()
    cache.close()
    payload = {"results": results, "count": len(results)}
    hint = _hint_stale(stats)
    if hint:
        payload["hint"] = hint
    return tool_result(payload)


def handle_tree(args: dict, **kw) -> str:
    cache = _cache(kw)
    folder = str(args.get("folder") or "").strip() or None
    tree = cache.tree(folder=folder)
    stats = cache.stats()
    cache.close()
    payload = {"tree": tree, "count": len(tree)}
    hint = _hint_stale(stats)
    if hint:
        payload["hint"] = hint
    return tool_result(payload)


def handle_analyze(args: dict, **kw) -> str:
    from .analysis import run_report
    cache = _cache(kw)
    sections = args.get("sections") or ["overview", "domains", "duplicates", "categories"]
    if isinstance(sections, str):
        sections = [s.strip() for s in sections.split(",") if s.strip()]
    use_llm = bool(args.get("use_llm_categorization"))
    check_links = bool(args.get("check_links"))
    report = run_report(cache, sections=sections,
                        use_llm_categorization=use_llm, check_links=check_links)
    if use_llm and "categories" in sections:
        report = _llm_categorize(kw, cache, report)
    stats = cache.stats()
    cache.close()
    if stats["last_sync"]:
        report["last_sync"] = stats["last_sync"]
    else:
        report["hint"] = "cache is empty — run firefox_bookmarks_sync first"
    return tool_result(report)


def _llm_categorize(kw, cache: BookmarksCache, report: dict) -> dict:
    """Batch-categorize uncategorized bookmarks via the host LLM. Cached afterwards."""
    ctx = _CTX
    if ctx is None or getattr(ctx, "llm", None) is None:
        report["categories_note"] = "LLM categorization unavailable in this context; returning cached categories only"
        return report
    rows = cache._con.execute(
        """SELECT n.guid, n.title, n.uri FROM nodes n
           LEFT JOIN categories c ON c.node_guid = n.guid
           WHERE n.deleted=0 AND n.type IN ('bookmark','query','livemark')
             AND n.uri != '' AND c.node_guid IS NULL
           ORDER BY n.date_added DESC LIMIT 250""").fetchall()
    if not rows:
        report["categories_note"] = "all bookmarks already categorized; nothing to do"
        report["categories"] = _cat_counts(cache)
        return report
    items = [{"id": r["guid"], "title": (r["title"] or "")[:120], "url": r["uri"]} for r in rows]
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["id", "category"],
                },
            }
        },
        "required": ["items"],
    }
    now = int(time.time())
    assigned = 0
    try:
        for i in range(0, len(items), 50):
            batch = items[i:i + 50]
            text = ("Categorize each bookmark into exactly one short theme (2-4 words, English), "
                    "chosen from a consistent small taxonomy (e.g. 'programming', 'tools', 'news', "
                    "'docs', 'personal', 'shopping', 'reference', 'other').\n"
                    "Return JSON: {\"items\": [{\"id\": ..., \"category\": ..., \"confidence\": 0..1}]}.\n")
            for it in batch:
                text += f'- id={it["id"]} title="{it["title"]}" url={it["url"]}\n'
            res = ctx.llm.complete_structured(
                instructions="You classify bookmarks into short topic categories. Respond with JSON only.",
                input=[{"type": "text", "text": text}],
                json_schema=schema,
                purpose="bookmarks categorization",
            )
            parsed = getattr(res, "parsed", None)
            if not parsed or "items" not in parsed:
                continue
            for it in parsed["items"]:
                guid = str(it.get("id", ""))
                if guid not in {r["guid"] for r in rows}:
                    continue
                cat = str(it.get("category", "other")).strip().lower()[:40] or "other"
                conf = float(it.get("confidence", 0.5))
                cache._con.execute(
                    "INSERT INTO categories(node_guid, category, confidence, categorized_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(node_guid) DO UPDATE SET "
                    "category=excluded.category, confidence=excluded.confidence, "
                    "categorized_at=excluded.categorized_at",
                    (guid, cat, conf, now))
                assigned += 1
        cache._con.commit()
        report["categories"] = _cat_counts(cache)
        report["categories_assigned"] = assigned
        report["categories_note"] = f"categorized {assigned} bookmarks with the host LLM (results cached)"
    except Exception as e:
        cache._con.commit()
        report["categories_note"] = f"LLM categorization partially failed: {type(e).__name__}: {e}"
        report["categories"] = _cat_counts(cache)
    return report


def _cat_counts(cache: BookmarksCache) -> list[dict]:
    rows = cache._con.execute(
        "SELECT c.category, COUNT(*) n FROM categories c JOIN nodes n ON n.guid=c.node_guid "
        "AND n.deleted=0 GROUP BY c.category ORDER BY n DESC").fetchall()
    return [{"category": r["category"], "count": int(r["n"])} for r in rows]


def handle_recheck(args: dict, **kw) -> str:
    from .linkcheck import check_urls
    cache = _cache(kw)
    domain = str(args.get("domain") or "").strip() or None
    folder = str(args.get("folder") or "").strip() or None
    limit = max(1, min(int(args.get("limit") or 50), 200))
    where = ["deleted=0", "type IN ('bookmark','query','livemark')", "uri LIKE 'http%'"]
    qargs: list = []
    if domain:
        where.append("uri LIKE ?"); qargs.append(f"%{domain}%")
    if folder:
        where.append("folder_path LIKE ?"); qargs.append(f"%{folder}%")
    rows = cache._con.execute(
        f"SELECT guid, uri FROM nodes WHERE {' AND '.join(where)} ORDER BY date_added DESC LIMIT ?",
        (*qargs, limit)).fetchall()
    if not rows:
        cache.close()
        return tool_result({"checked": 0, "alive": 0, "dead": 0, "results": [],
                            "hint": "no matching bookmarks in cache"})
    now = int(time.time())
    results = check_urls([r["uri"] for r in rows], max_workers=8, timeout=10)
    guid_by_url = {r["uri"]: r["guid"] for r in rows}
    for r in results:
        cache._con.execute(
            "INSERT INTO link_status(node_guid,http_status,ok,checked_at) VALUES(?,?,?,?) "
            "ON CONFLICT(node_guid) DO UPDATE SET http_status=excluded.http_status, "
            "ok=excluded.ok, checked_at=excluded.checked_at",
            (guid_by_url.get(r["url"], ""), r["status"], 1 if r["ok"] else 0, now))
    cache._con.commit()
    dead = [r for r in results if not r["ok"]]
    cache.close()
    return tool_result({
        "checked": len(results),
        "alive": len(results) - len(dead),
        "dead": len(dead),
        "results": results[:limit],
    })


# --- slash commands -----------------------------------------------------------

def cmd_bookmarks_sync(raw_args: str) -> str:
    return handle_sync({"force": "force" in raw_args.lower()})


def cmd_bookmarks_report(raw_args: str) -> str:
    return handle_analyze({"sections": ["overview", "domains", "duplicates"],
                           "use_llm_categorization": "categor" in raw_args.lower(),
                           "check_links": "links" in raw_args.lower()})
