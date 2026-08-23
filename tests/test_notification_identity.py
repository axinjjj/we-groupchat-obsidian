import unittest
import plistlib
import tempfile
from pathlib import Path

from core.launch_agent import LaunchAgentRecord
from core.notification_identity import (
    app_bundle_for_executable,
    notification_identity_status,
    notification_identity_status_for_launch_agent,
)


class FakeBundle:
    def __init__(self, bundle_identifier, bundle_name, bundle_path="/Applications/App.app"):
        self._bundle_identifier = bundle_identifier
        self._bundle_name = bundle_name
        self._bundle_path = bundle_path

    def bundleIdentifier(self):
        return self._bundle_identifier

    def objectForInfoDictionaryKey_(self, key):
        if key == "CFBundleName":
            return self._bundle_name
        return None

    def bundlePath(self):
        return self._bundle_path


class NotificationIdentityTests(unittest.TestCase):
    def test_expected_bundle_identity_is_ok(self):
        status = notification_identity_status(
            FakeBundle("io.github.indeliblevivi.we-groupchat-obsidian", "微信总结")
        )

        self.assertTrue(status["ok"])
        self.assertEqual(status["message"], "notification identity is stable")

    def test_python_bundle_identity_is_warned(self):
        status = notification_identity_status(
            FakeBundle("org.python.python", "Python", "/opt/homebrew/Python.app")
        )

        self.assertFalse(status["ok"])
        self.assertEqual(status["message"], "running under Python notification identity")
        self.assertEqual(status["bundle_identifier"], "org.python.python")

    def test_app_bundle_for_executable_walks_up_to_dot_app(self):
        executable = Path("/Applications/WeGroupchatObsidian.app/Contents/MacOS/app")

        self.assertEqual(
            app_bundle_for_executable(executable),
            Path("/Applications/WeGroupchatObsidian.app"),
        )

    def test_launch_agent_identity_reads_app_bundle_info_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "WeGroupchatObsidian.app"
            contents = app / "Contents"
            executable = contents / "MacOS" / "app"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            with (contents / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": "io.github.indeliblevivi.we-groupchat-obsidian",
                        "CFBundleName": "微信总结",
                        "CFBundleExecutable": "app",
                    },
                    handle,
                )
            record = LaunchAgentRecord(
                label="io.github.indeliblevivi.we-groupchat-obsidian",
                plist_path=Path(tmp) / "agent.plist",
                program_arguments=(str(executable), "--autostart"),
                working_directory=tmp,
            )

            status = notification_identity_status_for_launch_agent(record)

        self.assertTrue(status["ok"])
        self.assertEqual(status["bundle_identifier"], "io.github.indeliblevivi.we-groupchat-obsidian")
        self.assertEqual(status["bundle_name"], "微信总结")
        self.assertEqual(status["source"], "launch-agent-app-bundle")


if __name__ == "__main__":
    unittest.main()
