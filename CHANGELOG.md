# Changelog

## 1.0.0 — 2026-09-16

- MVP: FxA login (con supporto 2FA/TOTP), firma HAWK, derivazione chiavi (HKDF-SHA256),
  decryption BSO AES-256-CBC/HMAC (libreria `ffsync` completa, Python puro, zero dipendenze da Hermes)
- SQLite + FTS5 cache, with a deterministic schema (bookmark/folder/duplicate/orphan)
- 5 tools: `firefox_bookmarks_sync`, `_search`, `_tree`, `_analyze`, `_recheck`
- Commands: `/bookmarks-sync`, `/bookmarks-report`
- Skill `firefox-bookmarks` (Agent Skills format)
- Local mode: read `places.sqlite` (copy) with no account
- Offline link-check (HEAD, concurrency 4), opt-in
- Tests: 13 offline (crypto round-trip, HMAC, HAWK, parser, cache, handler)
