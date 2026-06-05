import re
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services import agent
from ..services.github import cfg_for_user, cfg_for_project
from ..services import github as gh
from ..services import engine as engine_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notes", tags=["prd"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_project_issue_number(note: models.MeetingNote, db: Session) -> int | None:
    """Return the issue number that PRD-related comments should be posted on.

    Hierarchy (most specific wins):
      1. The PRD's own sub-issue, if a PRD exists with one.
      2. The note's own sub-issue.
      3. The project's main ticket.
    """
    prd = db.query(models.PrdDocument).filter(
        models.PrdDocument.note_id == note.id
    ).first()
    if prd and prd.github_issue_number:
        return prd.github_issue_number
    if note.github_issue_number:
        return note.github_issue_number
    if note.project_id:
        project = db.query(models.Project).filter(models.Project.id == note.project_id).first()
        if project:
            return project.github_issue_number
    return None


def _build_prd_comment(note: models.MeetingNote, prd: models.PrdDocument, prd_markdown: str) -> str:
    title = note.title or "PRD"
    header = (
        f"## PRD: {title} — v{prd.current_version_number}\n\n"
        f"---\n\n"
    )
    body = header + prd_markdown
    if len(body) > 60000:
        body = body[:60000] + "\n\n_(Content truncated — see app for full PRD)_"
    if prd.github_file_url:
        body += f"\n\n---\n\n📥 **[View / Download PRD (.md)]({prd.github_file_url})**"
    return body


def _build_prd_update_comment(
    version: int,
    change_summary: str,
    changed_sections: list,
    reviewer_mention: str,
) -> str:
    sections_line = ", ".join(changed_sections) if changed_sections else "General updates"
    return (
        f"## PRD Updated (v{version})\n\n"
        f"**Changes:** {change_summary}\n\n"
        f"**Sections updated:** {sections_line}\n\n"
        f"---\n\n"
        f"The full updated PRD is in the comment above.\n\n"
        f"{reviewer_mention} — please review. "
        f"Reply **`APPROVED`** to approve, or leave further feedback."
    )


# ── Background tasks ──────────────────────────────────────────────────────────

