import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services.github import cfg_for_user, cfg_for_project
from ..services import github as gh

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects", tags=["projects"])


def _build_project_issue_body(title: str, client_name: str, short_description: str | None) -> str:
    desc_block = f"\n{short_description}\n" if short_description else ""
    return (
        f"## {title}\n"
        f"**Client:** {client_name}\n"
        f"{desc_block}\n"
        f"---\n\n"
        f"All Business Requirements and Product documents for this project are tracked "
        f"as comments below.\n\n"
        f"*Managed by Story Automation*"
    )


@router.post("", response_model=schemas.ProjectResponse, status_code=201)
def create_project(
    body: schemas.ProjectCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    cfg = cfg_for_user(current_user)

    # If customer_id provided, pull client_name from it
    client_name = body.client_name
    if body.customer_id:
        customer = db.query(models.Customer).filter(
            models.Customer.id == body.customer_id,
            models.Customer.creator_id == current_user.id,
        ).first()
        if customer:
            client_name = customer.name

    issue_title = f"[{client_name}] {body.title}"
    issue_body = _build_project_issue_body(body.title, client_name, body.short_description)

    issue: dict = {"url": None, "number": None, "node_id": None}
    item_id: str | None = None
    board_node_id: str | None = None
    board_url: str | None = None
    status_field_id: str | None = None
    status_options: dict = {}

    try:
        # 1. Create a dedicated GitHub Project board for this SERA project
        board_title = f"{client_name} — {body.title}"
        board = gh.create_project_board(board_title, body.short_description or "", cfg=cfg)
        board_node_id   = board["project_id"]
        board_url       = board["project_url"]
        status_field_id = board["status_field_id"]
        status_options  = board["status_options"]

        # Build a cfg that points at the new board so subsequent calls use it
        from dataclasses import replace as dc_replace
        board_cfg = dc_replace(
            cfg,
            project_id=board_node_id,
            status_field_id=status_field_id,
            status_options=status_options,
        )

        # 2. Create GitHub Issue in the repo
        issue = gh.create_issue(title=issue_title, body=issue_body, cfg=board_cfg)

        # 3. Add issue to the new board and set Backlog status
        item_id = gh.add_to_project(issue["node_id"], cfg=board_cfg)
        gh.update_project_status(item_id, "Backlog", cfg=board_cfg)

    except Exception as e:
        log.warning(f"GitHub setup failed for project: {e}")

    project = models.Project(
        creator_id=current_user.id,
        customer_id=body.customer_id or None,
        title=body.title,
        client_name=client_name,
        url=body.url,
        short_description=body.short_description,
        github_issue_url=issue.get("url"),
        github_issue_number=issue.get("number"),
        github_issue_node_id=issue.get("node_id"),
        github_project_item_id=item_id,
        github_project_node_id=board_node_id,
        github_project_url=board_url,
        github_status_field_id=status_field_id,
        github_status_options=status_options,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    return _project_response(project, db)


@router.get("", response_model=schemas.ProjectListResponse)
def list_projects(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    projects = (
        db.query(models.Project)
        .filter(models.Project.creator_id == current_user.id)
        .order_by(models.Project.created_at.desc())
        .all()
    )
    return {
        "projects": [_project_response(p, db) for p in projects],
        "total": len(projects),
    }


@router.get("/{project_id}", response_model=schemas.ProjectResponse)
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.creator_id == current_user.id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_response(project, db)


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.creator_id == current_user.id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Block deletion if any note under this project has an approved PRD
    approved_prd = (
        db.query(models.PrdDocument)
        .join(models.MeetingNote, models.MeetingNote.id == models.PrdDocument.note_id)
        .filter(
            models.MeetingNote.project_id == project_id,
            models.PrdDocument.status == "Approved",
        )
        .first()
    )
    if approved_prd:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete project: it contains notes with an approved PRD.",
        )

    # Cascade: delete notes → entries, attachments, BRD/PRD versions
    notes = db.query(models.MeetingNote).filter(
        models.MeetingNote.project_id == project_id
    ).all()
    for note in notes:
        db.query(models.NoteEntry).filter(
            models.NoteEntry.note_id == note.id
        ).delete(synchronize_session=False)
        db.query(models.NoteAttachment).filter(
            models.NoteAttachment.note_id == note.id
        ).delete(synchronize_session=False)
        db.query(models.BrdVersion).filter(
            models.BrdVersion.note_id == note.id
        ).delete(synchronize_session=False)
        prd = db.query(models.PrdDocument).filter(
            models.PrdDocument.note_id == note.id
        ).first()
        if prd:
            db.query(models.PrdVersion).filter(
                models.PrdVersion.prd_id == prd.id
            ).delete(synchronize_session=False)
            db.delete(prd)
        db.delete(note)

    db.delete(project)
    db.commit()


def _customer_dict(c: models.Customer, db: Session) -> dict:
    from .customers import _customer_response
    return _customer_response(c, db)


def _project_response(project: models.Project, db: Session) -> dict:
    notes_count = db.query(models.MeetingNote).filter(
        models.MeetingNote.project_id == project.id
    ).count()
    customer_data = None
    if project.customer_id:
        c = db.query(models.Customer).filter(
            models.Customer.id == project.customer_id
        ).first()
        if c:
            customer_data = _customer_dict(c, db)
    return {
        "id": project.id,
        "title": project.title,
        "client_name": project.client_name,
        "url": project.url,
        "short_description": project.short_description,
        "customer_id": project.customer_id,
        "customer": customer_data,
        "github_issue_url": project.github_issue_url,
        "github_issue_number": project.github_issue_number,
        "github_project_url": project.github_project_url,
        "status": project.status,
        "notes_count": notes_count,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }
