from __future__ import annotations

from dataclasses import dataclass


FREE_FORM_PROFILE = "free_form"
HUMAN_AI_INTIMACY_PROFILE = "human_ai_intimacy_v1"
HUMAN_AI_INTIMACY_LEGACY_NAMES = (
    "示例人机互动群",
    "♥︎示例人机互动群♥︎",
    "Example Interaction Lab",
)


@dataclass(frozen=True)
class TaxonomyResolution:
    profile: str
    source: str


def resolve_taxonomy_profile(
    config: dict,
    registered_profile_ids: set[str],
    *,
    source_chat_username: str = "",
    source_chat: str = "",
    vault_chat_name: str = "",
) -> TaxonomyResolution:
    assignments = config.get("monitor_chat_taxonomy_profiles")
    assignments = assignments if isinstance(assignments, dict) else {}
    username = str(source_chat_username or "").strip()
    if username and username in assignments:
        profile = str(assignments[username] or "").strip()
        if profile == FREE_FORM_PROFILE:
            return TaxonomyResolution("", "free_form")
        if profile in registered_profile_ids:
            return TaxonomyResolution(profile, "explicit")
        return TaxonomyResolution("", "unknown")

    display_name = str(source_chat or "").strip()
    if display_name in HUMAN_AI_INTIMACY_LEGACY_NAMES:
        if HUMAN_AI_INTIMACY_PROFILE in registered_profile_ids:
            return TaxonomyResolution(HUMAN_AI_INTIMACY_PROFILE, "legacy_name")
    return TaxonomyResolution("", "free_form")


def taxonomy_assignment_summary(
    config: dict,
    chats: list[dict],
    registered_profile_ids: set[str],
) -> dict[str, int]:
    counts = {"explicit": 0, "legacy_name": 0, "unknown": 0, "free_form": 0}
    aliases = config.get("monitor_chat_aliases")
    aliases = aliases if isinstance(aliases, dict) else {}
    for chat in chats:
        username = str(chat.get("username") or "").strip()
        resolution = resolve_taxonomy_profile(
            config,
            registered_profile_ids,
            source_chat_username=username,
            source_chat=str(chat.get("name") or ""),
            vault_chat_name=str(aliases.get(username) or ""),
        )
        counts[resolution.source] += 1
    return counts
