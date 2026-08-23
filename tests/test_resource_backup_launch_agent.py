import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.resource_backup_launch_agent as launch_agent


class ResourceBackupLaunchAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        (self.project / "scripts").mkdir(parents=True)
        (self.project / "scripts" / "resource_backup.py").write_text(
            "#!/usr/bin/env python3\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def completed(returncode=0, stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)

    def test_install_refuses_short_lived_worker_without_writing_plist(self):
        with (
            patch.object(launch_agent.Path, "home", return_value=self.home),
            patch.object(launch_agent, "_run", return_value=self.completed(1)),
        ):
            result = launch_agent.install(self.project, interval_seconds=30)
            plist_path = launch_agent.plist_path()

        self.assertEqual(result["state"], "long_lived_app_required")
        self.assertFalse(result["installed"])
        self.assertFalse(result["loaded"])
        self.assertFalse(plist_path.exists())

    def test_status_and_uninstall_are_idempotent(self):
        with (
            patch.object(launch_agent.Path, "home", return_value=self.home),
            patch.object(launch_agent, "_run", return_value=self.completed()),
        ):
            path = launch_agent.plist_path()
            path.parent.mkdir(parents=True)
            with path.open("wb") as handle:
                plistlib.dump({
                    "ProgramArguments": ["/tmp/App.app/Contents/MacOS/App", "--resource-backup-run"],
                }, handle)
            status = launch_agent.status()
            removed = launch_agent.uninstall()
            removed_again = launch_agent.uninstall()
            plist_path = launch_agent.plist_path()

        self.assertEqual(status["state"], "loaded")
        self.assertTrue(status["installed"])
        self.assertTrue(status["loaded"])
        self.assertEqual(status["runtime_identity"], "app_bundle")
        self.assertEqual(removed["state"], "uninstalled")
        self.assertEqual(removed_again["state"], "uninstalled")
        self.assertFalse(plist_path.exists())


if __name__ == "__main__":
    unittest.main()
