import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .services import github as gh
from .services import engine
from .routers import auth, tasks, chat, webhooks, notes, prd, projects, customers

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
    title="SERA API",
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
app.include_router(customers.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(chat.router)
app.include_router(webhooks.router)
app.include_router(notes.router)
app.include_router(notes.projects_notes_router)
app.include_router(prd.router)


@app.get("/health")
def health():
    return {"status": "ok"}
