#!/bin/bash
# MediaCrawler 单实体版启动脚本
# 固定 Playwright 浏览器路径，避免受 Cursor 沙盒等外部环境变量干扰

export PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

cd /data/vonjan/program/MediaCrawler-stable

# 安装 Playwright 浏览器（如不存在则下载，已存在则跳过）
conda run -n uvenv playwright install chromium

# 启动 API 服务
exec conda run --no-capture-output -n uvenv python -u -m uvicorn api.main:app --host 0.0.0.0 --port 9001
