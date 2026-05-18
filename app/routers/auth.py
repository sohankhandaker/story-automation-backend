from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..security import hash_password, verify_password, create_access_token
from ..deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=schemas.Token)
def register(body: schemas.UserCreate, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.email == body.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = models.User(
        name=body.name,
        email=body.email,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token({"sub": user.id})
    return {"access_token": token, "token_type": "bearer", "user": user}


@router.post("/login", response_model=schemas.Token)
def login(body: schemas.UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == body.email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token({"sub": user.id})
    return {"access_token": token, "token_type": "bearer", "user": user}


@router.get("/me", response_model=schemas.UserResponse)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user


@router.patch("/settings", response_model=schemas.UserResponse)
def update_settings(
    body: schemas.SettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if body.github_username is not None:
        current_user.github_username = body.github_username
    if body.reviewer_list is not None:
        current_user.reviewer_list = [r.model_dump() for r in body.reviewer_list]
    if body.gh_owner is not None:
        current_user.gh_owner = body.gh_owner or None
    if body.gh_repo is not None:
        current_user.gh_repo = body.gh_repo or None
    if body.gh_project_number is not None:
        current_user.gh_project_number = body.gh_project_number
    if body.gh_token is not None:
        current_user.gh_token = body.gh_token or None
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/device-token")
def register_device_token(
    body: schemas.DeviceTokenCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    tokens = list(current_user.device_tokens or [])
    if body.device_token not in tokens:
        tokens.append(body.device_token)
        current_user.device_tokens = tokens
        db.commit()
    return {"status": "ok"}
