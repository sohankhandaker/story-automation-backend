from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """Add new columns to existing tables without dropping data."""
    new_cols = [
        ("users", "gh_token", "VARCHAR"),
        ("users", "gh_owner", "VARCHAR"),
        ("users", "gh_repo", "VARCHAR"),
        ("users", "gh_project_number", "INTEGER"),
        ("tasks", "reviewer_github_usernames", "JSON"),
        ("meeting_notes", "brd_generation_phase", "INTEGER"),
        ("meeting_notes", "status", "VARCHAR"),
        ("meeting_notes", "current_version_number", "INTEGER"),
    ]
    is_sqlite = settings.database_url.startswith("sqlite")
    with engine.connect() as conn:
        for table, col, col_type in new_cols:
            try:
                if is_sqlite:
                    conn.execute(__import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                else:
                    conn.execute(__import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"))
                conn.commit()
            except Exception:
                conn.rollback()
