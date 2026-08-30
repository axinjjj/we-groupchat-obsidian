from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

from core.platform import (
    PathIdentityError,
    PlatformName,
    ReparsePointConflict,
    create_path_service,
)
from core.platform.windows_paths import (
    _ExistingWindowsPath,
    _FILE_ATTRIBUTE_DIRECTORY,
    _FILE_ATTRIBUTE_REPARSE_POINT,
    _normalize_windows_syntax,
    _to_extended_path,
)


class WindowsPathSyntaxTests(unittest.TestCase):
    @staticmethod
    def _identity_full_path(value):
        return value

    def test_unc_syntax_has_distinct_display_and_operational_forms(self):
        value = _normalize_windows_syntax(
            r"\\server\share\group chat\消息.db",
            get_full_path=self._identity_full_path,
        )
        self.assertEqual(
            value.display_path,
            r"\\server\share\group chat\消息.db",
        )
        self.assertEqual(
            value.operational_path,
            r"\\?\UNC\server\share\group chat\消息.db",
        )
        self.assertTrue(value.is_unc)

    def test_extended_unc_syntax_is_idempotent(self):
        value = _normalize_windows_syntax(
            r"\\?\UNC\server\share\group chat\消息.db",
            get_full_path=self._identity_full_path,
        )
        self.assertEqual(
            value.operational_path,
            r"\\?\UNC\server\share\group chat\消息.db",
        )

    def test_device_and_unsupported_extended_namespaces_fail_closed(self):
        for configured in (
            r"\\.\PIPE\wgo",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1",
        ):
            with self.subTest(configured=configured):
                with self.assertRaises(PathIdentityError):
                    _normalize_windows_syntax(
                        configured,
                        get_full_path=self._identity_full_path,
                    )

    def test_drive_relative_and_root_relative_paths_fail_closed(self):
        for configured in (r"C:relative.db", r"\relative.db"):
            with self.subTest(configured=configured):
                with self.assertRaises(PathIdentityError):
                    _normalize_windows_syntax(
                        configured,
                        get_full_path=self._identity_full_path,
                    )

    def test_reserved_and_trailing_dot_space_components_fail_closed(self):
        reasons = {
            r"C:\state\CON.txt": "reserved_name",
            r"C:\state\message.": "trailing_dot_or_space",
            "C:\\state\\message ": "trailing_dot_or_space",
            r"C:\state\message?.db": "reserved_character",
        }
        for configured, reason in reasons.items():
            with self.subTest(configured=configured):
                with self.assertRaises(PathIdentityError) as raised:
                    _normalize_windows_syntax(
                        configured,
                        get_full_path=self._identity_full_path,
                    )
                self.assertEqual(raised.exception.reason, reason)


