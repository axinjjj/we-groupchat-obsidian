import unittest

from core.config import _sanitize_config
from core.taxonomy_assignment import (
    FREE_FORM_PROFILE,
    HUMAN_AI_INTIMACY_LEGACY_NAMES,
    TaxonomyResolution,
    resolve_taxonomy_profile,
    taxonomy_assignment_summary,
)


REGISTERED = {"human_ai_intimacy_v1"}


class TaxonomyAssignmentTests(unittest.TestCase):
    def test_explicit_free_form_survives_sanitize_and_blocks_legacy_fallback(self):
        config = _sanitize_config({
            "monitor_chat_taxonomy_profiles": {
                " room@chatroom ": f" {FREE_FORM_PROFILE} ",
            }
        })

        result = resolve_taxonomy_profile(
            config,
            REGISTERED,
            source_chat_username="room@chatroom",
            source_chat="示例人机互动群",
        )

        self.assertEqual(
            config["monitor_chat_taxonomy_profiles"],
            {"room@chatroom": FREE_FORM_PROFILE},
        )
        self.assertEqual(result, TaxonomyResolution("", "free_form"))

    def test_explicit_username_assignment_wins_after_rename(self):
        config = {
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "human_ai_intimacy_v1",
            }
        }
        result = resolve_taxonomy_profile(
            config,
            REGISTERED,
            source_chat_username="room@chatroom",
            source_chat="完全改过的群名",
            vault_chat_name="稳定文件夹",
        )
        self.assertEqual(result, TaxonomyResolution("human_ai_intimacy_v1", "explicit"))

    def test_unknown_explicit_profile_does_not_fall_back_by_name(self):
        config = {
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "missing_profile",
            }
        }
        result = resolve_taxonomy_profile(
            config,
            REGISTERED,
            source_chat_username="room@chatroom",
            source_chat="示例人机互动群",
        )
        self.assertEqual(result, TaxonomyResolution("", "unknown"))

    def test_exact_legacy_name_is_compatibility_fallback(self):
        self.assertIn("示例人机互动群", HUMAN_AI_INTIMACY_LEGACY_NAMES)
        result = resolve_taxonomy_profile(
            {}, REGISTERED, source_chat="♥︎示例人机互动群♥︎"
        )
        self.assertEqual(result, TaxonomyResolution("human_ai_intimacy_v1", "legacy_name"))

    def test_legacy_fallback_rejects_changed_spacing_punctuation_and_case(self):
        for changed_name in (
            "示 例人机互动群",
            "♥示例人机互动群♥",
            "example interaction lab",
            "Example interaction lab",
        ):
            with self.subTest(changed_name=changed_name):
                result = resolve_taxonomy_profile(
                    {}, REGISTERED, source_chat=changed_name
                )
                self.assertEqual(result, TaxonomyResolution("", "free_form"))

    def test_username_bearing_write_does_not_fall_back_through_vault_alias(self):
        result = resolve_taxonomy_profile(
            {},
            REGISTERED,
            source_chat_username="room@chatroom",
            source_chat="群已经改名",
            vault_chat_name="示例人机互动群",
        )

        self.assertEqual(result, TaxonomyResolution("", "free_form"))

    def test_username_bearing_write_keeps_exact_display_name_compatibility(self):
        result = resolve_taxonomy_profile(
            {},
            REGISTERED,
            source_chat_username="room@chatroom",
            source_chat="示例人机互动群",
            vault_chat_name="Unrelated Alias",
        )

        self.assertEqual(
            result,
            TaxonomyResolution("human_ai_intimacy_v1", "legacy_name"),
        )

    def test_unrelated_chat_stays_free_form(self):
        result = resolve_taxonomy_profile(
            {}, REGISTERED, source_chat="国际法读书会"
        )
        self.assertEqual(result, TaxonomyResolution("", "free_form"))

    def test_summary_is_counts_only(self):
        config = {
            "monitor_chat_taxonomy_profiles": {
                "one@chatroom": "human_ai_intimacy_v1",
                "two@chatroom": "missing_profile",
            }
        }
        chats = [
            {"username": "one@chatroom", "name": "Secret One"},
            {"username": "two@chatroom", "name": "Secret Two"},
            {"username": "three@chatroom", "name": "Unrelated"},
        ]
        self.assertEqual(
            taxonomy_assignment_summary(config, chats, REGISTERED),
            {"explicit": 1, "legacy_name": 0, "unknown": 1, "free_form": 1},
        )


if __name__ == "__main__":
    unittest.main()
