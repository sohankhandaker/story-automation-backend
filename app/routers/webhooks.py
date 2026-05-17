"""
GitHub webhook receiver (optional — the polling engine handles reviewer actions).
This router handles webhooks if you expose the backend publicly via Render.com.
"""
import hashlib
import hmac
import json
import logging
import threading
from fastapi import APIRouter, Request, HTTPException, Header
from ..config import settings
from ..database import SessionLocal
from .. import models
from ..services import engine

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

_processed_deliveries: set[str] = set()


def _verify_signature(body: bytes, signature: str) -> bool:
    if not settings.github_token:
        return True  # Skip validation if no webhook secret configured
    secret = settings.secret_key.encode()
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    body = await request.body()

    # Idempotency
    if x_github_delivery and x_github_delivery in _processed_deliveries:
        return {"status": "already_processed"}
    if x_github_delivery:
        _processed_deliveries.add(x_github_delivery)

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Handle projects_v2_item — status change to Done
    if x_github_event == "projects_v2_item":
        action = payload.get("action")
        if action == "edited":
            changes = payload.get("changes", {})
            field_value = changes.get("field_value", {})
            new_value = field_value.get("to", {})
            if isinstance(new_value, dict) and new_value.get("name", "").lower() == "done":
                item_id = payload.get("projects_v2_item", {}).get("node_id")
                actor = payload.get("sender", {}).get("login", "")
                _handle_done_webhook(item_id, actor)

    # Handle issue_comment — reviewer feedback
    elif x_github_event == "issue_comment" and payload.get("action") == "created":
        comment = payload.get("comment", {})
        issue = payload.get("issue", {})
        commenter = comment.get("user", {}).get("login", "")
        issue_number = issue.get("number")
        comment_id = comment.get("id")
        comment_body = comment.get("body", "")
        if issue_number and commenter:
            _handle_comment_webhook(issue_number, commenter, comment_id, comment_body)

    return {"status": "ok"}


def _handle_done_webhook(item_id: str, actor_login: str):
    db = SessionLocal()
    try:
        task = db.query(models.Task).filter(
            models.Task.github_project_item_id == item_id,
            models.Task.status.in_(["In Review", "Changes Requested"]),
        ).first()
        if task and task.reviewer_github_username:
            if actor_login.lower() == task.reviewer_github_username.lower():
                t = threading.Thread(target=engine.run_done_by_reviewer, args=(task.id,), daemon=True)
                t.start()
    finally:
        db.close()


def _handle_comment_webhook(issue_number: int, commenter: str, comment_id: int, comment_body: str):
    db = SessionLocal()
    try:
        task = db.query(models.Task).filter(
            models.Task.github_issue_number == issue_number,
            models.Task.reviewer_github_username.isnot(None),
            models.Task.status.in_(["In Review"]),
        ).first()
        if task and task.reviewer_github_username.lower() == commenter.lower():
            processed = set(str(c) for c in (task.processed_comment_ids or []))
            if str(comment_id) not in processed:
                t = threading.Thread(
                    target=engine.run_review_comment,
                    args=(task.id, [comment_body], [comment_id]),
                    daemon=True,
                )
                t.start()
    finally:
        db.close()
