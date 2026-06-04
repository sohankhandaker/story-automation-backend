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


def _chat(prompt: str, max_tokens: int = 1500, retries: int = 3) -> str:
    """
    Unified chat call. Uses OpenRouter if OPENROUTER_API_KEY is set,
    otherwise falls back to Anthropic SDK. Retries on empty/None responses.
    Timeout: 180 s per attempt to prevent silent hangs on slow model responses.
    """
    import time

    # Per-attempt timeout in seconds — long enough for large BRD/PRD phases
    # but bounded so a hung request doesn't freeze the pipeline indefinitely.
    _TIMEOUT = 180.0

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            if _use_openrouter():
                client = OpenAI(
                    api_key=settings.openrouter_api_key,
                    base_url="https://openrouter.ai/api/v1",
                    timeout=_TIMEOUT,
                    max_retries=0,  # we handle retries ourselves
                )
                resp = client.chat.completions.create(
                    model=settings.openrouter_model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                content = resp.choices[0].message.content
                if not content:
                    finish = resp.choices[0].finish_reason
                    raise ValueError(f"Empty response from model (finish_reason={finish})")
                return content.strip()
            else:
                client = anthropic.Anthropic(
                    api_key=settings.anthropic_api_key,
                    timeout=_TIMEOUT,
                    max_retries=0,
                )
                msg = client.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return msg.content[0].text.strip()
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 5 * attempt
                log.warning(f"_chat attempt {attempt} failed ({e}), retrying in {wait}s…")
                time.sleep(wait)

    raise last_err


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
    (7, "KPIs, Roadmap & MVP Scope"),
    (8, "Risks, Acceptance Criteria, Dependencies & Checklists"),
]

_BRD_SYSTEM = """You are a senior business analyst producing a professional, international-standard Business Requirements Document (BRD).
Follow the EXACT format below — numbered sections, markdown tables with pipe syntax, proper headings.
Extract real details from the notes; infer reasonable values for anything not stated.
Return ONLY the markdown — no preamble, no "Here is..." commentary."""


def _brd_phase_prompt(phase_num: int, phase_name: str, raw_notes: str,
                      wiki_content: str, prior_brd: str, analysis: str = "") -> str:
    analysis_block = (
        f"\n**Notes Analysis (primary context — use this as your main input):**\n---\n{analysis[:3000]}\n---\n"
        if analysis else ""
    )
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

        7: """Generate sections 19–21.

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
Goal and possible additions.""",

        8: """Generate sections 27–37 (condensed but complete).

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
{analysis_block}{wiki_block}{prior_block}
**Phase {phase_num}/8 — {phase_name}**

{instructions[phase_num]}"""


def analyze_notes_to_draft(combined_notes: str, crawled_content: str = "") -> str:
    """Analyze meeting notes and produce a structured context document for BRD generation."""
    context_block = (
        f"\n\n**Additional context from linked resources:**\n---\n{crawled_content[:4000]}\n---"
        if crawled_content else ""
    )
    prompt = f"""You are a senior business analyst. Analyze these meeting notes and produce a comprehensive structured analysis document that will serve as the primary input for generating a full Business Requirements Document (BRD).

**Meeting Notes:**
---
{combined_notes[:6000]}
---
{context_block}

Produce a structured analysis with these sections:

# [Extract the project/product title from the notes]

## 1. Problem Statement
What problem is being solved? Why does this project exist?

## 2. Key Requirements
Numbered list of ALL requirements identified (explicit and implied).

## 3. Stakeholders
Who is mentioned or implied as a stakeholder, user, or decision-maker?

## 4. Key Decisions & Constraints
Decisions already made, constraints, compliance requirements.

## 5. Technical & Integration Context
Tech stack, platforms, integrations, data sources mentioned.

## 6. Business Goals & Success Metrics
What does success look like? KPIs or goals mentioned.

## 7. Open Questions & Gaps
Things not yet decided, missing information, areas needing clarification.

## 8. Context Summary
A comprehensive 3-4 paragraph synthesis of all the above for use in BRD generation.

Be thorough. Extract every requirement and constraint. Infer reasonable values for things implied but not stated. Use proper markdown formatting."""

    return _chat(prompt, max_tokens=3000)


