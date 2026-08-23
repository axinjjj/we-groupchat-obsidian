"""py2app 打包配置（可选；默认仍推荐源码目录 + 启动.command 分发）"""
from setuptools import setup

APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "resources/app_icon.icns",
    "plist": {
        "CFBundleIdentifier": "io.github.indeliblevivi.we-groupchat-obsidian",
        "CFBundleName": "WeGroupchatObsidian",
        "CFBundleDisplayName": "微信总结",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # 不在 Dock 显示图标
    },
    "packages": [
        "rumps",
        "Crypto",
        "zstandard",
        "anthropic",
        "openai",
        "requests",
        "scripts",
        "objc",
        "ai",
        "core",
        "ui",
    ],
    "resources": ["c_src", "使用说明.txt"],
}

setup(
    app=APP,
    name="WeGroupchatObsidian",
    options={"py2app": OPTIONS},
)
