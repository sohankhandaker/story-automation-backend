from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, EmailStr


# ── Auth ──────────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class ReviewerItem(BaseModel):
    name: str
    github_username: str
    email: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    github_username: Optional[str]
    reviewer_list: List[ReviewerItem] = []
    gh_owner: Optional[str] = None
    gh_repo: Optional[str] = None
    gh_project_number: Optional[int] = None
    gh_token: Optional[str] = None

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class SettingsUpdate(BaseModel):
    github_username: Optional[str] = None
    reviewer_list: Optional[List[ReviewerItem]] = None
    gh_owner: Optional[str] = None
    gh_repo: Optional[str] = None
    gh_project_number: Optional[int] = None
    gh_token: Optional[str] = None


# ── Task ──────────────────────────────────────────────────────────────────────

class StoryPhase(BaseModel):
    phase: int
    name: str
    content: str = ""
    completed: bool = False


class TaskCreate(BaseModel):
    message: str
    github_repo: Optional[str] = None
    github_project_number: Optional[int] = None


class TaskResponse(BaseModel):
    id: str
    title: str
    description: Optional[str]
    status: str
    priority: str
    creator_id: str
    reviewer_github_username: Optional[str]
    reviewer_name: Optional[str]
    github_issue_url: Optional[str]
    github_issue_number: Optional[int]
    current_phase: int
    total_phases: int
    story_phases: List[Any] = []
    max_review_cycles: int
    current_review_cycle: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TaskListResponse(BaseModel):
    tasks: List[TaskResponse]
    total: int


class TaskStatusUpdate(BaseModel):
    status: str


# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatMessageCreate(BaseModel):
    message: str


class ChatMessageResponse(BaseModel):
    id: str
    task_id: Optional[str]
    sender_type: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class ChatResponse(BaseModel):
    messages: List[ChatMessageResponse]


# ── Meeting Notes ─────────────────────────────────────────────────────────────

class NotesEnhanceRequest(BaseModel):
    raw_notes: str
    wiki_url: Optional[str] = None


class MeetingNoteResponse(BaseModel):
    id: str
    title: Optional[str]
    raw_notes: str
    wiki_url: Optional[str]
    brd_draft: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MeetingNotesListResponse(BaseModel):
    notes: List[MeetingNoteResponse]
    total: int


# ── Device token ──────────────────────────────────────────────────────────────

class DeviceTokenCreate(BaseModel):
    device_token: str
    platform: str  # android | ios
