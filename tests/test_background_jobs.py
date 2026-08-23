import unittest
from unittest.mock import patch

from core.background_jobs import dispatch_background_job, runtime_identity


class BackgroundJobDispatchTests(unittest.TestCase):
    def test_runtime_identity_distinguishes_app_bundle_from_python(self):
        self.assertEqual(
            runtime_identity(["/tmp/App.app/Contents/MacOS/App", "--job"]),
            "app_bundle",
        )
        self.assertEqual(runtime_identity(["/tmp/.venv/bin/python", "job.py"]), "python")

    def test_non_background_invocation_leaves_menu_app_startup_alone(self):
        self.assertIsNone(dispatch_background_job(["--autostart"]))

    def test_resource_backup_mode_runs_one_shot_without_menu_app(self):
        with patch("scripts.resource_backup.main", return_value=7) as main:
            result = dispatch_background_job(["--resource-backup-run"])

        self.assertEqual(result, 7)
        main.assert_called_once_with(["run"])

    def test_source_guard_mode_runs_one_shot_without_menu_app(self):
        with patch("scripts.wechat_source_guard_agent.main", return_value=3) as main:
            result = dispatch_background_job(["--source-guard-run"])

        self.assertEqual(result, 3)
        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
