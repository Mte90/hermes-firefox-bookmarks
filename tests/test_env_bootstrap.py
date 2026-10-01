"""Tests for headless Docker deployment via environment variables.

Run:  cd <plugin dir> && python -m pytest tests/test_env_bootstrap.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# force a sandboxed data dir BEFORE any cache import
_tmp = tempfile.mkdtemp(prefix="ffb-test-env-")
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

from firefox_bookmarks.ffsync.auth import (  # noqa: E402
    CredentialBundle, creds_path,
)
import firefox_bookmarks.tools as tools  # noqa: E402

_AUTH = "firefox_bookmarks.ffsync.auth.FxAAuth"
_SESSION = "firefox_bookmarks.ffsync.auth.acquire_storage_session"
_STORAGE = "firefox_bookmarks.ffsync.storage.SyncStorage"


def _mock_creds() -> CredentialBundle:
    return CredentialBundle(
        user_id="uid123",
        key_a=b"ka" * 16,
        key_b=b"kb" * 16,
        refresh_token="refresh123",
        key_id="keyid123",
        email_hint="t***@example.com",
    )


def _env(monkeypatch, tmp_path, email="test@example.com",
         password="securepassword123"):
    monkeypatch.setenv("FFB_HOME", str(tmp_path))
    monkeypatch.setenv("FFB_CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    monkeypatch.setenv("FFB_EMAIL", email)
    monkeypatch.setenv("FFB_PASSWORD", password)


def _storage_mock():
    storage = MagicMock()
    storage.collections_info.return_value = {"bookmarks": 123.0}
    storage.list_records.return_value = []
    return storage


# --- env login tests ---------------------------------------------------------

def test_env_login_success_no_totp(monkeypatch, tmp_path):
    """no creds + FFB_EMAIL/FFB_PASSWORD set → login called, creds saved, sync proceeds."""
    _env(monkeypatch, tmp_path)
    creds = _mock_creds()

    with patch(_AUTH) as mock_auth_cls, \
            patch(_SESSION), patch(_STORAGE) as mock_storage_cls:
        mock_auth = MagicMock()
        mock_auth.login.return_value = creds
        mock_auth_cls.return_value = mock_auth
        mock_storage_cls.return_value = _storage_mock()

        out = json.loads(tools.handle_sync({"force": True}))

    mock_auth.login.assert_called_once_with(
        "test@example.com", "securepassword123", totp_code=None)
    assert "error" not in out
    assert out["mode"] == "sync"
    assert creds_path().exists()  # persisted for later headless restarts


def test_env_login_missing_email(monkeypatch, tmp_path):
    """no email → NOT_CONFIGURED, login never attempted."""
    _env(monkeypatch, tmp_path, email="")

    with patch(_AUTH) as mock_auth_cls:
        out = json.loads(tools.handle_sync({"force": True}))

    mock_auth_cls.assert_not_called()
    assert "NOT_CONFIGURED" in out["error"]
    assert "FFB_EMAIL" in out["error"]


def test_env_login_missing_password(monkeypatch, tmp_path):
    """email set but no password → NOT_CONFIGURED, login never attempted."""
    _env(monkeypatch, tmp_path, password="")

    with patch(_AUTH) as mock_auth_cls:
        out = json.loads(tools.handle_sync({"force": True}))

    mock_auth_cls.assert_not_called()
    assert "NOT_CONFIGURED" in out["error"]


def test_existing_creds_wins_over_env(monkeypatch, tmp_path):
    """creds.json already present → env vars ignored, no login attempted."""
    from firefox_bookmarks.ffsync.auth import save_credentials

    _env(monkeypatch, tmp_path, email="env@example.com", password="envpassword")
    save_credentials(_mock_creds(), creds_path())  # after FFB_HOME is sandboxed

    with patch(_AUTH) as mock_auth_cls, \
            patch(_SESSION), patch(_STORAGE) as mock_storage_cls:
        mock_storage_cls.return_value = _storage_mock()

        out = json.loads(tools.handle_sync({"force": True}))

    mock_auth_cls.assert_not_called()
    assert "error" not in out


def test_env_login_whitespace_vars(monkeypatch, tmp_path):
    """whitespace-only FFB_EMAIL/FFB_PASSWORD → treated as missing."""
    _env(monkeypatch, tmp_path, email="   ", password="")

    with patch(_AUTH) as mock_auth_cls:
        out = json.loads(tools.handle_sync({"force": True}))

    mock_auth_cls.assert_not_called()
    assert "NOT_CONFIGURED" in out["error"]


def test_totp_from_tool_arg(monkeypatch, tmp_path):
    """totp_code tool argument is used on the first login with 2FA."""
    _env(monkeypatch, tmp_path)
    with patch(_AUTH) as mock_auth_cls, patch(_SESSION), patch(_STORAGE) as mstore:
        mock_auth = MagicMock()
        mock_auth.login.return_value = _mock_creds()
        mock_auth_cls.return_value = mock_auth
        mstore.return_value = _storage_mock()

        out = json.loads(tools.handle_sync({"force": True, "totp_code": "654321"}))

    mock_auth.login.assert_called_once_with(
        "test@example.com", "securepassword123", totp_code="654321")
    assert "error" not in out


def test_totp_required_tells_agent_to_ask(monkeypatch, tmp_path):
    """TOTP_REQUIRED without a code -> actionable ask-the-user-and-retry message."""
    from firefox_bookmarks.ffsync.auth import FxAError

    _env(monkeypatch, tmp_path)
    with patch(_AUTH) as mock_auth_cls, patch(_SESSION), patch(_STORAGE):
        mock_auth_cls.return_value.login.side_effect = FxAError(
            "TOTP_REQUIRED", "2FA required")

        out = json.loads(tools.handle_sync({"force": True}))

    assert "TOTP_REQUIRED" in out["error"]
    assert "totp_code" in out["error"]
