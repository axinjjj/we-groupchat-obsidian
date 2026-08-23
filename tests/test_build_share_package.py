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


if __name__ == "__main__":
    unittest.main()
