#!/usr/bin/env python3
"""Build a verified source-payload zip with generated control metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PROJECT_SLUG = "we-groupchat-obsidian"
DEFAULT_OUT_DIR = ROOT / "dist" / "share"
MANIFEST_NAME = "share-manifest.json"
MANIFEST_SCHEMA = "we-groupchat-obsidian.share-manifest.v1"
GUIDE_NAME = "群友使用说明.md"

EXCLUDED_PATHS = {
    "docs/working-continuity.md",
    GUIDE_NAME,
}

EXCLUDED_PREFIXES = (
    ".git/",
    ".venv/",
    "__pycache__/",
    "ai/__pycache__/",
    "core/__pycache__/",
    "scripts/__pycache__/",
    "tests/__pycache__/",
    "ui/__pycache__/",
    "build/",
    "dist/",
    "notes/",
    ".superpowers/",
    "docs/superpowers/",
)

EXCLUDED_NAMES = {
    ".DS_Store",
}

DENIED_BASENAMES = {
    ".env",
    "all_keys.json",
    "config.json",
    "resource_backup.json",
}

DENIED_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".log",
    ".pem",
    ".p12",
    ".key",
)

SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        rb"(?i)(?:api[_-]?key|access[_-]?token|password|secret)"
        rb"\s*[:=]\s*['\"][A-Za-z0-9_./+=:-]{20,}['\"]"
    ),
)


SHARE_GUIDE = """# we-groupchat-obsidian 群友使用说明

这是一份给群友看的快速说明。完整隐私边界、MCP 发送规则、Obsidian 工作流和开发说明请看 `README.zh-CN.md`。

## 先确认

- 只支持 macOS。
- 需要 Python 3.10+、已登录的微信桌面版、Xcode Command Line Tools。
- 需要一个 AI provider API Key，或者本地 Ollama。
- 这不是微信/Tencent 官方软件，不是机器人，也不是远程服务。
- 默认数据目录是 `~/.we-groupchat-obsidian/`，API Key 存在 macOS Keychain。
- 云端 AI 会收到你要求总结的聊天文本；想尽量本地化就用 Ollama。

## 第一次运行

1. 解压整个文件夹，不要只拷贝某一个 `.command` 文件。
2. 右键根目录的 `启动.command`，选择“打开”，再在弹窗中确认打开。
3. 脚本需要创建/更新 `.venv` 并安装 dependencies 时会先询问；输入 `y` 才继续，不同意就退出。
4. 菜单栏出现图标后，进入设置，选择 AI provider 并填写 API Key。

macOS 可能在菜单 app 启动后询问一次 WeChat App Data 访问。请确认发起者是本项目 app；
source guard 和资源索引都在这只长驻 app 内运行，不会每 300 秒启动一只新 Python 来重复询问。
历史补链接不读取附件 bytes；附件解析只接受本次 app 会话授权，CLI 则必须在单次 `run` 上显式传 `--resolve-files`。

这是 source-only CLI 分发，不包含 `.dmg` 或 bundled Python runtime。

如果微信更新后需要重新授权，普通启动不会偷偷重签名。确认要继续时再运行：

```bash
./启动.command --allow-wechat-resign
```

这一步可能会退出微信，并要求输入 Mac 登录密码；终端输入密码时不显示字符是正常的。

## 常用入口

```bash
./启动.command
./launchers/配置关注推送.command
./launchers/健康检查.command
./launchers/刷新数据源.command
./launchers/历史总结到Obsidian.command
./launchers/整理Obsidian输出.command
./launchers/安装自动启动.command
./launchers/卸载自动启动.command
./launchers/补跑遗漏笔记.command
```

## 建议先跑一次健康检查

```bash
./launchers/健康检查.command
```

默认输出是 redacted 的，适合排查 DB/key、LaunchAgent、通知 identity 和 Obsidian 输出状态。只有本机私下 debug 才考虑加 `--sensitive`。

## 不要上传或转发这些东西

- `.venv/`
- `.git/`
- `~/.we-groupchat-obsidian/`
- `~/.wechat-summary/`
- `all_keys.json`
- `*.db`
- `*.log`
- API Key、截图里的 token、真实聊天导出、Obsidian 私人 vault 内容

