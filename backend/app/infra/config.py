"""Reading the environment, in one place rather than two.

The edge process and the API process are separate by design -- a crash in one
must not take the other with it -- and they read the same settings. Two copies
of that reading is two places for them to drift, and a drift here means the two
halves of one node disagreeing about which database they are talking to.
"""

from __future__ import annotations


def database_url(env: dict) -> str:
    """Where to persist, or "" for nowhere.

    An explicit URL wins. Otherwise a URL is assembled, but only if a password
    was supplied: a password with a default is a password that ends up in
    production, and a node that silently persists nowhere is worse than one
    that says it has no database.
    """
    explicit = env.get("EVAC_DATABASE_URL")
    if explicit:
        return explicit

    password = env.get("EVAC_DB_PASSWORD")
    if not password:
        return ""

    host = env.get("EVAC_DB_HOST", "localhost")
    port = env.get("EVAC_DB_PORT", "5432")
    name = env.get("EVAC_DB_NAME", "firedrill")
    user = env.get("EVAC_DB_USER", "firedrill")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def assembly_zones(env: dict) -> frozenset:
    raw = env.get("EVAC_ASSEMBLY_ZONES", "")
    return frozenset(zone.strip() for zone in raw.split(",") if zone.strip())
