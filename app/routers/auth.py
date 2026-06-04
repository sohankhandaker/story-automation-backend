import logging
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, schemas
from ..security import hash_password, verify_password, create_access_token
from ..deps import get_current_user
from ..config import settings

log = logging.getLogger(__name__)
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
    fields = body.model_fields_set
    if "gh_owner" in fields:
        current_user.gh_owner = body.gh_owner or None
    if "gh_repo" in fields:
        current_user.gh_repo = body.gh_repo or None
    if "gh_project_number" in fields:
        current_user.gh_project_number = body.gh_project_number or None
    if "gh_token" in fields:
        current_user.gh_token = body.gh_token or None
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("/github")
def github_auth(platform: str = "web"):
    """Redirect to GitHub OAuth — platform='web' or 'mobile'."""
    params = {
        "client_id": settings.github_client_id,
        "scope": "user:email read:user project",   # project scope needed for createProjectV2
        "state": platform,
    }
    url = "https://github.com/login/oauth/authorize?" + urlencode(params)
    return RedirectResponse(url)


@router.get("/github/callback")
def github_callback(code: str, state: str = "web", db: Session = Depends(get_db)):
    """GitHub OAuth callback — exchanges code, creates user, redirects with JWT."""
    # Exchange code for GitHub access token
    try:
        token_resp = httpx.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        gh_token = token_resp.json().get("access_token")
    except Exception as e:
        log.error(f"GitHub token exchange failed: {e}")
        raise HTTPException(status_code=502, detail="GitHub token exchange failed")

    if not gh_token:
        raise HTTPException(status_code=400, detail="GitHub denied the request")

    headers = {"Authorization": f"token {gh_token}", "Accept": "application/json"}

    # Get user profile
    gh_user = httpx.get("https://api.github.com/user", headers=headers, timeout=10).json()

    # Get primary verified email if not public
    email: str = gh_user.get("email") or ""
    if not email:
        emails = httpx.get(
            "https://api.github.com/user/emails", headers=headers, timeout=10
        ).json()
        for e in (emails if isinstance(emails, list) else []):
            if e.get("primary") and e.get("verified"):
                email = e["email"]
                break

    if not email:
        raise HTTPException(status_code=400, detail="No verified email on GitHub account")

    # Find or auto-create user
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        user = models.User(
            name=gh_user.get("name") or gh_user.get("login", ""),
            email=email,
            hashed_password="",   # no password for SSO users
            github_username=gh_user.get("login"),
            avatar_url=gh_user.get("avatar_url"),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        log.info(f"New SSO user created: {email}")

    # Always refresh profile info + OAuth token on every login
    user.name = gh_user.get("name") or gh_user.get("login") or user.name
    user.github_username = gh_user.get("login") or user.github_username
    user.avatar_url = gh_user.get("avatar_url") or user.avatar_url
    user.gh_token = gh_token                             # user's OAuth token
    user.gh_owner = settings.github_owner               # SELISEdigitalplatforms
    user.gh_repo = settings.github_repo                 # selise-madp
    user.gh_project_number = settings.github_project_number  # 447
    db.commit()
    db.refresh(user)

    jwt_token = create_access_token({"sub": user.id})

    # Redirect back to the app
    if state == "mobile":
        redirect_url = f"sera://auth?token={jwt_token}"
    else:
        redirect_url = f"{settings.web_app_url}?auth_token={jwt_token}"

    return RedirectResponse(redirect_url)


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
