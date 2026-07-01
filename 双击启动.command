#!/bin/bash
# 定位到脚本所在目录
cd "$(dirname "$0")"

echo "========================================="
echo " 🎙️ 正在启动会议录音转文字工具..."
echo " 使用环境: /opt/homebrew/bin/python3 (3.14)"
echo "========================================="

/opt/homebrew/bin/python3 Voice-Text.py
