import os
from pathlib import Path
import tempfile
import unittest

from core.monitor import TopicMonitor, load_state, save_state
from core.monitor_state import MonitorStateStore


class FakeDB:
    def __init__(self, messages):
        self.messages = messages

    def get_messages(self, username, since_ts=0, limit=500, page_forward=False):
        messages = [m for m in self.messages if m["timestamp"] > since_ts]
        return messages[:limit] if page_forward and since_ts > 0 else messages[-limit:]

    def format_messages_for_ai(self, messages, show_group_nickname=False):
        return "\n".join(
            f"[{m['time_str']}] {m.get('sender', '')}: {m['text']}"
            for m in messages
        )


def msg(ts, text):
    return {
        "timestamp": ts,
        "time_str": f"2026-05-29 00:{ts:02d}",
        "sender": "成员",
        "text": text,
        "type": 1,
    }


class TopicMonitorResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "state.json")
        self.hits_dir = os.path.join(self.tmp.name, "hits")
        self.config = {
            "monitor_topic": "Claude Code 新功能",
            "monitor_chat_username": "chatroom",
            "monitor_chat_display_name": "示例技术群",
            "monitor_interval_minutes": 3,
            "monitor_max_messages_per_run": 2,
            "monitor_context_overlap_minutes": 0,
            "monitor_cooldown_minutes": 15,
            "monitor_ai_retry_delay_seconds": 0,
            "show_group_nickname": True,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def monitor(self, db, evaluator, now=1000):
        return TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=evaluator,
            now_func=lambda: now,
        )

    def test_retryable_ai_failure_is_retried_once_before_advancing_checkpoint(self):
        self.config["monitor_ai_retry_attempts"] = 1
        save_state({
            "last_checked_ts": 10,
            "ai_failure_count": 2,
            "ai_last_error": "legacy raw error",
            "ai_last_error_code": "ai_connection_error",
            "ai_last_error_ts": 900,
            "ai_next_retry_after": 999,
        }, self.state_file)
        calls = []

        def flaky_evaluation(*_):
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("deepseek API 请求超时，请检查网络连接后重试")
            return {"match": False, "score": 20}

        result = self.monitor(FakeDB([msg(11, "普通闲聊")]), flaky_evaluation).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(len(calls), 2)
        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 11)
        for key in (
            "ai_failure_count",
            "ai_last_error",
            "ai_last_error_code",
            "ai_last_error_ts",
            "ai_next_retry_after",
        ):
            self.assertNotIn(key, state)

    def test_retryable_ai_failure_commits_backoff_without_advancing_checkpoint(self):
        self.config["monitor_ai_retry_attempts"] = 0
        save_state({"last_checked_ts": 10, "last_topic_key": "keep-me"}, self.state_file)
        calls = []

        def fail_evaluation(*_):
            calls.append(True)
            raise RuntimeError("无法连接 deepseek API 服务器，请检查网络连接")

        with self.assertRaises(RuntimeError):
            self.monitor(FakeDB([msg(11, "普通闲聊")]), fail_evaluation, now=1000).check_once()

        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 10)
        self.assertEqual(state["last_topic_key"], "keep-me")
        self.assertEqual(state["ai_failure_count"], 1)
        self.assertEqual(state["ai_last_error_code"], "ai_connection_error")
        self.assertEqual(state["ai_last_error_ts"], 1000)
        self.assertEqual(state["ai_next_retry_after"], 1600)
        self.assertNotIn("ai_last_error", state)
        self.assertEqual(len(calls), 1)

    def test_ai_backoff_skips_provider_call_until_retry_deadline(self):
        self.config["monitor_ai_retry_attempts"] = 0
        save_state({"last_checked_ts": 10}, self.state_file)
        calls = []

        def fail_evaluation(*_):
            calls.append(True)
            raise RuntimeError("provider timeout")

        with self.assertRaisesRegex(RuntimeError, "provider timeout"):
            self.monitor(
                FakeDB([msg(11, "普通闲聊")]), fail_evaluation, now=1000
            ).check_once()

        result = self.monitor(
            FakeDB([msg(11, "普通闲聊")]),
            lambda *_: calls.append(True),
            now=1001,
        ).check_once()

        self.assertEqual(result["status"], "ai_backoff")
        self.assertEqual(result["retry_after_ts"], 1600)
        self.assertEqual(result["last_error_code"], "ai_timeout")
        self.assertEqual(len(calls), 1)

    def test_empty_ai_response_enters_backoff_without_advancing_checkpoint(self):
        self.config["monitor_ai_retry_attempts"] = 0
        save_state({"last_checked_ts": 10}, self.state_file)

        def empty_response(*_):
            raise RuntimeError("deepseek API 返回空响应，请稍后重试")

        with self.assertRaises(RuntimeError):
            self.monitor(FakeDB([msg(11, "可能值得记录的新内容")]), empty_response).check_once()

        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 10)
        self.assertEqual(state["ai_last_error_code"], "ai_empty_response")
        self.assertEqual(state["ai_next_retry_after"], 1600)

    def test_retryable_ai_failure_conflict_does_not_overwrite_newer_state(self):
        self.config["monitor_ai_retry_attempts"] = 0
        save_state({"last_checked_ts": 10}, self.state_file)
        store = MonitorStateStore(self.state_file)

        def concurrent_failure(*_):
            store.update(lambda state: state.update({
                "last_checked_ts": 77,
                "source_cursors": {
                    "logical-new": {
                        "generation_id": "generation-new",
                        "cursor_token": "[77,9]",
                    },
                },
            }))
            raise RuntimeError("provider timeout")

        result = self.monitor(
            FakeDB([msg(11, "普通闲聊")]), concurrent_failure, now=1000
        ).check_once()

        self.assertEqual(result["status"], "monitor_state_conflict")
        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 77)
        self.assertEqual(
            state["source_cursors"]["logical-new"]["cursor_token"],
            "[77,9]",
        )
        self.assertNotIn("ai_failure_count", state)
        self.assertNotIn("ai_next_retry_after", state)

    def test_corrupt_state_fails_closed_without_calling_ai_or_initializing(self):
        original = b'{"last_checked_ts":'
        Path(self.state_file).write_bytes(original)
        calls = []

        result = self.monitor(
            FakeDB([msg(11, "可能值得记录的新内容")]),
            lambda *_: calls.append(True),
        ).check_once()

        self.assertEqual(result["status"], "monitor_state_corrupt")
        self.assertEqual(calls, [])
        self.assertEqual(Path(self.state_file).read_bytes(), original)

    def test_stale_monitor_commit_returns_conflict_without_overwrite(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        store = MonitorStateStore(self.state_file)

        def concurrent_evaluation(*_):
            def mutate(state):
                state["concurrent_writer"] = True

            store.update(mutate)
            return {"match": False, "score": 20}

        result = self.monitor(
            FakeDB([msg(11, "普通闲聊")]),
            concurrent_evaluation,
        ).check_once()

        self.assertEqual(result["status"], "monitor_state_conflict")
        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 10)
        self.assertTrue(state["concurrent_writer"])


if __name__ == "__main__":
    unittest.main()
