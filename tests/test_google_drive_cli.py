import unittest
from unittest.mock import Mock, patch

from scripts import google_drive_file_sync as cli


class GoogleDriveFileSyncCliTests(unittest.TestCase):
    def test_required_commands_parse_as_separate_actions(self):
        parser = cli.build_parser()
        commands = (
            ["auth", "--client-secrets", "/tmp/client.json"],
            ["auth-status"],
            ["disconnect"],
            ["status"],
            ["enable"],
            ["disable"],
            ["pause"],
            ["resume"],
            ["scan"],
            ["run"],
            ["reconcile"],
            ["backfill", "--from", "2026-08-01"],
            ["backfill", "--from", "2026-08-01", "--apply"],
        )

        self.assertEqual(
            [parser.parse_args(argv).command for argv in commands],
            [argv[0] for argv in commands],
        )

    def test_enable_initializes_now_without_auth_source_or_remote_write(self):
        service = Mock()
        with patch.object(cli, "_set_config", return_value={"google_drive_file_sync_enabled": True}) as set_config, \
             patch.object(cli, "_service", return_value=service) as make_service:
            result = cli.main(["enable"])

        self.assertEqual(result, 0)
        set_config.assert_called_once_with(
            google_drive_file_sync_enabled=True,
            google_drive_file_sync_paused=False,
        )
        make_service.assert_called_once_with({"google_drive_file_sync_enabled": True})
        service.initialize_selected_chat_cursors.assert_called_once_with()

    def test_backfill_without_apply_remains_read_only(self):
        service = Mock()
        service.backfill.return_value = {"state": "planned", "inserted": 0}
        with patch.object(cli, "load_config", return_value={}), \
             patch.object(cli, "_service", return_value=service):
            result = cli.main(["backfill", "--from", "2026-08-01"])

        self.assertEqual(result, 0)
        service.backfill.assert_called_once()
        self.assertFalse(service.backfill.call_args.kwargs["apply"])

    def test_disconnect_only_removes_keychain_token(self):
        oauth = Mock()
        with patch.object(cli, "GoogleDriveOAuth", return_value=oauth):
            result = cli.main(["disconnect"])

        self.assertEqual(result, 0)
        oauth.disconnect.assert_called_once_with()

    def test_auth_status_validates_refresh_token_and_exits_nonzero_when_required(self):
        oauth = Mock()
        oauth.status.return_value = {"state": "auth_required", "connected": False}
        with patch.object(cli, "GoogleDriveOAuth", return_value=oauth):
            result = cli.main(["auth-status"])

        self.assertEqual(result, 1)
        oauth.status.assert_called_once_with(validate=True)

    def test_failed_one_shot_states_exit_nonzero(self):
        for command, state in (
            ("scan", "source_degraded"),
            ("run", "auth_required"),
            ("run", "retry_wait"),
            ("reconcile", "remote_degraded"),
            ("run", "failed"),
        ):
            with self.subTest(command=command, state=state):
                service = Mock()
                getattr(service, command).return_value = {"state": state}
                with patch.object(cli, "load_config", return_value={}), \
                     patch.object(cli, "_service", return_value=service):
                    result = cli.main([command])
                self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
