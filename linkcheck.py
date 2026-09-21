"""Dead-link checking: concurrent HTTP HEAD with per-request timeout."""
from __future__ import annotations

import concurrent.futures as cf
import time

import httpx

_UA = "hermes-plugin-firefox-bookmarks/1.0 (link-check; +read-only)"


def check_urls(urls: list[str], max_workers: int = 8, timeout: float = 10.0) -> list[dict]:
    urls = [u for u in dict.fromkeys(urls) if u.startswith(("http://", "https://"))]
    if not urls:
        return []
    out: list[dict] = []

    client = httpx.Client(headers={"User-Agent": _UA}, follow_redirects=True)

    def _one(url: str) -> dict:
        t0 = time.time()
        try:
            r = client.head(url, timeout=timeout)
            status = r.status_code
            ok = status < 400
        except httpx.HTTPError:
            try:  # some servers block HEAD; fall back to a streamed GET
                with client.stream("GET", url, timeout=timeout) as s:
                    status = s.response.status_code
                    ok = status < 400
            except httpx.HTTPError as e:
                return {"url": url, "status": 0, "ok": False,
                        "error": type(e).__name__,
                        "elapsed_ms": int((time.time() - t0) * 1000)}
        return {"url": url, "status": status, "ok": ok,
                "elapsed_ms": int((time.time() - t0) * 1000)}

    try:
        with cf.ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
            for fut in cf.as_completed([ex.submit(_one, u) for u in urls]):
                out.append(fut.result())
    finally:
        client.close()
    return out
