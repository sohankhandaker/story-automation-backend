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


BRD_PHASES = [
    (1, "Document Control & Executive Summary"),
    (2, "Business Context, Vision & Objectives"),
    (3, "Product Scope, Capability Map & Value Chain"),
    (4, "Stakeholders, Personas & Governance"),
    (5, "Business Processes & Functional Requirements"),
    (6, "Non-Functional Requirements, Data, Integrations & Business Rules"),
    (7, "KPIs, Roadmap, Risks, Acceptance Criteria & Checklists"),
]

_BRD_SYSTEM = """You are a senior business analyst producing a professional, international-standard Business Requirements Document (BRD).
Follow the EXACT format below — numbered sections, markdown tables with pipe syntax, proper headings.
Extract real details from the notes; infer reasonable values for anything not stated.
Return ONLY the markdown — no preamble, no "Here is..." commentary."""


def _brd_phase_prompt(phase_num: int, phase_name: str, raw_notes: str,
                      wiki_content: str, prior_brd: str) -> str:
    wiki_block = (
        f"\n**Context from wiki/documentation:**\n---\n{wiki_content[:2000]}\n---\n"
        if wiki_content else ""
    )
    prior_block = (
        f"\n**Previously generated BRD content (for consistency):**\n---\n{prior_brd[-3000:]}\n---\n"
        if prior_brd else ""
    )

    instructions = {
        1: """Generate the BRD document header and sections 1–2.

Start with:
# Business Requirements Document
# [Extract product title from notes]

**Working Product Name:** [name]
**Document Type:** Business Requirements Document
**Version:** 1.0 — Draft
**Date:** [today]
**Prepared By:** [from notes or TBD]
**Intended Audience:** Business sponsors, product owners, business analysts, solution architects, UX designers, engineering teams, QA teams
**Document Status:** Draft BRD for stakeholder review

---

## 1. Document Control

### 1.1 Version History
| Version | Date | Author | Description |
|---|---:|---|---|
| 0.1 | [date] | [author] | Initial BRD draft |
| 1.0 | [date] | [author] | Complete BRD with all sections |

### 1.2 Reviewers and Approvers
| Name | Role / Organization | Responsibility | Status |
|---|---|---|---|
(List 6–8 relevant reviewer roles, use TBD for names, Status: Pending)

### 1.3 Purpose of this Document
3 paragraphs: what this BRD covers, what problem it solves, and who the intended audience is.

### 1.4 Related Downstream Documents
Bullet list: PRD, SRS, Solution Architecture, UX Specification, Data Model, API Specification, Test Strategy, Deployment Plan, Security Assessment, Training Plan.

---

## 2. Executive Summary
3–4 paragraphs: current problem, business opportunity, proposed solution, and recommended approach.""",

        2: """Generate sections 3–4.

## 3. Business Context

### 3.1 Background
2 paragraphs explaining why this project exists and the broader context.

### 3.2 Current Situation
Describe the current state, tools used, and operational pain points.

### 3.3 Business Opportunity
What the platform enables and the opportunity it captures.

### 3.4 Strategic Positioning
Blockquote (> ...) positioning statement. Explain that this is not just a tactical fix.

---

## 4. Product Vision and Business Objectives

### 4.1 Product Vision
One clear vision statement.

### 4.2 Product Mission
2–3 sentences on the platform mission.

### 4.3 Business Objectives
| Objective ID | Business Objective | Description | Success Indicator |
|---|---|---|---|
(List 7–9 objectives: BO-01, BO-02, etc.)""",

        3: """Generate sections 5–7.

## 5. Product Scope

### 5.1 In Scope — MVP
Numbered list of 15–20 MVP capabilities.

### 5.2 In Scope — Platform Foundation
Bullet list of platform-level items designed for future even if MVP.

### 5.3 Out of Scope — MVP
Bullet list of explicitly excluded features.

---

## 6. Business Capability Map

| Capability Area | Business Capability | Description | MVP Priority |
|---|---|---|---|
(10–14 rows covering all major capability areas with Must Have / Should Have / Phase 2 / Future priorities)

---

## 7. Business Value Chain

```text
[Step 1]
  -> [Step 2]
  -> [Step 3]
  -> ...
  -> [Final value delivered]
```
2–3 sentences explaining what makes this value chain succeed.""",

        4: """Generate sections 8–10.

## 8. Stakeholders

### 8.1 Primary Stakeholders
| Stakeholder | Role / Interest |
|---|---|
(6–8 primary stakeholders)

### 8.2 Secondary Stakeholders
| Stakeholder | Role / Interest |
|---|---|
(4–6 secondary stakeholders)

---

## 9. User Personas

For each of 5–7 main user types:
### 9.X [Persona Name]
One-line description paragraph.
**Needs:** bullet list of what they need from the platform.

---

## 10. Governance and Operating Model

### 10.1 Governance Layers
| Layer | Responsibility |
|---|---|
(4–5 layers)

### 10.2 Suggested RACI Matrix
| Activity | [Role 1] | [Role 2] | [Role 3] | [Role 4] | [Role 5] |
|---|---|---|---|---|---|
(8–10 key activities, R/A/C/I values)

**Legend:** R = Responsible, A = Accountable, C = Consulted, I = Informed""",

        5: """Generate sections 11–13.

## 11. Business Assumptions and Constraints

### 11.1 Business Assumptions
| ID | Assumption |
|---|---|
(A-01 to A-10 or more)

### 11.2 Constraints
| ID | Constraint |
|---|---|
(C-01 to C-08 or more)

---

## 12. Business Process Overview

### 12.1 Primary User Lifecycle
Numbered list of 15–20 steps showing the complete end-to-end journey.

### 12.2 Admin / Organizer Lifecycle
Numbered list of 10–14 steps.

---

## 13. Functional Requirements

For each of 8–10 functional areas create a subsection:
### 13.X [Area Name]
| Req ID | Requirement | Priority |
|---|---|---|
(FR-001 onwards, 4–6 requirements per area)
Priority: Must Have / Should Have / Could Have / Phase 2 / Future""",

        6: """Generate sections 14–18.

## 14. Non-Functional Requirements
| NFR ID | Category | Requirement | Priority |
|---|---|---|---|
(NFR-001 onwards, 15–20 rows covering Usability, Performance, Scalability, Availability, Security, Privacy, Accessibility, Maintainability, Observability)

---

## 15. Data Requirements

### 15.1 Core Data Entities
| Entity | Description |
|---|---|
(12–18 entities)

### 15.2 Sensitive Data Categories
Bullet list of PII and sensitive fields.

### 15.3 Data Quality Rules
| Rule ID | Rule |
|---|---|
(DQ-001 to DQ-007+)

---

## 16. Integration Requirements
| Integration ID | Integration | Purpose | MVP Priority | Notes |
|---|---|---|---|---|
(INT-001 onwards, 6–8 integrations)

---

## 17. Role-Based Access Control
| Role | Key Permissions |
|---|---|
(8–12 roles)

---

## 18. Business Rules
| Rule ID | Business Rule |
|---|---|
(BR-001 to BR-016+)""",

        7: """Generate sections 19–21 and 27–37 (condensed but complete).

## 19. Reporting Requirements

### 19.1 Operational Reports
Bullet list of 12–15 operational reports.

### 19.2 Strategic Reports
Bullet list of 6–8 strategic reports.

---

## 20. Business KPIs and Measurement Framework

### 20.1 MVP KPIs
| KPI | Definition | Target Direction |
|---|---|---|
(8–10 KPIs)

---

## 21. MVP Scope and Phased Roadmap

### 21.1 MVP Objective
1–2 sentences.

### 21.2 MVP Feature Set
| Module | MVP Features |
|---|---|
(8–12 modules)

### 21.3 Phase 1 — MVP
Goal and outcome.

### 21.4 Phase 2 — Operational Expansion
Goal and possible additions.

### 21.5 Phase 3 — Multi-Program / National
Goal and possible additions.

### 21.6 Phase 4 — Ecosystem
Goal and possible additions.

---

## 27. Acceptance Criteria
| ID | Acceptance Criteria |
|---|---|
(AC-001 to AC-015+)

---

## 29. Risks and Mitigation
| Risk | Impact | Probability | Mitigation |
|---|---:|---:|---|
(8–10 risks)

---

## 30. Dependencies
| Dependency | Description | Owner |
|---|---|---|
(6–8 dependencies)

---

## 31. Open Questions
| ID | Open Question | Required For |
|---|---|---|
(OQ-001 to OQ-010+)

---

## 33. MVP Readiness Checklist
| Area | Readiness Item | Status |
|---|---|---|
(12–16 items, all Pending)

---

## 37. Conclusion
3 paragraphs: platform purpose, MVP goal, long-term vision.""",
    }

    return f"""{_BRD_SYSTEM}

**Raw Meeting Notes:**
---
{raw_notes[:4000]}
---
{wiki_block}{prior_block}
**Phase {phase_num}/7 — {phase_name}**

{instructions[phase_num]}"""


