import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from core.relation_audit import KNOWN_BROKEN_RELATION_REASON
from core.relation_markdown_cleanup import (
    CleanupError,
    CleanupExpectations,
    apply_cleanup,
    atomic_write_private,
    canonical_json_bytes,
    collect_database_evidence,
    open_read_only_db,
    preview_cleanup,
    read_file_bounded,
    relation_section_line_indexes,
    rollback_cleanup,
    sha256_bytes,
    splice_exact_relation_lines,
    status_cleanup,
    validate_topic_path,
    load_sealed_run,
)
from core.knowledge import KnowledgeStore
from scripts import repair_relation_markdown as cleanup_cli
from tests.paths import REPO_ROOT, repo_path


class RelationMarkdownCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_root = os.path.realpath(self.tmp.name)
        self.backup_db = os.path.join(self.tmp_root, "backup.db")
        self.current_db = os.path.join(self.tmp_root, "current.db")
        self.vault_root = os.path.join(self.tmp_root, "vault")
        os.makedirs(self.vault_root)
        self._create_database(self.backup_db)
        self._create_database(self.current_db)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def digest(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    @staticmethod
    def _create_database(path):
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE topics (
                    topic_id INTEGER PRIMARY KEY,
                    topic_key TEXT,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    semantic_tags_json TEXT NOT NULL DEFAULT '[]',
                    key_facts_json TEXT NOT NULL,
                    links_json TEXT NOT NULL,
                    files_json TEXT NOT NULL DEFAULT '[]',
                    source_chat TEXT NOT NULL,
                    source_chat_username TEXT NOT NULL DEFAULT '',
                    vault_chat_name TEXT NOT NULL DEFAULT '',
                    taxonomy_profile TEXT NOT NULL DEFAULT '',
                    taxonomy_version INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    obsidian_path TEXT NOT NULL,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE events (
                    event_id INTEGER PRIMARY KEY,
                    topic_id INTEGER NOT NULL,
                    relation TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    category TEXT NOT NULL,
                    semantic_tags_json TEXT NOT NULL DEFAULT '[]',
                    event_type TEXT NOT NULL,
                    status_hint TEXT NOT NULL,
                    source_chat TEXT NOT NULL,
                    source_chat_username TEXT NOT NULL DEFAULT '',
                    vault_chat_name TEXT NOT NULL DEFAULT '',
                    taxonomy_profile TEXT NOT NULL DEFAULT '',
                    taxonomy_version INTEGER NOT NULL DEFAULT 0,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    senders_json TEXT NOT NULL,
                    links_json TEXT NOT NULL,
                    files_json TEXT NOT NULL DEFAULT '[]',
                    message_hash TEXT NOT NULL,
                    messages_excerpt TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE relations (
                    relation_id INTEGER PRIMARY KEY,
                    source_topic_id INTEGER NOT NULL,
                    target_topic_id INTEGER NOT NULL,
                    relation TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(source_topic_id, target_topic_id, relation)
                );
                CREATE VIRTUAL TABLE topic_fts USING fts5(
                    topic_id UNINDEXED,
                    title,
                    category,
                    summary,
                    entities,
                    key_facts,
                    links
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(path, 0o600)

    def build_fixture(self, *, invalid_nonself, invalid_self):
        run_dir = os.path.join(self.tmp_root, "cleanup-run")
        if os.path.lexists(run_dir):
            if os.path.isdir(run_dir) and not os.path.islink(run_dir):
                shutil.rmtree(run_dir)
            else:
                os.unlink(run_dir)
        for path in (self.backup_db, self.current_db):
            os.unlink(path)
            self._create_database(path)
        shutil.rmtree(self.vault_root)
        os.makedirs(self.vault_root)
        topic_count = max(2, invalid_nonself + invalid_self + 1)
        topic_rows = []
        event_rows = []
        fts_rows = []
        for topic_id in range(1, topic_count + 1):
            title = f"Topic {topic_id}"
            topic_rows.append(
                (
                    topic_id,
                    f"topic:{topic_id}",
                    title,
                    "Category",
                    "active",
                    f"Summary {topic_id}",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    f"Chat {topic_id}",
                    f"chat-{topic_id}@chatroom",
                    f"Chat {topic_id}",
                    "taxonomy-v2",
                    2,
                    "2026-07-01 00:00",
                    "2026-07-11 00:00",
                    f"关注推送/Chat {topic_id}/Category/Topic {topic_id}.md",
                    1,
                    float(topic_id),
                    float(topic_id) + 0.5,
                )
            )
            event_rows.append(
                (
                    topic_id,
                    topic_id,
                    "new",
                    title,
                    f"Summary {topic_id}",
                    "Category",
                    "[]",
                    "message",
                    "active",
                    f"Chat {topic_id}",
                    f"chat-{topic_id}@chatroom",
                    f"Chat {topic_id}",
                    "taxonomy-v2",
                    2,
                    "2026-07-01 00:00",
                    "2026-07-01 00:01",
                    "[]",
                    "[]",
                    "[]",
                    f"hash-{topic_id}",
                    "fixture excerpt",
                    float(topic_id),
                )
            )
            fts_rows.append(
                (topic_id, title, "Category", f"Summary {topic_id}", "", "", "")
            )

        for path in (self.backup_db, self.current_db):
            conn = sqlite3.connect(path)
            try:
                conn.executemany(
                    "INSERT INTO topics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    topic_rows,
                )
                conn.executemany(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    event_rows,
                )
                conn.executemany(
                    "INSERT INTO topic_fts VALUES (?,?,?,?,?,?,?)",
                    fts_rows,
                )
                conn.commit()
            finally:
                conn.close()

        conn = sqlite3.connect(self.backup_db)
        try:
            relation_id = 1
            for offset in range(invalid_nonself):
                conn.execute(
                    "INSERT INTO relations VALUES (?, 1, ?, 'updates', ?, ?)",
                    (
                        relation_id,
                        offset + 2,
                        KNOWN_BROKEN_RELATION_REASON,
                        float(relation_id) + 0.125,
                    ),
                )
                relation_id += 1
            for offset in range(invalid_self):
                topic_id = invalid_nonself + offset + 2
                conn.execute(
                    "INSERT INTO relations VALUES (?, ?, ?, 'updates', ?, ?)",
                    (
                        relation_id,
                        topic_id,
                        topic_id,
                        KNOWN_BROKEN_RELATION_REASON,
                        float(relation_id) + 0.125,
                    ),
                )
                relation_id += 1
            conn.commit()
        finally:
            conn.close()
        os.chmod(self.backup_db, 0o600)

        return {
            "backup_db": self.backup_db,
            "current_db": self.current_db,
            "vault_root": self.vault_root,
            "run_dir": os.path.join(self.tmp_root, "cleanup-run"),
            "expectations": CleanupExpectations(
                backup_sha256=self.digest(self.backup_db),
                selected_edges=invalid_nonself + invalid_self,
                self_loops=invalid_self,
                renderable_edges=invalid_nonself,
            ),
        }

    @staticmethod
    def materialize_backup_source(fixture, source_topic_id=1):
        conn = sqlite3.connect(fixture["backup_db"])
        conn.row_factory = sqlite3.Row
        try:
            topic_row = conn.execute(
                "SELECT * FROM topics WHERE topic_id = ?", (source_topic_id,)
            ).fetchone()
            events = conn.execute(
                "SELECT * FROM events WHERE topic_id = ? ORDER BY created_at, event_id",
                (source_topic_id,),
            ).fetchall()
            relations = conn.execute(
                """
                SELECT r.relation, r.reason, r.target_topic_id,
                       t.title, t.obsidian_path
                FROM relations r
                JOIN topics t ON t.topic_id = r.target_topic_id
                WHERE r.source_topic_id = ?
                ORDER BY r.created_at, r.relation
                """,
                (source_topic_id,),
            ).fetchall()
        finally:
            conn.close()
        renderer = KnowledgeStore.__new__(KnowledgeStore)
        renderer.obsidian_subdir = "关注推送"
        data = renderer._render_markdown(
            renderer._topic_dict(topic_row), events, relations
        ).encode("utf-8")
        destination = os.path.join(fixture["vault_root"], topic_row["obsidian_path"])
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as handle:
            handle.write(data)
        return destination, data

    def refresh_expectations(self, fixture):
        prior = fixture["expectations"]
        fixture["expectations"] = CleanupExpectations(
            backup_sha256=self.digest(fixture["backup_db"]),
            selected_edges=prior.selected_edges,
            self_loops=prior.self_loops,
            renderable_edges=prior.renderable_edges,
        )
        os.chmod(fixture["backup_db"], 0o600)

    def preview_fixture(self, fixture):
        return preview_cleanup(
            backup_db=fixture["backup_db"],
            current_db=fixture["current_db"],
            vault_root=fixture["vault_root"],
            obsidian_subdir="关注推送",
            run_dir=fixture["run_dir"],
            generator_commit="test-generator-commit",
            expectations=fixture["expectations"],
        )

    def build_two_source_fixture(self):
        fixture = self.build_fixture(invalid_nonself=2, invalid_self=0)
        conn = sqlite3.connect(fixture["backup_db"])
        try:
            conn.execute(
                "UPDATE relations SET source_topic_id = 2 WHERE relation_id = 2"
            )
            conn.commit()
        finally:
            conn.close()
        self.refresh_expectations(fixture)
        sources = [self.materialize_backup_source(fixture, topic_id) for topic_id in (1, 2)]
        return fixture, sources

    def build_applied_two_source_fixture(self):
        fixture, sources = self.build_two_source_fixture()
        preview = self.preview_fixture(fixture)
        apply_cleanup(
            fixture["run_dir"],
            preview["manifest_sha256"],
            f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
        )
        with open(
            os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8"
        ) as handle:
            manifest = json.load(handle)
        return fixture, sources, preview, manifest

    @staticmethod
    def vault_snapshot(root):
        records = []
        for current, dirs, files in os.walk(root, followlinks=False):
            for name in sorted(dirs + files):
                path = os.path.join(current, name)
                relative = os.path.relpath(path, root)
                info = os.lstat(path)
                if os.path.islink(path):
                    payload = ("link", os.readlink(path))
                elif os.path.isfile(path):
                    if stat.S_IMODE(info.st_mode) & 0o444:
                        with open(path, "rb") as handle:
                            payload = ("file", hashlib.sha256(handle.read()).hexdigest())
                    else:
                        payload = ("file-unreadable", info.st_size)
                else:
                    payload = ("other", stat.S_IFMT(info.st_mode))
                records.append((relative, stat.S_IMODE(info.st_mode), payload))
        return records

    def assert_preview_refuses(self, code, fixture):
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        vault_before = self.vault_snapshot(fixture["vault_root"])
        self.assertFalse(os.path.lexists(fixture["run_dir"]))
        with self.assertRaises(CleanupError) as raised:
            preview_cleanup(
                backup_db=fixture["backup_db"],
                current_db=fixture["current_db"],
                vault_root=fixture["vault_root"],
                obsidian_subdir="关注推送",
                run_dir=fixture["run_dir"],
                generator_commit="test-generator-commit",
                expectations=fixture["expectations"],
            )
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)
        self.assertEqual(self.vault_snapshot(fixture["vault_root"]), vault_before)
        self.assertFalse(os.path.lexists(fixture["run_dir"]))

    def run_cleanup_cli(self, *arguments):
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [
                sys.executable,
                repo_path("scripts", "repair_relation_markdown.py"),
                *map(str, arguments),
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def seed_cli_secrets(self, fixture):
        source_title = "Secret Topic Title"
        source_path = "关注推送/Secret Chat/Category/Secret Topic Title.md"
        target_title = "Secret Target Title"
        target_path = "关注推送/Secret Target Chat/Category/Secret Target Title.md"
        for path in (fixture["backup_db"], fixture["current_db"]):
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    """
                    UPDATE topics
                    SET title = ?, obsidian_path = ?, source_chat = ?, vault_chat_name = ?
                    WHERE topic_id = 1
                    """,
                    (source_title, source_path, "Secret Chat", "Secret Chat"),
                )
                conn.execute(
                    """
                    UPDATE topics
                    SET title = ?, obsidian_path = ?, source_chat = ?, vault_chat_name = ?
                    WHERE topic_id = 2
                    """,
                    (
                        target_title,
                        target_path,
                        "Secret Target Chat",
                        "Secret Target Chat",
                    ),
                )
                conn.execute(
                    """
                    UPDATE events
                    SET title = ?, source_chat = ?, vault_chat_name = ?,
                        messages_excerpt = ?
                    WHERE topic_id = 1
                    """,
                    (source_title, "Secret Chat", "Secret Chat", "Secret Note Body"),
                )
                conn.execute(
                    "UPDATE topic_fts SET title = ? WHERE topic_id = 1",
                    (source_title,),
                )
                conn.execute(
                    "UPDATE topic_fts SET title = ? WHERE topic_id = 2",
                    (target_title,),
                )
                conn.commit()
            finally:
                conn.close()
        self.refresh_expectations(fixture)
        return {
            "source_title": source_title,
            "source_path": source_path,
            "target_title": target_title,
            "target_path": target_path,
            "body": "Secret Note Body",
        }

    def seed_cli_control_titles(self, fixture):
        source_title = "Control\tSource Title"
        target_title = "Control\tTarget Title"
        for path in (fixture["backup_db"], fixture["current_db"]):
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "UPDATE topics SET title = ? WHERE topic_id = 1",
                    (source_title,),
                )
                conn.execute(
                    "UPDATE topics SET title = ? WHERE topic_id = 2",
                    (target_title,),
                )
                conn.execute(
                    "UPDATE events SET title = ? WHERE topic_id = 1",
                    (source_title,),
                )
                conn.execute(
                    "UPDATE topic_fts SET title = ? WHERE topic_id = 1",
                    (source_title,),
                )
                conn.execute(
                    "UPDATE topic_fts SET title = ? WHERE topic_id = 2",
                    (target_title,),
                )
                conn.commit()
            finally:
                conn.close()
        self.refresh_expectations(fixture)

    def assert_cli_default_redacted(self, result, secrets):
        combined = result.stdout + result.stderr
        for secret in (
            secrets["source_title"],
            secrets["source_path"],
            secrets["target_title"],
            secrets["target_path"],
            secrets["body"],
            KNOWN_BROKEN_RELATION_REASON,
            "- updates:: [[",
        ):
            self.assertNotIn(secret, combined)

    def test_cli_preview_requires_explicit_paths_and_redacts(self):
        fixture = self.build_fixture(invalid_nonself=25, invalid_self=0)
        secrets = self.seed_cli_secrets(fixture)

        missing_arguments = self.run_cleanup_cli("preview", "--backup", secrets["source_path"])
        self.assertEqual(missing_arguments.returncode, 2)
        self.assertIn("cli_usage", missing_arguments.stderr)
        self.assert_cli_default_redacted(missing_arguments, secrets)

        os.mkdir(fixture["run_dir"], 0o700)
        existing_run = self.run_cleanup_cli(
            "preview",
            "--backup",
            fixture["backup_db"],
            "--db",
            fixture["current_db"],
            "--vault-root",
            fixture["vault_root"],
            "--obsidian-subdir",
            "关注推送",
            "--run-dir",
            fixture["run_dir"],
            "--generator-commit",
            "test-generator-commit",
        )
        self.assertEqual(existing_run.returncode, 1)
        self.assertIn("run_dir_exists", existing_run.stderr)
        self.assert_cli_default_redacted(existing_run, secrets)
        os.rmdir(fixture["run_dir"])

        self.materialize_backup_source(fixture)
        self.preview_fixture(fixture)
        sensitive_default = self.run_cleanup_cli(
            "status", "--run-dir", fixture["run_dir"], "--json", "--sensitive"
        )
        self.assertEqual(sensitive_default.returncode, 0, sensitive_default.stderr)
        sensitive_report = json.loads(sensitive_default.stdout)
        self.assertEqual(len(sensitive_report["examples"]), 5)
        self.assertEqual(
            set().union(*(example.keys() for example in sensitive_report["examples"])),
            {"relative_path", "source_title", "target_title"},
        )
        self.assertNotIn(secrets["body"], sensitive_default.stdout)
        self.assertNotIn(KNOWN_BROKEN_RELATION_REASON, sensitive_default.stdout)
        self.assertNotIn("- updates:: [[", sensitive_default.stdout)

        sensitive_max = self.run_cleanup_cli(
            "status",
            "--run-dir",
            fixture["run_dir"],
            "--json",
            "--sensitive",
            "--example-limit",
            "20",
        )
        self.assertEqual(sensitive_max.returncode, 0, sensitive_max.stderr)
        self.assertEqual(len(json.loads(sensitive_max.stdout)["examples"]), 20)

        with open(
            os.path.join(fixture["run_dir"], "state.json"), "rb"
        ) as handle:
            state_before = handle.read()
        for invalid_limit in ("-1", "21"):
            with self.subTest(invalid_limit=invalid_limit):
                invalid = self.run_cleanup_cli(
                    "status",
                    "--run-dir",
                    os.path.join(self.tmp_root, secrets["source_title"]),
                    "--json",
                    "--sensitive",
                    "--example-limit",
                    invalid_limit,
                )
                self.assertEqual(invalid.returncode, 2)
                self.assertEqual(json.loads(invalid.stdout)["errors"], ["example_limit"])
                self.assert_cli_default_redacted(invalid, secrets)
                with open(
                    os.path.join(fixture["run_dir"], "state.json"), "rb"
                ) as handle:
                    self.assertEqual(handle.read(), state_before)

        caught_errors = (
            (CleanupError("fixture_cleanup", secrets["body"]), "fixture_cleanup"),
            (sqlite3.OperationalError(secrets["body"]), "sqlite_error"),
            (OSError(secrets["body"]), "os_error"),
            (ValueError(secrets["body"]), "value_error"),
            (
                json.JSONDecodeError(secrets["body"], secrets["body"], 0),
                "json_error",
            ),
        )
        for error, expected_code in caught_errors:
            with self.subTest(expected_code=expected_code):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch.object(cleanup_cli, "status_cleanup", side_effect=error),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = cleanup_cli.main(
                        [
                            "status",
                            "--run-dir",
                            secrets["source_path"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 1)
                self.assertEqual(json.loads(stdout.getvalue())["errors"], [expected_code])
                self.assertNotIn(secrets["body"], stdout.getvalue() + stderr.getvalue())

    def test_cli_apply_status_and_rollback(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        secrets = self.seed_cli_secrets(fixture)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        manifest_sha256 = preview["manifest_sha256"]

        status_text = self.run_cleanup_cli("status", "--run-dir", fixture["run_dir"])
        self.assertEqual(status_text.returncode, 0, status_text.stderr)
        self.assertIn("state: planned", status_text.stdout)
        self.assert_cli_default_redacted(status_text, secrets)

        wrong_token = self.run_cleanup_cli(
            "apply",
            "--run-dir",
            fixture["run_dir"],
            "--manifest-sha256",
            manifest_sha256,
            "--confirm",
            f"wrong-{secrets['source_title']}",
        )
        self.assertEqual(wrong_token.returncode, 1)
        self.assertIn("apply_confirmation", wrong_token.stderr)
        self.assert_cli_default_redacted(wrong_token, secrets)
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

        applied = self.run_cleanup_cli(
            "apply",
            "--run-dir",
            fixture["run_dir"],
            "--manifest-sha256",
            manifest_sha256,
            "--confirm",
            f"APPLY_EXACT_RELATION_MARKDOWN:{manifest_sha256}",
            "--json",
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        applied_report = json.loads(applied.stdout)
        self.assertEqual(applied_report["state"], "applied")
        self.assertEqual(applied_report["applied_this_invocation"], 1)
        self.assertLessEqual(
            set(applied_report),
            {
                "applicable",
                "state",
                "manifest_sha256",
                "selected_count",
                "self_loop_count",
                "renderable_count",
                "source_file_count",
                "exact_match_count",
                "relation_count",
                "relation_set_digest",
                "risky_warning_count",
                "risky_warning_set_digest",
                "applied_this_invocation",
                "already_clean",
                "pending",
                "restored",
                "drifted",
                "errors",
            },
        )
        self.assert_cli_default_redacted(applied, secrets)

        status_json = self.run_cleanup_cli(
            "status", "--run-dir", fixture["run_dir"], "--json"
        )
        self.assertEqual(status_json.returncode, 0, status_json.stderr)
        self.assertEqual(json.loads(status_json.stdout)["state"], "applied")
        self.assert_cli_default_redacted(status_json, secrets)

        rolled_back = self.run_cleanup_cli(
            "rollback",
            "--run-dir",
            fixture["run_dir"],
            "--manifest-sha256",
            manifest_sha256,
            "--confirm",
            f"ROLLBACK_EXACT_RELATION_MARKDOWN:{manifest_sha256}",
        )
        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assertIn("state: rolled_back", rolled_back.stdout)
        self.assert_cli_default_redacted(rolled_back, secrets)
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

    def test_cli_rejects_abbreviated_options_before_engine_call(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        secrets = self.seed_cli_secrets(fixture)
        digest = "a" * 64
        cases = (
            (
                "preview",
                [
                    "preview",
                    "--back",
                    secrets["source_path"],
                    "--db",
                    secrets["target_path"],
                    "--vault-root",
                    secrets["body"],
                    "--obsidian-subdir",
                    "关注推送",
                    "--run-dir",
                    fixture["run_dir"],
                    "--generator-commit",
                    "secret-generator",
                ],
            ),
            (
                "status",
                ["status", "--run-d", secrets["source_path"]],
            ),
            (
                "apply",
                [
                    "apply",
                    "--run-dir",
                    secrets["source_path"],
                    "--manifest-s",
                    digest,
                    "--confirm",
                    f"secret-confirm-{secrets['body']}",
                ],
            ),
            (
                "rollback",
                [
                    "rollback",
                    "--run-dir",
                    secrets["source_path"],
                    "--manifest-sha256",
                    digest,
                    "--con",
                    f"secret-confirm-{secrets['body']}",
                ],
            ),
        )
        for command, arguments in cases:
            with self.subTest(command=command, surface="subprocess"):
                result = self.run_cleanup_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("cli_usage", result.stderr)
                self.assert_cli_default_redacted(result, secrets)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(
                    cleanup_cli,
                    "preview_cleanup",
                    return_value={"applicable": True},
                ) as preview_mock,
                patch.object(
                    cleanup_cli,
                    "status_cleanup",
                    return_value={"state": "drifted", "drifted": 1},
                ) as status_mock,
                patch.object(
                    cleanup_cli,
                    "apply_cleanup",
                    return_value={"state": "applied"},
                ) as apply_mock,
                patch.object(
                    cleanup_cli,
                    "rollback_cleanup",
                    return_value={"state": "rolled_back"},
                ) as rollback_mock,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = cleanup_cli.main(arguments)
            with self.subTest(command=command, surface="mocked"):
                self.assertEqual(exit_code, 2)
                self.assertIn("cli_usage", stderr.getvalue())
                self.assertNotIn(secrets["body"], stdout.getvalue() + stderr.getvalue())
                preview_mock.assert_not_called()
                status_mock.assert_not_called()
                apply_mock.assert_not_called()
                rollback_mock.assert_not_called()

    def test_cli_public_report_schema_refuses_malformed_values(self):
        secret = "Secret Public Report Payload"
        malformed_reports = (
            {"applicable": secret},
            {"state": secret},
            {"manifest_sha256": secret},
            {"relation_set_digest": {"nested": secret}},
            {"selected_count": True},
            {"selected_count": -1},
            {"selected_count": {"nested": secret}},
            {"errors": {"nested": secret}},
            {"errors": [secret]},
        )
        for index, report in enumerate(malformed_reports):
            for as_json in (False, True):
                with self.subTest(index=index, as_json=as_json):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    arguments = ["status", "--run-dir", secret]
                    if as_json:
                        arguments.append("--json")
                    with (
                        patch.object(
                            cleanup_cli,
                            "status_cleanup",
                            return_value=report,
                        ) as status_mock,
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        exit_code = cleanup_cli.main(arguments)
                    combined = stdout.getvalue() + stderr.getvalue()
                    self.assertEqual(exit_code, 1)
                    self.assertIn("public_report_schema", combined)
                    self.assertNotIn(secret, combined)
                    status_mock.assert_called_once()

        self.assertEqual(
            cleanup_cli.public_report({"errors": ["destination_drift"]}),
            {"errors": ["destination_drift"]},
        )

        for as_json in (False, True):
            with self.subTest(state="drifted", as_json=as_json):
                stdout = io.StringIO()
                stderr = io.StringIO()
                arguments = ["status", "--run-dir", "sealed-run"]
                if as_json:
                    arguments.append("--json")
                with (
                    patch.object(
                        cleanup_cli,
                        "status_cleanup",
                        return_value={"state": "drifted", "drifted": 1},
                    ),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = cleanup_cli.main(arguments)
                self.assertEqual(exit_code, 0)
                self.assertIn("drifted", stdout.getvalue())

    def test_cli_sensitive_examples_require_flat_control_safe_strings(self):
        secret = "Secret Nested Example"
        malformed_examples = (
            [
                {
                    "relative_path": "safe.md",
                    "source_title": [secret],
                    "target_title": "Safe target",
                }
            ],
            [
                {
                    "relative_path": "safe.md",
                    "source_title": "Safe source",
                    "target_title": "Safe target",
                    "extra": {"nested": secret},
                }
            ],
            [
                {
                    "relative_path": "safe.md",
                    "source_title": "Safe source\n" + secret,
                    "target_title": "Safe target",
                }
            ],
        )
        for index, examples in enumerate(malformed_examples):
            for as_json in (False, True):
                with self.subTest(index=index, as_json=as_json):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    arguments = [
                        "status",
                        "--run-dir",
                        "sealed-run",
                        "--sensitive",
                    ]
                    if as_json:
                        arguments.append("--json")
                    with (
                        patch.object(
                            cleanup_cli,
                            "status_cleanup",
                            return_value={"state": "planned"},
                        ),
                        patch.object(
                            cleanup_cli,
                            "_sensitive_examples",
                            return_value=examples,
                        ),
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        exit_code = cleanup_cli.main(arguments)
                    combined = stdout.getvalue() + stderr.getvalue()
                    self.assertEqual(exit_code, 1)
                    self.assertIn("public_report_schema", combined)
                    self.assertNotIn(secret, combined)

    def test_cli_sensitive_mutations_preflight_control_examples_before_writes(self):
        for command in ("apply", "rollback"):
            with self.subTest(command=command):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                self.seed_cli_control_titles(fixture)
                source_path, _source_preimage = self.materialize_backup_source(fixture)
                preview = self.preview_fixture(fixture)
                digest = preview["manifest_sha256"]
                if command == "rollback":
                    apply_cleanup(
                        fixture["run_dir"],
                        digest,
                        f"APPLY_EXACT_RELATION_MARKDOWN:{digest}",
                    )
                source_before = self.digest(source_path)
                state_path = os.path.join(fixture["run_dir"], "state.json")
                state_before = self.digest(state_path)
                backup_db_before = self.digest(fixture["backup_db"])
                current_db_before = self.digest(fixture["current_db"])
                token_prefix = (
                    "APPLY_EXACT_RELATION_MARKDOWN:"
                    if command == "apply"
                    else "ROLLBACK_EXACT_RELATION_MARKDOWN:"
                )

                result = self.run_cleanup_cli(
                    command,
                    "--run-dir",
                    fixture["run_dir"],
                    "--manifest-sha256",
                    digest,
                    "--confirm",
                    f"{token_prefix}{digest}",
                    "--json",
                    "--sensitive",
                )

                self.assertEqual(result.returncode, 1)
                self.assertEqual(
                    json.loads(result.stdout)["errors"], ["public_report_schema"]
                )
                self.assertEqual(self.digest(source_path), source_before)
                self.assertEqual(self.digest(state_path), state_before)
                self.assertEqual(self.digest(fixture["backup_db"]), backup_db_before)
                self.assertEqual(self.digest(fixture["current_db"]), current_db_before)
                if command == "apply":
                    self.assertFalse(
                        os.path.lexists(os.path.join(fixture["run_dir"], "backups"))
                    )
                    self.assertFalse(
                        os.path.lexists(os.path.join(fixture["run_dir"], "staged"))
                    )

    def test_cli_sensitive_mutation_preflight_io_and_schema_failures_are_zero_write(self):
        invalid_examples = [
            {
                "relative_path": "safe.md",
                "source_title": "Control\tSource",
                "target_title": "Safe target",
            }
        ]
        cases = (
            (OSError("fixture sensitive read failure"), "os_error"),
            (invalid_examples, "public_report_schema"),
        )
        for command in ("apply", "rollback"):
            for sensitive_result, error_code in cases:
                with self.subTest(command=command, error_code=error_code):
                    fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                    source_path, _source_preimage = self.materialize_backup_source(fixture)
                    preview = self.preview_fixture(fixture)
                    digest = preview["manifest_sha256"]
                    if command == "rollback":
                        apply_cleanup(
                            fixture["run_dir"],
                            digest,
                            f"APPLY_EXACT_RELATION_MARKDOWN:{digest}",
                        )
                    source_before = self.digest(source_path)
                    state_path = os.path.join(fixture["run_dir"], "state.json")
                    state_before = self.digest(state_path)
                    backup_db_before = self.digest(fixture["backup_db"])
                    current_db_before = self.digest(fixture["current_db"])
                    token_prefix = (
                        "APPLY_EXACT_RELATION_MARKDOWN:"
                        if command == "apply"
                        else "ROLLBACK_EXACT_RELATION_MARKDOWN:"
                    )
                    arguments = [
                        command,
                        "--run-dir",
                        fixture["run_dir"],
                        "--manifest-sha256",
                        digest,
                        "--confirm",
                        f"{token_prefix}{digest}",
                        "--json",
                        "--sensitive",
                    ]
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    if isinstance(sensitive_result, BaseException):
                        sensitive_patch = patch.object(
                            cleanup_cli,
                            "_sensitive_examples",
                            side_effect=sensitive_result,
                        )
                    else:
                        sensitive_patch = patch.object(
                            cleanup_cli,
                            "_sensitive_examples",
                            return_value=sensitive_result,
                        )
                    with (
                        sensitive_patch,
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        exit_code = cleanup_cli.main(arguments)

                    self.assertEqual(exit_code, 1)
                    self.assertEqual(
                        json.loads(stdout.getvalue())["errors"], [error_code]
                    )
                    self.assertEqual(self.digest(source_path), source_before)
                    self.assertEqual(self.digest(state_path), state_before)
                    self.assertEqual(
                        self.digest(fixture["backup_db"]), backup_db_before
                    )
                    self.assertEqual(
                        self.digest(fixture["current_db"]), current_db_before
                    )

    def test_cli_sensitive_apply_and_rollback_output_bounded_cached_examples(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        digest = preview["manifest_sha256"]

        applied = self.run_cleanup_cli(
            "apply",
            "--run-dir",
            fixture["run_dir"],
            "--manifest-sha256",
            digest,
            "--confirm",
            f"APPLY_EXACT_RELATION_MARKDOWN:{digest}",
            "--json",
            "--sensitive",
            "--example-limit",
            "1",
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        applied_report = json.loads(applied.stdout)
        self.assertEqual(applied_report["state"], "applied")
        self.assertEqual(len(applied_report["examples"]), 1)
        self.assertEqual(
            set(applied_report["examples"][0]), cleanup_cli.EXAMPLE_KEYS
        )

        rolled_back = self.run_cleanup_cli(
            "rollback",
            "--run-dir",
            fixture["run_dir"],
            "--manifest-sha256",
            digest,
            "--confirm",
            f"ROLLBACK_EXACT_RELATION_MARKDOWN:{digest}",
            "--json",
            "--sensitive",
            "--example-limit",
            "1",
        )
        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        rolled_back_report = json.loads(rolled_back.stdout)
        self.assertEqual(rolled_back_report["state"], "rolled_back")
        self.assertEqual(len(rolled_back_report["examples"]), 1)
        self.assertEqual(
            set(rolled_back_report["examples"][0]), cleanup_cli.EXAMPLE_KEYS
        )

    def test_preview_seals_full_preimage_manifest(self):
        fixture = self.build_fixture(invalid_nonself=2, invalid_self=0)
        self.materialize_backup_source(fixture)

        result = preview_cleanup(
            backup_db=fixture["backup_db"],
            current_db=fixture["current_db"],
            vault_root=fixture["vault_root"],
            obsidian_subdir="关注推送",
            run_dir=fixture["run_dir"],
            generator_commit="test-generator-commit",
            expectations=fixture["expectations"],
        )

        self.assertTrue(result["applicable"])
        self.assertEqual(result["renderable_count"], 2)
        self.assertEqual(result["exact_match_count"], 2)
        manifest_path = os.path.join(fixture["run_dir"], "manifest.json")
        self.assertTrue(os.path.isfile(manifest_path))
        self.assertEqual(os.stat(fixture["run_dir"]).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(manifest_path).st_mode & 0o777, 0o600)

    def test_preview_accepts_exact_legacy_preimage_without_source_contract(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_bytes = self.materialize_backup_source(fixture)
        lines = source_bytes.splitlines(keepends=True)
        self.assertEqual(lines[0], b"---\n")
        self.assertTrue(lines[1].startswith(b"source_app: "))
        self.assertTrue(lines[5].startswith(b"generated_at: "))
        legacy_bytes = b"".join([lines[0], *lines[6:]])
        with open(source_path, "wb") as handle:
            handle.write(legacy_bytes)

        result = self.preview_fixture(fixture)

        self.assertTrue(result["applicable"])
        with open(
            os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8"
        ) as handle:
            manifest = json.load(handle)
        self.assertEqual(
            manifest["files"][0]["historical_preimage_sha256"],
            sha256_bytes(legacy_bytes),
        )
        self.assertEqual(
            manifest["files"][0]["historical_preimage_size"], len(legacy_bytes)
        )

    def test_preview_groups_multiple_edges_per_source(self):
        fixture = self.build_fixture(invalid_nonself=2, invalid_self=0)
        self.materialize_backup_source(fixture)

        result = preview_cleanup(
            backup_db=fixture["backup_db"],
            current_db=fixture["current_db"],
            vault_root=fixture["vault_root"],
            obsidian_subdir="关注推送",
            run_dir=fixture["run_dir"],
            generator_commit="test-generator-commit",
            expectations=fixture["expectations"],
        )

        with open(os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(result["unique_source_file_count"], 1)
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(len(manifest["files"][0]["edges"]), 2)
        self.assertIn("post_sha256", manifest["files"][0])
        self.assertFalse(
            any("post_sha256" in edge for edge in manifest["files"][0]["edges"])
        )

    def test_apply_refuses_wrong_token_without_writes(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = preview_cleanup(
            backup_db=fixture["backup_db"],
            current_db=fixture["current_db"],
            vault_root=fixture["vault_root"],
            obsidian_subdir="关注推送",
            run_dir=fixture["run_dir"],
            generator_commit="test-generator-commit",
            expectations=fixture["expectations"],
        )
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])

        cases = (
            (
                preview["manifest_sha256"],
                "APPLY_EXACT_RELATION_MARKDOWN:wrong",
            ),
            (
                "0" * 64,
                f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
            ),
        )
        for manifest_sha256, confirm in cases:
            with self.subTest(manifest_sha256=manifest_sha256, confirm=confirm):
                with self.assertRaises(CleanupError):
                    apply_cleanup(
                        fixture["run_dir"],
                        manifest_sha256,
                        confirm,
                    )
                self.assertFalse(
                    os.path.lexists(os.path.join(fixture["run_dir"], "backups"))
                )
                self.assertFalse(
                    os.path.lexists(os.path.join(fixture["run_dir"], "staged"))
                )
                with open(source_path, "rb") as handle:
                    self.assertEqual(handle.read(), source_before)
                self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
                self.assertEqual(self.digest(fixture["current_db"]), current_before)

    def test_apply_backs_up_stages_and_splices_exact_bytes(self):
        fixture = self.build_fixture(invalid_nonself=2, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = preview_cleanup(
            backup_db=fixture["backup_db"],
            current_db=fixture["current_db"],
            vault_root=fixture["vault_root"],
            obsidian_subdir="关注推送",
            run_dir=fixture["run_dir"],
            generator_commit="test-generator-commit",
            expectations=fixture["expectations"],
        )
        manifest_path = os.path.join(fixture["run_dir"], "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        file_record = manifest["files"][0]
        expected_postimage = splice_exact_relation_lines(
            source_before,
            [edge["rendered_line"].encode("utf-8") for edge in file_record["edges"]],
        )
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"

        result = apply_cleanup(
            fixture["run_dir"],
            preview["manifest_sha256"],
            token,
        )

        backup_path = os.path.join(
            fixture["run_dir"], "backups", file_record["relative_path"]
        )
        staged_path = os.path.join(
            fixture["run_dir"], "staged", file_record["relative_path"]
        )
        with open(backup_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)
        with open(staged_path, "rb") as handle:
            self.assertEqual(handle.read(), expected_postimage)
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), expected_postimage)
        self.assertEqual(os.stat(backup_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(staged_path).st_mode & 0o777, 0o600)
        self.assertEqual(self.digest(backup_path), file_record["pre_sha256"])
        self.assertEqual(self.digest(source_path), file_record["post_sha256"])
        with open(os.path.join(fixture["run_dir"], "state.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["state"], "applied")
        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["applied_this_invocation"], 1)
        self.assertEqual(result["already_clean"], 0)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["drifted"], 0)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)

    def test_apply_resumes_crashes_at_each_replace_boundary(self):
        expectations = {
            "before_replace": (1, 0),
            "after_replace": (0, 1),
            "after_ledger": (0, 1),
        }
        for phase, (expected_applied, expected_clean) in expectations.items():
            with self.subTest(phase=phase):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                source_path, source_before = self.materialize_backup_source(fixture)
                preview = self.preview_fixture(fixture)
                with open(
                    os.path.join(fixture["run_dir"], "manifest.json"),
                    encoding="utf-8",
                ) as handle:
                    file_record = json.load(handle)["files"][0]
                token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
                raised_once = False

                def crash_hook(current_phase, _relative_path):
                    nonlocal raised_once
                    if current_phase == phase and not raised_once:
                        raised_once = True
                        raise RuntimeError(f"fixture crash at {phase}")

                with self.assertRaisesRegex(RuntimeError, f"fixture crash at {phase}"):
                    apply_cleanup(
                        fixture["run_dir"],
                        preview["manifest_sha256"],
                        token,
                        fault_hook=crash_hook,
                    )

                with open(source_path, "rb") as handle:
                    crashed_bytes = handle.read()
                expected_crash_digest = (
                    file_record["pre_sha256"]
                    if phase == "before_replace"
                    else file_record["post_sha256"]
                )
                self.assertEqual(sha256_bytes(crashed_bytes), expected_crash_digest)

                resumed = apply_cleanup(
                    fixture["run_dir"],
                    preview["manifest_sha256"],
                    token,
                )

                self.assertEqual(resumed["state"], "applied")
                self.assertEqual(resumed["applied_this_invocation"], expected_applied)
                self.assertEqual(resumed["already_clean"], expected_clean)
                self.assertEqual(resumed["pending"], 0)
                self.assertEqual(resumed["drifted"], 0)
                with open(source_path, "rb") as handle:
                    self.assertEqual(
                        sha256_bytes(handle.read()), file_record["post_sha256"]
                    )
                if phase != "before_replace":
                    self.assertNotEqual(source_before, crashed_bytes)

    def test_apply_full_rerun_uses_disk_hashes_and_verified_backup(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        first = apply_cleanup(
            fixture["run_dir"], preview["manifest_sha256"], token
        )
        second = apply_cleanup(
            fixture["run_dir"], preview["manifest_sha256"], token
        )

        self.assertEqual(first["applied_this_invocation"], 1)
        self.assertEqual(second["applied_this_invocation"], 0)
        self.assertEqual(second["already_clean"], 1)
        self.assertEqual(second["pending"], 0)
        self.assertEqual(second["drifted"], 0)

    def test_status_derives_state_from_hashes(self):
        fixture, _sources = self.build_two_source_fixture()
        preview = self.preview_fixture(fixture)
        apply_cleanup(
            fixture["run_dir"],
            preview["manifest_sha256"],
            f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
        )
        state_path = os.path.join(fixture["run_dir"], "state.json")
        state_before = self.digest(state_path)

        result = status_cleanup(fixture["run_dir"])

        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["already_clean"], 2)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["drifted"], 0)
        self.assertEqual(self.digest(state_path), state_before)

    def test_rollback_restores_exact_preimages(self):
        fixture, sources = self.build_two_source_fixture()
        preview = self.preview_fixture(fixture)
        apply_cleanup(
            fixture["run_dir"],
            preview["manifest_sha256"],
            f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
        )
        backup_db_before = self.digest(fixture["backup_db"])
        current_db_before = self.digest(fixture["current_db"])

        result = rollback_cleanup(
            fixture["run_dir"],
            preview["manifest_sha256"],
            f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
        )

        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(result["restored"], 2)
        self.assertEqual(result["drifted"], 0)
        for source_path, source_preimage in sources:
            with open(source_path, "rb") as handle:
                self.assertEqual(handle.read(), source_preimage)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_db_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_db_before)

    def test_status_is_read_only_and_classifies_unknown_destination_bytes(self):
        fixture, sources, _preview, _manifest = self.build_applied_two_source_fixture()
        with open(sources[1][0], "wb") as handle:
            handle.write(b"fixture unknown destination bytes\n")
        run_before = self.vault_snapshot(fixture["run_dir"])
        vault_before = self.vault_snapshot(fixture["vault_root"])
        backup_db_before = self.digest(fixture["backup_db"])
        current_db_before = self.digest(fixture["current_db"])

        result = status_cleanup(fixture["run_dir"])

        self.assertEqual(result["already_clean"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["drifted"], 1)
        self.assertEqual(self.vault_snapshot(fixture["run_dir"]), run_before)
        self.assertEqual(self.vault_snapshot(fixture["vault_root"]), vault_before)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_db_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_db_before)
        self.assertNotIn("关注推送", repr(result))
        self.assertNotIn("Topic", repr(result))

    def test_status_refuses_tampered_backup_and_database_snapshot_without_writes(self):
        cases = ("backup", "database")
        for case in cases:
            with self.subTest(case=case):
                fixture, _sources, _preview, manifest = self.build_applied_two_source_fixture()
                if case == "backup":
                    backup_path = os.path.join(
                        fixture["run_dir"],
                        "backups",
                        manifest["files"][0]["relative_path"],
                    )
                    with open(backup_path, "wb") as handle:
                        handle.write(b"tampered markdown backup")
                    expected_code = "artifact_checksum"
                else:
                    conn = sqlite3.connect(fixture["current_db"])
                    try:
                        conn.execute(
                            "INSERT INTO relations VALUES (99, 1, 2, 'related', 'status drift', 99.0)"
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    expected_code = "database_snapshot_drift"
                run_before = self.vault_snapshot(fixture["run_dir"])
                vault_before = self.vault_snapshot(fixture["vault_root"])
                with self.assertRaises(CleanupError) as raised:
                    status_cleanup(fixture["run_dir"])
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(self.vault_snapshot(fixture["run_dir"]), run_before)
                self.assertEqual(self.vault_snapshot(fixture["vault_root"]), vault_before)

    def test_rollback_refuses_authorization_without_any_writes(self):
        fixture, sources, preview, _manifest = self.build_applied_two_source_fixture()
        state_path = os.path.join(fixture["run_dir"], "state.json")
        state_before = self.digest(state_path)
        source_before = [self.digest(path) for path, _preimage in sources]
        backup_db_before = self.digest(fixture["backup_db"])
        current_db_before = self.digest(fixture["current_db"])
        cases = (
            (
                "0" * 64,
                f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
                "manifest_digest",
            ),
            (
                preview["manifest_sha256"],
                "ROLLBACK_EXACT_RELATION_MARKDOWN:wrong",
                "rollback_confirmation",
            ),
        )
        for digest, token, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CleanupError) as raised:
                    rollback_cleanup(fixture["run_dir"], digest, token)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.digest(state_path), state_before)
                self.assertEqual(
                    [self.digest(path) for path, _preimage in sources], source_before
                )
                self.assertEqual(self.digest(fixture["backup_db"]), backup_db_before)
                self.assertEqual(self.digest(fixture["current_db"]), current_db_before)

    def test_rollback_preflights_all_backups_and_destinations_before_source_writes(self):
        cases = ("missing-backup", "tampered-backup", "later-drift")
        for case in cases:
            with self.subTest(case=case):
                fixture, sources, preview, manifest = self.build_applied_two_source_fixture()
                if case == "missing-backup":
                    os.unlink(
                        os.path.join(
                            fixture["run_dir"],
                            "backups",
                            manifest["files"][1]["relative_path"],
                        )
                    )
                    expected_code = "artifact_missing"
                elif case == "tampered-backup":
                    with open(
                        os.path.join(
                            fixture["run_dir"],
                            "backups",
                            manifest["files"][1]["relative_path"],
                        ),
                        "wb",
                    ) as handle:
                        handle.write(b"tampered backup bytes")
                    expected_code = "artifact_checksum"
                else:
                    with open(sources[1][0], "wb") as handle:
                        handle.write(b"later destination drift\n")
                    expected_code = "destination_drift"
                state_path = os.path.join(fixture["run_dir"], "state.json")
                state_before = self.digest(state_path)
                source_before = [self.digest(path) for path, _preimage in sources]
                with self.assertRaises(CleanupError) as raised:
                    rollback_cleanup(
                        fixture["run_dir"],
                        preview["manifest_sha256"],
                        f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
                    )
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(self.digest(state_path), state_before)
                self.assertEqual(
                    [self.digest(path) for path, _preimage in sources], source_before
                )

    def test_rollback_repreflights_all_destinations_after_rolling_back_ledger(self):
        fixture, sources, preview, manifest = self.build_applied_two_source_fixture()
        from core import relation_markdown_cleanup as cleanup_module

        real_write_state = cleanup_module._write_state
        drifted_bytes = b"fixture drift after rolling-back ledger\n"
        injected = False

        def drift_after_rolling_back_state(run_dir, state, run_identity):
            nonlocal injected
            real_write_state(run_dir, state, run_identity)
            if state["state"] == "rolling_back" and not injected:
                injected = True
                with open(sources[1][0], "wb") as handle:
                    handle.write(drifted_bytes)

        backup_db_before = self.digest(fixture["backup_db"])
        current_db_before = self.digest(fixture["current_db"])
        with patch(
            "core.relation_markdown_cleanup._write_state",
            side_effect=drift_after_rolling_back_state,
        ):
            with self.assertRaises(CleanupError) as raised:
                rollback_cleanup(
                    fixture["run_dir"],
                    preview["manifest_sha256"],
                    f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
                )

        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "destination_drift")
        self.assertEqual(
            self.digest(sources[0][0]), manifest["files"][0]["post_sha256"]
        )
        with open(sources[1][0], "rb") as handle:
            self.assertEqual(handle.read(), drifted_bytes)
        with open(
            os.path.join(fixture["run_dir"], "state.json"), encoding="utf-8"
        ) as handle:
            self.assertEqual(json.load(handle)["state"], "rolling_back")
        self.assertEqual(self.digest(fixture["backup_db"]), backup_db_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_db_before)

    def test_rollback_resumes_pre_post_mixture_after_crash(self):
        fixture, sources, preview, manifest = self.build_applied_two_source_fixture()
        first_path = manifest["files"][0]["relative_path"]
        crashed = False

        def crash_after_first_restore(phase, relative_path):
            nonlocal crashed
            if phase == "after_restore" and relative_path == first_path and not crashed:
                crashed = True
                raise RuntimeError("fixture rollback crash")

        with self.assertRaisesRegex(RuntimeError, "fixture rollback crash"):
            rollback_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
                fault_hook=crash_after_first_restore,
            )
        self.assertEqual(self.digest(sources[0][0]), manifest["files"][0]["pre_sha256"])
        self.assertEqual(self.digest(sources[1][0]), manifest["files"][1]["post_sha256"])
        with open(
            os.path.join(fixture["run_dir"], "state.json"), encoding="utf-8"
        ) as handle:
            self.assertEqual(json.load(handle)["state"], "rolling_back")

        resumed = rollback_cleanup(
            fixture["run_dir"],
            preview["manifest_sha256"],
            f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
        )

        self.assertEqual(resumed["state"], "rolled_back")
        self.assertEqual(resumed["restored"], 1)
        self.assertEqual(resumed["already_restored"], 1)
        for source_path, source_preimage in sources:
            with open(source_path, "rb") as handle:
                self.assertEqual(handle.read(), source_preimage)

    def test_rollback_rolled_back_rerun_is_idempotent(self):
        fixture, _sources, preview, _manifest = self.build_applied_two_source_fixture()
        token = f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        first = rollback_cleanup(
            fixture["run_dir"], preview["manifest_sha256"], token
        )
        state_path = os.path.join(fixture["run_dir"], "state.json")
        state_before = self.digest(state_path)
        second = rollback_cleanup(
            fixture["run_dir"], preview["manifest_sha256"], token
        )
        self.assertEqual(first["restored"], 2)
        self.assertEqual(second["restored"], 0)
        self.assertEqual(second["already_restored"], 2)
        self.assertEqual(second["state"], "rolled_back")
        self.assertEqual(self.digest(state_path), state_before)

    def test_status_reports_rolling_back_pre_post_mixture_without_writes(self):
        fixture, sources, preview, manifest = self.build_applied_two_source_fixture()
        first_path = manifest["files"][0]["relative_path"]

        def crash_after_first_restore(phase, relative_path):
            if phase == "after_restore" and relative_path == first_path:
                raise RuntimeError("fixture status crash")

        with self.assertRaisesRegex(RuntimeError, "fixture status crash"):
            rollback_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
                fault_hook=crash_after_first_restore,
            )
        run_before = self.vault_snapshot(fixture["run_dir"])
        vault_before = self.vault_snapshot(fixture["vault_root"])

        result = status_cleanup(fixture["run_dir"])

        self.assertEqual(result["state"], "rolling_back")
        self.assertEqual(result["preimage_count"], 1)
        self.assertEqual(result["postimage_count"], 1)
        self.assertEqual(result["drifted"], 0)
        self.assertEqual(self.vault_snapshot(fixture["run_dir"]), run_before)
        self.assertEqual(self.vault_snapshot(fixture["vault_root"]), vault_before)
        self.assertEqual(self.digest(sources[0][0]), manifest["files"][0]["pre_sha256"])
        self.assertEqual(self.digest(sources[1][0]), manifest["files"][1]["post_sha256"])

    def test_rollback_refuses_unsafe_backup_artifacts_without_writes(self):
        for case, expected_code in (
            ("mode", "artifact_mode"),
            ("hardlink", "artifact_mode"),
            ("symlink", "artifact_path"),
        ):
            with self.subTest(case=case):
                fixture, sources, preview, manifest = self.build_applied_two_source_fixture()
                backup_path = os.path.join(
                    fixture["run_dir"],
                    "backups",
                    manifest["files"][1]["relative_path"],
                )
                if case == "mode":
                    os.chmod(backup_path, 0o644)
                elif case == "hardlink":
                    os.link(backup_path, os.path.join(self.tmp_root, "external-link"))
                else:
                    os.unlink(backup_path)
                    os.symlink(os.path.join(self.tmp_root, "missing-target"), backup_path)
                state_path = os.path.join(fixture["run_dir"], "state.json")
                state_before = self.digest(state_path)
                source_before = [self.digest(path) for path, _preimage in sources]

                with self.assertRaises(CleanupError) as raised:
                    rollback_cleanup(
                        fixture["run_dir"],
                        preview["manifest_sha256"],
                        f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
                    )

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(self.digest(state_path), state_before)
                self.assertEqual(
                    [self.digest(path) for path, _preimage in sources], source_before
                )

    def test_rollback_refuses_database_snapshot_drift_without_writes(self):
        fixture, sources, preview, _manifest = self.build_applied_two_source_fixture()
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (99, 1, 2, 'related', 'rollback drift', 99.0)"
            )
            conn.commit()
        finally:
            conn.close()
        state_path = os.path.join(fixture["run_dir"], "state.json")
        state_before = self.digest(state_path)
        source_before = [self.digest(path) for path, _preimage in sources]

        with self.assertRaises(CleanupError) as raised:
            rollback_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
            )

        self.assertEqual(raised.exception.code, "database_snapshot_drift")
        self.assertEqual(self.digest(state_path), state_before)
        self.assertEqual(
            [self.digest(path) for path, _preimage in sources], source_before
        )

    def test_rollback_requires_final_all_preimage_barrier(self):
        fixture, sources, preview, manifest = self.build_applied_two_source_fixture()
        final_path = manifest["files"][-1]["relative_path"]
        changed = False

        def drift_after_final_ledger(phase, relative_path):
            nonlocal changed
            if phase == "after_ledger" and relative_path == final_path and not changed:
                changed = True
                with open(sources[0][0], "wb") as handle:
                    handle.write(b"fixture post-restore drift\n")

        with self.assertRaises(CleanupError) as raised:
            rollback_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
                fault_hook=drift_after_final_ledger,
            )

        self.assertEqual(raised.exception.code, "rollback_verify")
        with open(
            os.path.join(fixture["run_dir"], "state.json"), encoding="utf-8"
        ) as handle:
            self.assertEqual(json.load(handle)["state"], "rolling_back")
        self.assertNotEqual(self.digest(sources[0][0]), manifest["files"][0]["pre_sha256"])
        self.assertEqual(self.digest(sources[1][0]), manifest["files"][1]["pre_sha256"])

    def test_apply_refuses_rolled_back_run_without_writes(self):
        fixture, sources, preview, _manifest = self.build_applied_two_source_fixture()
        rollback_cleanup(
            fixture["run_dir"],
            preview["manifest_sha256"],
            f"ROLLBACK_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
        )
        state_path = os.path.join(fixture["run_dir"], "state.json")
        state_before = self.digest(state_path)
        source_before = [self.digest(path) for path, _preimage in sources]

        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}",
            )

        self.assertEqual(raised.exception.code, "apply_state")
        self.assertEqual(self.digest(state_path), state_before)
        self.assertEqual(
            [self.digest(path) for path, _preimage in sources], source_before
        )

    def test_state_restored_evidence_is_valid_only_for_rollback_states(self):
        fixture, _sources, _preview, manifest = self.build_applied_two_source_fixture()
        state_path = os.path.join(fixture["run_dir"], "state.json")
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        record = manifest["files"][0]
        state["files"][record["relative_path"]] = {
            "state": "restored",
            "pre_sha256": record["pre_sha256"],
        }
        atomic_write_private(state_path, canonical_json_bytes(state), 0o600)
        with self.assertRaises(CleanupError) as raised:
            load_sealed_run(fixture["run_dir"])
        self.assertEqual(raised.exception.code, "state_schema")

    def test_apply_refuses_unexpected_resume_artifacts_before_source_writes(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        state_path = os.path.join(fixture["run_dir"], "state.json")
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        state["state"] = "backing_up"
        with open(state_path, "wb") as handle:
            handle.write(canonical_json_bytes(state))
        os.chmod(state_path, 0o600)
        unexpected = os.path.join(fixture["run_dir"], "backups", "unexpected.md")
        os.makedirs(os.path.dirname(unexpected), mode=0o700)
        with open(unexpected, "wb") as handle:
            handle.write(b"unexpected")
        os.chmod(unexpected, 0o600)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"

        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"], preview["manifest_sha256"], token
            )

        self.assertEqual(raised.exception.code, "artifact_unexpected")
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)
        self.assertFalse(os.path.lexists(os.path.join(fixture["run_dir"], "staged")))

    def test_load_sealed_run_rejects_unexpected_root_and_empty_artifact_directories(self):
        root_fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(root_fixture)
        self.preview_fixture(root_fixture)
        root_marker = os.path.join(root_fixture["run_dir"], "unexpected-root")
        with open(root_marker, "wb") as handle:
            handle.write(b"unexpected root entry")
        os.chmod(root_marker, 0o600)
        with self.assertRaises(CleanupError) as raised:
            load_sealed_run(root_fixture["run_dir"])
        self.assertEqual(raised.exception.code, "artifact_unexpected")

        for tree in ("backups", "staged"):
            with self.subTest(tree=tree):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                self.materialize_backup_source(fixture)
                preview = self.preview_fixture(fixture)
                token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"

                def stop_after_artifacts(phase, _relative_path):
                    if phase == "before_replace":
                        raise RuntimeError("fixture stop after artifacts")

                with self.assertRaisesRegex(
                    RuntimeError, "fixture stop after artifacts"
                ):
                    apply_cleanup(
                        fixture["run_dir"],
                        preview["manifest_sha256"],
                        token,
                        fault_hook=stop_after_artifacts,
                    )
                empty = os.path.join(fixture["run_dir"], tree, "unrelated-empty")
                os.mkdir(empty, 0o700)

                with self.assertRaises(CleanupError) as raised:
                    load_sealed_run(fixture["run_dir"])
                self.assertEqual(raised.exception.code, "artifact_unexpected")

    def test_apply_rechecks_destination_after_before_replace_hook(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, _source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        drifted_bytes = b"concurrent destination rewrite"

        def drift_before_replace(phase, _relative_path):
            if phase == "before_replace":
                with open(source_path, "wb") as handle:
                    handle.write(drifted_bytes)

        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                token,
                fault_hook=drift_before_replace,
            )

        self.assertEqual(raised.exception.code, "destination_drift")
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), drifted_bytes)

    def test_apply_reads_staged_artifact_through_pinned_no_follow_descriptor(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"

        def crash_before_replace(phase, _relative_path):
            if phase == "before_replace":
                raise RuntimeError("fixture stop after staging")

        with self.assertRaisesRegex(RuntimeError, "fixture stop after staging"):
            apply_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                token,
                fault_hook=crash_before_replace,
            )
        with open(
            os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8"
        ) as handle:
            record = json.load(handle)["files"][0]
        staged_path = os.path.join(
            fixture["run_dir"], "staged", record["relative_path"]
        )
        external_path = os.path.join(self.tmp_root, "external-staged.md")
        shutil.copyfile(staged_path, external_path)
        os.chmod(external_path, 0o600)
        real_lstat = os.lstat
        staged_checks = 0

        def substitute_after_final_staged_lstat(path):
            nonlocal staged_checks
            info = real_lstat(path)
            if os.path.abspath(path) == os.path.abspath(staged_path):
                staged_checks += 1
                if staged_checks == 3:
                    os.unlink(staged_path)
                    os.symlink(external_path, staged_path)
            return info

        with patch(
            "core.relation_markdown_cleanup.os.lstat",
            side_effect=substitute_after_final_staged_lstat,
        ):
            with self.assertRaises(CleanupError) as raised:
                apply_cleanup(
                    fixture["run_dir"], preview["manifest_sha256"], token
                )

        self.assertIn(raised.exception.code, {"artifact_path", "artifact_mode"})
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

    def test_apply_preflights_all_destinations_before_creating_artifacts(self):
        fixture, sources = self.build_two_source_fixture()
        preview = self.preview_fixture(fixture)
        first_path, first_before = sources[0]
        second_path, _second_before = sources[1]
        with open(second_path, "ab") as handle:
            handle.write(b"drift")
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"

        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"], preview["manifest_sha256"], token
            )

        self.assertEqual(raised.exception.code, "destination_drift")
        self.assertFalse(os.path.lexists(os.path.join(fixture["run_dir"], "backups")))
        self.assertFalse(os.path.lexists(os.path.join(fixture["run_dir"], "staged")))
        with open(first_path, "rb") as handle:
            self.assertEqual(handle.read(), first_before)

    def test_apply_backup_and_stage_failures_leave_all_sources_unchanged(self):
        from core import relation_markdown_cleanup as cleanup_module

        for tree in ("backups", "staged"):
            with self.subTest(tree=tree):
                fixture, sources = self.build_two_source_fixture()
                preview = self.preview_fixture(fixture)
                token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
                real_write = cleanup_module.atomic_write_private
                failed = False

                def fail_first_tree_write(path, data, mode=0o600, **kwargs):
                    nonlocal failed
                    if f"{os.sep}{tree}{os.sep}" in path and not failed:
                        failed = True
                        raise CleanupError(
                            "fixture_artifact_write", "fixture artifact write failure"
                        )
                    return real_write(path, data, mode, **kwargs)

                with patch(
                    "core.relation_markdown_cleanup.atomic_write_private",
                    side_effect=fail_first_tree_write,
                ):
                    with self.assertRaises(CleanupError) as raised:
                        apply_cleanup(
                            fixture["run_dir"], preview["manifest_sha256"], token
                        )
                self.assertEqual(raised.exception.code, "fixture_artifact_write")
                for path, before in sources:
                    with open(path, "rb") as handle:
                        self.assertEqual(handle.read(), before)

                resumed = apply_cleanup(
                    fixture["run_dir"], preview["manifest_sha256"], token
                )
                self.assertEqual(resumed["applied_this_invocation"], 2)
                self.assertEqual(resumed["already_clean"], 0)

    def test_apply_second_file_failure_stops_and_resumes_mixed_state(self):
        fixture, sources = self.build_two_source_fixture()
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        before_replace_calls = 0

        def fail_second_file(phase, _relative_path):
            nonlocal before_replace_calls
            if phase == "before_replace":
                before_replace_calls += 1
                if before_replace_calls == 2:
                    raise RuntimeError("fixture second-file crash")

        with self.assertRaisesRegex(RuntimeError, "fixture second-file crash"):
            apply_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                token,
                fault_hook=fail_second_file,
            )

        with open(
            os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8"
        ) as handle:
            records = json.load(handle)["files"]
        by_path = {path: before for path, before in sources}
        for index, record in enumerate(records):
            path = os.path.join(
                fixture["vault_root"], *record["relative_path"].split("/")
            )
            with open(path, "rb") as handle:
                digest = sha256_bytes(handle.read())
            self.assertEqual(
                digest,
                record["post_sha256"] if index == 0 else record["pre_sha256"],
            )
            self.assertIn(path, by_path)

        resumed = apply_cleanup(
            fixture["run_dir"], preview["manifest_sha256"], token
        )
        self.assertEqual(resumed["applied_this_invocation"], 1)
        self.assertEqual(resumed["already_clean"], 1)
        self.assertEqual(resumed["pending"], 0)
        self.assertEqual(resumed["drifted"], 0)

    def test_apply_post_artifact_barrier_blocks_all_source_writes_on_later_drift(self):
        fixture, sources = self.build_two_source_fixture()
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        first_path, first_before = sources[0]
        second_path, _second_before = sources[1]
        drifted_bytes = b"later file drift after artifact barrier"
        from core import relation_markdown_cleanup as cleanup_module

        real_write_state = cleanup_module._write_state
        injected = False

        def drift_after_applying_state(run_dir, state, run_identity):
            nonlocal injected
            real_write_state(run_dir, state, run_identity)
            if state["state"] == "applying" and not injected:
                injected = True
                with open(second_path, "wb") as handle:
                    handle.write(drifted_bytes)

        with patch(
            "core.relation_markdown_cleanup._write_state",
            side_effect=drift_after_applying_state,
        ):
            with self.assertRaises(CleanupError) as raised:
                apply_cleanup(
                    fixture["run_dir"], preview["manifest_sha256"], token
                )

        self.assertEqual(raised.exception.code, "destination_drift")
        with open(first_path, "rb") as handle:
            self.assertEqual(handle.read(), first_before)
        with open(second_path, "rb") as handle:
            self.assertEqual(handle.read(), drifted_bytes)

    def test_apply_final_all_post_barrier_refuses_earlier_destination_drift(self):
        fixture, sources = self.build_two_source_fixture()
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        with open(
            os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8"
        ) as handle:
            manifest = json.load(handle)
        final_path = manifest["files"][-1]["relative_path"]
        drifted_bytes = b"fixture drift after final apply ledger\n"
        injected = False

        def drift_after_final_ledger(phase, relative_path):
            nonlocal injected
            if phase == "after_ledger" and relative_path == final_path and not injected:
                injected = True
                with open(sources[0][0], "wb") as handle:
                    handle.write(drifted_bytes)

        backup_db_before = self.digest(fixture["backup_db"])
        current_db_before = self.digest(fixture["current_db"])
        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                token,
                fault_hook=drift_after_final_ledger,
            )

        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "destination_drift")
        with open(sources[0][0], "rb") as handle:
            self.assertEqual(handle.read(), drifted_bytes)
        self.assertEqual(self.digest(sources[1][0]), manifest["files"][1]["post_sha256"])
        with open(
            os.path.join(fixture["run_dir"], "state.json"), encoding="utf-8"
        ) as handle:
            state = json.load(handle)
        self.assertEqual(state["state"], "drifted")
        self.assertIn("destination_drift", state["errors"])
        status = status_cleanup(fixture["run_dir"])
        self.assertEqual(status["state"], "drifted")
        self.assertEqual(status["pending"], 0)
        self.assertEqual(status["already_clean"], 1)
        self.assertEqual(status["drifted"], 1)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_db_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_db_before)

        state_before_rerun = self.digest(
            os.path.join(fixture["run_dir"], "state.json")
        )
        with self.assertRaises(CleanupError) as rerun:
            apply_cleanup(
                fixture["run_dir"], preview["manifest_sha256"], token
            )
        self.assertEqual(rerun.exception.code, "destination_drift")
        self.assertEqual(
            self.digest(os.path.join(fixture["run_dir"], "state.json")),
            state_before_rerun,
        )
        with open(sources[0][0], "rb") as handle:
            self.assertEqual(handle.read(), drifted_bytes)

    def test_apply_final_barrier_preserves_exact_preimage_for_resume(self):
        fixture, sources = self.build_two_source_fixture()
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        with open(
            os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8"
        ) as handle:
            manifest = json.load(handle)
        final_path = manifest["files"][-1]["relative_path"]
        injected = False

        def restore_preimage_after_final_ledger(phase, relative_path):
            nonlocal injected
            if phase == "after_ledger" and relative_path == final_path and not injected:
                injected = True
                with open(sources[0][0], "wb") as handle:
                    handle.write(sources[0][1])

        backup_db_before = self.digest(fixture["backup_db"])
        current_db_before = self.digest(fixture["current_db"])
        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                token,
                fault_hook=restore_preimage_after_final_ledger,
            )

        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "apply_incomplete")
        self.assertEqual(self.digest(sources[0][0]), manifest["files"][0]["pre_sha256"])
        self.assertEqual(self.digest(sources[1][0]), manifest["files"][1]["post_sha256"])
        with open(
            os.path.join(fixture["run_dir"], "state.json"), encoding="utf-8"
        ) as handle:
            first_state = json.load(handle)
        self.assertEqual(first_state["state"], "applying")
        self.assertNotIn("destination_drift", first_state["errors"])
        first_status = status_cleanup(fixture["run_dir"])
        self.assertEqual(first_status["state"], "applying")
        self.assertEqual(first_status["pending"], 1)
        self.assertEqual(first_status["already_clean"], 1)
        self.assertEqual(first_status["drifted"], 0)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_db_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_db_before)

        resumed = apply_cleanup(
            fixture["run_dir"], preview["manifest_sha256"], token
        )

        self.assertEqual(resumed["state"], "applied")
        self.assertEqual(resumed["applied_this_invocation"], 1)
        self.assertEqual(resumed["already_clean"], 1)
        self.assertEqual(resumed["pending"], 0)
        self.assertEqual(resumed["drifted"], 0)
        final_status = status_cleanup(fixture["run_dir"])
        self.assertEqual(final_status["state"], "applied")
        self.assertEqual(final_status["pending"], 0)
        self.assertEqual(final_status["already_clean"], 2)
        self.assertEqual(final_status["drifted"], 0)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_db_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_db_before)

    def test_apply_refuses_missing_or_mismatched_verified_backup(self):
        for mutation, expected_code in (
            ("missing", "artifact_missing"),
            ("mismatch", "artifact_checksum"),
        ):
            with self.subTest(mutation=mutation):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                source_path, source_before = self.materialize_backup_source(fixture)
                preview = self.preview_fixture(fixture)
                token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"

                def stop_after_staging(phase, _relative_path):
                    if phase == "before_replace":
                        raise RuntimeError("fixture staged")

                with self.assertRaisesRegex(RuntimeError, "fixture staged"):
                    apply_cleanup(
                        fixture["run_dir"],
                        preview["manifest_sha256"],
                        token,
                        fault_hook=stop_after_staging,
                    )
                with open(
                    os.path.join(fixture["run_dir"], "manifest.json"),
                    encoding="utf-8",
                ) as handle:
                    record = json.load(handle)["files"][0]
                backup_path = os.path.join(
                    fixture["run_dir"], "backups", record["relative_path"]
                )
                if mutation == "missing":
                    os.unlink(backup_path)
                else:
                    with open(backup_path, "wb") as handle:
                        handle.write(b"tampered backup")
                    os.chmod(backup_path, 0o600)

                with self.assertRaises(CleanupError) as raised:
                    apply_cleanup(
                        fixture["run_dir"], preview["manifest_sha256"], token
                    )
                self.assertEqual(raised.exception.code, expected_code)
                with open(source_path, "rb") as handle:
                    self.assertEqual(handle.read(), source_before)

    def test_apply_refuses_postimage_before_verified_backups_and_unknown_hash(self):
        for mutation, expected_code in (
            ("post", "postimage_before_backups_verified"),
            ("unknown", "destination_drift"),
        ):
            with self.subTest(mutation=mutation):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                source_path, source_before = self.materialize_backup_source(fixture)
                preview = self.preview_fixture(fixture)
                with open(
                    os.path.join(fixture["run_dir"], "manifest.json"),
                    encoding="utf-8",
                ) as handle:
                    record = json.load(handle)["files"][0]
                if mutation == "post":
                    changed = splice_exact_relation_lines(
                        source_before,
                        [
                            edge["rendered_line"].encode("utf-8")
                            for edge in record["edges"]
                        ],
                    )
                else:
                    changed = b"unknown destination bytes"
                with open(source_path, "wb") as handle:
                    handle.write(changed)
                token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"

                with self.assertRaises(CleanupError) as raised:
                    apply_cleanup(
                        fixture["run_dir"], preview["manifest_sha256"], token
                    )

                self.assertEqual(raised.exception.code, expected_code)
                self.assertFalse(
                    os.path.lexists(os.path.join(fixture["run_dir"], "backups"))
                )
                self.assertFalse(
                    os.path.lexists(os.path.join(fixture["run_dir"], "staged"))
                )
                with open(source_path, "rb") as handle:
                    self.assertEqual(handle.read(), changed)

    def test_apply_refuses_current_relation_snapshot_drift_without_artifacts(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (50, 2, 1, 'related', 'new relation', 50.0)"
            )
            conn.commit()
        finally:
            conn.close()
        current_after_drift = self.digest(fixture["current_db"])
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"

        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"], preview["manifest_sha256"], token
            )

        self.assertEqual(raised.exception.code, "database_snapshot_drift")
        self.assertEqual(self.digest(fixture["current_db"]), current_after_drift)
        self.assertFalse(os.path.lexists(os.path.join(fixture["run_dir"], "backups")))
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

    def test_load_sealed_run_refuses_modes_schema_digest_and_renderer_tamper(self):
        cases = ("run-mode", "manifest-mode", "state-schema", "manifest-digest", "renderer")
        for case in cases:
            with self.subTest(case=case):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                source_path, source_before = self.materialize_backup_source(fixture)
                preview = self.preview_fixture(fixture)
                manifest_path = os.path.join(fixture["run_dir"], "manifest.json")
                state_path = os.path.join(fixture["run_dir"], "state.json")
                if case == "run-mode":
                    os.chmod(fixture["run_dir"], 0o755)
                elif case == "manifest-mode":
                    os.chmod(manifest_path, 0o644)
                elif case == "state-schema":
                    with open(state_path, encoding="utf-8") as handle:
                        state = json.load(handle)
                    state["schema"] = "wrong/state"
                    atomic_write_private(state_path, canonical_json_bytes(state), 0o600)
                elif case == "manifest-digest":
                    with open(manifest_path, encoding="utf-8") as handle:
                        manifest = json.load(handle)
                    manifest["generator_commit"] = "tampered-generator"
                    atomic_write_private(
                        manifest_path, canonical_json_bytes(manifest), 0o600
                    )
                else:
                    with open(manifest_path, encoding="utf-8") as handle:
                        manifest = json.load(handle)
                    manifest["renderer_source_sha256"] = "0" * 64
                    new_digest = sha256_bytes(canonical_json_bytes(manifest))
                    with open(state_path, encoding="utf-8") as handle:
                        state = json.load(handle)
                    state["manifest_sha256"] = new_digest
                    atomic_write_private(
                        manifest_path, canonical_json_bytes(manifest), 0o600
                    )
                    atomic_write_private(state_path, canonical_json_bytes(state), 0o600)

                with self.assertRaises(CleanupError):
                    load_sealed_run(fixture["run_dir"])
                self.assertFalse(
                    os.path.lexists(os.path.join(fixture["run_dir"], "backups"))
                )
                with open(source_path, "rb") as handle:
                    self.assertEqual(handle.read(), source_before)

    def test_load_sealed_run_requires_exact_state_schema(self):
        cases = (
            "extra-top-level",
            "error-type",
            "error-code",
            "unknown-file",
            "planned-files",
            "file-extra",
            "file-state",
            "file-hash",
        )
        for case in cases:
            with self.subTest(case=case):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                self.materialize_backup_source(fixture)
                self.preview_fixture(fixture)
                manifest_path = os.path.join(fixture["run_dir"], "manifest.json")
                state_path = os.path.join(fixture["run_dir"], "state.json")
                with open(manifest_path, encoding="utf-8") as handle:
                    record = json.load(handle)["files"][0]
                with open(state_path, encoding="utf-8") as handle:
                    state = json.load(handle)
                valid_file_state = {
                    "state": "applied",
                    "post_sha256": record["post_sha256"],
                }
                if case == "extra-top-level":
                    state["unexpected"] = "value"
                elif case == "error-type":
                    state["errors"] = [123]
                elif case == "error-code":
                    state["errors"] = ["../not-a-stable-code"]
                elif case == "unknown-file":
                    state["files"] = {"unmanaged.md": valid_file_state}
                elif case == "planned-files":
                    state["files"] = {record["relative_path"]: valid_file_state}
                else:
                    state["state"] = "applying"
                    state["files"] = {
                        record["relative_path"]: dict(valid_file_state)
                    }
                    if case == "file-extra":
                        state["files"][record["relative_path"]]["unexpected"] = True
                    elif case == "file-state":
                        state["files"][record["relative_path"]]["state"] = "pending"
                    else:
                        state["files"][record["relative_path"]]["post_sha256"] = "0" * 64
                atomic_write_private(
                    state_path, canonical_json_bytes(state), 0o600
                )

                with self.assertRaises(CleanupError) as raised:
                    load_sealed_run(fixture["run_dir"])
                self.assertEqual(raised.exception.code, "state_schema")

    def test_apply_refuses_digest_bound_input_path_tamper_without_writes(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        manifest_path = os.path.join(fixture["run_dir"], "manifest.json")
        state_path = os.path.join(fixture["run_dir"], "state.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["inputs"]["current_db"] = manifest["inputs"]["backup_db"]
        manifest["current"]["path_sha256"] = sha256_bytes(
            manifest["inputs"]["current_db"].encode("utf-8")
        )
        tampered_digest = sha256_bytes(canonical_json_bytes(manifest))
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        state["manifest_sha256"] = tampered_digest
        atomic_write_private(manifest_path, canonical_json_bytes(manifest), 0o600)
        atomic_write_private(state_path, canonical_json_bytes(state), 0o600)
        original_token = (
            f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        )

        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"], preview["manifest_sha256"], original_token
            )

        self.assertEqual(raised.exception.code, "manifest_digest")
        self.assertNotIn(self.tmp_root, str(raised.exception))
        self.assertFalse(os.path.lexists(os.path.join(fixture["run_dir"], "backups")))
        self.assertFalse(os.path.lexists(os.path.join(fixture["run_dir"], "staged")))
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

    def test_apply_keeps_sealed_run_inode_pinned_across_state_and_artifact_writes(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        saved_run = fixture["run_dir"] + "-saved"
        marker_bytes = b"replacement run marker"
        from core import relation_markdown_cleanup as cleanup_module

        real_revalidate = cleanup_module._revalidate_database_evidence
        substituted = False

        def substitute_run_after_evidence(manifest):
            nonlocal substituted
            result = real_revalidate(manifest)
            if not substituted:
                substituted = True
                os.replace(fixture["run_dir"], saved_run)
                os.mkdir(fixture["run_dir"], 0o700)
                with open(
                    os.path.join(fixture["run_dir"], "marker"), "wb"
                ) as handle:
                    handle.write(marker_bytes)
            return result

        with patch(
            "core.relation_markdown_cleanup._revalidate_database_evidence",
            side_effect=substitute_run_after_evidence,
        ):
            with self.assertRaises(CleanupError) as raised:
                apply_cleanup(
                    fixture["run_dir"], preview["manifest_sha256"], token
                )

        self.assertEqual(raised.exception.code, "run_dir_identity")
        with open(os.path.join(fixture["run_dir"], "marker"), "rb") as handle:
            self.assertEqual(handle.read(), marker_bytes)
        self.assertEqual(os.listdir(fixture["run_dir"]), ["marker"])
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

    def test_apply_keeps_artifact_parent_inode_pinned_until_atomic_write(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        marker_bytes = b"replacement artifact parent marker"
        from core import relation_markdown_cleanup as cleanup_module

        real_ensure = cleanup_module._ensure_private_artifact_parent
        substituted = False
        replacement_parent = None

        def substitute_backup_parent(run_dir, tree, relative_path, run_identity):
            nonlocal substituted, replacement_parent
            artifact_path, parent_identity, created_directories = real_ensure(
                run_dir, tree, relative_path, run_identity
            )
            if tree == "backups" and not substituted:
                substituted = True
                original_parent = os.path.dirname(artifact_path)
                saved_parent = original_parent + "-saved"
                os.replace(original_parent, saved_parent)
                os.mkdir(original_parent, 0o700)
                replacement_parent = original_parent
                with open(os.path.join(original_parent, "marker"), "wb") as handle:
                    handle.write(marker_bytes)
            return artifact_path, parent_identity, created_directories

        with patch(
            "core.relation_markdown_cleanup._ensure_private_artifact_parent",
            side_effect=substitute_backup_parent,
        ):
            with self.assertRaises(CleanupError) as raised:
                apply_cleanup(
                    fixture["run_dir"], preview["manifest_sha256"], token
                )

        self.assertEqual(raised.exception.code, "artifact_path")
        self.assertIsNotNone(replacement_parent)
        with open(os.path.join(replacement_parent, "marker"), "rb") as handle:
            self.assertEqual(handle.read(), marker_bytes)
        self.assertEqual(os.listdir(replacement_parent), ["marker"])
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

    def test_apply_exclusively_publishes_first_time_artifact_leaf(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        with open(
            os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8"
        ) as handle:
            record = json.load(handle)["files"][0]
        backup_path = os.path.join(
            fixture["run_dir"], "backups", record["relative_path"]
        )
        backup_parent = os.path.dirname(backup_path)
        marker_bytes = b"raced-in exact backup leaf marker"
        marker_identity = None
        from core import relation_markdown_cleanup as cleanup_module

        real_revalidate = cleanup_module._revalidate_supplied_run_parent
        injected = False

        def inject_leaf_before_publish(parent_expression, expected_identity):
            nonlocal injected, marker_identity
            result = real_revalidate(parent_expression, expected_identity)
            if parent_expression == backup_parent and not injected:
                injected = True
                with open(backup_path, "xb") as handle:
                    handle.write(marker_bytes)
                os.chmod(backup_path, 0o600)
                marker = os.stat(backup_path)
                marker_identity = (marker.st_dev, marker.st_ino)
            return result

        with patch(
            "core.relation_markdown_cleanup._revalidate_supplied_run_parent",
            side_effect=inject_leaf_before_publish,
        ):
            with self.assertRaises(CleanupError) as raised:
                apply_cleanup(
                    fixture["run_dir"], preview["manifest_sha256"], token
                )

        self.assertEqual(raised.exception.code, "artifact_conflict")
        with open(backup_path, "rb") as handle:
            self.assertEqual(handle.read(), marker_bytes)
        marker = os.stat(backup_path)
        self.assertEqual((marker.st_dev, marker.st_ino), marker_identity)
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

    def test_apply_refuses_identical_destination_parent_substitution_before_replace(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        original_parent = os.path.dirname(source_path)
        saved_parent = original_parent + "-saved"
        marker_bytes = b"replacement topic parent marker"
        substituted = False

        def substitute_identical_parent(phase, _relative_path):
            nonlocal substituted
            if phase == "before_replace" and not substituted:
                substituted = True
                os.replace(original_parent, saved_parent)
                os.mkdir(original_parent, 0o755)
                with open(source_path, "wb") as handle:
                    handle.write(source_before)
                with open(os.path.join(original_parent, "marker"), "wb") as handle:
                    handle.write(marker_bytes)

        with self.assertRaises(CleanupError) as raised:
            apply_cleanup(
                fixture["run_dir"],
                preview["manifest_sha256"],
                token,
                fault_hook=substitute_identical_parent,
            )

        self.assertEqual(raised.exception.code, "topic_identity_race")
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)
        with open(os.path.join(original_parent, "marker"), "rb") as handle:
            self.assertEqual(handle.read(), marker_bytes)
        with open(
            os.path.join(fixture["run_dir"], "state.json"), encoding="utf-8"
        ) as handle:
            self.assertEqual(json.load(handle)["state"], "drifted")

    def test_apply_final_atomic_boundary_refuses_same_inode_preimage_rewrite(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, _source_before = self.materialize_backup_source(fixture)
        preview = self.preview_fixture(fixture)
        token = f"APPLY_EXACT_RELATION_MARKDOWN:{preview['manifest_sha256']}"
        source_identity = (os.stat(source_path).st_dev, os.stat(source_path).st_ino)
        parent_info = os.stat(os.path.dirname(source_path))
        source_parent_identity = (parent_info.st_dev, parent_info.st_ino)
        drifted_bytes = b"same inode rewrite inside final atomic boundary"
        real_stat = os.stat
        injected = False

        def rewrite_after_final_destination_stat(
            path, *, dir_fd=None, follow_symlinks=True
        ):
            nonlocal injected
            info = real_stat(
                path, dir_fd=dir_fd, follow_symlinks=follow_symlinks
            )
            parent_identity = None
            if dir_fd is not None:
                parent = os.fstat(dir_fd)
                parent_identity = (parent.st_dev, parent.st_ino)
            if (
                not injected
                and path == os.path.basename(source_path)
                and parent_identity == source_parent_identity
                and follow_symlinks is False
            ):
                injected = True
                with open(source_path, "wb") as handle:
                    handle.write(drifted_bytes)
                rewritten = real_stat(source_path)
                self.assertEqual(
                    (rewritten.st_dev, rewritten.st_ino), source_identity
                )
            return info

        with patch(
            "core.relation_markdown_cleanup.os.stat",
            side_effect=rewrite_after_final_destination_stat,
        ):
            with self.assertRaises(CleanupError) as raised:
                apply_cleanup(
                    fixture["run_dir"], preview["manifest_sha256"], token
                )

        self.assertEqual(raised.exception.code, "destination_drift")
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), drifted_bytes)
        with open(
            os.path.join(fixture["run_dir"], "state.json"), encoding="utf-8"
        ) as handle:
            self.assertEqual(json.load(handle)["state"], "drifted")

    def test_exact_splice_preserves_every_non_target_byte(self):
        stale = "- updates:: [[关注推送/目标|目标]]\n".encode("utf-8")
        data = b"prefix\xff\n## " + "相关主题\n".encode("utf-8") + stale + b"\n## next\nbody"
        postimage = splice_exact_relation_lines(data, [stale])
        self.assertEqual(
            postimage,
            b"prefix\xff\n## " + "相关主题\n".encode("utf-8") + b"\n## next\nbody",
        )
        self.assertEqual(relation_section_line_indexes(data), [2, 3])

    def test_exact_splice_refuses_missing_duplicate_outside_and_non_lf_lines(self):
        heading = "## 相关主题\n".encode("utf-8")
        line = "- updates:: [[关注推送/目标|目标]]\n".encode("utf-8")
        cases = {
            "relation_line_missing": heading + b"## next\n",
            "relation_line_duplicate": heading + line + line + b"## next\n",
            "relation_line_outside_section": line + heading + line + b"## next\n",
            "relation_line_nonphysical": heading + line.replace(b"\n", b"\r\n") + b"## next\n",
        }
        for code, data in cases.items():
            with self.subTest(code=code):
                expected = line.replace(b"\n", b"\r\n") if code == "relation_line_nonphysical" else line
                with self.assertRaises(CleanupError) as raised:
                    splice_exact_relation_lines(data, [expected])
                self.assertEqual(raised.exception.code, code)

    def test_relation_section_refuses_missing_duplicate_and_malformed_headings(self):
        exact = "## 相关主题\n".encode("utf-8")
        cases = {
            "relation_section_missing": b"## other\n",
            "relation_section_duplicate": exact + exact,
            "relation_section_malformed": "## 相关主题\r\n".encode("utf-8"),
        }
        for code, data in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(CleanupError) as raised:
                    relation_section_line_indexes(data)
                self.assertEqual(raised.exception.code, code)

    def test_read_file_bounded_restores_sigalrm_timer_and_handler(self):
        with patch("core.relation_markdown_cleanup.signal.getsignal", return_value="old-handler"), patch(
            "core.relation_markdown_cleanup.signal.getitimer", return_value=(4.5, 1.25)
        ), patch(
            "core.relation_markdown_cleanup.time.monotonic", side_effect=(100.0, 100.5)
        ), patch("core.relation_markdown_cleanup.signal.signal") as signal_call, patch(
            "core.relation_markdown_cleanup.signal.setitimer"
        ) as timer_call, patch("builtins.open", side_effect=TimeoutError("fixture timeout")):
            with self.assertRaises(CleanupError) as raised:
                read_file_bounded("fixture", timeout_seconds=0.5)
        self.assertEqual(raised.exception.code, "materialization_timeout")
        self.assertEqual(timer_call.call_args_list[0].args, (signal.ITIMER_REAL, 0.5))
        self.assertEqual(timer_call.call_args_list[-2].args, (signal.ITIMER_REAL, 0))
        self.assertEqual(timer_call.call_args_list[-1].args, (signal.ITIMER_REAL, 4.0, 1.25))
        self.assertEqual(signal_call.call_args_list[-1].args, (signal.SIGALRM, "old-handler"))

    def test_read_file_bounded_retries_handler_restore_and_restores_timer_after_failure(self):
        restore_attempts = 0

        def signal_side_effect(_signum, handler):
            nonlocal restore_attempts
            if handler == "old-handler":
                restore_attempts += 1
                if restore_attempts == 1:
                    raise OSError("fixture transient handler restore failure")

        with patch(
            "core.relation_markdown_cleanup.signal.getsignal",
            return_value="old-handler",
        ), patch(
            "core.relation_markdown_cleanup.signal.getitimer", return_value=(2.0, 0.5)
        ), patch(
            "core.relation_markdown_cleanup.time.monotonic", side_effect=(10.0, 10.25)
        ), patch(
            "core.relation_markdown_cleanup.signal.signal",
            side_effect=signal_side_effect,
        ) as signal_call, patch(
            "core.relation_markdown_cleanup.signal.setitimer"
        ) as timer_call, patch("builtins.open", unittest.mock.mock_open(read_data=b"fixture")):
            with self.assertRaises(CleanupError) as raised:
                read_file_bounded("fixture", timeout_seconds=1)
        self.assertEqual(raised.exception.code, "materialization_timer_restore")
        self.assertEqual(restore_attempts, 2)
        self.assertEqual(
            timer_call.call_args_list[-1].args,
            (signal.ITIMER_REAL, 1.75, 0.5),
        )
        self.assertEqual(
            [call.args for call in signal_call.call_args_list[-2:]],
            [(signal.SIGALRM, "old-handler"), (signal.SIGALRM, "old-handler")],
        )

    def test_read_file_bounded_preserves_real_prior_timer_deadline(self):
        class SlowFile:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                time.sleep(0.12)
                return b"fixture"

        original_handler = signal.getsignal(signal.SIGALRM)
        fired = []

        def prior_handler(_signum, _frame):
            fired.append(time.monotonic())

        try:
            signal.signal(signal.SIGALRM, prior_handler)
            signal.setitimer(signal.ITIMER_REAL, 0.25)
            with patch("builtins.open", return_value=SlowFile()):
                self.assertEqual(read_file_bounded("fixture", 1), b"fixture")
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertEqual(interval, 0.0)
            self.assertGreater(remaining, 0.0)
            self.assertLess(remaining, 0.18)
            self.assertFalse(fired)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original_handler)

    def test_read_file_bounded_delivers_prior_timer_that_expired_during_read(self):
        class SlowFile:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                time.sleep(0.08)
                return b"fixture"

        original_handler = signal.getsignal(signal.SIGALRM)
        fired = []

        def prior_handler(_signum, _frame):
            fired.append(time.monotonic())

        try:
            signal.signal(signal.SIGALRM, prior_handler)
            signal.setitimer(signal.ITIMER_REAL, 0.03)
            with patch("builtins.open", return_value=SlowFile()):
                self.assertEqual(read_file_bounded("fixture", 1), b"fixture")
            time.sleep(0.01)
            self.assertTrue(fired)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original_handler)

    def test_read_fd_bounded_refuses_when_reader_cannot_be_interrupted(self):
        script = r'''\
import os
import signal
import threading
from unittest.mock import patch

from core import relation_markdown_cleanup as cleanup_module

descriptor = os.open(os.devnull, os.O_RDONLY)
blocker = threading.Event()

def blocking_read(*_args):
    blocker.wait()
    return b""

try:
    with patch.object(cleanup_module.os, "read", side_effect=blocking_read), \
         patch.object(cleanup_module.signal, "signal"), \
         patch.object(cleanup_module.signal, "setitimer"), \
         patch.object(cleanup_module.signal, "getsignal", return_value=signal.SIG_DFL), \
         patch.object(cleanup_module.signal, "getitimer", return_value=(0.0, 0.0)):
        try:
            cleanup_module._read_fd_bounded(descriptor, timeout_seconds=0.05)
        except cleanup_module.CleanupError as exc:
            if exc.code != "materialization_timeout":
                raise
        else:
            raise AssertionError("blocking reader unexpectedly completed")
finally:
    os.close(descriptor)
'''
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

    def test_read_fd_bounded_maps_worker_start_failure_to_unreadable(self):
        from core import relation_markdown_cleanup as cleanup_module

        descriptor = os.open(os.devnull, os.O_RDONLY)
        try:
            with patch.object(
                cleanup_module.threading.Thread,
                "start",
                side_effect=RuntimeError("fixture worker start failure"),
            ):
                with self.assertRaises(CleanupError) as raised:
                    cleanup_module._read_fd_bounded(descriptor, timeout_seconds=0.05)
        finally:
            os.close(descriptor)
        self.assertEqual(raised.exception.code, "materialization_unreadable")

    def test_read_fd_bounded_does_not_modify_process_alarm_state(self):
        from core import relation_markdown_cleanup as cleanup_module

        descriptor = os.open(os.devnull, os.O_RDONLY)
        try:
            with patch.object(cleanup_module.signal, "signal") as signal_call, patch.object(
                cleanup_module.signal, "setitimer"
            ) as timer_call:
                self.assertEqual(
                    cleanup_module._read_fd_bounded(
                        descriptor, timeout_seconds=0.05
                    ),
                    b"",
                )
        finally:
            os.close(descriptor)
        signal_call.assert_not_called()
        timer_call.assert_not_called()

    def test_preview_refuses_parent_symlink_substitution_after_nominal_validation(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, preimage = self.materialize_backup_source(fixture)
        source_parent = os.path.dirname(source_path)
        saved_parent = source_parent + "-saved"
        outside_parent = os.path.join(self.tmp.name, "outside-candidate")
        os.makedirs(outside_parent)
        outside_path = os.path.join(outside_parent, os.path.basename(source_path))
        with open(outside_path, "wb") as handle:
            handle.write(preimage)
        outside_before = self.digest(outside_path)
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        real_validate = validate_topic_path
        swapped = False

        def validate_then_swap(*args, **kwargs):
            nonlocal swapped
            result = real_validate(*args, **kwargs)
            if not swapped:
                swapped = True
                os.rename(source_parent, saved_parent)
                os.symlink(outside_parent, source_parent)
            return result

        with patch(
            "core.relation_markdown_cleanup.validate_topic_path",
            side_effect=validate_then_swap,
        ):
            with self.assertRaises(CleanupError) as raised:
                preview_cleanup(
                    backup_db=fixture["backup_db"],
                    current_db=fixture["current_db"],
                    vault_root=fixture["vault_root"],
                    obsidian_subdir="关注推送",
                    run_dir=fixture["run_dir"],
                    generator_commit="test-generator-commit",
                    expectations=fixture["expectations"],
                )
        self.assertIn(raised.exception.code, {"topic_path_symlink", "topic_path_race"})
        self.assertFalse(os.path.lexists(fixture["run_dir"]))
        self.assertEqual(self.digest(outside_path), outside_before)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)

    def test_preview_refuses_parent_substitution_after_final_candidate_fd_open(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, _preimage = self.materialize_backup_source(fixture)
        source_parent = os.path.dirname(source_path)
        saved_parent = source_parent + "-after-open-saved"
        outside_parent = os.path.join(self.tmp.name, "outside-after-final-open")
        os.makedirs(outside_parent)
        outside_path = os.path.join(outside_parent, os.path.basename(source_path))
        with open(outside_path, "wb") as handle:
            handle.write(b"different outside bytes\n")
        outside_before = self.digest(outside_path)
        from core import relation_markdown_cleanup as cleanup_module

        real_fd_read = cleanup_module._read_fd_bounded
        swapped = False

        def swap_after_final_open(descriptor, timeout_seconds=30):
            nonlocal swapped
            if not swapped:
                swapped = True
                os.rename(source_parent, saved_parent)
                os.symlink(outside_parent, source_parent)
            return real_fd_read(descriptor, timeout_seconds)

        with patch(
            "core.relation_markdown_cleanup._read_fd_bounded",
            side_effect=swap_after_final_open,
        ):
            with self.assertRaises(CleanupError) as raised:
                preview_cleanup(
                    backup_db=fixture["backup_db"],
                    current_db=fixture["current_db"],
                    vault_root=fixture["vault_root"],
                    obsidian_subdir="关注推送",
                    run_dir=fixture["run_dir"],
                    generator_commit="test-generator-commit",
                    expectations=fixture["expectations"],
                )
        self.assertIn(
            raised.exception.code,
            {"topic_path_symlink", "topic_path_race", "topic_identity_race"},
        )
        self.assertFalse(os.path.lexists(fixture["run_dir"]))
        self.assertEqual(self.digest(outside_path), outside_before)

    def test_preview_exclusive_publish_preserves_marker_and_empty_replacements(self):
        real_mkdir = os.mkdir
        for replacement_kind in ("marker", "empty"):
            with self.subTest(replacement_kind=replacement_kind):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                self.materialize_backup_source(fixture)
                created_staging_paths = []
                replacement_identity = None
                injected = False

                def mkdir_with_publish_conflict(path, mode=0o777, *, dir_fd=None):
                    nonlocal replacement_identity, injected
                    real_mkdir(path, mode, dir_fd=dir_fd)
                    path_text = os.fspath(path)
                    old_final_creation = dir_fd is None and os.path.abspath(path_text) == os.path.abspath(fixture["run_dir"])
                    new_staging_creation = dir_fd is not None and path_text.startswith(".cleanup-run.staging-")
                    if injected or not (old_final_creation or new_staging_creation):
                        return
                    injected = True
                    if old_final_creation:
                        created_path = os.path.join(
                            self.tmp.name, f"old-created-{replacement_kind}"
                        )
                        os.rename(fixture["run_dir"], created_path)
                        created_staging_paths.append(created_path)
                    else:
                        created_staging_paths.append(
                            os.path.join(os.path.dirname(fixture["run_dir"]), path_text)
                        )
                    real_mkdir(fixture["run_dir"], 0o700)
                    replacement_identity = (
                        os.lstat(fixture["run_dir"]).st_dev,
                        os.lstat(fixture["run_dir"]).st_ino,
                    )
                    if replacement_kind == "marker":
                        with open(
                            os.path.join(fixture["run_dir"], "unrelated-marker"),
                            "wb",
                        ) as handle:
                            handle.write(b"preserve publication replacement")

                with patch(
                    "core.relation_markdown_cleanup.os.mkdir",
                    side_effect=mkdir_with_publish_conflict,
                ):
                    with self.assertRaises(CleanupError) as raised:
                        preview_cleanup(
                            backup_db=fixture["backup_db"],
                            current_db=fixture["current_db"],
                            vault_root=fixture["vault_root"],
                            obsidian_subdir="关注推送",
                            run_dir=fixture["run_dir"],
                            generator_commit="test-generator-commit",
                            expectations=fixture["expectations"],
                        )
                self.assertIn(
                    raised.exception.code,
                    {"run_dir_publish_conflict", "run_dir_substituted"},
                )
                final_info = os.lstat(fixture["run_dir"])
                self.assertEqual(
                    (final_info.st_dev, final_info.st_ino), replacement_identity
                )
                self.assertFalse(
                    os.path.exists(os.path.join(fixture["run_dir"], "manifest.json"))
                )
                self.assertFalse(
                    os.path.exists(os.path.join(fixture["run_dir"], "state.json"))
                )
                if replacement_kind == "marker":
                    with open(
                        os.path.join(fixture["run_dir"], "unrelated-marker"), "rb"
                    ) as handle:
                        self.assertEqual(
                            handle.read(), b"preserve publication replacement"
                        )
                else:
                    self.assertEqual(os.listdir(fixture["run_dir"]), [])
                for created_path in created_staging_paths:
                    self.assertFalse(os.path.lexists(created_path))

    def test_darwin_exclusive_directory_publish_is_available(self):
        from core import relation_markdown_cleanup as cleanup_module

        if sys.platform == "darwin":
            self.assertTrue(cleanup_module._exclusive_directory_publish_available())

    def test_preview_refuses_initial_symlink_run_parent_before_evidence_reads(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        canonical_root = os.path.realpath(self.tmp.name)
        actual_parent = os.path.join(canonical_root, "actual-run-parent")
        alias_parent = os.path.join(canonical_root, "run-parent-alias")
        os.mkdir(actual_parent)
        os.symlink(actual_parent, alias_parent)
        aliased_run_dir = os.path.join(alias_parent, "cleanup-run")
        from core import relation_markdown_cleanup as cleanup_module

        real_collect = cleanup_module.collect_database_evidence
        with patch(
            "core.relation_markdown_cleanup.collect_database_evidence",
            wraps=real_collect,
        ) as collect_call:
            with self.assertRaises(CleanupError) as raised:
                preview_cleanup(
                    backup_db=fixture["backup_db"],
                    current_db=fixture["current_db"],
                    vault_root=fixture["vault_root"],
                    obsidian_subdir="关注推送",
                    run_dir=aliased_run_dir,
                    generator_commit="test-generator-commit",
                    expectations=fixture["expectations"],
                )
        self.assertEqual(raised.exception.code, "run_dir_parent_alias")
        self.assertEqual(collect_call.call_count, 0)
        self.assertFalse(os.path.lexists(os.path.join(actual_parent, "cleanup-run")))

    def test_preview_refuses_review_alias_redirect_without_touching_replacement(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        canonical_root = os.path.realpath(self.tmp.name)
        parent_a = os.path.join(canonical_root, "alias-target-a")
        parent_b = os.path.join(canonical_root, "alias-target-b")
        alias_parent = os.path.join(canonical_root, "redirectable-parent-alias")
        os.mkdir(parent_a)
        os.mkdir(parent_b)
        os.symlink(parent_a, alias_parent)
        replacement = os.path.join(parent_b, "cleanup-run")
        os.mkdir(replacement)
        marker = os.path.join(replacement, "unrelated-marker")
        with open(marker, "wb") as handle:
            handle.write(b"preserve alias replacement")
        aliased_run_dir = os.path.join(alias_parent, "cleanup-run")
        from core import relation_markdown_cleanup as cleanup_module

        real_publish = cleanup_module._rename_directory_exclusive

        def publish_then_redirect_alias(parent_fd, source_name, destination_name):
            real_publish(parent_fd, source_name, destination_name)
            os.unlink(alias_parent)
            os.symlink(parent_b, alias_parent)

        with patch(
            "core.relation_markdown_cleanup._rename_directory_exclusive",
            side_effect=publish_then_redirect_alias,
        ):
            with self.assertRaises(CleanupError) as raised:
                preview_cleanup(
                    backup_db=fixture["backup_db"],
                    current_db=fixture["current_db"],
                    vault_root=fixture["vault_root"],
                    obsidian_subdir="关注推送",
                    run_dir=aliased_run_dir,
                    generator_commit="test-generator-commit",
                    expectations=fixture["expectations"],
                )
        self.assertEqual(raised.exception.code, "run_dir_parent_alias")
        with open(marker, "rb") as handle:
            self.assertEqual(handle.read(), b"preserve alias replacement")
        self.assertFalse(os.path.exists(os.path.join(replacement, "manifest.json")))
        self.assertFalse(os.path.exists(os.path.join(replacement, "state.json")))

    def test_preview_cleans_published_artifacts_when_canonical_parent_redirects(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        parent_a = os.path.join(self.tmp_root, "canonical-parent-a")
        parent_a_saved = os.path.join(self.tmp_root, "canonical-parent-a-saved")
        parent_b = os.path.join(self.tmp_root, "canonical-parent-b")
        os.mkdir(parent_a)
        os.mkdir(parent_b)
        replacement = os.path.join(parent_b, "cleanup-run")
        os.mkdir(replacement)
        marker = os.path.join(replacement, "unrelated-marker")
        with open(marker, "wb") as handle:
            handle.write(b"preserve canonical-parent replacement")
        run_dir = os.path.join(parent_a, "cleanup-run")
        from core import relation_markdown_cleanup as cleanup_module

        real_publish = cleanup_module._rename_directory_exclusive

        def publish_then_redirect_parent(parent_fd, source_name, destination_name):
            real_publish(parent_fd, source_name, destination_name)
            os.rename(parent_a, parent_a_saved)
            os.symlink(parent_b, parent_a)

        with patch(
            "core.relation_markdown_cleanup._rename_directory_exclusive",
            side_effect=publish_then_redirect_parent,
        ):
            with self.assertRaises(CleanupError) as raised:
                preview_cleanup(
                    backup_db=fixture["backup_db"],
                    current_db=fixture["current_db"],
                    vault_root=fixture["vault_root"],
                    obsidian_subdir="关注推送",
                    run_dir=run_dir,
                    generator_commit="test-generator-commit",
                    expectations=fixture["expectations"],
                )
        self.assertEqual(raised.exception.code, "run_dir_parent_alias")
        with open(marker, "rb") as handle:
            self.assertEqual(handle.read(), b"preserve canonical-parent replacement")
        self.assertFalse(os.path.exists(os.path.join(replacement, "manifest.json")))
        self.assertFalse(os.path.exists(os.path.join(replacement, "state.json")))
        self.assertFalse(os.path.exists(os.path.join(parent_a_saved, "cleanup-run")))

    def test_preview_cleans_staging_when_exclusive_publish_fails(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        with patch(
            "core.relation_markdown_cleanup._rename_directory_exclusive",
            side_effect=CleanupError(
                "run_dir_publish_failed", "fixture publication failure"
            ),
        ):
            self.assert_preview_refuses("run_dir_publish_failed", fixture)
        self.assertEqual(
            [
                name
                for name in os.listdir(os.path.dirname(fixture["run_dir"]))
                if name.startswith(".cleanup-run.staging-")
            ],
            [],
        )

    def test_preview_refuses_unsupported_exclusive_publish_and_cleans_staging(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        with patch(
            "core.relation_markdown_cleanup._darwin_rename_exclusive_function",
            return_value=None,
        ):
            self.assert_preview_refuses("run_dir_publish_unsupported", fixture)
        self.assertEqual(
            [
                name
                for name in os.listdir(os.path.dirname(fixture["run_dir"]))
                if name.startswith(".cleanup-run.staging-")
            ],
            [],
        )

    def test_preview_cleans_pinned_artifacts_after_post_publish_replacement(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        from core import relation_markdown_cleanup as cleanup_module

        real_publish = cleanup_module._rename_directory_exclusive
        saved_original = os.path.join(self.tmp.name, "published-original-saved")

        def publish_then_replace(parent_fd, source_name, destination_name):
            real_publish(parent_fd, source_name, destination_name)
            os.rename(fixture["run_dir"], saved_original)
            os.mkdir(fixture["run_dir"], 0o700)
            with open(
                os.path.join(fixture["run_dir"], "unrelated-marker"), "wb"
            ) as handle:
                handle.write(b"preserve post-publish replacement")

        with patch(
            "core.relation_markdown_cleanup._rename_directory_exclusive",
            side_effect=publish_then_replace,
        ):
            with self.assertRaises(CleanupError) as raised:
                preview_cleanup(
                    backup_db=fixture["backup_db"],
                    current_db=fixture["current_db"],
                    vault_root=fixture["vault_root"],
                    obsidian_subdir="关注推送",
                    run_dir=fixture["run_dir"],
                    generator_commit="test-generator-commit",
                    expectations=fixture["expectations"],
                )
        self.assertEqual(raised.exception.code, "run_dir_substituted")
        with open(
            os.path.join(fixture["run_dir"], "unrelated-marker"), "rb"
        ) as handle:
            self.assertEqual(handle.read(), b"preserve post-publish replacement")
        self.assertFalse(os.path.exists(os.path.join(saved_original, "manifest.json")))
        self.assertFalse(os.path.exists(os.path.join(saved_original, "state.json")))

    def test_preview_candidate_and_publish_refusals_do_not_leak_fds(self):
        if not os.path.isdir("/dev/fd"):
            self.skipTest("fd inventory unavailable")
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        source_relative = "关注推送/Chat 1/Category/Topic 1.md"
        from core import relation_markdown_cleanup as cleanup_module

        baseline = len(os.listdir("/dev/fd"))
        for _index in range(50):
            data, info = cleanup_module._read_candidate_from_pinned_root(
                fixture["vault_root"], "关注推送", source_relative
            )
            self.assertTrue(data)
            self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(len(os.listdir("/dev/fd")), baseline)

        for _index in range(20):
            with patch(
                "core.relation_markdown_cleanup._rename_directory_exclusive",
                side_effect=CleanupError(
                    "run_dir_publish_failed", "fixture publication failure"
                ),
            ):
                with self.assertRaises(CleanupError):
                    preview_cleanup(
                        backup_db=fixture["backup_db"],
                        current_db=fixture["current_db"],
                        vault_root=fixture["vault_root"],
                        obsidian_subdir="关注推送",
                        run_dir=fixture["run_dir"],
                        generator_commit="test-generator-commit",
                        expectations=fixture["expectations"],
                    )
            self.assertFalse(os.path.lexists(fixture["run_dir"]))
        self.assertEqual(len(os.listdir("/dev/fd")), baseline)

    def test_preview_preserves_substituted_run_directory_at_all_seal_boundaries(self):
        phases = ("before-first-seal", "between-seals", "before-cleanup")
        from core import relation_markdown_cleanup as cleanup_module

        for phase in phases:
            with self.subTest(phase=phase):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                self.materialize_backup_source(fixture)
                substitute = os.path.join(self.tmp.name, f"substitute-{phase}")
                orphan = os.path.join(self.tmp.name, f"orphan-{phase}")
                os.mkdir(substitute)
                marker = os.path.join(substitute, "unrelated-marker")
                with open(marker, "wb") as handle:
                    handle.write(b"preserve unrelated bytes")
                real_atomic = cleanup_module._atomic_private_json
                calls = 0

                def place_substitute_at_final_path():
                    if os.path.lexists(fixture["run_dir"]):
                        os.rename(fixture["run_dir"], orphan)
                    os.rename(substitute, fixture["run_dir"])

                def substitute_during_seal(directory_or_fd, filename, value):
                    nonlocal calls
                    calls += 1
                    if phase == "before-first-seal" and calls == 1:
                        place_substitute_at_final_path()
                    real_atomic(directory_or_fd, filename, value)
                    if phase == "between-seals" and calls == 1:
                        place_substitute_at_final_path()
                    if phase == "before-cleanup" and calls == 2:
                        place_substitute_at_final_path()
                        raise OSError("fixture seal failure")

                with patch(
                    "core.relation_markdown_cleanup._atomic_private_json",
                    side_effect=substitute_during_seal,
                ):
                    with self.assertRaises(CleanupError) as raised:
                        preview_cleanup(
                            backup_db=fixture["backup_db"],
                            current_db=fixture["current_db"],
                            vault_root=fixture["vault_root"],
                            obsidian_subdir="关注推送",
                            run_dir=fixture["run_dir"],
                            generator_commit="test-generator-commit",
                            expectations=fixture["expectations"],
                        )
                self.assertIn(
                    raised.exception.code,
                    {
                        "run_dir_publish_conflict",
                        "run_dir_substituted",
                        "run_dir_cleanup",
                        "manifest_seal",
                    },
                )
                with open(os.path.join(fixture["run_dir"], "unrelated-marker"), "rb") as handle:
                    self.assertEqual(handle.read(), b"preserve unrelated bytes")
                for artifact in ("manifest.json", "state.json"):
                    if os.path.exists(orphan):
                        self.assertFalse(os.path.exists(os.path.join(orphan, artifact)))

    def test_preview_refuses_current_source_identity_and_history_drift(self):
        cases = (
            ("ordinary-key", "UPDATE topics SET topic_key = 'ordinary:drift' WHERE topic_id = 1", "source_identity_drift"),
            ("ordinary-title", "UPDATE topics SET title = 'Ordinary drift' WHERE topic_id = 1", "source_identity_drift"),
            ("history-key", "UPDATE topics SET topic_key = 'history-summary:current' WHERE topic_id = 1", "history_topic"),
            ("history-title", "UPDATE topics SET title = 'Current 历史总结' WHERE topic_id = 1", "history_topic"),
            ("history-event", "UPDATE events SET event_type = 'history_summary' WHERE topic_id = 1", "history_topic"),
        )
        for label, sql, code in cases:
            with self.subTest(label=label):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                self.materialize_backup_source(fixture)
                conn = sqlite3.connect(fixture["current_db"])
                try:
                    conn.execute(sql)
                    conn.commit()
                finally:
                    conn.close()
                self.assert_preview_refuses(code, fixture)

    def test_preview_refuses_blank_commit_and_existing_run_dir_without_reads_or_writes(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        vault_before = self.vault_snapshot(fixture["vault_root"])
        with self.assertRaises(CleanupError) as raised:
            preview_cleanup(
                backup_db=fixture["backup_db"],
                current_db=fixture["current_db"],
                vault_root=fixture["vault_root"],
                obsidian_subdir="关注推送",
                run_dir=fixture["run_dir"],
                generator_commit="   ",
                expectations=fixture["expectations"],
            )
        self.assertEqual(raised.exception.code, "generator_commit_blank")
        self.assertFalse(os.path.exists(fixture["run_dir"]))
        os.mkdir(fixture["run_dir"])
        marker = os.path.join(fixture["run_dir"], "marker")
        with open(marker, "wb") as handle:
            handle.write(b"preserve")
        with self.assertRaises(CleanupError) as raised:
            preview_cleanup(
                backup_db=fixture["backup_db"],
                current_db=fixture["current_db"],
                vault_root=fixture["vault_root"],
                obsidian_subdir="关注推送",
                run_dir=fixture["run_dir"],
                generator_commit="test-generator-commit",
                expectations=fixture["expectations"],
            )
        self.assertEqual(raised.exception.code, "run_dir_exists")
        with open(marker, "rb") as handle:
            self.assertEqual(handle.read(), b"preserve")
        self.assertEqual(self.vault_snapshot(fixture["vault_root"]), vault_before)

    def test_preview_refuses_missing_full_preimage_and_unreadable_candidates(self):
        missing = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.assert_preview_refuses("topic_file_missing", missing)

        drifted = self.build_fixture(invalid_nonself=1, invalid_self=0)
        path, _ = self.materialize_backup_source(drifted)
        with open(path, "ab") as handle:
            handle.write(b"manual edit\n")
        self.assert_preview_refuses("full_preimage_drift", drifted)

        unreadable = self.build_fixture(invalid_nonself=1, invalid_self=0)
        path, _ = self.materialize_backup_source(unreadable)
        os.chmod(path, 0)
        self.assert_preview_refuses("topic_file_unreadable", unreadable)

    def test_preview_refuses_symlink_hardlink_and_nonregular_candidates(self):
        symlinked = self.build_fixture(invalid_nonself=1, invalid_self=0)
        path, _ = self.materialize_backup_source(symlinked)
        parent = os.path.dirname(path)
        real_parent = parent + "-real"
        os.rename(parent, real_parent)
        os.symlink(real_parent, parent)
        self.assert_preview_refuses("topic_path_symlink", symlinked)

        hardlinked = self.build_fixture(invalid_nonself=1, invalid_self=0)
        path, _ = self.materialize_backup_source(hardlinked)
        os.link(path, path + ".alias")
        self.assert_preview_refuses("topic_file_hardlinked", hardlinked)

        nonregular = self.build_fixture(invalid_nonself=1, invalid_self=0)
        path, _ = self.materialize_backup_source(nonregular)
        os.unlink(path)
        os.mkdir(path)
        self.assert_preview_refuses("topic_file_nonregular", nonregular)

    def test_preview_refuses_traversal_out_of_subdir_and_protected_paths(self):
        path_cases = {
            "topic_path_invalid": "关注推送/../escape.md",
            "topic_path_outside_subdir": "Other/Topic.md",
            "protected_topic_path": "关注推送/00-按日期.md",
            "protected-index": "关注推送/Chat/Category/目录.md",
            "protected-digest": "关注推送/Daily Digest/Topic.md",
            "protected-maintenance": "关注推送/Maintenance/Topic.md",
            "protected-example-digest": "关注推送/Example Digest/Topic.md",
        }
        for label, source_path in path_cases.items():
            with self.subTest(label=label):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                for db_path in (fixture["backup_db"], fixture["current_db"]):
                    conn = sqlite3.connect(db_path)
                    try:
                        conn.execute(
                            "UPDATE topics SET obsidian_path = ? WHERE topic_id = 1",
                            (source_path,),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                self.refresh_expectations(fixture)
                expected = label if label in {"topic_path_invalid", "topic_path_outside_subdir"} else "protected_topic_path"
                self.assert_preview_refuses(expected, fixture)

    def test_preview_refuses_casefold_and_nfc_manifest_path_collisions(self):
        path_pairs = (
            ("关注推送/Chat/Topic.md", "关注推送/chat/topic.md"),
            ("关注推送/Chat/Café.md", "关注推送/Chat/Cafe\u0301.md"),
        )
        for first_path, second_path in path_pairs:
            with self.subTest(paths=(first_path, second_path)):
                fixture = self.build_fixture(invalid_nonself=2, invalid_self=0)
                for db_path in (fixture["backup_db"], fixture["current_db"]):
                    conn = sqlite3.connect(db_path)
                    try:
                        conn.execute(
                            "UPDATE relations SET source_topic_id = 2, target_topic_id = 3 WHERE relation_id = 2"
                        )
                        conn.execute(
                            "UPDATE topics SET obsidian_path = ? WHERE topic_id = 1",
                            (first_path,),
                        )
                        conn.execute(
                            "UPDATE topics SET obsidian_path = ? WHERE topic_id = 2",
                            (second_path,),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                self.refresh_expectations(fixture)
                self.assert_preview_refuses("manifest_path_collision", fixture)

    def test_preview_refuses_all_three_history_summary_signals(self):
        mutations = (
            ("UPDATE topics SET topic_key = 'history-summary:fixture' WHERE topic_id = 1", ()),
            ("UPDATE topics SET title = 'Fixture 历史总结' WHERE topic_id = 1", ()),
            ("UPDATE events SET event_type = 'history_summary' WHERE topic_id = 1", ()),
        )
        for sql, parameters in mutations:
            with self.subTest(sql=sql):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                conn = sqlite3.connect(fixture["backup_db"])
                try:
                    conn.execute(sql, parameters)
                    conn.commit()
                finally:
                    conn.close()
                self.refresh_expectations(fixture)
                self.materialize_backup_source(fixture)
                self.assert_preview_refuses("history_topic", fixture)

    def test_preview_accounts_for_protected_renderer_suppressed_selfloop(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=1)
        for db_path in (fixture["backup_db"], fixture["current_db"]):
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    UPDATE topics
                    SET topic_key = 'history-summary:selfloop',
                        title = 'Self-loop 历史总结'
                    WHERE topic_id = 3
                    """
                )
                conn.execute(
                    "UPDATE events SET event_type = 'history_summary' WHERE topic_id = 3"
                )
                conn.commit()
            finally:
                conn.close()
        self.refresh_expectations(fixture)
        self.materialize_backup_source(fixture, source_topic_id=1)

        evidence = collect_database_evidence(
            fixture["backup_db"],
            fixture["current_db"],
            fixture["expectations"],
        )
        self.assertEqual(evidence["selected_count"], 2)
        self.assertEqual(evidence["self_loop_count"], 1)
        self.assertEqual(evidence["renderable_count"], 1)
        self.assertEqual(
            [edge["relation_id"] for edge in evidence["renderable_edges"]], [1]
        )
        self.assertEqual(
            [relation[0] for relation in evidence["selected_relation_tuples"]],
            [1, 2],
        )

        preview = self.preview_fixture(fixture)
        self.assertEqual(preview["selected_count"], 2)
        self.assertEqual(preview["self_loop_count"], 1)
        self.assertEqual(preview["renderable_count"], 1)
        self.assertEqual(preview["exact_match_count"], 1)
        with open(
            os.path.join(fixture["run_dir"], "manifest.json"), encoding="utf-8"
        ) as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["counts"]["selected"], 2)
        self.assertEqual(manifest["counts"]["self_loops"], 1)
        self.assertEqual(manifest["counts"]["renderable"], 1)
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(
            [edge["relation_id"] for edge in manifest["files"][0]["edges"]], [1]
        )

    def test_preview_refuses_multiline_target_title_and_current_render_collision(self):
        multiline = self.build_fixture(invalid_nonself=1, invalid_self=0)
        for db_path in (multiline["backup_db"], multiline["current_db"]):
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE topics SET title = ? WHERE topic_id = 2",
                    ("Target\nInjected",),
                )
                conn.commit()
            finally:
                conn.close()
        self.refresh_expectations(multiline)
        self.assert_preview_refuses("multiline_target_title", multiline)

        collision = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(collision)
        conn = sqlite3.connect(collision["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (10, 1, 2, 'updates', 'legitimate fixture reason', 10.0)"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_preview_refuses("current_relation_collision", collision)

    def test_preview_refuses_duplicate_manifest_lines(self):
        fixture = self.build_fixture(invalid_nonself=2, invalid_self=0)
        for db_path in (fixture["backup_db"], fixture["current_db"]):
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE topics SET title = 'Topic 2', obsidian_path = '关注推送/Chat 2/Category/Topic 2.md' WHERE topic_id = 3"
                )
                conn.commit()
            finally:
                conn.close()
        self.refresh_expectations(fixture)
        self.materialize_backup_source(fixture)
        self.assert_preview_refuses("manifest_line_duplicate", fixture)

    def test_preview_refuses_mutated_missing_duplicate_crlf_and_outside_lines_as_preimage_drift(self):
        mutations = {
            "missing": lambda data, line: data.replace(line, b"", 1),
            "duplicate": lambda data, line: data.replace(line, line + line, 1),
            "crlf": lambda data, line: data.replace(line, line[:-1] + b"\r\n", 1),
            "outside": lambda data, line: line + data,
            "duplicate-heading": lambda data, line: data.replace(
                "## 来源\n".encode("utf-8"),
                "## 相关主题\n## 来源\n".encode("utf-8"),
                1,
            ),
            "malformed-heading": lambda data, line: data.replace(
                "## 相关主题\n".encode("utf-8"),
                "## 相关主题 \n".encode("utf-8"),
                1,
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
                path, data = self.materialize_backup_source(fixture)
                edge = collect_database_evidence(
                    fixture["backup_db"],
                    fixture["current_db"],
                    fixture["expectations"],
                )["renderable_edges"][0]
                line = edge["rendered_line"].encode("utf-8")
                with open(path, "wb") as handle:
                    handle.write(mutate(data, line))
                self.assert_preview_refuses("full_preimage_drift", fixture)

    def test_preview_seals_canonical_manifest_and_initial_state_privately(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        source_path, source_before = self.materialize_backup_source(fixture)
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])

        result = preview_cleanup(
            backup_db=fixture["backup_db"],
            current_db=fixture["current_db"],
            vault_root=fixture["vault_root"],
            obsidian_subdir="关注推送",
            run_dir=fixture["run_dir"],
            generator_commit="test-generator-commit",
            expectations=fixture["expectations"],
        )

        manifest_path = os.path.join(fixture["run_dir"], "manifest.json")
        state_path = os.path.join(fixture["run_dir"], "state.json")
        with open(manifest_path, "rb") as handle:
            manifest_bytes = handle.read()
        with open(state_path, "rb") as handle:
            state_bytes = handle.read()
        manifest = json.loads(manifest_bytes)
        state = json.loads(state_bytes)
        self.assertEqual(manifest_bytes, canonical_json_bytes(manifest))
        self.assertEqual(result["manifest_sha256"], sha256_bytes(manifest_bytes))
        self.assertEqual(manifest["generator_commit"], "test-generator-commit")
        self.assertEqual(len(manifest["renderer_source_sha256"]), 64)
        self.assertEqual(
            state,
            {
                "schema": "exact_relation_markdown_cleanup/state-v1",
                "manifest_sha256": result["manifest_sha256"],
                "state": "planned",
                "files": {},
                "errors": [],
            },
        )
        self.assertEqual(os.stat(state_path).st_mode & 0o777, 0o600)
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), source_before)
        public = repr(result)
        self.assertNotIn("Topic 1", public)
        self.assertNotIn("关注推送", public)
        self.assertNotIn(KNOWN_BROKEN_RELATION_REASON, public)

    def test_preview_removes_run_dir_when_state_sealing_fails_after_replace(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.materialize_backup_source(fixture)
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        vault_before = self.vault_snapshot(fixture["vault_root"])
        from core import relation_markdown_cleanup as cleanup_module

        real_atomic_write = cleanup_module._atomic_private_json
        calls = 0

        def fail_after_state_replace(directory, filename, value):
            nonlocal calls
            calls += 1
            real_atomic_write(directory, filename, value)
            if calls == 2:
                raise OSError("fixture durability failure")

        with patch(
            "core.relation_markdown_cleanup._atomic_private_json",
            side_effect=fail_after_state_replace,
        ):
            with self.assertRaises(CleanupError) as raised:
                preview_cleanup(
                    backup_db=fixture["backup_db"],
                    current_db=fixture["current_db"],
                    vault_root=fixture["vault_root"],
                    obsidian_subdir="关注推送",
                    run_dir=fixture["run_dir"],
                    generator_commit="test-generator-commit",
                    expectations=fixture["expectations"],
                )
        self.assertEqual(raised.exception.code, "manifest_seal")
        self.assertFalse(os.path.lexists(fixture["run_dir"]))
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)
        self.assertEqual(self.vault_snapshot(fixture["vault_root"]), vault_before)

    def assert_refuses_without_database_writes(
        self,
        code,
        fixture,
        expectations,
    ):
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        with self.assertRaises(CleanupError) as raised:
            collect_database_evidence(
                fixture["backup_db"],
                fixture["current_db"],
                expectations,
            )
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)
        self.assertEqual(raised.exception.code, code)

    @staticmethod
    def make_relations_nullable(path):
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                ALTER TABLE relations RENAME TO strict_relations;
                CREATE TABLE relations (
                    relation_id INTEGER PRIMARY KEY,
                    source_topic_id INTEGER,
                    target_topic_id INTEGER,
                    relation,
                    reason,
                    created_at
                );
                INSERT INTO relations
                SELECT relation_id, source_topic_id, target_topic_id,
                       relation, reason, created_at
                FROM strict_relations;
                DROP TABLE strict_relations;
                """
            )
            conn.commit()
        finally:
            conn.close()

    def test_collect_database_evidence_accounts_exact_selector_and_self_loops(self):
        fixture = self.build_fixture(invalid_nonself=2, invalid_self=1)
        evidence = collect_database_evidence(
            fixture["backup_db"],
            fixture["current_db"],
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=3,
                self_loops=1,
                renderable_edges=2,
            ),
        )
        self.assertEqual(evidence["selected_count"], 3)
        self.assertEqual(evidence["self_loop_count"], 1)
        self.assertEqual(len(evidence["renderable_edges"]), 2)
        self.assertEqual(evidence["current_known_invalid_count"], 0)

    def test_open_read_only_db_rejects_writes(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = open_read_only_db(fixture["current_db"])
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM relations")
        finally:
            conn.close()

    def test_canonical_json_bytes_rejects_nonfinite_numbers(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    canonical_json_bytes({"value": value})

    def test_collect_database_evidence_refuses_wrong_backup_checksum(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.assert_refuses_without_database_writes(
            "backup_checksum",
            fixture,
            CleanupExpectations(
                backup_sha256="0" * 64,
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_wrong_backup_mode(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        os.chmod(fixture["backup_db"], 0o644)
        self.assert_refuses_without_database_writes(
            "backup_mode",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_selector_count_drift(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.assert_refuses_without_database_writes(
            "selector_count",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=2,
                self_loops=0,
                renderable_edges=2,
            ),
        )

    def test_collect_database_evidence_refuses_self_loop_accounting_drift(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=1)
        self.assert_refuses_without_database_writes(
            "self_loop_count",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=2,
                self_loops=0,
                renderable_edges=2,
            ),
        )

    def test_collect_database_evidence_refuses_renderable_accounting_drift(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=1)
        self.assert_refuses_without_database_writes(
            "renderable_count",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=2,
                self_loops=1,
                renderable_edges=2,
            ),
        )

    def test_collect_database_evidence_refuses_backup_integrity_failure(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        expectations = CleanupExpectations(
            backup_sha256=self.digest(fixture["backup_db"]),
            selected_edges=1,
            self_loops=0,
            renderable_edges=1,
        )
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        with patch(
            "core.relation_markdown_cleanup._integrity_check",
            return_value="fixture corruption",
        ):
            with self.assertRaises(CleanupError) as raised:
                collect_database_evidence(
                    fixture["backup_db"],
                    fixture["current_db"],
                    expectations,
                )
        self.assertEqual(raised.exception.code, "backup_integrity")
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)

    def test_collect_database_evidence_refuses_current_known_invalid_rows(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (1, 1, 2, 'updates', ?, 1.25)",
                (KNOWN_BROKEN_RELATION_REASON,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_known_invalid",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_current_integrity_failure(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        expectations = CleanupExpectations(
            backup_sha256=self.digest(fixture["backup_db"]),
            selected_edges=1,
            self_loops=0,
            renderable_edges=1,
        )
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        with patch(
            "core.relation_markdown_cleanup._integrity_check",
            side_effect=["ok", "fixture corruption"],
        ):
            with self.assertRaises(CleanupError) as raised:
                collect_database_evidence(
                    fixture["backup_db"],
                    fixture["current_db"],
                    expectations,
                )
        self.assertEqual(raised.exception.code, "current_integrity")
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)

    def test_collect_database_evidence_refuses_current_fts_mismatch(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("DELETE FROM topic_fts WHERE topic_id = 2")
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_fts_mismatch",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_current_event_orphans(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("UPDATE events SET topic_id = 999 WHERE event_id = 1")
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_event_orphans",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_current_relation_orphans(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (1, 999, 2, 'related', 'orphan', 1.25)"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_relation_orphans",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_missing_current_topic(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("DELETE FROM events WHERE topic_id = 2")
            conn.execute("DELETE FROM topic_fts WHERE topic_id = 2")
            conn.execute("DELETE FROM topics WHERE topic_id = 2")
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "topic_missing",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_duplicate_backup_source_path_owner(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["backup_db"])
        try:
            source_path = conn.execute(
                "SELECT obsidian_path FROM topics WHERE topic_id = 1"
            ).fetchone()[0]
            conn.execute(
                "UPDATE topics SET obsidian_path = ? WHERE topic_id = 2",
                (source_path,),
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(fixture["backup_db"], 0o600)
        self.assert_refuses_without_database_writes(
            "source_path_owner_count",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_duplicate_current_source_path_owner(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            source_path = conn.execute(
                "SELECT obsidian_path FROM topics WHERE topic_id = 1"
            ).fetchone()[0]
            conn.execute(
                "UPDATE topics SET obsidian_path = ? WHERE topic_id = 2",
                (source_path,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "source_path_owner_count",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_source_path_drift(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "UPDATE topics SET obsidian_path = '关注推送/Drifted.md' WHERE topic_id = 1"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "source_path_drift",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_target_path_drift(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "UPDATE topics SET obsidian_path = '关注推送/Drifted.md' WHERE topic_id = 2"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "target_path_drift",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_target_title_drift(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("UPDATE topics SET title = 'Drifted title' WHERE topic_id = 2")
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "target_title_drift",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_missing_backup_with_typed_error(self):
        missing_backup = os.path.join(self.tmp.name, "missing-backup.db")
        current_before = self.digest(self.current_db)
        with self.assertRaises(CleanupError) as raised:
            collect_database_evidence(
                missing_backup,
                self.current_db,
                CleanupExpectations(
                    backup_sha256="0" * 64,
                    selected_edges=0,
                    self_loops=0,
                    renderable_edges=0,
                ),
            )
        self.assertEqual(raised.exception.code, "backup_unavailable")
        self.assertFalse(os.path.exists(missing_backup))
        self.assertEqual(self.digest(self.current_db), current_before)

    def test_collect_database_evidence_refuses_missing_current_db_with_typed_error(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        missing_current = os.path.join(self.tmp.name, "missing-current.db")
        backup_before = self.digest(fixture["backup_db"])
        with self.assertRaises(CleanupError) as raised:
            collect_database_evidence(
                fixture["backup_db"],
                missing_current,
                CleanupExpectations(
                    backup_sha256=backup_before,
                    selected_edges=1,
                    self_loops=0,
                    renderable_edges=1,
                ),
            )
        self.assertEqual(raised.exception.code, "current_open")
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertFalse(os.path.exists(missing_current))

    def test_exact_selector_excludes_near_matches(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["backup_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (2, 1, 2, 'related', ?, 2.25)",
                (KNOWN_BROKEN_RELATION_REASON,),
            )
            conn.execute(
                "INSERT INTO relations VALUES (3, 2, 1, 'updates', 'ordinary reason', 3.25)"
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(fixture["backup_db"], 0o600)

        evidence = collect_database_evidence(
            fixture["backup_db"],
            fixture["current_db"],
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

        self.assertEqual(evidence["selected_count"], 1)
        self.assertEqual([row[0] for row in evidence["selected_relation_tuples"]], [1])
        selected_tuple = evidence["selected_relation_tuples"][0]
        self.assertEqual(
            selected_tuple[4],
            sha256_bytes(KNOWN_BROKEN_RELATION_REASON.encode("utf-8")),
        )
        self.assertEqual(selected_tuple[5], format(1.125, ".17g"))
        self.assertNotIn(KNOWN_BROKEN_RELATION_REASON, repr(selected_tuple))

    def test_canonical_relation_and_risky_digests_are_complete_and_deterministic(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (20, 1, 2, 'updates', 'raw secret reason', 0.1)"
            )
            conn.execute(
                "INSERT INTO relations VALUES (5, 2, 1, 'contradicts', 'second reason', 5.125)"
            )
            conn.commit()
        finally:
            conn.close()
        expectations = CleanupExpectations(
            backup_sha256=self.digest(fixture["backup_db"]),
            selected_edges=1,
            self_loops=0,
            renderable_edges=1,
        )

        first = collect_database_evidence(
            fixture["backup_db"], fixture["current_db"], expectations
        )
        second = collect_database_evidence(
            fixture["backup_db"], fixture["current_db"], expectations
        )

        tuples = first["current_relation_tuples"]
        self.assertEqual([row[0] for row in tuples], [5, 20])
        self.assertEqual(tuples[1][4], sha256_bytes(b"raw secret reason"))
        self.assertEqual(tuples[1][5], format(0.1, ".17g"))
        self.assertNotIn("raw secret reason", repr(tuples))
        self.assertEqual(first["risky_warning_tuples"], tuples)
        self.assertEqual(
            first["current_relation_set_digest"],
            sha256_bytes(canonical_json_bytes(tuples)),
        )
        expected_snapshot = {
            "schema": "exact_relation_markdown_cleanup/v1",
            "topic_count": first["current_counts"]["topics"],
            "event_count": first["current_counts"]["events"],
            "fts_count": first["current_counts"]["fts"],
            "fts_parity": first["current_fts_parity"],
            "orphan_event_count": first["current_counts"]["orphan_events"],
            "orphan_relation_count": first["current_counts"]["orphan_relations"],
            "source_identities": first["source_identities"],
            "target_identities": first["target_identities"],
            "relation_set_digest": first["current_relation_set_digest"],
            "risky_warning_set_digest": first["risky_warning_set_digest"],
        }
        self.assertEqual(
            first["current_snapshot_digest"],
            sha256_bytes(canonical_json_bytes(expected_snapshot)),
        )
        self.assertEqual(first["risky_warning_count"], 2)
        self.assertEqual(
            first["current_relation_set_digest"],
            second["current_relation_set_digest"],
        )
        self.assertEqual(
            first["risky_warning_set_digest"],
            second["risky_warning_set_digest"],
        )
        self.assertEqual(
            first["current_snapshot_digest"],
            second["current_snapshot_digest"],
        )

    def test_collect_database_evidence_refuses_selector_join_accounting_drift(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["backup_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (2, 999, 2, 'updates', ?, 2.25)",
                (KNOWN_BROKEN_RELATION_REASON,),
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(fixture["backup_db"], 0o600)
        self.assert_refuses_without_database_writes(
            "selector_accounting",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_wraps_backup_schema_failures(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["backup_db"])
        try:
            conn.execute("DROP TABLE relations")
            conn.commit()
        finally:
            conn.close()
        os.chmod(fixture["backup_db"], 0o600)
        self.assert_refuses_without_database_writes(
            "backup_schema",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_wraps_current_schema_failures(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("DROP TABLE topic_fts")
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_schema",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_backup_text_timestamp(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["backup_db"])
        try:
            conn.execute(
                "UPDATE relations SET created_at = 'not-a-number' WHERE relation_id = 1"
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(fixture["backup_db"], 0o600)
        self.assert_refuses_without_database_writes(
            "backup_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_backup_blob_identity(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["backup_db"])
        try:
            conn.execute(
                "UPDATE topics SET title = ? WHERE topic_id = 2",
                (sqlite3.Binary(b"target-title"),),
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(fixture["backup_db"], 0o600)
        self.assert_refuses_without_database_writes(
            "backup_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_backup_nonfinite_timestamp(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["backup_db"])
        try:
            conn.execute(
                "UPDATE relations SET created_at = ? WHERE relation_id = 1",
                (float("inf"),),
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(fixture["backup_db"], 0o600)
        self.assert_refuses_without_database_writes(
            "backup_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_current_text_timestamp(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (1, 1, 2, 'related', 'reason', 'not-a-number')"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_current_blob_relation(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (1, 1, 2, ?, 'reason', 1.25)",
                (sqlite3.Binary(b"related"),),
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_current_blob_reason(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (1, 1, 2, 'related', ?, 1.25)",
                (sqlite3.Binary(b"reason"),),
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_current_null_reason(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        self.make_relations_nullable(fixture["current_db"])
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (1, 1, 2, 'related', NULL, 1.25)"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_current_blob_identity(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "UPDATE topics SET title = ? WHERE topic_id = 2",
                (sqlite3.Binary(b"target-title"),),
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_equal_count_missing_and_duplicate_fts(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("DELETE FROM topic_fts WHERE topic_id = 2")
            conn.execute(
                """
                INSERT INTO topic_fts(topic_id, title, category, summary, entities, key_facts, links)
                SELECT topic_id, title, category, summary, entities, key_facts, links
                FROM topic_fts
                WHERE topic_id = 1
                """
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_fts_mismatch",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_null_fts_topic_id(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("DELETE FROM topic_fts WHERE topic_id = 2")
            conn.execute(
                "INSERT INTO topic_fts VALUES (NULL, 'x', 'x', 'x', '', '', '')"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_fts_mismatch",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_noninteger_fts_topic_id(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("DELETE FROM topic_fts WHERE topic_id = 2")
            conn.execute(
                "INSERT INTO topic_fts VALUES ('topic-2', 'x', 'x', 'x', '', '', '')"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_fts_mismatch",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_orphan_fts_topic_id(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute("DELETE FROM topic_fts WHERE topic_id = 2")
            conn.execute(
                "INSERT INTO topic_fts VALUES (999, 'x', 'x', 'x', '', '', '')"
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_fts_mismatch",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_nonfinite_relation_timestamp(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (1, 1, 2, 'related', 'reason', ?)",
                (float("inf"),),
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_collect_database_evidence_refuses_blob_relation_id_field(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        conn = sqlite3.connect(fixture["current_db"])
        try:
            conn.execute(
                "INSERT INTO relations VALUES (1, ?, 2, 'related', 'reason', 1.25)",
                (sqlite3.Binary(b"1"),),
            )
            conn.commit()
        finally:
            conn.close()
        self.assert_refuses_without_database_writes(
            "current_value_type",
            fixture,
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

    def test_nullable_topic_key_has_explicit_canonical_policy(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        for path in (fixture["backup_db"], fixture["current_db"]):
            conn = sqlite3.connect(path)
            try:
                conn.execute("UPDATE topics SET topic_key = NULL WHERE topic_id = 1")
                conn.commit()
            finally:
                conn.close()
        os.chmod(fixture["backup_db"], 0o600)

        evidence = collect_database_evidence(
            fixture["backup_db"],
            fixture["current_db"],
            CleanupExpectations(
                backup_sha256=self.digest(fixture["backup_db"]),
                selected_edges=1,
                self_loops=0,
                renderable_edges=1,
            ),
        )

        self.assertIsNone(evidence["renderable_edges"][0]["source_topic_key"])
        self.assertIsNone(evidence["source_identities"][0][1])

    def test_collect_database_evidence_maps_current_canonicalization_failure(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        expectations = CleanupExpectations(
            backup_sha256=self.digest(fixture["backup_db"]),
            selected_edges=1,
            self_loops=0,
            renderable_edges=1,
        )
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        with patch(
            "core.relation_markdown_cleanup.canonical_json_bytes",
            side_effect=ValueError("fixture canonicalization failure"),
        ):
            with self.assertRaises(CleanupError) as raised:
                collect_database_evidence(
                    fixture["backup_db"],
                    fixture["current_db"],
                    expectations,
                )
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)
        self.assertEqual(raised.exception.code, "current_canonicalization")

    def test_collect_database_evidence_maps_backup_canonicalization_failure(self):
        fixture = self.build_fixture(invalid_nonself=1, invalid_self=0)
        expectations = CleanupExpectations(
            backup_sha256=self.digest(fixture["backup_db"]),
            selected_edges=1,
            self_loops=0,
            renderable_edges=1,
        )
        backup_before = self.digest(fixture["backup_db"])
        current_before = self.digest(fixture["current_db"])
        with patch(
            "core.relation_markdown_cleanup.canonical_json_bytes",
            side_effect=[
                b"{}",
                b"{}",
                b"{}",
                b"{}",
                ValueError("fixture canonicalization failure"),
            ],
        ):
            with self.assertRaises(CleanupError) as raised:
                collect_database_evidence(
                    fixture["backup_db"],
                    fixture["current_db"],
                    expectations,
                )
        self.assertEqual(self.digest(fixture["backup_db"]), backup_before)
        self.assertEqual(self.digest(fixture["current_db"]), current_before)
        self.assertEqual(raised.exception.code, "backup_canonicalization")


if __name__ == "__main__":
    unittest.main()
