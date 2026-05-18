from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime
from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..services import github as gh, agent, engine
from ..services.github import cfg_for_user

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("", response_model=schemas.TaskListResponse)
def list_tasks(
    status: str = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    q = db.query(models.Task).filter(models.Task.creator_id == current_user.id)
    if status:
        q = q.filter(models.Task.status == status)
    total = q.count()
    tasks = q.order_by(models.Task.created_at.desc()).offset((page - 1) * limit).limit(limit).all()
    return {"tasks": tasks, "total": total}


@router.post("", response_model=dict)
def create_task(
    body: schemas.TaskCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """First chat message — agent extracts info and creates GitHub issue + board item."""
    # Extract title and description from the chat message
    try:
        extracted = agent.extract_backlog_info(body.message)
    except Exception as e:
        extracted = {"title": body.message[:80], "description": body.message}

    # Create task in DB
    task = models.Task(
        title=extracted["title"],
        description=extracted["description"],
        status="Backlog",
        creator_id=current_user.id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # Save user's first chat message
    user_msg = models.ChatMessage(
        task_id=task.id,
        sender_id=current_user.id,
        sender_type="User",
        content=body.message,
    )
    db.add(user_msg)

    # Create GitHub issue
    user_cfg = cfg_for_user(current_user)
    issue_body = f"**Requirement:**\n\n{extracted['description']}\n\n*Created via Story Automation App*"
    try:
        issue = gh.create_issue(extracted["title"], issue_body, cfg=user_cfg)
        task.github_issue_url = issue["url"]
        task.github_issue_number = issue["number"]
        task.github_issue_node_id = issue["node_id"]

        # Add to GitHub Projects v2
        try:
            item_id = gh.add_to_project(issue["node_id"], cfg=user_cfg)
            task.github_project_item_id = item_id
            gh.update_project_status(item_id, "Backlog", cfg=user_cfg)
        except Exception as e:
            pass  # Board add is best-effort; task still created

        agent_reply = (
            f"Got it! I've created a backlog item on your GitHub board:\n\n"
            f"**Title:** {extracted['title']}\n"
            f"**Status:** Backlog\n"
            f"**GitHub Issue:** {issue['url']}\n\n"
            f"You can keep adding details here, or tap **Mark as Ready** when done."
        )
    except Exception as e:
        agent_reply = (
            f"Created the task locally (GitHub issue creation failed: {str(e)}).\n\n"
            f"**Title:** {extracted['title']}\n\n"
            f"You can keep adding details or tap **Mark as Ready**."
        )

    agent_msg = models.ChatMessage(
        task_id=task.id,
        sender_type="Agent",
        content=agent_reply,
    )
    db.add(agent_msg)
    db.commit()

    return {
        "task_id": task.id,
        "title": task.title,
        "github_issue_url": task.github_issue_url,
        "agent_reply": agent_reply,
    }


@router.get("/{task_id}", response_model=schemas.TaskResponse)
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = db.query(models.Task).filter(
        models.Task.id == task_id,
        models.Task.creator_id == current_user.id,
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}/status")
def update_status(
    task_id: str,
    body: schemas.TaskStatusUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = db.query(models.Task).filter(
        models.Task.id == task_id,
        models.Task.creator_id == current_user.id,
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if body.status == "Ready" and task.status == "Backlog":
        task.status = "Ready"
        task.updated_at = datetime.utcnow()
        db.commit()

        # Update GitHub board
        if task.github_project_item_id:
            try:
                gh.update_project_status(
                    task.github_project_item_id, "Ready",
                    cfg=cfg_for_user(current_user),
                )
            except Exception as e:
                pass

        # Add system message
        sys_msg = models.ChatMessage(
            task_id=task.id,
            sender_type="System",
            content="Task marked as Ready. The agent will now pick it up and create the full story.",
        )
        db.add(sys_msg)
        db.commit()

        # Trigger WorkflowEngine in background
        engine.trigger_ready_column(task.id)
        return {"status": "Ready", "message": "WorkflowEngine triggered"}

    raise HTTPException(status_code=400, detail=f"Cannot transition from {task.status} to {body.status}")
