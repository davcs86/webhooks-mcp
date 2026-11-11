import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from webhooks_mcp.constants import MIN_AUTH_KEY_CHARS


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    mcp_auth_key: str = field(repr=False)
    ingest_base_urls: dict[str, str]
    data_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        key = env.get("MCP_AUTH_KEY", "")
        if len(key) < MIN_AUTH_KEY_CHARS:
            raise ConfigError(
                f"MCP_AUTH_KEY must be set to at least {MIN_AUTH_KEY_CHARS} characters"
            )
        return cls(
            key, _parse_bases(env.get("INGEST_BASE_URLS")), Path(env.get("DATA_DIR", "/data"))
        )


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
