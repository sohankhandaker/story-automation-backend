"""
WorkflowEngine — orchestrates GitHub state transitions and StoryAgent calls.
Also contains the background polling scheduler.
"""
import logging
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from ..database import SessionLocal
from ..config import settings
from .. import models
from . import github as gh
from . import agent, email_service

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Keywords that count as reviewer approval (case-insensitive substring match)
_APPROVAL_KEYWORDS = {
    "approved", "approve", "lgtm", "looks good", "look good",
    "ship it", "shipit", "good to go", "✅", ":white_check_mark:",
    "accepted", "well done", "great job", "perfect",
}


def _is_approval(comment_body: str) -> bool:
    text = comment_body.lower().strip()
    return any(kw in text for kw in _APPROVAL_KEYWORDS)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _add_chat(db: Session, task: models.Task, sender_type: str, content: str, mentioned: Optional[str] = None):
    msg = models.ChatMessage(
        task_id=task.id,
        sender_type=sender_type,
        content=content,
        mentioned_github_username=mentioned,
    )
    db.add(msg)
    db.commit()


def _log_activity(db: Session, task: models.Task, action: str, trigger: str = None,
                  input_s: str = None, output_s: str = None, status: str = "Success", error: str = None):
    log_entry = models.ActivityLog(
        task_id=task.id,
        action=action,
        trigger=trigger,
        input_summary=input_s,
        output_summary=output_s,
        status=status,
        error_message=error,
    )
    db.add(log_entry)
    db.commit()


def _update_task_status(db: Session, task: models.Task, status: str):
    task.status = status
    task.updated_at = datetime.utcnow()
    db.commit()
    if task.github_project_item_id:
        try:
            gh.update_project_status(task.github_project_item_id, status)
        except Exception as e:
            log.error(f"GitHub status update failed for task {task.id}: {e}")


def _get_reviewer_email(db: Session, task: models.Task) -> Optional[str]:
    """Look up the reviewer's email from the creator's stored reviewer list."""
    if not task.reviewer_github_username:
        return None
    creator = db.query(models.User).filter(models.User.id == task.creator_id).first()
    if not creator:
        return None
    for r in (creator.reviewer_list or []):
        if r.get("github_username", "").lower() == task.reviewer_github_username.lower():
            return r.get("email") or None
    return None


def _get_creator(db: Session, task: models.Task) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.id == task.creator_id).first()


def _build_issue_body(task: models.Task, story_markdown: str, cycle: int = 0) -> str:
    """Build the full GitHub issue body with requirement + consolidated story."""
    header = (
        f"# {task.title}\n\n"
        f"**Requirement:**\n\n{task.description or ''}\n\n"
        f"---\n\n"
    )
    if cycle > 0:
        header += f"> ⟳ Updated story — review cycle {cycle}\n\n"
    return header + story_markdown


# ── READY_COLUMN trigger ───────────────────────────────────────────────────────

def run_ready_column(task_id: str):
    """Called in a background thread when a task is marked Ready."""
    db = SessionLocal()
    task = None
    try:
        task = db.query(models.Task).filter(models.Task.id == task_id).first()
        if not task or task.status not in ("Ready", "Backlog"):
            return

        _update_task_status(db, task, "In Progress")
        _add_chat(db, task, "Agent", "Starting story generation. I'll work through 10 phases...")
        _log_activity(db, task, "WorkflowEngine started", "READY_COLUMN")

        phases = []
        for phase_num, phase_name in agent.PHASES:
            try:
                _add_chat(db, task, "System", f"Phase {phase_num}/10 in progress: {phase_name}...")
                content = agent.generate_phase(phase_num, phase_name, task.title, task.description or "")

                phase_data = {"phase": phase_num, "name": phase_name, "content": content, "completed": True}
                phases.append(phase_data)

                task.story_phases = phases
                task.current_phase = phase_num
                db.commit()

                if task.github_issue_number:
                    try:
                        gh.add_comment(
                            task.github_issue_number,
                            f"### Phase {phase_num}/10: {phase_name}\n\n{content}",
                        )
                    except Exception as e:
                        log.warning(f"GitHub comment failed for phase {phase_num}: {e}")

                _add_chat(db, task, "Agent", f"Phase {phase_num}/10 complete: **{phase_name}** ✓")
                _log_activity(db, task, f"Phase {phase_num} generated", "READY_COLUMN", output_s=phase_name)

            except Exception as e:
                log.error(f"Phase {phase_num} failed for task {task.id}: {e}")
                _add_chat(db, task, "System", f"Phase {phase_num} failed: {str(e)}. Retrying...")
                _log_activity(db, task, f"Phase {phase_num} failed", error=str(e), status="Failed")

        # ── Publish consolidated story to GitHub issue body ──────────────────
        full_story = agent.build_full_story(phases)
        if task.github_issue_number and full_story:
            try:
                gh.update_issue_body(task.github_issue_number, _build_issue_body(task, full_story))
                log.info(f"Consolidated story published to issue #{task.github_issue_number}")
            except Exception as e:
                log.warning(f"Failed to publish consolidated story: {e}")

        _update_task_status(db, task, "In Review")
        _add_chat(
            db, task, "Agent",
            "Story generation complete! All 10 phases are done ✅\n\n"
            "The full story is now in the GitHub issue description.\n\n"
            "To request a review, type **@GitHubUsername** in the chat — "
            "e.g. `@sohankhandaker please review this`.",
        )
        _log_activity(db, task, "Story complete, moved to In Review", "READY_COLUMN")

    except Exception as e:
        log.error(f"WorkflowEngine READY_COLUMN failed for task {task_id}: {e}")
        if task:
            _add_chat(db, task, "System", f"An error occurred during story generation: {e}")
    finally:
        db.close()


