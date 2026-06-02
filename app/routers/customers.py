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
    db.delete(c)
    db.commit()
