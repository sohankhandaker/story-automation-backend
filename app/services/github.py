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


# ── Per-project board helper ─────────────────────────────────────────────────

def cfg_for_project(user, project) -> GHConfig:
    """Return a GHConfig pre-loaded with the project's own board details."""
    base = cfg_for_user(user)
    if (
        project is not None
        and getattr(project, "github_project_node_id", None)
    ):
        opts = getattr(project, "github_status_options", {}) or {}
        return GHConfig(
            token=base.token,
            owner=base.owner,
            repo=base.repo,
            project_number=getattr(project, "github_project_number_val", 0) or 0,
            project_id=project.github_project_node_id,
            status_field_id=getattr(project, "github_status_field_id", None),
            status_options=opts,
        )
    return base


# ── Create a new GitHub Project v2 board ──────────────────────────────────────

def _resolve_owner_id(login: str, cfg: GHConfig) -> Optional[str]:
    """Return the GraphQL node ID for an org or user login."""
    for q in [
        "query($l:String!){organization(login:$l){id}}",
        "query($l:String!){user(login:$l){id}}",
    ]:
        try:
            res = _gql(q, {"l": login}, cfg)
            oid = (res.get("organization") or res.get("user") or {}).get("id")
            if oid:
                return oid
        except Exception:
            continue
    return None


def _get_authenticated_user_node_id(cfg: GHConfig) -> Optional[str]:
    """Return the node_id of the token owner via REST."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{REST_BASE}/user", headers=_headers(cfg))
            resp.raise_for_status()
            return resp.json().get("node_id")
    except Exception:
        return None


def _do_create_project_v2(owner_id: str, title: str, cfg: GHConfig) -> dict:
    """Run createProjectV2 mutation and return project metadata."""
    create_q = """
    mutation($ownerId:ID!, $title:String!) {
      createProjectV2(input:{ownerId:$ownerId, title:$title}) {
        projectV2 {
          id number url
          fields(first:20) {
            nodes {
              ... on ProjectV2SingleSelectField { id name options { id name } }
            }
          }
        }
      }
    }
    """
    data = _gql(create_q, {"ownerId": owner_id, "title": title}, cfg)
    project = data["createProjectV2"]["projectV2"]

    status_field_id = None
    status_options: dict = {}
    for f in project["fields"]["nodes"]:
        if f.get("name", "").lower() == "status":
            status_field_id = f["id"]
            status_options = {opt["name"].lower(): opt["id"] for opt in f["options"]}
            break

    return {
        "project_id":      project["id"],
        "project_number":  project["number"],
        "project_url":     project["url"],
        "status_field_id": status_field_id,
        "status_options":  status_options,
    }


def create_project_board(title: str, description: str, cfg: GHConfig) -> dict:
    """
    Create a new GitHub Project v2.
    Strategy:
      1. Try under the configured owner (org SELISEdigitalplatforms) — needs org-admin rights.
      2. If that fails, try under the authenticated user's own account — needs only 'project' scope.
    """
    # Strategy 1: configured org/owner
    org_owner_id = _resolve_owner_id(cfg.owner, cfg)
    if org_owner_id:
        try:
            result = _do_create_project_v2(org_owner_id, title, cfg)
            log.info(f"Created GitHub project board under org {cfg.owner!r}: {result['project_url']}")
            return result
        except Exception as e:
            log.warning(f"Org-level board creation failed ({cfg.owner!r}): {e} — trying user account")

    # Strategy 2: authenticated user's own account
    user_node_id = _get_authenticated_user_node_id(cfg)
    if user_node_id:
        try:
            result = _do_create_project_v2(user_node_id, title, cfg)
            log.info(f"Created GitHub project board under user account: {result['project_url']}")
            return result
        except Exception as e:
            log.warning(f"User-level board creation also failed: {e}")

    raise RuntimeError(
        "Could not create GitHub Project v2 board under org or user account. "
        "Ensure the OAuth token has 'project' scope (re-login required)."
    )


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


def push_file(path: str, content: str, commit_message: str,
              cfg: Optional[GHConfig] = None) -> dict:
    """Create or update a file in the repository via the Contents API.

    Returns a dict with keys:
      - html_url: GitHub file page URL
      - raw_url:  raw.githubusercontent.com download URL
      - sha:      blob SHA (needed for future updates)
    """
    import base64
    cfg = cfg or _env_cfg()
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    url = f"{REST_BASE}/repos/{cfg.owner}/{cfg.repo}/contents/{path}"

    # Check if file already exists so we can pass its SHA for an update
    sha = None
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(url, headers=_headers(cfg))
            if r.status_code == 200:
                sha = r.json().get("sha")
    except Exception:
        pass

    body: dict = {"message": commit_message, "content": encoded}
    if sha:
        body["sha"] = sha

    with httpx.Client(timeout=30) as client:
        resp = client.put(url, json=body, headers=_headers(cfg))
        resp.raise_for_status()
        data = resp.json()

    file_data = data.get("content", {})
    html_url = file_data.get("html_url", "")
    # Build raw URL from html_url: replace github.com/…/blob/… with raw.githubusercontent.com/…
    raw_url = html_url.replace(
        "https://github.com", "https://raw.githubusercontent.com"
    ).replace("/blob/", "/")

    return {
        "html_url": html_url,
        "raw_url": raw_url,
        "sha": file_data.get("sha", ""),
    }