def enhance_notes_to_brd(
    raw_notes: str,
    wiki_content: str = "",
    phase_callback=None,
) -> dict:
    """
    Multi-phase professional BRD generation matching international enterprise standards.
    phase_callback(phase_num, total) is called after each phase completes.
    Returns {title, brd_markdown}.
    """
    total = len(BRD_PHASES)
    accumulated = ""
    sections = []

    for phase_num, phase_name in BRD_PHASES:
        log.info(f"BRD phase {phase_num}/{total}: {phase_name}")
        try:
            prompt = _brd_phase_prompt(phase_num, phase_name, raw_notes, wiki_content, accumulated)
            content = _chat(prompt, max_tokens=3500)
            sections.append(content)
            accumulated += "\n\n" + content
        except Exception as e:
            log.error(f"BRD phase {phase_num} failed: {e}")
            sections.append(f"\n\n> ⚠️ Section generation failed for Phase {phase_num}: {e}\n\n")

        if phase_callback:
            try:
                phase_callback(phase_num, total)
            except Exception:
                pass

    brd = "\n\n".join(sections)

    # Extract title from the first H1 that isn't "Business Requirements Document"
    title = "Business Requirements Document"
    for line in brd.splitlines():
        if line.startswith("# ") and "Business Requirements Document" not in line:
            title = line.lstrip("# ").strip()
            break

    return {"title": title, "brd_markdown": brd}


