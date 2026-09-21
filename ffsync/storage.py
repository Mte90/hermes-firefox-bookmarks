"""SyncStorage 1.5 REST client: list + decrypt BSO records from a collection."""
from __future__ import annotations

import json
import time

import httpx

from . import USER_AGENT
from .auth import SyncSession, FxAError
from .crypto import decrypt_payload, hawk_authorization


class SyncStorage:
    def __init__(self, session: SyncSession, client: httpx.Client | None = None):
        self.session = session
        self._client = client or httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT})

    def _headers(self, method: str, url: str) -> dict:
        h = self.session.hawk
        return {"Authorization": hawk_authorization(h.id, h.key, method, url)}

    def collections_info(self) -> dict:
        """GET /info/collections -> {name: last_modified}."""
        url = f"{self.session.hawk.api_endpoint}/info/collections"
        r = self._get(url, self._headers("GET", url))
        return r.json()

    def list_records(self, collection: str, newer: float | None = None,
                     limit: int = 1000) -> list[dict]:
        """Paged GET /storage/<collection>; returns decrypted payload dicts."""
        base = f"{self.session.hawk.api_endpoint}/storage/{collection}"
        bulk = self.session.bulk_keys.get(collection) or self.session.bulk_keys[""]
        out: list[dict] = []
        offset: str | None = None
        for _ in range(128):
            params: list[str] = [f"limit={limit}"]
            if newer is not None:
                params.append(f"newer={newer:.2f}")
            if offset:
                params.append(f"offset={offset}")
            url = base + "?" + "&".join(params)
            body, headers = self._get(url, self._headers("GET", url))
            records = body if isinstance(body, list) else [body]
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                try:
                    plain = decrypt_payload(
                        rec["payload"]["ciphertext"], rec["payload"]["iv"],
                        rec["payload"]["hmac"], bulk[0], bulk[1],
                    )
                    payload = json.loads(plain)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    continue
                payload["_id"] = rec.get("id", "")
                payload["_modified"] = float(rec.get("modified", 0))
                out.append(payload)
            offset = headers.get("X-Weave-Next-Offset")
            if not offset:
                break
        return out

    # -- low level ---------------------------------------------------------

    def _get(self, url: str, headers: dict) -> tuple[object, dict]:
        last_exc: Exception | None = None
        for _attempt in range(4):
            r = self._client.get(url, headers=headers)
            if r.status_code == 200:
                return r.json(), dict(r.headers)
            if r.status_code in (408, 429) or 500 <= r.status_code < 600:
                try:
                    delay = min(max(float(r.headers.get("Retry-After", "2")), 0.5), 30.0)
                except ValueError:
                    delay = 2.0
                time.sleep(delay)
                last_exc = FxAError("RATE_LIMITED", f"HTTP {r.status_code}")
                continue
            raise FxAError("NETWORK", f"storage GET failed (HTTP {r.status_code})")
        raise last_exc or FxAError("RATE_LIMITED", "storage GET retries exhausted")