这个 zip 只包含生成时冻结的 `share-manifest.json` allowlist。无 `.git` 的解压目录再次打包时也只会复制该 manifest 中逐项校验过的文件。
"""


class SharePackageError(RuntimeError):
    """Fail-closed source package boundary failure."""


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def should_exclude(path: str) -> bool:
    if path in EXCLUDED_PATHS:
        return True
    if Path(path).name in EXCLUDED_NAMES:
        return True
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in EXCLUDED_PREFIXES
    )


def _validate_path(path: str) -> str:
    value = str(path or "")
    pure = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise SharePackageError("unsafe_manifest_path")
    return value


def _deny_sensitive_path(path: str) -> None:
    name = PurePosixPath(path).name
    lowered = name.casefold()
    if lowered in DENIED_BASENAMES or any(
        lowered.endswith(suffix) for suffix in DENIED_SUFFIXES
    ):
        raise SharePackageError("sensitive_path_rejected")


def _scan_content(path: str, data: bytes) -> None:
    _deny_sensitive_path(path)
    for pattern in SECRET_PATTERNS:
        if pattern.search(data):
            raise SharePackageError("secret_scan_rejected")


def _run_git(args: list[str]) -> bytes:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SharePackageError("git_tree_unavailable") from exc


def git_entries() -> tuple[str, list[dict]]:
    commit = _run_git(["rev-parse", "HEAD"]).decode("ascii").strip()
    raw = _run_git(["ls-tree", "-r", "-z", "--full-tree", commit])
    entries = []
    path_keys = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
        path = _validate_path(raw_path.decode("utf-8"))
        if should_exclude(path):
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise SharePackageError("tracked_symlink_or_nonregular_rejected")
        path_key = unicodedata.normalize("NFC", path).casefold()
        if path_key in path_keys:
            raise SharePackageError("source_path_collision")
        path_keys.add(path_key)
        data = _run_git(["cat-file", "blob", object_id])
        _scan_content(path, data)
        entries.append({
            "path": path,
            "mode": mode,
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": data,
        })
    return commit, sorted(entries, key=lambda item: item["path"])


def git_files() -> list[str]:
    try:
        _commit, entries = git_entries()
    except SharePackageError:
        return []
    return [entry["path"] for entry in entries]


def _manifest_payload(commit: str, entries: list[dict]) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "source_commit": commit,
        "files": [
            {
                "path": entry["path"],
                "mode": entry["mode"],
                "sha256": entry["sha256"],
            }
            for entry in entries
        ],
    }


def manifest_entries() -> tuple[str, list[dict]]:
    manifest_path = ROOT / MANIFEST_NAME
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SharePackageError("share_manifest_missing")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except SharePackageError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise SharePackageError("share_manifest_invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != MANIFEST_SCHEMA:
        raise SharePackageError("share_manifest_invalid")
    raw_entries = payload.get("files")
    if not isinstance(raw_entries, list):
        raise SharePackageError("share_manifest_invalid")
    entries = []
    seen = set()
    path_keys = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise SharePackageError("share_manifest_invalid")
        path = _validate_path(raw.get("path"))
        if path in seen or should_exclude(path):
            raise SharePackageError("share_manifest_invalid")
        seen.add(path)
        path_key = unicodedata.normalize("NFC", path).casefold()
        if path_key in path_keys:
            raise SharePackageError("share_manifest_invalid")
        path_keys.add(path_key)
        mode = str(raw.get("mode") or "")
        digest = str(raw.get("sha256") or "")
        if mode not in {"100644", "100755"} or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            raise SharePackageError("share_manifest_invalid")
        source = ROOT / path
        try:
            current = ROOT
            for index, part in enumerate(PurePosixPath(path).parts):
                current = current / part
                source_mode = os.lstat(current).st_mode
                if stat.S_ISLNK(source_mode):
                    raise SharePackageError("manifest_member_not_regular")
                if index < len(PurePosixPath(path).parts) - 1:
                    if not stat.S_ISDIR(source_mode):
                        raise SharePackageError("manifest_member_not_regular")
                elif not stat.S_ISREG(source_mode):
                    raise SharePackageError("manifest_member_not_regular")
            executable = bool(source_mode & stat.S_IXUSR)
            if executable != (mode == "100755"):
                raise SharePackageError("manifest_member_mode_mismatch")
            data = source.read_bytes()
        except SharePackageError:
            raise
        except OSError as exc:
            raise SharePackageError("manifest_member_unavailable") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise SharePackageError("manifest_member_hash_mismatch")
        _scan_content(path, data)
        entries.append({"path": path, "mode": mode, "sha256": digest, "data": data})
    return str(payload.get("source_commit") or "manifest-only"), sorted(
        entries, key=lambda item: item["path"]
    )


def source_entries() -> tuple[str, list[dict]]:
    if (ROOT / ".git").exists():
        return git_entries()
    return manifest_entries()


def source_files() -> list[str]:
    _commit, entries = source_entries()
    return [entry["path"] for entry in entries]


def copy_sources(entries: list[dict], package_dir: Path) -> None:
    for entry in entries:
        destination = package_dir / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry["data"])
        destination.chmod(0o755 if entry["mode"] == "100755" else 0o644)


def write_share_controls(package_dir: Path, commit: str, entries: list[dict]) -> None:
    (package_dir / GUIDE_NAME).write_text(SHARE_GUIDE, encoding="utf-8")
    (package_dir / MANIFEST_NAME).write_text(
        json.dumps(
            _manifest_payload(commit, entries),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def validate_package_tree(package_dir: Path, entries: list[dict]) -> None:
    expected = {entry["path"] for entry in entries} | {GUIDE_NAME, MANIFEST_NAME}
    observed = set()
    for path in package_dir.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(package_dir).as_posix()
        if path.is_symlink() or not path.is_file():
            raise SharePackageError("package_nonregular_member")
        observed.add(relative)
    if observed != expected:
        raise SharePackageError("package_member_set_mismatch")
    manifest = json.loads((package_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        data = (package_dir / entry["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise SharePackageError("package_member_hash_mismatch")


def _write_zip(zip_path: Path, package_name: str, package_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in package_dir.rglob("*") if item.is_file()):
            relative = path.relative_to(package_dir).as_posix()
            info = zipfile.ZipInfo(f"{package_name}/{relative}")
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = stat.S_IMODE(os.stat(path).st_mode)
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, path.read_bytes())
    expected = {
        f"{package_name}/{path.relative_to(package_dir).as_posix()}"
        for path in package_dir.rglob("*")
        if path.is_file()
    }
    with zipfile.ZipFile(zip_path) as archive:
        if set(archive.namelist()) != expected:
            raise SharePackageError("zip_member_set_mismatch")


def build(out_dir: Path, package_name: str) -> Path:
    if (
        not package_name
        or package_name in {".", ".."}
        or PurePosixPath(package_name).name != package_name
        or "\\" in package_name
    ):
        raise SharePackageError("unsafe_package_name")
    out_dir.mkdir(parents=True, exist_ok=True)
    package_dir = out_dir / package_name
    zip_path = out_dir / f"{package_name}.zip"

    if package_dir.exists() or zip_path.exists():
        raise SharePackageError("output_exists")

    commit, entries = source_entries()
    with tempfile.TemporaryDirectory(prefix=".share-build-", dir=out_dir) as temp:
        temp_root = Path(temp)
        temp_package = temp_root / package_name
        temp_zip = temp_root / f"{package_name}.zip"
        temp_package.mkdir(parents=True)
        copy_sources(entries, temp_package)
        write_share_controls(temp_package, commit, entries)
        validate_package_tree(temp_package, entries)
        _write_zip(temp_zip, package_name, temp_package)
        os.replace(temp_package, package_dir)
        os.replace(temp_zip, zip_path)
    print(f"wrote {zip_path}")
    print(
        f"staged {len(entries)} verified payload files + 2 generated control files "
        f"in {package_dir}"
    )
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="Output directory for staging folder and zip.",
    )
    parser.add_argument(
        "--name",
        default=f"{PROJECT_SLUG}-share-{date.today().isoformat()}",
        help="Package folder/zip name.",
    )
    args = parser.parse_args()
    try:
        build(Path(args.out_dir), args.name)
    except SharePackageError as exc:
        print(f"refusing package: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
