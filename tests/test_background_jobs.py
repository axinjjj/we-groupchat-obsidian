import io
import json
import unittest
from contextlib import redirect_stdout
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

    def test_retired_resource_backup_mode_does_not_touch_app_data(self):
        output = io.StringIO()
        with (
            patch("scripts.resource_backup.main") as main,
            redirect_stdout(output),
        ):
            result = dispatch_background_job(["--resource-backup-run"])

        self.assertEqual(result, 2)
        main.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["state"],
            "long_lived_app_required",
        )

    def test_retired_source_guard_mode_does_not_touch_app_data(self):
        output = io.StringIO()
        with (
            patch("core.wechat_source_guard.WeChatSourceGuard") as guard,
            redirect_stdout(output),
        ):
            result = dispatch_background_job(["--source-guard-run"])

        self.assertEqual(result, 2)
        guard.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["state"],
            "long_lived_app_required",
        )


if __name__ == "__main__":
    unittest.main()
