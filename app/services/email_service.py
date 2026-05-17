"""
Email notification service using SMTP (works with Gmail App Passwords,
Office 365, SendGrid SMTP relay, or any standard SMTP server).

Set SMTP_USERNAME / SMTP_PASSWORD in .env to enable.
If not configured, notifications are skipped silently (no crash).
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..config import settings

log = logging.getLogger(__name__)


def _is_configured() -> bool:
    return bool(settings.smtp_username and settings.smtp_password)


def _from_address() -> str:
    addr = settings.email_from_address or settings.smtp_username
    name = settings.email_from_name
    return f"{name} <{addr}>"


def _send(to_email: str, subject: str, html_body: str) -> None:
    """Low-level SMTP send. Uses SSL for port 465, STARTTLS for port 587/25."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _from_address()
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    use_ssl = settings.smtp_port == 465
    if use_ssl:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.sendmail(settings.smtp_username, to_email, msg.as_string())
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.sendmail(settings.smtp_username, to_email, msg.as_string())


# ── Public helpers ────────────────────────────────────────────────────────────

def send_review_request(
    *,
    reviewer_name: str,
    reviewer_email: str,
    requester_name: str,
    story_title: str,
    github_issue_url: str,
) -> bool:
    """
    Notify a reviewer that their review has been requested.
    Returns True on success, False if email not configured or send fails.
    """
    if not _is_configured():
        log.info("Email not configured — skipping review-request notification")
        return False
    if not reviewer_email:
        log.info(f"No email stored for reviewer '{reviewer_name}' — skipping")
        return False

    subject = f"[Story Review] {story_title}"
    html = _review_request_html(
        reviewer_name=reviewer_name,
        requester_name=requester_name,
        story_title=story_title,
        github_issue_url=github_issue_url,
    )
    try:
        _send(reviewer_email, subject, html)
        log.info(f"Review-request email sent to {reviewer_email}")
        return True
    except Exception as e:
        log.error(f"Failed to send review-request email to {reviewer_email}: {e}")
        return False


def send_review_updated(
    *,
    reviewer_name: str,
    reviewer_email: str,
    story_title: str,
    github_issue_url: str,
    change_summary: str,
    cycle: int,
) -> bool:
    """
    Notify reviewer that the story was revised based on their feedback.
    Returns True on success.
    """
    if not _is_configured() or not reviewer_email:
        return False

    subject = f"[Story Updated — Cycle {cycle}] {story_title}"
    html = _review_updated_html(
        reviewer_name=reviewer_name,
        story_title=story_title,
        github_issue_url=github_issue_url,
        change_summary=change_summary,
        cycle=cycle,
    )
    try:
        _send(reviewer_email, subject, html)
        log.info(f"Review-updated email sent to {reviewer_email} (cycle {cycle})")
        return True
    except Exception as e:
        log.error(f"Failed to send review-updated email to {reviewer_email}: {e}")
        return False


def send_story_approved(
    *,
    creator_name: str,
    creator_email: str,
    reviewer_name: str,
    story_title: str,
    github_issue_url: str,
) -> bool:
    """
    Notify creator that their story was approved.
    Returns True on success.
    """
    if not _is_configured() or not creator_email:
        return False

    subject = f"[Story Approved ✅] {story_title}"
    html = _approved_html(
        creator_name=creator_name,
        reviewer_name=reviewer_name,
        story_title=story_title,
        github_issue_url=github_issue_url,
    )
    try:
        _send(creator_email, subject, html)
        log.info(f"Approval email sent to creator {creator_email}")
        return True
    except Exception as e:
        log.error(f"Failed to send approval email to {creator_email}: {e}")
        return False


# ── HTML templates ────────────────────────────────────────────────────────────

