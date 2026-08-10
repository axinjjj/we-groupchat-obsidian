#!/usr/bin/env python3
"""Run an authenticated, sealed taxonomy migration without leaking private data."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from urllib.parse import quote

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config
from core.knowledge import safe_path_part
from core.taxonomy_migration import (
    MigrationError,
    apply_migration,
    load_sealed_run,
    preview_migration,
    rollback_migration,
    status_migration,
)


PUBLIC_KEYS = {
    "applicable", "state", "manifest_sha256", "profile_version",
    "topic_change_count", "rendered_topic_count", "managed_index_count",
    "file_count", "pending", "applied", "already_clean",
    "database_total", "database_pending", "database_applied",
    "database_already_clean", "database_drifted",
    "applied_this_invocation",
    "restored", "drifted", "errors",
}
EXAMPLE_KEYS = {"relative_path", "title"}
MAX_EXAMPLE_LIMIT = 20
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_COUNT_KEYS = PUBLIC_KEYS - {
    "applicable", "state", "manifest_sha256", "errors",
}
_PUBLIC_STATES = {
    "planned", "mixed", "applied", "already_clean", "drifted", "rolled_back",
}
_BIDI_FORMATTING_CONTROLS = {
    "\u061c",  # ARABIC LETTER MARK
    "\u200e",  # LEFT-TO-RIGHT MARK
    "\u200f",  # RIGHT-TO-LEFT MARK
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
}


class CliUsageError(ValueError):
    """An argparse failure whose details are intentionally not public."""


class SafeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, _message):
        raise CliUsageError("cli_usage")


def _add_public_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def _add_sensitive_output(parser: argparse.ArgumentParser) -> None:
    _add_public_output(parser)
    parser.add_argument("--sensitive", action="store_true")
    parser.add_argument("--example-limit", type=int, default=5)


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        description="Guarded taxonomy migration",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=SafeArgumentParser,
    )

    preview = commands.add_parser("preview", allow_abbrev=False)
    preview.add_argument("--profile", required=True)
    preview.add_argument("--run-dir", required=True)
    _add_sensitive_output(preview)

    status = commands.add_parser("status", allow_abbrev=False)
    status.add_argument("--run-dir", required=True)
    _add_sensitive_output(status)

    for name in ("apply", "rollback"):
        command = commands.add_parser(
            name,
            allow_abbrev=False,
            description=(
                "Live mutation requires all vault writers to remain quiescent "
                "for the full operation: stop the monitor/LaunchAgent and pause "
                "Obsidian, any editor, or any external vault writer. Writers that "
                "bypass this precondition are outside the recovery guarantee."
            ),
        )
        command.add_argument("--run-dir", required=True)
        command.add_argument("--manifest-sha256", required=True)
        command.add_argument(
            "--confirm",
            required=True,
            help=(
                "exact token: "
                f"{name.upper()}_TAXONOMY_MIGRATION:<full-sha256>"
            ),
        )
        _add_public_output(command)
    return parser


def _schema_error() -> MigrationError:
    return MigrationError(
        "public_report_schema",
        "public report value does not match the safe schema",
    )


def _safe_error_code(error) -> str:
    if isinstance(error, str):
        code = error
    elif isinstance(error, MigrationError):
        code = error.code
    elif isinstance(error, CliUsageError):
        code = "cli_usage"
    elif isinstance(error, json.JSONDecodeError):
        code = "json_error"
    elif isinstance(error, sqlite3.Error):
        code = "sqlite_error"
    elif isinstance(error, OSError):
        code = "os_error"
    elif isinstance(error, ValueError):
        code = "value_error"
    else:
        code = "migration_error"
    if not isinstance(code, str) or _STABLE_CODE.fullmatch(code) is None:
        return "migration_error"
    return code


def _safe_text(value: object) -> bool:
    return isinstance(value, str) and all(
        unicodedata.category(character) != "Cc"
        and character not in {"\u2028", "\u2029"}
        and character not in _BIDI_FORMATTING_CONTROLS
        for character in value
    )


def _canonical_posix_relative_path(value: str) -> bool:
    """Validate canonical POSIX-relative syntax independent of the host OS."""
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
    ):
        return False
    parts = value.split("/")
    return all(part and part not in {".", ".."} for part in parts)


def _safe_examples(value: object, limit: int) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_EXAMPLE_LIMIT:
        raise _schema_error()
    output = []
    for item in value:
        if not isinstance(item, dict) or set(item) != EXAMPLE_KEYS:
            raise _schema_error()
        if not all(_safe_text(item[key]) for key in EXAMPLE_KEYS):
            raise _schema_error()
        if not _canonical_posix_relative_path(item["relative_path"]):
            raise _schema_error()
        output.append({key: item[key] for key in sorted(EXAMPLE_KEYS)})
    return output[:limit]


def public_report(report, sensitive=False, example_limit=5) -> dict:
    """Return only validated counts, states, sealed hashes, and opt-in examples."""
    if (
        type(example_limit) is not int
        or example_limit < 0
        or example_limit > MAX_EXAMPLE_LIMIT
        or not isinstance(report, dict)
    ):
        raise _schema_error()
    aliases = {
        "restored": ("restored", "restored_this_invocation"),
    }
    output = {}
    for key in sorted(PUBLIC_KEYS - {"errors"}):
        candidates = aliases.get(key, (key,))
        value = next((report[name] for name in candidates if name in report), None)
        if not any(name in report for name in candidates):
            continue
        if key == "applicable":
            if type(value) is not bool:
                raise _schema_error()
        elif key == "state":
            if not isinstance(value, str) or value not in _PUBLIC_STATES:
                raise _schema_error()
        elif key == "manifest_sha256":
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise _schema_error()
        elif key in _COUNT_KEYS:
            if type(value) is not int or value < 0:
                raise _schema_error()
        output[key] = value
    if "errors" in report:
        if not isinstance(report["errors"], list):
            raise _schema_error()
        output["errors"] = [_safe_error_code(error) for error in report["errors"]]
    if sensitive:
        output["examples"] = _safe_examples(report.get("_examples", []), example_limit)
    return output


def _manifest_metadata(run_dir: str) -> dict:
    manifest, manifest_sha, _state = load_sealed_run(run_dir)
    projection = manifest["projection"]
    return {
        "manifest_sha256": manifest_sha,
        "profile_version": manifest["taxonomy_version"],
        "topic_change_count": len(projection["topic_changes"]),
        "rendered_topic_count": len(projection["render_topic_ids"]),
        "managed_index_count": len(projection["managed_date_index_paths"]),
        "file_count": len(manifest["files"]),
    }


def _sensitive_examples(config: dict, run_dir: str, limit: int) -> list[dict]:
    if limit == 0:
        return []
    manifest, _manifest_sha, _state = load_sealed_run(run_dir)
    topic_files = [item for item in manifest["files"] if item["kind"] == "topic"]
    db_path = Path(config["monitor_knowledge_db"]).expanduser().absolute()
    uri = f"file:{quote(db_path.as_posix(), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        usernames = set()
        for key in ("monitor_chat_taxonomy_profiles", "monitor_chat_aliases"):
            value = config.get(key)
            if isinstance(value, dict):
                usernames.update(item for item in value if isinstance(item, str))
        singleton = config.get("monitor_chat_username")
        if isinstance(singleton, str) and singleton:
            usernames.add(singleton)
        chats = config.get("monitor_chats")
        if isinstance(chats, list):
            usernames.update(
                item["username"]
                for item in chats
                if isinstance(item, dict)
                and isinstance(item.get("username"), str)
                and item["username"]
            )
        def privacy_key(value: str) -> str:
            """Use NFKC plus casefold on both forbidden and candidate text."""
            return unicodedata.normalize("NFKC", value).casefold()

        forbidden = {
            privacy_key(value)
            for username in usernames
            for value in (username, safe_path_part(username, ""))
            if value
        }
        examples = []
        for item in topic_files:
            row = conn.execute(
                "SELECT title FROM topics WHERE topic_id = ?",
                (item["topic_id"],),
            ).fetchone()
            if row is None or not isinstance(row[0], str):
                raise MigrationError(
                    "sensitive_example_identity",
                    "sensitive example identity is unavailable",
                )
            example = {
                "relative_path": item["destination_relative_path"],
                "title": row[0],
            }
            public_text = privacy_key("\n".join(example.values()))
            if any(value in public_text for value in forbidden):
                continue
            examples.append(example)
            if len(examples) >= limit:
                break
        return examples
    finally:
        conn.close()


def _print_report(report: dict, *, as_json: bool, stream=None) -> None:
    stream = stream or sys.stdout
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), file=stream)
        return
    for key in sorted(report):
        value = report[key]
        if isinstance(value, (list, dict, bool)) or value is None:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        print(f"{key}: {value}", file=stream)


def _print_error(error, *, as_json: bool) -> None:
    report = public_report({"applicable": False, "errors": [error]})
    _print_report(report, as_json=as_json, stream=sys.stdout if as_json else sys.stderr)


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in arguments
    try:
        args = build_parser().parse_args(arguments)
    except CliUsageError as exc:
        _print_error(exc, as_json=json_requested)
        return 2

    if args.command in {"preview", "status"} and not 0 <= args.example_limit <= MAX_EXAMPLE_LIMIT:
        _print_error(
            MigrationError("example_limit", "example limit is out of range"),
            as_json=args.json,
        )
        return 2

    try:
        config = None
        if args.command in {"preview", "status", "apply"}:
            config = load_config()
        if args.command == "preview":
            report = preview_migration(config, args.profile, args.run_dir)
        elif args.command == "status":
            report = status_migration(config, args.run_dir)
        elif args.command == "apply":
            report = apply_migration(
                config, args.run_dir, args.manifest_sha256, args.confirm
            )
        else:
            report = rollback_migration(
                args.run_dir, args.manifest_sha256, args.confirm
            )

        report = dict(report)
        try:
            report.update(_manifest_metadata(args.run_dir))
        except (KeyError, TypeError):
            # Mocked unit boundaries may return a complete safe report without
            # creating a sealed run. Real sealed-run errors remain fail closed.
            pass
        if args.command in {"preview", "status"} and args.sensitive:
            report["_examples"] = _sensitive_examples(
                config, args.run_dir, args.example_limit
            )
        output = public_report(
            report,
            sensitive=getattr(args, "sensitive", False),
            example_limit=getattr(args, "example_limit", 5),
        )
        _print_report(output, as_json=args.json)
        expected = {"apply": "applied", "rollback": "rolled_back"}.get(args.command)
        return 0 if expected is None or output.get("state") == expected else 1
    except Exception as exc:
        _print_error(exc, as_json=args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
