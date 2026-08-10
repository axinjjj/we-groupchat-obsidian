"""Closed producer metadata contract for generated Markdown."""
from datetime import datetime


SOURCE_APP = "we-groupchat-obsidian"
SOURCE_SCHEMA_VERSION = 1
PROJECTION_KINDS = frozenset({
    "date_index",
    "daily_digest",
    "history_summary",
    "review_surface",
})


def _value(item, key, default=""):
    try:
        return item[key]
    except (IndexError, KeyError, TypeError):
        return default


def aware_iso_from_timestamp(value):
    """Return a seconds-precision aware ISO timestamp for a canonical row time."""
    return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")


def atomic_source_lines(topic_id, generated_at):
    """Render the version-1 atomic topic metadata lines."""
    topic_id = int(topic_id)
    parsed = datetime.fromisoformat(str(generated_at))
    if topic_id <= 0 or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid atomic source metadata")
    return [
        f"source_app: {SOURCE_APP}",
        "source_kind: knowledge_topic",
        f"source_schema_version: {SOURCE_SCHEMA_VERSION}",
        f"source_id: wg_topic_{topic_id}",
        f"generated_at: {generated_at}",
    ]


def projection_source_lines(kind):
    """Render the version-1 metadata lines for a known projection kind."""
    if kind not in PROJECTION_KINDS:
        raise ValueError("unknown projection kind")
    return [
        f"source_app: {SOURCE_APP}",
        "source_kind: projection",
        f"source_schema_version: {SOURCE_SCHEMA_VERSION}",
        f"projection_kind: {kind}",
    ]


def is_history_summary(topic, events):
    """Classify history projections from canonical row and event signals."""
    return (
        str(_value(topic, "topic_key")).startswith("history-summary:")
        or "历史总结" in str(_value(topic, "title"))
        or any(
            _value(event, "event_type") == "history_summary"
            for event in events
        )
    )
