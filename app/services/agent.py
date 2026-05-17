"""
StoryAgent — supports OpenRouter (preferred) and Anthropic direct (fallback).
Set OPENROUTER_API_KEY to use OpenRouter. If not set, falls back to ANTHROPIC_API_KEY.
"""
import logging
from openai import OpenAI
import anthropic
from ..config import settings

log = logging.getLogger(__name__)

PHASES = [
    (1,  "User Story & Business Goal"),
    (2,  "Functional Requirements"),
    (3,  "Non-Functional Requirements"),
    (4,  "Acceptance Criteria"),
    (5,  "Technical Implementation Notes"),
    (6,  "API Requirements"),
    (7,  "UI/UX Requirements"),
    (8,  "Validation Rules & Edge Cases"),
    (9,  "Error Handling"),
    (10, "Test Scenarios & Definition of Done"),
]


def _use_openrouter() -> bool:
    return bool(settings.openrouter_api_key)


def _chat(prompt: str, max_tokens: int = 1500) -> str:
    """
    Unified chat call. Uses OpenRouter if OPENROUTER_API_KEY is set,
    otherwise falls back to Anthropic SDK.
    """
    if _use_openrouter():
        client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        resp = client.chat.completions.create(
            model=settings.openrouter_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()
    else:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()


def extract_backlog_info(chat_message: str) -> dict:
    """Extract a GitHub issue title and description from the user's first chat message."""
    prompt = f"""You are a technical business analyst. The user sent this requirement message:

"{chat_message}"

Extract a concise GitHub issue title (max 80 chars) and a clear initial description.
Respond ONLY in this format:
TITLE: <title>
DESCRIPTION: <description>"""

    text = _chat(prompt, max_tokens=512)
    title = ""
    description = ""
    for line in text.splitlines():
        if line.startswith("TITLE:"):
            title = line[6:].strip()
        elif line.startswith("DESCRIPTION:"):
            description = line[12:].strip()
    if not title:
        title = chat_message[:80]
    if not description:
        description = chat_message
    return {"title": title, "description": description}


def refine_description(existing_description: str, new_message: str) -> str:
    """Incorporate a follow-up chat message into the existing task description."""
    prompt = f"""You are a technical business analyst. The user is refining a requirement.

Current description:
{existing_description}

User's new message:
"{new_message}"

Produce an updated, merged description that incorporates the new information. Return only the updated description text."""
    return _chat(prompt, max_tokens=1024)


def generate_phase(phase_number: int, phase_name: str, task_title: str, task_description: str) -> str:
    """Generate content for a single story phase."""
    prompt = f"""You are a senior business analyst writing a structured user story.

Task Title: {task_title}
Task Description: {task_description}

Write the **{phase_name}** section of the user story document.
Be specific, technical, and production-ready. Use markdown formatting.
Return only the section content (no preamble, no section heading — just the content)."""
    return _chat(prompt, max_tokens=1500)


def update_story_from_comments(
    task_title: str,
    task_description: str,
    current_story: str,
    reviewer_comments: list[str],
) -> dict:
    """Update the full story based on reviewer comments. Returns {updated_story, change_summary}."""
    comments_text = "\n".join(f"- {c}" for c in reviewer_comments)
    prompt = f"""You are a senior business analyst. The reviewer has requested changes to this user story.

Task Title: {task_title}
Task Description: {task_description}

Current Story:
{current_story}

Reviewer Comments:
{comments_text}

1. Update the story to address all reviewer comments.
2. Write a brief change summary (2-3 sentences).

Respond in this format:
CHANGE_SUMMARY: <summary>
UPDATED_STORY:
<full updated story in markdown>"""

    text = _chat(prompt, max_tokens=4000)
    change_summary = ""
    updated_story = text

    if "CHANGE_SUMMARY:" in text and "UPDATED_STORY:" in text:
        parts = text.split("UPDATED_STORY:", 1)
        summary_part = parts[0]
        updated_story = parts[1].strip()
        for line in summary_part.splitlines():
            if line.startswith("CHANGE_SUMMARY:"):
                change_summary = line[15:].strip()

    return {"updated_story": updated_story, "change_summary": change_summary}


def build_full_story(phases: list[dict]) -> str:
    """Combine all completed phases into a single markdown document."""
    parts = []
    for p in phases:
        if p.get("completed"):
            parts.append(f"## {p['name']}\n\n{p['content']}")
    return "\n\n---\n\n".join(parts)
