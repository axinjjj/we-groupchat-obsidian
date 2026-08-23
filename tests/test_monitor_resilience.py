import os
import tempfile
import unittest

from core.monitor import TopicMonitor, load_state, save_state


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
        save_state({"last_checked_ts": 10}, self.state_file)
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
        self.assertNotIn("ai_next_retry_after", state)

    def test_retryable_ai_failure_sets_backoff_without_advancing_checkpoint(self):
        self.config["monitor_ai_retry_attempts"] = 0
        self.config["monitor_ai_failure_backoff_minutes"] = 10
        save_state({"last_checked_ts": 10}, self.state_file)
        calls = []

        def fail_evaluation(*_):
            calls.append(True)
            raise RuntimeError("无法连接 deepseek API 服务器，请检查网络连接")

        with self.assertRaises(RuntimeError):
            self.monitor(FakeDB([msg(11, "普通闲聊")]), fail_evaluation, now=1000).check_once()

        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 10)
        self.assertEqual(state["ai_failure_count"], 1)
        self.assertEqual(state["ai_next_retry_after"], 1600)

        result = self.monitor(FakeDB([msg(11, "普通闲聊")]), fail_evaluation, now=1001).check_once()

        self.assertEqual(result["status"], "ai_backoff")
        self.assertEqual(len(calls), 1)
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 10)

    def test_empty_ai_response_sets_backoff_without_advancing_checkpoint(self):
        self.config["monitor_ai_retry_attempts"] = 0
        save_state({"last_checked_ts": 10}, self.state_file)

        def empty_response(*_):
            raise RuntimeError("deepseek API 返回空响应，请稍后重试")

        with self.assertRaises(RuntimeError):
            self.monitor(FakeDB([msg(11, "可能值得记录的新内容")]), empty_response).check_once()

        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 10)
        self.assertEqual(state["ai_failure_count"], 1)
        self.assertGreater(state["ai_next_retry_after"], 1000)


if __name__ == "__main__":
    unittest.main()
