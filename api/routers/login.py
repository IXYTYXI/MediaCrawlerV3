# -*- coding: utf-8 -*-
"""
远程扫码登录 API
通过 Web 界面在无图形界面的 Linux 服务器上完成小红书扫码登录

流程: 启动浏览器 → 打开登录页 → 截图二维码 → 用户扫码 → 检测成功 → 保存Cookie
"""
import asyncio
import base64
import json
import os
import time
from typing import Optional, Dict, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(prefix="/login", tags=["login"])

# ==================== 全局登录会话状态 ====================

_login_state: Dict[str, Any] = {
    "status": "idle",          # idle / starting / waiting_scan / success / failed / cancelled
    "qrcode_base64": "",       # 二维码 base64 图片
    "screenshot_base64": "",   # 整页截图 base64
    "message": "",
    "started_at": 0,
    "platform": "",
}

_browser_context = None
_context_page = None
_playwright_instance = None
_browser = None
_login_task: Optional[asyncio.Task] = None

# 防止并发操作的锁
_login_lock = asyncio.Lock()
_cleanup_lock = asyncio.Lock()


def _reset_state():
    global _login_state
    _login_state = {
        "status": "idle",
        "qrcode_base64": "",
        "screenshot_base64": "",
        "message": "",
        "started_at": 0,
        "platform": "",
    }


# ==================== API 端点 ====================

@router.post("/start")
async def start_login(platform: str = "xhs"):
    """
    启动远程扫码登录
    1. 启动 headless 浏览器
    2. 打开小红书登录页
    3. 提取二维码图片
    """
    global _login_task

    async with _login_lock:
        if _login_state["status"] in ("starting", "waiting_scan"):
            return {"success": False, "message": "登录流程已在运行中"}

        _reset_state()
        _login_state["status"] = "starting"
        _login_state["platform"] = platform
        _login_state["started_at"] = time.time()
        _login_state["message"] = "正在启动浏览器..."

        # 异步启动登录流程
        _login_task = asyncio.create_task(_run_login_flow(platform))

    return {"success": True, "message": "登录流程已启动"}


@router.get("/qrcode")
async def get_qrcode():
    """获取当前二维码图片和状态"""
    return {
        "status": _login_state["status"],
        "qrcode": _login_state["qrcode_base64"],
        "screenshot": _login_state["screenshot_base64"],
        "message": _login_state["message"],
        "elapsed": int(time.time() - _login_state["started_at"]) if _login_state["started_at"] else 0,
    }


@router.get("/status")
async def get_login_status():
    """获取登录状态"""
    return {
        "status": _login_state["status"],
        "message": _login_state["message"],
        "platform": _login_state["platform"],
    }


