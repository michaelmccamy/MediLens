"""Database session setup.

One engine and one session factory for the whole process, built from the
configured DATABASE_URL. Kept separate from models.py so retrieval and audit
code can import sessions without pulling in every table definition.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from medilens.config import Settings
from medilens.db.models import Base


def build_engine(settings: Settings) -> Engine:
    engine = create_engine(settings.database_url)
    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    session_factory = sessionmaker(bind=engine)
    return session_factory


def create_all_tables(engine: Engine) -> None:
    """Create any missing tables for the operational schema.

    Idempotent: create_all only creates tables that do not already exist, so
    running the ingest command repeatedly is safe. This is the MVP schema
    bootstrap; a real migration tool replaces it before the schema evolves in
    production.
    """
    Base.metadata.create_all(engine)


def upgrade_schema(engine: Engine) -> None:
    """Apply additive column upgrades that create_all cannot make.

    create_all only creates missing tables; it never alters existing ones, so
    a database bootstrapped before a column existed silently diverges from the
    models and inserts fail at the driver level. This helper adds the known
    additive columns when absent. Idempotent, additive only (never drops or
    rewrites data), and a stopgap until a real migration tool is adopted.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "payer_policy" not in inspector.get_table_names():
        return

    existing_columns = set()
    for column in inspector.get_columns("payer_policy"):
        existing_columns.add(column["name"])

    statements: list[str] = []
    if "service" not in existing_columns:
        statements.append(
            "ALTER TABLE payer_policy "
            "ADD COLUMN service VARCHAR(256) NOT NULL DEFAULT ''"
        )
    if "service_keywords" not in existing_columns:
        statements.append(
            "ALTER TABLE payer_policy "
            "ADD COLUMN service_keywords VARCHAR(256) NOT NULL DEFAULT ''"
        )

    if len(statements) == 0:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
