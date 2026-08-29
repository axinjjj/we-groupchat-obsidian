"""Key extraction - compile and run C scanner to extract DB keys from WeChat process memory."""
import json
import os
import re
import shlex
import subprocess
import sys

from .config import APP_DIR, DATA_DIR, ensure_private_file, load_config


C_SOURCE = os.path.join(APP_DIR, "c_src", "find_keys_macos.c")
C_BINARY = os.path.join(DATA_DIR, "find_keys_macos")
KEYS_FILE = os.path.join(DATA_DIR, "all_keys.json")
EXTRACT_LOG = os.path.join(DATA_DIR, "extract_keys.log")
DEFAULT_WECHAT_APP = "/Applications/WeChat.app"
WECHAT_PROCESS_NAMES = ("WeChat", "WeChatAppEx", "微信")
WECHAT_PROCESS_PATTERNS = (
    r"/WeChat\.app/Contents/MacOS/WeChat($| )",
    r"/WeChatAppEx\.app/Contents/MacOS/WeChatAppEx($| )",
)
REQUIRED_DATABASE_PATTERNS = (
    re.compile(r"^contact/contact\.db$"),
    re.compile(r"^session/session\.db$"),
    re.compile(r"^emoticon/emoticon\.db$"),
    re.compile(r"^message/(?:biz_)?message_\d+\.db$"),
    re.compile(r"^message/message_fts\.db$"),
)


def _first_pid(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None
    except Exception:
        return None


def process_lookup_available():
    """Return whether this process can inspect the platform process list."""
    if sys.platform == "win32":
        from .windows_key_extractor import process_lookup_available as windows_lookup

        return windows_lookup()
    try:
        pid = str(os.getpid())
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "pid="],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and any(
            line.strip() == pid for line in result.stdout.splitlines()
        )
    except Exception:
        return False


def get_wechat_pid():
    """Get WeChat main process PID for key scanning."""
    if sys.platform == "win32":
        from .windows_key_extractor import get_wechat_pids

        pids = get_wechat_pids()
        return pids[0] if pids else None
    for name in WECHAT_PROCESS_NAMES:
        pid = _first_pid(["pgrep", "-x", name])
        if pid:
            return pid

    for pattern in WECHAT_PROCESS_PATTERNS:
        pid = _first_pid(["pgrep", "-f", pattern])
        if pid:
            return pid

    return None


def is_wechat_running():
    """Check if WeChat is running."""
    return get_wechat_pid() is not None


def get_wechat_app_path():
    """Get WeChat.app path, preferring system-installed location."""
    if sys.platform == "win32":
        candidates = (
            os.path.join(os.environ.get("ProgramFiles", ""), "Tencent", "Weixin", "Weixin.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Tencent", "WeChat", "WeChat.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tencent", "WeChat", "WeChat.exe"),
        )
        return next((path for path in candidates if os.path.isfile(path)), None)
    try:
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (path to application "WeChat")'],
            capture_output=True,
            text=True,
        )
        path = result.stdout.strip()
        if result.returncode == 0 and path and os.path.isdir(path):
            return path.rstrip("/")
    except Exception:
        pass

    if os.path.isdir(DEFAULT_WECHAT_APP):
        return DEFAULT_WECHAT_APP
    return None


def is_required_database(rel_path):
    """Return whether the app reads this database directly."""
    normalized = rel_path.replace("\\", "/")
    return any(pattern.match(normalized) for pattern in REQUIRED_DATABASE_PATTERNS)


def is_wechat_signed():
    """Check if WeChat has been re-signed (hardened runtime removed)."""
    if sys.platform == "win32":
        return True
    app_path = get_wechat_app_path()
    if not app_path:
        return False

    try:
        result2 = subprocess.run(
            ["codesign", "-dvv", app_path],
            capture_output=True, text=True,
        )
        if result2.returncode != 0:
            return False
        flags = result2.stderr
        # Hardened runtime shows "runtime" in flags
        return "runtime" not in flags.lower()
    except Exception:
        return False