@router.post("/cancel")
async def cancel_login():
    """取消登录"""
    global _login_task

    async with _login_lock:
        _login_state["status"] = "cancelled"
        _login_state["message"] = "已取消"

        if _login_task and not _login_task.done():
            _login_task.cancel()
            # 等待 task 结束（它的 finally 会调 _cleanup_browser）
            try:
                await asyncio.wait_for(_login_task, timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    # task 结束后再兜底清理（幂等安全）
    await _cleanup_browser()
    return {"success": True, "message": "登录已取消"}


@router.post("/refresh")
async def refresh_qrcode():
    """刷新二维码（重新截图当前页面）"""
    page = _context_page  # 捕获引用，避免竞态
    if page and _login_state["status"] == "waiting_scan":
        try:
            if page.is_closed():
                return {"success": False, "message": "页面已关闭"}
            await _capture_qrcode(page)
            return {"success": True, "message": "二维码已刷新"}
        except Exception as e:
            return {"success": False, "message": f"刷新失败: {e}"}
    return {"success": False, "message": "没有活跃的登录会话"}


# ==================== 登录流程核心 ====================

async def _run_login_flow(platform: str):
    """完整的登录流程"""
    global _browser_context, _context_page, _playwright_instance, _browser

    try:
        from playwright.async_api import async_playwright
        import config

        _login_state["message"] = "正在启动浏览器..."

        # 1. 启动 Playwright
        _playwright_instance = await async_playwright().start()
        chromium = _playwright_instance.chromium

        # 2. 使用持久化上下文（保存登录状态）
        user_data_dir = os.path.join(
            os.getcwd(), "browser_data",
            config.USER_DATA_DIR % platform
        )
        os.makedirs(user_data_dir, exist_ok=True)

        _login_state["message"] = "正在打开浏览器..."

        _browser_context = await chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            accept_downloads=True,
            headless=True,
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )

        # 3. 打开小红书
        _context_page = await _browser_context.new_page()

        # 添加反检测脚本
        stealth_js_path = os.path.join(os.getcwd(), "libs", "stealth.min.js")
        if os.path.exists(stealth_js_path):
            await _context_page.add_init_script(path=stealth_js_path)

        _login_state["message"] = "正在打开小红书..."
        await _context_page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # 4. 检查是否已登录（严格模式：只信 UI 元素，不信残留 cookie）
        _login_state["message"] = "检查登录状态..."
        if await _check_already_logged_in(_context_page, _browser_context, is_initial_check=True):
            _login_state["status"] = "success"
            _login_state["message"] = "已登录（Cookie有效）"
            await _take_screenshot(_context_page)
            await _save_cookies_to_file(_browser_context, platform)
            await asyncio.sleep(2)
            return

        # 5. 记录扫码前的 web_session，后续用于检测变化
        global _pre_scan_web_session
        try:
            from tools import utils as _utils
            _pre_cookies = await _browser_context.cookies()
            _, _pre_dict = _utils.convert_cookies(_pre_cookies)
            _pre_scan_web_session = _pre_dict.get("web_session", "")
            print(f"[Login] 扫码前 web_session: {_pre_scan_web_session[:16] if _pre_scan_web_session else '无'}...")
        except Exception:
            _pre_scan_web_session = ""

        # 6. 尝试触发登录弹窗
        _login_state["message"] = "等待登录页面..."
        await _trigger_login_dialog(_context_page)
        await asyncio.sleep(2)

        # 7. 提取二维码
        _login_state["message"] = "提取二维码..."
        await _capture_qrcode(_context_page)

        if not _login_state["qrcode_base64"] and not _login_state["screenshot_base64"]:
            await _take_screenshot(_context_page)

        _login_state["status"] = "waiting_scan"
        _login_state["message"] = "请用小红书APP扫描二维码（60秒内有效）"

        # 7. 等待登录成功
        # 小红书二维码约 60 秒过期，总共等 5 分钟（期间可多次自动刷新二维码）
        QR_LIFETIME = 55       # 二维码生命周期（留 5s 余量，实际约 60s 过期）
        TOTAL_TIMEOUT = 300    # 总超时
        qr_refresh_count = 0
        last_log_time = 0
        qr_born_time = time.time()  # 当前二维码生成时间

        for i in range(TOTAL_TIMEOUT):
            if _login_state["status"] == "cancelled":
                return

            page = _context_page
            ctx = _browser_context
            if not page or page.is_closed() or not ctx:
                return

            now = time.time()
            qr_age = now - qr_born_time           # 当前二维码已存活秒数
            qr_remaining = max(0, QR_LIFETIME - int(qr_age))  # 二维码剩余秒数

            # ── 每 2 秒刷新截图 + 检测页面状态 ──
            if i % 2 == 0:
                await _take_screenshot(page)
                await _capture_qrcode(page)

                # 主动检测二维码过期（页面文字 or 超时）
                page_expired = await _check_qrcode_expired(page)
                time_expired = qr_age >= QR_LIFETIME

                if page_expired or time_expired:
                    qr_refresh_count += 1
                    reason = "页面显示已过期" if page_expired else f"已超过{QR_LIFETIME}s"
                    _login_state["message"] = f"二维码已过期（{reason}），正在自动刷新...（第{qr_refresh_count}次）"
                    print(f"[Login] 二维码过期（{reason}），自动刷新（第{qr_refresh_count}次）")

                    await _auto_refresh_qrcode(page)
                    qr_born_time = time.time()  # 重置二维码计时

                    await asyncio.sleep(2)
                    await _take_screenshot(page)
                    await _capture_qrcode(page)
                    if _login_state["qrcode_base64"]:
                        _login_state["message"] = f"二维码已刷新（第{qr_refresh_count}次），请在60秒内扫码"
                    else:
                        _login_state["message"] = "已刷新，请查看截图扫码"
                    continue

                # 检测页面状态变化（验证码等）
                try:
                    page_content = await page.content()
                    if "请通过验证" in page_content or "滑动" in page_content:
                        _login_state["message"] = "需要滑块验证，请查看页面截图"
                    elif _login_state["qrcode_base64"]:
                        if qr_remaining <= 15:
                            _login_state["message"] = f"二维码即将过期（{qr_remaining}s），请尽快扫码"
                        else:
                            _login_state["message"] = f"请用小红书APP扫码（{qr_remaining}s后自动刷新）"
                    else:
                        _login_state["message"] = "等待中...请查看页面截图"
                except Exception:
                    pass

            # ── 每秒检查登录状态 ──
            if await _check_already_logged_in(page, ctx):
                _login_state["status"] = "success"
                _login_state["message"] = "登录成功！Cookie已保存"
                await _take_screenshot(page)
                print("[Login] ★ 登录成功！正在保存 Cookie...")

                await _save_cookies_to_file(ctx, platform)

                await asyncio.sleep(3)
                return

            # 每 30s 输出一次控制台日志
            if now - last_log_time > 30:
                remaining = TOTAL_TIMEOUT - i
                print(f"[Login] 等待扫码中... 已等待 {i}s，总剩余 {remaining}s，"
                      f"二维码剩余 {qr_remaining}s（已刷新{qr_refresh_count}次）")
                last_log_time = now

            await asyncio.sleep(1)

        # 超时
        _login_state["status"] = "failed"
        _login_state["message"] = "登录超时（5分钟），请点击「重新登录」再试"
        print("[Login] 登录超时（5分钟）")

    except asyncio.CancelledError:
        _login_state["status"] = "cancelled"
        _login_state["message"] = "登录已取消"
    except Exception as e:
        _login_state["status"] = "failed"
        _login_state["message"] = f"登录异常: {str(e)}"
    finally:
        await _cleanup_browser()


