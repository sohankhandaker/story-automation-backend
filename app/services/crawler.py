"""
Web page crawler — extracts readable text from a URL for AI context.
Uses httpx + BeautifulSoup. Strips nav/footer/scripts and returns plain text.
"""
import logging
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StoryAutomation/1.0; +https://github.com)"
    )
}
_TIMEOUT = 15
_MAX_CHARS = 8000


def fetch_page_text(url: str) -> str:
    """Fetch a URL and return its main readable text (max 8000 chars)."""
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "meta", "link"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        result = "\n".join(lines)
        return result[:_MAX_CHARS]
    except Exception as e:
        log.warning(f"Crawler failed for {url}: {e}")
        return ""
