import os
import unittest

from tests.paths import REPO_ROOT


class RepositoryLayoutTests(unittest.TestCase):
    def test_root_python_files_are_deliberate_entrypoints(self):
        root_python = {
            path.name for path in REPO_ROOT.glob("*.py") if path.is_file()
        }

        self.assertEqual(root_python, {"app.py", "mcp_server.py", "setup.py"})

    def test_unittests_live_in_importable_tests_package(self):
        tests_root = REPO_ROOT / "tests"

        self.assertTrue((tests_root / "__init__.py").is_file())
        self.assertGreater(len(list(tests_root.glob("test_*.py"))), 0)
        self.assertEqual(list(REPO_ROOT.glob("test_*.py")), [])

    def test_operator_scripts_are_an_explicit_package(self):
        self.assertTrue((REPO_ROOT / "scripts" / "__init__.py").is_file())

    def test_finder_launchers_have_one_root_compatibility_entrypoint(self):
        expected = {
            "启动.command",
            "配置关注推送.command",
            "健康检查.command",
            "刷新数据源.command",
            "历史总结到Obsidian.command",
            "整理Obsidian输出.command",
            "安装自动启动.command",
            "卸载自动启动.command",
            "补跑遗漏笔记.command",
        }
        root_launchers = {
            path.name for path in REPO_ROOT.glob("*.command") if path.is_file()
        }
        canonical_launchers = {
            path.name
            for path in (REPO_ROOT / "launchers").glob("*.command")
            if path.is_file()
        }

        self.assertEqual(root_launchers, {"启动.command"})
        self.assertEqual(canonical_launchers, expected)
        self.assertTrue(all(
            os.access(REPO_ROOT / "launchers" / name, os.X_OK)
            for name in expected
        ))
        root_start = (REPO_ROOT / "启动.command").read_text(encoding="utf-8")
        self.assertIn("launchers/启动.command", root_start)
        self.assertNotIn("ensure_python()", root_start)


if __name__ == "__main__":
    unittest.main()
