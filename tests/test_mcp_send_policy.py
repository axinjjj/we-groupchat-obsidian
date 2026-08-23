import unittest

from core.mcp_send_confirmation import SendConfirmationStore
from core.mcp_send_policy import check_send_policy


class McpSendPolicyTests(unittest.TestCase):
    def test_disabled_mode_blocks_send(self):
        decision = check_send_policy(
            {"mcp_send_mode": "disabled"},
            text="hello",
            chat_name="Example Group",
        )

        self.assertEqual(decision["action"], "blocked")
        self.assertIn("disabled", decision["reason"])

    def test_non_disabled_modes_reject_blank_target(self):
        decision = check_send_policy(
            {"mcp_send_mode": "dry_run"},
            text="hello",
            chat_name="",
        )

        self.assertEqual(decision["action"], "blocked")
        self.assertIn("target", decision["reason"])

    def test_dry_run_reports_without_sending(self):
        decision = check_send_policy(
            {"mcp_send_mode": "dry_run"},
            text="hello",
            chat_name="Example Group",
        )

        self.assertEqual(decision["action"], "dry_run")
        self.assertEqual(decision["target"], "Example Group")

    def test_allowlist_uses_stable_username(self):
        decision = check_send_policy(
            {
                "mcp_send_mode": "allowlist",
                "mcp_send_allowlist": ["room@chatroom"],
            },
            text="hello",
            chat_name="Example Group",
            resolve_username=lambda target: "room@chatroom" if target == "Example Group" else None,
        )

        self.assertEqual(decision["action"], "send")
        self.assertEqual(decision["username"], "room@chatroom")

    def test_allowlist_blocks_unlisted_username(self):
        decision = check_send_policy(
            {
                "mcp_send_mode": "allowlist",
                "mcp_send_allowlist": ["safe-room@chatroom"],
            },
            text="hello",
            chat_name="Example Group",
            resolve_username=lambda _target: "room@chatroom",
        )

        self.assertEqual(decision["action"], "blocked")
        self.assertIn("allowlist", decision["reason"])

    def test_enabled_mode_allows_named_target(self):
        decision = check_send_policy(
            {"mcp_send_mode": "enabled"},
            text="hello",
            chat_name="Example Group",
        )

        self.assertEqual(decision["action"], "send")
        self.assertEqual(decision["target"], "Example Group")

    def test_prepare_returns_nonce_for_real_send(self):
        store = SendConfirmationStore(now_func=lambda: 1000, nonce_func=lambda: "nonce-1")

        prepared = store.prepare(
            {"mcp_send_mode": "allowlist", "mcp_send_allowlist": ["room@chatroom"]},
            text="hello",
            chat_name="Example Group",
            resolve_username=lambda _target: "room@chatroom",
        )

        self.assertEqual(prepared["action"], "confirm_required")
        self.assertEqual(prepared["nonce"], "nonce-1")
        self.assertEqual(prepared["target"], "Example Group")
        self.assertEqual(prepared["text_preview"], "hello")
        self.assertEqual(prepared["expires_at"], 1120)

    def test_confirm_sends_once_when_nonce_target_and_text_match(self):
        store = SendConfirmationStore(now_func=lambda: 1000, nonce_func=lambda: "nonce-1")
        store.prepare(
            {"mcp_send_mode": "enabled"},
            text="hello",
            chat_name="Example Group",
        )
        calls = []

        result = store.confirm(
            "nonce-1",
            text="hello",
            chat_name="Example Group",
            config={"mcp_send_mode": "enabled"},
            send_func=lambda text, target: calls.append((text, target)) or (True, "sent"),
        )

        self.assertEqual(result["action"], "sent")
        self.assertEqual(calls, [("hello", "Example Group")])
        retry = store.confirm(
            "nonce-1",
            text="hello",
            chat_name="Example Group",
            config={"mcp_send_mode": "enabled"},
            send_func=lambda *_: (True, "sent"),
        )
        self.assertEqual(retry["action"], "blocked")

    def test_confirm_blocks_wrong_or_expired_nonce_without_sending(self):
        now = [1000]
        store = SendConfirmationStore(now_func=lambda: now[0], nonce_func=lambda: "nonce-1")
        store.prepare({"mcp_send_mode": "enabled"}, text="hello", chat_name="Example Group")
        calls = []

        wrong = store.confirm(
            "wrong",
            text="hello",
            chat_name="Example Group",
            config={"mcp_send_mode": "enabled"},
            send_func=lambda text, target: calls.append((text, target)) or (True, "sent"),
        )
        now[0] = 1201
        expired = store.confirm(
            "nonce-1",
            text="hello",
            chat_name="Example Group",
            config={"mcp_send_mode": "enabled"},
            send_func=lambda text, target: calls.append((text, target)) or (True, "sent"),
        )

        self.assertEqual(wrong["action"], "blocked")
        self.assertEqual(expired["action"], "blocked")
        self.assertEqual(calls, [])

    def test_confirm_blocks_changed_target_or_text_without_sending(self):
        store = SendConfirmationStore(now_func=lambda: 1000, nonce_func=lambda: "nonce-1")
        store.prepare({"mcp_send_mode": "enabled"}, text="hello", chat_name="Example Group")
        calls = []

        result = store.confirm(
            "nonce-1",
            text="changed",
            chat_name="Example Group",
            config={"mcp_send_mode": "enabled"},
            send_func=lambda text, target: calls.append((text, target)) or (True, "sent"),
        )

        self.assertEqual(result["action"], "blocked")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
