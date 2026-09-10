"""The event log and its companions.

Revision ID: 0001_event_log
Revises:
Create Date: 2026-09-10

One table matters here. `evac_events` is the source of truth: presence,
identity, the ledger, the board and the report are all derived from it. If it
survives, a node can be rebuilt from nothing.

There are deliberately no foreign keys between these tables. An event whose
drill row failed to write is still evidence, and a constraint that discards it
to protect referential tidiness would trade the thing that matters for the thing
that looks correct.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_event_log"
down_revision = None
branch_labels = None
depends_on = None

Json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
# SQLite only autoincrements a column declared exactly `INTEGER PRIMARY KEY`.
AutoKey = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "evac_events",
        sa.Column("id", AutoKey, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("site_id", sa.String(64), nullable=False),
        sa.Column("drill_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("ts_ms", sa.BigInteger, nullable=False),
        sa.Column("subject", sa.String(128), nullable=True),
        sa.Column("payload", Json, nullable=False, server_default="{}"),
        sa.Column("ingested_at_ms", sa.BigInteger, nullable=False),
        # Idempotency enforced by the database rather than by the application
        # remembering. Scoped to the drill: a pipeline restarted between two
        # drills begins its sequence again, and a global constraint would reject
        # the second drill's first events as duplicates of the first drill's.
        sa.UniqueConstraint("drill_id", "source", "seq",
                            name="uq_evac_events_drill_source_seq"),
    )
    op.create_index("ix_evac_events_tenant_id", "evac_events", ["tenant_id"])
    op.create_index("ix_evac_events_site_id", "evac_events", ["site_id"])
    op.create_index("ix_evac_events_drill_id", "evac_events", ["drill_id"])
    # The only query shape that matters for correctness: a whole drill, in the
    # order things happened.
    op.create_index("ix_evac_events_replay", "evac_events",
                    ["drill_id", "ts_ms", "id"])
    op.create_index("ix_evac_events_subject", "evac_events",
                    ["drill_id", "subject"])

    op.create_table(
        "drills",
        sa.Column("drill_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("site_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_ms", sa.BigInteger, nullable=False),
        sa.Column("started_ms", sa.BigInteger, nullable=True),
        sa.Column("completed_ms", sa.BigInteger, nullable=True),
        sa.Column("roster_snapshot", Json, nullable=False, server_default="{}"),
    )
    op.create_index("ix_drills_tenant_id", "drills", ["tenant_id"])
    op.create_index("ix_drills_site_id", "drills", ["site_id"])
    op.create_index("ix_drills_site_created", "drills", ["site_id", "created_ms"])

    op.create_table(
        "zone_kinds",
        sa.Column("site_id", sa.String(64), primary_key=True),
        sa.Column("zone_id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        # A zone's kind is a safety decision: tag a corridor ASSEMBLY and
        # everyone standing in it is marked safe. So it is attributable.
        sa.Column("tagged_by", sa.String(64), nullable=False),
        sa.Column("tagged_at_ms", sa.BigInteger, nullable=False),
    )

    op.create_table(
        "audit_entries",
        sa.Column("entry_id", sa.String(36), primary_key=True),
        sa.Column("drill_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("ts_ms", sa.BigInteger, nullable=False),
        sa.Column("subject", sa.String(128), nullable=True),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("before", Json, nullable=True),
        sa.Column("after", Json, nullable=True),
        sa.Column("context", Json, nullable=False, server_default="{}"),
    )
    op.create_index("ix_audit_entries_drill_id", "audit_entries", ["drill_id"])
    op.create_index("ix_audit_entries_actor_id", "audit_entries", ["actor_id"])
    op.create_index("ix_audit_entries_ts_ms", "audit_entries", ["ts_ms"])


def downgrade() -> None:
    # Present because Alembic expects it. Running it destroys the only record of
    # a drill, so it exists for a development reset and nothing else.
    op.drop_table("audit_entries")
    op.drop_table("zone_kinds")
    op.drop_table("drills")
    op.drop_table("evac_events")
