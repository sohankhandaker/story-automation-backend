from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services import agent
from ..services.crawler import fetch_page_text

router = APIRouter(prefix="/api/notes", tags=["notes"])


def _generate_brd(note_id: str):
    """Background task: crawl wiki + call AI, then update the note."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == note_id).first()
        if not note:
            return
        wiki_content = fetch_page_text(note.wiki_url) if note.wiki_url else ""
        result = agent.enhance_notes_to_brd(note.raw_notes, wiki_content)
        note.title = result["title"]
        note.brd_draft = result["brd_markdown"]
        db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"BRD generation failed for note {note_id}: {e}")
    finally:
        db.close()


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
    from fastapi import HTTPException
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.delete("/{note_id}", status_code=204)
def delete_note(
    note_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    from fastapi import HTTPException
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    db.delete(note)
    db.commit()
