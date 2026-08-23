import os
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
        self.data_dir = self.root / "data"
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

    def test_install_writes_short_lived_background_agent_without_keepalive(self):
        with (
            patch.object(launch_agent.Path, "home", return_value=self.home),
            patch.object(launch_agent, "DATA_DIR", str(self.data_dir)),
            patch.object(launch_agent, "_run", return_value=self.completed()) as run,
            patch.dict(
                os.environ,
                {"WE_GROUPCHAT_OBSIDIAN_DATA_DIR": str(self.data_dir)},
                clear=False,
            ),
        ):
            result = launch_agent.install(self.project, interval_seconds=30)
            plist_path = launch_agent.plist_path()

        self.assertEqual(result["state"], "installed")
        self.assertEqual(result["interval_seconds"], 60)
        self.assertTrue(plist_path.is_file())
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        self.assertEqual(
            payload["Label"],
            launch_agent.RESOURCE_BACKUP_LAUNCH_AGENT_LABEL,
        )
        self.assertEqual(payload["ProgramArguments"][-1], "run")
        self.assertEqual(payload["WorkingDirectory"], str(self.project.resolve()))
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 60)
        self.assertEqual(payload["ProcessType"], "Background")
        self.assertTrue(payload["LowPriorityIO"])
        self.assertNotIn("KeepAlive", payload)
        self.assertEqual(
            payload["EnvironmentVariables"]["WE_GROUPCHAT_OBSIDIAN_DATA_DIR"],
            str(self.data_dir),
        )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], "launchctl")
        self.assertEqual(run.call_args_list[0].args[1], "bootout")
        self.assertEqual(run.call_args_list[1].args[1], "bootstrap")

    def test_install_fails_without_publishing_a_loaded_state(self):
        responses = [self.completed(), self.completed(1, "bootstrap failed")]
        with (
            patch.object(launch_agent.Path, "home", return_value=self.home),
            patch.object(launch_agent, "DATA_DIR", str(self.data_dir)),
            patch.object(launch_agent, "_run", side_effect=responses),
        ):
            result = launch_agent.install(self.project, interval_seconds=300)

        self.assertEqual(result["state"], "install_failed")
        self.assertTrue(result["installed"])
        self.assertFalse(result["loaded"])
        self.assertIn("bootstrap failed", result["error"])

    def test_status_and_uninstall_are_idempotent(self):
        with (
            patch.object(launch_agent.Path, "home", return_value=self.home),
            patch.object(launch_agent, "DATA_DIR", str(self.data_dir)),
            patch.object(launch_agent, "_run", return_value=self.completed()),
        ):
            installed = launch_agent.install(self.project, interval_seconds=300)
            status = launch_agent.status()
            removed = launch_agent.uninstall()
            removed_again = launch_agent.uninstall()
            plist_path = launch_agent.plist_path()

        self.assertEqual(installed["state"], "installed")
        self.assertEqual(status["state"], "loaded")
        self.assertTrue(status["installed"])
        self.assertTrue(status["loaded"])
        self.assertEqual(removed["state"], "uninstalled")
        self.assertEqual(removed_again["state"], "uninstalled")
        self.assertFalse(plist_path.exists())


if __name__ == "__main__":
    unittest.main()