async def _trigger_login_dialog(page):
    """尝试触发登录弹窗"""
    try:
        # 尝试点击登录按钮
        login_btn = page.locator("xpath=//*[@id='app']/div[1]/div[2]/div[1]/ul/div[1]/button")
        if await login_btn.count() > 0:
            await login_btn.click()
            await asyncio.sleep(2)
            return

        # 尝试其他登录入口
        login_text = page.locator("text=登录")
        if await login_text.count() > 0:
            await login_text.first.click()
            await asyncio.sleep(2)
    except Exception:
        pass


async def _auto_refresh_qrcode(page):
    """自动刷新过期的二维码"""
    try:
        # 方法1: 点击页面上的刷新/重新获取按钮
        refresh_selectors = [
            "xpath=//*[contains(text(),'点击刷新')]",
            "xpath=//*[contains(text(),'重新获取')]",
            "xpath=//*[contains(text(),'刷新二维码')]",
            "xpath=//div[contains(@class,'qrcode-expired')]",
            "xpath=//div[contains(@class,'qrcode')]//div[contains(@class,'refresh')]",
        ]
        for sel in refresh_selectors:
            elem = await page.query_selector(sel)
            if elem:
                await elem.click()
                print(f"[Login] 点击了刷新按钮: {sel}")
                return

        # 方法2: 点击二维码图片本身（有些实现是点击二维码刷新）
        qr_elem = await page.query_selector("xpath=//img[@class='qrcode-img']")
        if qr_elem:
            await qr_elem.click()
            await asyncio.sleep(1)
            # 检查是否真的刷新了
            new_qr = await page.query_selector("xpath=//img[@class='qrcode-img']")
            if new_qr:
                print("[Login] 点击二维码尝试刷新")
                return

        # 方法3: 重新加载页面并触发登录弹窗
        print("[Login] 无法找到刷新按钮，重新加载页面")
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await _trigger_login_dialog(page)

    except Exception as e:
        print(f"[Login] 自动刷新二维码失败: {e}")


