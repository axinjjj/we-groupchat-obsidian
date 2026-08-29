"""One-shot macOS WeChat source availability guard.

The guard owns only the decision to request a normal background application
launch. It never reads chat content, refreshes keys, drives UI, or terminates
WeChat. TopicMonitor intentionally does not import this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import uuid
from typing import Callable

from . import file_lock as fcntl

from .key_extractor import get_cached_keys, is_wechat_running, process_lookup_available
from .project_identity import data_dir


STATE_SCHEMA_VERSION = 1
GUARD_STATES = {
    "disabled",
    "healthy",
    "missing_grace",
    "restart_backoff",
    "paused",
    "degraded",
    "process_lookup_unknown",
}
OPEN_WECHAT_ARGV = ["open", "-g", "-a", "WeChat"]


def source_guard_dir(home: str | os.PathLike[str] | None = None) -> Path:
    return data_dir(home) / "source_guard"


def state_path(home: str | os.PathLike[str] | None = None) -> Path:
    return source_guard_dir(home) / "state.json"


def receipts_dir(home: str | os.PathLike[str] | None = None) -> Path:
    return source_guard_dir(home) / "receipts"


def lock_path(home: str | os.PathLike[str] | None = None) -> Path:
    return source_guard_dir(home) / "source_guard.lock"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def atomic_write_json(path: Path, payload: dict) -> None:
    _private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def default_state() -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "state": "disabled",
        "last_check_at": 0,
        "last_seen_running_at": 0,
        "missing_since": 0,
        "last_restart_attempt_at": 0,
        "restart_attempts_in_window": 0,
        "restart_attempt_timestamps": [],
        "backoff_until": 0,
        "pause_until": "",
        "last_result": "never_checked",
        "last_error_code": "",
        "source_freshness": "unknown",
        "last_source_mtime": 0,
        "stale_episode_started_at": 0,
        "stale_episode_notified_at": 0,
        "last_notification_key": "",
        "last_notification_at": 0,
    }


def load_state(path: Path | None = None) -> dict:
    path = path or state_path()
    state = default_state()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return state
    if not isinstance(loaded, dict):
        return state
    state.update(loaded)
    if state.get("state") not in GUARD_STATES:
        state["state"] = "degraded"
        state["last_error_code"] = "invalid_persisted_state"
    return state


@contextmanager
def guard_lock(path: Path):
    _private_dir(path.parent)
    handle = path.open("a+b")
    try:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _pause_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text == "indefinite":
        return float("inf")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def pause_until_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_process_probe() -> bool | None:
    if not process_lookup_available():
        return None
    return bool(is_wechat_running())


def latest_source_mtime(db_dir: str | os.PathLike[str]) -> float | None:
    root = Path(db_dir).expanduser()
    if not root.is_dir():
        return None
    latest = 0.0
    keys = get_cached_keys() or {}
    for relative in keys:
        relative_path = Path(str(relative))
        if (
            relative_path.is_absolute()
            or len(relative_path.parts) != 2
            or relative_path.parts[0] != "message"
        ):
            continue
        base = root / relative_path
        for path in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
            try:
                value = path.stat().st_mtime
            except OSError:
                continue
            latest = max(latest, value)
    return latest or None


def launch_wechat(runner: Callable = subprocess.run) -> subprocess.CompletedProcess:
    return runner(OPEN_WECHAT_ARGV, capture_output=True, text=True)


def notify_user(title: str, message: str, runner: Callable = subprocess.run) -> None:
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    runner(["osascript", "-e", script, title, message], capture_output=True, text=True)


class WeChatSourceGuard:
    def __init__(
        self,
        config: dict,
        *,
        state_file: Path | None = None,
        receipt_root: Path | None = None,
        lock_file: Path | None = None,
        now_func: Callable[[], float] = time.time,
        process_probe: Callable[[], bool | None] = default_process_probe,
        launch_runner: Callable = subprocess.run,
        notifier: Callable[[str, str], None] = notify_user,
        source_mtime_func: Callable[[str | os.PathLike[str]], float | None] = latest_source_mtime,
    ):
        self.config = dict(config or {})
        self.state_file = Path(state_file or state_path())
        self.receipt_root = Path(receipt_root or receipts_dir())
        self.lock_file = Path(lock_file or lock_path())
        self.now_func = now_func
        self.process_probe = process_probe
        self.launch_runner = launch_runner
        self.notifier = notifier
        self.source_mtime_func = source_mtime_func

    def _int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        return int(value) if isinstance(value, int) else default

    def _save(self, state: dict) -> dict:
        state["schema_version"] = STATE_SCHEMA_VERSION
        atomic_write_json(self.state_file, state)
        return dict(state)

    def _prune_attempts(self, state: dict, now: float) -> list[float]:
        window = self._int("wechat_source_guard_restart_window_seconds", 1800)
        attempts = []
        for value in state.get("restart_attempt_timestamps") or []:
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                continue
            if timestamp >= now - window:
                attempts.append(timestamp)
        state["restart_attempt_timestamps"] = attempts
        state["restart_attempts_in_window"] = len(attempts)
        return attempts

    def _notify_once(self, state: dict, key: str, title: str, message: str, now: float) -> None:
        cooldown = self._int("wechat_source_guard_notification_cooldown_seconds", 3600)
        same_key = state.get("last_notification_key") == key
        recent = now - float(state.get("last_notification_at") or 0) < cooldown
        if same_key and recent:
            return
        try:
            self.notifier(title, message)
        except Exception:
            return
        state["last_notification_key"] = key
        state["last_notification_at"] = now

    def _notify_stale_episode(self, state: dict, now: float) -> None:
        if not float(state.get("stale_episode_started_at") or 0):
            state["stale_episode_started_at"] = now
        if float(state.get("stale_episode_notified_at") or 0):
            return
        try:
            self.notifier(
                "微信数据源需要检查",
                "微信仍在运行，但本地数据库长时间没有更新；请确认登录与同步状态。",
            )
        except Exception:
            return
        state["stale_episode_notified_at"] = now

    @staticmethod
    def _clear_stale_episode(state: dict) -> None:
        state["stale_episode_started_at"] = 0
        state["stale_episode_notified_at"] = 0

    def _write_receipt(self, payload: dict, now: float) -> Path:
        _private_dir(self.receipt_root)
        receipt = self.receipt_root / f"{int(now)}-{uuid.uuid4().hex[:12]}.json"
        atomic_write_json(receipt, {"schema_version": 1, "created_at": now, **payload})
        return receipt

    def _source_freshness(self, state: dict, now: float) -> str:
        db_dir = str(self.config.get("db_dir") or "")
        mtime = self.source_mtime_func(db_dir) if db_dir else None
        state["last_source_mtime"] = float(mtime or 0)
        if mtime is None:
            return "unknown"
        stale_seconds = self._int("wechat_source_guard_stale_seconds", 7200)
        return "stale" if now - mtime > stale_seconds else "fresh"

    def check(self) -> dict:
        now = float(self.now_func())
        with guard_lock(self.lock_file) as acquired:
            if not acquired:
                state = load_state(self.state_file)
                return {**state, "last_result": "busy"}

            state = load_state(self.state_file)
            state["last_check_at"] = now
            attempts = self._prune_attempts(state, now)

            if not bool(self.config.get("wechat_source_guard_enabled", False)):
                self._clear_stale_episode(state)
                state.update({
                    "state": "disabled",
                    "pause_until": str(self.config.get("wechat_source_guard_pause_until") or ""),
                    "last_result": "disabled",
                    "last_error_code": "",
                })
                return self._save(state)

            pause_text = str(self.config.get("wechat_source_guard_pause_until") or "").strip()
            pause_ts = _pause_timestamp(pause_text)
            state["pause_until"] = pause_text
            if pause_ts is not None and pause_ts > now:
                state.update({"state": "paused", "last_result": "paused", "last_error_code": ""})
                return self._save(state)

            running = self.process_probe()
            if running is None:
                state.update({
                    "state": "process_lookup_unknown",
                    "last_result": "process_lookup_unknown",
                    "last_error_code": "process_lookup_unknown",
                })
                return self._save(state)

            if running:
                freshness = self._source_freshness(state, now)
                state.update({
                    "state": "healthy",
                    "last_seen_running_at": now,
                    "missing_since": 0,
                    "last_restart_attempt_at": 0,
                    "restart_attempts_in_window": 0,
                    "restart_attempt_timestamps": [],
                    "backoff_until": 0,
                    "last_result": "stale_source" if freshness == "stale" else "healthy",
                    "last_error_code": "",
                    "source_freshness": freshness,
                })
                if freshness == "stale":
                    self._notify_stale_episode(state, now)
                else:
                    self._clear_stale_episode(state)
                return self._save(state)

            self._clear_stale_episode(state)
            state["source_freshness"] = "unavailable"
            missing_since = float(state.get("missing_since") or 0)
            if not missing_since:
                state.update({
                    "state": "missing_grace",
                    "missing_since": now,
                    "last_result": "missing_grace_started",
                    "last_error_code": "",
                })
                return self._save(state)

            grace = self._int("wechat_source_guard_grace_seconds", 300)
            if now - missing_since < grace:
                state.update({"state": "missing_grace", "last_result": "missing_grace"})
                return self._save(state)

            backoff_until = float(state.get("backoff_until") or 0)
            if backoff_until > now:
                state.update({"state": "restart_backoff", "last_result": "restart_backoff"})
                return self._save(state)

            budget = self._int("wechat_source_guard_restart_budget", 3)
            if len(attempts) >= budget:
                state.update({
                    "state": "degraded",
                    "restart_attempts_in_window": len(attempts),
                    "last_result": "restart_budget_exhausted",
                    "last_error_code": "restart_budget_exhausted",
                })
                self._notify_once(
                    state,
                    "restart_budget_exhausted",
                    "微信数据源保活已暂停",
                    "微信多次未能恢复，source guard 已停止继续拉起；请人工检查登录状态。",
                    now,
                )
                return self._save(state)

            result = launch_wechat(self.launch_runner)
            attempts.append(now)
            state["restart_attempt_timestamps"] = attempts
            state["restart_attempts_in_window"] = len(attempts)
            state["last_restart_attempt_at"] = now
            base = self._int("wechat_source_guard_backoff_base_seconds", 300)
            backoff = base * (2 ** max(0, len(attempts) - 1))
            state["backoff_until"] = now + backoff
            succeeded = result.returncode == 0
            if succeeded:
                state.update({
                    "state": "restart_backoff",
                    "last_result": "launch_requested",
                    "last_error_code": "",
                })
            else:
                error_code = f"launch_exit_{result.returncode}"
                state.update({
                    "state": "degraded" if len(attempts) >= budget else "restart_backoff",
                    "last_result": "launch_failed",
                    "last_error_code": error_code,
                })
                self._notify_once(
                    state,
                    error_code,
                    "微信数据源启动失败",
                    "source guard 无法通过正常的 macOS application launch 打开微信；请人工检查。",
                    now,
                )

            self._write_receipt(
                {
                    "reason": "missing_after_grace",
                    "grace_elapsed": True,
                    "budget_used": len(attempts),
                    "budget_limit": budget,
                    "launch_result": "requested" if succeeded else "failed",
                    "error_code": state["last_error_code"],
                    "next_state": state["state"],
                    "backoff_until": state["backoff_until"],
                },
                now,
            )
            return self._save(state)


def source_guard_status(config: dict, *, state_file: Path | None = None, now: float | None = None) -> dict:
    state = load_state(state_file or state_path())
    current = float(time.time() if now is None else now)
    persisted_state = state.get("state") or "disabled"
    enabled = bool(config.get("wechat_source_guard_enabled", False))
    pause_text = str(config.get("wechat_source_guard_pause_until") or "").strip()
    pause_timestamp = _pause_timestamp(pause_text)
    if not enabled:
        effective_state = "disabled"
        effective_result = "disabled"
    elif pause_timestamp is not None and pause_timestamp > current:
        effective_state = "paused"
        effective_result = "paused"
    else:
        effective_state = persisted_state
        effective_result = state.get("last_result") or "never_checked"
    attempts = []
    window = int(config.get("wechat_source_guard_restart_window_seconds", 1800) or 1800)
    for value in state.get("restart_attempt_timestamps") or []:
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value >= current - window:
            attempts.append(value)
    budget = int(config.get("wechat_source_guard_restart_budget", 3) or 3)
    return {
        **state,
        "state": effective_state,
        "last_result": effective_result,
        "persisted_state": persisted_state,
        "pause_until": pause_text,
        "enabled": enabled,
        "restart_budget_remaining": max(0, budget - len(attempts)),
        "missing_duration": max(0, current - float(state.get("missing_since") or current)) if state.get("missing_since") else 0,
    }
