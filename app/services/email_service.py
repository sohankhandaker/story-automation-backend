"""
Email notification service — SMTP with branded HTML templates.
Set SMTP_* vars in .env to enable. All functions are no-ops when unconfigured.
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
    name = settings.email_from_name or "Story Automation"
    return f"{name} <{addr}>"


def _send(to_email: str, subject: str, html_body: str) -> None:
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


# ── Public API ────────────────────────────────────────────────────────────────

def send_review_request(
    *,
    reviewer_name: str,
    reviewer_email: str,
    requester_name: str,
    story_title: str,
    github_issue_url: str,
) -> bool:
    if not _is_configured() or not reviewer_email:
        return False
    try:
        _send(
            reviewer_email,
            f"Review Requested: {story_title}",
            _tpl_review_request(reviewer_name, requester_name, story_title, github_issue_url),
        )
        log.info(f"Review-request email sent to {reviewer_email}")
        return True
    except Exception as e:
        log.error(f"Failed to send review-request email: {e}")
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
    if not _is_configured() or not reviewer_email:
        return False
    try:
        _send(
            reviewer_email,
            f"Story Updated (Cycle {cycle}): {story_title}",
            _tpl_review_updated(reviewer_name, story_title, github_issue_url, change_summary, cycle),
        )
        log.info(f"Review-updated email sent to {reviewer_email} (cycle {cycle})")
        return True
    except Exception as e:
        log.error(f"Failed to send review-updated email: {e}")
        return False


def send_story_approved(
    *,
    creator_name: str,
    creator_email: str,
    reviewer_name: str,
    story_title: str,
    github_issue_url: str,
) -> bool:
    if not _is_configured() or not creator_email:
        return False
    try:
        _send(
            creator_email,
            f"Story Approved ✓: {story_title}",
            _tpl_approved(creator_name, reviewer_name, story_title, github_issue_url),
        )
        log.info(f"Approval email sent to creator {creator_email}")
        return True
    except Exception as e:
        log.error(f"Failed to send approval email: {e}")
        return False


def send_feedback_received(
    *,
    creator_name: str,
    creator_email: str,
    reviewer_name: str,
    story_title: str,
    github_issue_url: str,
    feedback_summary: str,
    cycle: int,
) -> bool:
    """Notify the story creator when a reviewer leaves feedback."""
    if not _is_configured() or not creator_email:
        return False
    try:
        _send(
            creator_email,
            f"Feedback Received (Cycle {cycle}): {story_title}",
            _tpl_feedback_received(creator_name, reviewer_name, story_title,
                                   github_issue_url, feedback_summary, cycle),
        )
        log.info(f"Feedback-received email sent to creator {creator_email}")
        return True
    except Exception as e:
        log.error(f"Failed to send feedback-received email: {e}")
        return False


# ── Base layout ───────────────────────────────────────────────────────────────

def _base(header_icon: str, header_title: str, header_sub: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{header_title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#EEF4FB;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;padding:32px 16px}}
  .outer{{max-width:600px;margin:0 auto}}

  /* top logo bar */
  .logo-bar{{text-align:center;padding:0 0 20px}}
  .logo-bar span{{display:inline-flex;align-items:center;gap:8px;
    background:#fff;padding:8px 18px;border-radius:20px;
    box-shadow:0 1px 6px rgba(24,139,255,.15)}}
  .logo-bar .dot{{width:10px;height:10px;border-radius:50%;
    background:linear-gradient(135deg,#188BFF,#0A5FC4)}}
  .logo-bar strong{{font-size:13px;color:#0A5FC4;letter-spacing:.2px}}

  /* card */
  .card{{background:#fff;border-radius:16px;overflow:hidden;
    box-shadow:0 4px 28px rgba(10,95,196,.10)}}

  /* header */
  .hd{{background:linear-gradient(135deg,#188BFF 0%,#0A5FC4 100%);
    padding:36px 36px 32px;position:relative;overflow:hidden}}
  .hd::after{{content:'';position:absolute;right:-40px;top:-40px;
    width:200px;height:200px;border-radius:50%;
    background:rgba(255,255,255,.06)}}
  .hd-icon{{font-size:32px;margin-bottom:12px}}
  .hd h1{{color:#fff;font-size:22px;font-weight:700;line-height:1.3;letter-spacing:-.3px}}
  .hd p{{color:rgba(255,255,255,.72);font-size:13px;margin-top:6px}}

  /* body */
  .bd{{padding:32px 36px}}
  .bd p{{color:#374151;font-size:15px;line-height:1.65;margin-bottom:16px}}
  .bd p:last-child{{margin-bottom:0}}

  /* info card */
  .info{{background:#F0F7FF;border:1px solid #C7E0FF;border-radius:12px;
    padding:20px 22px;margin:24px 0}}
  .info-row{{display:flex;align-items:flex-start;gap:12px;margin-bottom:14px}}
  .info-row:last-child{{margin-bottom:0}}
  .info-label{{font-size:11px;font-weight:700;color:#5B8DB8;text-transform:uppercase;
    letter-spacing:.7px;white-space:nowrap;padding-top:2px}}
  .info-value{{font-size:14px;font-weight:600;color:#1E3A5F;line-height:1.4}}
  .info-value.light{{font-weight:400;color:#374151}}

  /* badge */
  .badge{{display:inline-block;background:linear-gradient(135deg,#188BFF,#0A5FC4);
    color:#fff;font-size:11px;font-weight:700;padding:3px 10px;
    border-radius:10px;letter-spacing:.3px;vertical-align:middle;margin-left:8px}}
  .badge-green{{background:linear-gradient(135deg,#10B981,#059669)}}
  .badge-amber{{background:linear-gradient(135deg,#F59E0B,#D97706)}}

  /* CTA button */
  .cta-wrap{{text-align:center;margin:28px 0 8px}}
  .cta{{display:inline-block;background:linear-gradient(135deg,#188BFF,#0A5FC4);
    color:#fff!important;text-decoration:none;padding:14px 36px;
    border-radius:10px;font-weight:700;font-size:15px;
    box-shadow:0 4px 14px rgba(24,139,255,.35);letter-spacing:.1px}}

  /* how-to box */
  .howto{{background:#F8FAFC;border-radius:12px;padding:20px 22px;margin:24px 0}}
  .howto-title{{font-size:12px;font-weight:700;color:#94A3B8;text-transform:uppercase;
    letter-spacing:.7px;margin-bottom:14px}}
  .howto-item{{display:flex;gap:12px;margin-bottom:12px}}
  .howto-item:last-child{{margin-bottom:0}}
  .howto-icon{{width:28px;height:28px;border-radius:8px;display:flex;
    align-items:center;justify-content:center;font-size:14px;flex-shrink:0}}
  .howto-icon.blue{{background:#E0EEFF}}
  .howto-icon.green{{background:#D1FAE5}}
  .howto-text{{font-size:14px;color:#374151;line-height:1.5;padding-top:4px}}
  .howto-text strong{{color:#1E3A5F}}
  code{{background:#E8F0FE;color:#1D4ED8;padding:2px 6px;border-radius:4px;
    font-size:13px;font-weight:600}}

  /* divider */
  .div{{border:none;border-top:1px solid #E9F0F8;margin:24px 0}}

  /* footer */
  .ft{{padding:20px 36px 28px;text-align:center}}
  .ft p{{font-size:12px;color:#94A3B8;line-height:1.6}}
  .ft a{{color:#188BFF;text-decoration:none}}
</style>
</head>
<body>
<div class="outer">
  <div class="logo-bar">
    <span><span class="dot"></span><strong>SELISE Story Automation</strong></span>
  </div>
  <div class="card">
    <div class="hd">
      <div class="hd-icon">{header_icon}</div>
      <h1>{header_title}</h1>
      <p>{header_sub}</p>
    </div>
    <div class="bd">
{body}
    </div>
    <hr class="div">
    <div class="ft">
      <p>This is an automated message from <strong>SELISE Story Automation</strong>.<br>
      Do not reply — use <a href="https://github.com">GitHub</a> to interact with the story.</p>
    </div>
  </div>
</div>
</body>
</html>"""


