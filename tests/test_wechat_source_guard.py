import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.wechat_source_guard import (
    OPEN_WECHAT_ARGV,
    WeChatSourceGuard,
    atomic_write_json,
    latest_source_mtime,
    load_state,
    source_guard_status,
)
from scripts.wechat_source_guard import build_agent_plist


class Clock:
    def __init__(self, value=1000):
        self.value = float(value)

    def __call__(self):
        return self.value


class WeChatSourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.receipts = self.root / "receipts"
        self.lock = self.root / "guard.lock"
        self.clock = Clock()
        self.config = {
            "wechat_source_guard_enabled": True,
            "wechat_source_guard_grace_seconds": 300,
            "wechat_source_guard_restart_budget": 3,
            "wechat_source_guard_restart_window_seconds": 1800,
            "wechat_source_guard_backoff_base_seconds": 300,
            "wechat_source_guard_notification_cooldown_seconds": 3600,
            "wechat_source_guard_pause_until": "",
            "wechat_source_guard_stale_seconds": 7200,
            "db_dir": str(self.root / "db"),
        }
        self.launches = []
        self.notifications = []

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, *, probe=lambda: False, launch_code=0, source_mtime=None, launch_runner=None):
        def runner(argv, **kwargs):
            self.launches.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, launch_code, stdout="", stderr="failed" if launch_code else "")

        return WeChatSourceGuard(
            self.config,
            state_file=self.state,
            receipt_root=self.receipts,
            lock_file=self.lock,
            now_func=self.clock,
            process_probe=probe,
            launch_runner=launch_runner or runner,
            notifier=lambda title, message: self.notifications.append((title, message)),
            source_mtime_func=lambda _path: source_mtime,
        )

    def write_state(self, **values):
        state = load_state(self.state)
        state.update(values)
        atomic_write_json(self.state, state)

    def test_source_freshness_uses_message_database_directory_only(self):
        db_root = self.root / "db"
        message_dir = db_root / "message"
        unrelated_dir = db_root / "hardlink"
        message_dir.mkdir(parents=True)
        unrelated_dir.mkdir(parents=True)
        source_db = message_dir / "message_0.db"
        unrelated_db = unrelated_dir / "unrelated.db"
        source_db.write_bytes(b"source")
        unrelated_db.write_bytes(b"unrelated")
        os.utime(source_db, (1000, 1000))
        os.utime(unrelated_db, (2000, 2000))

        with patch(
            "core.wechat_source_guard.get_cached_keys",
            return_value={"message/message_0.db": {"enc_key": "fixture"}},
        ):
            self.assertEqual(latest_source_mtime(db_root), 1000)

    def test_disabled_never_launches(self):
        self.config["wechat_source_guard_enabled"] = False
        result = self.guard().check()
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(self.launches, [])

    def test_pause_blocks_launch(self):
        self.config["wechat_source_guard_pause_until"] = "indefinite"
        result = self.guard().check()
        self.assertEqual(result["state"], "paused")
        self.assertEqual(self.launches, [])

    def test_expired_pause_resumes_normal_check(self):
        self.config["wechat_source_guard_pause_until"] = "1970-01-01T00:01:00Z"
        result = self.guard(probe=lambda: True, source_mtime=self.clock()).check()
        self.assertEqual(result["state"], "healthy")

    def test_first_missing_only_starts_grace(self):
        result = self.guard().check()
        self.assertEqual(result["state"], "missing_grace")
        self.assertEqual(result["missing_since"], self.clock())
        self.assertEqual(self.launches, [])

    def test_grace_not_elapsed_never_launches(self):
        self.write_state(state="missing_grace", missing_since=self.clock() - 299)
        result = self.guard().check()
        self.assertEqual(result["last_result"], "missing_grace")
        self.assertEqual(self.launches, [])

    def test_grace_elapsed_launches_once_with_structured_argv(self):
        self.write_state(state="missing_grace", missing_since=self.clock() - 301)
        result = self.guard().check()
        self.assertEqual(result["state"], "restart_backoff")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.launches[0][0], OPEN_WECHAT_ARGV)
        self.assertEqual(len(list(self.receipts.glob("*.json"))), 1)

    def test_process_lookup_unknown_never_launches(self):
        result = self.guard(probe=lambda: None).check()
        self.assertEqual(result["state"], "process_lookup_unknown")
        self.assertEqual(self.launches, [])

    def test_restart_budget_degrades_without_launching(self):
        self.write_state(
            state="missing_grace",
            missing_since=1,
            restart_attempt_timestamps=[900, 950, 990],
        )
        result = self.guard().check()
        self.assertEqual(result["state"], "degraded")
        self.assertEqual(result["last_result"], "restart_budget_exhausted")
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.notifications), 1)

    def test_budget_window_expiry_allows_recovery(self):
        self.config["wechat_source_guard_restart_window_seconds"] = 60
        self.write_state(
            state="missing_grace",
            missing_since=1,
            restart_attempt_timestamps=[100, 200, 300],
        )
        result = self.guard().check()
        self.assertEqual(result["last_result"], "launch_requested")
        self.assertEqual(result["restart_attempts_in_window"], 1)

    def test_backoff_blocks_another_launch(self):
        self.write_state(state="restart_backoff", missing_since=1, backoff_until=self.clock() + 1)
        result = self.guard().check()
        self.assertEqual(result["state"], "restart_backoff")
        self.assertEqual(self.launches, [])

    def test_launch_failure_enters_retry_then_degraded(self):
        self.write_state(state="missing_grace", missing_since=1)
        first = self.guard(launch_code=7).check()
        self.assertEqual(first["state"], "restart_backoff")
        self.assertEqual(first["last_error_code"], "launch_exit_7")
        self.clock.value = first["backoff_until"] + 1
        second = self.guard(launch_code=7).check()
        self.clock.value = second["backoff_until"] + 1
        third = self.guard(launch_code=7).check()
        self.assertEqual(third["state"], "degraded")

    def test_running_clears_missing_and_attempt_state(self):
        self.write_state(
            state="restart_backoff",
            missing_since=900,
            backoff_until=2000,
            restart_attempt_timestamps=[950],
        )
        result = self.guard(probe=lambda: True, source_mtime=self.clock()).check()
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["missing_since"], 0)
        self.assertEqual(result["restart_attempts_in_window"], 0)
        self.assertEqual(result["backoff_until"], 0)

    def test_stale_source_warns_without_restarting(self):
        result = self.guard(probe=lambda: True, source_mtime=self.clock() - 7201).check()
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["last_result"], "stale_source")
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.notifications), 1)

    def test_notification_is_throttled(self):
        self.guard(probe=lambda: True, source_mtime=self.clock() - 7201).check()
        self.clock.value += 60
        self.guard(probe=lambda: True, source_mtime=self.clock() - 7201).check()
        self.assertEqual(len(self.notifications), 1)

    def test_stale_notification_occurs_once_per_stale_episode(self):
        stale_mtime = self.clock() - 7201
        self.guard(probe=lambda: True, source_mtime=stale_mtime).check()
        self.clock.value += 7200
        still_stale = self.guard(probe=lambda: True, source_mtime=stale_mtime).check()
        self.assertEqual(still_stale["last_result"], "stale_source")
        self.assertEqual(len(self.notifications), 1)

        self.clock.value += 1
        fresh = self.guard(probe=lambda: True, source_mtime=self.clock()).check()
        self.assertEqual(fresh["source_freshness"], "fresh")
        self.clock.value += 1
        self.guard(probe=lambda: True, source_mtime=self.clock() - 7201).check()
        self.assertEqual(len(self.notifications), 2)

    def test_concurrent_one_shots_do_not_duplicate_launch(self):
        self.write_state(state="missing_grace", missing_since=1)
        entered = threading.Event()
        release = threading.Event()

        def slow_runner(argv, **kwargs):
            self.launches.append((argv, kwargs))
            entered.set()
            release.wait(timeout=2)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        first_result = []
        thread = threading.Thread(target=lambda: first_result.append(self.guard(launch_runner=slow_runner).check()))
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        second = self.guard(launch_runner=slow_runner).check()
        release.set()
        thread.join(timeout=2)
        self.assertEqual(second["last_result"], "busy")
        self.assertEqual(len(self.launches), 1)

    def test_state_and_receipts_are_private(self):
        self.write_state(state="missing_grace", missing_since=1)
        self.guard().check()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        receipt = next(self.receipts.glob("*.json"))
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        text = receipt.read_text(encoding="utf-8")
        self.assertNotIn("chatroom", text)
        self.assertNotIn("wxid", text)

    def test_status_reports_effective_disabled_and_pause_states(self):
        self.write_state(state="healthy", last_result="healthy")
        disabled = source_guard_status(
            {**self.config, "wechat_source_guard_enabled": False},
            state_file=self.state,
            now=self.clock(),
        )
        self.assertEqual(disabled["state"], "disabled")
        self.assertEqual(disabled["persisted_state"], "healthy")

        paused = source_guard_status(
            {**self.config, "wechat_source_guard_pause_until": "indefinite"},
            state_file=self.state,
            now=self.clock(),
        )
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["last_result"], "paused")


