# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/main.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

"""
MediaCrawler WebUI API Server
Start command: uvicorn api.main:app --port 8080 --reload
Or: python -m api.main
"""
import asyncio
import os
import secrets
import subprocess
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from .routers import crawler_router, data_router, websocket_router, login_router

app = FastAPI(
    title="MediaCrawler WebUI API",
    description="API for controlling MediaCrawler from WebUI",
    version="1.0.0"
)

# Get webui static files directory
WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")

# ==================== API Key 认证 ====================
# 从环境变量读取，未设置则自动生成（首次启动时打印到控制台）
API_KEY = os.environ.get("MC_API_KEY", "")
if not API_KEY:
    # 从配置文件读取
    try:
        import json
        _cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "anti_crawl_config.json")
        with open(_cfg_path, "r", encoding="utf-8") as _f:
            _cfg = json.load(_f)
        API_KEY = _cfg.get("api_key", "")
    except Exception:
        pass

if not API_KEY:
    API_KEY = secrets.token_urlsafe(32)
    print("=" * 60)
    print(f"[安全] 未设置 API Key，已自动生成:")
    print(f"  API_KEY = {API_KEY}")
    print(f"  设置方式（二选一）:")
    print(f"    1. 环境变量: export MC_API_KEY='{API_KEY}'")
    print(f"    2. 配置文件: anti_crawl_config.json 中添加 \"api_key\": \"{API_KEY}\"")
    print(f"  访问时在 URL 中加 ?api_key=xxx 或 Header 中加 X-API-Key: xxx")
    print("=" * 60)

# 不需要认证的路径（页面和健康检查）
_PUBLIC_PATHS = {"/", "/login", "/api/health", "/docs", "/openapi.json", "/redoc"}
_PUBLIC_PREFIXES = ("/assets/", "/logos/", "/static/", "/api/ws/")  # 静态资源和 WebSocket


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    """API Key 认证中间件"""
    path = request.url.path

    # 公开路径跳过认证
    if path in _PUBLIC_PATHS:
        return await call_next(request)
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return await call_next(request)

    # 检查 API Key（支持 query param 和 header 两种方式）
    key = request.query_params.get("api_key") or request.headers.get("X-API-Key")
    if key != API_KEY:
        return JSONResponse(
            status_code=401,
            content={"detail": "未授权：缺少或无效的 API Key。请在 URL 中加 ?api_key=xxx 或 Header 中加 X-API-Key"}
        )

    return await call_next(request)


# CORS configuration - allow all origins for remote access
# 远程部署时需要通过 IP 访问，因此允许所有来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # allow_origins=["*"] 时不能 allow_credentials=True
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(crawler_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(websocket_router, prefix="/api")
app.include_router(login_router, prefix="/api")


@app.get("/")
async def serve_frontend():
    """Return frontend page"""
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": "MediaCrawler WebUI API",
        "version": "1.0.0",
        "docs": "/docs",
        "note": "WebUI not found, please build it first: cd webui && npm run build"
    }


@app.get("/login")
async def serve_login_page():
    """Return remote login page"""
    login_path = os.path.join(os.path.dirname(__file__), "login.html")
    if os.path.exists(login_path):
        return FileResponse(login_path, media_type="text/html")
    return {"message": "Login page not found"}


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/env/check")
async def check_environment():
    """Check if MediaCrawler environment is configured correctly"""
    try:
        # Run uv run main.py --help command to check environment
        process = await asyncio.create_subprocess_exec(
            "uv", "run", "main.py", "--help",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="."  # Project root directory
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=30.0  # 30 seconds timeout
        )

        if process.returncode == 0:
            return {
                "success": True,
                "message": "MediaCrawler environment configured correctly",
                "output": stdout.decode("utf-8", errors="ignore")[:500]  # Truncate to first 500 characters
            }
        else:
            error_msg = stderr.decode("utf-8", errors="ignore") or stdout.decode("utf-8", errors="ignore")
            return {
                "success": False,
                "message": "Environment check failed",
                "error": error_msg[:500]
            }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "message": "Environment check timeout",
            "error": "Command execution exceeded 30 seconds"
        }
    except FileNotFoundError:
        return {
            "success": False,
            "message": "uv command not found",
            "error": "Please ensure uv is installed and configured in system PATH"
        }
    except Exception as e:
        return {
            "success": False,
            "message": "Environment check error",
            "error": str(e)
        }


@app.get("/api/config/platforms")
async def get_platforms():
    """Get list of supported platforms"""
    return {
        "platforms": [
            {"value": "xhs", "label": "Xiaohongshu", "icon": "book-open"},
            {"value": "dy", "label": "Douyin", "icon": "music"},
            {"value": "ks", "label": "Kuaishou", "icon": "video"},
            {"value": "bili", "label": "Bilibili", "icon": "tv"},
            {"value": "wb", "label": "Weibo", "icon": "message-circle"},
            {"value": "tieba", "label": "Baidu Tieba", "icon": "messages-square"},
            {"value": "zhihu", "label": "Zhihu", "icon": "help-circle"},
        ]
    }


@app.get("/api/config/options")
async def get_config_options():
    """Get all configuration options"""
    return {
        "login_types": [
            {"value": "qrcode", "label": "QR Code Login"},
            {"value": "cookie", "label": "Cookie Login"},
        ],
        "crawler_types": [
            {"value": "search", "label": "Search Mode"},
            {"value": "detail", "label": "Detail Mode"},
            {"value": "creator", "label": "Creator Mode"},
        ],
        "save_options": [
            {"value": "json", "label": "JSON File"},
            {"value": "csv", "label": "CSV File"},
            {"value": "excel", "label": "Excel File"},
            {"value": "sqlite", "label": "SQLite Database"},
            {"value": "db", "label": "MySQL Database"},
            {"value": "mongodb", "label": "MongoDB Database"},
        ],
    }


# Mount static resources - must be placed after all routes
if os.path.exists(WEBUI_DIR):
    assets_dir = os.path.join(WEBUI_DIR, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    # Mount logos directory
    logos_dir = os.path.join(WEBUI_DIR, "logos")
    if os.path.exists(logos_dir):
        app.mount("/logos", StaticFiles(directory=logos_dir), name="logos")
    # Mount other static files (e.g., vite.svg)
    app.mount("/static", StaticFiles(directory=WEBUI_DIR), name="webui-static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8088)
