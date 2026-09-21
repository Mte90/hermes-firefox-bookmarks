"""FxA login + session refresh + credentials persistence.

Flow (mirrors ffsclient / Mozilla Ecosystem docs):
  1. POST /account/login?keys=true            -> sessionToken, keyFetchToken, (verified|verificationMethod)
  2. POST /session/verify/totp (if TOTP 2FA)
  3. GET  /account/keys  (HAWK from keyFetchToken)  -> bundle -> kA/kB
  4. POST /oauth/token (HAWK from sessionToken)     -> access+refresh token (keys=true)
  5. POST /account/scoped-key-data                    -> keyRotationTimestamp -> keyID
  6. POST tokenserver /oauth/token (form, sessionState) -> access token
  7. GET  tokenserver /1.0/sync/1.5 (Bearer + X-KeyID)  -> HAWK id/key + apiEndpoint
  8. GET  <endpoint>/storage/crypto/keys (HAWK)         -> collection key bundles

Persisted (0600): user_id, kA, kB, refresh_token, key_id, masked email.
kB is the root of everything: sessionState, HAWK session, collection keys.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import (
    FxA_AUTH_URL, TOKEN_SERVER_URL, OAUTH_CLIENT_ID, OAUTH_SCOPE, USER_AGENT,
    PICL_PREFIX,
)
from .crypto import (
    derive_key, stretch_password, unbundle, hawk_authorization, hawk_token_auth,
    key_bundle_from_master, key_bundle_from_b64_array, decrypt_payload, xor_bytes,
)


class FxAError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


# --- dataclasses ------------------------------------------------------------

@dataclass
class CredentialBundle:
    user_id: str
    key_a: bytes
    key_b: bytes
    refresh_token: str
    key_id: str
    email_hint: str = ""          # masked, e.g. "d***@example.com"
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({
            "user_id": self.user_id,
            "key_a": base64.b64encode(self.key_a).decode(),
            "key_b": base64.b64encode(self.key_b).decode(),
            "refresh_token": self.refresh_token,
            "key_id": self.key_id,
            "email_hint": self.email_hint,
            "created_at": self.created_at,
        })

    @classmethod
    def from_json(cls, raw: str) -> "CredentialBundle":
        d = json.loads(raw)
        return cls(
            user_id=d["user_id"],
            key_a=base64.b64decode(d["key_a"]),
            key_b=base64.b64decode(d["key_b"]),
            refresh_token=d["refresh_token"],
            key_id=d["key_id"],
            email_hint=d.get("email_hint", ""),
            created_at=float(d.get("created_at", 0)),
        )


@dataclass
class HawkSession:
    id: str
    key: bytes
    api_endpoint: str
    expires_at: float


@dataclass
class SyncSession:
    """Everything needed to read the storage API."""
    creds: CredentialBundle
    hawk: HawkSession
    bulk_keys: dict[str, tuple[bytes, bytes]]  # collection -> (enc, hmac); "" = default
    account_keys: tuple[bytes, bytes] = (b"", b"")  # syncKeys (enc,hmac) from kB


def mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    user, domain = email.rsplit("@", 1)
    if len(user) <= 2:
        u = user[0] + "*" * max(1, len(user) - 1)
    else:
        u = user[0] + "*" * (len(user) - 2) + user[-1]
    return f"{u}@{domain.split('.', 1)[0]}"


# --- credential store -------------------------------------------------------

def creds_path(home_dir: str | os.PathLike | None = None) -> Path:
    base = Path(os.environ.get("FFB_HOME") or os.environ.get("HERMES_HOME") or (home_dir or Path.home() / ".hermes"))
    return base / "plugin-data" / "firefox-bookmarks" / "creds.json"


def load_credentials(path: Path) -> CredentialBundle | None:
    try:
        return CredentialBundle.from_json(path.read_text())
    except (FileNotFoundError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def save_credentials(bundle: CredentialBundle, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(bundle.to_json())


# --- login ------------------------------------------------------------------

class FxAAuth:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True,
        )

    # -- low-level helpers --------------------------------------------------

    def _resp_json(self, r: httpx.Response, what: str) -> dict:
        if r.status_code != 200:
            try:
                err = r.json()
                code = str(err.get("code", r.status_code))
                msg = str(err.get("message", ""))[:300]
            except ValueError:
                code, msg = str(r.status_code), r.text[:300]
            raise FxAError(code, f"{what} failed (HTTP {r.status_code}): {msg}")
        return r.json()

    def _fetch_keys(self, key_fetch: bytes, stretch: bytes) -> tuple[bytes, bytes]:
        hawk = hawk_token_auth(key_fetch, "keyFetchToken")
        url = f"{FxA_AUTH_URL}/account/keys"
        r = self._client.get(url, headers={
            "Authorization": _hawk_hdr(hawk, "GET", url),
        })
        d = self._resp_json(r, "/account/keys")
        bundle = bytes.fromhex(d["bundle"])
        keys = unbundle("account/keys", hawk["bundle_key"], bundle)
        if len(keys) < 64:
            raise FxAError("CRYPTO", "account/keys bundle shorter than 64 bytes")
        unwrap = derive_key(stretch, "unwrapBkey", 32)
        return keys[:32], xor_bytes(keys[32:64], unwrap)

    def _oauth_tokens(self, session_token: bytes) -> dict:
        hawk = hawk_token_auth(session_token, "sessionToken")
        body = json.dumps({"clientId": OAUTH_CLIENT_ID, "keys": True,
                           "scope": OAUTH_SCOPE, "grantType": "fxa-credentials"})
        url = f"{FxA_AUTH_URL}/oauth/token"
        r = self._client.post(url, content=body, headers={
            "Content-Type": "application/json",
            "Authorization": _hawk_hdr(hawk, "POST", url, body),
        })
        return self._resp_json(r, "/oauth/token")

    def _scoped_key_data(self, session_token: bytes) -> dict:
        hawk = hawk_token_auth(session_token, "sessionToken")
        body = json.dumps({"clientId": OAUTH_CLIENT_ID, "keys": True, "scope": OAUTH_SCOPE})
        url = f"{FxA_AUTH_URL}/account/scoped-key-data"
        r = self._client.post(url, content=body, headers={
            "Content-Type": "application/json",
            "Authorization": _hawk_hdr(hawk, "POST", url, body),
        })
        d = self._resp_json(r, "/account/scoped-key-data")
        if OAUTH_SCOPE not in d:
            raise FxAError("CRYPTO", "scoped-key-data missing oldsync scope")
        return d[OAUTH_SCOPE]

    # -- public flow ----------------------------------------------------------

    def login(self, email: str, password: str, totp_code: str | None = None) -> CredentialBundle:
        c = self._client
        stretch = stretch_password(email, password)
        auth_pw = derive_key(stretch, "authPW", 32)
        body = json.dumps({"email": email, "authPW": auth_pw.hex(), "reason": "login"})

        def _do_login() -> dict:
            r = c.post(f"{FxA_AUTH_URL}/account/login?keys=true", content=body,
                       headers={"Content-Type": "application/json"})
            return self._resp_json(r, "/account/login")

        data = _do_login()
        if not data.get("verified"):
            vm = data.get("verificationMethod", "")
            if "totp" in vm:
                if not totp_code:
                    raise FxAError("TOTP_REQUIRED",
                                   "Firefox Account requires TOTP 2FA. Rerun with the 6-digit code.")
                r = c.post(f"{FxA_AUTH_URL}/session/verify/totp",
                           content=json.dumps({"code": totp_code.strip()}),
                           headers={"Content-Type": "application/json"})
                self._resp_json(r, "/session/verify/totp")
                data = _do_login()
            elif vm:
                raise FxAError("2FA_UNSUPPORTED",
                               f"verification method '{vm}' not supported by v1 (TOTP only).")
            if not data.get("verified"):
                raise FxAError("AUTH_EXPIRED", "session still unverified after TOTP check")

        session_token = bytes.fromhex(data["sessionToken"])
        key_fetch = bytes.fromhex(data["keyFetchToken"])
        user_id = data.get("uid", "")

        k_a, k_b = self._fetch_keys(key_fetch, stretch)
        odata = self._oauth_tokens(session_token)
        skd = self._scoped_key_data(session_token)
        client_state = base64.urlsafe_b64encode(hashlib.sha256(k_b).digest()[:16]).decode().rstrip("=")
        key_id = f"{skd['keyRotationTimestamp']}-{client_state}"

        return CredentialBundle(
            user_id=user_id, key_a=k_a, key_b=k_b,
            refresh_token=odata["refreshToken"], key_id=key_id,
            email_hint=mask_email(email),
        )


# --- storage session ---------------------------------------------------------

def _hawk_hdr(hawk: dict, method: str, url: str, body: str = "") -> str:
    return hawk_authorization(hawk["id"], hawk["key"], method, url, body=body)


def acquire_storage_session(creds: CredentialBundle, client: httpx.Client | None = None) -> SyncSession:
    """Refresh token -> tokenserver HAWK -> /storage/crypto/keys -> SyncSession."""
    c = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    session_state = hashlib.sha256(creds.key_b).digest()[:16].hex()
    r = c.post(
        f"{TOKEN_SERVER_URL}/oauth/token",
        data={
            "client_id": OAUTH_CLIENT_ID,
            "scope": OAUTH_SCOPE,
            "grant_type": "fxa-credentials",
            "sessionState": session_state,
            "refreshToken": creds.refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 200:
        raise FxAError("AUTH_EXPIRED",
                       f"token refresh failed (HTTP {r.status_code}); rerun setup with your password")
    tok = r.json()

    r2 = c.get(f"{TOKEN_SERVER_URL}/1.0/sync/1.5",
               headers={"Authorization": f"Bearer {tok['access_token']}",
                        "X-KeyID": creds.key_id})
    if r2.status_code != 200:
        raise FxAError("NETWORK", f"tokenserver HAWK failed (HTTP {r2.status_code})")
    hawk = r2.json()
    if hawk.get("algorithm", "sha256") != "sha256":
        raise FxAError("UNSUPPORTED", f"HAWK algorithm {hawk.get('algorithm')} unsupported")
    hawk_sess = HawkSession(
        id=hawk["id"], key=bytes.fromhex(hawk["key"]),
        api_endpoint=hawk["apiEndpoint"].rstrip("/"),
        expires_at=time.time() + float(hawk.get("duration", 600)),
    )

    ep = hawk_sess.api_endpoint
    url = f"{ep}/storage/crypto/keys"
    r3 = c.get(url, headers={"Authorization": _hawk_hdr(
        {"id": hawk_sess.id, "key": hawk_sess.key}, "GET", url)})
    if r3.status_code != 200:
        raise FxAError("NETWORK", f"crypto/keys fetch failed (HTTP {r3.status_code})")
    bso = r3.json()
    sync_keys = key_bundle_from_master(creds.key_b, PICL_PREFIX + "oldsync")
    plain = decrypt_payload(bso["payload"]["ciphertext"], bso["payload"]["iv"],
                            bso["payload"]["hmac"], sync_keys[0], sync_keys[1])
    ck = json.loads(plain)
    bulk: dict[str, tuple[bytes, bytes]] = {}
    if ck.get("default"):
        bulk[""] = key_bundle_from_b64_array(ck["default"])
    for coll, arr in (ck.get("collections") or {}).items():
        bulk[coll] = key_bundle_from_b64_array(arr)
    if "" not in bulk:
        raise FxAError("CRYPTO", "no default key bundle in crypto/keys")

    return SyncSession(creds=creds, hawk=hawk_sess, bulk_keys=bulk, account_keys=sync_keys)
