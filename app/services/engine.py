"""
WorkflowEngine — orchestrates GitHub state transitions and StoryAgent calls.
Also contains the background polling scheduler.
"""
import logging
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from apscheduler.schedulers.background import BackgroundScheduler

from ..database import SessionLocal
from ..config import settings
from .. import models
from . import github as gh
from .github import cfg_for_user
from . import agent, email_service

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Keywords that count as reviewer approval (case-insensitive substring match)
_APPROVAL_KEYWORDS = {
    "approved", "approve", "lgtm", "looks good", "look good",
    "ship it", "shipit", "good to go", "✅", ":white_check_mark:",
    "accepted", "well done", "great job", "perfect",
}

# Keywords that confirm a pending BRD change proposal (case-insensitive)
_CONFIRM_CHANGE_KEYWORDS = {
    "confirm changes", "apply changes", "apply update", "confirm update",
    "yes apply", "yes, apply", "proceed with changes", "go ahead and update",
    "confirm", "apply",
}


def _is_confirm_change(text: str) -> bool:
    t = text.lower().strip()
    return any(k in t for k in _CONFIRM_CHANGE_KEYWORDS)


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


def _update_task_status(db: Session, task: models.Task, status: str, cfg=None):
    task.status = status
    task.updated_at = datetime.utcnow()
    db.commit()
    if task.github_project_item_id:
        try:
            gh.update_project_status(task.github_project_item_id, status, cfg=cfg)
        except Exception as e:
            log.error(f"GitHub status update failed for task {task.id}: {e}")


def _get_reviewer_email(db: Session, task: models.Task) -> Optional[str]:
    """Look up the primary reviewer's email from the creator's stored reviewer list."""
    if not task.reviewer_github_username:
        return None
    creator = db.query(models.User).filter(models.User.id == task.creator_id).first()
    if not creator:
        return None
    for r in (creator.reviewer_list or []):
        if r.get("github_username", "").lower() == task.reviewer_github_username.lower():
            return r.get("email") or None
    return None


