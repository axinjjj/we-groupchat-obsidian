import os
import tempfile
import unittest

from core.windows_permissions import (
    is_private_to_current_user,
    restrict_path_to_current_user,
)


@unittest.skipUnless(os.name == "nt", "Windows ACL behavior")
class WindowsPermissionsTests(unittest.TestCase):
    def test_restricts_file_and_directory_to_current_user(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "private.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")

            self.assertTrue(restrict_path_to_current_user(root, is_directory=True))
            self.assertTrue(restrict_path_to_current_user(path, is_directory=False))
            self.assertTrue(is_private_to_current_user(root))
            self.assertTrue(is_private_to_current_user(path))


if __name__ == "__main__":
    unittest.main()
