"""intel_app DML grants (spec 10 §1; deferred from Task 2).

The 0001 role stub creates ``intel_app`` NOLOGIN without table privileges.
RLS policies are ``TO intel_app``; without GRANTs the role cannot DML even
its own scoped rows. This migration is the production stand-in that
tests/integration previously applied ad hoc:

- USAGE on schema public
- SELECT/INSERT/UPDATE/DELETE on every existing table (and default
  privileges so later migrations keep working)
- USAGE/SELECT on sequences
- membership of the migrating role in ``intel_app`` so worker/API
  connections can ``SET LOCAL ROLE intel_app`` (the dispatcher stays
  the table-owning role and never SET ROLE)

intel_app is still not LOGIN, not SUPERUSER, not BYPASSRLS, and does
not own tables (spec 10 §1).

Revision ID: 0004_intel_app_grants
Revises: 0003_index_generation
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_intel_app_grants"
down_revision: str | None = "0003_index_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Scope constants kept so the schema-manifest loader can import this
# module like 0001–0003. This revision creates no tables.
RLS_OWNER_TABLES: tuple[str, ...] = ()
RLS_INDUSTRY_TABLES: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN"
        " IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'intel_app') THEN"
        " CREATE ROLE intel_app NOLOGIN NOSUPERUSER NOBYPASSRLS;"
        " END IF;"
        " END $$;"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO intel_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA"
        " public TO intel_app"
    )
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO intel_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT,"
        " UPDATE, DELETE ON TABLES TO intel_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT"
        " ON SEQUENCES TO intel_app"
    )
    # Connecting role (migration/table owner) must be a member of intel_app
    # to SET LOCAL ROLE. Superuser membership is how the dispatcher still
    # sees the whole queue while FORCE RLS + TO intel_app policies apply
    # to the app role.
    op.execute(
        "DO $$ BEGIN"
        " EXECUTE format('GRANT intel_app TO %I', current_user);"
        " EXCEPTION WHEN duplicate_object THEN NULL;"
        " END $$;"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT,"
        " UPDATE, DELETE ON TABLES FROM intel_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT"
        " ON SEQUENCES FROM intel_app"
    )
    op.execute(
        "REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM intel_app"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA"
        " public FROM intel_app"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM intel_app")
    # Membership and the role itself stay: dropping a role that other
    # sessions may hold is a deployment decision (same as 0001).