def update_brd_from_feedback(
    current_brd: str,
    reviewer_comments: list[str],
) -> dict:
    """Update BRD sections based on reviewer feedback.
    Returns {updated_markdown, change_summary, changed_sections}."""
    comments_text = "\n".join(f"- {c}" for c in reviewer_comments)
    prompt = f"""You are a senior business analyst updating a Business Requirements Document (BRD) based on reviewer feedback.

Reviewer Feedback:
{comments_text}

Current BRD:
{current_brd[:12000]}

Instructions:
1. Update ONLY the sections that need to change based on the feedback.
2. Keep all other sections word-for-word identical.
3. List the exact section headings you changed (e.g. "6. Non-Functional Requirements").
4. Write a 2-3 sentence summary of what changed and why.

Respond EXACTLY in this format (no text before CHANGE_SUMMARY):
CHANGE_SUMMARY: <2-3 sentence summary>
CHANGED_SECTIONS: <comma-separated exact section headings that were changed>
UPDATED_BRD:
<full updated BRD in markdown>"""

    text = _chat(prompt, max_tokens=7000)

    change_summary = ""
    changed_sections = []
    updated_markdown = current_brd  # fallback if parse fails

    if "UPDATED_BRD:" in text:
        parts = text.split("UPDATED_BRD:", 1)
        header = parts[0]
        updated_markdown = parts[1].strip()
        for line in header.splitlines():
            s = line.strip()
            if s.startswith("CHANGE_SUMMARY:"):
                change_summary = s[15:].strip()
            elif s.startswith("CHANGED_SECTIONS:"):
                raw = s[17:].strip()
                changed_sections = [x.strip() for x in raw.split(",") if x.strip()]

    return {
        "updated_markdown": updated_markdown,
        "change_summary": change_summary,
        "changed_sections": changed_sections,
    }


def build_full_story(phases: list[dict]) -> str:
    """Combine all completed phases into a single markdown document."""
    parts = []
    for p in phases:
        if p.get("completed"):
            parts.append(f"## {p['name']}\n\n{p['content']}")
    return "\n\n---\n\n".join(parts)
