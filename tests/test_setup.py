import ast
import unittest

from tests.paths import repo_path


class SetupPy2AppTests(unittest.TestCase):
    def _setup_options(self):
        tree = ast.parse(repo_path("setup.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "OPTIONS":
                        return ast.literal_eval(node.value)
        return None

    def _setup_call_keywords(self):
        tree = ast.parse(repo_path("setup.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setup":
                return {keyword.arg for keyword in node.keywords}
        return set()

    def test_py2app_plist_declares_stable_bundle_identity(self):
        options = self._setup_options()

        self.assertIsNotNone(options)
        plist = options["plist"]
        self.assertIn("CFBundleIdentifier", plist)
        self.assertEqual(plist["CFBundleIdentifier"], "io.github.indeliblevivi.we-groupchat-obsidian")
        self.assertEqual(plist["CFBundleName"], "WeGroupchatObsidian")
        self.assertIn("CFBundleDisplayName", plist)
        self.assertEqual(plist["CFBundleDisplayName"], "微信总结")
        self.assertTrue(plist["LSUIElement"])

    def test_py2app_uses_project_notification_icon(self):
        options = self._setup_options()

        self.assertEqual(options["iconfile"], "resources/app_icon.icns")
        self.assertTrue(repo_path(options["iconfile"]).is_file())

    def test_py2app_does_not_package_repository_tests(self):
        self.assertNotIn("tests", self._setup_options()["packages"])

    def test_setup_py_keeps_dependencies_in_requirements_file(self):
        keywords = self._setup_call_keywords()

        self.assertNotIn("install_requires", keywords)
        self.assertNotIn("setup_requires", keywords)


if __name__ == "__main__":
    unittest.main()
