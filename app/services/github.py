"""
GitHub service — REST API for issues/comments, GraphQL for Projects v2.
Supports per-user config (token/owner/repo/project) falling back to env vars.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import httpx
from ..config import settings

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"


@dataclass
class GHConfig:
    token: str
    owner: str
    repo: str
    project_number: int
    # Cached after first project lookup
    project_id: Optional[str] = field(default=None, repr=False)
    status_field_id: Optional[str] = field(default=None, repr=False)
    status_options: dict = field(default_factory=dict, repr=False)


# Global config (from env vars), initialised on startup
_global_cfg: Optional[GHConfig] = None

# Per-user cache: user_id → GHConfig
_user_cfg_cache: dict[str, GHConfig] = {}


def _env_cfg() -> GHConfig:
    return GHConfig(
        token=settings.github_token,
        owner=settings.github_owner,
        repo=settings.github_repo,
        project_number=settings.github_project_number,
        project_id=_global_cfg.project_id if _global_cfg else None,
        status_field_id=_global_cfg.status_field_id if _global_cfg else None,
        status_options=_global_cfg.status_options if _global_cfg else {},
    )


def cfg_for_user(user) -> GHConfig:
    """Return the GitHub config to use for a given user (per-user or env fallback)."""
    has_user_cfg = (
        user is not None
        and user.gh_token
        and user.gh_owner
        and user.gh_repo
        and user.gh_project_number
    )
    if not has_user_cfg:
        return _env_cfg()

    uid = user.id
    cached = _user_cfg_cache.get(uid)
    if cached and cached.token == user.gh_token and cached.owner == user.gh_owner:
        return cached

    cfg = GHConfig(
        token=user.gh_token,
        owner=user.gh_owner,
        repo=user.gh_repo,
        project_number=user.gh_project_number,
    )
    _user_cfg_cache[uid] = cfg
    return cfg


def _headers(cfg: GHConfig) -> dict:
    return {
        "Authorization": f"Bearer {cfg.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gql(query: str, variables: dict, cfg: GHConfig) -> dict:
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {cfg.token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        return data["data"]


def _ensure_project(cfg: GHConfig) -> None:
    """Lazily fetch and cache project ID + status field for the given config."""
    if cfg.project_id:
        return

    query = """
    query($login: String!, $number: Int!) {
      user(login: $login) {
        projectV2(number: $number) {
          id
          fields(first: 30) {
            nodes {
              ... on ProjectV2SingleSelectField {
                id name options { id name }
              }
            }
          }
        }
      }
    }
    """
    org_query = query.replace("user(login:", "organization(login:")

    data = None
    for q in [query, org_query]:
        try:
            result = _gql(q, {"login": cfg.owner, "number": cfg.project_number}, cfg)
            owner_key = "user" if "user" in result else "organization"
            project = result.get(owner_key, {}).get("projectV2")
            if project:
                data = project
                break
        except Exception:
            continue

    if not data:
        raise RuntimeError(
            f"Could not fetch GitHub project for owner={cfg.owner} "
            f"project_number={cfg.project_number}"
        )

    cfg.project_id = data["id"]
    for f in data["fields"]["nodes"]:
        if f.get("name", "").lower() == "status":
            cfg.status_field_id = f["id"]
            cfg.status_options = {opt["name"].lower(): opt["id"] for opt in f["options"]}
            break

    log.info(f"GitHub project ready: owner={cfg.owner} id={cfg.project_id}")


# ── Called once on startup (populates global config) ─────────────────────────

def init_project():
    global _global_cfg
    if not settings.github_token or not settings.github_owner:
        log.warning("GitHub credentials not configured — skipping project init")
        return
    cfg = GHConfig(
        token=settings.github_token,
        owner=settings.github_owner,
        repo=settings.github_repo,
        project_number=settings.github_project_number,
    )
    try:
        _ensure_project(cfg)
        _global_cfg = cfg
        log.info(f"Global GitHub project: statuses={list(cfg.status_options.keys())}")
    except Exception as e:
        log.warning(f"Global project init failed: {e}")


# ── Issues ────────────────────────────────────────────────────────────────────

def create_issue(title: str, body: str, cfg: Optional[GHConfig] = None) -> dict:
    cfg = cfg or _env_cfg()
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{REST_BASE}/repos/{cfg.owner}/{cfg.repo}/issues",
            json={"title": title, "body": body},
            headers=_headers(cfg),
        )
        resp.raise_for_status()
        data = resp.json()
        return {"url": data["html_url"], "number": data["number"], "node_id": data["node_id"]}


def update_issue_body(issue_number: int, body: str, cfg: Optional[GHConfig] = None) -> None:
    cfg = cfg or _env_cfg()
    with httpx.Client(timeout=30) as client:
        resp = client.patch(
            f"{REST_BASE}/repos/{cfg.owner}/{cfg.repo}/issues/{issue_number}",
            json={"body": body},
            headers=_headers(cfg),
        )
        resp.raise_for_status()


def add_comment(issue_number: int, body: str, cfg: Optional[GHConfig] = None) -> int:
    cfg = cfg or _env_cfg()
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{REST_BASE}/repos/{cfg.owner}/{cfg.repo}/issues/{issue_number}/comments",
            json={"body": body},
            headers=_headers(cfg),
        )
        resp.raise_for_status()
        return resp.json()["id"]


def get_comments_since(issue_number: int, since: Optional[datetime] = None,
                        cfg: Optional[GHConfig] = None) -> list[dict]:
    cfg = cfg or _env_cfg()
    params = {"per_page": 100}
    if since:
        params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            f"{REST_BASE}/repos/{cfg.owner}/{cfg.repo}/issues/{issue_number}/comments",
            params=params,
            headers=_headers(cfg),
        )
        resp.raise_for_status()
        return resp.json()


# ── Projects v2 ───────────────────────────────────────────────────────────────

def add_to_project(issue_node_id: str, cfg: Optional[GHConfig] = None) -> str:
    cfg = cfg or _env_cfg()
    _ensure_project(cfg)
    mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { id }
      }
    }
    """
    data = _gql(mutation, {"projectId": cfg.project_id, "contentId": issue_node_id}, cfg)
    return data["addProjectV2ItemById"]["item"]["id"]


def update_project_status(item_id: str, status_name: str,
                           cfg: Optional[GHConfig] = None) -> None:
    cfg = cfg or _env_cfg()
    _ensure_project(cfg)
    option_id = cfg.status_options.get(status_name.lower())
    if not option_id:
        log.warning(f"Status '{status_name}' not in options: {list(cfg.status_options.keys())}")
        return
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId itemId: $itemId
        fieldId: $fieldId value: { singleSelectOptionId: $optionId }
      }) { projectV2Item { id } }
    }
    """
    _gql(mutation, {
        "projectId": cfg.project_id,
        "itemId": item_id,
        "fieldId": cfg.status_field_id,
        "optionId": option_id,
    }, cfg)


def get_project_item_status(item_id: str, cfg: Optional[GHConfig] = None) -> Optional[str]:
    cfg = cfg or _env_cfg()
    _ensure_project(cfg)
    query = """
    query($id: ID!) {
      node(id: $id) {
        ... on ProjectV2Item {
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
      }
    }
    """
    try:
        data = _gql(query, {"id": item_id}, cfg)
        for node in data["node"]["fieldValues"]["nodes"]:
            if node.get("field", {}).get("name", "").lower() == "status":
                return node.get("name")
    except Exception as e:
        log.error(f"Error fetching project item status: {e}")
    return None
