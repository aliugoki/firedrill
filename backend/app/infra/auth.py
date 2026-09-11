"""Who is calling, and how much of that we actually know.

Two modes, and which one is the default matters more than either implementation.

**JWT (the default).** The caller presents a signed token. Permissions and zone
assignments are claims inside it, so a warden cannot widen their own scope by
editing a header.

**Trusted headers (opt-in only).** A gateway in front of the service has already
verified the caller and passes the result on. This is a real deployment shape
and it is supported — but it must be turned on deliberately, because a service
that trusts `X-Permissions` from anyone who can reach it has no authentication
at all. Before this module existed, that was the behaviour, and the security
document said so. Now the insecure mode has a name and a switch.

The verification is deliberately narrow:

  * one algorithm, configured, never read from the token. Taking `alg` from the
    thing you are verifying is how `alg: none` and HS256/RS256 confusion work;
  * expiry always checked, with no "unless it is missing" branch;
  * issuer and audience checked when configured;
  * permissions come from claims, never from the request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class AuthError(Exception):
    """The caller could not be established. Always a 401, never a 500."""


class AuthConfigError(Exception):
    """The service is misconfigured in a way that would be unsafe to run."""


@dataclass(frozen=True, slots=True)
class Caller:
    """Everything the service knows about who is asking."""

    user_id: str
    permissions: frozenset = frozenset()
    zones: frozenset = frozenset()
    tenant_id: str | None = None
    expires_at: int | None = None
    via: str = "jwt"

    def may(self, permission: str) -> bool:
        return permission in self.permissions

    def covers(self, zone_id: str) -> bool:
        """A warden is scoped to the zones they were assigned.

        An empty set means unrestricted — a roving supervisor. That fails open,
        which is the wrong direction, so `docs/EVAC120_SECURITY.md` records it
        and sites are told to treat an unassigned warden as a finding.
        """
        return not self.zones or zone_id in self.zones


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """How to establish a caller.

    `trust_headers` defaults to False. A service that trusts identity headers
    from anyone who can reach it has no authentication, and that must be a
    decision somebody made rather than the state you end up in by not
    configuring anything.
    """

    secret: str = ""
    algorithm: str = "HS256"
    issuer: str | None = None
    audience: str | None = None
    leeway_s: int = 30
    trust_headers: bool = False

    @property
    def is_configured(self) -> bool:
        return bool(self.secret) or self.trust_headers

    def describe_gap(self) -> str | None:
        if self.is_configured:
            return None
        return ("no EVAC_JWT_SECRET and headers are not trusted, so every "
                "request will be refused; set one or the other")


#: Algorithms this service will verify. HS256 only, and the token does not get a
#: vote: reading `alg` from the thing being verified is precisely how `alg: none`
#: and RS256-verified-as-HS256 work.
ALLOWED_ALGORITHMS = ("HS256", "HS384", "HS512")


def verify(token: str, settings: AuthSettings, *, now: int | None = None) -> Caller:
    """Turn a bearer token into a caller, or raise `AuthError`."""
    import jwt

    if not settings.secret:
        raise AuthConfigError(
            "no signing secret is configured; refusing to verify a token "
            "against nothing")
    if settings.algorithm not in ALLOWED_ALGORITHMS:
        raise AuthConfigError(
            f"{settings.algorithm} is not an allowed algorithm; this service "
            f"verifies only {', '.join(ALLOWED_ALGORITHMS)}")
    if not token:
        raise AuthError("no token presented")

    options = {
        "require": ["exp", "sub"],
        "verify_exp": True,
        "verify_signature": True,
    }
    kwargs: dict = {}
    if settings.issuer:
        kwargs["issuer"] = settings.issuer
        options["verify_iss"] = True
    if settings.audience:
        kwargs["audience"] = settings.audience
        options["verify_aud"] = True
    else:
        # Without a configured audience there is nothing to check it against,
        # and PyJWT would otherwise reject any token that carries one.
        options["verify_aud"] = False

    try:
        claims = jwt.decode(
            token, settings.secret,
            # A tuple, from configuration. Never `claims["alg"]`.
            algorithms=[settings.algorithm],
            options=options, leeway=settings.leeway_s, **kwargs)
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("the token has expired") from exc
    except jwt.InvalidSignatureError as exc:
        raise AuthError("the token signature does not verify") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise AuthError(f"the token is missing a required claim: {exc.claim}") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"the token is not valid: {exc}") from exc

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise AuthError("the token has no subject, so nothing can be attributed")

    return Caller(
        user_id=subject,
        permissions=frozenset(_string_list(claims.get("permissions"))),
        zones=frozenset(_string_list(claims.get("zones"))),
        tenant_id=claims.get("tenant_id"),
        expires_at=claims.get("exp"),
        via="jwt")


def from_headers(user_id: str, permissions: str, zones: str,
                 settings: AuthSettings, tenant_id: str = "") -> Caller:
    """Take the caller a gateway has already established.

    Refuses unless `trust_headers` is on. The failure message names the setting,
    because the alternative is somebody spending an afternoon on a 401 that is
    really a deployment decision nobody made.
    """
    if not settings.trust_headers:
        raise AuthError(
            "identity headers are not trusted by this service; present a "
            "bearer token, or set EVAC_TRUST_IDENTITY_HEADERS=true if a "
            "gateway in front of it has already authenticated the caller")
    if not user_id:
        raise AuthError("no caller identity")
    return Caller(
        user_id=user_id,
        permissions=frozenset(p.strip() for p in permissions.split(",") if p.strip()),
        zones=frozenset(z.strip() for z in zones.split(",") if z.strip()),
        # A gateway had no way to say which tenant it had authenticated, so
        # every gateway caller was tenantless and every tenant check passed.
        tenant_id=tenant_id.strip() or None,
        via="gateway")


def issue(user_id: str, settings: AuthSettings, *,
          permissions=(), zones=(), tenant_id: str | None = None,
          ttl_s: int = 43_200, now: int | None = None) -> str:
    """Mint a token. For tests, for a warden device, and for local operation.

    The default lifetime is twelve hours: a warden carries a device through a
    whole drill day, and a token that expires mid-evacuation would log them out
    at the assembly point.
    """
    import jwt

    if not settings.secret:
        raise AuthConfigError("cannot issue a token without a signing secret")

    now = now if now is not None else int(time.time())
    claims: dict = {
        "sub": user_id,
        "iat": now,
        "exp": now + ttl_s,
        "permissions": list(permissions),
        "zones": list(zones),
    }
    if tenant_id:
        claims["tenant_id"] = tenant_id
    if settings.issuer:
        claims["iss"] = settings.issuer
    if settings.audience:
        claims["aud"] = settings.audience
    return jwt.encode(claims, settings.secret, algorithm=settings.algorithm)


def settings_from_env(env: dict) -> AuthSettings:
    return AuthSettings(
        secret=env.get("EVAC_JWT_SECRET", ""),
        algorithm=env.get("EVAC_JWT_ALGORITHM", "HS256"),
        issuer=env.get("EVAC_JWT_ISSUER") or None,
        audience=env.get("EVAC_JWT_AUDIENCE") or None,
        leeway_s=int(env.get("EVAC_JWT_LEEWAY_S", "30")),
        trust_headers=str(
            env.get("EVAC_TRUST_IDENTITY_HEADERS", "")).lower() in ("1", "true", "yes"),
    )


def _string_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]
