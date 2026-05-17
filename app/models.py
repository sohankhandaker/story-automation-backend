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
