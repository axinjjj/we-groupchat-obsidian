import plistlib
import tempfile
import unittest
from pathlib import Path

from core.launch_agent import (
    DEFAULT_LABEL,
    choose_install_record,
    discover_managed_launch_agents,
)


class LaunchAgentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project_dir = self.root / "repo"
        self.project_dir.mkdir()
        self.agents_dir = self.root / "LaunchAgents"
        self.agents_dir.mkdir()

    def write_plist(self, label, project_dir=None, filename=None):
        project_dir = Path(project_dir or self.project_dir)
        path = self.agents_dir / (filename or f"{label}.plist")
        with path.open("wb") as handle:
            plistlib.dump(
                {
                    "Label": label,
                    "ProgramArguments": [
                        "/bin/bash",
                        str(project_dir / "启动.command"),
                        "--autostart",
                    ],
                    "WorkingDirectory": str(project_dir),
                },
                handle,
            )
        return path

    def test_fresh_install_uses_neutral_default_label(self):
        record = choose_install_record(self.project_dir, launch_agents_dir=self.agents_dir)

        self.assertEqual(record.label, DEFAULT_LABEL)
        self.assertEqual(record.plist_path, self.agents_dir / f"{DEFAULT_LABEL}.plist")

    def test_existing_managed_plist_preserves_label_by_default(self):
        self.write_plist("com.example.local-wechat-summary")

        record = choose_install_record(self.project_dir, launch_agents_dir=self.agents_dir)

        self.assertEqual(record.label, "com.example.local-wechat-summary")

    def test_unrelated_plist_is_ignored(self):
        other_project = self.root / "other-repo"
        other_project.mkdir()
        self.write_plist(DEFAULT_LABEL, project_dir=other_project)

        records = discover_managed_launch_agents(self.project_dir, self.agents_dir)

        self.assertEqual(records, [])

    def test_migrate_label_targets_neutral_default(self):
        self.write_plist("com.example.local-wechat-summary")

        record = choose_install_record(
            self.project_dir,
            launch_agents_dir=self.agents_dir,
            migrate_label=True,
        )

        self.assertEqual(record.label, DEFAULT_LABEL)
        self.assertEqual(record.plist_path, self.agents_dir / f"{DEFAULT_LABEL}.plist")

    def test_discovers_legacy_named_runtime_copy_for_new_repo(self):
        project_dir = self.root / "we-groupchat-obsidian"
        project_dir.mkdir()
        runtime_dir = self.root / "projects" / "mac-wechat-summary"
        runtime_dir.mkdir(parents=True)
        self.write_plist("com.example.local-wechat-summary", project_dir=runtime_dir)

        records = discover_managed_launch_agents(
            project_dir,
            self.agents_dir,
            include_related=True,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].match_kind, "runtime-copy")
        self.assertEqual(records[0].label, "com.example.local-wechat-summary")

    def test_discovers_same_named_runtime_app_bundle(self):
        project_dir = self.root / "we-groupchat-obsidian"
        project_dir.mkdir()
        runtime_dir = self.root / "projects" / "we-groupchat-obsidian"
        app_executable = runtime_dir / "dist" / "WeGroupchatObsidian.app" / "Contents" / "MacOS" / "WeGroupchatObsidian"
        app_executable.parent.mkdir(parents=True)
        app_executable.write_text("#!/bin/sh\n", encoding="utf-8")
        path = self.agents_dir / f"{DEFAULT_LABEL}.plist"
        with path.open("wb") as handle:
            plistlib.dump(
                {
                    "Label": DEFAULT_LABEL,
                    "ProgramArguments": [str(app_executable), "--autostart"],
                    "WorkingDirectory": str(runtime_dir),
                },
                handle,
            )

        records = discover_managed_launch_agents(
            project_dir,
            self.agents_dir,
            include_related=True,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].match_kind, "runtime-copy")
        self.assertEqual(records[0].program_arguments[0], str(app_executable))


if __name__ == "__main__":
    unittest.main()