# ── Templates ─────────────────────────────────────────────────────────────────

def _tpl_review_request(
    reviewer_name: str,
    requester_name: str,
    story_title: str,
    github_issue_url: str,
) -> str:
    body = f"""
      <p>Hi <strong>{reviewer_name}</strong>,</p>
      <p><strong>{requester_name}</strong> has requested your review on an AI-generated user story.
      The full story is ready and waiting for your feedback.</p>

      <div class="info">
        <div class="info-row">
          <span class="info-label">Story</span>
          <span class="info-value">{story_title}</span>
        </div>
        <div class="info-row">
          <span class="info-label">Requested by</span>
          <span class="info-value light">{requester_name}</span>
        </div>
        <div class="info-row">
          <span class="info-label">Status</span>
          <span class="info-value"><span class="badge badge-amber">Awaiting Review</span></span>
        </div>
      </div>

      <div class="cta-wrap">
        <a class="cta" href="{github_issue_url}">Open Story on GitHub →</a>
      </div>

      <div class="howto">
        <div class="howto-title">How to respond</div>
        <div class="howto-item">
          <div class="howto-icon blue">📝</div>
          <div class="howto-text">
            <strong>Give feedback:</strong> Leave a comment on the GitHub issue describing
            what should change. The AI agent will revise the story and notify you.
          </div>
        </div>
        <div class="howto-item">
          <div class="howto-icon green">✅</div>
          <div class="howto-text">
            <strong>Approve:</strong> Comment <code>APPROVED</code> (or <code>LGTM</code>
            / <code>Looks good</code>) and the story is marked as Done automatically.
          </div>
        </div>
      </div>
"""
    return _base("👋", "Your review is requested", "SELISE Story Automation · User Story Review", body)


