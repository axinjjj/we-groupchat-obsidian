#!/usr/bin/env python3
"""Audit relation integrity or run the explicitly confirmed exact repair."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.relation_audit import (
    RelationRepairError,
    audit_relations,
    repair_known_invalid_relations,
)
from core.config import load_config

REPAIR_CONFIRM_TOKEN = "DELETE_EXACT_KNOWN_INVALID_RELATIONS"


def build_parser():
    parser = argparse.ArgumentParser(
        description="audit-only diagnostics plus an explicitly confirmed exact known-invalid repair",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit_parser = subparsers.add_parser("audit", help="Run the read-only relation audit")
    audit_parser.add_argument(
        "--db",
        default="",
        help="Knowledge SQLite path; defaults to monitor_knowledge_db from local config",
    )
    audit_parser.add_argument("--sensitive", action="store_true", help="Include bounded topic titles in examples")
    audit_parser.add_argument("--example-limit", type=int, default=5, help="Maximum number of examples")
    audit_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    repair_parser = subparsers.add_parser(
        "apply-known-invalid",
        help="Delete only exact known-invalid missing-method update edges",
    )
    repair_parser.add_argument(
        "--db",
        default="",
        help="Knowledge SQLite path; defaults to monitor_knowledge_db from local config",
    )
    repair_parser.add_argument("--backup", required=True, help="New SQLite backup path; must not exist")
    repair_parser.add_argument("--expect-count", required=True, type=int, help="Fresh exact row count")
    repair_parser.add_argument("--confirm", required=True, help="Exact destructive confirmation token")
    repair_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def _print_text(report):
    print("Knowledge relation audit-only report")
    print(f"available: {report['available']}")
    if report.get("error"):
        print(f"error: {report['error']}")
    print(f"topics: {report['total_topics']}")
    print(f"events: {report['total_events']}")
    print(f"relations: {report['total_relations']}")
    print(f"relation counts: {report['relation_counts']}")
    print(f"known broken reason: {report['known_broken_reason_count']}")
    print(f"broader relation failures: {report['broader_relation_failure_count']}")
    print(f"affected source topics: {report['affected_source_topic_count']}")
    print(f"affected target topics: {report['affected_target_topic_count']}")
    print(f"cross-chat edges: {report['cross_chat_edge_count']}")
    print(f"risky cross-chat edges: {report['cross_chat_risky_edge_count']}")
    print(f"self loops: {report['self_loop_count']}")
    print(f"orphan events: {report['orphan_event_count']}")
    print(f"orphan relations: {report['orphan_relation_count']}")
    print(f"FTS rows: {report['fts_row_count']} (matches topics: {report['fts_matches_topics']})")
    dominant = report.get("dominant_relation") or "none"
    print(f"dominant relation: {dominant} ({report['dominant_relation_ratio']:.1%})")
    print(f"warnings: {', '.join(report['warnings']) or 'none'}")
    if report.get("examples"):
        print("examples:")
        for item in report["examples"]:
            print("  " + json.dumps(item, ensure_ascii=False, sort_keys=True))


def main(argv=None):
    args = build_parser().parse_args(argv)
    db_path = args.db
    if not db_path:
        db_path = str(load_config().get("monitor_knowledge_db") or "")
    if args.command == "audit":
        report = audit_relations(
            db_path,
            sensitive=args.sensitive,
            example_limit=args.example_limit,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _print_text(report)
        return 0 if report.get("available") else 1

    if args.confirm != REPAIR_CONFIRM_TOKEN:
        print("confirmation token mismatch; exact relation repair was not run", file=sys.stderr)
        return 2
    try:
        report = repair_known_invalid_relations(
            db_path,
            backup_path=args.backup,
            expected_count=args.expect_count,
        )
    except (RelationRepairError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for key in sorted(report):
            print(f"{key}: {report[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
