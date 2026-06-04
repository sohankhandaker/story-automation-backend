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
        ("meeting_notes", "github_issue_url", "VARCHAR"),
        ("meeting_notes", "github_issue_number", "INTEGER"),
        ("meeting_notes", "github_issue_node_id", "VARCHAR"),
        ("meeting_notes", "github_project_item_id", "VARCHAR"),
        ("meeting_notes", "reviewer_github_username", "VARCHAR"),
        ("meeting_notes", "reviewer_name", "VARCHAR"),
        ("meeting_notes", "reviewers", "JSON"),
        ("meeting_notes", "github_last_checked_at", "TIMESTAMP"),
        ("meeting_notes", "processed_comment_ids", "JSON"),
        # prd_documents new columns
        ("prd_documents", "reviewers", "JSON"),
        # BRD Q&A: pending change proposals awaiting reviewer confirmation
        ("meeting_notes", "pending_brd_changes", "JSON"),
        # PRD Q&A: same pattern as BRD
        ("prd_documents", "pending_prd_changes", "JSON"),
        # GitHub file hosting for direct .md downloads
        ("meeting_notes", "github_file_url", "VARCHAR"),
        ("meeting_notes", "github_file_raw_url", "VARCHAR"),
        ("prd_documents", "github_file_url", "VARCHAR"),
        ("prd_documents", "github_file_raw_url", "VARCHAR"),
        # Project layer
        ("meeting_notes", "project_id", "VARCHAR"),
        ("projects", "url", "VARCHAR"),
        ("projects", "customer_id", "VARCHAR"),
        # Per-project GitHub Project board (Option B)
        ("users", "avatar_url", "VARCHAR"),
        ("projects", "github_project_node_id", "VARCHAR"),
        ("projects", "github_project_url", "VARCHAR"),
        ("projects", "github_status_field_id", "VARCHAR"),
        ("projects", "github_status_options", "JSON"),
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