async def _capture_qrcode(page):
    """提取二维码图片（对比去重，避免闪烁）"""
    try:
        if page.is_closed():
            return

        new_qr = ""

        # 方法1: 直接获取二维码图片元素
        qr_selector = "xpath=//img[@class='qrcode-img']"
        qr_elem = await page.query_selector(qr_selector)
        if qr_elem:
            src = await qr_elem.get_attribute("src")
            if src:
                if src.startswith("data:image"):
                    new_qr = src.split(",", 1)[-1] if "," in src else src
                elif src.startswith("http"):
                    import httpx
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        resp = await client.get(src)
                        if resp.status_code == 200:
                            new_qr = base64.b64encode(resp.content).decode()
                else:
                    new_qr = src

        if new_qr:
            # 只在内容真正变化时才更新，避免前端 img 重复渲染闪烁
            if new_qr != _login_state.get("qrcode_base64", ""):
                _login_state["qrcode_base64"] = new_qr
                print("[Login] 二维码已更新")
            return

        # 方法2: Canvas 渲染的二维码
        canvas = await page.query_selector("canvas")
        if canvas:
            screenshot = await canvas.screenshot()
            _login_state["qrcode_base64"] = base64.b64encode(screenshot).decode()
            return

    except Exception:
        pass

    # 方法3: 整页截图兜底
    await _take_screenshot(page)


async def _take_screenshot(page):
    """截取当前页面"""
    try:
        if page.is_closed():
            return
        screenshot = await page.screenshot(type="png")
        _login_state["screenshot_base64"] = base64.b64encode(screenshot).decode()
    except Exception:
        pass


async def _save_cookies_to_file(browser_context, platform: str):
    """将浏览器中的 cookie 保存到共享文件，供爬虫和保活脚本读取"""
    try:
        import json
        from tools import utils

        cookies = await browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(cookies)
        web_session = cookie_dict.get("web_session", "")

        if not web_session:
            return

        # 保存到 data/cookies/{platform}_cookies.json
        cookie_dir = os.path.join(os.getcwd(), "data", "cookies")
        os.makedirs(cookie_dir, exist_ok=True)
        cookie_file = os.path.join(cookie_dir, f"{platform}_cookies.json")

        cookie_data = {
            "web_session": web_session,
            "cookie_str": "; ".join(f"{k}={v}" for k, v in cookie_dict.items()),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "qr_login",
        }
        with open(cookie_file, "w", encoding="utf-8") as f:
            json.dump(cookie_data, f, ensure_ascii=False, indent=2)

        print(f"[Login] ✅ Cookie 已同步到 {cookie_file} (web_session={web_session[:16]}...)")
    except Exception as e:
        print(f"[Login] ⚠️ 保存 cookie 文件失败: {e}")


# 用于记录扫码前的 web_session，检测变化
_pre_scan_web_session: str = ""


