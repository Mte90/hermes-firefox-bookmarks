"""Firefox Sync crypto primitives.

Implements the Mozilla "personal identity" crypto chain as used by
``ffsclient`` and the SyncStorage 1.5 spec:

* ``stretch_password``  — PBKDF2-SHA256(1000) over ``identity.mozilla.com/picl/v1/quickStretch:<email>``
* ``derive_key``        — HKDF-SHA256 with empty salt, info = ``picl/<namespace>``
* ``unbundle``          — XOR cipher + trailing-32 HMAC-SHA256 (for /account/keys)
* ``key_bundle_from_master`` — HKDF 64 bytes -> (encryption_key, hmac_key)
* ``decrypt_payload``   — AES-256-CBC + PKCS7, HMAC-SHA256 over the *base64* ciphertext string
* ``hawk_*``            — HAWK request signing (for storage) and HAWK-token auth (for FxA)

All functions are pure and unit-testable offline.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import os
import secrets
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_SHA256 = hashes.SHA256()


# --- PBKDF2 / HKDF ---------------------------------------------------------

def pbkdf2_sha256(password: bytes, salt: bytes, iterations: int, dklen: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=_SHA256, length=dklen, salt=salt, iterations=iterations)
    return kdf.derive(password)


def stretch_password(email: str, password: str) -> bytes:
    salt = f"identity.mozilla.com/picl/v1/quickStretch:{email}".encode("utf-8")
    return pbkdf2_sha256(password.encode("utf-8"), salt, 1000, 32)


def derive_key(secret: bytes, namespace: str, size: int, info_prefix: str = "identity.mozilla.com/picl/v1/") -> bytes:
    """HKDF-SHA256(secret, salt=b'', info=picl/<namespace>)."""
    info = (info_prefix + namespace).encode("utf-8")
    kdf = HKDF(algorithm=_SHA256, length=size, salt=b"", info=info)
    return kdf.derive(secret)


def xor_bytes(a: bytes, b: bytes) -> bytes:
    if len(a) != len(b):
        raise ValueError("xor length mismatch")
    return bytes(x ^ y for x, y in zip(a, b))


# --- /account/keys bundle --------------------------------------------------

def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return _hmac.new(key, data, hashlib.sha256).digest()


def unbundle(namespace: str, bundle_key: bytes, payload: bytes) -> bytes:
    """Decipher a ``/account/keys`` bundle: last 32 bytes = HMAC over ciphertext."""
    if len(payload) < 32:
        raise ValueError("bundle payload too short")
    ciphertext = payload[:-32]
    expected = payload[-32:]
    km = derive_key(bundle_key, namespace, 32 + len(ciphertext))
    if not _hmac.compare_digest(_hmac_sha256(km[:32], ciphertext), expected):
        raise ValueError("bundle HMAC mismatch (wrong key fetch token?)")
    return xor_bytes(km[32:], ciphertext)


# --- key bundles -----------------------------------------------------------

def key_bundle_from_master(master: bytes, info: str) -> tuple[bytes, bytes]:
    """HKDF-64 -> (encryption_key[32], hmac_key[32])."""
    km = derive_key(master, info, 64, info_prefix="")
    return km[:32], km[32:]


def key_bundle_from_b64_array(arr: list[str]) -> tuple[bytes, bytes]:
    if len(arr) != 2:
        raise ValueError("keydata must be a 2-element array")
    return base64.b64decode(arr[0]), base64.b64decode(arr[1])


# --- BSO payload decrypt ---------------------------------------------------

def _strip_pkcs7(data: bytes, blocksize: int = 16) -> bytes:
    if not data:
        return data
    if len(data) % blocksize:
        return data  # not block-aligned; return as-is, caller will fail later
    pad = data[-1]
    if 0 < pad <= blocksize and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt_payload(ciphertext_b64: str, iv_b64: str, hmac_hex: str,
                    encryption_key: bytes, hmac_key: bytes) -> bytes:
    """Decrypt a Sync BSO payload. HMAC is computed over the *base64 string*."""
    iv = base64.b64decode(iv_b64)
    hmac_val = bytes.fromhex(hmac_hex)
    ciphertext = base64.b64decode(ciphertext_b64)
    if not _hmac.compare_digest(_hmac_sha256(hmac_key, ciphertext_b64.encode("utf-8")), hmac_val):
        raise ValueError("payload HMAC mismatch (wrong collection key?)")
    if len(ciphertext) % 16:
        raise ValueError("ciphertext not block-aligned")
    dec = Cipher(algorithms.AES(encryption_key), modes.CBC(iv)).decryptor()
    plain = dec.update(ciphertext) + dec.finalize()
    return _strip_pkcs7(plain)


def encrypt_payload(plaintext: bytes, encryption_key: bytes, hmac_key: bytes) -> dict:
    """Inverse of :func:`decrypt_payload` (used by local-mode fixture tests)."""
    iv = os.urandom(16)
    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(encryption_key), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    ct_b64 = base64.b64encode(ct).decode("ascii")
    return {
        "ciphertext": ct_b64,
        "iv": base64.b64encode(iv).decode("ascii"),
        "hmac": _hmac_sha256(hmac_key, ct_b64.encode("utf-8")).hex(),
    }


# --- HAWK ------------------------------------------------------------------

def _hawk_signing_string(method: str, url: str, mac_key: bytes,
                         content_type: str = "application/json", body: str = "") -> tuple[str, bytes, bytes, str, str]:
    """Return (mac, ts, nonce, payload_hash)."""
    from urllib.parse import urlparse
    if method.upper() == "GET" or not body:
        payload_hash = ""
    else:
        p = "hawk.1.payload\n" + content_type + "\n" + body + "\n"
        payload_hash = base64.b64encode(hashlib.sha256(p.encode("utf-8")).digest()).decode("ascii")
    u = urlparse(url)
    host = u.hostname or ""
    port = str(u.port) if u.port else ("443" if u.scheme == "https" else "80")
    rpath = u.path
    if u.query:
        rpath += "?" + u.query
    ts = str(_now_ts())
    nonce = base64.b64encode(secrets.token_bytes(5)).decode("ascii")
    sig = "\n".join([
        "hawk.1.header", ts, nonce, method.upper(), rpath,
        host.lower(), port.lower(), payload_hash, "", "",
    ])
    mac = base64.b64encode(_hmac_sha256(mac_key, sig.encode("utf-8"))).decode("ascii")
    return mac, ts, nonce, payload_hash


def _now_ts() -> int:
    import time
    return int(time.time())


def hawk_authorization(mac_id: str, mac_key: bytes, method: str, url: str,
                       content_type: str = "application/json", body: str = "") -> str:
    mac, ts, nonce, payload_hash = _hawk_signing_string(method, url, mac_key, content_type, body)
    header = (f'Hawk id="{mac_id}", mac="{mac}", ts="{ts}", nonce="{nonce}"')
    if payload_hash:
        header += f', hash="{payload_hash}"'
    return header


def hawk_token_auth(token: bytes, token_type: str) -> dict:
    """Derive (id, key, bundle_key) from an FxA sessionToken/keyFetchToken."""
    km = derive_key(token, token_type, 96)
    return {
        "id": km[:32].hex(),
        "key": km[32:64],
        "bundle_key": km[64:],
    }


def __all__check() -> None:  # pragma: no cover - self-test helper
    assert len(hashlib.sha256(b"x").digest()) == 32
