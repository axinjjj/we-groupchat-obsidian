"""Local review queue for actionable WeChat monitor hits.

The queue stores derived review items with enough provenance for a human/Codex
review pass without copying raw chat bodies or touching WeChat data.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import DATA_DIR, ensure_private_dir, ensure_private_file
from .link_preview import is_wechat_record_url
from .url_safety import redact_url_for_display, redact_urls_in_text

QUEUE_DIR = os.path.join(DATA_DIR, "review_queue")
PENDING_FILE = "pending.jsonl"
SCHEMA_VERSION = "review_queue.v1"
SUGGESTED_ACTIONS = {
    "read_note",
    "import_resource",
    "evaluate_reference",
    "review_risk",
    "follow_up_resource",
    "archive_reference",
    "ignore",
}
ACTIONABILITIES = {
    "none",
    "follow_up_resource",
    "import_resource",
    "evaluate_reference",
    "review_risk",
}
QUEUE_ACTIONABILITIES = {
    "follow_up_resource",
    "import_resource",
    "evaluate_reference",
    "review_risk",
}
SIGNAL_LEVELS = {"high", "medium", "low"}
ACTIVE_STATUS = "pending"
TERMINAL_STATUSES = {"reviewed", "ignored", "imported"}
ALL_STATUSES = {ACTIVE_STATUS, *TERMINAL_STATUSES}
RESOURCE_STATUSES = {
    "attached",
    "linked",
    "mentioned_private",
    "mentioned_pending",
    "none",
}
PENDING_RESOURCE_STATUSES = {"mentioned_private", "mentioned_pending"}

P1_SUBJECT_KEYWORDS = (
    "security",
    "credential",
    "secret",
    "token",
    "api key",
    "apikey",
    "password",
    "cookie",
    "account",
    "auth",
    "oauth",
    "权限",
    "密钥",
    "泄漏",
    "登录",
    "账号",
    "vps",
    "infrastructure",
    "production",
    "生产",
)

P1_RISK_KEYWORDS = (
    "risk",
    "leak",
    "leaked",
    "exposed",
    "exposure",
    "compromised",
    "stolen",
    "unauthorized",
    "revoked",
    "revoke",
    "incident",
    "breach",
    "accidentally",
    "pasted publicly",
    "deploy risk",
    "泄漏",
    "泄露",
    "暴露",
    "被盗",
    "盗用",
    "未授权",
    "异常访问",
    "撤销",
    "吊销",
    "安全事故",
    "风险",
    "入侵",
    "攻破",
    "误发",
    "公开粘贴",
)

DERIVED_FIELDS = {
    "priority",
    "suggested_action",
    "signal_level",
    "actionability",
    "queue_worthy",
}

P2_KEYWORDS = (
    "source package",
    "github",
    "repo",
    "patch",
    "deploy",
    "telegram",
    "tg-",
    "mcp",
    "hook",
    "runtime",
    "二改",
    "部署",
    "源码",
    "仓库",
    "ai intimacy",
    "ai亲密",
    "ai 亲密",
    "人机恋技巧",
    "互动玩法",
    "新玩法",
    "玩法",
    "私密语音",
    "语音通话",
    "亲密感知",
    "活人感",
    "身体体验",
    "触觉",
    "传感",
    "智能床垫",
    "涩涩插件",
    "色色",
)


def _now_text(now_func=time.time) -> str:
    return datetime.fromtimestamp(now_func()).isoformat(timespec="seconds")


def _clean_text(value, limit=400) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    return text[:limit]


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _normalize_resource_status(value) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "private": "mentioned_private",
        "privately_mentioned": "mentioned_private",
        "pending": "mentioned_pending",
        "to_be_shared": "mentioned_pending",
        "not_shared_yet": "mentioned_pending",
        "file": "attached",
        "files": "attached",
        "attachment": "attached",
        "url": "linked",
        "link": "linked",
    }
    text = aliases.get(text, text)
    return text if text in RESOURCE_STATUSES else "none"


def _normalize_links(value, limit=20) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    links = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if is_wechat_record_url(text):
            continue
        text = redact_url_for_display(text)
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        links.append(text[:500])
        if len(links) >= limit:
            break
    return links


def _has_wechat_record_link(value) -> bool:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return False
    return any(is_wechat_record_url(str(item or "").strip()) for item in value)


def _normalize_files(value, limit=20) -> list[dict[str, str]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    files = []
    seen = set()
    for item in value:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), 180)
        if not name:
            continue
        ref = {
            "name": name,
            "month": _clean_text(item.get("month"), 7),
            "month_dir": str(item.get("month_dir") or "").strip(),
            "sender": _clean_text(item.get("sender"), 80),
            "time": _clean_text(item.get("time"), 40),
        }
        key = (ref["name"].casefold(), ref["month"], ref["sender"].casefold(), ref["time"])
        if key in seen:
            continue
        seen.add(key)
        files.append(ref)
        if len(files) >= limit:
            break
    return files


def _resource_key(resources: dict) -> str:
    files = [
        {
            "name": item.get("name", ""),
            "month": item.get("month", ""),
            "sender": item.get("sender", ""),
            "time": item.get("time", ""),
        }
        for item in _normalize_files(resources.get("files"))
    ]
    links = _normalize_links(resources.get("links"))
    return json.dumps({"files": files, "links": links}, ensure_ascii=False, sort_keys=True)


def stable_review_id(item: dict) -> str:
    resources = item.get("resources") or {}
    resources_key = _resource_key(resources)
    has_resources = bool(_normalize_files(resources.get("files")) or _normalize_links(resources.get("links")))
    lead_key = _clean_text(item.get("lead_key"), 160)
    if item.get("resource_lead") and lead_key and not has_resources:
        basis = {
            "source_chat": item.get("source_chat", ""),
            "lead_key": lead_key,
            "resources": resources_key,
        }
    elif item.get("message_hash") or has_resources:
        basis = {
            "message_hash": item.get("message_hash", ""),
            "resources": resources_key,
        }
    else:
        basis = {
            "source_chat": item.get("source_chat", ""),
            "title": item.get("title", ""),
            "window_start": item.get("window_start", ""),
            "window_end": item.get("window_end", ""),
        }
    digest = hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"rq-{digest[:16]}"


def _haystack(item: dict) -> str:
    resources = item.get("resources") or {}
    files = " ".join(ref.get("name", "") for ref in _normalize_files(resources.get("files")))
    links = " ".join(_normalize_links(resources.get("links")))
    return " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("summary") or ""),
            str(item.get("resource_status") or ""),
            str(item.get("lead_key") or ""),
            files,
            links,
        ]
    ).casefold()


def _contains_any(text: str, patterns: Iterable[str]) -> bool:
    return any(pattern.casefold() in text for pattern in patterns)


def priority_for_item(item: dict) -> str:
    if str(item.get("actionability") or "").strip() == "review_risk":
        return "P1"
    text = _haystack(item)
    if _contains_any(text, P1_SUBJECT_KEYWORDS) and _contains_any(text, P1_RISK_KEYWORDS):
        return "P1"
    resources = item.get("resources") or {}
    files = _normalize_files(resources.get("files"))
    links = _normalize_links(resources.get("links"))
    resource_status = _normalize_resource_status(item.get("resource_status"))
    if files:
        return "P2"
    if item.get("resource_lead") or resource_status != "none":
        return "P2"
    if links and _contains_any(text, P2_KEYWORDS):
        return "P2"
    if _contains_any(text, P2_KEYWORDS):
        return "P2"
    return "P3"


def suggested_action_for_item(item: dict) -> str:
    priority = item.get("priority") or priority_for_item(item)
    resources = item.get("resources") or {}
    files = _normalize_files(resources.get("files"))
    links = _normalize_links(resources.get("links"))
    text = _haystack(item)
    resource_status = _normalize_resource_status(item.get("resource_status"))
    if priority == "P1":
        return "review_risk"
    if files:
        return "import_resource"
    if resource_status in PENDING_RESOURCE_STATUSES or (item.get("resource_lead") and not links):
        return "follow_up_resource"
    if links and _contains_any(text, P2_KEYWORDS):
        return "evaluate_reference"
    if links:
        return "read_note"
    if priority == "P2":
        return "read_note"
    return "archive_reference"


def actionability_for_item(item: dict) -> str:
    explicit = str(item.get("actionability") or "").strip()
    if explicit in ACTIONABILITIES:
        return explicit

    action = item.get("suggested_action") or suggested_action_for_item(item)
    if action in QUEUE_ACTIONABILITIES:
        return action
    return "none"


def signal_level_for_item(item: dict) -> str:
    explicit = str(item.get("signal_level") or "").strip().lower()
    if explicit in SIGNAL_LEVELS:
        return explicit
    priority = item.get("priority") or priority_for_item(item)
    if priority in {"P1", "P2"}:
        return "high"
    return "medium" if priority == "P3" else "low"


def queue_worthy_for_item(item: dict) -> bool:
    return actionability_for_item(item) in QUEUE_ACTIONABILITIES


class ReviewQueue:
    """Append-only local JSONL queue with a latest-state view by id."""

    def __init__(self, queue_dir: str | os.PathLike[str] = QUEUE_DIR, now_func=time.time):
        self.queue_dir = os.path.abspath(os.path.expanduser(str(queue_dir)))
        self.pending_path = os.path.join(self.queue_dir, PENDING_FILE)
        self.now_func = now_func

    @classmethod
    def from_config(cls, config: dict | None = None, now_func=time.time) -> "ReviewQueue":
        config = config or {}
        queue_dir = config.get("review_queue_dir") or QUEUE_DIR
        return cls(queue_dir, now_func=now_func)

    def build_item(self, data: dict) -> dict:
        resources = data.get("resources") or {}
        files = _normalize_files(resources.get("files"))
        links = _normalize_links(resources.get("links"))
        has_private_record_link = _has_wechat_record_link(resources.get("links"))
        resource_lead = _truthy(data.get("resource_lead"))
        resource_status = _normalize_resource_status(data.get("resource_status"))
        if resource_status == "linked" and not files and not links and has_private_record_link:
            resource_status = "mentioned_private"
            resource_lead = True
        if resource_status == "none":
            if files:
                resource_status = "attached"
            elif resource_lead:
                resource_status = "mentioned_pending"
        if resource_status in PENDING_RESOURCE_STATUSES:
            resource_lead = True
        item = {
            "schema_version": SCHEMA_VERSION,
            "id": "",
            "status": str(data.get("status") or ACTIVE_STATUS).strip() or ACTIVE_STATUS,
            "created_at": _clean_text(data.get("created_at"), 40) or _now_text(self.now_func),
            "priority": _clean_text(data.get("priority"), 8),
            "suggested_action": _clean_text(data.get("suggested_action"), 40),
            "signal_level": _clean_text(data.get("signal_level"), 16),
            "actionability": _clean_text(data.get("actionability"), 40),
            "queue_worthy": bool(data.get("queue_worthy")) if "queue_worthy" in data else False,
            "source_chat": redact_urls_in_text(
                _clean_text(data.get("source_chat"), 120)
            ),
            "window_start": _clean_text(data.get("window_start"), 40),
            "window_end": _clean_text(data.get("window_end"), 40),
            "title": redact_urls_in_text(
                _clean_text(data.get("title") or "待审阅内容", 160)
            ),
            "summary": redact_urls_in_text(
                _clean_text(data.get("summary"), 1000)
            ),
            "knowledge_topic_id": data.get("knowledge_topic_id"),
            "knowledge_event_id": data.get("knowledge_event_id"),
            "obsidian_path": _clean_text(data.get("obsidian_path"), 500),
            "resource_lead": resource_lead,
            "resource_status": resource_status,
            "lead_key": redact_urls_in_text(
                _clean_text(data.get("lead_key"), 160)
            ),
            "resources": {
                "files": files,
                "links": links,
            },
            "message_hash": _clean_text(data.get("message_hash"), 128),
        }
        item["id"] = _clean_text(data.get("id"), 80) or stable_review_id(item)
        if item["status"] not in ALL_STATUSES:
            item["status"] = ACTIVE_STATUS
        if item["priority"] not in {"P1", "P2", "P3"}:
            item["priority"] = priority_for_item(item)
        if item["suggested_action"] not in SUGGESTED_ACTIONS:
            item["suggested_action"] = suggested_action_for_item(item)
        item["actionability"] = actionability_for_item(item)
        item["signal_level"] = signal_level_for_item(item)
        item["queue_worthy"] = queue_worthy_for_item(item)
        return item

    def create_or_reuse(self, data: dict) -> dict:
        item = self.build_item(data)
        existing = self.get(item["id"])
        if existing:
            return existing
        self._append(item)
        return item

    def list(self, status: str | None = None) -> list[dict]:
        items = list(self._latest_by_id().values())
        if status:
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: (item.get("created_at", ""), item.get("id", "")))

    def pending(self) -> list[dict]:
        return self.list(ACTIVE_STATUS)

    def pending_count(self) -> int:
        return len(self.pending())

    def audit(self, stale_days: int = 14, *, sensitive: bool = False) -> dict:
        pending = self.pending()
        read_note_items = self.legacy_digest_only_items(pending)
        actionable_items = [
            item
            for item in pending
            if item.get("actionability") in QUEUE_ACTIONABILITIES
        ]
        stale_resource_leads = [
            item
            for item in actionable_items
            if item.get("actionability") == "follow_up_resource"
            and self._is_stale(item, stale_days)
        ]
        priority_preview = self._priority_preview(pending)
        risk_rule_preview = self._risk_rule_preview(pending, sensitive=sensitive)
        return {
            "total_pending": len(pending),
            "actionable_items": self._audit_bucket(actionable_items, "keep_actionable"),
            "read_note_legacy_items": self._audit_bucket(read_note_items, "archive_digest_only"),
            "stale_resource_leads": self._audit_bucket(stale_resource_leads, "review_or_archive"),
            "priority_preview": priority_preview,
            "risk_rule_preview": risk_rule_preview,
        }

    def rederive_item(self, item: dict) -> dict:
        """Rebuild derived fields from source data without appending queue state."""
        source = {key: value for key, value in item.items() if key not in DERIVED_FIELDS}
        return self.build_item(source)

    def _priority_preview(self, pending: list[dict]) -> dict:
        current_counts = {}
        derived_counts = {}
        transitions = {}
        changed_ids = []
        for item in pending:
            current = str(item.get("priority") or "P3")
            derived = self.rederive_item(item)["priority"]
            current_counts[current] = current_counts.get(current, 0) + 1
            derived_counts[derived] = derived_counts.get(derived, 0) + 1
            if current == derived:
                continue
            transition = f"{current}->{derived}"
            transitions[transition] = transitions.get(transition, 0) + 1
            if len(changed_ids) < 20:
                changed_ids.append(str(item.get("id") or ""))
        return {
            "current_counts": dict(sorted(current_counts.items())),
            "derived_counts": dict(sorted(derived_counts.items())),
            "would_change_count": sum(transitions.values()),
            "transitions": dict(sorted(transitions.items())),
            "changed_ids": changed_ids,
            "applied": False,
        }

    def _risk_rule_preview(self, pending: list[dict], *, sensitive: bool) -> dict:
        current_p1 = [item for item in pending if item.get("priority") == "P1"]
        remains_p1 = 0
        resulting_actionability_counts = {}
        examples = []
        for item in current_p1:
            derived = self.rederive_item(item)
            actionability = str(derived.get("actionability") or "none")
            resulting_actionability_counts[actionability] = (
                resulting_actionability_counts.get(actionability, 0) + 1
            )
            if derived.get("priority") == "P1":
                remains_p1 += 1
                continue
            if len(examples) < 5:
                example = {"id": str(item.get("id") or "")}
                if sensitive:
                    example["title"] = str(item.get("title") or "")
                examples.append(example)
        return {
            "current_pending_p1_count": len(current_p1),
            "remains_p1_count": remains_p1,
            "would_downgrade_count": len(current_p1) - remains_p1,
            "resulting_actionability_counts": dict(sorted(resulting_actionability_counts.items())),
            "examples": examples,
            "applied": False,
        }

    def legacy_digest_only_items(self, pending: list[dict] | None = None) -> list[dict]:
        pending = self.pending() if pending is None else pending
        return [
            item
            for item in pending
            if item.get("suggested_action") in {"read_note", "archive_reference"}
            and not item.get("queue_worthy")
        ]

    def cleanup_legacy_digest_only(
        self,
        *,
        dry_run: bool = True,
        status: str = "reviewed",
        limit: int | None = None,
    ) -> dict:
        status = str(status or "").strip()
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"unsupported review status: {status}")

        all_items = self.legacy_digest_only_items()
        items = all_items
        if limit is not None:
            limit = max(0, int(limit))
            items = items[:limit]

        updated = []
        if not dry_run:
            for item in items:
                updated.append(self.mark(item["id"], status))

        result_items = updated if updated else items
        return {
            "applied": not dry_run,
            "status": status,
            "reason": "legacy_digest_only",
            "matched_count": len(all_items),
            "selected_count": len(items),
            "updated_count": len(updated),
            "items": [
                {
                    "id": item.get("id", ""),
                    "title": item.get("title", ""),
                    "suggested_action": item.get("suggested_action", ""),
                    "actionability": item.get("actionability", "none"),
                    "status": item.get("status", ""),
                }
                for item in result_items
            ],
        }

    def _audit_bucket(self, items: list[dict], recommended_action: str) -> dict:
        examples = [
            {
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "suggested_action": item.get("suggested_action", ""),
                "actionability": item.get("actionability", "none"),
            }
            for item in items[:5]
        ]
        return {
            "count": len(items),
            "recommended_action": recommended_action,
            "examples": examples,
        }

    def _is_stale(self, item: dict, stale_days: int) -> bool:
        try:
            created_ts = datetime.fromisoformat(str(item.get("created_at", ""))).timestamp()
        except ValueError:
            return False
        return self.now_func() - created_ts > stale_days * 24 * 60 * 60

    def get(self, item_id: str) -> dict | None:
        return self._latest_by_id().get(str(item_id or "").strip())

    def mark(self, item_id: str, status: str) -> dict:
        status = str(status or "").strip()
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"unsupported review status: {status}")
        item = self.get(item_id)
        if not item:
            raise KeyError(f"review queue item not found: {item_id}")
        updated = dict(item)
        updated["status"] = status
        updated["updated_at"] = _now_text(self.now_func)
        self._append(updated)
        return updated

    def _append(self, item: dict) -> None:
        ensure_private_dir(self.queue_dir)
        with open(self.pending_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        ensure_private_file(self.pending_path)

    def _latest_by_id(self) -> dict[str, dict]:
        path = Path(self.pending_path)
        if not path.exists():
            return {}
        latest: dict[str, dict] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if item_id:
                latest[item_id] = self.build_item(item)
        return latest
