import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx2
import jwt

from webhooks_mcp.constants import (
    JWKS_FETCH_TIMEOUT_SECONDS,
    JWKS_MAX_AGE_SECONDS,
    JWKS_MAX_RESPONSE_BYTES,
    JWKS_REFETCH_MIN_SECONDS,
)
from webhooks_mcp.store import utcnow


class IssuerUnavailable(Exception):
    pass


class _FetchFailed(Exception):
    pass


class JwksCache:
    def __init__(
        self, issuer: str, client: httpx2.AsyncClient, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._issuer = issuer
        self._client = client
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    def _in_age(self, now: float) -> bool:
        return (
            bool(self._keys)
            and self._fetched_at is not None
            and now - self._fetched_at < JWKS_MAX_AGE_SECONDS
        )

    async def key(self, kid: str) -> jwt.PyJWK | None:
        now = self._clock().timestamp()
        if kid in self._keys and self._in_age(now):
            return self._keys[kid]
        async with self._lock:
            now = self._clock().timestamp()
            if kid in self._keys and self._in_age(now):
                return self._keys[kid]
            if self._last_attempt is None or now - self._last_attempt >= JWKS_REFETCH_MIN_SECONDS:
                self._last_attempt = now
                try:
                    keys = await self._fetch()
                except (
                    httpx2.HTTPError,
                    TimeoutError,
                    ValueError,
                    jwt.PyJWTError,
                    _FetchFailed,
                ):
                    pass
                else:
                    self._keys, self._fetched_at = keys, now
            if not self._in_age(now):
                raise IssuerUnavailable
            return self._keys.get(kid)

    async def _get_json(self, url: str) -> Any:
        async with (
            asyncio.timeout(JWKS_FETCH_TIMEOUT_SECONDS),
            self._client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise _FetchFailed
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > JWKS_MAX_RESPONSE_BYTES:
                    raise _FetchFailed
        return json.loads(body)

    async def _fetch(self) -> dict[str, jwt.PyJWK]:
        discovery = await self._get_json(f"{self._issuer}/.well-known/openid-configuration")
        if not isinstance(discovery, dict) or discovery.get("issuer") != self._issuer:
            raise _FetchFailed
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise _FetchFailed
        parts = urlsplit(jwks_uri)
        if parts.scheme != "https" or parts.netloc != urlsplit(self._issuer).netloc:
            raise _FetchFailed
        doc = await self._get_json(jwks_uri)
        if not isinstance(doc, dict):
            raise _FetchFailed
        keys = {
            k.key_id: k
            for k in jwt.PyJWKSet.from_dict(doc).keys
            if isinstance(k.key_id, str) and k.key_id
        }
        if not keys:
            raise _FetchFailed
        return keys

    async def aclose(self) -> None:
        await self._client.aclose()
