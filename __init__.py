"""Hermes plugin — Firefox bookmarks (read-only).

Read, search and analyze the user's Firefox bookmarks via Firefox Sync
(or local ``places.sqlite``). Cache-first: all queries hit the local SQLite
cache; the Mozilla contact only happens during sync.
"""
from __future__ import annotations

import json
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent


def register(ctx) -> None:
    from .tools import (
        TOOL_SCHEMAS, _set_ctx,
        handle_sync, handle_search, handle_tree, handle_analyze, handle_recheck,
        cmd_bookmarks_sync, cmd_bookmarks_report,
    )

    _set_ctx(ctx)

    handlers = {
        "firefox_bookmarks_sync": handle_sync,
        "firefox_bookmarks_search": handle_search,
        "firefox_bookmarks_tree": handle_tree,
        "firefox_bookmarks_analyze": handle_analyze,
        "firefox_bookmarks_recheck": handle_recheck,
    }
    for name, handler in handlers.items():
        ctx.register_tool(
            name=name,
            toolset="firefox_bookmarks",
            schema=TOOL_SCHEMAS[name],
            handler=handler,
            emoji="🔖",
        )

    ctx.register_skill(
        "firefox-bookmarks",
        _PLUGIN_DIR / "skill" / "firefox-bookmarks" / "SKILL.md",
        description="Search, sync and analyze the user's Firefox bookmarks (local cache).",
    )

    ctx.register_command(
        "bookmarks-sync",
        cmd_bookmarks_sync,
        description="Sync Firefox bookmarks into the local cache (pass force to force a refresh)",
        args_hint="[force]",
    )
    ctx.register_command(
        "bookmarks-report",
        cmd_bookmarks_report,
        description="Bookmarks analysis report (overview/domains/duplicates)",
        args_hint="[categorize] [links]",
    )
