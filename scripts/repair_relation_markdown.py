#!/usr/bin/env python3
"""Run the sealed, exact-line-only stale relation Markdown cleanup."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.relation_markdown_cleanup import (
    CleanupError,
    apply_cleanup,
    load_sealed_run,
    open_read_only_db,
    preview_cleanup,
    rollback_cleanup,
    status_cleanup,
)


PUBLIC_KEYS = {
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
}
EXAMPLE_KEYS = {"relative_path", "source_title", "target_title"}
MAX_EXAMPLE_LIMIT = 20
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()
BOOLEAN_KEYS = {"applicable"}
DIGEST_KEYS = {
    "manifest_sha256",
    "relation_set_digest",
    "risky_warning_set_digest",
}
COUNT_KEYS = PUBLIC_KEYS - BOOLEAN_KEYS - DIGEST_KEYS - {"state", "errors"}
PUBLIC_STATES = {
    "planned",
    "backing_up",
    "backups_verified",
    "applying",
    "applied",
    "rolling_back",
    "rolled_back",
    "drifted",
}


class CliUsageError(ValueError):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, _message):
        raise CliUsageError("cli_usage")


def _add_output_options(parser):
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--sensitive", action="store_true")
    parser.add_argument("--example-limit", type=int, default=5)


def build_parser():
    parser = SafeArgumentParser(
        description="Guarded exact stale relation Markdown cleanup",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=SafeArgumentParser,
    )

    preview_parser = subparsers.add_parser("preview", allow_abbrev=False)
    preview_parser.add_argument("--backup", required=True)
    preview_parser.add_argument("--db", required=True)
    preview_parser.add_argument("--vault-root", required=True)
    preview_parser.add_argument("--obsidian-subdir", required=True)
    preview_parser.add_argument("--run-dir", required=True)
    preview_parser.add_argument("--generator-commit", required=True)
    _add_output_options(preview_parser)

    status_parser = subparsers.add_parser("status", allow_abbrev=False)
    status_parser.add_argument("--run-dir", required=True)
    _add_output_options(status_parser)

    apply_parser = subparsers.add_parser("apply", allow_abbrev=False)
    apply_parser.add_argument("--run-dir", required=True)
    apply_parser.add_argument("--manifest-sha256", required=True)
    apply_parser.add_argument("--confirm", required=True)
    _add_output_options(apply_parser)

    rollback_parser = subparsers.add_parser("rollback", allow_abbrev=False)
    rollback_parser.add_argument("--run-dir", required=True)
    rollback_parser.add_argument("--manifest-sha256", required=True)
    rollback_parser.add_argument("--confirm", required=True)
    _add_output_options(rollback_parser)
    return parser


def _first_present(report, *keys):
    for key in keys:
        if key in report:
            return report[key]
    return _MISSING


def _public_report_schema_error():
    return CleanupError(
        "public_report_schema",
        "public report value does not match the safe schema",
    )


def _validate_public_value(key, value):
    if key in BOOLEAN_KEYS:
        if type(value) is not bool:
            raise _public_report_schema_error()
        return value
    if key == "state":
        if not isinstance(value, str) or value not in PUBLIC_STATES:
            raise _public_report_schema_error()
        return value
    if key in DIGEST_KEYS:
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise _public_report_schema_error()
        return value
    if key in COUNT_KEYS:
        if type(value) is not int or value < 0:
            raise _public_report_schema_error()
        return value
    raise _public_report_schema_error()


def _control_safe_example_text(value):
    return all(
        unicodedata.category(character) != "Cc"
        and character not in {"\u2028", "\u2029"}
        for character in value
    )


def _stable_error_code(error):
    if isinstance(error, str):
        if _STABLE_CODE.fullmatch(error) is None:
            raise _public_report_schema_error()
        return error
    if isinstance(error, CleanupError):
        code = error.code
    elif isinstance(error, json.JSONDecodeError):
        code = "json_error"
    elif isinstance(error, sqlite3.Error):
        code = "sqlite_error"
    elif isinstance(error, OSError):
        code = "os_error"
    elif isinstance(error, CliUsageError):
        code = "cli_usage"
    elif isinstance(error, ValueError):
        code = "value_error"
    else:
        raise _public_report_schema_error()
    if not isinstance(code, str) or _STABLE_CODE.fullmatch(code) is None:
        return "cleanup_error"
    return code


def _validated_sensitive_examples(examples, example_limit):
    if not isinstance(examples, list) or len(examples) > MAX_EXAMPLE_LIMIT:
        raise _public_report_schema_error()
    validated_examples = []
    for example in examples:
        if not isinstance(example, dict) or set(example) != EXAMPLE_KEYS:
            raise _public_report_schema_error()
        if any(
            not isinstance(example[key], str)
            or not _control_safe_example_text(example[key])
            for key in EXAMPLE_KEYS
        ):
            raise _public_report_schema_error()
        validated_examples.append(
            {key: example[key] for key in sorted(EXAMPLE_KEYS)}
        )
    return validated_examples[:example_limit]


def public_report(report, *, sensitive=False, example_limit=5) -> dict:
    """Return only privacy-safe public fields and bounded opt-in examples."""
    if (
        type(example_limit) is not int
        or example_limit < 0
        or example_limit > MAX_EXAMPLE_LIMIT
    ):
        raise _public_report_schema_error()
    if not isinstance(report, dict):
        raise _public_report_schema_error()
    output = {}
    aliases = {
        "source_file_count": ("source_file_count", "unique_source_file_count"),
        "relation_count": ("relation_count", "current_relation_count"),
        "relation_set_digest": (
            "relation_set_digest",
            "current_relation_set_digest",
        ),
        "drifted": ("drifted", "drifted_count"),
    }
    for key in sorted(PUBLIC_KEYS - {"errors"}):
        value = _first_present(report, *aliases.get(key, (key,)))
        if value is not _MISSING:
            output[key] = _validate_public_value(key, value)
    if "errors" in report:
        errors = report["errors"]
        if not isinstance(errors, list):
            raise _public_report_schema_error()
        output["errors"] = [_stable_error_code(error) for error in errors]
    if sensitive:
        output["examples"] = _validated_sensitive_examples(
            report.get("_examples", []), example_limit
        )
    return output


def _sensitive_examples(run_dir, example_limit):
    if example_limit == 0:
        return []
    manifest, _manifest_sha256, _state = load_sealed_run(run_dir)
    source_topic_ids = [record["source_topic_id"] for record in manifest["files"]]
    source_titles = {}
    conn = open_read_only_db(manifest["inputs"]["current_db"])
    try:
        for source_topic_id in source_topic_ids:
            row = conn.execute(
                "SELECT title FROM topics WHERE topic_id = ?",
                (source_topic_id,),
            ).fetchone()
            if row is None or not isinstance(row["title"], str):
                raise CleanupError(
                    "sensitive_example_identity",
                    "sensitive example source identity is unavailable",
                )
            source_titles[source_topic_id] = row["title"]
    finally:
        conn.close()

    examples = []
    for record in manifest["files"]:
        for edge in record["edges"]:
            target_title = edge.get("target_title")
            if not isinstance(target_title, str):
                raise CleanupError(
                    "sensitive_example_identity",
                    "sensitive example target identity is unavailable",
                )
            examples.append(
                {
                    "relative_path": record["relative_path"],
                    "source_title": source_titles[record["source_topic_id"]],
                    "target_title": target_title,
                }
            )
            if len(examples) >= example_limit:
                return examples
    return examples


def _print_report(report, *, as_json, stream=None):
    stream = stream or sys.stdout
    if as_json:
        print(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            file=stream,
        )
        return
    for key in sorted(report):
        value = report[key]
        if isinstance(value, (dict, list, tuple, bool)) or value is None:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        print(f"{key}: {value}", file=stream)


def _print_error(error, *, as_json):
    report = public_report({"applicable": False, "errors": [error]})
    _print_report(report, as_json=as_json, stream=sys.stdout if as_json else sys.stderr)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in arguments
    try:
        args = build_parser().parse_args(arguments)
    except CliUsageError as exc:
        _print_error(exc, as_json=json_requested)
        return 2

    if args.example_limit < 0 or args.example_limit > MAX_EXAMPLE_LIMIT:
        _print_error(
            CleanupError("example_limit", "example limit is out of range"),
            as_json=args.json,
        )
        return 2

    try:
        prevalidated_examples = None
        if args.sensitive and args.command in {"apply", "rollback"}:
            prevalidated_examples = _validated_sensitive_examples(
                _sensitive_examples(args.run_dir, args.example_limit),
                args.example_limit,
            )
        if args.command == "preview":
            report = preview_cleanup(
                backup_db=args.backup,
                current_db=args.db,
                vault_root=args.vault_root,
                obsidian_subdir=args.obsidian_subdir,
                run_dir=args.run_dir,
                generator_commit=args.generator_commit,
            )
        elif args.command == "status":
            report = status_cleanup(args.run_dir)
        elif args.command == "apply":
            report = apply_cleanup(
                args.run_dir,
                args.manifest_sha256,
                args.confirm,
            )
        else:
            report = rollback_cleanup(
                args.run_dir,
                args.manifest_sha256,
                args.confirm,
            )
        if args.sensitive and prevalidated_examples is None:
            report = dict(report)
            report["_examples"] = _sensitive_examples(
                args.run_dir,
                args.example_limit,
            )
        public = public_report(
            report,
            sensitive=args.sensitive and prevalidated_examples is None,
            example_limit=args.example_limit,
        )
        if prevalidated_examples is not None:
            public["examples"] = prevalidated_examples
        _print_report(public, as_json=args.json)
        if args.command == "preview":
            return 0 if public.get("applicable") is True else 1
        if args.command == "apply":
            return 0 if public.get("state") == "applied" else 1
        if args.command == "rollback":
            return 0 if public.get("state") == "rolled_back" else 1
        return 0
    except (
        CleanupError,
        json.JSONDecodeError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        _print_error(exc, as_json=args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
