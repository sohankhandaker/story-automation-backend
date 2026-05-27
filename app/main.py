import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .database import init_db
from .services import github as gh
from .services import engine
from .routers import auth, tasks, chat, webhooks, notes, prd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up...")
    init_db()
    gh.init_project()
    engine.start_scheduler()
    yield
    log.info("Shutting down...")
    engine.stop_scheduler()


app = FastAPI(
    title="Story Automation API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(tasks.router)
app.include_router(chat.router)
app.include_router(webhooks.router)
app.include_router(notes.router)
app.include_router(prd.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.delete("/admin/purge-all", include_in_schema=False)
def purge_all(x_purge_token: str = Header(default=None)):
    if x_purge_token != "purge-selise-2026":
        raise HTTPException(status_code=403, detail="Forbidden")
    from .database import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text(
            "TRUNCATE TABLE prd_versions, prd_documents, brd_versions, "
            "note_attachments, note_entries, meeting_notes, "
            "activity_logs, review_cycles, chat_messages, tasks, users "
            "RESTART IDENTITY CASCADE"
        ))
        db.commit()
        return {"status": "all data purged"}
    finally:
        db.close()
