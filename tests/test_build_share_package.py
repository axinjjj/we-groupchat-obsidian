import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import build_share_package as share
from tests.paths import REPO_ROOT


class SharePackageTests(unittest.TestCase):
    def test_internal_superpowers_tree_is_excluded(self):
        self.assertTrue(share.should_exclude(".superpowers/"))
        self.assertTrue(share.should_exclude(".superpowers/sdd/internal-report.md"))

    def test_test_bytecode_cache_is_excluded_after_suite_reorganization(self):
        self.assertTrue(share.should_exclude("tests/__pycache__/"))
        self.assertTrue(share.should_exclude("tests/__pycache__/test_config.pyc"))

    def test_share_source_inventory_contains_packaged_tests_only(self):
        inventory = set(share.source_files())
        self.assertNotIn(share.GUIDE_NAME, inventory)
        expected = {
            path.relative_to(REPO_ROOT).as_posix()
            for path in (REPO_ROOT / "tests").glob("test_*.py")
            if path.relative_to(REPO_ROOT).as_posix() in inventory
        }

        self.assertTrue(expected <= inventory)
        self.assertIn("tests/__init__.py", inventory)
        self.assertIn("tests/paths.py", inventory)
        self.assertFalse(any(
            "/" not in path and path.startswith("test_") and path.endswith(".py")
            for path in inventory
        ))

    def test_share_source_inventory_uses_layered_finder_launchers(self):
        inventory = set(share.source_files())
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
            {
                path.removeprefix("launchers/")
                for path in inventory
                if path.startswith("launchers/")
            },
            launcher_names,
        )
        self.assertFalse(any(
            "/" not in path and path.endswith(".command") and path != "启动.command"
            for path in inventory
        ))

    @staticmethod
    def write_manifest(root, entries):
        payload = {
            "schema": share.MANIFEST_SCHEMA,
            "source_commit": "fixture",
            "files": entries,
        }
        (root / share.MANIFEST_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_no_git_source_without_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            share, "ROOT", Path(tmp)
        ), patch.object(
            share,
            "git_entries",
            side_effect=share.SharePackageError("git_tree_unavailable"),
        ):
            with self.assertRaisesRegex(
                share.SharePackageError, "share_manifest_missing"
            ):
                share.source_entries()

    def test_no_git_source_copies_only_hash_verified_manifest_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "README.md"
            allowed.write_bytes(b"public source\n")
            (root / ".env").write_text("PRIVATE=secret", encoding="utf-8")
            digest = hashlib.sha256(allowed.read_bytes()).hexdigest()
            self.write_manifest(root, [{
                "path": "README.md",
                "mode": "100644",
                "sha256": digest,
            }])
            with patch.object(share, "ROOT", root), patch.object(
                share,
                "git_entries",
                side_effect=share.SharePackageError("git_tree_unavailable"),
            ):
                commit, entries = share.source_entries()

            self.assertEqual(commit, "fixture")
            self.assertEqual([entry["path"] for entry in entries], ["README.md"])

    def test_manifest_hash_tamper_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "README.md"
            target.write_bytes(b"changed")
            self.write_manifest(root, [{
                "path": "README.md",
                "mode": "100644",
                "sha256": "0" * 64,
            }])
            with patch.object(share, "ROOT", root):
                with self.assertRaisesRegex(
                    share.SharePackageError, "manifest_member_hash_mismatch"
                ):
                    share.manifest_entries()

            target.unlink()
            real_parent = root / "real-docs"
            real_parent.mkdir()
            nested = real_parent / "guide.md"
            nested.write_bytes(b"guide")
            os.symlink(real_parent, root / "docs")
            self.write_manifest(root, [{
                "path": "docs/guide.md",
                "mode": "100644",
                "sha256": hashlib.sha256(b"guide").hexdigest(),
            }])
            with patch.object(share, "ROOT", root):
                with self.assertRaisesRegex(
                    share.SharePackageError, "manifest_member_not_regular"
                ):
                    share.manifest_entries()

            outside = root / "outside"
            outside.write_bytes(b"changed")
            os.symlink(outside, target)
            self.write_manifest(root, [{
                "path": "README.md",
                "mode": "100644",
                "sha256": hashlib.sha256(b"changed").hexdigest(),
            }])
            with patch.object(share, "ROOT", root):
                with self.assertRaisesRegex(
                    share.SharePackageError, "manifest_member_not_regular"
                ):
                    share.manifest_entries()

    def test_git_tree_symlink_is_rejected_before_blob_read(self):
        with patch.object(
            share,
            "_run_git",
            side_effect=[
                b"a" * 40 + b"\n",
                b"120000 blob " + b"b" * 40 + b"\tlinked.txt\0",
            ],
        ):
            with self.assertRaisesRegex(
                share.SharePackageError,
                "tracked_symlink_or_nonregular_rejected",
            ):
                share.git_entries()

    def test_package_manifest_and_zip_member_sets_are_exact(self):
        entry = {
            "path": "README.md",
            "mode": "100644",
            "sha256": hashlib.sha256(b"public\n").hexdigest(),
            "data": b"public\n",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            share, "source_entries", return_value=("commit", [entry])
        ):
            out = Path(tmp)
            zip_path = share.build(out, "fixture-share")
            manifest = json.loads(
                (out / "fixture-share" / share.MANIFEST_NAME).read_text(
                    encoding="utf-8"
                )
            )
            with zipfile.ZipFile(zip_path) as archive:
                members = set(archive.namelist())

        self.assertEqual(manifest["files"], [{
            "path": "README.md",
            "mode": "100644",
            "sha256": entry["sha256"],
        }])
        self.assertEqual(members, {
            "fixture-share/README.md",
            f"fixture-share/{share.GUIDE_NAME}",
            f"fixture-share/{share.MANIFEST_NAME}",
        })

    def test_package_name_cannot_escape_output_or_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaisesRegex(
                share.SharePackageError, "unsafe_package_name"
            ):
                share.build(out, "../escape")
            (out / "existing").mkdir()
            with self.assertRaisesRegex(share.SharePackageError, "output_exists"):
                share.build(out, "existing")


if __name__ == "__main__":
    unittest.main()
