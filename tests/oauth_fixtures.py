import asyncio
import base64
import functools
import json

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from webhooks_mcp.jwks import JwksCache
from webhooks_mcp.token_verifier import JwksTokenVerifier

ISSUER = "https://auth.example"
RESOURCE = "https://wm.example/mcp"
DISCOVERY = "/.well-known/openid-configuration"


@functools.cache
def _private_key(kid: str) -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StubIssuer:
    def __init__(self, clock) -> None:
        self._clock = clock
        self.requests: list[str] = []
        self.down = False
        self.discovery: dict = {"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks"}
        self.jwks: dict = {"keys": []}
        self.override: dict[str, httpx2.Response] = {}
        self.gate: asyncio.Event | None = None
        self.delay = 0.0
        self.add_key("k1")

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.requests.append(path)
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.down:
            raise httpx2.ConnectError("issuer down", request=request)
        if path in self.override:
            return self.override[path]
        if path == DISCOVERY:
            return httpx2.Response(200, json=self.discovery)
        if path == "/jwks":
            return httpx2.Response(200, json=self.jwks)
        return httpx2.Response(404)

    def add_key(self, kid: str) -> None:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_private_key(kid).public_key()))
        self.jwks["keys"].append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})

    def token(
        self,
        *,
        drop: tuple[str, ...] = (),
        header: dict | None = None,
        key: RSAPrivateKey | None = None,
        **claims,
    ) -> str:
        now = int(self._clock().timestamp())
        body = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": "u1",
            "exp": now + 3600,
            "scp": ["webhooks-mcp"],
            **claims,
        }
        for name in drop:
            body.pop(name)
        headers = {"kid": "k1", "typ": "at+jwt", **(header or {})}
        headers = {k: v for k, v in headers.items() if v is not None or k == "typ"}
        return jwt.encode(body, key or _private_key("k1"), algorithm="RS256", headers=headers)


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def unsigned(header: dict, claims: dict) -> str:
    return f"{_b64(header)}.{_b64(claims)}."


@pytest.fixture
def issuer(clock) -> StubIssuer:
    return StubIssuer(clock)


@pytest.fixture
async def jwks_cache(issuer, clock):
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(issuer.handler))
    cache = JwksCache(ISSUER, client, clock)
    yield cache
    await cache.aclose()


@pytest.fixture
def verifier(jwks_cache, clock) -> JwksTokenVerifier:
    return JwksTokenVerifier(ISSUER, RESOURCE, jwks_cache, clock)
