# -*- coding: utf-8 -*-
"""
MediaCrawler Multi-Tenant Gateway
用户管理 + Docker 容器生命周期 + 反向代理
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

import docker
import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger("gateway")

app = FastAPI(title="MediaCrawler Gateway")

# ==================== 配置 ====================

DATA_DIR = Path(os.environ.get("GATEWAY_DATA_DIR", "/data/mc-gateway"))
USERS_FILE = DATA_DIR / "users.json"
MC_IMAGE = os.environ.get("MC_IMAGE", "mediacrawler:latest")
MC_USERS_DIR = Path(os.environ.get("MC_USERS_DIR", "/data/mc-users"))
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "ooioo.us.ci")
MAIL_CONTAINER = os.environ.get("MAIL_CONTAINER", "mailserver")
ADMIN_PASSWORD = os.environ.get("GATEWAY_ADMIN_PWD", "admin123")
PORT_RANGE_START = int(os.environ.get("MC_PORT_START", "9020"))
PORT_RANGE_END = int(os.environ.get("MC_PORT_END", "9040"))
CONTAINER_MEM_LIMIT = os.environ.get("MC_MEM_LIMIT", "2g")
CONTAINER_SHM_SIZE = os.environ.get("MC_SHM_SIZE", "512m")
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")  # e.g. "/crawler"
SHELL_PASSWORD = os.environ.get("MC_SHELL_PWD", "shellAdmin!2026")

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _bp(path: str) -> str:
    """给路径加上 BASE_PATH 前缀"""
    return f"{BASE_PATH}{path}"


# ==================== 用户存储 ====================

ADMIN_HASH_FILE = DATA_DIR / "admin_hash.txt"
_tokens: dict = {}  # token -> {"username": str, "is_admin": bool, "expires": float}


def _get_admin_hash() -> str:
    if ADMIN_HASH_FILE.exists():
        return ADMIN_HASH_FILE.read_text().strip()
    return hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()


def _set_admin_hash(new_hash: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ADMIN_HASH_FILE.write_text(new_hash)


def _load_users() -> dict:
    if USERS_FILE.exists():
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_users(users: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def _hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ==================== Docker 管理 ====================

def _docker_client() -> docker.DockerClient:
    return docker.from_env()


def _container_name(username: str) -> str:
    return f"mediacrawler-{username}"


def _get_container(username: str):
    try:
        client = _docker_client()
        return client.containers.get(_container_name(username))
    except docker.errors.NotFound:
        return None
    except Exception:
        return None


def _allocate_port(users: dict) -> int:
    used_ports = {u.get("port") for u in users.values() if u.get("port")}
    for port in range(PORT_RANGE_START, PORT_RANGE_END):
        if port not in used_ports:
            return port
    raise RuntimeError("No available ports")


def _ensure_user_dirs(username: str):
    base = MC_USERS_DIR / username
    for sub in ["browser_data", "data/cookies", "data/xhs", "config", "logs"]:
        (base / sub).mkdir(parents=True, exist_ok=True)

    config_file = base / "config" / "anti_crawl_config.json"
    if not config_file.exists():
        src = Path("/data/vonjan/program/MediaCrawler-stable/config/anti_crawl_config.json")
        if src.exists():
            import shutil
            shutil.copy2(src, config_file)


def _create_container(username: str, port: int, password: str) -> str:
    _container_tokens.pop(username, None)
    _ensure_user_dirs(username)
    user_dir = MC_USERS_DIR / username
    client = _docker_client()

    existing = _get_container(username)
    if existing:
        existing.remove(force=True)

    container = client.containers.run(
        MC_IMAGE,
        name=_container_name(username),
        ports={"9001/tcp": ("127.0.0.1", port)},
        volumes={
            str(user_dir / "browser_data"): {"bind": "/app/browser_data", "mode": "rw"},
            str(user_dir / "data"): {"bind": "/app/data", "mode": "rw"},
            str(user_dir / "config"): {"bind": "/app/config", "mode": "rw"},
            str(user_dir / "logs"): {"bind": "/app/logs", "mode": "rw"},
        },
        environment={
            "MC_DASHBOARD_PWD": password,
            "MC_SHELL_PWD": SHELL_PASSWORD,
            "PYTHONUNBUFFERED": "1",
        },
        detach=True,
        mem_limit=CONTAINER_MEM_LIMIT,
        shm_size=CONTAINER_SHM_SIZE,
        restart_policy={"Name": "unless-stopped"},
    )
    return container.id


def _create_email(username: str, password: str) -> tuple:
    """在 docker-mailserver 中创建邮箱，返回 (success, message)"""
    email = f"{username}@{MAIL_DOMAIN}"
    try:
        client = _docker_client()
        mail_c = client.containers.get(MAIL_CONTAINER)
        result = mail_c.exec_run(
            f"setup email add {email} {password}",
            demux=True,
        )
        stdout = (result.output[0] or b"").decode().strip()
        stderr = (result.output[1] or b"").decode().strip()
        if result.exit_code == 0:
            return True, email
        return False, stderr or stdout or f"exit_code={result.exit_code}"
    except docker.errors.NotFound:
        return False, f"邮件容器 {MAIL_CONTAINER} 未找到"
    except Exception as e:
        return False, str(e)


def _update_email_password(username: str, new_password: str) -> tuple:
    """修改 docker-mailserver 中的邮箱密码"""
    email = f"{username}@{MAIL_DOMAIN}"
    try:
        client = _docker_client()
        mail_c = client.containers.get(MAIL_CONTAINER)
        result = mail_c.exec_run(
            f"setup email update {email} {new_password}",
            demux=True,
        )
        stdout = (result.output[0] or b"").decode().strip()
        stderr = (result.output[1] or b"").decode().strip()
        if result.exit_code == 0:
            return True, email
        return False, stderr or stdout or f"exit_code={result.exit_code}"
    except docker.errors.NotFound:
        return False, f"邮件容器 {MAIL_CONTAINER} 未找到"
    except Exception as e:
        return False, str(e)


def _delete_email(username: str) -> tuple:
    """从 docker-mailserver 中删除邮箱"""
    email = f"{username}@{MAIL_DOMAIN}"
    try:
        client = _docker_client()
        mail_c = client.containers.get(MAIL_CONTAINER)
        result = mail_c.exec_run(f"setup email del -y {email}", demux=True)
        return result.exit_code == 0, email
    except Exception as e:
        return False, str(e)


def _get_container_status(username: str) -> dict:
    c = _get_container(username)
    if not c:
        return {"status": "not_created"}
    c.reload()
    return {
        "status": c.status,
        "id": c.short_id,
        "started_at": c.attrs.get("State", {}).get("StartedAt", ""),
    }


# ==================== 认证中间件 ====================

def _get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("mc_token") or request.headers.get(
        "Authorization", ""
    ).replace("Bearer ", "")
    if not token or token not in _tokens:
        return None
    info = _tokens[token]
    if time.time() > info["expires"]:
        _tokens.pop(token, None)
        return None
    return info


def _require_admin(request: Request) -> dict:
    user = _get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ==================== API 路由 ====================


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/manage/api/login")
async def login(req: LoginRequest):
    users = _load_users()
    username = req.username.strip().lower()
    password = req.password

    is_admin = False
    if username == "admin":
        if not hmac.compare_digest(
            _hash_pw(password), _get_admin_hash()
        ):
            raise HTTPException(status_code=401, detail="密码错误")
        is_admin = True
    else:
        user = users.get(username)
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")
        if not hmac.compare_digest(
            _hash_pw(password), user.get("password_hash", "")
        ):
            raise HTTPException(status_code=401, detail="密码错误")

    token = secrets.token_hex(32)
    _tokens[token] = {
        "username": username,
        "is_admin": is_admin,
        "expires": time.time() + 86400,
    }
    resp = JSONResponse({"success": True, "username": username, "is_admin": is_admin})
    resp.set_cookie("mc_token", token, httponly=True, max_age=86400, path=_bp("/"))
    return resp


@app.post("/manage/api/logout")
async def logout(request: Request):
    token = request.cookies.get("mc_token", "")
    _tokens.pop(token, None)
    resp = JSONResponse({"success": True})
    resp.delete_cookie("mc_token", path=_bp("/"))
    return resp


@app.get("/manage/api/me")
async def get_me(request: Request):
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    result = {"username": user["username"], "is_admin": user["is_admin"]}
    if not user["is_admin"]:
        users = _load_users()
        u = users.get(user["username"], {})
        result["port"] = u.get("port")
        result["email"] = u.get("email", f"{user['username']}@{MAIL_DOMAIN}")
        result["container"] = _get_container_status(user["username"])
    return {"success": True, "data": result}


# ---- 用户自助 API ----


class UserChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@app.post("/manage/api/user/change-password")
async def user_change_password(req: UserChangePasswordRequest, request: Request):
    cur = _get_current_user(request)
    if not cur:
        raise HTTPException(status_code=401, detail="未登录")
    username = cur["username"]
    if username == "admin":
        raise HTTPException(status_code=400, detail="管理员请使用管理员改密功能")

    users = _load_users()
    user = users.get(username)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not hmac.compare_digest(_hash_pw(req.old_password), user.get("password_hash", "")):
        raise HTTPException(status_code=400, detail="旧密码错误")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少6位")

    messages = []

    users[username]["password_hash"] = _hash_pw(req.new_password)
    users[username]["dashboard_password"] = req.new_password
    _save_users(users)
    messages.append("登录密码已更新")

    email_ok, email_info = _update_email_password(username, req.new_password)
    if email_ok:
        messages.append(f"邮箱密码已更新")
    else:
        messages.append(f"邮箱密码更新失败: {email_info}")

    c = _get_container(username)
    if c:
        try:
            c.reload()
            if c.status == "running":
                c.stop(timeout=5)
            port = user["port"]
            _create_container(username, port, req.new_password)
            messages.append("Dashboard 密码已更新")
        except Exception as e:
            messages.append(f"容器重启失败: {e}")

    return {"success": True, "message": "；".join(messages)}


# ---- 管理员 API ----


class CreateUserRequest(BaseModel):
    username: str
    password: str


@app.get("/manage/api/admin/users")
async def list_users(request: Request):
    _require_admin(request)
    users = _load_users()
    result = []
    for name, info in users.items():
        cs = _get_container_status(name)
        result.append({
            "username": name,
            "port": info.get("port"),
            "email": info.get("email", f"{name}@{MAIL_DOMAIN}"),
            "created_at": info.get("created_at", ""),
            "container": cs,
        })
    return {"success": True, "data": result}


@app.post("/manage/api/admin/users")
async def create_user(req: CreateUserRequest, request: Request):
    _require_admin(request)
    users = _load_users()
    username = req.username.strip().lower()
    if not username or username == "admin":
        raise HTTPException(status_code=400, detail="无效的用户名")
    if username in users:
        raise HTTPException(status_code=409, detail="用户已存在")

    port = _allocate_port(users)
    users[username] = {
        "password_hash": _hash_pw(req.password),
        "port": port,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dashboard_password": req.password,
    }
    _save_users(users)

    messages = []
    container_ok = True
    try:
        _create_container(username, port, req.password)
    except Exception as e:
        container_ok = False
        messages.append(f"容器启动失败: {e}")

    email_ok, email_info = _create_email(username, req.password)
    if email_ok:
        users[username]["email"] = email_info
        _save_users(users)
        messages.append(f"邮箱 {email_info} 已创建")
    else:
        messages.append(f"邮箱创建失败: {email_info}")

    summary = f"用户 {username} 已创建" + ("" if not messages else "。" + "；".join(messages))
    return {"success": True, "message": summary, "port": port, "email": email_info if email_ok else None}


@app.delete("/manage/api/admin/users/{username}")
async def delete_user(username: str, request: Request):
    _require_admin(request)
    users = _load_users()
    if username not in users:
        raise HTTPException(status_code=404, detail="用户不存在")

    c = _get_container(username)
    if c:
        try:
            c.remove(force=True)
        except Exception:
            pass

    _delete_email(username)

    del users[username]
    _save_users(users)
    return {"success": True, "message": f"用户 {username} 及其邮箱已删除"}


class ResetPasswordRequest(BaseModel):
    new_password: str


@app.post("/manage/api/admin/users/{username}/reset-password")
async def reset_user_password(username: str, req: ResetPasswordRequest, request: Request):
    _require_admin(request)
    users = _load_users()
    user = users.get(username)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="密码至少6位")

    messages = []

    users[username]["password_hash"] = _hash_pw(req.new_password)
    users[username]["dashboard_password"] = req.new_password
    _save_users(users)
    messages.append("Gateway 密码已更新")

    email_ok, email_info = _update_email_password(username, req.new_password)
    if email_ok:
        messages.append(f"邮箱 {email_info} 密码已更新")
    else:
        messages.append(f"邮箱密码更新失败: {email_info}")

    c = _get_container(username)
    if c and c.status == "running":
        try:
            c.stop(timeout=5)
            port = user["port"]
            _create_container(username, port, req.new_password)
            messages.append("容器已重启（Dashboard 密码已更新）")
        except Exception as e:
            messages.append(f"容器重启失败: {e}")

    return {"success": True, "message": "；".join(messages)}


@app.post("/manage/api/admin/users/{username}/start")
async def start_user_container(username: str, request: Request):
    _require_admin(request)
    users = _load_users()
    user = users.get(username)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    c = _get_container(username)
    if c:
        c.reload()
        if c.status == "running":
            return {"success": True, "message": "容器已在运行"}
        c.start()
        return {"success": True, "message": "容器已启动"}

    _create_container(username, user["port"], user.get("dashboard_password", "changeme"))
    return {"success": True, "message": "容器已创建并启动"}


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@app.post("/manage/api/admin/change-password")
async def change_admin_password(req: ChangePasswordRequest, request: Request):
    _require_admin(request)
    if not hmac.compare_digest(_hash_pw(req.old_password), _get_admin_hash()):
        raise HTTPException(status_code=400, detail="旧密码错误")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少6位")
    _set_admin_hash(_hash_pw(req.new_password))
    return {"success": True, "message": "管理员密码已修改"}


@app.post("/manage/api/admin/users/{username}/stop")
async def stop_user_container(username: str, request: Request):
    _require_admin(request)
    c = _get_container(username)
    if not c:
        return {"success": True, "message": "容器不存在"}
    c.stop(timeout=10)
    return {"success": True, "message": "容器已停止"}


# ==================== 反向代理 ====================

_proxy_client = httpx.AsyncClient(timeout=300.0, follow_redirects=False)
_container_tokens: dict = {}  # username -> {"token": str, "port": int}


async def _get_container_token(username: str, port: int) -> str:
    """获取或复用容器内的 auth token，实现免密登录"""
    cached = _container_tokens.get(username)
    if cached and cached["port"] == port:
        return cached["token"]

    users = _load_users()
    password = users.get(username, {}).get("dashboard_password", "")
    if not password:
        return ""

    try:
        resp = await _proxy_client.post(
            f"http://127.0.0.1:{port}/api/auth/login",
            json={"password": password},
            timeout=5.0,
        )
        data = resp.json()
        if data.get("success"):
            token = data["token"]
            _container_tokens[username] = {"token": token, "port": port}
            return token
    except Exception:
        pass
    return ""


def _resolve_target(request: Request) -> tuple:
    """从 cookie 中解析当前普通用户的目标容器端口"""
    user = _get_current_user(request)
    if not user:
        return None, None, None

    username = user["username"]
    if username == "admin":
        return None, None, None

    users = _load_users()
    u = users.get(username)
    if not u:
        return None, None, None
    return username, u.get("port"), u.get("dashboard_password", "")


@app.api_route(
    "/workspace/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_to_container(request: Request, path: str):
    """反向代理：/workspace/xxx -> 用户容器（普通用户专用，禁止访问 Shell）"""
    username, port, _ = _resolve_target(request)
    if not port:
        return RedirectResponse(_bp("/manage/login"), status_code=302)

    if path == "shell" or path.startswith("api/shell/"):
        return JSONResponse(
            status_code=403,
            content={"error": "Shell 终端仅管理员可访问"},
        )

    container_token = await _get_container_token(username, port)

    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)
    if container_token:
        headers["Authorization"] = f"Bearer {container_token}"

    body = await request.body()

    try:
        resp = await _proxy_client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )

        if resp.status_code == 401 and container_token:
            _container_tokens.pop(username, None)
            new_token = await _get_container_token(username, port)
            if new_token:
                headers["Authorization"] = f"Bearer {new_token}"
                resp = await _proxy_client.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=body,
                )
    except httpx.ConnectError:
        return JSONResponse(
            status_code=502,
            content={"error": "容器未运行或正在启动中，请稍后重试"},
        )

    excluded = {"transfer-encoding", "content-encoding", "content-length"}
    resp_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in excluded
    }

    content = resp.content
    if container_token and "text/html" in resp.headers.get("content-type", ""):
        html = content.decode("utf-8", errors="replace")
        if "</head>" in html:
            inject = f'<script>localStorage.setItem("mc_token","{container_token}");</script>'
            html = html.replace("</head>", f"{inject}</head>", 1)
            content = html.encode("utf-8")

    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
    )


# ==================== WebSocket 反向代理 ====================


def _get_user_from_cookie(ws_scope: dict) -> Optional[dict]:
    """从 WebSocket scope 的 cookie 中提取用户信息"""
    headers = dict(ws_scope.get("headers", []))
    cookie_header = headers.get(b"cookie", b"").decode()
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("mc_token="):
            token = part[len("mc_token="):]
            if token in _tokens:
                info = _tokens[token]
                if time.time() <= info["expires"]:
                    return info
    return None


async def _bridge_websocket(websocket: WebSocket, port: int, path: str, query_params: str = ""):
    """双向桥接 Gateway WebSocket 与容器内 WebSocket"""
    target_url = f"ws://127.0.0.1:{port}/{path}"
    if query_params:
        target_url += f"?{query_params}"

    await websocket.accept()

    try:
        async with websockets.connect(target_url, ping_interval=20, ping_timeout=60) as container_ws:
            async def client_to_container():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await container_ws.send(data)
                except WebSocketDisconnect:
                    pass

            async def container_to_client():
                try:
                    async for msg in container_ws:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            done, pending = await asyncio.wait(
                [asyncio.create_task(client_to_container()),
                 asyncio.create_task(container_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception as e:
        logger.warning("WebSocket proxy error: %s", e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/workspace/{path:path}")
async def proxy_ws_to_container(websocket: WebSocket, path: str):
    """普通用户 WebSocket 代理（禁止 Shell）"""
    user = _get_user_from_cookie(websocket.scope)
    if not user:
        await websocket.close(code=4001, reason="未登录")
        return

    if user["username"] == "admin" or path.startswith("api/shell/"):
        await websocket.close(code=4003, reason="Shell 终端仅管理员可访问")
        return

    users = _load_users()
    u = users.get(user["username"])
    if not u or not u.get("port"):
        await websocket.close(code=4004, reason="用户或端口不存在")
        return

    qs = str(websocket.query_params) if websocket.query_params else ""
    await _bridge_websocket(websocket, u["port"], path, qs)


# ==================== 管理员代理（路径式路由，自动传播 target） ====================


@app.api_route(
    "/manage/proxy/{target_user}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def admin_proxy_to_container(request: Request, target_user: str, path: str):
    """管理员专用反向代理：/manage/proxy/{user}/xxx -> 用户容器"""
    _require_admin(request)

    users = _load_users()
    u = users.get(target_user)
    if not u or not u.get("port"):
        return JSONResponse(status_code=404, content={"error": f"用户 {target_user} 不存在或端口未分配"})

    port = u["port"]
    container_token = await _get_container_token(target_user, port)

    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)
    if container_token:
        headers["Authorization"] = f"Bearer {container_token}"

    body = await request.body()

    try:
        resp = await _proxy_client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )

        if resp.status_code == 401 and container_token:
            _container_tokens.pop(target_user, None)
            new_token = await _get_container_token(target_user, port)
            if new_token:
                headers["Authorization"] = f"Bearer {new_token}"
                resp = await _proxy_client.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=body,
                )
    except httpx.ConnectError:
        return JSONResponse(
            status_code=502,
            content={"error": "容器未运行或正在启动中，请稍后重试"},
        )

    excluded = {"transfer-encoding", "content-encoding", "content-length"}
    resp_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in excluded
    }

    content = resp.content
    if container_token and "text/html" in resp.headers.get("content-type", ""):
        html = content.decode("utf-8", errors="replace")
        if "</head>" in html:
            inject = f'<script>localStorage.setItem("mc_token","{container_token}");</script>'
            html = html.replace("</head>", f"{inject}</head>", 1)
            content = html.encode("utf-8")

    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
    )


@app.websocket("/manage/proxy/{target_user}/{path:path}")
async def admin_proxy_ws(websocket: WebSocket, target_user: str, path: str):
    """管理员专用 WebSocket 代理"""
    user = _get_user_from_cookie(websocket.scope)
    if not user or not user.get("is_admin"):
        await websocket.close(code=4003, reason="需要管理员权限")
        return

    users = _load_users()
    u = users.get(target_user)
    if not u or not u.get("port"):
        await websocket.close(code=4004, reason=f"用户 {target_user} 不存在")
        return

    qs = str(websocket.query_params) if websocket.query_params else ""
    await _bridge_websocket(websocket, u["port"], path, qs)


# ==================== 页面路由 ====================

GATEWAY_DIR = Path(__file__).parent


def _serve_html(template_name: str) -> HTMLResponse:
    html_path = GATEWAY_DIR / "templates" / template_name
    if not html_path.exists():
        return HTMLResponse(f"<h1>{template_name} not found</h1>", status_code=500)
    html = html_path.read_text(encoding="utf-8")
    inject = f'<script>const BASE="{BASE_PATH}";</script>'
    html = html.replace("</head>", f"{inject}</head>", 1)
    return HTMLResponse(html)


@app.get("/manage/login")
async def login_page():
    return _serve_html("login.html")


@app.get("/manage/admin")
async def admin_page(request: Request):
    user = _get_current_user(request)
    if not user or not user.get("is_admin"):
        return RedirectResponse(_bp("/manage/login"), status_code=302)
    return _serve_html("admin.html")


@app.get("/manage/portal")
async def portal_page(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(_bp("/manage/login"), status_code=302)
    if user["is_admin"]:
        return RedirectResponse(_bp("/manage/admin"), status_code=302)
    return _serve_html("portal.html")


@app.get("/")
async def root(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(_bp("/manage/login"), status_code=302)
    if user["is_admin"]:
        return RedirectResponse(_bp("/manage/admin"), status_code=302)
    return RedirectResponse(_bp("/manage/portal"), status_code=302)


@app.get("/manage/health")
async def health():
    return {"status": "ok"}
