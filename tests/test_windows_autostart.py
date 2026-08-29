import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from scripts.windows_autostart import (
    TASK_NAME,
    TASK_XML_NAMESPACE,
    build_task_xml,
    install,
    uninstall,
    validate,
)


class WindowsAutostartTests(unittest.TestCase):
    def make_project(self, root: Path) -> Path:
        project = root / "Project Name"
        launcher = project / "launchers" / "启动.ps1"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("", encoding="utf-8")
        return project

    def test_task_is_per_user_keepalive_without_embedded_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.make_project(Path(tmp))

            xml = build_task_xml(
                project_dir=project,
                user_sid="S-1-5-21-fixture",
                environ={"SystemRoot": r"C:\Windows"},
            )

        root = ElementTree.fromstring(xml)
        ns = {"task": TASK_XML_NAMESPACE}
        self.assertEqual(
            root.findtext("task:Principals/task:Principal/task:LogonType", namespaces=ns),
            "InteractiveToken",
        )
        self.assertEqual(
            root.findtext("task:Settings/task:RestartOnFailure/task:Count", namespaces=ns),
            "3",
        )
        self.assertEqual(
            root.findtext("task:Settings/task:RestartOnFailure/task:Interval", namespaces=ns),
            "PT1M",
        )
        arguments = root.findtext("task:Actions/task:Exec/task:Arguments", namespaces=ns)
        self.assertIn("--autostart", arguments)
        self.assertIn("--no-pause", arguments)
        self.assertNotIn("raw-key", arguments)

    def test_install_registers_generated_task_definition(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        with tempfile.TemporaryDirectory() as tmp:
            project = self.make_project(Path(tmp))

            result = install(
                project_dir=project,
                user_sid="S-1-5-21-fixture",
                runner=runner,
            )

        self.assertEqual(result, 0)
        command = runner.call_args.args[0]
        self.assertEqual(command[:5], ["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML"])
        self.assertEqual(command[-1], "/F")

    def test_uninstall_is_idempotent_and_forgets_remembered_raw_key(self):
        runner = Mock(return_value=SimpleNamespace(returncode=1))
        with patch("scripts.windows_autostart.delete_key") as delete_key:
            result = uninstall(runner=runner)

        self.assertEqual(result, 0)
        runner.assert_called_once_with(
            ["schtasks.exe", "/Query", "/TN", TASK_NAME],
            stdout=-3,
            stderr=-3,
            check=False,
        )
        delete_key.assert_called_once()

    def test_validate_registers_queries_and_cleans_random_task(self):
        runner = Mock(side_effect=[
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=0),
        ])
        with patch("scripts.windows_autostart.current_user_sid_string", return_value="S-1-5-21-fixture"):
            result = validate(runner=runner)

        self.assertEqual(result, 0)
        calls = [call.args[0] for call in runner.call_args_list]
        task_names = [arguments[3] for arguments in calls]
        self.assertTrue(task_names[0].startswith(f"{TASK_NAME}-validation-"))
        self.assertEqual(task_names, [task_names[0]] * 3)
        self.assertEqual(calls[0][1], "/Create")
        self.assertEqual(calls[1][1], "/Query")
        self.assertEqual(calls[2][1], "/Delete")


if __name__ == "__main__":
    unittest.main()
