"""CLI probe + one-time interactive setup.

Usage (run in a CLI session, NOT via gateway chat):
  python -m ffsync.probe setup            # interactive login, stores creds
  python -m ffsync.probe sync             # one sync, prints summary
  python -m ffsync.probe local <profile>  # dump bookmarks from a local profile
  python -m ffsync.probe status           # show credential/cache status

Secrets are read via getpass and never echoed.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "status"

    if cmd == "setup":
        from .auth import FxAAuth, save_credentials, creds_path, load_credentials
        from .storage import SyncStorage
        from .auth import acquire_storage_session
        from .bookmarks import records_to_nodes

        existing = load_credentials(creds_path())
        if existing:
            print(f"Credentials already stored for {existing.email_hint}.")
            if "force" not in sys.argv:
                ans = input("Re-login anyway? [y/N] ").strip().lower()
                if ans != "y":
                    return 0
        email = input("Firefox Account email: ").strip()
        password = getpass.getpass("Password: ")
        totp = input("TOTP code (leave empty if no 2FA): ").strip() or None
        auth = FxAAuth()
        print("Logging in to Firefox Accounts ...")
        creds = auth.login(email, password, totp_code=totp)
        save_credentials(creds, creds_path())
        print(f"OK: logged in as {creds.email_hint}")

        print("Fetching sync collection info ...")
        session = acquire_storage_session(creds)
        storage = SyncStorage(session)
        info = storage.collections_info()
        bm = info.get("bookmarks")
        print(f"Storage endpoint ready. 'bookmarks' collection: "
              f"{'modified ' + str(bm) if bm is not None else 'EMPTY (no bookmarks synced to Firefox Account)'}")
        if bm is not None:
            records = storage.list_records("bookmarks")
            nodes = records_to_nodes(records)
            n_bm = sum(1 for n in nodes if n.is_bookmark)
            print(f"Fetched {len(nodes)} records ({n_bm} bookmarks). "
                  "Run /bookmarks-sync in a Hermes session to load the cache.")
        return 0

    if cmd == "sync":
        from .auth import load_credentials, creds_path, acquire_storage_session
        from .storage import SyncStorage
        from .bookmarks import records_to_nodes
        creds = load_credentials(creds_path())
        if creds is None:
            print("No credentials. Run: python -m ffsync.probe setup")
            return 1
        session = acquire_storage_session(creds)
        storage = SyncStorage(session)
        info = storage.collections_info()
        records = storage.list_records("bookmarks")
        nodes = records_to_nodes(records)
        n_bm = sum(1 for n in nodes if n.is_bookmark)
        print(f"bookmarks collection: {len(nodes)} records, {n_bm} bookmarks "
              f"(last modified: {info.get('bookmarks')})")
        return 0

    if cmd == "local":
        if len(argv) < 2:
            print("Usage: python -m ffsync.probe local <path-to-profile-dir-or-places.sqlite>")
            return 1
        from .local import load_from_places, find_profile_dir
        p = Path(argv[1])
        if p.is_dir():
            p = p / "places.sqlite"
        nodes = load_from_places(p)
        n_bm = sum(1 for n in nodes if n.is_bookmark)
        print(f"Local profile: {len(nodes)} records, {n_bm} bookmarks")
        for n in [x for x in nodes if x.is_bookmark][:10]:
            print(f"  - {n.title or '(untitled)'}  {n.url}")
        return 0

    # status
    from .auth import load_credentials, creds_path
    # compute cache file path without importing the plugin-root cache module (which
    # uses relative imports and is not a bare-importable target from ffsync.probe).
    import os as _os
    _override = _os.environ.get("FFB_CACHE_PATH")
    if _override:
        cache_file = Path(_override)
    else:
        _home = Path(_os.environ.get("FFB_HOME") or _os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        cache_file = _home / "plugin-data" / "firefox-bookmarks" / "cache.sqlite3"
    creds = load_credentials(creds_path())
    print(f"credentials: {'stored (' + creds.email_hint + ')' if creds else 'NOT configured'}")
    cp = cache_file
    if cp.exists():
        import os
        print(f"cache: {cp} ({os.path.getsize(cp)} bytes)")
    else:
        print(f"cache: not created yet ({cp})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
