import unittest

from scripts.build_share_package import should_exclude


class SharePackageTests(unittest.TestCase):
    def test_internal_superpowers_tree_is_excluded(self):
        self.assertTrue(should_exclude(".superpowers/"))
        self.assertTrue(should_exclude(".superpowers/sdd/internal-report.md"))


if __name__ == "__main__":
    unittest.main()