async def _check_already_logged_in(page, browser_context, *, is_initial_check: bool = False) -> bool:
    """
    检查是否已登录。

    is_initial_check=True  → 页面刚打开，通过浏览器内 JS 实际调 API 验证
    is_initial_check=False → 扫码轮询中，检测 cookie 变化 + 二维码消失
    """
    global _pre_scan_web_session
    try:
        if page.is_closed():
            return False

        from tools import utils

        cookies = await browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(cookies)
        web_session = cookie_dict.get("web_session", "")

        if is_initial_check:
            # ── 初次检查：在浏览器内发真实 API 请求验证 session ──
            if not web_session:
                print("[Login] 初次检查: 无 web_session cookie，需要登录")
                return False

            print(f"[Login] 初次检查: 发现残留 web_session={web_session[:12]}...，验证有效性...")
            try:
                # 在浏览器上下文中用 JS fetch 调小红书 API，自带完整 cookie
                result = await page.evaluate("""
                    async () => {
                        try {
                            const resp = await fetch('https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo', {
                                credentials: 'include',
                                headers: { 'Content-Type': 'application/json' }
                            });
                            if (!resp.ok) return { valid: false, status: resp.status };
                            const data = await resp.json();
                            return { valid: data.success === true, nickname: data.data?.nickname || '' };
                        } catch(e) {
                            return { valid: false, error: e.message };
                        }
                    }
                """)
                if result.get("valid"):
                    nickname = result.get("nickname", "?")
                    print(f"[Login] 初次检查: ✅ Session 有效！用户: {nickname}")
                    return True
                else:
                    print(f"[Login] 初次检查: ❌ Session 无效 ({result})，需要重新登录")
                    return False
            except Exception as e:
                print(f"[Login] 初次检查: API验证异常 ({e})，视为未登录")
                return False

        else:
            # ── 轮询阶段：检测 cookie 变化 + 二维码弹窗消失 ──
            if web_session and _pre_scan_web_session:
                if web_session != _pre_scan_web_session:
                    # web_session 发生了变化 → 新登录
                    login_dialog = await page.query_selector("xpath=//img[@class='qrcode-img']")
                    if not login_dialog:
                        print(f"[Login] 轮询检测: cookie 变化 ({_pre_scan_web_session[:8]}→{web_session[:8]})，登录成功")
                        return True

            # 二维码弹窗消失 + 页面有 web_session（无论是否变化）
            if web_session:
                login_dialog = await page.query_selector("xpath=//img[@class='qrcode-img']")
                # 同时检查登录弹窗的父容器也不存在
                login_overlay = await page.query_selector("xpath=//div[contains(@class,'login-container')]")
                if not login_dialog and not login_overlay:
                    # 再用 JS 快速验证一下
                    try:
                        result = await page.evaluate("""
                            async () => {
                                try {
                                    const resp = await fetch('https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo', {
                                        credentials: 'include',
                                        headers: { 'Content-Type': 'application/json' }
                                    });
                                    if (!resp.ok) return false;
                                    const data = await resp.json();
                                    return data.success === true;
                                } catch(e) { return false; }
                            }
                        """)
                        if result:
                            print(f"[Login] 轮询检测: API 验证通过，登录成功")
                            return True
                    except Exception:
                        pass

    except Exception as e:
        print(f"[Login] 登录检测异常: {e}")
    return False


async def _check_qrcode_expired(page) -> bool:
    """检查二维码是否已过期"""
    try:
        if page.is_closed():
            return False

        page_content = await page.content()
        expire_keywords = ["二维码已过期", "二维码已失效", "重新获取二维码", "qrcode expired"]
        for kw in expire_keywords:
            if kw in page_content:
                return True

        # 检查是否有刷新/重新获取按钮覆盖在二维码上
        refresh_sel = await page.query_selector("xpath=//*[contains(text(),'点击刷新')]")
        if refresh_sel:
            return True

    except Exception:
        pass
    return False


# ==================== WebSocket 远程浏览器 ====================

# 远程浏览器会话状态（全局唯一，新连接会接管旧连接）
_ws_browser_context = None
_ws_page = None
_ws_playwright = None
_ws_active: Optional[WebSocket] = None  # 当前活跃的 WebSocket 连接

BROWSER_WIDTH = 1024
BROWSER_HEIGHT = 680


async def _ws_cleanup_browser():
    """清理远程浏览器资源（不关 WebSocket）"""
    global _ws_browser_context, _ws_page, _ws_playwright
    try:
        if _ws_page and not _ws_page.is_closed():
            await _ws_page.close()
    except Exception:
        pass
    _ws_page = None

    try:
        if _ws_browser_context:
            await _ws_browser_context.close()
    except Exception:
        pass
    _ws_browser_context = None

    try:
        if _ws_playwright:
            await _ws_playwright.stop()
    except Exception:
        pass
    _ws_playwright = None


def _ws_is_connected(ws: WebSocket) -> bool:
    """检查 WebSocket 是否仍然连接"""
    try:
        return ws.client_state.name == "CONNECTED"
    except Exception:
        return False


async def _ws_safe_send_json(ws: WebSocket, data: dict) -> bool:
    """安全地发送 JSON，失败返回 False"""
    try:
        if _ws_is_connected(ws):
            await ws.send_json(data)
            return True
    except Exception:
        pass
    return False


async def _ws_safe_send_bytes(ws: WebSocket, data: bytes) -> bool:
    """安全地发送二进制，失败返回 False"""
    try:
        if _ws_is_connected(ws):
            await ws.send_bytes(data)
            return True
    except Exception:
        pass
    return False


