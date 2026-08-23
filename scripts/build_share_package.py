#!/usr/bin/env python3
"""Build a sanitized source zip for sharing we-groupchat-obsidian."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_SLUG = "we-groupchat-obsidian"
DEFAULT_OUT_DIR = ROOT / "dist" / "share"

EXCLUDED_PATHS = {
    "docs/working-continuity.md",
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

这个 zip 已经排除了本机 runtime、cache/build 产物和内部 continuity docs。你自己运行后再二次转发时，也请重新打 sanitized 包。
"""


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def should_exclude(path: str) -> bool:
    if path in EXCLUDED_PATHS:
        return True
    if Path(path).name in EXCLUDED_NAMES:
        return True
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def git_files() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [item for item in result.stdout.decode("utf-8").split("\0") if item]


def fallback_files() -> list[str]:
    paths: list[str] = []
    for root, dirs, files in os.walk(ROOT):
        root_path = Path(root)
        dirs[:] = [
            name
            for name in dirs
            if not should_exclude(rel(root_path / name) + "/")
        ]
        for name in files:
            path = rel(root_path / name)
            if not should_exclude(path):
                paths.append(path)
    return sorted(paths)


def source_files() -> list[str]:
    files = git_files() or fallback_files()
    filtered = [path for path in files if not should_exclude(path)]
    self_path = rel(Path(__file__).resolve())
    if self_path not in filtered and not should_exclude(self_path):
        filtered.append(self_path)
    return sorted(filtered)


def copy_sources(paths: list[str], package_dir: Path) -> None:
    for path in paths:
        src = ROOT / path
        dest = package_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def write_share_guide(package_dir: Path) -> None:
    guide_path = package_dir / "群友使用说明.md"
    guide_path.write_text(SHARE_GUIDE, encoding="utf-8")


def validate_package_tree(package_dir: Path) -> list[str]:
    problems: list[str] = []
    for path in package_dir.rglob("*"):
        rel_path = path.relative_to(package_dir).as_posix()
        if should_exclude(rel_path):
            problems.append(rel_path)
    return problems


def build(out_dir: Path, package_name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    package_dir = out_dir / package_name
    zip_path = out_dir / f"{package_name}.zip"

    if package_dir.exists():
        shutil.rmtree(package_dir)
    if zip_path.exists():
        zip_path.unlink()

    package_dir.mkdir(parents=True)
    paths = source_files()
    copy_sources(paths, package_dir)
    write_share_guide(package_dir)

    problems = validate_package_tree(package_dir)
    if problems:
        for problem in problems:
            print(f"refusing package: excluded path present: {problem}", file=sys.stderr)
        raise SystemExit(2)

    archive_base = zip_path.with_suffix("")
    shutil.make_archive(str(archive_base), "zip", root_dir=out_dir, base_dir=package_name)
    print(f"wrote {zip_path}")
    print(f"staged {len(paths) + 1} files in {package_dir}")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory for staging folder and zip.")
    parser.add_argument("--name", default=f"{PROJECT_SLUG}-share-{date.today().isoformat()}", help="Package folder/zip name.")
    args = parser.parse_args()
    build(Path(args.out_dir), args.name)


if __name__ == "__main__":
    main()
