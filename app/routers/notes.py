import logging
import re
from datetime import datetime
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services import agent
from ..services.crawler import fetch_page_text
from ..services.github import cfg_for_user
from ..services import github as gh

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notes", tags=["notes"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _combined_notes(note: models.MeetingNote) -> str:
    """Combine all note entries chronologically into a single string."""
    entries = sorted(note.entries or [], key=lambda e: e.created_at)
    if entries:
        return "\n\n---\n\n".join(
            f"[Entry {i + 1} — {e.created_at.strftime('%Y-%m-%d %H:%M')}]\n{e.content}"
            for i, e in enumerate(entries)
        )
    return note.raw_notes


def _build_brd_issue_body(note: models.MeetingNote, brd_markdown: str) -> str:
    header = (
        f"# {note.title}\n\n"
        f"**BRD Document** — v{note.current_version_number}\n\n"
        f"---\n\n"
    )
    body = header + brd_markdown
    if len(body) > 60000:
        body = body[:60000] + "\n\n_(Content truncated — see app for full BRD)_"
    return body


# ── Background tasks ──────────────────────────────────────────────────────────

def _full_brd_pipeline(note_id: str):
    """
    Full pipeline triggered by Mark as Ready:
      Phase 1 — crawl all URLs in notes + wiki_url
      Phase 2 — AI analysis of all notes
      Phases 3-9 — 7-phase BRD generation using analysis as context
    Sets status to "Pending Review" when done.
    """
    from ..database import SessionLocal
    db = SessionLocal()
    note = None
    try:
        note = db.query(models.MeetingNote).filter(models.MeetingNote.id == note_id).first()
        if not note:
            return

        creator = db.query(models.User).filter(models.User.id == note.creator_id).first()
        cfg = cfg_for_user(creator)

        combined = _combined_notes(note)

        # Phase 1: Crawl URLs
        note.brd_generation_phase = 1
        db.commit()

        urls_to_crawl = []
        if note.wiki_url:
            urls_to_crawl.append(note.wiki_url)
        for u in re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', combined)[:3]:
            if u not in urls_to_crawl:
                urls_to_crawl.append(u)

        crawled_content = ""
        for url in urls_to_crawl[:4]:
            try:
                content = fetch_page_text(url)
                if content:
                    crawled_content += f"\n\n[From {url}]\n{content[:3000]}"
            except Exception as e:
                log.warning(f"Crawl failed for {url}: {e}")

        # Phase 2: Analyze notes
        note.brd_generation_phase = 2
        db.commit()

        analysis = agent.analyze_notes_to_draft(combined, crawled_content)

        # Update GitHub issue with analysis so the ticket has meaningful content immediately
        if note.github_issue_number:
            try:
                analysis_body = (
                    f"# BRD In Progress\n\n"
                    f"*Generating full BRD — estimated 1–2 minutes…*\n\n"
                    f"---\n\n## Notes Analysis\n\n{analysis}"
                )
                if len(analysis_body) > 60000:
                    analysis_body = analysis_body[:60000] + "\n\n_(truncated)_"
                gh.update_issue_body(note.github_issue_number, analysis_body, cfg=cfg)
            except Exception as e:
                log.warning(f"GitHub analysis update failed: {e}")

        # Extract tentative title from analysis
        for line in analysis.splitlines():
            stripped = line.lstrip("#* ").strip("* ").strip()
            if stripped and len(stripped) > 3 and not stripped.lower().startswith("notes analysis"):
                if line.startswith("#"):
                    note.title = stripped
                    db.commit()
                    break

        # Phases 3-9: 7-phase BRD generation (offset +2 from BRD phase number)
        def _phase_cb(phase_num: int, _total: int):
            try:
                note.brd_generation_phase = phase_num + 2
                db.commit()
            except Exception as e:
                log.warning(f"Phase callback commit failed: {e}")

        result = agent.enhance_notes_to_brd(
            combined, crawled_content, analysis=analysis, phase_callback=_phase_cb
        )

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
        note.status = "Pending Review"
        db.commit()

        # Update GitHub issue with full BRD
        if note.github_issue_number:
            try:
                gh.update_issue_body(
                    note.github_issue_number,
                    _build_brd_issue_body(note, result["brd_markdown"]),
                    cfg=cfg,
                )
                gh.add_comment(
                    note.github_issue_number,
                    "✅ BRD generation complete!\n\n"
                    "The full Business Requirements Document is now in the issue description above.\n\n"
                    "The creator will assign a reviewer shortly.",
                    cfg=cfg,
                )
            except Exception as e:
                log.warning(f"GitHub BRD publish failed: {e}")

    except Exception as e:
        log.error(f"BRD pipeline failed for note {note_id}: {e}")
        try:
            if note:
                note.brd_generation_phase = None
                note.status = "Draft"  # allow user to retry
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _update_brd(note_id: str, feedback: str):
    """Update BRD from reviewer/creator feedback, save new version."""
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

        if note.github_issue_number:
            try:
                creator = db.query(models.User).filter(
                    models.User.id == note.creator_id
                ).first()
                cfg = cfg_for_user(creator)
                gh.update_issue_body(
                    note.github_issue_number,
                    _build_brd_issue_body(note, result["updated_markdown"]),
                    cfg=cfg,
                )
                reviewer = f"@{note.reviewer_github_username}" if note.reviewer_github_username else "Reviewer"
                from ..services.engine import _build_brd_update_comment
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
                log.warning(f"GitHub update failed after BRD feedback: {e}")

        note.status = "In Review"
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

