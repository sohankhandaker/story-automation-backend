from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "development"
    secret_key: str = "dev-secret-key-change-in-production"
    database_url: str = "sqlite:///./app.db"

    github_token: str = ""
    github_owner: str = "SELISEdigitalplatforms"
    github_repo: str = "selise-madp"
    github_project_number: int = 447

    # GitHub OAuth SSO
    github_client_id: str = ""
    github_client_secret: str = ""
    web_app_url: str = "https://story-automation-mobile.onrender.com"

    # OpenRouter (preferred) — set this to use OpenRouter
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-3.5-sonnet"

    # Anthropic direct (fallback) — used if openrouter_api_key is empty
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    poll_interval_seconds: int = 60
    max_review_cycles: int = 5
    reviewer_reminder_hours: int = 48

    # Email / SMTP  ─────────────────────────────────────────────────────────────
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""        # e.g. yourapp@gmail.com
    smtp_password: str = ""        # Gmail App Password (not your login password)
    email_from_name: str = "SELISE SERA"
    email_from_address: str = ""   # defaults to smtp_username if empty

    class Config:
        env_file = ".env"


settings = Settings()
