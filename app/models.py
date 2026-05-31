import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, Text, Boolean, JSON, ForeignKey
from sqlalchemy.orm import relationship
from .database import Base


def gen_id():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    github_username = Column(String, nullable=True)
    device_tokens = Column(JSON, default=list)
    reviewer_list = Column(JSON, default=list)  # [{name, github_username}]
    # Per-user GitHub project config (overrides env vars when set)
    gh_token = Column(String, nullable=True)
    gh_owner = Column(String, nullable=True)
    gh_repo = Column(String, nullable=True)
    gh_project_number = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tasks = relationship("Task", back_populates="creator")
    messages = relationship("ChatMessage", back_populates="sender")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, default=gen_id)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String, default="Backlog")
    priority = Column(String, default="Medium")
    creator_id = Column(String, ForeignKey("users.id"))
    reviewer_github_username = Column(String, nullable=True)
    reviewer_name = Column(String, nullable=True)
    reviewer_github_usernames = Column(JSON, default=list)  # all reviewers
    github_issue_url = Column(String, nullable=True)
    github_issue_number = Column(Integer, nullable=True)
    github_issue_node_id = Column(String, nullable=True)
    github_project_item_id = Column(String, nullable=True)
    labels = Column(JSON, default=list)
    current_phase = Column(Integer, default=0)
    total_phases = Column(Integer, default=10)
    story_phases = Column(JSON, default=list)
    max_review_cycles = Column(Integer, default=5)
    current_review_cycle = Column(Integer, default=0)
    github_last_checked_at = Column(DateTime, nullable=True)
    processed_comment_ids = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = relationship("User", back_populates="tasks")
    messages = relationship("ChatMessage", back_populates="task", order_by="ChatMessage.created_at")
    review_cycles = relationship("ReviewCycle", back_populates="task")
    activity_logs = relationship("ActivityLog", back_populates="task")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True, default=gen_id)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    sender_id = Column(String, ForeignKey("users.id"), nullable=True)
    sender_type = Column(String, default="User")  # User / Agent / System
    content = Column(Text, nullable=False)
    mentioned_github_username = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="messages")
    sender = relationship("User", back_populates="messages")


class ReviewCycle(Base):
    __tablename__ = "review_cycles"

    id = Column(String, primary_key=True, default=gen_id)
    task_id = Column(String, ForeignKey("tasks.id"))
    cycle_number = Column(Integer)
    reviewer_github_username = Column(String)
    status = Column(String, default="Pending")
    review_comments = Column(JSON, default=list)
    agent_change_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    task = relationship("Task", back_populates="review_cycles")


class MeetingNote(Base):
    __tablename__ = "meeting_notes"

    id = Column(String, primary_key=True, default=gen_id)
    creator_id = Column(String, ForeignKey("users.id"))
    title = Column(String, nullable=True)
    raw_notes = Column(Text, nullable=False)
    wiki_url = Column(String, nullable=True)
    brd_draft = Column(Text, nullable=True)
    brd_generation_phase = Column(Integer, nullable=True)
    status = Column(String, default="Draft")           # Draft | In Review | Changes Requested | Approved
    current_version_number = Column(Integer, default=0)
    # GitHub integration
    github_issue_url = Column(String, nullable=True)
    github_issue_number = Column(Integer, nullable=True)
    github_issue_node_id = Column(String, nullable=True)
    github_project_item_id = Column(String, nullable=True)
    reviewer_github_username = Column(String, nullable=True)
    reviewer_name = Column(String, nullable=True)
    reviewers = Column(JSON, default=list)  # [{github_username, name, status}]
    github_last_checked_at = Column(DateTime, nullable=True)
    processed_comment_ids = Column(JSON, default=list)
    pending_brd_changes = Column(JSON, nullable=True)  # {feedback, summary, comment_id} — awaiting CONFIRM CHANGES
    github_file_url = Column(String, nullable=True)    # HTML URL of BRD .md file in repo
    github_file_raw_url = Column(String, nullable=True)  # raw.githubusercontent.com download URL
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = relationship("User")
    versions = relationship("BrdVersion", back_populates="note", order_by="BrdVersion.version_number")
    entries = relationship("NoteEntry", back_populates="note", order_by="NoteEntry.created_at")
    attachments = relationship("NoteAttachment", back_populates="note", order_by="NoteAttachment.created_at")
    prd = relationship("PrdDocument", back_populates="note", uselist=False)