def compile_scanner():
    """Compile C key scanner."""
    if sys.platform == "win32":
        return process_lookup_available()
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(C_BINARY):
        # Check if recompilation needed
        if os.path.getmtime(C_BINARY) >= os.path.getmtime(C_SOURCE):
            return True

    try:
        result = subprocess.run(
            ["cc", "-O2", "-o", C_BINARY, C_SOURCE, "-framework", "Foundation"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"编译失败: {result.stderr}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"编译失败: {e}", file=sys.stderr)
        return False


def extract_keys(raw_key_hex=None):
    """Refresh platform database keys.

    macOS runs the original C scanner with administrator authorization.
    Windows requires an explicit raw key and derives verified page keys.

    Returns:
        dict: {db_rel_path: {"enc_key": hex_string}, ...} or None.
    """
    if sys.platform == "win32":
        if raw_key_hex is None:
            return None
        from .windows_key_extractor import extract_keys_from_raw_key

        config = load_config()
        db_dir = config.get("db_dir", "")
        keys_path = config.get("keys_file") or KEYS_FILE
        if not db_dir or not os.path.isdir(db_dir):
            return None
        return extract_keys_from_raw_key(raw_key_hex, db_dir, keys_path)

    if raw_key_hex is not None:
        raise RuntimeError("raw_key_import_is_windows_only")

    if not compile_scanner():
        return None

    pid = get_wechat_pid()
    if not pid:
        return None
    home_dir = os.path.expanduser("~")
    db_dir = load_config().get("db_dir", "")
    scanner_output = ""

    # C scanner outputs all_keys.json to cwd, so cd to DATA_DIR
    try:
        result = subprocess.run(
            ["sudo", "-n", C_BINARY, str(pid), home_dir, db_dir],
            capture_output=True, text=True,
            cwd=DATA_DIR,
            timeout=60,
        )
        scanner_output += "\n".join((result.stdout or "", result.stderr or ""))

        if result.returncode != 0:
            # Try interactive sudo via osascript dialog
            shell_command = (
                f"cd {shlex.quote(DATA_DIR)} && "
                f"{shlex.quote(C_BINARY)} {pid} {shlex.quote(home_dir)} {shlex.quote(db_dir)}"
            )
            result = subprocess.run(
                ["osascript", "-e",
                 f"do shell script {json.dumps(shell_command)} with administrator privileges"],
                capture_output=True, text=True,
                timeout=60,
            )
            scanner_output += "\n".join((result.stdout or "", result.stderr or ""))
            if result.returncode != 0:
                return None

    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

    # Read output keys file
    keys_path = os.path.join(DATA_DIR, "all_keys.json")
    if not os.path.exists(keys_path):
        if db_dir and os.path.isdir(db_dir):
            return _rematch_keys_from_output(db_dir, scanner_output) or None
        return None

    try:
        with open(keys_path) as f:
            keys = json.load(f)
        # Filter out metadata fields
        keys = {k: v for k, v in keys.items() if not k.startswith("_")}
        ensure_private_file(keys_path)
    except (json.JSONDecodeError, OSError):
        return None

    # If C scanner couldn't match DBs due to permission issues, re-match in Python
    # Python runs as current user and can read sandbox files
    if not keys and db_dir and os.path.isdir(db_dir):
        keys = _rematch_keys_from_output(db_dir, scanner_output)

    if not keys:
        try:
            os.remove(keys_path)
        except OSError:
            pass
        return None

    return keys


def extract_keys_from_raw_key(raw_key_hex):
    """Explicitly derive Windows Weixin page keys from a user-supplied raw key."""
    if sys.platform != "win32":
        raise RuntimeError("raw_key_import_is_windows_only")
    return extract_keys(raw_key_hex=raw_key_hex)


def _parse_raw_keys_from_text(text):
    """Parse all key+salt pairs found in scanner stdout/stderr text."""
    raw_keys = []  # [(key_hex, salt_hex), ...]
    for line in str(text or "").splitlines():
        line = line.strip()
        # 格式: "(unknown)  <key_hex 64>  <salt_hex 32>"
        # 或:   "db_name   <key_hex 64>  <salt_hex 32>"
        parts = line.split()
        if len(parts) < 3:
            continue
        key_hex = parts[-2]
        salt_hex = parts[-1]
        if len(key_hex) == 64 and len(salt_hex) == 32:
            try:
                bytes.fromhex(key_hex)
                bytes.fromhex(salt_hex)
                raw_keys.append((key_hex.lower(), salt_hex.lower()))
            except ValueError:
                continue
    return raw_keys


def _parse_raw_keys_from_log(log_path=EXTRACT_LOG):
    """Parse legacy key+salt pairs from extract_keys.log if it exists."""
    raw_keys = []
    if not os.path.exists(log_path):
        return raw_keys
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            raw_keys = _parse_raw_keys_from_text(f.read())
    except OSError:
        pass
    return raw_keys


def _rematch_keys_from_output(db_dir, scanner_output):
    """Read db headers and match against key+salt pairs from scanner output.

    Solves the issue where root cannot read macOS sandbox files.
    """
    raw_keys = _parse_raw_keys_from_text(scanner_output)
    if not raw_keys:
        return {}

    print(f"[key_extractor] 从 scanner 输出解析到 {len(raw_keys)} 个 key+salt 对，用 Python 重新匹配...")

    # Build salt -> key_hex index
    salt_to_key = {}
    for key_hex, salt_hex in raw_keys:
        salt_to_key[salt_hex] = key_hex

    # Walk all .db files under db_dir, read salt
    matched = {}
    for root, _dirs, files in os.walk(db_dir):
        for fname in files:
            if not fname.endswith(".db"):
                continue
            full_path = os.path.join(root, fname)
            rel = os.path.relpath(full_path, db_dir).replace("\\", "/")
            try:
                with open(full_path, "rb") as f:
                    header = f.read(16)
                if len(header) < 16:
                    continue
                # Unencrypted SQLite, skip
                if header[:15] == b"SQLite format 3":
                    continue
                file_salt = header.hex().lower()
                if file_salt in salt_to_key:
                    matched[rel] = {"enc_key": salt_to_key[file_salt]}
                    print(f"  ✓ 匹配: {rel}")
            except OSError:
                continue

    if matched:
        # Save to all_keys.json
        try:
            with open(KEYS_FILE, "w") as f:
                json.dump(matched, f, indent=2)
            ensure_private_file(KEYS_FILE)
            print(f"[key_extractor] Python 重新匹配成功: {len(matched)} 个数据库")
        except OSError:
            pass

    return matched


def _rematch_keys_from_log(db_dir):
    """Legacy helper for manually recovering from an existing extract_keys.log."""
    try:
        with open(EXTRACT_LOG, encoding="utf-8", errors="replace") as f:
            return _rematch_keys_from_output(db_dir, f.read())
    except OSError:
        return {}


def get_cached_keys():
    """Get cached keys (without re-extraction)."""
    if not os.path.exists(KEYS_FILE):
        return None
    try:
        with open(KEYS_FILE) as f:
            keys = json.load(f)
        keys = {k: v for k, v in keys.items() if not k.startswith("_")}
        return keys if keys else None
    except (json.JSONDecodeError, OSError):
        return None


def check_new_databases(db_dir, keys):
    """Detect new encrypted databases under db_dir that are missing keys.

    Scans db_storage directory for all .db files, reads first 16 bytes to
    check if encrypted, compares against existing keys, returns list of
    databases missing keys.

    Args:
        db_dir: db_storage directory path.
        keys: Current key dict {rel_path: {"enc_key": ...}}.

    Returns:
        list[str]: Relative paths of databases missing keys.
    """
    missing = []
    normalized_keys = {k.replace("\\", "/") for k in keys}
    for root, _dirs, files in os.walk(db_dir):
        for fname in files:
            if not fname.endswith(".db"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, db_dir).replace("\\", "/")
            if not is_required_database(rel):
                continue
            if rel in normalized_keys:
                continue  # Already has key
            # Read first 16 bytes to check if encrypted
            try:
                with open(full, "rb") as f:
                    header = f.read(16)
                if len(header) < 16 or header[:15] == b"SQLite format 3":
                    continue  # Too small or unencrypted, skip
                missing.append(rel)
            except OSError:
                continue
    return sorted(missing)
