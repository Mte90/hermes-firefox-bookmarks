"""ffsync — Firefox Sync 1.5 data layer, independent of Hermes.

Pure-Python client for reading the ``bookmarks`` collection from Firefox
Sync (cloud) or from a local ``places.sqlite``. No Hermes imports.

Reference implementation: github.com/Mikescher/firefox-sync-client (Go),
cross-checked against the Mozilla SyncStorage 1.5 spec.
"""
from __future__ import annotations

__version__ = "1.0.0"

# --- Mozilla endpoints / OAuth (read-only) --------------------------------
FxA_AUTH_URL = "https://api.accounts.firefox.com/v1"
TOKEN_SERVER_URL = "https://token.services.mozilla.com"
OAUTH_CLIENT_ID = "e7ce535d93522896"
OAUTH_SCOPE = "https://identity.mozilla.com/apps/oldsync"

PICL_PREFIX = "identity.mozilla.com/picl/v1/"
COLL_BOOKMARKS = "bookmarks"
COLL_CRYPTO = "crypto"
RECORD_CRYPTO_KEYS = "keys"
USER_AGENT = "hermes-plugin-firefox-bookmarks/1.0 (read-only)"