def generate_cr_summary(raw_notes: str, prd_content: str) -> str:
    """Generate a structured Change Request summary against the approved PRD."""
    prompt = f"""You are a senior business analyst. A project team has an approved PRD and a new Change Request.

## Approved PRD (context)
{prd_content[:5000]}

## Change Request Notes
{raw_notes}

Write a professional Change Request Summary document in Markdown that includes:
1. **Executive Summary** — one paragraph describing the change
2. **Business Justification** — why this change is needed
3. **Scope of Change** — which PRD sections are affected and how
4. **Proposed Changes** — clear, numbered list of what changes
5. **Impact Assessment** — risks, dependencies, estimated effort level (Low/Medium/High)
6. **Acceptance Criteria** — how we know the change is complete

Return ONLY the markdown document. No preamble."""
    return _chat(prompt, max_tokens=3000)


def generate_cr_planner_doc(cr_title: str, cr_summary: str, prd_content: str) -> str:
    """Generate a planner handoff document for a finalised Change Request."""
    prompt = f"""You are a technical project planner. A Change Request has been approved.

## Approved PRD (reference)
{prd_content[:4000]}

## Change Request Summary
{cr_summary[:3000]}

## Change Request Title
{cr_title}

Generate a **Planner Handoff Document** in Markdown suitable for a development team. Include:
1. **Change Overview** — what is being changed and why
2. **Technical Tasks** — numbered breakdown of implementation tasks with clear acceptance criteria
3. **Files / Modules Likely Affected** — based on the PRD context
4. **Definition of Done** — checklist a developer can tick off
5. **Testing Checklist** — what must be tested before this can be released

Return ONLY the markdown document."""
    return _chat(prompt, max_tokens=3000)


def enhance_against_prd(raw_text: str, prd_content: str) -> str:
    """
    Enhance a change-request draft against the project's approved PRD.
    Returns the improved, structured change-request text.
    """
    prompt = f"""You are a senior business analyst helping a team write a Change Request (CR).

The project has an approved Product Requirements Document (PRD):
---
{prd_content[:6000]}
---

The user has drafted the following change request notes:
---
{raw_text}
---

Rewrite the change request so that it:
1. Is clearly structured (what changes, why, impact on existing PRD sections).
2. Explicitly references the relevant PRD sections it modifies or extends.
3. Is concise and professional — no filler words.
4. Preserves the user's original intent exactly.

Return ONLY the enhanced change request text. Do not include any explanation or preamble."""
    return _chat(prompt, max_tokens=2000)


