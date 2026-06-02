import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services.github import cfg_for_user
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

    issue_title = f"[{body.client_name}] {body.title}"
    issue_body = _build_project_issue_body(body.title, body.client_name, body.short_description)

    try:
        issue = gh.create_issue(title=issue_title, body=issue_body, cfg=cfg)
        item_id = gh.add_to_project(issue["node_id"], cfg=cfg)
        gh.update_project_status(item_id, "Backlog", cfg=cfg)
    except Exception as e:
        log.warning(f"GitHub issue creation failed for project: {e}")
        issue = {"url": None, "number": None, "node_id": None}
        item_id = None

    # If customer_id provided, pull client_name from it
    client_name = body.client_name
    if body.customer_id:
        customer = db.query(models.Customer).filter(
            models.Customer.id == body.customer_id,
            models.Customer.creator_id == current_user.id,
        ).first()
        if customer:
            client_name = customer.name

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
        "status": project.status,
        "notes_count": notes_count,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }
