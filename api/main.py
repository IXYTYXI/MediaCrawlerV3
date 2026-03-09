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
Start command: python -m api.main  (默认端口 9001)
Or: uvicorn api.main:app --host 0.0.0.0 --port 9001 --reload
"""
import asyncio
import hashlib
import hmac
import os
import secrets
import subprocess
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from .routers import crawler_router, data_router, websocket_router, dashboard_router, login_router, control_router, terminal_router, shell_router, feishu_event_router, task_router

app = FastAPI(
    title="MediaCrawler WebUI API",
    description="API for controlling MediaCrawler from WebUI",
    version="1.0.0"
)

# Get webui static files directory
WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")

# CORS configuration - allow all origins for remote access
# 远程部署时需要通过 IP 访问，因此允许所有来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # allow_origins=["*"] 时不能 allow_credentials=True
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== 访问密码保护 ====================
# 修改这里设置你的密码（请改成你自己的复杂密码）
DASHBOARD_PASSWORD = os.environ.get("MC_DASHBOARD_PWD", "changeme")
SHELL_PASSWORD = os.environ.get("MC_SHELL_PWD", "") or DASHBOARD_PASSWORD

_PASSWORD_HASH = hashlib.sha256(DASHBOARD_PASSWORD.encode()).hexdigest()
_SHELL_PASSWORD_HASH = hashlib.sha256(SHELL_PASSWORD.encode()).hexdigest()
_auth_tokens: set = set()
_shell_tokens: set = set()
_shell_challenges: dict = {}  # nonce -> expiry_time

_PUBLIC_PATHS = {"/", "/api/health", "/api/auth/login", "/api/auth/check", "/favicon.ico", "/api/shell/challenge", "/api/shell/auth", "/api/feishu/event", "/api/feishu/card_action"}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "请求格式错误"})

    input_hash = hashlib.sha256(password.encode()).hexdigest()
    if not hmac.compare_digest(input_hash, _PASSWORD_HASH):
        return JSONResponse(status_code=401, content={"success": False, "message": "密码错误"})

    token = secrets.token_hex(32)
    _auth_tokens.add(token)
    return {"success": True, "token": token}


@app.get("/api/auth/check")
async def auth_check(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return {"success": True, "authenticated": token in _auth_tokens}


# ==================== Shell 命令行独立认证（challenge-response，无明文密码） ====================

@app.get("/api/shell/challenge")
async def shell_challenge():
    """获取随机 nonce，用于 challenge-response 认证"""
    import time
    now = time.time()
    expired = [k for k, v in _shell_challenges.items() if v < now]
    for k in expired:
        del _shell_challenges[k]
    nonce = secrets.token_hex(32)
    _shell_challenges[nonce] = now + 300  # 5 分钟有效
    return {"nonce": nonce}


@app.post("/api/shell/auth")
async def shell_auth(request: Request):
    """
    命令行解锁：客户端发送 response = SHA256(nonce + SHA256(password))，
    服务端用存储的 hash 验证，密码永不传至网络。
    """
    import time
    try:
        body = await request.json()
        nonce = body.get("nonce", "")
        response = body.get("response", "")
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "请求格式错误"})

    if not nonce or not response:
        return JSONResponse(status_code=400, content={"success": False, "message": "缺少参数"})

    now = time.time()
    if nonce not in _shell_challenges:
        return JSONResponse(status_code=401, content={"success": False, "message": "挑战已过期，请刷新"})
    if _shell_challenges[nonce] < now:
        del _shell_challenges[nonce]
        return JSONResponse(status_code=401, content={"success": False, "message": "挑战已过期"})
    del _shell_challenges[nonce]

    expected = hashlib.sha256((nonce + _SHELL_PASSWORD_HASH).encode()).hexdigest()
    if not hmac.compare_digest(response, expected):
        return JSONResponse(status_code=401, content={"success": False, "message": "解锁失败"})

    token = secrets.token_hex(32)
    _shell_tokens.add(token)
    return {"success": True, "token": token}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    if path in _PUBLIC_PATHS:
        return await call_next(request)

    if path.startswith("/assets/") or path.endswith((".js", ".css", ".png", ".ico", ".svg", ".woff", ".woff2")):
        return await call_next(request)

    if "upgrade" in request.headers.get("upgrade", "").lower() or path.endswith("/ws"):
        token = request.query_params.get("token", "")
        if path == "/api/shell/ws":
            if token in _shell_tokens:
                return await call_next(request)
        elif token in _auth_tokens:
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "未授权"})

    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token in _auth_tokens:
        return await call_next(request)

    accept = request.headers.get("Accept", "")
    if "text/html" in accept:
        return await call_next(request)

    return JSONResponse(status_code=401, content={"success": False, "message": "请先登录"})
# Register routers
app.include_router(crawler_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(websocket_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(login_router, prefix="/api")
app.include_router(control_router, prefix="/api")
app.include_router(terminal_router, prefix="/api")
app.include_router(shell_router, prefix="/api")
app.include_router(feishu_event_router, prefix="/api")
app.include_router(task_router, prefix="/api")


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}


DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard")


@app.get("/")
async def serve_dashboard_root():
    """用户首页 → 数据仪表盘"""
    index_path = os.path.join(DASHBOARD_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html", headers=_NO_CACHE_HEADERS)
    return {"message": "Dashboard not found"}


@app.get("/dashboard")
async def serve_dashboard():
    """数据仪表盘（保留旧路径兼容）"""
    index_path = os.path.join(DASHBOARD_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html", headers=_NO_CACHE_HEADERS)
    return {"message": "Dashboard not found"}


@app.get("/control")
async def serve_control_page():
    """管理员控制面板（需知道地址才能访问）"""
    control_path = os.path.join(os.path.dirname(__file__), "control.html")
    if os.path.exists(control_path):
        return FileResponse(control_path, media_type="text/html", headers=_NO_CACHE_HEADERS)
    return {"message": "Control panel not found"}


@app.get("/notuse")
async def serve_frontend_hidden():
    """React Dashboard (hidden)"""
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "WebUI not found"}


@app.get("/login")
async def serve_login_page():
    """Return remote login page"""
    login_path = os.path.join(os.path.dirname(__file__), "login.html")
    if os.path.exists(login_path):
        return FileResponse(login_path, media_type="text/html")
    return {"message": "Login page not found"}


@app.get("/terminal")
async def serve_terminal_page():
    """Return web terminal log viewer page"""
    terminal_path = os.path.join(os.path.dirname(__file__), "terminal.html")
    if os.path.exists(terminal_path):
        return FileResponse(terminal_path, media_type="text/html")
    return {"message": "Terminal page not found"}


@app.get("/shell")
async def serve_shell_page():
    """Return shell command line page (password protected)"""
    shell_path = os.path.join(os.path.dirname(__file__), "shell.html")
    if os.path.exists(shell_path):
        return FileResponse(shell_path, media_type="text/html")
    return {"message": "Shell page not found"}


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/env/check")
async def check_environment():
    """Check if MediaCrawler environment is configured correctly"""
    try:
        # Run uv run main.py --help command to check environment
        process = await asyncio.create_subprocess_exec(
            "conda", "run", "--no-capture-output", "-n", "uvenv",
            "python", "-u", "main.py", "--help",
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
    uvicorn.run(app, host="0.0.0.0", port=9001)
