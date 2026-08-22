import threading
import unittest
from unittest.mock import Mock, patch

from app import WeGroupchatObsidianApp


class AppGoogleDriveSyncTests(unittest.TestCase):
    def app(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {
            "google_drive_file_sync_enabled": False,
            "google_drive_file_sync_paused": False,
            "google_drive_file_sync_selected_chats": [],
            "google_drive_file_sync_interval_seconds": 300,
        }
        app.db = object()
        app._drive_sync_timer = None
        app._drive_sync_lock = threading.Lock()
        return app

    def test_enable_initializes_cursor_only_and_keeps_auth_selection_upload_separate(self):
        app = self.app()
        service = Mock()
        app._drive_sync_service = Mock(return_value=service)
        app._configure_drive_sync_timer = Mock()
        app._rebuild_drive_sync_menu = Mock()

        with patch("app.save_config") as save, patch("app._notify"):
            app._toggle_drive_sync(None)

        self.assertTrue(app.config["google_drive_file_sync_enabled"])
        service.initialize_selected_chat_cursors.assert_called_once_with()
        service.run.assert_not_called()
        save.assert_called_once_with(app.config)

    def test_timer_exists_only_while_enabled_and_unpaused(self):
        app = self.app()
        fake_timer = Mock()
        with patch("app.rumps.Timer", return_value=fake_timer) as timer:
            app._configure_drive_sync_timer()
            timer.assert_not_called()

            app.config["google_drive_file_sync_enabled"] = True
            app.config["google_drive_file_sync_paused"] = True
            app._configure_drive_sync_timer()
            timer.assert_not_called()

            app.config["google_drive_file_sync_paused"] = False
            app._configure_drive_sync_timer()

        timer.assert_called_once_with(app._on_drive_sync_timer, 300)
        fake_timer.start.assert_called_once_with()

    def test_background_consumer_calls_one_shot_worker_and_queues_ui_finish(self):
        app = self.app()
        service = Mock()
        service.run.return_value = {
            "state": "healthy",
            "scanned": 2,
            "queued": 1,
            "uploaded": 1,
            "shortcuts": 1,
        }
        app._drive_sync_service = Mock(return_value=service)
        app._run_on_main = Mock()
        config = dict(app.config, google_drive_file_sync_enabled=True)

        with patch("app.load_config", return_value=config):
            app._run_drive_sync_consumer(manual=False)

        app._drive_sync_service.assert_called_once_with(remote=True, config=config)
        service.run.assert_called_once_with()
        app._run_on_main.assert_called_once_with(
            app._finish_drive_sync_run,
            service.run.return_value,
            False,
        )
        self.assertFalse(app._drive_sync_lock.locked())

    def test_menu_exposes_required_manual_controls(self):
        app = self.app()
        app._drive_sync_status = Mock(return_value={
            "state": "disabled",
            "auth": "auth_required",
            "selected_chat_count": 0,
            "queue_counts": {},
            "root_state": "unknown",
        })

        menu = app._build_drive_sync_menu()

        labels = set(menu.keys())
        self.assertIn("▶️ 开启同步", labels)
        self.assertIn("🔄 立即同步一次", labels)
        self.assertIn("🎯 选择群聊...", labels)
        self.assertIn("📂 打开 Drive 根目录", labels)
        self.assertIn("🔐 重新授权...", labels)


if __name__ == "__main__":
    unittest.main()
