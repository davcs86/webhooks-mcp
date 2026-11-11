import pytest

from webhooks_mcp.config import ConfigError, Settings

K = "k" * 32
BASES = '{"default":"https://h/"}'


def test_from_env_ok():
    s = Settings.from_env({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES})
    assert s.ingest_base_urls == {"default": "https://h"}
    assert str(s.data_dir) == "/data"


def test_data_dir_override():
    s = Settings.from_env({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES, "DATA_DIR": "/x"})
    assert str(s.data_dir) == "/x"


@pytest.mark.parametrize(
    "env,var",
    [
        ({}, "MCP_AUTH_KEY"),
        ({"MCP_AUTH_KEY": "short"}, "MCP_AUTH_KEY"),
        ({"MCP_AUTH_KEY": K}, "INGEST_BASE_URLS"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": "not json"}, "INGEST_BASE_URLS"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": '{"staging":"https://h"}'}, "INGEST_BASE_URLS"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": '["https://h"]'}, "INGEST_BASE_URLS"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": '{"default":""}'}, "INGEST_BASE_URLS"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": '{"default":"ftp://h"}'}, "INGEST_BASE_URLS"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": '{"default":3}'}, "INGEST_BASE_URLS"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": '{"default":"https://"}'}, "INGEST_BASE_URLS"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": '{"default":"http:///x"}'}, "INGEST_BASE_URLS"),
    ],
)
def test_from_env_errors_name_the_variable(env, var):
    with pytest.raises(ConfigError, match=var):
        Settings.from_env(env)


def test_error_never_contains_the_key():
    secret = "s3cret-short"
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({"MCP_AUTH_KEY": secret})
    assert secret not in str(exc.value)