def _tpl_review_updated(
    reviewer_name: str,
    story_title: str,
    github_issue_url: str,
    change_summary: str,
    cycle: int,
) -> str:
    body = f"""
      <p>Hi <strong>{reviewer_name}</strong>,</p>
      <p>The AI agent has revised the user story based on your feedback.
      Please take another look and approve or leave further comments.</p>

      <div class="info">
        <div class="info-row">
          <span class="info-label">Story</span>
          <span class="info-value">{story_title} <span class="badge">Cycle {cycle}</span></span>
        </div>
        <div class="info-row">
          <span class="info-label">What changed</span>
          <span class="info-value light">{change_summary}</span>
        </div>
        <div class="info-row">
          <span class="info-label">Status</span>
          <span class="info-value"><span class="badge badge-amber">Needs Re-review</span></span>
        </div>
      </div>

      <div class="cta-wrap">
        <a class="cta" href="{github_issue_url}">View Updated Story →</a>
      </div>

      <div class="howto">
        <div class="howto-title">What to do next</div>
        <div class="howto-item">
          <div class="howto-icon blue">📝</div>
          <div class="howto-text">
            <strong>Still have feedback?</strong> Leave another comment on GitHub
            and the agent will update it again.
          </div>
        </div>
        <div class="howto-item">
          <div class="howto-icon green">✅</div>
          <div class="howto-text">
            <strong>Happy with it?</strong> Comment <code>APPROVED</code> to close the loop
            and mark the story as Done.
          </div>
        </div>
      </div>
"""
    return _base("✏️", "Story revised — please re-review",
                 f"Review Cycle {cycle} · SELISE Story Automation", body)


def _tpl_approved(
    creator_name: str,
    reviewer_name: str,
    story_title: str,
    github_issue_url: str,
) -> str:
    body = f"""
      <p>Hi <strong>{creator_name}</strong>,</p>
      <p>Great news! <strong>{reviewer_name}</strong> has approved your user story.
      The task is now marked as <strong>Done</strong> and the final story is on GitHub.</p>

      <div class="info">
        <div class="info-row">
          <span class="info-label">Story</span>
          <span class="info-value">{story_title}</span>
        </div>
        <div class="info-row">
          <span class="info-label">Approved by</span>
          <span class="info-value light">{reviewer_name}</span>
        </div>
        <div class="info-row">
          <span class="info-label">Status</span>
          <span class="info-value"><span class="badge badge-green">Done ✓</span></span>
        </div>
      </div>

      <div class="cta-wrap">
        <a class="cta" href="{github_issue_url}">View Final Story →</a>
      </div>
"""
    return _base("🎉", "Your story has been approved!", "SELISE Story Automation", body)


def _tpl_feedback_received(
    creator_name: str,
    reviewer_name: str,
    story_title: str,
    github_issue_url: str,
    feedback_summary: str,
    cycle: int,
) -> str:
    body = f"""
      <p>Hi <strong>{creator_name}</strong>,</p>
      <p><strong>{reviewer_name}</strong> has left feedback on your user story.
      The AI agent is now updating the story based on their comments.</p>

      <div class="info">
        <div class="info-row">
          <span class="info-label">Story</span>
          <span class="info-value">{story_title} <span class="badge">Cycle {cycle}</span></span>
        </div>
        <div class="info-row">
          <span class="info-label">Reviewer</span>
          <span class="info-value light">{reviewer_name}</span>
        </div>
        <div class="info-row">
          <span class="info-label">Feedback</span>
          <span class="info-value light">{feedback_summary}</span>
        </div>
        <div class="info-row">
          <span class="info-label">Status</span>
          <span class="info-value"><span class="badge badge-amber">Changes Requested</span></span>
        </div>
      </div>

      <p>The AI agent will revise the story and notify <strong>{reviewer_name}</strong>
      when it's ready for re-review. No action needed from you right now.</p>

      <div class="cta-wrap">
        <a class="cta" href="{github_issue_url}">View on GitHub →</a>
      </div>
"""
    return _base("💬", "Feedback received on your story",
                 f"Review Cycle {cycle} · SELISE Story Automation", body)