# ── REVIEW_COMMENT trigger ────────────────────────────────────────────────────

def run_review_comment(task_id: str, comment_texts: list[str], comment_ids: list[int]):
    """Called when reviewer leaves feedback (non-approval) comments."""
    db = SessionLocal()
    task = None
    try:
        task = db.query(models.Task).filter(models.Task.id == task_id).first()
        if not task:
            return

        if task.current_review_cycle >= task.max_review_cycles:
            _add_chat(
                db, task, "System",
                f"Maximum review cycles ({task.max_review_cycles}) reached. "
                "Please resolve manually with the reviewer.",
            )
            if task.github_issue_number:
                gh.add_comment(
                    task.github_issue_number,
                    f"⚠️ Maximum review cycles ({task.max_review_cycles}) reached. "
                    "Manual resolution required.",
                )
            return

        # Mark comments as processed
        processed = list(task.processed_comment_ids or [])
        processed.extend(str(c) for c in comment_ids)
        task.processed_comment_ids = processed
        task.current_review_cycle += 1
        cycle_num = task.current_review_cycle

        rc = models.ReviewCycle(
            task_id=task.id,
            cycle_number=cycle_num,
            reviewer_github_username=task.reviewer_github_username or "",
            status="Changes Requested",
            review_comments=comment_texts,
        )
        db.add(rc)

        _update_task_status(db, task, "Changes Requested")
        _add_chat(
            db, task, "Agent",
            f"Reviewer feedback received (cycle {cycle_num}/{task.max_review_cycles}). "
            "Updating the story…",
        )
        if task.github_issue_number:
            gh.add_comment(
                task.github_issue_number,
                "🔄 Changes noted. Updating the story based on reviewer feedback…",
            )

        current_story = agent.build_full_story(task.story_phases or [])
        result = agent.update_story_from_comments(
            task.title,
            task.description or "",
            current_story,
            comment_texts,
        )
        updated_story = result["updated_story"]
        change_summary = result["change_summary"]

        rc.agent_change_summary = change_summary
        rc.status = "Updated"

        # ── Update GitHub issue body with revised story ───────────────────────
        if task.github_issue_number:
            try:
                gh.update_issue_body(
                    task.github_issue_number,
                    _build_issue_body(task, updated_story, cycle=cycle_num),
                )
            except Exception as e:
                log.warning(f"Failed to update issue body after review cycle: {e}")

            mention = f"@{task.reviewer_github_username}" if task.reviewer_github_username else "Reviewer"
            gh.add_comment(
                task.github_issue_number,
                f"## ✏️ Story Revised (Cycle {cycle_num})\n\n"
                f"**Changes summary:** {change_summary}\n\n"
                f"The issue description above has been updated with the revised story.\n\n"
                f"{mention} — please check the updated story in the issue description.\n\n"
                f"> Reply **`APPROVED`** to approve, or leave further feedback below.",
            )

        _update_task_status(db, task, "In Review")

        # ── Email re-notification to reviewer ────────────────────────────────
        reviewer_email = _get_reviewer_email(db, task)
        if reviewer_email and task.github_issue_url:
            email_service.send_review_updated(
                reviewer_name=task.reviewer_name or task.reviewer_github_username or "Reviewer",
                reviewer_email=reviewer_email,
                story_title=task.title,
                github_issue_url=task.github_issue_url,
                change_summary=change_summary,
                cycle=cycle_num,
            )

        _add_chat(
            db, task, "Agent",
            f"Story updated (cycle {cycle_num}/{task.max_review_cycles}).\n\n"
            f"**Summary of changes:** {change_summary}\n\n"
            + ("Reviewer notified via email and GitHub." if reviewer_email else "Reviewer notified via GitHub."),
        )
        _log_activity(db, task, f"Review cycle {cycle_num} complete", "REVIEW_COMMENT", output_s=change_summary)
        db.commit()

    except Exception as e:
        log.error(f"WorkflowEngine REVIEW_COMMENT failed for task {task_id}: {e}")
        if task:
            _add_chat(db, task, "System", f"Error during story update: {e}")
    finally:
        db.close()


