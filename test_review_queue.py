import json
import os
import io
import tempfile
import unittest
from contextlib import redirect_stdout

from core.review_queue import ReviewQueue, suggested_action_for_item
from scripts import review_queue as review_queue_script


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_or_reuse_writes_derived_item_with_stable_id(self):
        item = self.queue.create_or_reuse(
            {
                "source_chat": "示例技术群",
                "window_start": "2026-06-17 10:00",
                "window_end": "2026-06-17 10:05",
                "title": "Example Toolkit patch",
                "summary": "示例成员分享了 example-toolkit.zip，可用于评估测试部署。",
                "knowledge_topic_id": 7,
                "knowledge_event_id": 9,
                "obsidian_path": "微信群聊/关注推送/示例技术群/技术方法/Example Toolkit.md",
                "resources": {
                    "files": [
                        {
                            "name": "example-toolkit.zip",
                            "month": "2026-06",
                            "month_dir": "/private/wechat/msg/file/2026-06",
                            "sender": "成员",
                            "time": "2026-06-17 10:02",
                        }
                    ],
                    "links": ["https://github.com/example/example-toolkit"],
                },
                "message_hash": "abc123",
            }
        )
        reused = self.queue.create_or_reuse(dict(item))

        self.assertEqual(item["schema_version"], "review_queue.v1")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["priority"], "P2")
        self.assertEqual(item["suggested_action"], "import_resource")
        self.assertEqual(item["id"], reused["id"])
        self.assertEqual(self.queue.pending_count(), 1)

        with open(self.queue.pending_path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("[00:", raw)
        self.assertNotIn("<msg>", raw)
        saved = json.loads(raw)
        self.assertEqual(saved["summary"], "示例成员分享了 example-toolkit.zip，可用于评估测试部署。")
        self.assertEqual(saved["resources"]["files"][0]["month_dir"], "/private/wechat/msg/file/2026-06")

    def test_mark_updates_status_without_creating_new_pending_item(self):
        item = self.queue.create_or_reuse(
            {
                "source_chat": "Example General Chat",
                "title": "设计讨论",
                "summary": "一条偏哲学的设计讨论。",
                "message_hash": "design-only",
            }
        )

        marked = self.queue.mark(item["id"], "reviewed")

        self.assertEqual(marked["status"], "reviewed")
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(self.queue.get(item["id"])["status"], "reviewed")

    def test_dedup_uses_message_hash_and_resources_not_title(self):
        first = self.queue.create_or_reuse(
            {
                "source_chat": "示例技术群",
                "title": "第一次标题",
                "summary": "同一个资源。",
                "resources": {"files": [{"name": "example-toolkit.zip"}], "links": []},
                "message_hash": "same-message",
            }
        )
        second = self.queue.create_or_reuse(
            {
                "source_chat": "示例技术群",
                "title": "AI 改写后的标题",
                "summary": "同一个资源。",
                "resources": {"files": [{"name": "example-toolkit.zip"}], "links": []},
                "message_hash": "same-message",
            }
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.queue.pending_count(), 1)

    def test_resource_lead_defaults_to_follow_up_action_and_stable_lead_id(self):
        first = self.queue.create_or_reuse(
            {
                "source_chat": "示例技术群",
                "title": "示例互动设计资源线索",
                "summary": "示例作者提到一份测试资源，其他示例成员请求一份，但消息窗口里还没有文件或链接。",
                "resource_lead": True,
                "resource_status": "mentioned_private",
                "lead_key": "interaction-skills-private-share",
                "message_hash": "first-window",
            }
        )
        second = self.queue.create_or_reuse(
            {
                "source_chat": "示例技术群",
                "title": "其他示例成员继续索要资源",
                "summary": "同一条资源线索继续被索要，artifact 仍未到手。",
                "resource_lead": True,
                "resource_status": "mentioned_private",
                "lead_key": "interaction-skills-private-share",
                "message_hash": "second-window",
            }
        )

        self.assertEqual(first["priority"], "P2")
        self.assertEqual(first["suggested_action"], "follow_up_resource")
        self.assertTrue(first["resource_lead"])
        self.assertEqual(first["resource_status"], "mentioned_private")
        self.assertEqual(first["lead_key"], "interaction-skills-private-share")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.queue.pending_count(), 1)

    def test_wechat_forwarded_record_shell_url_is_follow_up_not_link(self):
        shell_url = (
            "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate"
            "?t=page/favorite_record__w_unsupport"
        )

        item = self.queue.build_item(
            {
                "source_chat": "示例技术群",
                "title": "示例教程合并记录",
                "summary": "示例成员转发了收藏里的示例合并记录。",
                "resources": {"files": [], "links": [shell_url]},
                "resource_status": "linked",
                "message_hash": "wechat-record-shell",
            }
        )

        self.assertEqual(item["resources"]["links"], [])
        self.assertTrue(item["resource_lead"])
        self.assertEqual(item["resource_status"], "mentioned_private")
        self.assertEqual(item["suggested_action"], "follow_up_resource")

    def test_priority_and_action_defaults_from_resources_and_risk_keywords(self):
        p1 = self.queue.build_item(
            {
                "title": "VPS token 泄漏风险",
                "summary": "需要检查 account key 和 deploy 配置。",
                "message_hash": "risk",
            }
        )
        p2 = self.queue.build_item(
            {
                "title": "source package",
                "summary": "一个 zip 包。",
                "resources": {"files": [{"name": "patch.zip"}], "links": []},
                "message_hash": "file",
            }
        )
        p3 = self.queue.build_item(
            {
                "title": "AI伴侣交互哲学讨论",
                "summary": "只是设计和关系连续性的讨论。",
                "message_hash": "design",
            }
        )

        self.assertEqual(p1["priority"], "P1")
        self.assertEqual(p1["suggested_action"], "review_risk")
        self.assertEqual(p2["priority"], "P2")
        self.assertEqual(p2["suggested_action"], "import_resource")
        self.assertEqual(p3["priority"], "P3")
        self.assertEqual(p3["suggested_action"], "archive_reference")
        self.assertEqual(suggested_action_for_item(p2), "import_resource")

    def test_protected_subject_without_risk_signal_is_not_p1(self):
        cases = [
            ("VPS deployment notes", "记录正常部署步骤。"),
            ("account login instructions", "正常账号登录教程。"),
            ("token usage documentation", "说明 token 的常规用法。"),
            ("账号登录教程", "这是正常配置说明。"),
        ]

        for index, (title, summary) in enumerate(cases):
            with self.subTest(title=title):
                item = self.queue.build_item(
                    {"title": title, "summary": summary, "message_hash": f"safe-{index}"}
                )
                self.assertNotEqual(item["priority"], "P1")
                self.assertNotEqual(item["suggested_action"], "review_risk")

    def test_protected_subject_with_explicit_risk_is_p1(self):
        cases = [
            ("VPS token leaked in production", "Credential was exposed publicly."),
            ("account login appears compromised", "Unauthorized access is suspected."),
            ("API key accidentally pasted publicly", "Secret needs revocation."),
            ("账号疑似被盗", "密钥意外泄漏，需要立即撤销。"),
        ]

        for index, (title, summary) in enumerate(cases):
            with self.subTest(title=title):
                item = self.queue.build_item(
                    {"title": title, "summary": summary, "message_hash": f"risk-{index}"}
                )
                self.assertEqual(item["priority"], "P1")
                self.assertEqual(item["suggested_action"], "review_risk")

    def test_explicit_trusted_review_risk_actionability_is_p1(self):
        item = self.queue.build_item(
            {
                "title": "locally verified incident",
                "summary": "Trusted local code already classified this incident.",
                "actionability": "review_risk",
                "message_hash": "trusted-risk",
            }
        )

        self.assertEqual(item["priority"], "P1")
        self.assertEqual(item["suggested_action"], "review_risk")
        self.assertTrue(item["queue_worthy"])

    def test_ai_intimacy_play_topics_are_p2_read_notes(self):
        item = self.queue.build_item(
            {
                "source_chat": "示例人机互动群",
                "title": "示例人机互动玩法：语音模式与状态反馈",
                "summary": "示例成员讨论了语音模式、状态反馈和互动玩法配置，没有附链接或文件。",
                "message_hash": "ai-intimacy-play",
            }
        )

        self.assertEqual(item["priority"], "P2")
        self.assertEqual(item["suggested_action"], "read_note")

    def test_high_signal_read_note_is_not_queue_worthy(self):
        item = self.queue.build_item(
            {
                "source_chat": "示例人机互动群",
                "title": "示例人机互动玩法：语音模式与状态反馈",
                "summary": "示例成员讨论了语音模式、状态反馈和互动玩法配置，没有附链接或文件。",
                "message_hash": "ai-intimacy-play",
            }
        )

        self.assertEqual(item["priority"], "P2")
        self.assertEqual(item["suggested_action"], "read_note")
        self.assertEqual(item["signal_level"], "high")
        self.assertEqual(item["actionability"], "none")
        self.assertFalse(item["queue_worthy"])

    def test_actionable_items_are_queue_worthy(self):
        resource_lead = self.queue.build_item(
            {
                "source_chat": "示例人机互动群",
                "title": "示例互动设计资源线索",
                "summary": "示例作者提到一份测试资源，其他示例成员请求一份。",
                "resource_lead": True,
                "resource_status": "mentioned_private",
                "lead_key": "interaction-skills-private-share",
                "message_hash": "resource-lead",
            }
        )
        risk = self.queue.build_item(
            {
                "title": "VPS token 泄漏风险",
                "summary": "需要检查 account key 和 deploy 配置。",
                "message_hash": "risk",
            }
        )

        self.assertEqual(resource_lead["actionability"], "follow_up_resource")
        self.assertTrue(resource_lead["queue_worthy"])
        self.assertEqual(risk["actionability"], "review_risk")
        self.assertTrue(risk["queue_worthy"])

    def test_raw_queue_worthy_input_is_rederived(self):
        read_note = self.queue.build_item(
            {
                "source_chat": "示例人机互动群",
                "title": "示例人机互动玩法：语音模式与状态反馈",
                "summary": "示例成员讨论了语音模式、状态反馈和互动玩法配置，没有附链接或文件。",
                "message_hash": "raw-queue-worthy-read-note",
                "queue_worthy": True,
            }
        )
        resource_lead = self.queue.build_item(
            {
                "source_chat": "示例人机互动群",
                "title": "示例互动设计资源线索",
                "summary": "示例作者提到一份测试资源，其他示例成员请求一份。",
                "resource_lead": True,
                "resource_status": "mentioned_private",
                "lead_key": "interaction-skills-private-share",
                "message_hash": "raw-queue-worthy-resource-lead",
                "queue_worthy": False,
            }
        )

        self.assertEqual(read_note["suggested_action"], "read_note")
        self.assertEqual(read_note["actionability"], "none")
        self.assertFalse(read_note["queue_worthy"])
        self.assertEqual(resource_lead["actionability"], "follow_up_resource")
        self.assertTrue(resource_lead["queue_worthy"])

    def test_audit_reports_legacy_read_note_without_mutating_queue(self):
        read_note = self.queue.create_or_reuse(
            {
                "source_chat": "示例人机互动群",
                "title": "示例人机互动功能",
                "summary": "高信号但无下一步动作。",
                "priority": "P2",
                "suggested_action": "read_note",
                "message_hash": "read-note-legacy",
            }
        )
        resource = self.queue.create_or_reuse(
            {
                "source_chat": "示例人机互动群",
                "title": "示例互动设计资源线索",
                "summary": "示例作者表示可以私发测试资源。",
                "resource_lead": True,
                "resource_status": "mentioned_private",
                "lead_key": "interaction-skills-private-share",
                "message_hash": "resource",
            }
        )

        audit = self.queue.audit()

        self.assertEqual(audit["total_pending"], 2)
        self.assertEqual(audit["read_note_legacy_items"]["count"], 1)
        self.assertEqual(audit["actionable_items"]["count"], 1)
        self.assertEqual(self.queue.pending_count(), 2)
        self.assertEqual(self.queue.get(read_note["id"])["status"], "pending")
        self.assertEqual(self.queue.get(resource["id"])["status"], "pending")

    def test_audit_previews_priority_reclassification_without_mutating_queue(self):
        self.queue.create_or_reuse(
            {
                "title": "VPS deployment notes",
                "summary": "记录正常部署步骤。",
                "priority": "P1",
                "suggested_action": "review_risk",
                "actionability": "review_risk",
                "message_hash": "legacy-vps",
            }
        )
        self.queue.create_or_reuse(
            {
                "title": "account login guide",
                "summary": "正常账号登录教程。",
                "priority": "P1",
                "suggested_action": "review_risk",
                "actionability": "review_risk",
                "message_hash": "legacy-account",
            }
        )
        with open(self.queue.pending_path, "rb") as handle:
            before = handle.read()
        before_mtime = os.path.getmtime(self.queue.pending_path)

        audit = self.queue.audit()

        self.assertEqual(audit["priority_preview"]["current_counts"], {"P1": 2})
        self.assertEqual(audit["priority_preview"]["derived_counts"], {"P2": 1, "P3": 1})
        self.assertEqual(audit["priority_preview"]["would_change_count"], 2)
        self.assertEqual(
            audit["priority_preview"]["transitions"],
            {"P1->P2": 1, "P1->P3": 1},
        )
        self.assertEqual(audit["risk_rule_preview"]["current_pending_p1_count"], 2)
        self.assertEqual(audit["risk_rule_preview"]["remains_p1_count"], 0)
        self.assertEqual(audit["risk_rule_preview"]["would_downgrade_count"], 2)
        self.assertEqual(
            audit["risk_rule_preview"]["resulting_actionability_counts"],
            {"none": 2},
        )
        self.assertEqual(len(audit["risk_rule_preview"]["examples"]), 2)
        self.assertNotIn("title", audit["risk_rule_preview"]["examples"][0])
        self.assertNotIn("VPS deployment notes", repr(audit["priority_preview"]))
        with open(self.queue.pending_path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(os.path.getmtime(self.queue.pending_path), before_mtime)

        sensitive_audit = self.queue.audit(sensitive=True)
        self.assertIn("title", sensitive_audit["risk_rule_preview"]["examples"][0])

    def test_cleanup_legacy_digest_only_dry_run_does_not_mutate_queue(self):
        read_note = self.queue.create_or_reuse(
            {
                "source_chat": "示例人机互动群",
                "title": "示例人机互动功能",
                "summary": "高信号但无下一步动作。",
                "priority": "P2",
                "suggested_action": "read_note",
                "message_hash": "read-note-cleanup",
            }
        )
        resource = self.queue.create_or_reuse(
            {
                "source_chat": "示例人机互动群",
                "title": "示例互动设计资源线索",
                "summary": "示例作者表示可以私发测试资源。",
                "resource_lead": True,
                "resource_status": "mentioned_private",
                "lead_key": "interaction-skills-private-share",
                "message_hash": "resource-cleanup",
            }
        )

        plan = self.queue.cleanup_legacy_digest_only(dry_run=True)

        self.assertFalse(plan["applied"])
        self.assertEqual(plan["matched_count"], 1)
        self.assertEqual(plan["selected_count"], 1)
        self.assertEqual(plan["updated_count"], 0)
        self.assertEqual(plan["status"], "reviewed")
        self.assertEqual(plan["items"][0]["id"], read_note["id"])
        self.assertEqual(self.queue.pending_count(), 2)
        self.assertEqual(self.queue.get(read_note["id"])["status"], "pending")
        self.assertEqual(self.queue.get(resource["id"])["status"], "pending")

    def test_cleanup_legacy_digest_only_apply_marks_only_digest_debt(self):
        read_note = self.queue.create_or_reuse(
            {
                "source_chat": "示例人机互动群",
                "title": "示例人机互动功能",
                "summary": "高信号但无下一步动作。",
                "priority": "P2",
                "suggested_action": "read_note",
                "message_hash": "read-note-apply",
            }
        )
        resource = self.queue.create_or_reuse(
            {
                "source_chat": "示例人机互动群",
                "title": "示例互动设计资源线索",
                "summary": "示例作者表示可以私发测试资源。",
                "resource_lead": True,
                "resource_status": "mentioned_private",
                "lead_key": "interaction-skills-private-share",
                "message_hash": "resource-apply",
            }
        )

        result = self.queue.cleanup_legacy_digest_only(dry_run=False, status="reviewed")

        self.assertTrue(result["applied"])
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(self.queue.get(read_note["id"])["status"], "reviewed")
        self.assertEqual(self.queue.get(resource["id"])["status"], "pending")
        self.assertEqual(self.queue.pending_count(), 1)

    def test_cleanup_command_defaults_to_dry_run_and_requires_apply_to_mutate(self):
        read_note = self.queue.create_or_reuse(
            {
                "source_chat": "示例人机互动群",
                "title": "示例人机互动功能",
                "summary": "高信号但无下一步动作。",
                "priority": "P2",
                "suggested_action": "read_note",
                "message_hash": "read-note-cli",
            }
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = review_queue_script.main([
                "--queue-dir",
                self.queue.queue_dir,
                "cleanup",
            ])

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertFalse(output["applied"])
        self.assertEqual(output["matched_count"], 1)
        self.assertEqual(self.queue.get(read_note["id"])["status"], "pending")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = review_queue_script.main([
                "--queue-dir",
                self.queue.queue_dir,
                "cleanup",
                "--apply",
            ])

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertTrue(output["applied"])
        self.assertEqual(output["updated_count"], 1)
        self.assertEqual(self.queue.get(read_note["id"])["status"], "reviewed")


if __name__ == "__main__":
    unittest.main()
