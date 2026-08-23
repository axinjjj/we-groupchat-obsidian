import unittest

from scripts.build_share_package import should_exclude, source_files
from tests.paths import REPO_ROOT


class SharePackageTests(unittest.TestCase):
    def test_internal_superpowers_tree_is_excluded(self):
        self.assertTrue(should_exclude(".superpowers/"))
        self.assertTrue(should_exclude(".superpowers/sdd/internal-report.md"))

    def test_test_bytecode_cache_is_excluded_after_suite_reorganization(self):
        self.assertTrue(should_exclude("tests/__pycache__/"))
        self.assertTrue(should_exclude("tests/__pycache__/test_config.pyc"))

    def test_share_source_inventory_contains_packaged_tests_only(self):
        inventory = set(source_files())
        expected = {
            path.relative_to(REPO_ROOT).as_posix()
            for path in (REPO_ROOT / "tests").glob("test_*.py")
        }

        self.assertTrue(expected <= inventory)
        self.assertIn("tests/__init__.py", inventory)
        self.assertIn("tests/paths.py", inventory)
        self.assertFalse(any(
            "/" not in path and path.startswith("test_") and path.endswith(".py")
            for path in inventory
        ))

    def test_share_source_inventory_uses_layered_finder_launchers(self):
        inventory = set(source_files())
        launcher_names = {
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

        self.assertIn("启动.command", inventory)
        self.assertEqual(
            {path.removeprefix("launchers/") for path in inventory if path.startswith("launchers/")},
            launcher_names,
        )
        self.assertFalse(any(
            "/" not in path and path.endswith(".command") and path != "启动.command"
            for path in inventory
        ))


if __name__ == "__main__":
    unittest.main()
