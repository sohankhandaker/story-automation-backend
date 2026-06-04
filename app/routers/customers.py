import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/customers", tags=["customers"])


def _customer_response(c: models.Customer, db: Session) -> dict:
    projects_count = db.query(models.Project).filter(
        models.Project.customer_id == c.id
    ).count()
    return {
        "id": c.id,
        "name": c.name,
        "url": c.url,
        "short_description": c.short_description,
        "projects_count": projects_count,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


@router.post("", response_model=schemas.CustomerResponse, status_code=201)
def create_customer(
    body: schemas.CustomerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    customer = models.Customer(
        creator_id=current_user.id,
        name=body.name,
        url=body.url or None,
        short_description=body.short_description or None,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return _customer_response(customer, db)


@router.get("", response_model=schemas.CustomerListResponse)
def list_customers(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    customers = (
        db.query(models.Customer)
        .filter(models.Customer.creator_id == current_user.id)
        .order_by(models.Customer.name)
        .all()
    )
    return {
        "customers": [_customer_response(c, db) for c in customers],
        "total": len(customers),
    }


@router.get("/{customer_id}", response_model=schemas.CustomerResponse)
def get_customer(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    c = db.query(models.Customer).filter(
        models.Customer.id == customer_id,
        models.Customer.creator_id == current_user.id,
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    return _customer_response(c, db)


@router.patch("/{customer_id}", response_model=schemas.CustomerResponse)
def update_customer(
    customer_id: str,
    body: schemas.CustomerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    c = db.query(models.Customer).filter(
        models.Customer.id == customer_id,
        models.Customer.creator_id == current_user.id,
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    c.name = body.name
    c.url = body.url or None
    c.short_description = body.short_description or None
    db.commit()
    db.refresh(c)
    return _customer_response(c, db)


@router.get("/{customer_id}/projects", response_model=schemas.ProjectListResponse)
def list_customer_projects(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    c = db.query(models.Customer).filter(
        models.Customer.id == customer_id,
        models.Customer.creator_id == current_user.id,
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")

    from .projects import _project_response
    projects = (
        db.query(models.Project)
        .filter(
            models.Project.customer_id == customer_id,
            models.Project.creator_id == current_user.id,
        )
        .order_by(models.Project.created_at.desc())
        .all()
    )
    return {
        "projects": [_project_response(p, db) for p in projects],
        "total": len(projects),
    }


@router.delete("/{customer_id}", status_code=204)
def delete_customer(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    c = db.query(models.Customer).filter(
        models.Customer.id == customer_id,
        models.Customer.creator_id == current_user.id,
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")

    # Block deletion if any note under any project of this customer has an approved PRD
    project_ids = [
        p.id for p in db.query(models.Project.id).filter(
            models.Project.customer_id == customer_id
        ).all()
    ]
    if project_ids:
        approved_prd = (
            db.query(models.PrdDocument)
            .join(models.MeetingNote, models.MeetingNote.id == models.PrdDocument.note_id)
            .filter(
                models.MeetingNote.project_id.in_(project_ids),
                models.PrdDocument.status == "Approved",
            )
            .first()
        )
        if approved_prd:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete customer: one or more projects contain notes with an approved PRD.",
            )

    # Cascade: delete projects → notes → all child records
    projects = db.query(models.Project).filter(
        models.Project.customer_id == customer_id
    ).all()
    for project in projects:
        notes = db.query(models.MeetingNote).filter(
            models.MeetingNote.project_id == project.id
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

    db.delete(c)
    db.commit()
