#!/bin/bash

version_at_least() {
    local actual_major="$1"
    local actual_minor="$2"
    local minimum_major="$3"
    local minimum_minor="$4"
    [[ "$actual_major" -gt "$minimum_major" ]] || {
        [[ "$actual_major" -eq "$minimum_major" && "$actual_minor" -ge "$minimum_minor" ]]
    }
}

confirm_homebrew_python_install() {
    local answer=""
    read -r -p "运行 brew install python@3.12？[y/N] " answer || return 1
    [[ "$answer" == "y" || "$answer" == "Y" ]]
}

confirm_dependency_install() {
    local answer=""
    read -r -p "创建/更新 .venv 并安装 Python dependencies？[y/N] " answer || return 1
    [[ "$answer" == "y" || "$answer" == "Y" ]]
}
