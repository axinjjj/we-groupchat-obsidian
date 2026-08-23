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


if __name__ == "__main__":
    unittest.main()
