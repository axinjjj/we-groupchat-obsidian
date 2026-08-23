import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

from core.knowledge import KnowledgeMetadataQueryError
from scripts.configure_monitor import (
    choose_taxonomy_profile,
    choose_vault_alias,
    configure,
)


class ConfigureMonitorTests(unittest.TestCase):
    def run_configure(
        self,
        config,
        group,
        *,
        save_config,
        prompt_side_effect=None,
        stdout=None,
    ):
        prompts = prompt_side_effect or ["1", "ollama", "", "topic", "3", "关注推送"]
        groups = group if isinstance(group, list) else [group]
        with (
            patch("scripts.configure_monitor.load_config", return_value=config),
            patch("scripts.configure_monitor.get_cached_keys", return_value={"key": "value"}),
            patch("scripts.configure_monitor.os.path.isdir", return_value=True),
            patch("scripts.configure_monitor.WeChatDB"),
            patch("scripts.configure_monitor.list_groups", return_value=groups),
            patch("scripts.configure_monitor.prompt", side_effect=prompts),
            patch("scripts.configure_monitor.choose_obsidian_root", return_value="/vault"),
            patch("scripts.configure_monitor.ensure_obsidian_vault"),
            patch("scripts.configure_monitor.save_config", side_effect=save_config),
            patch("scripts.configure_monitor.reset_state_to_now"),
        ):
            output = stdout if stdout is not None else StringIO()
            with redirect_stdout(output):
                return configure(Namespace(search="", limit=80))

    def test_v1_singleton_is_marked_and_defaulted_in_group_selection(self):
        config = {
            "db_dir": "/db",
            "ai_provider": "ollama",
            "monitor_chat_username": "room@chatroom",
            "monitor_chat_display_name": "Legacy Room",
        }
        group = {"username": "room@chatroom", "name": "Legacy Room"}
        groups = [
            {"username": "other@chatroom", "name": "Other Room"},
            group,
        ]
        prompt_defaults = []
        prompt_values = iter(["ollama", "", "topic", "3", "关注推送"])

        def prompt_with_default(_text, default=None):
            prompt_defaults.append(default)
            if len(prompt_defaults) == 1:
                return default
            return next(prompt_values)

        store = MagicMock()
        store.vault_chat_alias_candidates.return_value = []
        output = StringIO()
        with (
            patch("scripts.configure_monitor.KnowledgeStore.from_config", return_value=store),
            patch("scripts.configure_monitor.choose_taxonomy_profile", return_value=""),
            patch("scripts.configure_monitor.choose_vault_alias", return_value="Legacy Room"),
        ):
            result = self.run_configure(
                config,
                groups,
                save_config=lambda _config: None,
                prompt_side_effect=prompt_with_default,
                stdout=output,
            )

        self.assertEqual(result, 0)
        self.assertEqual(prompt_defaults[0], "2")
        self.assertIn("2. Legacy Room *", output.getvalue())

    def test_legacy_chat_defaults_to_human_ai_preset(self):
        group = {"username": "room@chatroom", "name": "示例人机互动群"}
        output = StringIO()
        with (
            patch("scripts.configure_monitor.prompt", return_value="1"),
            redirect_stdout(output),
        ):
            result = choose_taxonomy_profile(group, {})
        self.assertEqual(result, "human_ai_intimacy_v1")
        self.assertIn("示例人机互动群 的知识库分类", output.getvalue())

    def test_explicit_free_form_returns_empty_profile(self):
        group = {"username": "room@chatroom", "name": "示例人机互动群"}
        output = StringIO()
        with (
            patch("scripts.configure_monitor.prompt", return_value="2"),
            redirect_stdout(output),
        ):
            result = choose_taxonomy_profile(group, {})
        self.assertEqual(result, "")
        self.assertIn("2. 自由分类", output.getvalue())

    def test_existing_alias_is_never_overwritten(self):
        group = {"username": "room@chatroom", "name": "新群名"}
        config = {"monitor_chat_aliases": {"room@chatroom": "旧稳定文件夹"}}
        self.assertEqual(choose_vault_alias(group, config, []), "旧稳定文件夹")

    def test_single_metadata_candidate_becomes_upgrade_alias(self):
        group = {"username": "room@chatroom", "name": "新群名"}
        self.assertEqual(choose_vault_alias(group, {}, ["旧稳定文件夹"]), "旧稳定文件夹")

    def test_ambiguous_metadata_requires_explicit_selection(self):
        group = {"username": "room@chatroom", "name": "新群名"}
        output = StringIO()
        with (
            patch("scripts.configure_monitor.prompt", return_value="2"),
            redirect_stdout(output),
        ):
            result = choose_vault_alias(group, {}, ["旧文件夹", "另一个文件夹"])
        self.assertEqual(result, "另一个文件夹")
        self.assertIn("找到多个历史 vault 文件夹", output.getvalue())

    def test_configure_refuses_unstable_username_before_save(self):
        saved = MagicMock()

        result = self.run_configure(
            {"db_dir": "/db", "ai_provider": "ollama"},
            {"username": "wxid_unstable", "name": "不稳定群"},
            save_config=saved,
        )

        self.assertEqual(result, 1)
        saved.assert_not_called()

    def test_configure_reads_alias_metadata_and_persists_preferences(self):
        config = {
            "db_dir": "/db",
            "ai_provider": "ollama",
            "monitor_chat_aliases": {"old@chatroom": "Existing Vault"},
            "monitor_chat_taxonomy_profiles": {
                "old@chatroom": "human_ai_intimacy_v1"
            },
        }
        group = {"username": "room@chatroom", "name": "新群名"}
        saved = []
        store = MagicMock()
        store.vault_chat_alias_candidates.return_value = ["Stable Vault"]

        with (
            patch("scripts.configure_monitor.KnowledgeStore.from_config", return_value=store) as from_config,
            patch(
                "scripts.configure_monitor.choose_taxonomy_profile",
                return_value="human_ai_intimacy_v1",
            ) as choose_profile,
            patch(
                "scripts.configure_monitor.choose_vault_alias",
                return_value="Stable Vault",
            ) as choose_alias,
        ):
            result = self.run_configure(config, group, save_config=saved.append)

        self.assertEqual(result, 0)
        self.assertTrue(from_config.call_args.kwargs["read_only"])
        store.vault_chat_alias_candidates.assert_called_once_with("room@chatroom")
        choose_profile.assert_called_once_with(group, config)
        choose_alias.assert_called_once_with(group, config, ["Stable Vault"])
        self.assertEqual(saved[0]["monitor_chat_aliases"], {
            "old@chatroom": "Existing Vault",
            "room@chatroom": "Stable Vault",
        })
        self.assertEqual(saved[0]["monitor_chat_taxonomy_profiles"], {
            "old@chatroom": "human_ai_intimacy_v1",
            "room@chatroom": "human_ai_intimacy_v1",
        })

    def test_deepseek_model_prompt_defaults_to_v4_flash(self):
        config = {"db_dir": "/db", "ai_provider": "deepseek"}
        group = {"username": "room@chatroom", "name": "Example Group"}
        prompt_defaults = {}
        saved = []
        store = MagicMock()
        store.vault_chat_alias_candidates.return_value = []

        def accept_default(text, default=None):
            prompt_defaults[text] = default
            return default or ""

        with (
            patch("scripts.configure_monitor.KnowledgeStore.from_config", return_value=store),
            patch("scripts.configure_monitor.choose_taxonomy_profile", return_value=""),
            patch("scripts.configure_monitor.choose_vault_alias", return_value="Example Group"),
            patch("scripts.configure_monitor.load_key", return_value="test-api-key"),
            patch("scripts.configure_monitor.prompt_yes_no", return_value=False),
        ):
            result = self.run_configure(
                config,
                group,
                save_config=saved.append,
                prompt_side_effect=accept_default,
            )

        self.assertEqual(result, 0)
        self.assertEqual(prompt_defaults["AI provider"], "deepseek")
        self.assertEqual(prompt_defaults["AI model"], "deepseek-v4-flash")
        self.assertEqual(saved[0]["ai_model"], "deepseek-v4-flash")

    def test_configure_refuses_metadata_query_failure_before_alias_seed_or_save(self):
        config = {
            "db_dir": "/db",
            "ai_provider": "ollama",
            "monitor_chat_aliases": {},
        }
        group = {"username": "room@chatroom", "name": "Private Group Name"}
        saved = MagicMock()
        store = MagicMock()
        store.vault_chat_alias_candidates.side_effect = KnowledgeMetadataQueryError(
            "knowledge metadata query failed: /private/db"
        )

        with (
            patch("scripts.configure_monitor.KnowledgeStore.from_config", return_value=store),
            patch("scripts.configure_monitor.choose_taxonomy_profile") as choose_profile,
            patch("scripts.configure_monitor.choose_vault_alias") as choose_alias,
        ):
            output = StringIO()
            result = self.run_configure(
                config,
                group,
                save_config=saved,
                stdout=output,
            )

        self.assertEqual(result, 1)
        saved.assert_not_called()
        choose_profile.assert_not_called()
        choose_alias.assert_not_called()
        self.assertEqual(config["monitor_chat_aliases"], {})
        self.assertIn("无法读取历史 vault metadata，配置未保存。", output.getvalue())
        self.assertNotIn("/private/db", output.getvalue())


if __name__ == "__main__":
    unittest.main()
