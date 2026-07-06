"""Database session setup.

One engine and one session factory for the whole process, built from the
configured DATABASE_URL. Kept separate from models.py so retrieval and audit
code can import sessions without pulling in every table definition.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from medilens.config import Settings


def build_engine(settings: Settings) -> Engine:
    engine = create_engine(settings.database_url)
    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    session_factory = sessionmaker(bind=engine)
    return session_factory