class NoteEntry(Base):
    __tablename__ = "note_entries"

    id = Column(String, primary_key=True, default=gen_id)
    note_id = Column(String, ForeignKey("meeting_notes.id"))
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    note = relationship("MeetingNote", back_populates="entries")


class NoteAttachment(Base):
    __tablename__ = "note_attachments"

    id = Column(String, primary_key=True, default=gen_id)
    note_id = Column(String, ForeignKey("meeting_notes.id"))
    filename = Column(String, nullable=False)
    mime_type = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    content_text = Column(Text, nullable=True)  # extracted text included in BRD context
    created_at = Column(DateTime, default=datetime.utcnow)

    note = relationship("MeetingNote", back_populates="attachments")


class BrdVersion(Base):
    __tablename__ = "brd_versions"

    id = Column(String, primary_key=True, default=gen_id)
    note_id = Column(String, ForeignKey("meeting_notes.id"))
    version_number = Column(Integer, nullable=False)
    brd_markdown = Column(Text, nullable=False)
    change_summary = Column(String, nullable=True)
    changed_sections = Column(JSON, default=list)
    reviewer_comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    note = relationship("MeetingNote", back_populates="versions")


class PrdDocument(Base):
    __tablename__ = "prd_documents"

    id = Column(String, primary_key=True, default=gen_id)
    note_id = Column(String, ForeignKey("meeting_notes.id"), unique=True)
    prd_draft = Column(Text, nullable=True)
    prd_generation_phase = Column(Integer, nullable=True)
    status = Column(String, default="Draft")
    current_version_number = Column(Integer, default=0)
    github_issue_url = Column(String, nullable=True)
    github_issue_number = Column(Integer, nullable=True)
    github_issue_node_id = Column(String, nullable=True)
    github_project_item_id = Column(String, nullable=True)
    reviewer_github_username = Column(String, nullable=True)
    reviewer_name = Column(String, nullable=True)
    reviewers = Column(JSON, default=list)  # [{github_username, name, status}]
    github_last_checked_at = Column(DateTime, nullable=True)
    processed_comment_ids = Column(JSON, default=list)
    pending_prd_changes = Column(JSON, nullable=True)  # {feedback, summary} — awaiting CONFIRM CHANGES
    github_file_url = Column(String, nullable=True)    # HTML URL of PRD .md file in repo
    github_file_raw_url = Column(String, nullable=True)  # raw.githubusercontent.com download URL
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    note = relationship("MeetingNote", back_populates="prd")
    versions = relationship("PrdVersion", back_populates="prd", order_by="PrdVersion.version_number")


class PrdVersion(Base):
    __tablename__ = "prd_versions"

    id = Column(String, primary_key=True, default=gen_id)
    prd_id = Column(String, ForeignKey("prd_documents.id"))
    version_number = Column(Integer, nullable=False)
    prd_markdown = Column(Text, nullable=False)
    change_summary = Column(String, nullable=True)
    changed_sections = Column(JSON, default=list)
    reviewer_comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    prd = relationship("PrdDocument", back_populates="versions")


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(String, primary_key=True, default=gen_id)
    task_id = Column(String, ForeignKey("tasks.id"))
    action = Column(String)
    trigger = Column(String, nullable=True)
    input_summary = Column(Text, nullable=True)
    output_summary = Column(Text, nullable=True)
    status = Column(String, default="Success")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="activity_logs")
