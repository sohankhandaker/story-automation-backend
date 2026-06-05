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
        f"*Managed by SERA*"
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

    issue: dict = {"url": None, "number": None, "node_id": None, "id": None}
    item_id: str | None = None

    # Single shared board: configured via env (GITHUB_PROJECT_NUMBER, e.g. 447).
    # We do NOT create a new board per project anymore — each project is just an
    # issue on the shared board, and its notes/PRDs become sub-issues.
    try:
        issue = gh.create_issue(title=issue_title, body=issue_body, cfg=cfg)
    except Exception as e:
        # 403 often means the user's OAuth token lacks repo write access on the org.
        # Fall back to the server PAT and retry before giving up.
        if "403" in str(e):
            fallback = gh._env_cfg()
            if fallback.token and fallback.token != cfg.token:
                log.info("User token returned 403 — retrying issue creation with server PAT")
                try:
                    issue = gh.create_issue(title=issue_title, body=issue_body, cfg=fallback)
                    cfg = fallback
                except Exception as e2:
                    log.error(f"GitHub issue creation failed (server fallback): {e2}")
                    raise HTTPException(status_code=502, detail=f"GitHub issue could not be created: {e2}.")
            else:
                log.error(f"GitHub issue creation failed: {e}")
                raise HTTPException(status_code=502, detail=f"GitHub issue could not be created: {e}.")
        else:
            log.error(f"GitHub issue creation failed: {e}")
            raise HTTPException(
                status_code=502,
                detail=(
                    f"GitHub issue could not be created: {e}. Ensure your OAuth "
                    f"token has 'repo' scope and the OAuth app is approved for the org."
                ),
            )

    try:
        item_id = gh.add_to_project(issue["node_id"], cfg=cfg)
        gh.update_project_status(item_id, "Draft", cfg=cfg)
    except Exception as e:
        log.warning(f"GitHub board item setup failed: {e}")

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
        github_issue_id=issue.get("id"),
        github_project_item_id=item_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    return _project_response(project, db)


@router.get("/debug-github")
def debug_github(current_user: models.User = Depends(get_current_user)):
    """Diagnose GitHub project board creation — returns exact error if it fails."""
    cfg = cfg_for_user(current_user)
    result = {
        "token_prefix": cfg.token[:8] + "..." if cfg.token else "MISSING",
        "owner": cfg.owner,
        "repo": cfg.repo,
        "project_number": cfg.project_number,
    }

    # Step 1: resolve owner node ID
    try:
        owner_id = gh._resolve_owner_id(cfg.owner, cfg)
        result["owner_id"] = owner_id
        result["owner_id_ok"] = bool(owner_id)
    except Exception as e:
        result["owner_id_error"] = str(e)
        return result

    if not owner_id:
        result["owner_id_error"] = f"Could not resolve node ID for {cfg.owner!r}"
        return result

    # Step 2: attempt createProjectV2
    try:
        board = gh._do_create_project_v2(owner_id, "SERA Debug Test Board", cfg)
        result["board_created"] = True
        result["board_url"] = board["project_url"]
        # Clean up: delete the test board immediately
        try:
            from ..services.github import _gql
            _gql(
                "mutation($id:ID!){deleteProjectV2(input:{projectId:$id}){projectV2{id}}}",
                {"id": board["project_id"]}, cfg
            )
            result["test_board_deleted"] = True
        except Exception:
            result["test_board_deleted"] = False
            result["note"] = f"Test board created at {board['project_url']} — delete manually"
    except Exception as e:
        result["board_created"] = False
        result["error"] = str(e)

    return result


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

    # Collect GitHub issue refs to delete after DB cleanup
    cfg = cfg_for_project(current_user, project)
    gh_issues_to_delete: list[tuple[str, int]] = []
    for note in notes:
        prd = db.query(models.PrdDocument).filter(
            models.PrdDocument.note_id == note.id
        ).first()
        if prd and prd.github_issue_node_id and prd.github_issue_number:
            gh_issues_to_delete.append((prd.github_issue_node_id, prd.github_issue_number))
        if note.github_issue_node_id and note.github_issue_number and \
           note.github_issue_number != project.github_issue_number:
            gh_issues_to_delete.append((note.github_issue_node_id, note.github_issue_number))

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

    project_issue_node_id = project.github_issue_node_id
    project_issue_number = project.github_issue_number
    db.delete(project)
    db.commit()

    # Delete the project's main GitHub issue and all sub-issues.
    if project_issue_node_id and project_issue_number:
        try:
            gh.delete_issue(project_issue_node_id, project_issue_number, cfg=cfg)
        except Exception as e:
            log.warning(f"GitHub project issue delete failed: {e}")
    for node_id, number in gh_issues_to_delete:
        try:
            gh.delete_issue(node_id, number, cfg=cfg)
        except Exception as e:
            log.warning(f"GitHub sub-issue #{number} delete failed: {e}")


def _customer_dict(c: models.Customer, db: Session) -> dict:
    from .customers import _customer_response
    return _customer_response(c, db)


def _project_response(project: models.Project, db: Session) -> dict:
    notes_count = db.query(models.MeetingNote).filter(
        models.MeetingNote.project_id == project.id,
        models.MeetingNote.note_type != "change_request",
    ).count()
    change_request_count = db.query(models.MeetingNote).filter(
        models.MeetingNote.project_id == project.id,
        models.MeetingNote.note_type == "change_request",
    ).count()
    customer_data = None
    if project.customer_id:
        c = db.query(models.Customer).filter(
            models.Customer.id == project.customer_id
        ).first()
        if c:
            customer_data = _customer_dict(c, db)
    # True when any note under this project has a PRD sent to the Planner
    has_sent_prd = db.query(models.PrdDocument).join(
        models.MeetingNote, models.MeetingNote.id == models.PrdDocument.note_id
    ).filter(
        models.MeetingNote.project_id == project.id,
        models.PrdDocument.status == "Sent to Planner",
    ).first() is not None
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
        "has_sent_prd": has_sent_prd,
        "notes_count": notes_count,
        "change_request_count": change_request_count,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }
