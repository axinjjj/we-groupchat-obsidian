"""Link extraction plus an inert compatibility surface for remote previews."""
from __future__ import annotations

from urllib.parse import urlparse

from .url_safety import URL_RE, redact_url_for_display, redact_urls_in_text


LINK_PREVIEW_STATE = "link_preview_disabled"


def extract_links(text):
    """Extract unique HTTP(S) links from text, preserving exact first values."""
    links = []
    seen = set()
    for match in URL_RE.findall(str(text or "")):
        url = match.rstrip(".,;:!?，。；：！？、")
        if url and url not in seen:
            links.append(url)
            seen.add(url)
    return links


def is_wechat_record_url(url):
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    if parsed.netloc.casefold() != "support.weixin.qq.com":
        return False
    return "favorite_record" in parsed.query or "favorite_record" in parsed.path


def fetch_link_preview(url, *_args, **_kwargs):
    """Return a zero-network compatibility receipt.

    The former in-process crawler validated DNS separately from the HTTP peer
    connection and is therefore retired. Keeping this inert function avoids
    breaking callers that imported it while making every invocation fail
    closed, including calls made under an old ``monitor_fetch_links=true``
    configuration.
    """
    return {
        "url": redact_url_for_display(url),
        "status": LINK_PREVIEW_STATE,
        "state": LINK_PREVIEW_STATE,
        "network_requests": 0,
        "title": "",
        "summary": "Remote link preview is disabled; the URL was not requested.",
    }


def format_link_previews(previews):
    """Format legacy preview records without exposing URL credentials."""
    lines = ["Remote link preview is disabled; network_requests=0."]
    for idx, item in enumerate(previews or (), 1):
        url = redact_url_for_display(item.get("url", ""))
        status = str(item.get("state") or item.get("status") or LINK_PREVIEW_STATE)
        title = redact_urls_in_text(item.get("title", ""))
        summary = redact_urls_in_text(item.get("summary", ""))
        parts = [f"{idx}. {url}", f"状态：{status}"]
        if title:
            parts.append(f"标题：{title}")
        if summary:
            parts.append(f"摘要：{summary}")
        lines.append("\n   ".join(parts))
    return "\n".join(lines)


# Compatibility name for callers of the former module-private helper.
_redact_url_for_display = redact_url_for_display
