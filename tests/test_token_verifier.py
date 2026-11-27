import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from oauth_fixtures import ISSUER, RESOURCE, unsigned
from webhooks_mcp.constants import TOKEN_CLOCK_SKEW_SECONDS as SKEW
from webhooks_mcp.jwks import IssuerUnavailable

pytest_plugins = ["oauth_fixtures"]


async def test_valid_token_maps_to_access_token(verifier, issuer, clock):
    now = int(clock().timestamp())
    t = issuer.token(
        scp=["webhooks-mcp", "offline_access"], client_id="webhooks-mcp-claude", exp=now + 600
    )
    a = await verifier.verify_token(t)
    assert (a.token, a.scopes, a.client_id, a.subject, a.resource, a.expires_at) == (
        t,
        ["webhooks-mcp", "offline_access"],
        "webhooks-mcp-claude",
        "u1",
        RESOURCE,
        now + 600,
    )


@pytest.mark.parametrize(
    "make",  # make(now) -> keyword arguments for issuer.token
    [
        lambda n: {"iss": "https://other.example"},
        lambda n: {"iss": ISSUER + "/"},
        lambda n: {"aud": "https://wm.example/other"},
        lambda n: {"aud": ["https://wm.example/other"]},
        lambda n: {"drop": ("aud",)},
        lambda n: {"drop": ("exp",)},
        lambda n: {"drop": ("iss",)},
        lambda n: {"exp": n - SKEW - 1},
        lambda n: {"nbf": n + SKEW + 1},
        lambda n: {"iat": n + SKEW + 1},
        lambda n: {"exp": "soon"},
        lambda n: {"exp": True},
        lambda n: {"exp": float("inf")},
        lambda n: {"nbf": "x"},
        lambda n: {"iat": False},
        lambda n: {"header": {"typ": "JWT"}},
        lambda n: {"header": {"typ": None}},
        lambda n: {"header": {"typ": 1}},
    ],
)
async def test_refused_tokens_are_none(verifier, issuer, clock, make):
    now = int(clock().timestamp())
    assert await verifier.verify_token(issuer.token(**make(now))) is None


@pytest.mark.parametrize("typ", ["at+jwt", "AT+JWT", "application/at+jwt"])
async def test_both_typ_spellings_accepted(verifier, issuer, typ):
    assert await verifier.verify_token(issuer.token(header={"typ": typ}))


async def test_clock_skew_is_tolerated(verifier, issuer, clock):
    now = int(clock().timestamp())
    assert await verifier.verify_token(
        issuer.token(exp=now - SKEW + 1, nbf=now + SKEW - 1, iat=now + SKEW - 1)
    )


async def test_aud_list_containing_the_resource_is_accepted(verifier, issuer):
    assert await verifier.verify_token(issuer.token(aud=["https://x.example", RESOURCE]))


async def test_bad_signature_is_refused(verifier, issuer):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await verifier.verify_token(issuer.token(key=other)) is None


async def test_unknown_kid_is_refused_when_a_key_is_cached(verifier, issuer):
    assert await verifier.verify_token(issuer.token())
    assert await verifier.verify_token(issuer.token(header={"kid": "nope"})) is None


async def test_unknown_kid_with_no_key_cached_raises(verifier, issuer):
    issuer.down = True
    with pytest.raises(IssuerUnavailable):
        await verifier.verify_token(issuer.token())


@pytest.mark.parametrize(
    "make",
    [
        lambda i: unsigned({"alg": "none", "typ": "at+jwt", "kid": "k1"}, {"iss": ISSUER}),
        lambda i: jwt.encode(
            {"iss": ISSUER}, "s" * 64, algorithm="HS256", headers={"kid": "k1", "typ": "at+jwt"}
        ),
        lambda i: i.token(header={"kid": None}),
        lambda i: "k" * 40,
        lambda i: "not.a.jwt",
        lambda i: "",
    ],
)
async def test_refused_before_any_fetch(verifier, issuer, make):
    issuer.down = True  # no key cached: a fetch would raise, so None proves none happened
    assert await verifier.verify_token(make(issuer)) is None and issuer.requests == []


@pytest.mark.parametrize(
    "kw,scopes",
    [
        ({"scp": ["webhooks-mcp"]}, ["webhooks-mcp"]),
        ({"drop": ("scp",), "scope": "openid webhooks-mcp"}, ["openid", "webhooks-mcp"]),
        ({"drop": ("scp",)}, []),
        ({"scp": "webhooks-mcp"}, []),
        ({"scp": [1]}, []),
        ({"scp": "webhooks-mcp", "scope": "webhooks-mcp"}, []),  # a malformed scp is never rescued
    ],
)
async def test_scopes(verifier, issuer, kw, scopes):
    assert (await verifier.verify_token(issuer.token(**kw))).scopes == scopes


async def test_missing_or_non_string_sub_and_client_id_are_empty(verifier, issuer):
    for kw in ({"drop": ("sub",)}, {"sub": 7, "client_id": ["x"]}):
        a = await verifier.verify_token(issuer.token(**kw))
        assert (a.subject, a.client_id) == ("", "")
