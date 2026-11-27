import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken

from webhooks_mcp.constants import TOKEN_CLOCK_SKEW_SECONDS
from webhooks_mcp.jwks import JwksCache
from webhooks_mcp.store import utcnow

_TYPS = ("at+jwt", "application/at+jwt")


def _is_time(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _scopes(claims: dict[str, Any]) -> list[str]:
    if "scp" in claims:
        scp = claims["scp"]
        if isinstance(scp, list) and all(isinstance(s, str) for s in scp):
            return scp
        return []
    scope = claims.get("scope")
    return scope.split() if isinstance(scope, str) else []


def _str_claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    return value if isinstance(value, str) else ""


class JwksTokenVerifier:
    def __init__(
        self,
        issuer: str,
        resource: str,
        keys: JwksCache,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._issuer = issuer
        self._resource = resource
        self._keys = keys
        self._clock = clock

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        typ, kid = header.get("typ"), header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(typ, str)
            or typ.lower() not in _TYPS
            or not isinstance(kid, str)
            or not kid
        ):
            return None
        key = await self._keys.key(kid)  # IssuerUnavailable propagates (→ 503)
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._resource,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "verify_sub": False,
                },
            )
        except jwt.PyJWTError:
            return None
        now = self._clock().timestamp()
        skew = TOKEN_CLOCK_SKEW_SECONDS
        exp = claims["exp"]
        if not _is_time(exp) or exp <= now - skew:
            return None
        for name in ("nbf", "iat"):
            if name in claims and (not _is_time(claims[name]) or claims[name] > now + skew):
                return None
        return AccessToken(
            token=token,
            client_id=_str_claim(claims, "client_id"),
            scopes=_scopes(claims),
            expires_at=int(exp),
            resource=self._resource,
            subject=_str_claim(claims, "sub"),
        )

    async def aclose(self) -> None:
        await self._keys.aclose()
