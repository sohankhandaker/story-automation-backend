"""
GitHub service — REST API for issues/comments, GraphQL for Projects v2.
"""
import logging
from datetime import datetime
from typing import Optional
import httpx
from ..config import settings

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"

# Cached on startup
_project_id: Optional[str] = None
_status_field_id: Optional[str] = None
_status_options: dict[str, str] = {}  # {name_lower: option_id}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gql(query: str, variables: dict = None) -> dict:
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {settings.github_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        return data["data"]


def init_project():
    """Fetch and cache project ID and status field/options. Called once on startup."""
    global _project_id, _status_field_id, _status_options

    if not settings.github_token or not settings.github_owner:
        log.warning("GitHub credentials not configured — skipping project init")
        return

    query = """
    query($login: String!, $number: Int!) {
      user(login: $login) {
        projectV2(number: $number) {
          id
          fields(first: 30) {
            nodes {
              ... on ProjectV2SingleSelectField {
                id
                name
                options { id name }
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
            result = _gql(q, {"login": settings.github_owner, "number": settings.github_project_number})
            owner_key = "user" if "user" in result else "organization"
            project = result.get(owner_key, {}).get("projectV2")
            if project:
                data = project
                break
        except Exception:
            continue

    if not data:
        log.warning("Could not fetch GitHub project. Check GITHUB_OWNER and GITHUB_PROJECT_NUMBER.")
        return

    _project_id = data["id"]
    for field in data["fields"]["nodes"]:
        if field.get("name", "").lower() == "status":
            _status_field_id = field["id"]
            _status_options = {opt["name"].lower(): opt["id"] for opt in field["options"]}
            break

    log.info(f"GitHub project ready. ID={_project_id}, statuses={list(_status_options.keys())}")


# ── Issues ────────────────────────────────────────────────────────────────────

def create_issue(title: str, body: str) -> dict:
    """Create a GitHub issue. Returns {url, number, node_id}."""
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{REST_BASE}/repos/{settings.github_owner}/{settings.github_repo}/issues",
            json={"title": title, "body": body},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return {"url": data["html_url"], "number": data["number"], "node_id": data["node_id"]}


def update_issue_body(issue_number: int, body: str) -> None:
    with httpx.Client(timeout=30) as client:
        resp = client.patch(
            f"{REST_BASE}/repos/{settings.github_owner}/{settings.github_repo}/issues/{issue_number}",
            json={"body": body},
            headers=_headers(),
        )
        resp.raise_for_status()


def add_comment(issue_number: int, body: str) -> int:
    """Post a comment. Returns comment ID."""
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{REST_BASE}/repos/{settings.github_owner}/{settings.github_repo}/issues/{issue_number}/comments",
            json={"body": body},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["id"]


def get_comments_since(issue_number: int, since: Optional[datetime] = None) -> list[dict]:
    """Return comments on an issue, optionally filtered by `since` datetime."""
    params = {"per_page": 100}
    if since:
        params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            f"{REST_BASE}/repos/{settings.github_owner}/{settings.github_repo}/issues/{issue_number}/comments",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


# ── Projects v2 ───────────────────────────────────────────────────────────────

def add_to_project(issue_node_id: str) -> str:
    """Add an issue to the GitHub Projects v2 board. Returns project item ID."""
    if not _project_id:
        raise RuntimeError("GitHub project not initialized")
    mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { id }
      }
    }
    """
    data = _gql(mutation, {"projectId": _project_id, "contentId": issue_node_id})
    return data["addProjectV2ItemById"]["item"]["id"]


def update_project_status(item_id: str, status_name: str) -> None:
    """Move a project item to the given status column."""
    if not _project_id or not _status_field_id:
        log.warning("Project not initialized — skipping status update")
        return
    option_id = _status_options.get(status_name.lower())
    if not option_id:
        log.warning(f"Status '{status_name}' not found in project options: {list(_status_options.keys())}")
        return
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { singleSelectOptionId: $optionId }
      }) {
        projectV2Item { id }
      }
    }
    """
    _gql(mutation, {
        "projectId": _project_id,
        "itemId": item_id,
        "fieldId": _status_field_id,
        "optionId": option_id,
    })


def get_project_item_status(item_id: str) -> Optional[str]:
    """Return the current status name of a project item."""
    query = """
    query($id: ID!) {
      node(id: $id) {
        ... on ProjectV2Item {
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
      }
    }
    """
    try:
        data = _gql(query, {"id": item_id})
        for node in data["node"]["fieldValues"]["nodes"]:
            if node.get("field", {}).get("name", "").lower() == "status":
                return node.get("name")
    except Exception as e:
        log.error(f"Error fetching project item status: {e}")
    return None
