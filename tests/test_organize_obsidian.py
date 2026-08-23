import io
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import scripts.organize_obsidian as organize_obsidian
from scripts.organize_obsidian import (
    format_date_index_plan,
    format_knowledge_audit_report,
    format_taxonomy_review_brief,
    format_taxonomy_migration_plan,
    main,
)


class OrganizeObsidianTests(unittest.TestCase):
    def test_source_metadata_dry_run_is_counts_paths_only_and_read_only(self):
        self.assertTrue(
            hasattr(organize_obsidian, "plan_source_metadata_regeneration"),
            "source metadata planner is missing",
        )
        plan = {
            "database_path": "/runtime/knowledge.db",
            "vault_root": "/vault",
            "atomic_paths": ["关注推送/Chat/Topic.md"],
            "history_summary_paths": ["关注推送/Chat/History.md"],
            "date_index_targets": [{
                "rel_path": "关注推送/00-按日期.md",
                "status": "update",
                "conflict_path": "",
            }],
            "date_index_conflict_count": 0,
            "date_index_skip_count": 0,
            "daily_digest_paths": ["关注推送/Daily Digest/2026-07-16 Daily Digest.md"],
            "rewrite_candidate_count": 4,
        }
        stdout = io.StringIO()
        with (
            patch.object(
                organize_obsidian,
                "plan_source_metadata_regeneration",
                return_value=plan,
            ) as planner,
            patch.object(organize_obsidian, "load_config", return_value={"fixture": True}),
            patch.object(
                organize_obsidian.KnowledgeStore,
                "from_config",
                side_effect=AssertionError("writable store constructed"),
            ),
            patch.object(sys, "argv", ["organize_obsidian.py", "--source-metadata-dry-run"]),
            patch("sys.stdout", new=stdout),
        ):
            self.assertEqual(organize_obsidian.main(), 0)

        planner.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("/runtime/knowledge.db", output)
        self.assertIn("/vault", output)
        self.assertIn("关注推送/Chat/Topic.md", output)
        self.assertIn("rewrite candidates: 4", output)
        self.assertNotIn("summary:", output)
        self.assertNotIn("entities:", output)
        self.assertNotIn("markdown:", output)

    def test_no_argument_path_reexports_without_path_migration(self):
        store = Mock()
        store.reexport_all.return_value = 3
        with (
            patch("scripts.organize_obsidian.load_config", return_value={
                "monitor_obsidian_root": "/vault",
                "monitor_obsidian_subdir": "关注推送",
            }),
            patch("scripts.organize_obsidian.KnowledgeStore.from_config", return_value=store),
            patch.object(sys, "argv", ["organize_obsidian.py"]),
            patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            self.assertEqual(main(), 0)

        store.reexport_all.assert_called_once_with()
        store.reorganize_paths.assert_not_called()
        self.assertIn("重新导出: 3 篇", stdout.getvalue())

    def test_format_date_index_plan_reports_fallback_conflicts(self):
        lines = format_date_index_plan({
            "target_count": 2,
            "conflict_count": 1,
            "targets": [
                {
                    "rel_path": "关注推送/00-按日期.generated.md",
                    "path": "/vault/关注推送/00-按日期.generated.md",
                    "status": "fallback",
                    "conflict_path": "/vault/关注推送/00-按日期.md",
                },
                {
                    "rel_path": "关注推送/示例稳定群名/00-按日期.md",
                    "path": "/vault/关注推送/示例稳定群名/00-按日期.md",
                    "status": "create",
                    "conflict_path": "",
                },
            ],
        })

        text = "\n".join(lines)
        self.assertIn("日期索引: 2 个目标", text)
        self.assertIn("冲突/降级: 1 个", text)
        self.assertIn("fallback", text)
        self.assertIn("00-按日期.generated.md", text)

    def test_date_indexes_only_writes_indexes_without_reorganizing_notes(self):
        store = Mock()
        store.write_date_indexes.return_value = {
            "written_count": 1,
            "skipped_count": 0,
            "skipped": [],
        }

        with (
            patch(
                "scripts.organize_obsidian.load_config",
                return_value={
                    "monitor_obsidian_root": "/vault",
                    "monitor_obsidian_subdir": "关注推送",
                },
            ),
            patch("scripts.organize_obsidian.KnowledgeStore.from_config", return_value=store),
            patch.object(sys, "argv", ["organize_obsidian.py", "--date-indexes-only"]),
            patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            result = main()

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("日期索引更新完成", output)
        self.assertIn("写入/更新: 1 个", output)
        self.assertIn("跳过: 0 个", output)
        store.write_date_indexes.assert_called_once_with()
        store.find_category_changes.assert_not_called()
        store.reorganize_paths.assert_not_called()

    def test_dry_run_reports_single_date_index_targets(self):
        store = Mock()
        store.find_category_changes.return_value = []
        store.plan_date_indexes.return_value = {
            "target_count": 1,
            "conflict_count": 0,
            "targets": [
                {
                    "rel_path": "关注推送/00-按日期.md",
                    "path": "/vault/关注推送/00-按日期.md",
                    "status": "update",
                    "conflict_path": "",
                },
            ],
        }

        with (
            patch(
                "scripts.organize_obsidian.load_config",
                return_value={
                    "monitor_obsidian_root": "/vault",
                    "monitor_obsidian_subdir": "关注推送",
                },
            ),
            patch("scripts.organize_obsidian.KnowledgeStore.from_config", return_value=store),
            patch.object(sys, "argv", ["organize_obsidian.py", "--dry-run"]),
            patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            result = main()

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("日期索引: 1 个目标", output)
        self.assertIn("关注推送/00-按日期.md", output)
        self.assertNotIn("关注推送/按日期/2026-06.md", output)
        store.find_category_changes.assert_called_once_with()
        store.plan_date_indexes.assert_called_once_with()
        store.reorganize_paths.assert_not_called()

    def test_taxonomy_dry_run_reports_mapping_without_writing(self):
        store = Mock()
        store.plan_taxonomy_migration.return_value = {
            "profile": "human_ai_intimacy_v1",
            "taxonomy_version": 2,
            "assignment_source_counts": {
                "explicit": 2,
                "legacy_name": 1,
                "stable_alias": 1,
            },
            "folder_categories": ["互动实验与玩法", "待归类"],
            "total_topic_count": 8,
            "scoped_topic_count": 3,
            "migratable_topic_count": 2,
            "legacy_history_summary_count": 1,
            "category_change_count": 2,
            "path_change_count": 2,
            "unresolved_count": 1,
            "category_mappings": [
                {
                    "from": "AI伴侣交互",
                    "to": "互动实验与玩法",
                    "count": 1,
                    "example_paths": ["关注推送/示例人机互动群/AI伴侣交互/测试.md"],
                },
                {
                    "from": "非常细的新标签",
                    "to": "待归类",
                    "count": 1,
                    "example_paths": ["关注推送/Example Interaction Lab/非常细的新标签/测试.md"],
                },
            ],
            "changes": [
                {
                    "from_path": "关注推送/示例人机互动群/AI伴侣交互/测试.md",
                    "to_path": "关注推送/示例人机互动群/互动实验与玩法/测试.md",
                }
            ],
        }
        config = {
            "monitor_obsidian_root": "/vault",
            "monitor_obsidian_subdir": "关注推送",
        }

        with (
            patch("scripts.organize_obsidian.load_config", return_value=config),
            patch("scripts.organize_obsidian.KnowledgeStore.from_config", return_value=store) as from_config,
            patch.object(sys, "argv", ["organize_obsidian.py", "--taxonomy-dry-run"]),
            patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            result = main()

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("Taxonomy migration dry-run", output)
        self.assertIn("profile: human_ai_intimacy_v1 v2", output)
        self.assertIn("scoped topics: 3 / 8", output)
        self.assertIn("migratable topics: 2", output)
        self.assertIn("legacy history summaries: 1", output)
        self.assertIn(
            "assignment sources: stored=0, explicit=2, legacy_name=1, stable_alias=1",
            output,
        )
        self.assertIn("AI伴侣交互 -> 互动实验与玩法 | 1", output)
        self.assertIn("非常细的新标签 -> 待归类 | 1", output)
        self.assertIn("关注推送/示例人机互动群/互动实验与玩法/测试.md", output)
        from_config.assert_called_once_with(config, read_only=True)
        store.plan_taxonomy_migration.assert_called_once_with("human_ai_intimacy_v1")
        store.reorganize_paths.assert_not_called()

    def test_taxonomy_dry_run_reports_zero_migratable_topics_without_scoped_fallback(self):
        lines = format_taxonomy_migration_plan({
            "profile": "human_ai_intimacy_v1",
            "taxonomy_version": 2,
            "total_topic_count": 6,
            "scoped_topic_count": 6,
            "migratable_topic_count": 0,
            "legacy_history_summary_count": 6,
            "category_change_count": 0,
            "path_change_count": 0,
            "unresolved_count": 0,
            "category_mappings": [],
            "changes": [],
        })

        text = "\n".join(lines)

        self.assertIn("migratable topics: 0", text)
        self.assertNotIn("migratable topics: 6", text)
        self.assertIn("assignment sources: stored=0, explicit=0, legacy_name=0", text)

    def test_taxonomy_review_brief_reports_full_mapping_unresolved_and_metadata_only(self):
        lines = format_taxonomy_review_brief(
            {
                "profile": "human_ai_intimacy_v1",
                "taxonomy_version": 2,
                "total_topic_count": 8,
                "scoped_topic_count": 5,
                "migratable_topic_count": 4,
                "legacy_history_summary_count": 1,
                "assignment_source_counts": {"stored": 2, "explicit": 1},
                "category_change_count": 3,
                "path_change_count": 3,
                "unresolved_count": 1,
                "category_mappings": [
                    {
                        "from": "AI模型",
                        "to": "模型与平台",
                        "count": 2,
                        "example_paths": ["关注推送/示例人机互动群/AI模型/模型.md"],
                    },
                    {
                        "from": "非常细的新标签",
                        "to": "待归类",
                        "count": 1,
                        "example_paths": ["关注推送/Example Interaction Lab/非常细的新标签/测试.md"],
                    },
                ],
                "unresolved_items": [
                    {
                        "topic_id": 7,
                        "title": "边缘话题观察",
                        "from": "非常细的新标签",
                        "source_chat": "Example Interaction Lab",
                        "from_path": "关注推送/Example Interaction Lab/非常细的新标签/测试.md",
                        "to_path": "关注推送/Example Interaction Lab/待归类/测试.md",
                    }
                ],
            },
            [
                {
                    "title": "已在正确 folder 但缺 profile",
                    "from": "模型与平台",
                    "to": "模型与平台",
                    "reason": "taxonomy_profile",
                    "from_path": "关注推送/示例人机互动群/模型与平台/模型.md",
                    "to_path": "关注推送/示例人机互动群/模型与平台/模型.md",
                }
            ],
        )

        text = "\n".join(lines)
        self.assertIn("# Taxonomy Migration Review Brief", text)
        self.assertIn("profile: human_ai_intimacy_v1 v2", text)
        self.assertIn("category/path changes: 3", text)
        self.assertIn("metadata-only backfills: 1", text)
        self.assertIn("assignment sources: stored=2, explicit=1, legacy_name=0", text)
        self.assertIn("- AI模型 -> 模型与平台 | 2", text)
        self.assertIn("- 非常细的新标签 -> 待归类 | 1", text)
        self.assertIn("## Unresolved 待归类 Items", text)
        self.assertIn("边缘话题观察", text)
        self.assertIn("关注推送/Example Interaction Lab/待归类/测试.md", text)
        self.assertIn("## Metadata-Only Backfills", text)
        self.assertIn("已在正确 folder 但缺 profile", text)

    def test_taxonomy_review_brief_cli_is_read_only_and_does_not_reorganize(self):
        store = Mock()
        store.plan_taxonomy_migration.return_value = {
            "profile": "human_ai_intimacy_v1",
            "taxonomy_version": 2,
            "total_topic_count": 1,
            "scoped_topic_count": 1,
            "migratable_topic_count": 1,
            "legacy_history_summary_count": 0,
            "category_change_count": 0,
            "path_change_count": 0,
            "unresolved_count": 0,
            "category_mappings": [],
            "unresolved_items": [],
            "changes": [],
        }
        store.find_category_changes.return_value = [
            {
                "title": "已在正确 folder 但缺 profile",
                "from": "模型与平台",
                "to": "模型与平台",
                "reason": "taxonomy_profile",
                "from_path": "关注推送/示例人机互动群/模型与平台/模型.md",
                "to_path": "关注推送/示例人机互动群/模型与平台/模型.md",
            }
        ]
        config = {
            "monitor_obsidian_root": "/vault",
            "monitor_obsidian_subdir": "关注推送",
        }

        with (
            patch("scripts.organize_obsidian.load_config", return_value=config),
            patch("scripts.organize_obsidian.KnowledgeStore.from_config", return_value=store) as from_config,
            patch.object(sys, "argv", ["organize_obsidian.py", "--taxonomy-review-brief"]),
            patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            result = main()

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("# Taxonomy Migration Review Brief", output)
        self.assertIn("metadata-only backfills: 1", output)
        from_config.assert_called_once_with(config, read_only=True)
        store.plan_taxonomy_migration.assert_called_once_with("human_ai_intimacy_v1", example_limit=50)
        store.find_category_changes.assert_called_once_with()
        store.reorganize_paths.assert_not_called()

    def test_taxonomy_review_brief_cli_can_write_report_to_output_file(self):
        store = Mock()
        store.plan_taxonomy_migration.return_value = {
            "profile": "human_ai_intimacy_v1",
            "taxonomy_version": 2,
            "total_topic_count": 1,
            "scoped_topic_count": 1,
            "migratable_topic_count": 1,
            "legacy_history_summary_count": 0,
            "category_change_count": 0,
            "path_change_count": 0,
            "unresolved_count": 0,
            "category_mappings": [],
            "unresolved_items": [],
            "changes": [],
        }
        store.find_category_changes.return_value = []
        config = {
            "monitor_obsidian_root": "/vault",
            "monitor_obsidian_subdir": "关注推送",
        }

        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "Maintenance", "Taxonomy Review Brief.md")
            with (
                patch("scripts.organize_obsidian.load_config", return_value=config),
                patch("scripts.organize_obsidian.KnowledgeStore.from_config", return_value=store),
                patch.object(
                    sys,
                    "argv",
                    [
                        "organize_obsidian.py",
                        "--taxonomy-review-brief",
                        "--taxonomy-review-output",
                        output_path,
                    ],
                ),
                patch("sys.stdout", new=io.StringIO()) as stdout,
            ):
                result = main()

            self.assertEqual(result, 0)
            self.assertTrue(os.path.exists(output_path))
            with open(output_path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("# Taxonomy Migration Review Brief", text)
            self.assertTrue(text.endswith("\n"))
            self.assertIn(f"Taxonomy review written: {output_path}", stdout.getvalue())

    def test_format_knowledge_audit_report_is_shareable_markdown(self):
        lines = format_knowledge_audit_report({
            "total_topics": 12,
            "relation_edge_count": 3,
            "relation_counts": {"related": 2, "contradicts": 1},
            "relation_examples": [
                {
                    "relation": "related",
                    "source_title": "示例工具额度观察",
                    "source_path": "关注推送/示例人机互动群/模型与平台/示例工具额度观察.md",
                    "target_title": "示例编码工具使用经验",
                    "target_path": "关注推送/示例人机互动群/模型与平台/示例编码工具使用经验.md",
                }
            ],
            "duplicate_group_count": 1,
            "duplicate_examples": [
                {
                    "primary": "Example Model 2.0 发布传闻",
                    "merged": ["Example Model 2.0 今天发布?"],
                }
            ],
            "taxonomy": {
                "profile": "human_ai_intimacy_v1",
                "scoped_topic_count": 10,
                "assignment_source_counts": {"stored": 7, "legacy_name": 3},
                "unresolved_count": 2,
                "category_change_count": 8,
                "path_change_count": 8,
            },
            "category_change_count": 5,
        })

        text = "\n".join(lines)
        self.assertIn("# Knowledge Relation Audit", text)
        self.assertIn("relation edges: 3", text)
        self.assertIn("related: 2", text)
        self.assertIn("[[关注推送/示例人机互动群/模型与平台/示例工具额度观察|示例工具额度观察]]", text)
        self.assertIn("duplicate clusters: 1", text)
        self.assertIn("unresolved 待归类: 2", text)
        self.assertIn("assignment sources: stored=7, explicit=0, legacy_name=3", text)

    def test_knowledge_audit_cli_reads_store_without_reorganizing(self):
        store = Mock()
        store.knowledge_audit.return_value = {
            "total_topics": 2,
            "relation_edge_count": 1,
            "relation_counts": {"related": 1},
            "relation_examples": [],
            "duplicate_group_count": 0,
            "duplicate_examples": [],
            "taxonomy": {
                "profile": "human_ai_intimacy_v1",
                "scoped_topic_count": 2,
                "unresolved_count": 0,
                "category_change_count": 0,
                "path_change_count": 0,
            },
            "category_change_count": 0,
        }
        config = {
            "monitor_obsidian_root": "/vault",
            "monitor_obsidian_subdir": "关注推送",
        }

        with (
            patch("scripts.organize_obsidian.load_config", return_value=config),
            patch("scripts.organize_obsidian.KnowledgeStore.from_config", return_value=store) as from_config,
            patch.object(sys, "argv", ["organize_obsidian.py", "--knowledge-audit"]),
            patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            result = main()

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("# Knowledge Relation Audit", output)
        self.assertIn("topics: 2", output)
        from_config.assert_called_once_with(config, read_only=True)
        store.knowledge_audit.assert_called_once_with(
            taxonomy_profile="human_ai_intimacy_v1",
            duplicate_threshold=85,
        )
        store.reorganize_paths.assert_not_called()

    def test_knowledge_audit_cli_can_write_report_to_output_file(self):
        store = Mock()
        store.knowledge_audit.return_value = {
            "total_topics": 2,
            "relation_edge_count": 1,
            "relation_counts": {"related": 1},
            "relation_examples": [],
            "duplicate_group_count": 0,
            "duplicate_examples": [],
            "taxonomy": {
                "profile": "human_ai_intimacy_v1",
                "scoped_topic_count": 2,
                "unresolved_count": 0,
                "category_change_count": 0,
                "path_change_count": 0,
            },
            "category_change_count": 0,
        }
        config = {
            "monitor_obsidian_root": "/vault",
            "monitor_obsidian_subdir": "关注推送",
        }

        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "Maintenance", "Knowledge Relation Audit.md")
            with (
                patch("scripts.organize_obsidian.load_config", return_value=config),
                patch("scripts.organize_obsidian.KnowledgeStore.from_config", return_value=store),
                patch.object(
                    sys,
                    "argv",
                    [
                        "organize_obsidian.py",
                        "--knowledge-audit",
                        "--knowledge-audit-output",
                        output_path,
                    ],
                ),
                patch("sys.stdout", new=io.StringIO()) as stdout,
            ):
                result = main()

            self.assertEqual(result, 0)
            self.assertTrue(os.path.exists(output_path))
            with open(output_path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("# Knowledge Relation Audit", text)
            self.assertTrue(text.endswith("\n"))
            self.assertIn(f"Knowledge audit written: {output_path}", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