@unittest.skipUnless(sys.platform == "win32", "Windows path identity gate")
class WindowsPathIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wgo_path_identity_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = create_path_service(PlatformName.WINDOWS)

    def test_drive_spaces_cjk_emoji_and_source_relative_path(self):
        target = self.root / "group chat" / "消息😀.db"
        target.parent.mkdir()
        target.write_bytes(b"public synthetic fixture")

        value = self.service.describe(target, source_root=self.root)

        self.assertTrue(value.operational_path.startswith("\\\\?\\"))
        self.assertEqual(Path(value.display_path).name, "消息😀.db")
        self.assertEqual(value.source_relative_path, "group chat/消息😀.db")
        self.assertTrue(value.identity_key.startswith("windows-file:v1:"))

    def test_existing_case_aliases_have_one_identity_key(self):
        target = self.root / "CaseAlias.db"
        target.write_bytes(b"case alias")
        hardlink = self.root / "hardlink.db"
        os.link(target, hardlink)

        canonical = self.service.describe(target)
        alias = self.service.describe(self.root / "casealias.DB")
        extended = self.service.describe(_to_extended_path(str(target)))
        dot_alias = self.service.describe(target.parent / "." / target.name)
        hardlink_alias = self.service.describe(hardlink)

        self.assertEqual(alias.identity_key, canonical.identity_key)
        self.assertEqual(extended.identity_key, canonical.identity_key)
        self.assertEqual(dot_alias.identity_key, canonical.identity_key)
        self.assertEqual(hardlink_alias.identity_key, canonical.identity_key)
        self.assertEqual(alias.display_path, canonical.display_path)

    def test_missing_final_component_case_aliases_share_parent_identity(self):
        first = self.service.describe(self.root / "Future.db")
        second = self.service.describe(self.root / "future.DB")
        unicode_first = self.service.describe(self.root / "Ångström.db")
        unicode_second = self.service.describe(self.root / "ångström.DB")

        self.assertEqual(first.identity_key, second.identity_key)
        self.assertEqual(
            unicode_first.identity_key,
            unicode_second.identity_key,
        )
        self.assertTrue(first.identity_key.startswith("windows-child:v1:"))

    def test_long_missing_final_component_uses_extended_operational_path(self):
        display_parent = str(self.root)
        segment = "long-segment-0123456789"
        while len(display_parent) < 280:
            display_parent = os.path.join(display_parent, segment)
        os.makedirs(_to_extended_path(display_parent))

        value = self.service.describe(os.path.join(display_parent, "future.db"))

        self.assertGreater(len(value.display_path), 260)
        self.assertTrue(value.operational_path.startswith("\\\\?\\"))
        self.assertTrue(value.identity_key.startswith("windows-child:v1:"))

    def test_windows_path_is_not_posix_shell_unescaped(self):
        parent = self.root / "literal"
        parent.mkdir()
        configured = str(parent) + r"\ leading-space.db"

        value = self.service.describe(configured)

        self.assertEqual(Path(value.display_path).name, " leading-space.db")

    def test_unc_is_syntax_only_and_not_live_source_support(self):
        with self.assertRaises(PathIdentityError) as raised:
            self.service.describe(r"\\server\share\group\messages.db")
        self.assertEqual(raised.exception.reason, "remote_path_unsupported")

    def test_source_root_escape_fails_closed(self):
        outside = self.root.parent / "outside.db"
        with self.assertRaises(PathIdentityError) as raised:
            self.service.describe(outside, source_root=self.root)
        self.assertEqual(raised.exception.reason, "source_root_escape")

    def test_directory_symlink_fixture_is_rejected_when_host_allows_it(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink fixture unavailable: {exc.winerror}")

        with self.assertRaises(ReparsePointConflict):
            self.service.describe(link)

    def test_junction_and_unknown_reparse_tags_share_fail_closed_boundary(self):
        for tag in (0xA0000003, 0xA000001D):
            with self.subTest(tag=tag):
                value = _ExistingWindowsPath(
                    operational_path=r"\\?\C:\synthetic",
                    volume_serial=1,
                    file_id=b"\0" * 16,
                    attributes=(
                        _FILE_ATTRIBUTE_DIRECTORY
                        | _FILE_ATTRIBUTE_REPARSE_POINT
                    ),
                    reparse_tag=tag,
                    filesystem_name="NTFS",
                    case_sensitive=False,
                )
                with self.assertRaises(ReparsePointConflict) as raised:
                    self.service._validate_existing(value)
                self.assertEqual(
                    raised.exception.reason,
                    f"tag_{tag:08x}",
                )

    def test_case_sensitive_directory_and_non_ntfs_fail_closed(self):
        base = dict(
            operational_path=r"\\?\C:\synthetic",
            volume_serial=1,
            file_id=b"\0" * 16,
            attributes=_FILE_ATTRIBUTE_DIRECTORY,
            reparse_tag=0,
        )
        cases = (
            (
                _ExistingWindowsPath(
                    **base,
                    filesystem_name="NTFS",
                    case_sensitive=True,
                ),
                "case_sensitive_directory_unsupported",
            ),
            (
                _ExistingWindowsPath(
                    **base,
                    filesystem_name="ReFS",
                    case_sensitive=False,
                ),
                "unsupported_filesystem",
            ),
        )
        for value, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(PathIdentityError) as raised:
                    self.service._validate_existing(value)
                self.assertEqual(raised.exception.reason, reason)


@unittest.skipUnless(sys.platform == "darwin", "macOS path identity gate")
class MacOSPathIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wgo_path_identity_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = create_path_service(PlatformName.MACOS)

    def test_existing_and_symlink_aliases_share_inode_identity(self):
        target = self.root / "messages.db"
        target.write_bytes(b"alias")
        link = self.root / "messages-link.db"
        link.symlink_to(target)

        canonical = self.service.describe(target)
        alias = self.service.describe(link)

        self.assertEqual(alias.identity_key, canonical.identity_key)
        self.assertEqual(alias.operational_path, canonical.operational_path)

    def test_missing_final_component_has_stable_parent_identity(self):
        first = self.service.describe(self.root / "future.db")
        second = self.service.describe(self.root / "future.db")

        self.assertEqual(first.identity_key, second.identity_key)
        self.assertTrue(first.identity_key.startswith("macos-child:v1:"))

    def test_source_relative_path_is_slash_normalized(self):
        target = self.root / "group chat" / "消息😀.db"
        target.parent.mkdir()
        target.write_bytes(b"relative")

        value = self.service.describe(target, source_root=self.root)

        self.assertEqual(value.source_relative_path, "group chat/消息😀.db")

    def test_source_root_escape_fails_closed(self):
        outside = self.root.parent / "outside.db"
        with self.assertRaises(PathIdentityError) as raised:
            self.service.describe(outside, source_root=self.root)
        self.assertEqual(raised.exception.reason, "source_root_escape")


if __name__ == "__main__":
    unittest.main()