def enhance_notes_to_brd(
    raw_notes: str,
    wiki_content: str = "",
    analysis: str = "",
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
        # Update phase BEFORE the API call so the app shows the correct step
        # immediately — even during retry waits the UI reflects the right phase.
        if phase_callback:
            try:
                phase_callback(phase_num, total)
            except Exception:
                pass
        try:
            prompt = _brd_phase_prompt(phase_num, phase_name, raw_notes, wiki_content, accumulated, analysis)
            content = _chat(prompt, max_tokens=5000)
            sections.append(content)
            accumulated += "\n\n" + content
        except Exception as e:
            log.error(f"BRD phase {phase_num} failed: {e}")
            sections.append(f"\n\n> ⚠️ Section generation failed for Phase {phase_num}: {e}\n\n")

    brd = "\n\n".join(sections)

    # Extract title from the first H1 that isn't "Business Requirements Document"
    title = "Business Requirements Document"
    for line in brd.splitlines():
        if line.startswith("# ") and "Business Requirements Document" not in line:
            title = line.lstrip("# ").strip()
            break

    return {"title": title, "brd_markdown": brd}


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into [(heading_line, body), ...].
    heading_line is '' for content before the first heading."""
    import re
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in markdown.splitlines():
        if re.match(r"^#{1,4}\s", line):
            sections.append((current_heading, "\n".join(current_lines)))
            current_heading = line.rstrip()
            current_lines = []
        else:
            current_lines.append(line)
    sections.append((current_heading, "\n".join(current_lines)))
    return sections


def _merge_section_updates(original: str, updates: str) -> str:
    """Splice updated sections into the original document.
    Only sections whose headings appear in *updates* are replaced; all
    others are kept verbatim from *original*."""
    import re

    def _norm(h: str) -> str:
        return re.sub(r"^#+\s*", "", h).strip().lower()

    update_map = {
        _norm(h): (h, body)
        for h, body in _split_sections(updates)
        if h
    }

    parts: list[str] = []
    for heading, body in _split_sections(original):
        key = _norm(heading)
        if heading and key in update_map:
            new_h, new_body = update_map[key]
            parts.append(new_h)
            if new_body.strip():
                parts.append(new_body)
        else:
            if heading:
                parts.append(heading)
            if body.strip() or not heading:
                parts.append(body)

    return "\n".join(parts)


def update_brd_from_feedback(
    current_brd: str,
    reviewer_comments: list[str],
) -> dict:
    """Update BRD sections based on reviewer feedback.
    Returns {updated_markdown, change_summary, changed_sections}.

    Uses a surgical approach: the model outputs ONLY the changed sections,
    then Python merges them back into the full document — avoiding token-limit
    truncation that would silently drop unchanged sections."""
    comments_text = "\n".join(f"- {c}" for c in reviewer_comments)
    prompt = f"""You are a senior business analyst updating a Business Requirements Document (BRD).

Reviewer Feedback:
{comments_text}

Current BRD:
{current_brd}

IMPORTANT: Output ONLY the sections that need to change — do NOT repeat unchanged sections.
Each changed section must start with its exact heading (##, ###, etc.) as it appears above.

Respond EXACTLY in this format:
CHANGE_SUMMARY: <2-3 sentence summary of what changed and why>
CHANGED_SECTIONS: <comma-separated exact section headings that were changed>
SECTION_UPDATES:
<heading and full new content for each changed section only>"""

    text = _chat(prompt, max_tokens=6000)

    change_summary = ""
    changed_sections = []
    section_updates = ""

    if "SECTION_UPDATES:" in text:
        parts = text.split("SECTION_UPDATES:", 1)
        header = parts[0]
        section_updates = parts[1].strip()
        for line in header.splitlines():
            s = line.strip()
            if s.startswith("CHANGE_SUMMARY:"):
                change_summary = s[15:].strip()
            elif s.startswith("CHANGED_SECTIONS:"):
                raw = s[17:].strip()
                changed_sections = [x.strip() for x in raw.split(",") if x.strip()]

    updated_markdown = _merge_section_updates(current_brd, section_updates) if section_updates else current_brd

    return {
        "updated_markdown": updated_markdown,
        "change_summary": change_summary,
        "changed_sections": changed_sections,
    }


PRD_PHASES = [
    (1,  "Document Header, Purpose & Product Summary"),
    (2,  "Product Goals, Non-Goals, Target Users & Platforms"),
    (3,  "MVP Scope & Core Concepts"),
    (4,  "Architecture & User Flows"),
    (5,  "State Machines & Functional Requirements"),
    (6,  "Screen Requirements & Data Model"),
    (7,  "API Contract & Permissions Matrix"),
    (8,  "Validation Rules, Notifications & Reporting"),
    (9,  "NFRs, Error Handling & Edge Cases"),
    (10, "AI Coding Guidance & User Stories"),
]

_PRD_SYSTEM = """You are a senior product manager producing a professional, engineering-ready Product Requirements Document (PRD).
Extract real details from the BRD; infer reasonable values for gaps; keep all requirements specific and testable.
Return ONLY the markdown section content — no preamble, no "Here is..." commentary."""


def _prd_phase_prompt(phase_num: int, phase_name: str, brd_content: str, prior_prd: str) -> str:
    prior_block = (
        f"\n**Previously generated PRD content (for consistency):**\n---\n{prior_prd[-3000:]}\n---\n"
        if prior_prd else ""
    )

    from datetime import date
    today = date.today().isoformat()

    instructions = {
        1: f"""Generate the PRD document header and opening sections.

Start with EXACTLY this header block (fill in values from BRD):
# Product Requirements Document (PRD)
**Product:** [Extract product name from BRD]
**Version:** 1.0 — Draft
**Generated from BRD:** [Extract BRD title]
**Date:** {today}
**Prepared By:** Product Team
**Status:** Draft — awaiting engineering & design review

---

## 1. Purpose

Write 2–3 paragraphs explaining:
- Why this PRD exists and what decisions it enables
- What problem the product solves and for whom
- How this document should be used by engineers, designers, and QA

---

## 2. Product Summary

Write a concise product summary (3–5 sentences) covering:
- What the product is
- The core value proposition
- The primary users and key workflows
- The target platform(s)
- The go-live goal""",

        2: """## 3. Product Goals

| Goal ID | Goal | Description | Priority |
|---|---|---|---|
(List 6–9 goals: PG-01 to PG-09. Cover adoption, engagement, operational efficiency, compliance, and business outcomes. Priority: Must Have / Should Have / Could Have)

---

## 4. Non-Goals

Explicit list of what this product will NOT do in the current scope (8–12 items). Be precise — state exactly what is excluded and why (e.g. "No native desktop app — web and mobile only for MVP").

---

## 5. Target Users

For each of 4–6 user types, write a persona block:

### [Role Name] (e.g. Volunteer, Admin, Organizer)
- **Description:** 1–2 sentences on who this user is
- **Goals:** 3–4 bullet points of what they want to achieve on the platform
- **Pain Points:** 2–3 current frustrations the product resolves
- **Tech Comfort:** Low / Medium / High
- **Key Scenarios:** 2–3 common scenarios they experience

---

## 6. Platforms

| Platform | Supported | Notes |
|---|---|---|
(List all platforms: iOS App, Android App, Web App (responsive), Admin Web Portal, etc. Mark MVP support clearly)""",

        3: """## 7. MVP Scope

| Module | Feature | Priority | Included in MVP |
|---|---|---|---|
(List 20–30 features across all modules. Priority: Must Have / Should Have / Could Have. MVP: Yes / No / Deferred)

---

## 8. Core Concepts

Define 8–14 domain-specific terms and concepts that engineers and designers MUST understand before working on this product. For each:

### [Concept Name]
1–3 sentence definition. Include any business rules directly tied to the concept.

Example:
### Opportunity
A structured volunteer engagement listing created by an Organizer. An Opportunity has a defined capacity, schedule, skills required, and lifecycle from Draft → Published → Active → Closed.""",

        4: f"""## 9. Architecture Overview

Write a high-level architecture overview (3–4 paragraphs) covering:
- Frontend layers (mobile app, web portal, admin dashboard)
- Backend layers (API server, background jobs, external integrations)
- Data layer (primary database, file storage, cache)
- Key external service dependencies

Then provide a text-based architecture diagram:
```
┌─────────────────────────────────────────────┐
│              Client Layer                    │
│  [Mobile App (iOS/Android)]  [Web Portal]   │
└───────────────────┬─────────────────────────┘
                    │ HTTPS / REST
┌───────────────────▼─────────────────────────┐
│               API Gateway / Backend         │
│  [Auth Service]  [Core API]  [Jobs Worker]  │
└───────────────────┬─────────────────────────┘
                    │
┌───────────────────▼─────────────────────────┐
│               Data Layer                    │
│  [PostgreSQL]  [File Storage]  [Cache]      │
└─────────────────────────────────────────────┘
```
(Adapt the diagram to match the actual product architecture from the BRD)

---

## 10. User Flows

For each of 6–10 critical user flows, write a detailed flow:

### Flow [N]: [Flow Name]
- **Actor:** [who performs this flow]
- **Precondition:** [what must be true before the flow starts]
- **Steps:**
  1. Step one (include UI action + system response)
  2. Step two
  3. …continue to completion
- **Postcondition:** [what is true after success]
- **Failure Cases:**
  - [Failure scenario 1] → [system response / recovery]
  - [Failure scenario 2] → [system response / recovery]""",

        5: """## 11. State Machines

For each of 4–7 entities that have meaningful lifecycle states, write a state machine:

### [Entity] State Machine
**States:** List all valid states

**Transitions:**
| From State | Event / Trigger | To State | Guard Condition | Side Effects |
|---|---|---|---|---|
(All valid transitions with trigger event, guard condition, and side effects like notifications or data changes)

**State Diagram (text):**
```
[Draft] ──(publish)──▶ [Published] ──(fill)──▶ [Active] ──(complete)──▶ [Closed]
                              │                                              │
                          (archive)                                    (archive)
                              ▼                                              ▼
                         [Archived]                                   [Archived]
```
(Adapt to actual entity lifecycle from the BRD)

---

## 12. Functional Requirements

For each of 8–12 functional modules, write a requirements table:

### 12.X [Module Name]

| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
(FR-[MODULE]-001 onwards. Priority: Must Have / Should Have / Could Have. AC should be testable — "Given X, when Y, then Z" format. 4–8 requirements per module.)""",

        6: """## 13. Screen Requirements

List all screens/views by actor group. For each screen:

| ID | Screen Name | Actor | Description | Key Elements | Entry Points |
|---|---|---|---|---|---|

Use these ID prefixes:
- VS-xxx — Volunteer-facing screens
- AS-xxx — Admin-facing screens
- OS-xxx — Organizer-facing screens
- SS-xxx — Shared / common screens (login, profile, notifications, etc.)

(List 20–35 screens total across all actor groups)

For each screen that has complex behaviour, add a detail block:
#### [ID]: [Screen Name]
**Purpose:** one sentence
**Primary Actions:** bullet list of what the user can do
**Data Displayed:** bullet list of key data shown
**Validation / Business Rules:** any rules enforced on this screen
**Empty / Error States:** what the screen shows when no data or an error occurs

---

## 14. Data Model

For each of 8–14 core entities, define the data structure:

### [Entity Name]
```json
{{
  "id": "uuid — primary key",
  "field_name": "type — description (constraints, e.g. required, unique, max length)",
  "...": "..."
}}
```
**Relationships:**
- Belongs to: [other entities]
- Has many: [other entities]
- Many-to-many with: [other entities] (through [junction])

**Indexes:** list fields that need database indexes for performance
**Notes:** any important business rules tied to this entity's data""",

        7: """## 15. API Contract

Group all endpoints by domain. For each endpoint:

| Method | Path | Description | Auth Required | Request | Response | Status Codes |
|---|---|---|---|---|---|---|

Use these domain groups (add/remove as needed based on the BRD):
- Auth & Session
- User & Profile
- [Core Domain 1] (e.g. Opportunities / Listings)
- [Core Domain 2] (e.g. Applications / Registrations)
- [Core Domain 3] (e.g. Check-in / Attendance)
- Admin & Reporting
- Notifications
- File Uploads

For each domain write a section header and table. Target 4–8 endpoints per domain, 40–60 endpoints total.

---

## 16. Permissions Matrix

| Action / Feature | [Role 1] | [Role 2] | [Role 3] | [Role 4] | [Role 5] | Notes |
|---|---|---|---|---|---|---|
(30–50 rows covering all significant actions. Use: ✓ = allowed, ✗ = denied, Own = own records only, Org = own organisation only)

Roles should match the Target Users section (e.g. Guest, Volunteer, Organizer, Admin, Super Admin).""",

        8: """## 17. Validation Rules

| Field | Rule ID | Rule | Error Message |
|---|---|---|---|
(VR-001 onwards. Cover all user-input fields across all forms. 25–40 rules total. Be specific: max lengths, regex patterns, cross-field rules, async uniqueness checks.)

---

## 18. Notification Requirements

| Trigger Event | Channels | Recipient(s) | Template Summary | Timing | Priority |
|---|---|---|---|---|---|
(20–30 notification rules. Channels: Push, Email, In-App, SMS. Priority: Critical / High / Normal / Low.)

---

## 19. Reporting Requirements

### 19.1 Operational Reports
| Report | Description | Audience | Frequency | Filters |
|---|---|---|---|---|
(10–15 operational reports)

### 19.2 Analytics & Dashboards
| Dashboard / Widget | Metrics Shown | Audience | Update Frequency |
|---|---|---|---|
(6–10 dashboard components)

### 19.3 Export Requirements
| Export Type | Format | Fields | Audience |
|---|---|---|---|
(4–8 data export types: CSV, PDF, Excel, etc.)""",

        9: """## 20. Non-Functional Requirements

| NFR ID | Category | Requirement | Metric / Target | Priority |
|---|---|---|---|---|
(NFR-001 onwards. 20–30 rows covering: Performance, Scalability, Availability, Security, Privacy/GDPR, Accessibility/WCAG, Internationalisation, Maintainability, Observability/Logging, Disaster Recovery)

---

## 21. Error Handling

### 21.1 Error Response Format
Define the standard API error response structure:
```json
{{
  "error": {{
    "code": "ERROR_CODE",
    "message": "Human-readable message",
    "field": "affected_field_if_applicable",
    "details": {{}}
  }}
}}
```

### 21.2 Error Code Reference
| Error Code | HTTP Status | Scenario | User-Facing Message |
|---|---|---|---|
(25–40 error codes covering auth, validation, not found, conflict, rate limit, server error cases)

### 21.3 Client-Side Error Handling
Describe how the frontend should handle each category of error (network timeout, 4xx, 5xx, validation, optimistic update rollback).

---

## 22. Edge Cases

List 15–25 edge cases that engineers and QA must handle. Group by module:

### [Module Name]
- **[EC-XXX]:** [scenario] → [expected behaviour]
- **[EC-XXX]:** [scenario] → [expected behaviour]

Cover: concurrent operations, boundary conditions, empty states, permission edge cases, race conditions, data integrity edge cases.""",

        10: """## 23. AI Coding Guidance

This section gives AI coding assistants (e.g. GitHub Copilot, Claude Code, Cursor) explicit context to generate high-quality, consistent code.

### 23.1 Tech Stack
List the exact technologies, frameworks, versions, and libraries to be used (frontend, backend, database, infra, testing).

### 23.2 Code Style & Conventions
- Naming conventions (files, components, functions, variables, database columns)
- File and folder structure
- Preferred patterns (e.g. repository pattern, service layer, feature slices)
- Error handling approach
- Logging conventions

### 23.3 Critical Business Logic Notes
List 8–12 non-obvious business rules that an AI must know to generate correct code (e.g. "Capacity is calculated from confirmed applications only, not pending ones").

### 23.4 Anti-Patterns to Avoid
Bullet list of 6–10 patterns explicitly banned in this codebase (e.g. "Never expose internal IDs in public API responses", "Never store PII in logs").

### 23.5 Testing Requirements
- Required test types per layer (unit, integration, e2e)
- Minimum coverage targets
- Test naming conventions
- Key scenarios that must have test coverage

---

## 24. User Stories

Group stories by Epic. For each Epic:

### Epic EP-[N]: [Epic Name]
**Goal:** one sentence describing what this epic enables.

| Story ID | As a... | I want to... | So that... | Priority | Acceptance Criteria |
|---|---|---|---|---|---|
(US-[EP]-001 onwards. 4–8 stories per epic. Priority: Must Have / Should Have / Could Have. AC: specific, testable, "Given/When/Then" where helpful.)

(Create 8–12 epics covering all major product areas from the BRD. Total: 50–80 user stories.)""",
    }

    total = len(PRD_PHASES)
    return f"""{_PRD_SYSTEM}

**Approved BRD Content (primary input — extract all details from this):**
---
{brd_content[:6000]}
---
{prior_block}
**Phase {phase_num}/{total} — {phase_name}**

{instructions[phase_num]}"""


def generate_prd_from_brd(brd_content: str, phase_callback=None) -> dict:
    """Generate PRD phase by phase from an approved BRD.
    phase_callback(phase_num, total) is called after each phase completes.
    Returns {phases: [...], full_prd: str}."""
    total = len(PRD_PHASES)
    accumulated = ""
    sections = []

    for phase_num, phase_name in PRD_PHASES:
        log.info(f"PRD phase {phase_num}/{total}: {phase_name}")
        # Update phase BEFORE the API call — same fix as BRD.
        if phase_callback:
            try:
                phase_callback(phase_num, total)
            except Exception:
                pass
        try:
            prompt = _prd_phase_prompt(phase_num, phase_name, brd_content, accumulated)
            content = _chat(prompt, max_tokens=4000)
            sections.append(content)
            accumulated += "\n\n" + content
        except Exception as e:
            log.error(f"PRD phase {phase_num} failed: {e}")
            sections.append(f"\n\n> Section generation failed for Phase {phase_num}: {e}\n\n")

    full_prd = "\n\n".join(sections)
    return {"phases": sections, "full_prd": full_prd}


def update_prd_from_feedback(prd_content: str, feedback: str) -> dict:
    """Update PRD based on reviewer feedback.
    Returns {updated_prd, change_summary, changed_sections}.

    Uses the same surgical section-merge approach as update_brd_from_feedback."""
    prompt = f"""You are a senior product manager updating a Product Requirements Document (PRD).

Feedback:
{feedback}

Current PRD:
{prd_content}

IMPORTANT: Output ONLY the sections that need to change — do NOT repeat unchanged sections.
Each changed section must start with its exact heading (##, ###, etc.) as it appears above.

Respond EXACTLY in this format:
CHANGE_SUMMARY: <2-3 sentence summary of what changed and why>
CHANGED_SECTIONS: <comma-separated exact section headings that were changed>
SECTION_UPDATES:
<heading and full new content for each changed section only>"""

    text = _chat(prompt, max_tokens=6000)

    change_summary = ""
    changed_sections = []
    section_updates = ""

    if "SECTION_UPDATES:" in text:
        parts = text.split("SECTION_UPDATES:", 1)
        header = parts[0]
        section_updates = parts[1].strip()
        for line in header.splitlines():
            s = line.strip()
            if s.startswith("CHANGE_SUMMARY:"):
                change_summary = s[15:].strip()
            elif s.startswith("CHANGED_SECTIONS:"):
                raw = s[17:].strip()
                changed_sections = [x.strip() for x in raw.split(",") if x.strip()]

    updated_prd = _merge_section_updates(prd_content, section_updates) if section_updates else prd_content

    return {
        "updated_prd": updated_prd,
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


# ── BRD Q&A and smart comment classification ──────────────────────────────────

def classify_brd_comment(comment: str) -> str:
    """Classify a reviewer comment as 'question' or 'change_request'.

    Uses a fast single-token call — the model replies with exactly one word.
    Falls back to 'change_request' when the classification is ambiguous,
    because treating a change request as a question is less disruptive than
    silently ignoring a change request.
    """
    prompt = f"""You are classifying a reviewer comment on a Business Requirements Document.

Comment: "{comment}"

Reply with EXACTLY ONE word, nothing else:
- "question"       — if the reviewer is asking for clarification or more information
- "change_request" — if the reviewer wants to modify, add, or remove BRD content"""

    try:
        result = _chat(prompt, max_tokens=8).strip().lower().split()[0]
        return result if result in ("question", "change_request") else "change_request"
    except Exception:
        return "change_request"


def answer_brd_question(question: str, brd_content: str) -> str:
    """Answer a reviewer's question about the BRD without modifying it."""
    prompt = f"""You are the senior business analyst who wrote this BRD.
A reviewer has asked a question — answer it clearly using the BRD content.
If the answer is not covered in the BRD, say so and suggest what information would be needed.

BRD:
{brd_content[:8000]}

Reviewer's question: {question}

Provide a clear, concise answer (2-5 sentences). Do NOT propose changes."""

    return _chat(prompt, max_tokens=600)


def propose_brd_change_summary(feedback: str, brd_content: str) -> str:
    """Summarise what BRD sections would change, without generating the full update.

    Returns a bullet-point proposal that is posted to GitHub for reviewer
    confirmation before the actual update is applied.
    """
    prompt = f"""A reviewer has requested changes to a BRD. Summarise what would be updated.

BRD (first 4000 chars):
{brd_content[:4000]}

Requested changes: {feedback}

Write 2-4 bullet points describing:
- Which section(s) would be affected (use the exact heading from the BRD)
- What specifically would be added, removed, or changed

Be precise and specific. Do NOT write the actual updated BRD content."""

    return _chat(prompt, max_tokens=400)