def _full_prd_pipeline(prd_id: str):
    """Generate the full PRD phase by phase from the approved BRD.
    Posts progress and result as comments on the project's GitHub issue."""
    from ..database import SessionLocal
    db = SessionLocal()
    prd = None
    try:
        prd = db.query(models.PrdDocument).filter(models.PrdDocument.id == prd_id).first()
        if not prd:
            return

        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == prd.note_id).first()
        if not note or not note.brd_draft:
            log.error(f"PRD pipeline: note or BRD not found for prd_id={prd_id}")
            prd.status = "Draft"
            db.commit()
            return

        creator = db.query(models.User).filter(models.User.id == note.creator_id).first()
        cfg = cfg_for_user(creator)
        issue_number = _get_project_issue_number(note, db)

        def _phase_cb(phase_num: int, _total: int):
            try:
                prd.prd_generation_phase = phase_num
                db.commit()
            except Exception as e:
                log.warning(f"PRD phase callback commit failed: {e}")

        # Pre-analysis: extract structured engineering context from the BRD
        # (mirrors analyze_notes_to_draft used in BRD pipeline)
        brd_analysis = ""
        try:
            log.info(f"PRD pipeline: running BRD pre-analysis for prd_id={prd_id}")
            brd_analysis = agent.analyze_brd_for_prd(note.brd_draft)
        except Exception as e:
            log.warning(f"BRD pre-analysis failed (continuing without it): {e}")

        result = agent.generate_prd_from_brd(
            note.brd_draft,
            brd_analysis=brd_analysis,
            phase_callback=_phase_cb,
        )

        v1 = models.PrdVersion(
            prd_id=prd.id,
            version_number=1,
            prd_markdown=result["full_prd"],
            change_summary="Initial AI generation from approved BRD",
            changed_sections=[],
        )
        db.add(v1)

        prd.prd_draft = result["full_prd"]
        prd.prd_generation_phase = None
        prd.current_version_number = 1
        prd.status = "Pending Review"
        db.commit()

        # Push .md file first so the download link is available in the comment
        title = note.title if note else None
        safe_title = re.sub(r'[^\w\s\-]', '', title or 'PRD').strip().replace(' ', '_')
        file_path = f"prd/{safe_title}_{prd.id[:8]}.md"
        try:
            file_info = gh.push_file(
                path=file_path,
                content=result["full_prd"],
                commit_message=f"docs(prd): add PRD for {title or prd.id[:8]}",
                cfg=cfg,
            )
            prd.github_file_url = file_info["html_url"]
            prd.github_file_raw_url = file_info["raw_url"]
            db.commit()
            log.info(f"PRD .md pushed to repo: {file_info['html_url']}")
        except Exception as e:
            log.warning(f"PRD file push failed: {e}")

        # Post PRD as a comment (includes download link if file was pushed)
        if issue_number:
            try:
                gh.add_comment(
                    issue_number,
                    _build_prd_comment(note, prd, result["full_prd"]),
                    cfg=cfg,
                )
                gh.add_comment(
                    issue_number,
                    "PRD generation complete!\n\nThe full Product Requirements Document is in the comment above.\n\nThe creator will assign a reviewer shortly.",
                    cfg=cfg,
                )
            except Exception as e:
                log.warning(f"GitHub PRD comment failed: {e}")

    except Exception as e:
        log.error(f"PRD pipeline failed for prd_id={prd_id}: {e}")
        try:
            if prd:
                prd.prd_generation_phase = None
                prd.status = "Draft"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _update_prd(prd_id: str, feedback: str):
    """Update PRD from reviewer/creator feedback, save new version."""
    from ..database import SessionLocal
    db = SessionLocal()
    prd = None
    try:
        prd = db.query(models.PrdDocument).filter(models.PrdDocument.id == prd_id).first()
        if not prd or not prd.prd_draft:
            return

        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == prd.note_id).first()
        creator = db.query(models.User).filter(models.User.id == note.creator_id).first() if note else None
        cfg = cfg_for_user(creator)
        issue_number = _get_project_issue_number(note, db) if note else None

        result = agent.update_prd_from_feedback(
            prd_content=prd.prd_draft,
            feedback=feedback,
            brd_content=note.brd_draft if note and note.brd_draft else "",
        )

        next_ver = (prd.current_version_number or 1) + 1
        version = models.PrdVersion(
            prd_id=prd.id,
            version_number=next_ver,
            prd_markdown=result["updated_prd"],
            change_summary=result["change_summary"],
            changed_sections=result["changed_sections"],
            reviewer_comment=feedback,
        )
        db.add(version)

        prd.prd_draft = result["updated_prd"]
        prd.current_version_number = next_ver
        prd.prd_generation_phase = None
        prd.status = "Pending Review"

        if issue_number and note:
            try:
                reviewer = f"@{prd.reviewer_github_username}" if prd.reviewer_github_username else "Reviewer"
                gh.add_comment(
                    issue_number,
                    _build_prd_comment(note, prd, result["updated_prd"]) + "\n\n---\n\n" +
                    _build_prd_update_comment(
                        version=next_ver,
                        change_summary=result["change_summary"],
                        changed_sections=result["changed_sections"],
                        reviewer_mention=reviewer,
                    ),
                    cfg=cfg,
                )
            except Exception as e:
                log.warning(f"GitHub update comment failed after PRD feedback: {e}")

        db.commit()

    except Exception as e:
        log.error(f"PRD update failed for prd_id={prd_id}: {e}")
        try:
            if prd:
                prd.prd_generation_phase = None
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/{note_id}/prd/generate", response_model=schemas.PrdResponse, status_code=202)
def generate_prd(
    note_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Start PRD generation from an approved BRD. All output posted as comments on the project's issue."""
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.status != "Approved":
        raise HTTPException(status_code=400, detail="BRD must be Approved before generating PRD")
    if not note.brd_draft:
        raise HTTPException(status_code=400, detail="No BRD content found")

    # Check if PRD already exists — allow retry only from Draft
    existing_prd = db.query(models.PrdDocument).filter(
        models.PrdDocument.note_id == note_id
    ).first()
    if existing_prd and existing_prd.status not in ("Draft",):
        raise HTTPException(
            status_code=400,
            detail=f"PRD already exists with status '{existing_prd.status}'"
        )

    cfg = cfg_for_user(current_user)

    # Architecture: one project = one GitHub ticket. PRD generation, comments,
    # Q&A, and the download link all live on the project's main issue — no
    # PRD sub-issue.
    project = (
        db.query(models.Project).filter(models.Project.id == note.project_id).first()
        if note.project_id else None
    )
    parent_issue_number = (project.github_issue_number if project else None) or note.github_issue_number
    parent_issue_id     = (project.github_issue_id     if project else None) or note.github_issue_id
    parent_issue_node_id = (project.github_issue_node_id if project else None) or note.github_issue_node_id
    parent_issue_url    = (project.github_issue_url    if project else None) or note.github_issue_url

    if parent_issue_number:
        try:
            comment_cfg = cfg_for_project(current_user, project) if project else cfg
            try:
                gh.add_comment(
                    parent_issue_number,
                    f"### PRD Generation Started\n\nGenerating Product Requirements Document from the approved BRD…",
                    cfg=comment_cfg,
                )
            except Exception as e:
                if "403" in str(e):
                    fallback = gh._env_cfg()
                    if fallback.token and fallback.token != comment_cfg.token:
                        gh.add_comment(
                            parent_issue_number,
                            f"### PRD Generation Started\n\nGenerating Product Requirements Document from the approved BRD…",
                            cfg=fallback,
                        )
                    else:
                        raise
                else:
                    raise
        except Exception as e:
            log.warning(f"GitHub PRD start comment failed: {e}")

    # PRD shares the project's issue (no PRD sub-issue).
    issue_number_for_prd = parent_issue_number
    issue_node_id        = parent_issue_node_id
    issue_url            = parent_issue_url
    issue_id_for_prd     = parent_issue_id
    project_item_id      = None

    if existing_prd:
        existing_prd.prd_draft = None
        existing_prd.prd_generation_phase = 0
        existing_prd.status = "In Progress"
        existing_prd.current_version_number = 0
        existing_prd.github_issue_number = issue_number_for_prd
        existing_prd.github_issue_node_id = issue_node_id
        existing_prd.github_issue_id = issue_id_for_prd
        existing_prd.github_project_item_id = project_item_id
        existing_prd.github_issue_url = issue_url
        existing_prd.github_last_checked_at = datetime.utcnow()
        db.commit()
        db.refresh(existing_prd)
        background_tasks.add_task(_full_prd_pipeline, existing_prd.id)
        return existing_prd

    prd = models.PrdDocument(
        note_id=note_id,
        prd_generation_phase=0,
        status="In Progress",
        github_issue_number=issue_number_for_prd,
        github_issue_node_id=issue_node_id,
        github_issue_id=issue_id_for_prd,
        github_project_item_id=project_item_id,
        github_issue_url=issue_url,
        github_last_checked_at=datetime.utcnow(),
    )
    db.add(prd)
    db.commit()
    db.refresh(prd)

    background_tasks.add_task(_full_prd_pipeline, prd.id)
    return prd


@router.get("/{note_id}/prd", response_model=schemas.PrdResponse)
def get_prd(
    note_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    prd = db.query(models.PrdDocument).filter(
        models.PrdDocument.note_id == note_id
    ).first()
    if not prd:
        raise HTTPException(status_code=404, detail="PRD not found")
    return prd


@router.get("/{note_id}/prd/versions", response_model=schemas.PrdVersionListResponse)
def list_prd_versions(
    note_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    prd = db.query(models.PrdDocument).filter(
        models.PrdDocument.note_id == note_id
    ).first()
    if not prd:
        raise HTTPException(status_code=404, detail="PRD not found")

    versions = (
        db.query(models.PrdVersion)
        .filter(models.PrdVersion.prd_id == prd.id)
        .order_by(models.PrdVersion.version_number.desc())
        .all()
    )
    return {"versions": versions, "total": len(versions)}


@router.post("/{note_id}/prd/assign-reviewer", response_model=schemas.PrdResponse)
def assign_prd_reviewer(
    note_id: str,
    body: schemas.AssignReviewerRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    prd = db.query(models.PrdDocument).filter(
        models.PrdDocument.note_id == note_id
    ).first()
    if not prd:
        raise HTTPException(status_code=404, detail="PRD not found")
    if prd.status not in ("Pending Review", "In Review", "Changes Requested"):
        raise HTTPException(status_code=400, detail="PRD is not ready for reviewer assignment")

    cfg = cfg_for_user(current_user)

    # Move parent project's board card to In Review (PRD lives under a
    # sub-issue with no board card of its own).
    if note.project_id:
        _proj = db.query(models.Project).filter(
            models.Project.id == note.project_id
        ).first()
        if _proj and _proj.github_project_item_id:
            try:
                project_cfg = cfg_for_project(current_user, _proj)
                gh.update_project_status(_proj.github_project_item_id, "In Review", cfg=project_cfg)
            except Exception as e:
                log.warning(f"PRD board status update failed: {e}")

    # Append to reviewers list (no duplicates)
    reviewers = list(prd.reviewers or [])
    existing_usernames = {r["github_username"].lower() for r in reviewers}
    is_new = body.reviewer_github_username.lower() not in existing_usernames
    if is_new:
        reviewers.append({
            "github_username": body.reviewer_github_username,
            "name": body.reviewer_name or body.reviewer_github_username,
            "status": "Pending",
        })
        prd.reviewers = reviewers
        flag_modified(prd, "reviewers")

    prd.reviewer_github_username = body.reviewer_github_username
    prd.reviewer_name = body.reviewer_name
    prd.status = "In Review"
    db.commit()
    db.refresh(prd)

    if is_new:
        issue_number = _get_project_issue_number(note, db)
        if issue_number:
            try:
                gh.add_comment(
                    issue_number,
                    f"@{body.reviewer_github_username} — this PRD has been assigned to you for review.\n\n"
                    f"The full Product Requirements Document is in the comment above.\n\n"
                    f"> Reply **`APPROVED`** to approve, or leave detailed feedback below and the AI agent will update the PRD automatically.",
                    cfg=cfg,
                )
            except Exception as e:
                log.warning(f"Failed to post PRD assignment comment: {e}")

    return prd


@router.post("/{note_id}/prd/feedback", response_model=schemas.PrdResponse, status_code=202)
def submit_prd_feedback(
    note_id: str,
    body: schemas.PrdFeedbackRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    prd = db.query(models.PrdDocument).filter(
        models.PrdDocument.note_id == note_id
    ).first()
    if not prd:
        raise HTTPException(status_code=404, detail="PRD not found")
    if not prd.prd_draft:
        raise HTTPException(status_code=400, detail="PRD not yet generated")

    prd.prd_generation_phase = 0
    prd.status = "In Progress"
    db.commit()
    db.refresh(prd)
    background_tasks.add_task(_update_prd, prd.id, body.feedback)
    return prd


# TEMPORARY: manual PRD approval for full-flow testing. Remove after testing.
@router.post("/{note_id}/prd/test-approve", response_model=schemas.PrdResponse)
def test_approve_prd(
    note_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    prd = db.query(models.PrdDocument).filter(
        models.PrdDocument.note_id == note_id
    ).first()
    if not prd:
        raise HTTPException(status_code=404, detail="PRD not found")
    if not prd.prd_draft:
        raise HTTPException(status_code=400, detail="PRD not generated yet")
    if prd.status == "Approved":
        return prd

    engine_svc.run_prd_approved(prd.id)
    db.refresh(prd)
    return prd

@router.post("/{note_id}/prd/send-to-planner", response_model=schemas.PrdResponse)
def send_prd_to_planner(
    note_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    prd = db.query(models.PrdDocument).filter(
        models.PrdDocument.note_id == note_id
    ).first()
    if not prd:
        raise HTTPException(status_code=404, detail="PRD not found")
    if prd.status != "Approved":
        raise HTTPException(status_code=400, detail="PRD must be Approved before sending to Planner")

    cfg = cfg_for_user(current_user)

    prd.status = "Sent to Planner"
    db.commit()
    db.refresh(prd)

    issue_number = _get_project_issue_number(note, db)
    if issue_number:
        try:
            gh.add_comment(
                issue_number,
                "PRD approved and sent to Planner Agent.",
                cfg=cfg,
            )
        except Exception as e:
            log.warning(f"Failed to post send-to-planner comment: {e}")

    return prd
