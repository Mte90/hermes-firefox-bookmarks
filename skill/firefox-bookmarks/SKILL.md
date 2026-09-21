---
name: firefox-bookmarks
description: >
  Search, sync and analyze the user's Firefox bookmarks (local cache).
  Use when the user asks about their saved links/bookmarks, wants
  a bookmarks report, or asks to refresh the bookmark cache.
---

# Firefox bookmarks (read-only)

## When to use
- The user asks about links they saved: "did I save anything about X?",
  "find the link to Y in my bookmarks".
- The user wants an overview/report of the collection (count, domains,
  duplicates, categories, dead links).
- The user asks to refresh the bookmark cache ("sync my bookmarks").

## Operating rules
- ALWAYS check `last_sync` in tool responses. If the cache is empty, run
  `firefox_bookmarks_sync` first (or tell the user to run `/bookmarks-sync`
  in the CLI session to configure it). If `last_sync` is > 7 days old,
  propose a sync before answering.
- `firefox_bookmarks_search`: use SHORT topical queries (1-3 keywords),
  never full sentences. Combine `domain`/`folder`/`tag` filters to narrow.
- For "organize" requests: run `firefox_bookmarks_tree` first to
  understand the structure, then `firefox_bookmarks_analyze` with categories.
  NEVER claim to have modified anything — v1 is read-only.
- `firefox_bookmarks_analyze`:
  - `use_llm_categorization=true` only when the user explicitly asks for
    thematic grouping (it sends titles+URLs to the configured LLM).
  - `check_links=true` contacts the websites: ONLY with explicit user
    consent. Report the summary, not every URL.
- `firefox_bookmarks_recheck`: same consent rule as `check_links`.
- Credentials and secrets: never echo tokens, account identifiers, or
  credentials into chat. Only the masked `account_hint` (e.g. `d***@gmail.com`)
  may appear.

## Setup (one-time)
If a tool returns `NOT_CONFIGURED`, the user must run the one-time setup in
the CLI session (interactive login with email + password, TOTP code if 2FA).
Instruct them: run `/bookmarks-setup` (or the equivalent CLI command) — do
NOT ask for their password in chat.