def _get_all_reviewer_emails(db: Session, task: models.Task) -> list[tuple[str, str]]:
    """Return [(github_username, email), ...] for all reviewers who have emails."""
    usernames = list(task.reviewer_github_usernames or [])
    if not usernames and task.reviewer_github_username:
        usernames = [task.reviewer_github_username]
    creator = db.query(models.User).filter(models.User.id == task.creator_id).first()
    if not creator:
        return []
    lookup = {r.get("github_username", "").lower(): r for r in (creator.reviewer_list or [])}
    result = []
    for u in usernames:
        r = lookup.get(u.lower(), {})
        email = r.get("email")
        name = r.get("name", u)
        if email:
            result.append((name, email))
    return result


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

        creator = _get_creator(db, task)
        cfg = cfg_for_user(creator)

        _update_task_status(db, task, "In Progress", cfg=cfg)
        _add_chat(db, task, "Agent", "Starting story generation. I'll work through 10 phases...")
        _log_activity(db, task, "WorkflowEngine started", "READY_COLUMN")

        phases = []
        for phase_num, phase_name in agent.PHASES:
            try:
                _add_chat(db, task, "System", f"Phase {phase_num}/10 in progress: {phase_name}...")
                content = agent.generate_phase(phase_num, phase_name, task.title, task.description or "")

                phase_data = {"phase": phase_num, "name": phase_name, "content": content, "completed": True}
                phases.append(phase_data)

                task.story_phases = list(phases)  # new list object so SQLAlchemy detects the change
                task.current_phase = phase_num
                flag_modified(task, "story_phases")
                db.commit()

                if task.github_issue_number:
                    try:
                        gh.add_comment(
                            task.github_issue_number,
                            f"### Phase {phase_num}/10: {phase_name}\n\n{content}",
                            cfg=cfg,
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
                gh.update_issue_body(task.github_issue_number, _build_issue_body(task, full_story), cfg=cfg)
                log.info(f"Consolidated story published to issue #{task.github_issue_number}")
            except Exception as e:
                log.warning(f"Failed to publish consolidated story: {e}")

        _update_task_status(db, task, "In Review", cfg=cfg)
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

        creator = _get_creator(db, task)
        cfg = cfg_for_user(creator)

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
                    cfg=cfg,
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

        _update_task_status(db, task, "Changes Requested", cfg=cfg)
        _add_chat(
            db, task, "Agent",
            f"Reviewer feedback received (cycle {cycle_num}/{task.max_review_cycles}). "
            "Updating the story…",
        )
        if task.github_issue_number:
            gh.add_comment(
                task.github_issue_number,
                "🔄 Changes noted. Updating the story based on reviewer feedback…",
                cfg=cfg,
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
                    cfg=cfg,
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
                cfg=cfg,
            )

        _update_task_status(db, task, "In Review", cfg=cfg)

        # ── Email all reviewers + notify creator ─────────────────────────────
        reviewer_emails = _get_all_reviewer_emails(db, task)
        for reviewer_name_e, reviewer_email_e in reviewer_emails:
            if task.github_issue_url:
                email_service.send_review_updated(
                    reviewer_name=reviewer_name_e,
                    reviewer_email=reviewer_email_e,
                    story_title=task.title,
                    github_issue_url=task.github_issue_url,
                    change_summary=change_summary,
                    cycle=cycle_num,
                )
        if creator and task.github_issue_url:
            email_service.send_feedback_received(
                creator_name=creator.name,
                creator_email=creator.email,
                reviewer_name=task.reviewer_name or task.reviewer_github_username or "Reviewer",
                story_title=task.title,
                github_issue_url=task.github_issue_url,
                feedback_summary="\n".join(comment_texts[:3]),
                cycle=cycle_num,
            )

        _add_chat(
            db, task, "Agent",
            f"Story updated (cycle {cycle_num}/{task.max_review_cycles}).\n\n"
            f"**Summary of changes:** {change_summary}\n\n"
            + ("Reviewer notified via email and GitHub." if reviewer_emails else "Reviewer notified via GitHub."),
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

        creator = _get_creator(db, task)
        cfg = cfg_for_user(creator)

        _update_task_status(db, task, "Done", cfg=cfg)
        reviewer = task.reviewer_name or task.reviewer_github_username or "the reviewer"

        # ── Email creator that story was approved ─────────────────────────────
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
                cfg=cfg,
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
    1. Checks tasks for new reviewer comments / board moves.
    2. Checks BRD notes for new reviewer comments / approval.
    """
    db = SessionLocal()
    try:
        from sqlalchemy import or_
        tasks = db.query(models.Task).filter(
            models.Task.status.in_(["In Review", "Changes Requested"]),
            or_(
                models.Task.reviewer_github_username.isnot(None),
                models.Task.reviewer_github_usernames.isnot(None),
            ),
            models.Task.github_issue_number.isnot(None),
        ).all()

        for task in tasks:
            _check_reviewer_comments(db, task)
            _check_done_status(db, task)

        # BRD notes in review (has at least one reviewer assigned)
        brd_notes = db.query(models.MeetingNote).filter(
            models.MeetingNote.status.in_(["In Review", "Changes Requested"]),
            models.MeetingNote.github_issue_number.isnot(None),
        ).all()
        brd_notes = [
            n for n in brd_notes
            if (n.reviewers and len(n.reviewers) > 0) or n.reviewer_github_username
        ]

        for note in brd_notes:
            # Recovery path: if all reviewers are already Approved in the JSON
            # but note.status never flipped (e.g. thread was killed on restart),
            # trigger approval immediately without waiting for a new comment.
            if note.reviewers:
                all_already_approved = all(
                    r.get("status") == "Approved" for r in note.reviewers
                )
                if all_already_approved and note.status != "Approved":
                    log.info(
                        f"Recovery: all reviewers Approved in DB but status={note.status} "
                        f"for note {note.id} — triggering run_brd_approved"
                    )
                    threading.Thread(
                        target=run_brd_approved, args=(note.id,), daemon=True
                    ).start()
                    continue

            _check_brd_reviewer_comments(db, note)
            _check_brd_done_status(db, note)

        # PRD documents in review (same structure as BRD notes)
        prd_docs = db.query(models.PrdDocument).filter(
            models.PrdDocument.status.in_(["In Review", "Changes Requested"]),
            models.PrdDocument.github_issue_number.isnot(None),
        ).all()
        prd_docs = [
            p for p in prd_docs
            if (p.reviewers and len(p.reviewers) > 0) or p.reviewer_github_username
        ]

        for prd in prd_docs:
            # Recovery: all reviewers already Approved in DB but status not flipped
            if prd.reviewers:
                all_already_approved = all(
                    r.get("status") == "Approved" for r in prd.reviewers
                )
                if all_already_approved and prd.status != "Approved":
                    log.info(
                        f"Recovery: all PRD reviewers Approved in DB but status={prd.status} "
                        f"for prd {prd.id} — triggering run_prd_approved"
                    )
                    threading.Thread(
                        target=run_prd_approved, args=(prd.id,), daemon=True
                    ).start()
                    continue

            _check_prd_reviewer_comments(db, prd)
            _check_prd_done_status(db, prd)

    except Exception as e:
        log.error(f"Polling job error: {e}")
    finally:
        db.close()


def _check_reviewer_comments(db: Session, task: models.Task):
    try:
        creator = _get_creator(db, task)
        cfg = cfg_for_user(creator)
        since = task.github_last_checked_at
        comments = gh.get_comments_since(task.github_issue_number, since, cfg=cfg)
        task.github_last_checked_at = datetime.utcnow()
        db.commit()

        processed = set(str(c) for c in (task.processed_comment_ids or []))
        reviewer_usernames = set(
            u.lower() for u in (task.reviewer_github_usernames or [])
        ) or ({task.reviewer_github_username.lower()} if task.reviewer_github_username else set())
        new_comments = [
            c for c in comments
            if c["user"]["login"].lower() in reviewer_usernames
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
        creator = _get_creator(db, task)
        cfg = cfg_for_user(creator)
        current_status = gh.get_project_item_status(task.github_project_item_id, cfg=cfg)
        if current_status and current_status.lower() == "done" and task.status != "Done":
            threading.Thread(
                target=run_done_by_reviewer, args=(task.id,), daemon=True
            ).start()
    except Exception as e:
        log.error(f"Done check failed for task {task.id}: {e}")


# ── BRD helpers ──────────────────────────────────────────────────────────────

def _extract_changed_section_content(brd_markdown: str, changed_sections: list[str]) -> str:
    """Build a GitHub comment block showing only the changed section content."""
    if not changed_sections:
        return ""

    lines = brd_markdown.splitlines()
    sections: dict[str, list[str]] = {}
    current_header = None
    current_lines: list[str] = []

    for line in lines:
        if line.startswith("#"):
            if current_header is not None:
                sections[current_header] = current_lines
            current_header = line.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_header:
        sections[current_header] = current_lines

    blocks = []
    for section_name in changed_sections:
        # fuzzy match: check if any stored header contains the changed section name
        content = None
        for header, body in sections.items():
            if section_name.lower() in header.lower() or header.lower() in section_name.lower():
                content = "\n".join(body).strip()
                break
        if content:
            preview = content[:1200] + ("…" if len(content) > 1200 else "")
            blocks.append(
                f"<details>\n<summary>📝 {section_name}</summary>\n\n{preview}\n\n</details>"
            )

    return "\n\n".join(blocks)


def _build_brd_update_comment(
    version: int,
    change_summary: str,
    changed_sections: list[str],
    updated_markdown: str,
    reviewer_mention: str,
) -> str:
    sections_line = ", ".join(changed_sections) if changed_sections else "General updates"
    diff_blocks = _extract_changed_section_content(updated_markdown, changed_sections)

    body = (
        f"## ✏️ BRD Updated (v{version})\n\n"
        f"**Changes:** {change_summary}\n\n"
        f"**Sections updated:** {sections_line}\n\n"
    )
    if diff_blocks:
        body += f"### Changed sections\n\n{diff_blocks}\n\n"
    body += (
        f"---\n\n"
        f"The full updated BRD is in the issue description above.\n\n"
        f"{reviewer_mention} — please review. "
        f"Reply **`APPROVED`** to approve, or leave further feedback."
    )
    return body


# ── BRD review polling ────────────────────────────────────────────────────────

def _check_brd_reviewer_comments(db: Session, note: models.MeetingNote):
    try:
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first()
        cfg = cfg_for_user(creator)
        since = note.github_last_checked_at
        comments = gh.get_comments_since(note.github_issue_number, since, cfg=cfg)
        note.github_last_checked_at = datetime.utcnow()
        db.commit()

        # Build set of all assigned reviewer usernames
        reviewer_usernames = {
            r["github_username"].lower() for r in (note.reviewers or [])
        }
        if not reviewer_usernames and note.reviewer_github_username:
            reviewer_usernames = {note.reviewer_github_username.lower()}

        processed = set(str(c) for c in (note.processed_comment_ids or []))
        new_comments = [
            c for c in comments
            if c["user"]["login"].lower() in reviewer_usernames
            and str(c["id"]) not in processed
        ]
        if not new_comments:
            return

        # ── Classify new comments into four buckets ───────────────────────────
        # Priority order: confirm_change > approval > question > change_request
        confirmations  = [c for c in new_comments if _is_confirm_change(c["body"])]
        approvals      = [c for c in new_comments if not _is_confirm_change(c["body"]) and _is_approval(c["body"])]
        other_comments = [c for c in new_comments if not _is_confirm_change(c["body"]) and not _is_approval(c["body"])]

        # Mark all new comments as processed up-front so re-polls don't re-process them
        processed_list = list(note.processed_comment_ids or [])
        processed_list.extend(str(c["id"]) for c in new_comments)
        note.processed_comment_ids = processed_list
        flag_modified(note, "processed_comment_ids")
        db.commit()

        # ── Handle CONFIRM CHANGES ────────────────────────────────────────────
        if confirmations and note.pending_brd_changes:
            pending = note.pending_brd_changes
            log.info(f"Reviewer confirmed pending BRD changes for note {note.id}")
            note.pending_brd_changes = None
            flag_modified(note, "pending_brd_changes")
            db.commit()
            threading.Thread(
                target=run_brd_review_comment,
                args=(note.id, [pending["feedback"]], []),
                daemon=True,
            ).start()
            return  # one action per poll cycle

        # ── Handle APPROVED ───────────────────────────────────────────────────
        if approvals:
            approvers = {c["user"]["login"].lower() for c in approvals}
            reviewers = list(note.reviewers or [])
            for r in reviewers:
                if r["github_username"].lower() in approvers:
                    r["status"] = "Approved"
            note.reviewers = reviewers
            flag_modified(note, "reviewers")
            db.commit()

            all_approved = all(r["status"] == "Approved" for r in reviewers) if reviewers else True
            if all_approved:
                threading.Thread(target=run_brd_approved, args=(note.id,), daemon=True).start()
            else:
                pending_reviewers = [r["github_username"] for r in reviewers if r["status"] != "Approved"]
                try:
                    approver_names = ", ".join(f"@{c['user']['login']}" for c in approvals)
                    gh.add_comment(
                        note.github_issue_number,
                        f"✅ {approver_names} approved the BRD.\n\n"
                        f"Still waiting for approval from: {', '.join('@' + u for u in pending_reviewers)}",
                        cfg=cfg,
                    )
                except Exception as e:
                    log.warning(f"Failed to post partial approval comment: {e}")

        # ── Handle questions and change requests ──────────────────────────────
        questions = []
        change_requests = []
        for c in other_comments:
            comment_type = agent.classify_brd_comment(c["body"])
            log.info(f"BRD comment classified as '{comment_type}' for note {note.id}")
            if comment_type == "question":
                questions.append(c)
            else:
                change_requests.append(c)

        # Answer each question independently (no BRD modification)
        for q in questions:
            threading.Thread(
                target=run_brd_answer_question,
                args=(note.id, q["body"], q["id"]),
                daemon=True,
            ).start()

        # Combine all change requests into a single proposal
        if change_requests:
            combined_feedback = "\n\n".join(c["body"] for c in change_requests)
            comment_ids = [c["id"] for c in change_requests]
            threading.Thread(
                target=run_brd_propose_changes,
                args=(note.id, combined_feedback, comment_ids),
                daemon=True,
            ).start()

    except Exception as e:
        log.error(f"BRD comment check failed for note {note.id}: {e}")


def _check_brd_done_status(db: Session, note: models.MeetingNote):
    if not note.github_project_item_id:
        log.debug(f"BRD done check skipped (no project_item_id) for note {note.id}")
        return
    try:
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first()
        cfg = cfg_for_user(creator)
        current_status = gh.get_project_item_status(note.github_project_item_id, cfg=cfg)
        log.info(f"BRD board status for note {note.id}: '{current_status}' (note.status='{note.status}')")
        if current_status and current_status.lower() == "done" and note.status != "Approved":
            log.info(f"BRD board moved to Done — triggering approval for note {note.id}")
            threading.Thread(target=run_brd_approved, args=(note.id,), daemon=True).start()
    except Exception as e:
        log.error(f"BRD done check failed for note {note.id}: {e}")


def run_brd_review_comment(note_id: str, comment_texts: list[str], comment_ids: list[int]):
    """Called when reviewer leaves feedback on a BRD GitHub issue."""
    db = SessionLocal()
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == note_id).first()
        if not note:
            return

        creator = db.query(models.User).filter(models.User.id == note.creator_id).first()
        cfg = cfg_for_user(creator)

        processed = list(note.processed_comment_ids or [])
        processed.extend(str(c) for c in comment_ids)
        note.processed_comment_ids = processed
        note.status = "Changes Requested"
        note.brd_generation_phase = 0  # update in progress
        db.commit()

        if note.github_issue_number:
            gh.add_comment(
                note.github_issue_number,
                "🔄 Feedback received. Updating the BRD based on reviewer comments…",
                cfg=cfg,
            )

        result = agent.update_brd_from_feedback(
            current_brd=note.brd_draft or "",
            reviewer_comments=comment_texts,
        )

        next_ver = (note.current_version_number or 1) + 1
        version = models.BrdVersion(
            note_id=note.id,
            version_number=next_ver,
            brd_markdown=result["updated_markdown"],
            change_summary=result["change_summary"],
            changed_sections=result["changed_sections"],
            reviewer_comment="\n".join(comment_texts[:3]),
        )
        db.add(version)

        note.brd_draft = result["updated_markdown"]
        note.current_version_number = next_ver
        note.brd_generation_phase = None
        note.status = "In Review"

        if note.github_issue_number:
            try:
                from ..routers.notes import _build_brd_issue_body
                gh.update_issue_body(
                    note.github_issue_number,
                    _build_brd_issue_body(note, result["updated_markdown"]),
                    cfg=cfg,
                )
                reviewer = f"@{note.reviewer_github_username}" if note.reviewer_github_username else "Reviewer"
                gh.add_comment(
                    note.github_issue_number,
                    _build_brd_update_comment(
                        version=next_ver,
                        change_summary=result["change_summary"],
                        changed_sections=result["changed_sections"],
                        updated_markdown=result["updated_markdown"],
                        reviewer_mention=reviewer,
                    ),
                    cfg=cfg,
                )
            except Exception as e:
                log.warning(f"GitHub update failed after BRD review comment: {e}")

        db.commit()
        log.info(f"BRD note {note_id} updated to v{next_ver} from reviewer feedback")

    except Exception as e:
        log.error(f"run_brd_review_comment failed for note {note_id}: {e}")
        try:
            note.brd_generation_phase = None
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


def run_brd_answer_question(note_id: str, question: str, comment_id: int):
    """Answer a reviewer's question about the BRD as a GitHub comment.
    The BRD is NOT modified."""
    db = SessionLocal()
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == note_id).first()
        if not note or not note.brd_draft:
            return
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first()
        cfg = cfg_for_user(creator)

        answer = agent.answer_brd_question(question, note.brd_draft)
        gh.add_comment(
            note.github_issue_number,
            f"**Answer to your question:**\n\n{answer}\n\n"
            f"---\n"
            f"_The BRD has not been modified. If you'd like changes made, "
            f"describe them and I'll propose an update for your confirmation._",
            cfg=cfg,
        )
        log.info(f"Answered BRD question for note {note_id} (comment {comment_id})")
    except Exception as e:
        log.error(f"run_brd_answer_question failed for note {note_id}: {e}")
    finally:
        db.close()


def run_brd_propose_changes(note_id: str, feedback: str, comment_ids: list):
    """Propose BRD changes to the reviewer without applying them.
    Stores the proposal in pending_brd_changes; the BRD is updated only
    after the reviewer replies with a CONFIRM CHANGES keyword."""
    db = SessionLocal()
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == note_id).first()
        if not note or not note.brd_draft:
            return
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first()
        cfg = cfg_for_user(creator)

        summary = agent.propose_brd_change_summary(feedback, note.brd_draft)

        # Store pending proposal — applied only on CONFIRM CHANGES
        note.pending_brd_changes = {
            "feedback": feedback,
            "summary": summary,
            "comment_ids": [str(c) for c in comment_ids],
        }
        flag_modified(note, "pending_brd_changes")
        db.commit()

        reviewer = f"@{note.reviewer_github_username}" if note.reviewer_github_username else "Reviewer"
        gh.add_comment(
            note.github_issue_number,
            f"**Proposed BRD Updates:**\n\n{summary}\n\n"
            f"---\n"
            f"{reviewer} — Reply **`CONFIRM CHANGES`** to apply these updates to the BRD, "
            f"or provide additional feedback to refine them.\n\n"
            f"_The BRD has not been modified yet._",
            cfg=cfg,
        )
        log.info(f"Proposed BRD changes for note {note_id}, awaiting confirmation")
    except Exception as e:
        log.error(f"run_brd_propose_changes failed for note {note_id}: {e}")
    finally:
        db.close()


def run_brd_approved(note_id: str):
    """Called when reviewer approves the BRD."""
    db = SessionLocal()
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == note_id).first()
        if not note or note.status == "Approved":
            return

        creator = db.query(models.User).filter(models.User.id == note.creator_id).first()
        cfg = cfg_for_user(creator)

        note.status = "Approved"
        db.commit()

        if note.github_project_item_id:
            try:
                gh.update_project_status(note.github_project_item_id, "Done", cfg=cfg)
            except Exception as e:
                log.warning(f"GitHub board status update failed: {e}")

        reviewer = note.reviewer_name or note.reviewer_github_username or "the reviewer"
        if note.github_issue_number:
            gh.add_comment(
                note.github_issue_number,
                f"✅ BRD approved by {reviewer} and marked as **Done**.\n\nThank you for the review!",
                cfg=cfg,
            )

        log.info(f"BRD note {note_id} approved by {reviewer}")

    except Exception as e:
        log.error(f"run_brd_approved failed for note {note_id}: {e}")
    finally:
        db.close()


# ── PRD polling helpers (mirror of BRD helpers) ───────────────────────────────

def _check_prd_reviewer_comments(db: Session, prd: models.PrdDocument):
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == prd.note_id).first()
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first() if note else None
        cfg = cfg_for_user(creator)

        comments = gh.get_comments_since(prd.github_issue_number, prd.github_last_checked_at, cfg=cfg)
        prd.github_last_checked_at = datetime.utcnow()
        db.commit()

        reviewer_usernames = {r["github_username"].lower() for r in (prd.reviewers or [])}
        if not reviewer_usernames and prd.reviewer_github_username:
            reviewer_usernames = {prd.reviewer_github_username.lower()}

        processed = set(str(c) for c in (prd.processed_comment_ids or []))
        new_comments = [
            c for c in comments
            if c["user"]["login"].lower() in reviewer_usernames
            and str(c["id"]) not in processed
        ]
        if not new_comments:
            return

        confirmations  = [c for c in new_comments if _is_confirm_change(c["body"])]
        approvals      = [c for c in new_comments if not _is_confirm_change(c["body"]) and _is_approval(c["body"])]
        other_comments = [c for c in new_comments if not _is_confirm_change(c["body"]) and not _is_approval(c["body"])]

        processed_list = list(prd.processed_comment_ids or [])
        processed_list.extend(str(c["id"]) for c in new_comments)
        prd.processed_comment_ids = processed_list
        flag_modified(prd, "processed_comment_ids")
        db.commit()

        # CONFIRM CHANGES → apply pending proposal
        if confirmations and prd.pending_prd_changes:
            pending = prd.pending_prd_changes
            log.info(f"Reviewer confirmed pending PRD changes for prd {prd.id}")
            prd.pending_prd_changes = None
            flag_modified(prd, "pending_prd_changes")
            db.commit()
            threading.Thread(
                target=run_prd_review_comment,
                args=(prd.id, [pending["feedback"]], []),
                daemon=True,
            ).start()
            return

        # APPROVED → mark reviewers and check all-approved
        if approvals:
            approvers = {c["user"]["login"].lower() for c in approvals}
            reviewers = list(prd.reviewers or [])
            for r in reviewers:
                if r["github_username"].lower() in approvers:
                    r["status"] = "Approved"
            prd.reviewers = reviewers
            flag_modified(prd, "reviewers")
            db.commit()

            all_approved = all(r["status"] == "Approved" for r in reviewers) if reviewers else True
            if all_approved:
                threading.Thread(target=run_prd_approved, args=(prd.id,), daemon=True).start()
            else:
                pending_reviewers = [r["github_username"] for r in reviewers if r["status"] != "Approved"]
                try:
                    approver_names = ", ".join(f"@{c['user']['login']}" for c in approvals)
                    gh.add_comment(
                        prd.github_issue_number,
                        f"✅ {approver_names} approved the PRD.\n\n"
                        f"Still waiting for approval from: {', '.join('@' + u for u in pending_reviewers)}",
                        cfg=cfg,
                    )
                except Exception as e:
                    log.warning(f"Failed to post partial PRD approval comment: {e}")

        # Questions and change requests
        questions = []
        change_requests = []
        for c in other_comments:
            comment_type = agent.classify_brd_comment(c["body"])
            log.info(f"PRD comment classified as '{comment_type}' for prd {prd.id}")
            if comment_type == "question":
                questions.append(c)
            else:
                change_requests.append(c)

        for q in questions:
            threading.Thread(
                target=run_prd_answer_question,
                args=(prd.id, q["body"], q["id"]),
                daemon=True,
            ).start()

        if change_requests:
            combined_feedback = "\n\n".join(c["body"] for c in change_requests)
            comment_ids = [c["id"] for c in change_requests]
            threading.Thread(
                target=run_prd_propose_changes,
                args=(prd.id, combined_feedback, comment_ids),
                daemon=True,
            ).start()

    except Exception as e:
        log.error(f"PRD comment check failed for prd {prd.id}: {e}")


def _check_prd_done_status(db: Session, prd: models.PrdDocument):
    if not prd.github_project_item_id:
        return
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == prd.note_id).first()
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first() if note else None
        cfg = cfg_for_user(creator)
        current_status = gh.get_project_item_status(prd.github_project_item_id, cfg=cfg)
        log.info(f"PRD board status for prd {prd.id}: '{current_status}' (prd.status='{prd.status}')")
        if current_status and current_status.lower() == "done" and prd.status != "Approved":
            log.info(f"PRD board moved to Done — triggering approval for prd {prd.id}")
            threading.Thread(target=run_prd_approved, args=(prd.id,), daemon=True).start()
    except Exception as e:
        log.error(f"PRD done check failed for prd {prd.id}: {e}")


def run_prd_answer_question(prd_id: str, question: str, comment_id: int):
    """Answer a reviewer question about the PRD without modifying it."""
    db = SessionLocal()
    try:
        prd = db.query(models.PrdDocument).filter(models.PrdDocument.id == prd_id).first()
        if not prd or not prd.prd_draft:
            return
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == prd.note_id).first()
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first() if note else None
        cfg = cfg_for_user(creator)

        answer = agent.answer_brd_question(question, prd.prd_draft)
        gh.add_comment(
            prd.github_issue_number,
            f"**Answer to your question:**\n\n{answer}\n\n"
            f"---\n"
            f"_The PRD has not been modified. If you'd like changes made, "
            f"describe them and I'll propose an update for your confirmation._",
            cfg=cfg,
        )
        log.info(f"Answered PRD question for prd {prd_id} (comment {comment_id})")
    except Exception as e:
        log.error(f"run_prd_answer_question failed for prd {prd_id}: {e}")
    finally:
        db.close()


def run_prd_propose_changes(prd_id: str, feedback: str, comment_ids: list):
    """Propose PRD changes without applying them; waits for CONFIRM CHANGES."""
    db = SessionLocal()
    try:
        prd = db.query(models.PrdDocument).filter(models.PrdDocument.id == prd_id).first()
        if not prd or not prd.prd_draft:
            return
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == prd.note_id).first()
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first() if note else None
        cfg = cfg_for_user(creator)

        summary = agent.propose_brd_change_summary(feedback, prd.prd_draft)

        prd.pending_prd_changes = {
            "feedback": feedback,
            "summary": summary,
            "comment_ids": [str(c) for c in comment_ids],
        }
        flag_modified(prd, "pending_prd_changes")
        db.commit()

        reviewer = f"@{prd.reviewer_github_username}" if prd.reviewer_github_username else "Reviewer"
        gh.add_comment(
            prd.github_issue_number,
            f"**Proposed PRD Updates:**\n\n{summary}\n\n"
            f"---\n"
            f"{reviewer} — Reply **`CONFIRM CHANGES`** to apply these updates to the PRD, "
            f"or provide additional feedback to refine them.\n\n"
            f"_The PRD has not been modified yet._",
            cfg=cfg,
        )
        log.info(f"Proposed PRD changes for prd {prd_id}, awaiting confirmation")
    except Exception as e:
        log.error(f"run_prd_propose_changes failed for prd {prd_id}: {e}")
    finally:
        db.close()


def run_prd_review_comment(prd_id: str, comment_texts: list[str], comment_ids: list):
    """Apply confirmed PRD changes from reviewer feedback."""
    from ..routers.prd import _build_prd_issue_body, _build_prd_update_comment
    db = SessionLocal()
    try:
        prd = db.query(models.PrdDocument).filter(models.PrdDocument.id == prd_id).first()
        if not prd:
            return
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == prd.note_id).first()
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first() if note else None
        cfg = cfg_for_user(creator)

        processed = list(prd.processed_comment_ids or [])
        processed.extend(str(c) for c in comment_ids)
        prd.processed_comment_ids = processed
        prd.status = "Changes Requested"
        prd.prd_generation_phase = 0
        db.commit()

        if prd.github_issue_number:
            gh.add_comment(prd.github_issue_number,
                "🔄 Feedback confirmed. Updating the PRD…", cfg=cfg)

        result = agent.update_prd_from_feedback(
            prd_content=prd.prd_draft or "",
            feedback="\n\n".join(comment_texts),
        )

        next_ver = (prd.current_version_number or 1) + 1
        version = models.PrdVersion(
            prd_id=prd.id,
            version_number=next_ver,
            prd_markdown=result["updated_prd"],
            change_summary=result["change_summary"],
            changed_sections=result["changed_sections"],
            reviewer_comment="\n".join(comment_texts[:3]),
        )
        db.add(version)

        prd.prd_draft = result["updated_prd"]
        prd.current_version_number = next_ver
        prd.prd_generation_phase = None
        prd.status = "In Review"

        if prd.github_issue_number and note:
            try:
                gh.update_issue_body(
                    prd.github_issue_number,
                    _build_prd_issue_body(note, result["updated_prd"]),
                    cfg=cfg,
                )
                reviewer = f"@{prd.reviewer_github_username}" if prd.reviewer_github_username else "Reviewer"
                gh.add_comment(
                    prd.github_issue_number,
                    _build_prd_update_comment(
                        version=next_ver,
                        change_summary=result["change_summary"],
                        changed_sections=result["changed_sections"],
                        reviewer_mention=reviewer,
                    ),
                    cfg=cfg,
                )
            except Exception as e:
                log.warning(f"GitHub update failed after PRD review comment: {e}")

        db.commit()
        log.info(f"PRD {prd_id} updated to v{next_ver} from reviewer feedback")

    except Exception as e:
        log.error(f"run_prd_review_comment failed for prd {prd_id}: {e}")
        try:
            prd.prd_generation_phase = None
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


def run_prd_approved(prd_id: str):
    """Called when reviewer approves the PRD."""
    db = SessionLocal()
    try:
        prd = db.query(models.PrdDocument).filter(models.PrdDocument.id == prd_id).first()
        if not prd or prd.status == "Approved":
            return
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == prd.note_id).first()
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first() if note else None
        cfg = cfg_for_user(creator)

        prd.status = "Approved"
        db.commit()

        if prd.github_project_item_id:
            try:
                gh.update_project_status(prd.github_project_item_id, "Done", cfg=cfg)
            except Exception as e:
                log.warning(f"PRD board status update failed: {e}")

        if prd.github_issue_number:
            try:
                reviewer = prd.reviewer_name or prd.reviewer_github_username or "the reviewer"
                gh.add_comment(
                    prd.github_issue_number,
                    f"✅ PRD approved by {reviewer}!\n\n"
                    f"The PRD is ready to be sent to the Planner.",
                    cfg=cfg,
                )
            except Exception as e:
                log.warning(f"PRD approval comment failed: {e}")

        log.info(f"PRD {prd_id} approved")

    except Exception as e:
        log.error(f"run_prd_approved failed for prd {prd_id}: {e}")
    finally:
        db.close()


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
