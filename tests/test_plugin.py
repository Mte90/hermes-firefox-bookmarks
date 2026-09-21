"""Offline tests for the firefox-bookmarks plugin (no network, no account).

Run:  cd <plugin dir> && /opt/hermes/.venv/bin/python -m pytest tests -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import importlib.util
import types

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# force a sandboxed data dir BEFORE any cache import
_tmp = tempfile.mkdtemp(prefix="ffb-test-")
os.environ["FFB_HOME"] = _tmp
os.environ.pop("FFB_CACHE_PATH", None)


def _load_plugin_pkg():
    """Load the plugin the same way the Hermes loader does (top-level package)."""
    name = "firefox_bookmarks"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_pkg = _load_plugin_pkg()

import firefox_bookmarks.ffsync.crypto as crypto  # noqa: E402
from firefox_bookmarks.ffsync.bookmarks import (  # noqa: E402
    records_to_nodes, build_folder_paths, normalize_url, Node)
from firefox_bookmarks.ffsync.local import load_from_places  # noqa: E402
from firefox_bookmarks.ffsync.auth import mask_email  # noqa: E402
import firefox_bookmarks.cache as cache_mod  # noqa: E402
import firefox_bookmarks.tools as tools  # noqa: E402

BookmarksCache = cache_mod.BookmarksCache
cache_path = cache_mod.cache_path
_fts_query = cache_mod._fts_query


# --- crypto -----------------------------------------------------------------

def test_key_bundle_deterministic():
    master = os.urandom(32)
    k1 = crypto.key_bundle_from_master(master, "identity.mozilla.com/picl/v1/oldsync")
    k2 = crypto.key_bundle_from_master(master, "identity.mozilla.com/picl/v1/oldsync")
    assert k1 == k2
    assert len(k1[0]) == 32 and len(k1[1]) == 32


def test_encrypt_decrypt_roundtrip():
    enc, mac = crypto.key_bundle_from_master(os.urandom(32), "oldsync")
    plain = json.dumps({"hello": "world", "n": 42}).encode()
    bso = crypto.encrypt_payload(plain, enc, mac)
    out = crypto.decrypt_payload(bso["ciphertext"], bso["iv"], bso["hmac"], enc, mac)
    assert json.loads(out) == {"hello": "world", "n": 42}


def test_decrypt_bad_hmac_raises():
    enc, mac = crypto.key_bundle_from_master(os.urandom(32), "oldsync")
    bso = crypto.encrypt_payload(b"x" * 16, enc, mac)
    bad = dict(bso, hmac="00" * 32)
    with pytest.raises(ValueError):
        crypto.decrypt_payload(bad["ciphertext"], bad["iv"], bad["hmac"], enc, mac)


def test_unbundle_roundtrip():
    import hashlib as _h
    from ffsync.crypto import derive_key, xor_bytes
    bundle_key = os.urandom(32)
    plaintext = b"ka" * 16 + b"kb" * 16
    # emulate the FxA bundle: km = HKDF(bundle_key, "account/keys", 32+len)
    namespace = "account/keys"
    km = crypto.derive_key(bundle_key, namespace, 32 + len(plaintext))
    ct = xor_bytes(km[32:], plaintext)
    sig = crypto._hmac_sha256(km[:32], ct)
    bundle = ct + sig
    out = crypto.unbundle(namespace, bundle_key, bundle)
    assert out == plaintext


def test_hawk_header_shape():
    hdr = crypto.hawk_authorization("abc123", os.urandom(32), "GET",
                                    "https://sync-2.mozilla.org/1.5/uid/storage/bookmarks")
    assert hdr.startswith("Hawk id=\"abc123\"")
    assert 'mac="' in hdr and "ts=\"" in hdr and "nonce=\"" in hdr


def test_stretch_password_known_length():
    out = crypto.stretch_password("a@b.com", "pw")
    assert len(out) == 32


def test_mask_email():
    from ffsync.auth import mask_email
    assert mask_email("daniele.mte90@example.com").endswith("@example")
    assert "daniele" not in mask_email("daniele.mte90@example.com")


# --- bookmarks parser --------------------------------------------------------

RECORDS = [
    {"id": "root", "type": "folder", "title": "Menu", "children": ["a"], "parent": ""},
    {"id": "a", "type": "folder", "title": "Work", "children": ["b", "c"], "parent": "root"},
    {"id": "b", "type": "bookmark", "title": "Systemd man",
     "url": "https://www.freedesktop.org/software/systemd/man/systemd.timer.html",
     "tags": "linux,docs", "parent": "a", "dateAdded": 1700000000000000},
    {"id": "c", "type": "bookmark", "title": "GitHub",
     "url": "https://github.com/?utm_source=x", "tags": "dev", "parent": "a"},
    {"id": "d", "type": "separator", "parent": "a"},
]


def test_records_to_nodes_and_paths():
    nodes = records_to_nodes(RECORDS)
    assert len(nodes) == 5
    paths = build_folder_paths(nodes)
    assert paths["b"] == "Menu/Work"
    b = next(n for n in nodes if n.guid == "b")
    assert b.is_bookmark and b.tags == ["linux", "docs"]


def test_normalize_url_dedupe():
    a = "https://example.com/page/?utm_source=x&id=1"
    b = "https://example.com/page"
    # utm stripped; id kept -> different; but trailing slash stripped
    assert normalize_url("https://example.com/page/") == "https://example.com/page"
    na = normalize_url(a)
    nb = normalize_url("https://example.com/page/?id=1")
    assert na == nb


# --- local mode ----------------------------------------------------------------

def _make_places(tmp: Path) -> Path:
    import sqlite3
    con = sqlite3.connect(tmp / "places.sqlite")
    con.executescript("""
    CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT);
    CREATE TABLE moz_bookmarks (
      guid TEXT PRIMARY KEY, fk INTEGER, title TEXT, type INTEGER,
      parent TEXT, dateAdded INTEGER, lastModified INTEGER, fkIndex INTEGER);
    """)
    con.executemany("INSERT INTO moz_places VALUES(?,?)",
                    [(1, "https://a.example/1"), (2, "https://b.example/2")])
    con.executemany(
        "INSERT INTO moz_bookmarks VALUES(?,?,?,?,?,?,?,?)",
        [("g1", 1, "A page", 1, "root", 1700000000000000, 0, 0),
         ("g2", 2, "B page", 1, "root", 1700000000000001, 0, 1),
         ("root", None, "Bookmarks Menu", 2, None, 0, 0, -1)])
    con.commit()
    con.close()
    return tmp / "places.sqlite"


def test_local_places(tmp_path):
    places = _make_places(tmp_path)
    nodes = load_from_places(places)
    bms = [n for n in nodes if n.is_bookmark]
    assert len(bms) == 2
    assert {n.url for n in bms} == {"https://a.example/1", "https://b.example/2"}


# --- cache ----------------------------------------------------------------------

def _sample_nodes():
    return records_to_nodes(RECORDS)


def test_cache_replace_search_tree_dupes():
    with tempfile.TemporaryDirectory() as td:
        cache = BookmarksCache(Path(td) / "c.sqlite3")
        res = cache.replace(_sample_nodes(), mode="test")
        assert res["total_bookmarks"] == 2

        rows = cache.search(query="systemd")
        assert len(rows) == 1 and "freedesktop" in rows[0]["url"]

        rows = cache.search(query="github", domain="github.com")
        assert len(rows) == 1

        tree = cache.tree()
        assert any("Work" in t["folder_path"] for t in tree)
        work = next(t for t in tree if "Work" in t["folder_path"])
        assert work["bookmark_count"] == 2

        dupes = cache.duplicates()
        assert dupes == []

        # duplicate via identical normalized url
        from ffsync.bookmarks import Node
        extra = Node(guid="e1", type="bookmark", title="dup",
                     url="https://example.com/page/", parent="a")
        nodes = _sample_nodes() + [extra]
        extra2 = Node(guid="e2", type="bookmark", title="dup2",
                      url="https://example.com/page", parent="a")
        nodes.append(extra2)
        cache.replace(nodes, mode="test")
        dupes = cache.duplicates()
        assert len(dupes) == 1 and dupes[0]["count"] == 2

        s = cache.stats()
        assert s["total_bookmarks"] == 4
        cache.close()


def test_cache_fts_fallback():
    with tempfile.TemporaryDirectory() as td:
        cache = BookmarksCache(Path(td) / "c2.sqlite3")
        cache.replace(_sample_nodes(), mode="test")
        # query with weird chars that would break a naive FTS
        rows = cache.search(query='systemd "cron"')
        assert isinstance(rows, list)
        cache.close()


# --- tools handlers --------------------------------------------------------------

def test_handlers_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("FFB_CACHE_PATH", str(tmp_path / "h.sqlite3"))
    c = BookmarksCache()
    c.replace(_sample_nodes(), mode="test")
    c.close()

    out = json.loads(tools.handle_search({"query": "systemd"}))
    assert "error" not in out
    assert out["count"] >= 1

    out = json.loads(tools.handle_tree({}))
    assert out["count"] >= 1

    out = json.loads(tools.handle_analyze({"sections": ["overview", "domains", "duplicates"]}))
    assert out["overview"]["total_bookmarks"] == 2

    # sync with no credentials -> NOT_CONFIGURED (force skips the freshness short-circuit)
    out = json.loads(tools.handle_sync({"force": True}))
    assert "NOT_CONFIGURED" in out.get("error", "")

    # search with no filters
    out = json.loads(tools.handle_search({}))
    assert "error" in out
