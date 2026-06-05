import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services import github as gh, agent, email_service
from ..services.github import cfg_for_user
from ..services.engine import _safe_add_comment

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

    # ── Check for @mentions (review request — supports multiple) ──
    mention_matches = MENTION_RE.findall(body.message)
    if mention_matches and task.status == "In Review":
        user_cfg = cfg_for_user(current_user)
        already_notified = set(u.lower() for u in (task.reviewer_github_usernames or []))
        new_usernames = [u for u in mention_matches if u.lower() not in already_notified]

        # Merge into stored list
        all_usernames = list(task.reviewer_github_usernames or [])
        all_usernames += [u for u in mention_matches if u not in all_usernames]
        task.reviewer_github_usernames = all_usernames
        # Keep singular field as first reviewer for backward compat
        task.reviewer_github_username = all_usernames[0] if all_usernames else None

        replies = []
        for github_username in mention_matches:
            # Lookup name + email from reviewer list
            reviewer_name = github_username
            reviewer_email = None
            for r in (current_user.reviewer_list or []):
                if r.get("github_username", "").lower() == github_username.lower():
                    reviewer_name = r.get("name", github_username)
                    reviewer_email = r.get("email") or None
                    break

            if github_username in [u for u in mention_matches if u.lower() in already_notified]:
                replies.append(f"@{github_username} was already notified.")
                continue

            # Update reviewer name on task for first/only reviewer
            if not task.reviewer_name or task.reviewer_github_username == github_username:
                task.reviewer_name = reviewer_name

            if task.github_issue_number:
                if _safe_add_comment(
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
                    user_cfg,
                ):
                    replies.append(f"@{github_username} notified via GitHub.")
                else:
                    replies.append(f"@{github_username} — GitHub notification failed.")

            if reviewer_email and task.github_issue_url:
                sent = email_service.send_review_request(
                    reviewer_name=reviewer_name,
                    reviewer_email=reviewer_email,
                    requester_name=current_user.name,
                    story_title=task.title,
                    github_issue_url=task.github_issue_url,
                )
                if sent:
                    replies.append(f"Email sent to **{reviewer_name}** ({reviewer_email}).")

        db.commit()
        agent_reply = "Review requested.\n\n" + "\n".join(f"• {r}" for r in replies)
        agent_msg = models.ChatMessage(
            task_id=task.id,
            sender_type="Agent",
            content=agent_reply,
            mentioned_github_username=", ".join(mention_matches),
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
                        f"**Requirement:**\n\n{updated_desc}\n\n*Updated via SERA App*",
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