@router.post("", response_model=schemas.MeetingNoteResponse, status_code=201)
def create_note(
    body: schemas.NotesEnhanceRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Save the first note entry. No BRD generation — that happens on Mark as Ready."""
    note = models.MeetingNote(
        creator_id=current_user.id,
        raw_notes=body.raw_notes,
        wiki_url=body.wiki_url or None,
        title="New Note",
        status="Draft",
    )
    db.add(note)
    db.flush()

    entry = models.NoteEntry(note_id=note.id, content=body.raw_notes)
    db.add(entry)
    db.commit()
    db.refresh(note)
    return note


@router.post("/{note_id}/entries", response_model=schemas.MeetingNoteResponse, status_code=201)
def add_entry(
    note_id: str,
    body: schemas.NoteEntryCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Add another note entry while still in Draft. No BRD generation yet."""
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.status != "Draft":
        raise HTTPException(status_code=400, detail="Can only add notes while in Draft status")

    entry = models.NoteEntry(note_id=note.id, content=body.content)
    db.add(entry)
    db.commit()
    db.refresh(note)
    return note


@router.get("/{note_id}/entries", response_model=schemas.NoteEntriesListResponse)
def list_entries(
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
    entries = (
        db.query(models.NoteEntry)
        .filter(models.NoteEntry.note_id == note_id)
        .order_by(models.NoteEntry.created_at.asc())
        .all()
    )
    return {"entries": entries, "total": len(entries)}


@router.post("/{note_id}/mark-ready", response_model=schemas.MeetingNoteResponse, status_code=202)
def mark_ready(
    note_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Create GitHub ticket, move board to In Progress, fire the full BRD pipeline.
    No reviewer assignment here — that happens after BRD is generated.
    """
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.status != "Draft":
        raise HTTPException(status_code=400, detail=f"Note is already {note.status}")

    cfg = cfg_for_user(current_user)

    preview = note.raw_notes[:60] + ("…" if len(note.raw_notes) > 60 else "")
    issue = gh.create_issue(
        title=f"BRD: {preview}",
        body="🔄 Analyzing meeting notes and generating BRD…\n\nThis will be updated automatically.",
        cfg=cfg,
    )
    item_id = gh.add_to_project(issue["node_id"], cfg=cfg)
    gh.update_project_status(item_id, "In Progress", cfg=cfg)

    note.github_issue_url = issue["url"]
    note.github_issue_number = issue["number"]
    note.github_issue_node_id = issue["node_id"]
    note.github_project_item_id = item_id
    note.status = "In Progress"
    note.brd_generation_phase = 0
    note.github_last_checked_at = datetime.utcnow()
    db.commit()
    db.refresh(note)

    background_tasks.add_task(_full_brd_pipeline, note.id)
    return note


@router.post("/{note_id}/assign-reviewer", response_model=schemas.MeetingNoteResponse)
def assign_reviewer(
    note_id: str,
    body: schemas.AssignReviewerRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Assign reviewer once BRD is ready. Moves board to In Review and notifies reviewer."""
    note = db.query(models.MeetingNote).filter(
        models.MeetingNote.id == note_id,
        models.MeetingNote.creator_id == current_user.id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.status != "Pending Review":
        raise HTTPException(status_code=400, detail="BRD is not ready for reviewer assignment")

    cfg = cfg_for_user(current_user)

    if note.github_project_item_id:
        try:
            gh.update_project_status(note.github_project_item_id, "In Review", cfg=cfg)
        except Exception as e:
            log.warning(f"Board status update failed: {e}")

    note.reviewer_github_username = body.reviewer_github_username
    note.reviewer_name = body.reviewer_name
    note.status = "In Review"
    db.commit()
    db.refresh(note)

    try:
        gh.add_comment(
            note.github_issue_number,
            f"👋 @{body.reviewer_github_username} — this BRD has been assigned to you for review.\n\n"
            f"The full Business Requirements Document is in the issue description above.\n\n"
            f"> Reply **`APPROVED`** to approve, or leave detailed feedback below and the AI agent will update the BRD automatically.",
            cfg=cfg,
        )
    except Exception as e:
        log.warning(f"Failed to post assignment comment: {e}")

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

    note.brd_generation_phase = 0
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
