"""Deterministic analysis over the local cache (no LLM, no network)."""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone

from .cache import BookmarksCache, _iso


def overview(cache: BookmarksCache) -> dict:
    s = cache.stats()
    rows = cache._con.execute(
        "SELECT title,uri,date_added FROM nodes WHERE deleted=0 "
        "AND type IN ('bookmark','query','livemark') ORDER BY date_added DESC LIMIT 10").fetchall()
    # folder coverage
    total_bm = s["total_bookmarks"]
    in_folder = cache._con.execute(
        "SELECT COUNT(*) c FROM nodes WHERE deleted=0 AND type IN ('bookmark','query','livemark') "
        "AND folder_path != ''").fetchone()["c"]
    ages = [r["date_added"] for r in cache._con.execute(
        "SELECT date_added FROM nodes WHERE deleted=0 AND type IN ('bookmark','query','livemark') "
        "AND date_added > 0")]
    avg_age_days = None
    if ages:
        now_us = int(time.time()) * 1_000_000
        avg_age_days = round((now_us - sum(ages) / len(ages)) / 1e6 / 86400, 1)
    return {
        "total_bookmarks": total_bm,
        "folders": s["folders"],
        "without_folder": total_bm - in_folder,
        "avg_age_days": avg_age_days,
        "last_sync": _iso(s["last_sync"] * 1_000_000) if s["last_sync"] else "",
        "mode": s["mode"],
        "most_recent": [{"title": r["title"], "url": r["uri"]} for r in rows],
    }


def domains(cache: BookmarksCache, top: int = 20) -> list[dict]:
    top_urls = cache.top_domains(limit=top)
    counter: Counter = Counter()
    for row in top_urls:
        counter[row["domain"]] += row["count"]
    return [{"domain": d, "count": c} for d, c in counter.most_common(top)]


def duplicates(cache: BookmarksCache, limit: int = 20) -> list[dict]:
    dups = cache.duplicates(limit=limit)
    out = []
    for group in dups:
        variants = group["variants"]
        titles = Counter(g["title"] for g in variants)
        out.append({
            "count": group["count"],
            "normalized": variants[0]["url"],
            "variants": variants,
            "same_title": titles.most_common(1)[0][1] == len(variants) if variants else False,
        })
    return out


def categorize_cached(cache: BookmarksCache, limit: int = 200) -> list[dict]:
    """Return existing LLM categories if any (v1 caches them)."""
    rows = cache._con.execute(
        "SELECT c.category, COUNT(*) n FROM categories c "
        "JOIN nodes n ON n.guid=c.node_guid AND n.deleted=0 "
        "GROUP BY c.category ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
    return [{"category": r["category"], "count": r["n"]} for r in rows]


def run_report(cache: BookmarksCache, sections: list[str] | None = None,
               use_llm_categorization: bool = False, check_links: bool = False) -> dict:
    sections = sections or ["overview", "domains", "duplicates", "categories"]
    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}
    if "overview" in sections:
        report["overview"] = overview(cache)
    if "domains" in sections:
        report["domains"] = domains(cache)
    if "duplicates" in sections:
        report["duplicates"] = duplicates(cache)
    if "categories" in sections:
        report["categories"] = categorize_cached(cache)
        if use_llm_categorization:
            # LLM categorization is orchestrated in tools.py (needs ctx.llm);
            # here we only mark that cached categories are returned above.
            report["categories_note"] = "cached categories only; run with LLM pass to assign new ones"
    if check_links:
        from .linkcheck import check_urls
        rows = cache._con.execute(
            "SELECT guid,uri FROM nodes WHERE deleted=0 AND type IN ('bookmark','query','livemark') "
            "AND uri LIKE 'http%' LIMIT 200").fetchall()
        res = check_urls([r["uri"] for r in rows], max_workers=8, timeout=10)
        dead = [r for r in res if not r["ok"]]
        report["dead_links"] = dead[:50]
        report["linkcheck_summary"] = {
            "checked": len(res),
            "alive": sum(1 for r in res if r["ok"]),
            "dead": len(dead),
        }
        cache._con.executemany(
            "INSERT INTO link_status(node_guid,http_status,ok,checked_at) VALUES(?,?,?,?) "
            "ON CONFLICT(node_guid) DO UPDATE SET http_status=excluded.http_status,"
            "ok=excluded.ok,checked_at=excluded.checked_at",
            [(r["guid"], r["status"], 1 if r["ok"] else 0, int(time.time())) for r in res])
        cache._con.commit()
    return report