_BASE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ margin:0; padding:0; background:#F2F5FA; font-family: -apple-system, 'Segoe UI', Arial, sans-serif; }}
  .wrap {{ max-width:600px; margin:32px auto; background:#ffffff; border-radius:16px;
           overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#188BFF,#0A5FC4); padding:32px 36px; }}
  .header h1 {{ margin:0; color:#fff; font-size:20px; font-weight:700; letter-spacing:-.3px; }}
  .header p  {{ margin:6px 0 0; color:rgba(255,255,255,.75); font-size:13px; }}
  .body  {{ padding:32px 36px; }}
  .body p {{ margin:0 0 16px; color:#2D3748; font-size:15px; line-height:1.6; }}
  .card  {{ background:#F7FAFF; border:1px solid #D8E8FF; border-radius:10px;
            padding:18px 20px; margin:20px 0; }}
  .card .label {{ font-size:11px; font-weight:600; color:#5B8DB8; text-transform:uppercase;
                  letter-spacing:.6px; margin-bottom:4px; }}
  .card .value {{ font-size:15px; font-weight:600; color:#1A2B3C; }}
  .cta  {{ display:inline-block; margin:8px 0; background:linear-gradient(135deg,#188BFF,#0A5FC4);
           color:#fff !important; text-decoration:none; padding:13px 28px;
           border-radius:8px; font-weight:600; font-size:15px; }}
  .steps {{ background:#F7FAFF; border-left:4px solid #188BFF; border-radius:0 8px 8px 0;
            padding:16px 20px; margin:20px 0; }}
  .steps p {{ margin:0 0 8px; font-size:14px; }}
  .steps p:last-child {{ margin:0; }}
  .steps strong {{ color:#188BFF; }}
  .footer {{ padding:20px 36px; border-top:1px solid #EEF2F7; }}
  .footer p {{ margin:0; font-size:12px; color:#94A3B8; }}
  .badge {{ display:inline-block; background:#E8F4FF; color:#188BFF; font-size:11px;
            font-weight:600; padding:3px 9px; border-radius:12px; border:1px solid #B8D9FF; }}
</style>
</head>
<body>
<div class="wrap">
{content}
<div class="footer">
  <p>Sent by <strong>SELISE Story Automation</strong> · Do not reply to this email.<br>
  Open the GitHub issue to interact with the story.</p>
</div>
</div>
</body>
</html>
"""


def _review_request_html(
    reviewer_name: str,
    requester_name: str,
    story_title: str,
    github_issue_url: str,
) -> str:
    content = f"""
<div class="header">
  <h1>👋 Your review is requested</h1>
  <p>SELISE Story Automation</p>
</div>
<div class="body">
  <p>Hi <strong>{reviewer_name}</strong>,</p>
  <p><strong>{requester_name}</strong> has requested your review on a user story.</p>

  <div class="card">
    <div class="label">Story</div>
    <div class="value">{story_title}</div>
  </div>

  <p>The complete story is in the GitHub issue description.
  Please open the link below to read it:</p>

  <p><a class="cta" href="{github_issue_url}">Open Story on GitHub →</a></p>

  <div class="steps">
    <p>📝 <strong>To give feedback:</strong><br>
    Leave a comment on the GitHub issue describing what should change.
    The AI agent will revise the story and notify you.</p>
    <p>✅ <strong>To approve:</strong><br>
    Comment <strong><code>APPROVED</code></strong> (or <code>LGTM</code> / <code>Looks good</code>)
    and the story will be marked as Done automatically.</p>
  </div>
</div>
"""
    return _BASE.format(content=content)


def _review_updated_html(
    reviewer_name: str,
    story_title: str,
    github_issue_url: str,
    change_summary: str,
    cycle: int,
) -> str:
    content = f"""
<div class="header">
  <h1>✏️ Story revised — your re-review needed</h1>
  <p>Review cycle {cycle} · SELISE Story Automation</p>
</div>
<div class="body">
  <p>Hi <strong>{reviewer_name}</strong>,</p>
  <p>The AI agent has revised the story based on your feedback. Please take another look.</p>

  <div class="card">
    <div class="label">Story <span class="badge">Cycle {cycle}</span></div>
    <div class="value">{story_title}</div>
  </div>

  <div class="card">
    <div class="label">What changed</div>
    <div class="value" style="font-weight:400;font-size:14px;">{change_summary}</div>
  </div>

  <p>The updated story is in the GitHub issue description:</p>
  <p><a class="cta" href="{github_issue_url}">View Updated Story →</a></p>

  <div class="steps">
    <p>📝 <strong>Still have feedback?</strong> Leave another comment on GitHub.</p>
    <p>✅ <strong>Happy with it?</strong> Comment <strong><code>APPROVED</code></strong> to close the loop.</p>
  </div>
</div>
"""
    return _BASE.format(content=content)


def _approved_html(
    creator_name: str,
    reviewer_name: str,
    story_title: str,
    github_issue_url: str,
) -> str:
    content = f"""
<div class="header">
  <h1>🎉 Your story has been approved!</h1>
  <p>SELISE Story Automation</p>
</div>
<div class="body">
  <p>Hi <strong>{creator_name}</strong>,</p>
  <p>Great news — <strong>{reviewer_name}</strong> has approved your user story.
  The task is now marked as <strong>Done</strong>.</p>

  <div class="card">
    <div class="label">Approved story</div>
    <div class="value">{story_title}</div>
  </div>

  <p><a class="cta" href="{github_issue_url}">View Final Story →</a></p>
</div>
"""
    return _BASE.format(content=content)
