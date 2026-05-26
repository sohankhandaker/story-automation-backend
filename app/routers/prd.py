import logging
from datetime import datetime
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services import agent
from ..services.github import cfg_for_user
from ..services import github as gh

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notes", tags=["prd"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_prd_issue_body(note: models.MeetingNote, prd_markdown: str) -> str:
    title = note.title or "PRD"
    header = (
        f"# PRD: {title}\n\n"
        f"**Product Requirements Document** — v{note.prd.current_version_number if note.prd else 1}\n\n"
        f"---\n\n"
    )
    body = header + prd_markdown
    if len(body) > 60000:
        body = body[:60000] + "\n\n_(Content truncated — see app for full PRD)_"
    return body


def _build_prd_update_comment(
    version: int,
    change_summary: str,
    changed_sections: list,
    reviewer_mention: str,
) -> str:
    sections_line = ", ".join(changed_sections) if changed_sections else "General updates"
    body = (
        f"## PRD Updated (v{version})\n\n"
        f"**Changes:** {change_summary}\n\n"
        f"**Sections updated:** {sections_line}\n\n"
        f"---\n\n"
        f"The full updated PRD is in the issue description above.\n\n"
        f"{reviewer_mention} — please review. "
        f"Reply **`APPROVED`** to approve, or leave further feedback."
    )
    return body


# ── Background tasks ──────────────────────────────────────────────────────────

def _full_prd_pipeline(prd_id: str):
    """Generate the full PRD phase by phase from the approved BRD."""
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

        def _phase_cb(phase_num: int, _total: int):
            try:
                prd.prd_generation_phase = phase_num
                db.commit()
            except Exception as e:
                log.warning(f"PRD phase callback commit failed: {e}")

        result = agent.generate_prd_from_brd(note.brd_draft, phase_callback=_phase_cb)

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

        # Update GitHub issue with full PRD
        if prd.github_issue_number:
            try:
                gh.update_issue_body(
                    prd.github_issue_number,
                    _build_prd_issue_body(note, result["full_prd"]),
                    cfg=cfg,
                )
                gh.add_comment(
                    prd.github_issue_number,
                    "PRD generation complete!\n\n"
                    "The full Product Requirements Document is now in the issue description above.\n\n"
                    "The creator will assign a reviewer shortly.",
                    cfg=cfg,
                )
            except Exception as e:
                log.warning(f"GitHub PRD publish failed: {e}")

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

        result = agent.update_prd_from_feedback(
            prd_content=prd.prd_draft,
            feedback=feedback,
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
                log.warning(f"GitHub update failed after PRD feedback: {e}")

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
    """
    Start PRD generation from an approved BRD.
    Creates PrdDocument, creates GitHub issue, fires _full_prd_pipeline in background.
    """
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

    brd_title = note.title or "BRD"
    issue = gh.create_issue(
        title=f"PRD: {brd_title}",
        body="Generating Product Requirements Document from approved BRD...\n\nThis will be updated automatically.",
        cfg=cfg,
    )
    item_id = gh.add_to_project(issue["node_id"], cfg=cfg)
    gh.update_project_status(item_id, "In Progress", cfg=cfg)

    if existing_prd:
        # Retry from Draft: reuse and reset
        existing_prd.prd_draft = None
        existing_prd.prd_generation_phase = 0
        existing_prd.status = "In Progress"
        existing_prd.current_version_number = 0
        existing_prd.github_issue_url = issue["url"]
        existing_prd.github_issue_number = issue["number"]
        existing_prd.github_issue_node_id = issue["node_id"]
        existing_prd.github_project_item_id = item_id
        existing_prd.github_last_checked_at = datetime.utcnow()
        db.commit()
        db.refresh(existing_prd)
        background_tasks.add_task(_full_prd_pipeline, existing_prd.id)
        return existing_prd

    prd = models.PrdDocument(
        note_id=note_id,
        prd_generation_phase=0,
        status="In Progress",
        github_issue_url=issue["url"],
        github_issue_number=issue["number"],
        github_issue_node_id=issue["node_id"],
        github_project_item_id=item_id,
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

    if prd.github_project_item_id:
        try:
            gh.update_project_status(prd.github_project_item_id, "In Review", cfg=cfg)
        except Exception as e:
            log.warning(f"PRD board status update failed: {e}")

    prd.reviewer_github_username = body.reviewer_github_username
    prd.reviewer_name = body.reviewer_name
    prd.status = "In Review"
    db.commit()
    db.refresh(prd)

    try:
        gh.add_comment(
            prd.github_issue_number,
            f"@{body.reviewer_github_username} — this PRD has been assigned to you for review.\n\n"
            f"The full Product Requirements Document is in the issue description above.\n\n"
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

    if prd.github_issue_number:
        try:
            gh.add_comment(
                prd.github_issue_number,
                "PRD approved and sent to Planner Agent.",
                cfg=cfg,
            )
        except Exception as e:
            log.warning(f"Failed to post send-to-planner comment: {e}")

    return prd