# ── DONE_BY_REVIEWER trigger ──────────────────────────────────────────────────

def run_done_by_reviewer(task_id: str):
    """Called when reviewer approves (via comment keyword OR board move)."""
    db = SessionLocal()
    try:
        task = db.query(models.Task).filter(models.Task.id == task_id).first()
        if not task or task.status == "Done":
            return

        _update_task_status(db, task, "Done")
        reviewer = task.reviewer_name or task.reviewer_github_username or "the reviewer"

        # ── Email creator that story was approved ─────────────────────────────
        creator = _get_creator(db, task)
        if creator and task.github_issue_url:
            email_service.send_story_approved(
                creator_name=creator.name,
                creator_email=creator.email,
                reviewer_name=reviewer,
                story_title=task.title,
                github_issue_url=task.github_issue_url,
            )

        _add_chat(
            db, task, "Agent",
            f"🎉 Your story has been **approved** by {reviewer}!\n\n"
            f"The task is now **Done**.\n\n"
            f"GitHub Issue: {task.github_issue_url}",
        )
        if task.github_issue_number:
            gh.add_comment(
                task.github_issue_number,
                f"✅ Story approved and marked as **Done** by @{task.reviewer_github_username}.\n\n"
                "Thank you for the review!",
            )
        _log_activity(db, task, "Task approved and marked Done", "DONE_BY_REVIEWER")

    except Exception as e:
        log.error(f"DONE_BY_REVIEWER failed for task {task_id}: {e}")
    finally:
        db.close()


# ── Background thread helpers ─────────────────────────────────────────────────

def trigger_ready_column(task_id: str):
    t = threading.Thread(target=run_ready_column, args=(task_id,), daemon=True)
    t.start()


# ── Polling job ───────────────────────────────────────────────────────────────

def _poll_github():
    """
    Runs every POLL_INTERVAL_SECONDS.
    1. Checks for new reviewer comments — approval keywords → Done, otherwise → review cycle.
    2. Checks if reviewer moved the project board item to Done.
    """
    db = SessionLocal()
    try:
        tasks = db.query(models.Task).filter(
            models.Task.status.in_(["In Review", "Changes Requested"]),
            models.Task.reviewer_github_username.isnot(None),
            models.Task.github_issue_number.isnot(None),
        ).all()

        for task in tasks:
            _check_reviewer_comments(db, task)
            _check_done_status(db, task)

    except Exception as e:
        log.error(f"Polling job error: {e}")
    finally:
        db.close()


def _check_reviewer_comments(db: Session, task: models.Task):
    try:
        since = task.github_last_checked_at
        comments = gh.get_comments_since(task.github_issue_number, since)
        task.github_last_checked_at = datetime.utcnow()
        db.commit()

        processed = set(str(c) for c in (task.processed_comment_ids or []))
        new_comments = [
            c for c in comments
            if c["user"]["login"].lower() == task.reviewer_github_username.lower()
            and str(c["id"]) not in processed
        ]

        if not new_comments:
            return

        # ── Split into approval vs feedback ──────────────────────────────────
        approval = [c for c in new_comments if _is_approval(c["body"])]
        feedback = [c for c in new_comments if not _is_approval(c["body"])]

        # Mark ALL new comments as processed regardless of path
        processed_list = list(task.processed_comment_ids or [])
        processed_list.extend(str(c["id"]) for c in new_comments)
        task.processed_comment_ids = processed_list
        db.commit()

        if approval:
            log.info(f"Approval detected for task {task.id} from @{task.reviewer_github_username}")
            threading.Thread(
                target=run_done_by_reviewer, args=(task.id,), daemon=True
            ).start()
        elif feedback:
            comment_texts = [c["body"] for c in feedback]
            comment_ids = [c["id"] for c in feedback]
            threading.Thread(
                target=run_review_comment,
                args=(task.id, comment_texts, comment_ids),
                daemon=True,
            ).start()

    except Exception as e:
        log.error(f"Comment check failed for task {task.id}: {e}")


def _check_done_status(db: Session, task: models.Task):
    """Fallback: detect approval via board card move to Done."""
    if not task.github_project_item_id:
        return
    try:
        current_status = gh.get_project_item_status(task.github_project_item_id)
        if current_status and current_status.lower() == "done" and task.status != "Done":
            threading.Thread(
                target=run_done_by_reviewer, args=(task.id,), daemon=True
            ).start()
    except Exception as e:
        log.error(f"Done check failed for task {task.id}: {e}")


def start_scheduler():
    scheduler.add_job(
        _poll_github,
        "interval",
        seconds=settings.poll_interval_seconds,
        id="github_poll",
        replace_existing=True,
    )
    scheduler.start()
    log.info(f"Polling scheduler started (every {settings.poll_interval_seconds}s)")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
