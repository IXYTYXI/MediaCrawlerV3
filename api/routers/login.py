# -*- coding: utf-8 -*-
"""
远程扫码登录 API
通过 Web 界面在无图形界面的 Linux 服务器上完成小红书扫码登录

流程: 启动浏览器 → 打开登录页 → 截图二维码 → 用户扫码 → 检测成功 → 保存Cookie
"""
import asyncio
import base64
import os
import time
from typing import Optional, Dict, Any

from fastapi import APIRouter

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

        # 4. 检查是否已登录
        _login_state["message"] = "检查登录状态..."
        if await _check_already_logged_in(_context_page, _browser_context):
            _login_state["status"] = "success"
            _login_state["message"] = "已登录（Cookie有效）"
            await _take_screenshot(_context_page)
            await asyncio.sleep(2)
            return  # finally 会清理

        # 5. 尝试触发登录弹窗
        _login_state["message"] = "等待登录页面..."
        await _trigger_login_dialog(_context_page)
        await asyncio.sleep(2)

        # 6. 提取二维码
        _login_state["message"] = "提取二维码..."
        await _capture_qrcode(_context_page)

        if not _login_state["qrcode_base64"] and not _login_state["screenshot_base64"]:
            # 直接截图整个页面
            await _take_screenshot(_context_page)

        _login_state["status"] = "waiting_scan"
        _login_state["message"] = "请用小红书APP扫描二维码登录"

        # 7. 等待登录成功（轮询120秒）
        for i in range(120):
            if _login_state["status"] == "cancelled":
                return

            # 捕获当前 page 引用（防止被 cleanup 置空）
            page = _context_page
            ctx = _browser_context
            if not page or page.is_closed() or not ctx:
                return

            # 每3秒刷新一次截图
            if i % 3 == 0:
                await _take_screenshot(page)
                await _capture_qrcode(page)

            # 检查登录状态
            if await _check_already_logged_in(page, ctx):
                _login_state["status"] = "success"
                _login_state["message"] = "登录成功！Cookie已保存"
                await _take_screenshot(page)
                await asyncio.sleep(3)
                return  # finally 会清理

            await asyncio.sleep(1)

        # 超时
        _login_state["status"] = "failed"
        _login_state["message"] = "登录超时（120秒），请重试"

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


async def _capture_qrcode(page):
    """提取二维码图片"""
    try:
        if page.is_closed():
            return

        # 方法1: 直接获取二维码图片元素
        qr_selector = "xpath=//img[@class='qrcode-img']"
        qr_elem = await page.query_selector(qr_selector)
        if qr_elem:
            src = await qr_elem.get_attribute("src")
            if src:
                if src.startswith("data:image"):
                    # data URL 格式，提取 base64
                    _login_state["qrcode_base64"] = src.split(",", 1)[-1] if "," in src else src
                elif src.startswith("http"):
                    # URL 格式，下载图片
                    import httpx
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        resp = await client.get(src)
                        if resp.status_code == 200:
                            _login_state["qrcode_base64"] = base64.b64encode(resp.content).decode()
                else:
                    _login_state["qrcode_base64"] = src
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


async def _check_already_logged_in(page, browser_context) -> bool:
    """检查是否已登录"""
    try:
        if page.is_closed():
            return False

        from tools import utils

        # 方法1: 检查 "我" 按钮
        me_selector = "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']"
        if await page.is_visible(me_selector, timeout=500):
            return True

        # 方法2: 检查 cookie
        cookies = await browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(cookies)
        web_session = cookie_dict.get("web_session", "")
        # 有 web_session 且页面没有登录弹窗
        if web_session:
            login_dialog = await page.query_selector("xpath=//img[@class='qrcode-img']")
            if not login_dialog:
                return True

    except Exception:
        pass
    return False


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
