"""Token verification, and the specific ways JWT is usually got wrong.

Each of the attacks below is a real, named vulnerability class rather than a
hypothetical, which is why the verification is narrow and why the algorithm
comes from configuration rather than from the token.
"""

from __future__ import annotations

import time

import jwt
import pytest

from app.infra.auth import (
    ALLOWED_ALGORITHMS,
    AuthConfigError,
    AuthError,
    AuthSettings,
    Caller,
    from_headers,
    issue,
    settings_from_env,
    verify,
)

SECRET = "a-test-signing-secret-long-enough-to-be-plausible"
NOW = 1_788_000_000


@pytest.fixture
def settings() -> AuthSettings:
    return AuthSettings(secret=SECRET)


class TestTheHappyPath:
    def test_a_token_round_trips(self, settings):
        token = issue("warden-7", settings,
                      permissions=["evac:read", "evac:warden"],
                      zones=["assembly-north"], tenant_id="tenant-1")
        caller = verify(token, settings)
        assert caller.user_id == "warden-7"
        assert caller.may("evac:warden") is True
        assert caller.covers("assembly-north") is True
        assert caller.covers("assembly-south") is False
        assert caller.tenant_id == "tenant-1"

    def test_a_warden_with_no_zones_is_unrestricted(self, settings):
        # A roving supervisor. Fails open, which is the wrong direction, so the
        # security document records it as something to treat as a finding.
        caller = verify(issue("warden-9", settings, permissions=["evac:warden"]),
                        settings)
        assert caller.covers("anywhere") is True

    def test_the_default_lifetime_covers_a_drill_day(self, settings):
        # A token that expires mid-evacuation logs a warden out at the assembly
        # point. Issued against the real clock, because verification uses it.
        issued_at = int(time.time())
        caller = verify(issue("w", settings, now=issued_at), settings)
        assert caller.expires_at - issued_at >= 12 * 3_600


class TestTheKnownAttacks:
    def test_alg_none_is_refused(self, settings):
        # The token does not get a vote on how it is verified.
        forged = jwt.encode({"sub": "attacker", "exp": NOW + 3_600,
                             "permissions": ["evac:admin"]},
                            "", algorithm="none")
        with pytest.raises(AuthError):
            verify(forged, settings)

    def test_a_token_signed_with_another_secret_is_refused(self, settings):
        forged = jwt.encode({"sub": "attacker", "exp": int(time.time()) + 3_600},
                            "a-different-secret", algorithm="HS256")
        with pytest.raises(AuthError, match="signature"):
            verify(forged, settings)

    def test_only_hmac_algorithms_are_allowed(self):
        # Algorithm confusion: an RS256 token verified as HS256 with the public
        # key as the secret. Refusing anything but HMAC closes it.
        assert set(ALLOWED_ALGORITHMS) == {"HS256", "HS384", "HS512"}
        with pytest.raises(AuthConfigError, match="not an allowed algorithm"):
            verify("x", AuthSettings(secret=SECRET, algorithm="RS256"))

    def test_an_expired_token_is_refused(self, settings):
        expired = issue("w", settings, ttl_s=1, now=NOW - 10_000)
        with pytest.raises(AuthError, match="expired"):
            verify(expired, settings)

    def test_a_token_with_no_expiry_is_refused(self, settings):
        # Never a "unless it is missing" branch. A token without an expiry is
        # a credential that lives forever.
        forever = jwt.encode({"sub": "w"}, SECRET, algorithm="HS256")
        with pytest.raises(AuthError, match="exp"):
            verify(forever, settings)

    def test_a_token_with_no_subject_is_refused(self, settings):
        anonymous = jwt.encode({"exp": int(time.time()) + 3_600},
                               SECRET, algorithm="HS256")
        with pytest.raises(AuthError, match="sub"):
            verify(anonymous, settings)

    def test_a_blank_subject_is_refused(self, settings):
        blank = jwt.encode({"sub": "   ", "exp": int(time.time()) + 3_600},
                           SECRET, algorithm="HS256")
        with pytest.raises(AuthError, match="nothing can be attributed"):
            verify(blank, settings)

    def test_garbage_is_refused_without_crashing(self, settings):
        for rubbish in ("", "not.a.token", "a.b.c", "Bearer x"):
            with pytest.raises(AuthError):
                verify(rubbish, settings)

    def test_verifying_against_no_secret_is_a_configuration_error(self):
        # Not a 401. Distinguished so an operator is not sent looking for a
        # broken client when the service is the problem.
        with pytest.raises(AuthConfigError, match="against nothing"):
            verify("anything", AuthSettings(secret=""))


