"""The schema. Small, explicit, and mostly one table.

SQLAlchemy Core rather than the ORM: an append-only event log is a poor fit for
object mapping, and the queries here are a handful of inserts and one ordered
scan.

**JSON, not JSONB, with a Postgres variant.** The payload is written and read
whole and is never queried by key, so the only thing JSONB buys is indexing
nobody needs — and using the plain type means the schema is identical on SQLite,
which is what lets the tests exercise real SQL instead of a mock.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

#: JSON everywhere, JSONB on Postgres. Identical semantics for what this stores.
Json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

#: An auto-assigned surrogate key. SQLite only autoincrements a column declared
#: exactly `INTEGER PRIMARY KEY`, so a BIGINT key is silently never populated
#: and every insert fails a NOT NULL check. SQLite's INTEGER is 64-bit anyway,
#: so the variant costs nothing and Postgres still gets BIGSERIAL.
AutoKey = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


evac_events = sa.Table(
    "evac_events", metadata,
    sa.Column("id", AutoKey, primary_key=True, autoincrement=True),
    sa.Column("event_id", sa.String(36), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
    sa.Column("site_id", sa.String(64), nullable=False, index=True),
    sa.Column("drill_id", sa.String(64), nullable=False, index=True),
    sa.Column("source", sa.String(128), nullable=False),
    sa.Column("source_kind", sa.String(16), nullable=False),
    sa.Column("seq", sa.BigInteger, nullable=False),
    sa.Column("type", sa.String(48), nullable=False),
    sa.Column("ts_ms", sa.BigInteger, nullable=False),
    sa.Column("subject", sa.String(128), nullable=True),
    sa.Column("payload", Json, nullable=False, server_default="{}"),
    sa.Column("ingested_at_ms", sa.BigInteger, nullable=False),

    # Idempotency, enforced by the database rather than by the application
    # remembering. Scoped to the drill rather than global: a pipeline restarted
    # between two drills on the same day begins its sequence again, and a global
    # constraint would silently reject the second drill's first events as
    # duplicates of the first drill's.
    sa.UniqueConstraint("drill_id", "source", "seq",
                        name="uq_evac_events_drill_source_seq"),

    # Replay reads a whole drill in time order. This is the only query shape
    # that matters for correctness, so it is the only composite index.
    sa.Index("ix_evac_events_replay", "drill_id", "ts_ms", "id"),
    sa.Index("ix_evac_events_subject", "drill_id", "subject"),
)


drills = sa.Table(
    "drills", metadata,
    sa.Column("drill_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
    sa.Column("site_id", sa.String(64), nullable=False, index=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_ms", sa.BigInteger, nullable=False),
    sa.Column("started_ms", sa.BigInteger, nullable=True),
    sa.Column("completed_ms", sa.BigInteger, nullable=True),
    # The roster as it stood when the drill began. Frozen deliberately: a roster
    # that shifts mid-drill moves the denominator under the operator, and a
    # person who "disappeared" because HR updated a record looks identical on
    # screen to one who disappeared in a stairwell.
    sa.Column("roster_snapshot", Json, nullable=False, server_default="{}"),
    sa.Index("ix_drills_site_created", "site_id", "created_ms"),
)


zone_kinds = sa.Table(
    "zone_kinds", metadata,
    sa.Column("site_id", sa.String(64), primary_key=True),
    sa.Column("zone_id", sa.String(64), primary_key=True),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("tagged_by", sa.String(64), nullable=False),
    sa.Column("tagged_at_ms", sa.BigInteger, nullable=False),
    # Who decided, and when. A zone's kind is a safety decision — tag a corridor
    # as ASSEMBLY and everyone standing in it is marked safe — so it should be
    # attributable rather than anonymous configuration.
    #
    # It is not yet. Nothing reads or writes this table: an edge node takes its
    # tags from `EVAC_ZONE_KINDS`, which is exactly the anonymous configuration
    # the sentence above argues against. The table is the shape the attributable
    # flow will have, and until somebody builds the endpoint that writes it,
    # "who tagged the corridor" has no answer.
)


audit_entries = sa.Table(
    "audit_entries", metadata,
    sa.Column("entry_id", sa.String(36), primary_key=True),
    sa.Column("drill_id", sa.String(64), nullable=True, index=True),
    sa.Column("action", sa.String(48), nullable=False),
    sa.Column("actor_id", sa.String(64), nullable=False, index=True),
    sa.Column("ts_ms", sa.BigInteger, nullable=False, index=True),
    sa.Column("subject", sa.String(128), nullable=True),
    sa.Column("summary", sa.Text, nullable=False, server_default=""),
    sa.Column("before", Json, nullable=True),
    sa.Column("after", Json, nullable=True),
    sa.Column("context", Json, nullable=False, server_default="{}"),
)


#: Every table, in dependency order. There are no foreign keys between them on
#: purpose: an event whose drill row failed to write is still evidence, and a
#: constraint that discards it to protect referential tidiness would be trading
#: the thing that matters for the thing that looks correct.
ALL_TABLES = (evac_events, drills, zone_kinds, audit_entries)
