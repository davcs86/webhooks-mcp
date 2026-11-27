import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings
from pydantic import ValidationError

from webhooks_mcp.constants import MIN_AUTH_KEY_CHARS, REQUIRED_SCOPE

AuthMode = Literal["static", "static+oauth", "oauth"]
AUTH_MODES: tuple[AuthMode, ...] = ("static", "static+oauth", "oauth")
OAUTH_MODES: tuple[AuthMode, ...] = ("static+oauth", "oauth")
_AUTH_SETTINGS_VARS = {"issuer_url": "OAUTH_ISSUER", "resource_server_url": "OAUTH_RESOURCE"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    mcp_auth_key: str | None = field(repr=False)  # None only in "oauth"
    ingest_base_urls: dict[str, str]
    data_dir: Path
    auth_mode: AuthMode = "static"
    oauth_issuer: str | None = None
    oauth_resource: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        mode = env.get("AUTH_MODE", "static")
        if mode not in AUTH_MODES:
            raise ConfigError("AUTH_MODE must be one of: static, static+oauth, oauth")
        key = env.get("MCP_AUTH_KEY")
        if mode == "oauth":
            if key is not None:
                raise ConfigError("MCP_AUTH_KEY must not be set when AUTH_MODE is oauth")
        elif key is None or len(key) < MIN_AUTH_KEY_CHARS:
            raise ConfigError(
                f"MCP_AUTH_KEY must be set to at least {MIN_AUTH_KEY_CHARS} characters"
            )
        issuer, resource = env.get("OAUTH_ISSUER"), env.get("OAUTH_RESOURCE")
        if mode in OAUTH_MODES:
            issuer, resource = _check_oauth_urls(issuer, resource)
        else:
            for var, value in (("OAUTH_ISSUER", issuer), ("OAUTH_RESOURCE", resource)):
                if value is not None:
                    raise ConfigError(f"{var} must not be set when AUTH_MODE is static")
        return cls(
            key,
            _parse_bases(env.get("INGEST_BASE_URLS")),
            Path(env.get("DATA_DIR", "/data")),
            mode,
            issuer,
            resource,
        )


def _https_parts(raw: str, var: str):
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise ConfigError(f"{var} must be an https URL") from None
    if parts.scheme != "https" or not parts.netloc or "?" in raw or "#" in raw:
        raise ConfigError(f"{var} must be an https URL with no query or fragment")
    return parts


def _check_oauth_urls(issuer: str | None, resource: str | None) -> tuple[str, str]:
    if issuer is None:
        raise ConfigError("OAUTH_ISSUER must be set when AUTH_MODE is an OAuth mode")
    if resource is None:
        raise ConfigError("OAUTH_RESOURCE must be set when AUTH_MODE is an OAuth mode")
    _https_parts(issuer, "OAUTH_ISSUER")
    if issuer.endswith("/"):
        raise ConfigError("OAUTH_ISSUER must not end with a slash")
    if _https_parts(resource, "OAUTH_RESOURCE").path != "/mcp":
        raise ConfigError("OAUTH_RESOURCE path must be exactly /mcp")
    try:
        AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource,
            required_scopes=[REQUIRED_SCOPE],
            validate_token_resource=False,
        )
    except ValidationError as exc:
        var = _AUTH_SETTINGS_VARS[str(exc.errors()[0]["loc"][0])]
        raise ConfigError(f"{var} is not a valid URL") from None
    return issuer, resource


def _is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _parse_bases(raw: str | None) -> dict[str, str]:
    if not raw:
        raise ConfigError("INGEST_BASE_URLS is not set")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ConfigError("INGEST_BASE_URLS is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise ConfigError("INGEST_BASE_URLS must be a JSON object")
    bases: dict[str, str] = {}
    for label, url in parsed.items():
        if not isinstance(url, str) or not _is_http_url(url):
            raise ConfigError(f"INGEST_BASE_URLS entry {label!r} must be an http(s) URL")
        bases[label] = url.rstrip("/")
    if "default" not in bases:
        raise ConfigError("INGEST_BASE_URLS must contain a 'default' key")
    return bases
