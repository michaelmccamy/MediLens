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
