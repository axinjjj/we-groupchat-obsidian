import threading
import unittest
from unittest.mock import Mock, patch

from app import WeGroupchatObsidianApp


class AppResourceBackupTests(unittest.TestCase):
    def make_app(self, *, resolve_files=False):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {
            "resource_backup_enabled": True,
            "resource_backup_file_resolution_enabled": resolve_files,
        }
        app.db = object()
        app._resource_backup_lock = threading.Lock()
        app._source_guard_lock = threading.Lock()
        app._run_on_main = Mock()
        return app

    def test_long_lived_consumer_skips_attachment_cache_by_default(self):
        app = self.make_app(resolve_files=False)
        capture = Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {"captured_links": 1, "captured_files": 1},
            "resolve": {"state": "skipped"},
        }
        app._resource_capture_service = Mock(return_value=capture)
        backup = Mock()
        backup.run.return_value = {"state": "sync_delegated"}

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
        ):
            app._run_resource_backup_consumer(manual=False)

        capture.run.assert_called_once_with(resolve_limit=50, resolve_files=False)
        backup.run.assert_called_once_with()
        self.assertFalse(app._resource_backup_lock.locked())

    def test_file_resolution_requires_explicit_config_opt_in(self):
        app = self.make_app(resolve_files=True)
        capture = Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {},
            "resolve": {"state": "healthy"},
        }
        app._resource_capture_service = Mock(return_value=capture)
        backup = Mock()
        backup.run.return_value = {"state": "idle"}

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
        ):
            app._run_resource_backup_consumer(manual=False)

        capture.run.assert_called_once_with(resolve_limit=50, resolve_files=True)

    def test_link_backfill_apply_never_calls_file_resolver(self):
        app = self.make_app(resolve_files=False)
        capture = Mock()
        capture.backfill_links.return_value = {
            "state": "applied",
            "source_complete": True,
            "discovered_links": 3,
            "inserted_links": 2,
        }
        capture.resolve_pending_files.side_effect = AssertionError(
            "links-only entry must not touch attachment cache"
        )
        app._resource_capture_service = Mock(return_value=capture)
        backup = Mock()
        backup.run.return_value = {"state": "sync_delegated"}

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
        ):
            app._apply_link_backfill(0)

        capture.backfill_links.assert_called_once_with(0, apply=True)
        capture.resolve_pending_files.assert_not_called()
        backup.run.assert_called_once_with()

    def test_source_guard_runs_inside_long_lived_app(self):
        app = self.make_app()
        guard = Mock()
        guard.check.return_value = {"state": "healthy", "last_result": "healthy"}

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.WeChatSourceGuard", return_value=guard),
        ):
            app._run_source_guard_consumer()

        guard.check.assert_called_once_with()
        self.assertFalse(app._source_guard_lock.locked())

    def test_link_backfill_reports_busy_without_overlapping_live_worker(self):
        app = self.make_app()
        app._resource_backup_lock.acquire()
        app._plan_link_backfill(0, "all")

        app._run_on_main.assert_called_once_with(
            app._confirm_link_backfill_plan,
            0,
            "all",
            {"state": "busy", "source_complete": False},
        )


if __name__ == "__main__":
    unittest.main()
