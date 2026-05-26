import logging
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services import agent
from ..services.crawler import fetch_page_text

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notes", tags=["notes"])


# ── Background tasks ──────────────────────────────────────────────────────────

def _generate_brd(note_id: str):
    """Initial BRD generation: crawl wiki + 7-phase AI generation."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == note_id).first()
        if not note:
            return

        wiki_content = fetch_page_text(note.wiki_url) if note.wiki_url else ""

        def _phase_cb(phase_num: int, total: int):
            try:
                note.brd_generation_phase = phase_num
                db.commit()
            except Exception as e:
                log.warning(f"Phase callback commit failed: {e}")

        result = agent.enhance_notes_to_brd(note.raw_notes, wiki_content, phase_callback=_phase_cb)

        # Save snapshot as version 1
        v1 = models.BrdVersion(
            note_id=note.id,
            version_number=1,
            brd_markdown=result["brd_markdown"],
            change_summary="Initial AI generation",
            changed_sections=[],
        )
        db.add(v1)

        note.title = result["title"]
        note.brd_draft = result["brd_markdown"]
        note.brd_generation_phase = None
        note.current_version_number = 1
        db.commit()

    except Exception as e:
        log.error(f"BRD generation failed for note {note_id}: {e}")
        try:
            note.brd_generation_phase = None
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _update_brd(note_id: str, feedback: str):
    """Update BRD from reviewer feedback, save new version."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == note_id).first()
        if not note or not note.brd_draft:
            return

        result = agent.update_brd_from_feedback(
            current_brd=note.brd_draft,
            reviewer_comments=[feedback],
        )

        next_ver = (note.current_version_number or 1) + 1
        version = models.BrdVersion(
            note_id=note.id,
            version_number=next_ver,
            brd_markdown=result["updated_markdown"],
            change_summary=result["change_summary"],
            changed_sections=result["changed_sections"],
            reviewer_comment=feedback,
        )
        db.add(version)

        note.brd_draft = result["updated_markdown"]
        note.current_version_number = next_ver
        note.brd_generation_phase = None
        db.commit()

    except Exception as e:
        log.error(f"BRD update failed for note {note_id}: {e}")
        try:
            note.brd_generation_phase = None
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("", response_model=schemas.MeetingNoteResponse, status_code=202)
def create_note(
    body: schemas.NotesEnhanceRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    note = models.MeetingNote(
        creator_id=current_user.id,
        raw_notes=body.raw_notes,
        wiki_url=body.wiki_url or None,
        title="Processing…",
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    background_tasks.add_task(_generate_brd, note.id)
    return note


@router.post("/{note_id}/feedback", response_model=schemas.MeetingNoteResponse, status_code=202)
def submit_feedback(
    note_id: str,
    body: schemas.BrdFeedbackRequest,
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
    if not note.brd_draft:
        raise HTTPException(status_code=400, detail="BRD not yet generated")

    note.brd_generation_phase = 0   # 0 = update in progress (distinct from generation phases 1-7)
    note.status = "Changes Requested"
    db.commit()
    db.refresh(note)
    background_tasks.add_task(_update_brd, note.id, body.feedback)
    return note


@router.get("", response_model=schemas.MeetingNotesListResponse)
def list_notes(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notes = (
        db.query(models.MeetingNote)
        .filter(models.MeetingNote.creator_id == current_user.id)
        .order_by(models.MeetingNote.created_at.desc())
        .all()
    )
    return {"notes": notes, "total": len(notes)}


@router.get("/{note_id}", response_model=schemas.MeetingNoteResponse)
def get_note(
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
    return note


@router.get("/{note_id}/versions", response_model=schemas.BrdVersionListResponse)
def list_versions(
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
    versions = (
        db.query(models.BrdVersion)
        .filter(models.BrdVersion.note_id == note_id)
        .order_by(models.BrdVersion.version_number.desc())
        .all()
    )
    return {"versions": versions, "total": len(versions)}


@router.delete("/{note_id}", status_code=204)
def delete_note(
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
    db.delete(note)
    db.commit()