@router.websocket("/ws/browser")
async def ws_remote_browser(ws: WebSocket):
    """
    WebSocket 远程浏览器端点。
    - 服务端推送：截图帧 (binary JPEG) 或 JSON 控制消息
    - 客户端发送：鼠标/键盘事件 JSON
    新连接会踢掉旧连接并接管浏览器（或重新启动）。
    """
    global _ws_browser_context, _ws_page, _ws_playwright, _ws_active

    await ws.accept()
    print("[WS-Browser] 客户端已连接")

    # 踢掉旧连接
    if _ws_active and _ws_is_connected(_ws_active):
        try:
            await _ws_active.close(code=1000, reason="新客户端接入")
        except Exception:
            pass
    _ws_active = ws

    try:
        # ── 1. 启动浏览器（如果还没有或已关闭） ──
        need_start = (_ws_page is None or _ws_page.is_closed()
                      or _ws_browser_context is None)

        if need_start:
            await _ws_cleanup_browser()

            if not await _ws_safe_send_json(ws, {"type": "status", "message": "正在启动浏览器..."}):
                return

            from playwright.async_api import async_playwright
            import config

            _ws_playwright = await async_playwright().start()

            user_data_dir = os.path.join(
                os.getcwd(), "browser_data",
                config.USER_DATA_DIR % "xhs"
            )
            os.makedirs(user_data_dir, exist_ok=True)

            _ws_browser_context = await _ws_playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=True,
                viewport={"width": BROWSER_WIDTH, "height": BROWSER_HEIGHT},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )

            _ws_page = await _ws_browser_context.new_page()

            stealth_js_path = os.path.join(os.getcwd(), "libs", "stealth.min.js")
            if os.path.exists(stealth_js_path):
                await _ws_page.add_init_script(path=stealth_js_path)

            if not await _ws_safe_send_json(ws, {"type": "status", "message": "正在打开小红书..."}):
                return
            await _ws_page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
            await asyncio.sleep(2)

        # ── 2. 记录初始 cookie ──
        from tools import utils
        init_cookies = await _ws_browser_context.cookies()
        _, init_dict = utils.convert_cookies(init_cookies)
        init_ws = init_dict.get("web_session", "")

        if not await _ws_safe_send_json(ws, {
            "type": "status",
            "message": "浏览器已就绪，请扫码或操作页面",
            "width": BROWSER_WIDTH,
            "height": BROWSER_HEIGHT,
        }):
            return

        print(f"[WS-Browser] 浏览器已就绪，初始 web_session={init_ws[:12] if init_ws else '无'}...")

        # ── 3. 主循环 ──
        login_success = False
        frame_interval = 0.3       # 300ms 一帧 ≈ 3 FPS
        cookie_check_interval = 3
        last_cookie_check = 0

        while _ws_active is ws and _ws_is_connected(ws):
            frame_start = time.time()

            # ── 接收所有待处理的客户端事件（一次处理多个） ──
            should_break = False
            for _ in range(20):  # 最多处理 20 个积压事件
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=0.005)
                    evt = json.loads(raw)

                    if evt.get("type") == "close_browser":
                        print("[WS-Browser] 客户端请求关闭浏览器")
                        await _ws_cleanup_browser()
                        await _ws_safe_send_json(ws, {"type": "browser_closed", "message": "浏览器已关闭"})
                        should_break = True
                        break

                    await _handle_ws_event(evt)
                except asyncio.TimeoutError:
                    break  # 没有更多事件了
                except WebSocketDisconnect:
                    print("[WS-Browser] 客户端断开")
                    should_break = True
                    break
                except Exception:
                    break
            if should_break:
                break

            # ── 页面存活检查 ──
            if not _ws_page or _ws_page.is_closed():
                await _ws_safe_send_json(ws, {"type": "error", "message": "浏览器页面已关闭"})
                break

            # ── 截图推送 ──
            try:
                screenshot = await _ws_page.screenshot(type="jpeg", quality=40)
            except Exception as e:
                print(f"[WS-Browser] 截图失败: {e}")
                break

            if not await _ws_safe_send_bytes(ws, screenshot):
                print("[WS-Browser] 发送截图失败，客户端可能已断开")
                break

            # ── cookie 检查 ──
            now = time.time()
            if now - last_cookie_check >= cookie_check_interval:
                last_cookie_check = now
                try:
                    cur_cookies = await _ws_browser_context.cookies()
                    _, cur_dict = utils.convert_cookies(cur_cookies)
                    cur_ws = cur_dict.get("web_session", "")

                    if cur_ws and cur_ws != init_ws:
                        valid = await _ws_page.evaluate("""
                            async () => {
                                try {
                                    const r = await fetch('https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo', {
                                        credentials: 'include',
                                        headers: { 'Content-Type': 'application/json' }
                                    });
                                    if (!r.ok) return false;
                                    const d = await r.json();
                                    return d.success === true;
                                } catch(e) { return false; }
                            }
                        """)
                        if valid:
                            print(f"[WS-Browser] ★ 登录成功！web_session={cur_ws[:16]}...")
                            await _save_cookies_to_file(_ws_browser_context, "xhs")
                            await _ws_safe_send_json(ws, {
                                "type": "login_success",
                                "message": "登录成功！Cookie 已保存",
                                "web_session": cur_ws[:16] + "...",
                            })
                            login_success = True
                            # 推送最后一帧
                            try:
                                shot = await _ws_page.screenshot(type="jpeg", quality=40)
                                await _ws_safe_send_bytes(ws, shot)
                            except Exception:
                                pass
                            await asyncio.sleep(2)
                            break
                except Exception as e:
                    print(f"[WS-Browser] cookie 检查异常: {e}")

            # ── 帧率控制 ──
            elapsed = time.time() - frame_start
            sleep_time = max(0, frame_interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    except WebSocketDisconnect:
        print("[WS-Browser] 客户端断开")
    except Exception as e:
        print(f"[WS-Browser] 异常: {e}")
        await _ws_safe_send_json(ws, {"type": "error", "message": str(e)})
    finally:
        if _ws_active is ws:
            _ws_active = None
        # 注意：不自动清理浏览器，以便重连时复用
        print("[WS-Browser] 会话结束")


async def _handle_ws_event(event: dict):
    """处理前端发送的鼠标/键盘事件"""
    if not _ws_page or _ws_page.is_closed():
        return

    etype = event.get("type", "")
    x = event.get("x", 0)
    y = event.get("y", 0)

    try:
        if etype == "click":
            await _ws_page.mouse.click(x, y)
        elif etype == "mousedown":
            await _ws_page.mouse.move(x, y)
            await _ws_page.mouse.down()
        elif etype == "mousemove":
            await _ws_page.mouse.move(x, y)
        elif etype == "mouseup":
            await _ws_page.mouse.up()
        elif etype == "scroll":
            delta_x = event.get("deltaX", 0)
            delta_y = event.get("deltaY", 0)
            await _ws_page.mouse.wheel(delta_x, delta_y)
        elif etype == "keypress":
            key = event.get("key", "")
            if key:
                await _ws_page.keyboard.press(key)
        elif etype == "keydown":
            key = event.get("key", "")
            if key:
                await _ws_page.keyboard.down(key)
        elif etype == "keyup":
            key = event.get("key", "")
            if key:
                await _ws_page.keyboard.up(key)
        elif etype == "type":
            text = event.get("text", "")
            if text:
                await _ws_page.keyboard.type(text)
        elif etype == "select_all_delete":
            # Ctrl+A 全选，然后删除
            await _ws_page.keyboard.press("Control+a")
            await _ws_page.keyboard.press("Backspace")
        elif etype == "navigate":
            url = event.get("url", "")
            if url:
                await _ws_page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        print(f"[WS-Browser] 事件处理失败 ({etype}): {e}")


async def _cleanup_browser():
    """清理浏览器资源（幂等，可重复调用）"""
    global _browser_context, _context_page, _playwright_instance, _browser

    async with _cleanup_lock:
        try:
            if _context_page and not _context_page.is_closed():
                await _context_page.close()
        except Exception:
            pass
        _context_page = None

        try:
            if _browser_context:
                await _browser_context.close()
        except Exception:
            pass
        _browser_context = None

        try:
            if _browser:
                await _browser.close()
        except Exception:
            pass
        _browser = None

        try:
            if _playwright_instance:
                await _playwright_instance.stop()
        except Exception:
            pass
        _playwright_instance = None
