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


def test_repr_never_contains_the_key():
    s = Settings.from_env({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES})
    assert K not in repr(s) and "https://h" in repr(s)


ISS, RES = "https://auth.example", "https://wm.example/mcp"


def _env(mode="oauth", **over):  # value None removes the variable
    env = {"AUTH_MODE": mode, "INGEST_BASE_URLS": BASES, "OAUTH_ISSUER": ISS, "OAUTH_RESOURCE": RES}
    if mode == "static+oauth":
        env["MCP_AUTH_KEY"] = K
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def test_auth_mode_defaults_to_static():
    s = Settings.from_env({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES})
    assert (s.auth_mode, s.oauth_issuer, s.oauth_resource) == ("static", None, None)


@pytest.mark.parametrize("mode,key", [("static+oauth", K), ("oauth", None)])
def test_oauth_modes_parse(mode, key):
    s = Settings.from_env(_env(mode))
    assert (s.auth_mode, s.mcp_auth_key, s.oauth_issuer, s.oauth_resource) == (mode, key, ISS, RES)


def test_issuer_keeps_its_path_verbatim():
    assert Settings.from_env(_env(OAUTH_ISSUER=ISS + "/realm")).oauth_issuer == ISS + "/realm"


@pytest.mark.parametrize(
    "env,var",
    [
        (_env(AUTH_MODE="OAuth"), "AUTH_MODE"),
        (_env(AUTH_MODE=""), "AUTH_MODE"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES, "OAUTH_ISSUER": ISS}, "OAUTH_ISSUER"),
        ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES, "OAUTH_RESOURCE": RES}, "OAUTH_RESOURCE"),
        (_env("static+oauth", MCP_AUTH_KEY=None), "MCP_AUTH_KEY"),
        (_env("static+oauth", MCP_AUTH_KEY="short"), "MCP_AUTH_KEY"),
        (_env(MCP_AUTH_KEY=K), "MCP_AUTH_KEY"),
        (_env(MCP_AUTH_KEY=""), "MCP_AUTH_KEY"),
        (_env(OAUTH_ISSUER=None), "OAUTH_ISSUER"),
        (_env(OAUTH_RESOURCE=None), "OAUTH_RESOURCE"),
        *[
            (_env(OAUTH_ISSUER=v), "OAUTH_ISSUER")
            for v in (
                "http://auth.example",
                "https://auth.example/",
                "https://auth.example?x=1",
                "https://auth.example#f",
                "https://auth.example?",
                "https://auth.example#",
                "auth.example",
                "https://",
                "https://exa mple",
                " https://auth.example",
                "https://auth.example\n",
                "https://auth.example/ ",
                "https://auth.ex\tample",
            )
        ],
        *[
            (_env(OAUTH_RESOURCE=v), "OAUTH_RESOURCE")
            for v in (
                "http://wm.example/mcp",
                "https://wm.example",
                "https://wm.example/",
                "https://wm.example/mcp/",
                "https://wm.example/MCP",
                "https://wm.example/x/mcp",
                "https://wm.example/mcp?x",
                "https://wm.example/mcp?",
                "https://wm.example/mcp#",
                "https://wm ex/mcp",
                "https://wm.example/mcp\n",
                "https://wm.example/m\tcp",
            )
        ],
    ],
)
def test_auth_refusals_name_the_variable(env, var):
    with pytest.raises(ConfigError, match=var):
        Settings.from_env(env)


def test_oauth_refusal_never_echoes_the_key():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env(_env(MCP_AUTH_KEY="s" * 40))
    assert "s" * 40 not in str(exc.value)