class TestIssuerAndAudience:
    def test_a_wrong_issuer_is_refused(self):
        strict = AuthSettings(secret=SECRET, issuer="evac-central")
        forged = jwt.encode({"sub": "w", "exp": int(time.time()) + 3_600,
                             "iss": "somewhere-else"}, SECRET, algorithm="HS256")
        with pytest.raises(AuthError):
            verify(forged, strict)

    def test_a_matching_issuer_passes(self):
        strict = AuthSettings(secret=SECRET, issuer="evac-central")
        assert verify(issue("w", strict), strict).user_id == "w"

    def test_a_wrong_audience_is_refused(self):
        strict = AuthSettings(secret=SECRET, audience="evac-edge")
        forged = jwt.encode({"sub": "w", "exp": int(time.time()) + 3_600,
                             "aud": "something-else"}, SECRET, algorithm="HS256")
        with pytest.raises(AuthError):
            verify(forged, strict)

    def test_an_unconfigured_audience_does_not_reject_tokens_that_carry_one(
        self, settings
    ):
        # PyJWT would otherwise refuse any token with an aud claim.
        with_aud = jwt.encode({"sub": "w", "exp": int(time.time()) + 3_600,
                               "aud": "anything"}, SECRET, algorithm="HS256")
        assert verify(with_aud, settings).user_id == "w"


class TestTrustedHeaders:
    def test_they_are_refused_by_default(self):
        # A service that trusts X-Permissions from anyone who can reach it has
        # no authentication at all.
        with pytest.raises(AuthError, match="not trusted"):
            from_headers("someone", "evac:admin", "", AuthSettings())

    def test_the_refusal_names_the_setting(self):
        with pytest.raises(AuthError, match="EVAC_TRUST_IDENTITY_HEADERS"):
            from_headers("someone", "", "", AuthSettings())

    def test_they_work_when_switched_on(self):
        gateway = AuthSettings(trust_headers=True)
        caller = from_headers("commander-1", "evac:read,evac:operate",
                              "assembly-north", gateway)
        assert caller.may("evac:operate") is True
        assert caller.via == "gateway"

    def test_an_empty_identity_is_still_refused(self):
        with pytest.raises(AuthError, match="no caller identity"):
            from_headers("", "evac:read", "", AuthSettings(trust_headers=True))


class TestConfiguration:
    def test_an_unconfigured_service_names_its_gap(self):
        # The safe default refuses everything, and an operator should find that
        # out from a dashboard rather than a wall of 401s.
        gap = AuthSettings().describe_gap()
        assert gap and "EVAC_JWT_SECRET" in gap

    def test_a_configured_service_has_no_gap(self):
        assert AuthSettings(secret=SECRET).describe_gap() is None
        assert AuthSettings(trust_headers=True).describe_gap() is None

    def test_settings_are_read_from_the_environment(self):
        settings = settings_from_env({
            "EVAC_JWT_SECRET": SECRET,
            "EVAC_JWT_ISSUER": "evac-central",
            "EVAC_TRUST_IDENTITY_HEADERS": "true",
        })
        assert settings.secret == SECRET
        assert settings.issuer == "evac-central"
        assert settings.trust_headers is True

    def test_header_trust_is_off_unless_explicitly_true(self):
        for value in ("", "no", "false", "0", "maybe"):
            assert settings_from_env(
                {"EVAC_TRUST_IDENTITY_HEADERS": value}).trust_headers is False


class TestClaimShapes:
    def test_permissions_may_arrive_as_a_list_or_a_string(self, settings):
        as_string = jwt.encode(
            {"sub": "w", "exp": int(time.time()) + 3_600,
             "permissions": "evac:read,evac:warden"}, SECRET, algorithm="HS256")
        assert verify(as_string, settings).may("evac:warden") is True

    def test_missing_claims_produce_an_empty_scope_not_a_crash(self, settings):
        bare = jwt.encode({"sub": "w", "exp": int(time.time()) + 3_600},
                          SECRET, algorithm="HS256")
        caller = verify(bare, settings)
        assert caller.permissions == frozenset()
        assert caller.may("evac:read") is False

    def test_a_caller_is_immutable(self, settings):
        caller = verify(issue("w", settings, permissions=["evac:read"]), settings)
        with pytest.raises(Exception):
            caller.permissions = frozenset({"evac:admin"})
