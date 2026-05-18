import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services import github as gh, agent, email_service
from ..services.github import cfg_for_user

router = APIRouter(prefix="/api/tasks", tags=["chat"])

MENTION_RE = re.compile(r"@(\w[\w-]*)")


@router.post("/{task_id}/chat", response_model=schemas.ChatMessageResponse)
def send_chat(
    task_id: str,
    body: schemas.ChatMessageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = db.query(models.Task).filter(
        models.Task.id == task_id,
        models.Task.creator_id == current_user.id,
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Save user message
    user_msg = models.ChatMessage(
        task_id=task.id,
        sender_id=current_user.id,
        sender_type="User",
        content=body.message,
    )
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    # ── Check for @mention (review request) ──
    mention_match = MENTION_RE.search(body.message)
    if mention_match and task.status == "In Review":
        github_username = mention_match.group(1)
        already_notified = (
            task.reviewer_github_username
            and task.reviewer_github_username.lower() == github_username.lower()
        )

        # Find reviewer name + email from user's reviewer list
        reviewer_name = github_username
        reviewer_email = None
        for r in (current_user.reviewer_list or []):
            if r.get("github_username", "").lower() == github_username.lower():
                reviewer_name = r.get("name", github_username)
                reviewer_email = r.get("email") or None
                break

        # Store reviewer on task
        task.reviewer_github_username = github_username
        task.reviewer_name = reviewer_name
        db.commit()

        # Post GitHub comment @mentioning reviewer with clear instructions
        user_cfg = cfg_for_user(current_user)
        if already_notified:
            agent_reply = f"@{github_username} has already been notified. They will see any new comments you leave here."
        else:
            agent_reply = f"Review requested. @{github_username} has been notified via GitHub."
            if task.github_issue_number:
                try:
                    gh.add_comment(
                        task.github_issue_number,
                        f"👋 @{github_username} — your review is requested for this user story.\n\n"
                        f"**The full story is in the issue description above** (scroll to the top).\n\n"
                        f"---\n\n"
                        f"### How to respond\n\n"
                        f"**To give feedback:**\n"
                        f"Leave a comment below describing what should be changed. "
                        f"The AI agent will update the story and notify you.\n\n"
                        f"**To approve:**\n"
                        f"Reply with a comment containing **`APPROVED`** (or `LGTM` / `Looks good`) "
                        f"and the story will be marked as Done automatically.\n\n"
                        f"---\n"
                        f"*Requested by @{current_user.github_username or current_user.name}*",
                        cfg=user_cfg,
                    )
                except Exception as e:
                    agent_reply += f"\n\n(GitHub notification failed: {e})"

            # ── Direct email notification to reviewer ────────────────────────
            if reviewer_email and task.github_issue_url:
                sent = email_service.send_review_request(
                    reviewer_name=reviewer_name,
                    reviewer_email=reviewer_email,
                    requester_name=current_user.name,
                    story_title=task.title,
                    github_issue_url=task.github_issue_url,
                )
                if sent:
                    agent_reply += f"\n\nDirect email sent to **{reviewer_name}** ({reviewer_email})."
                else:
                    agent_reply += "\n\n*(Email notification not configured — reviewer notified via GitHub only.)*"
            else:
                agent_reply += "\n\n*(No email stored for this reviewer — notified via GitHub @mention only.)*"

        agent_msg = models.ChatMessage(
            task_id=task.id,
            sender_type="Agent",
            content=agent_reply,
            mentioned_github_username=github_username,
        )
        db.add(agent_msg)
        db.commit()
        return user_msg

    # ── Drafting refinement (task still in Backlog) ──
    if task.status == "Backlog":
        try:
            updated_desc = agent.refine_description(task.description or "", body.message)
            task.description = updated_desc

            # Update GitHub issue body
            if task.github_issue_number:
                try:
                    gh.update_issue_body(
                        task.github_issue_number,
                        f"**Requirement:**\n\n{updated_desc}\n\n*Updated via Story Automation App*",
                        cfg=cfg_for_user(current_user),
                    )
                except Exception:
                    pass

            db.commit()
            agent_reply = "Got it. I've updated the GitHub issue with your additional details."
        except Exception as e:
            agent_reply = f"Update noted locally (could not call AI: {e})."

        agent_msg = models.ChatMessage(
            task_id=task.id,
            sender_type="Agent",
            content=agent_reply,
        )
        db.add(agent_msg)
        db.commit()

    return user_msg


@router.get("/{task_id}/chat", response_model=schemas.ChatResponse)
def get_chat(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = db.query(models.Task).filter(
        models.Task.id == task_id,
        models.Task.creator_id == current_user.id,
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"messages": task.messages}