class SourceGuardLaunchAgentTests(unittest.TestCase):
    def test_plist_is_one_shot_start_interval_without_keepalive(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            executable = (
                project
                / "dist"
                / "WeGroupchatObsidian.app"
                / "Contents"
                / "MacOS"
                / "WeGroupchatObsidian"
            )
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            payload = build_agent_plist(
                {"wechat_source_guard_interval_seconds": 420},
                project_dir=project,
            )
        self.assertEqual(payload["StartInterval"], 420)
        self.assertNotIn("KeepAlive", payload)
        self.assertEqual(
            payload["ProgramArguments"],
            [str(executable.resolve()), "--source-guard-run"],
        )

    def test_plist_falls_back_to_source_python_without_app_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            payload = build_agent_plist({}, project_dir=project)

        self.assertTrue(payload["ProgramArguments"][0].endswith("/.venv/bin/python"))
        self.assertTrue(
            payload["ProgramArguments"][1].endswith(
                "/scripts/wechat_source_guard_agent.py"
            )
        )

    def test_plist_preserves_explicit_data_dir_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            data_dir = str(project / "private-data")
            with patch.dict(
                os.environ,
                {"WE_GROUPCHAT_OBSIDIAN_DATA_DIR": data_dir},
                clear=False,
            ):
                payload = build_agent_plist({}, project_dir=project)
        self.assertEqual(
            payload["EnvironmentVariables"],
            {"WE_GROUPCHAT_OBSIDIAN_DATA_DIR": data_dir},
        )


if __name__ == "__main__":
    unittest.main()
