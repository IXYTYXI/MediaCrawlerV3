# -*- coding: utf-8 -*-
"""
小红书 Session 保活脚本

功能：在爬虫未运行期间，定期打开小红书页面，刷新 web_session cookie，
      防止 session 因长时间不活跃而过期。

原理：
  - 使用与爬虫相同的 Playwright 持久化浏览器目录（browser_data/xhs_user_data_dir）
  - 定期启动 headless 浏览器访问小红书，触发服务器续期 session
  - 如果浏览器目录被爬虫占用（SingletonLock），自动跳过，不会干扰爬虫运行

使用方式：
  # 前台运行（测试）
  conda run -n uvenv python -m tools.session_keeper

  # 后台运行（推荐放在 tmux 里）
  conda run -n uvenv python -m tools.session_keeper --interval 2

  # 自定义参数
  conda run -n uvenv python -m tools.session_keeper --interval 2 --max-retries 5
"""

import asyncio
import argparse
import os
import sys
import time
from datetime import datetime

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _setup_logger():
    """配置保活脚本的日志（控制台 + 文件，按天轮转保留14天）"""
    import logging
    from logging.handlers import TimedRotatingFileHandler

    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("SessionKeeper")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [SessionKeeper] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # 文件（按天轮转，保留14天）
    fh = TimedRotatingFileHandler(
        os.path.join(log_dir, "session_keeper.log"),
        when="midnight", interval=1, backupCount=14, encoding="utf-8",
    )
    fh.suffix = "%Y-%m-%d"
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


_logger = _setup_logger()


def _log(msg: str):
    """日志输出（控制台+文件）"""
    _logger.info(msg)


def _get_browser_data_dir() -> str:
    """获取浏览器持久化数据目录"""
    import config
    user_data_dir_name = config.USER_DATA_DIR % "xhs"
    return os.path.join(PROJECT_ROOT, "browser_data", user_data_dir_name)


def _is_browser_locked(browser_data_dir: str) -> bool:
    """检查浏览器数据目录是否被占用"""
    lock_file = os.path.join(browser_data_dir, "SingletonLock")
    return os.path.exists(lock_file)


def _get_cookies_mtime(browser_data_dir: str) -> float:
    """获取 Cookies 文件最后修改时间"""
    cookies_file = os.path.join(browser_data_dir, "Default", "Cookies")
    if os.path.exists(cookies_file):
        return os.path.getmtime(cookies_file)
    return 0


async def refresh_session(browser_data_dir: str) -> bool:
    """
    启动浏览器访问小红书，刷新 session cookie
    
    Returns:
        True=刷新成功, False=失败
    """
    from playwright.async_api import async_playwright

    stealth_path = os.path.join(PROJECT_ROOT, "libs", "stealth.min.js")

    pw = await async_playwright().start()
    try:
        _log("启动 headless 浏览器...")
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=browser_data_dir,
            headless=True,
            accept_downloads=False,
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        # 注入 stealth 脚本
        if os.path.exists(stealth_path):
            await ctx.add_init_script(path=stealth_path)

        # 读取刷新前的 cookie
        cookies_before = await ctx.cookies()
        ws_before = ""
        for c in cookies_before:
            if c.get("name") == "web_session":
                ws_before = c.get("value", "")
                break

        if not ws_before:
            _log("警告: 浏览器中无 web_session cookie，可能需要重新登录")
            await ctx.close()
            return False

        masked_before = ws_before[:8] + "..." + ws_before[-4:] if len(ws_before) > 12 else ws_before
        _log(f"当前 web_session: {masked_before}")

        # 访问小红书首页
        page = await ctx.new_page()
        _log("访问 xiaohongshu.com ...")
        await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")

        # 等待页面加载，模拟真实用户停留
        await asyncio.sleep(5)

        # 简单滚动一下页面，增加真实感
        await page.evaluate("window.scrollTo(0, 300)")
        await asyncio.sleep(2)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)

        # 读取刷新后的 cookie
        cookies_after = await ctx.cookies()
        ws_after = ""
        for c in cookies_after:
            if c.get("name") == "web_session":
                ws_after = c.get("value", "")
                break

        # 检查登录状态
        is_logged_in = False
        try:
            # 检查是否有"我"的链接（登录状态标志）
            me_selector = "xpath=//a[contains(@href, '/user/profile/')]"
            if await page.is_visible(me_selector, timeout=3000):
                is_logged_in = True
            else:
                # 没有二维码也算登录状态
                qr = await page.query_selector("xpath=//img[@class='qrcode-img']")
                if not qr:
                    is_logged_in = True
        except Exception:
            pass

        await page.close()
        await ctx.close()

        if ws_after:
            masked_after = ws_after[:8] + "..." + ws_after[-4:] if len(ws_after) > 12 else ws_after
            changed = "已更新" if ws_after != ws_before else "未变化"
            login_status = "已登录" if is_logged_in else "可能已过期"
            _log(f"刷新后 web_session: {masked_after} ({changed}, {login_status})")
            return is_logged_in
        else:
            _log("刷新后 web_session 丢失，session 可能已过期")
            return False

    except Exception as e:
        error_msg = str(e)
        if "SingletonLock" in error_msg or "already running" in error_msg.lower():
            _log("浏览器目录被占用（爬虫正在运行），跳过")
        else:
            _log(f"刷新失败: {error_msg}")
        return False
    finally:
        try:
            await pw.stop()
        except Exception:
            pass


def _seconds_until_next_midnight() -> float:
    """计算距离下一个 00:00:00 还有多少秒"""
    from datetime import timedelta
    now = datetime.now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = today_midnight + timedelta(days=1)
    return (next_midnight - now).total_seconds()


def _seconds_until_0318() -> float:
    """计算距离下一个 03:18:00 还有多少秒（凌晨前置刷新，覆盖服务端 3:30 左右刷新）"""
    from datetime import timedelta
    now = datetime.now()
    today_0318 = now.replace(hour=3, minute=18, second=0, microsecond=0)
    if now < today_0318:
        return (today_0318 - now).total_seconds()
    next_0318 = today_0318 + timedelta(days=1)
    return (next_0318 - now).total_seconds()


def _do_refresh(browser_data_dir: str, consecutive_failures: int, max_retries: int, reason: str = "") -> int:
    """
    执行一次 session 刷新逻辑

    Args:
        browser_data_dir: 浏览器数据目录
        consecutive_failures: 当前连续失败次数
        max_retries: 最大连续失败次数
        reason: 触发刷新的原因说明

    Returns:
        更新后的 consecutive_failures 值
    """
    if reason:
        _log(f"[触发原因] {reason}")

    # 检查锁文件
    if _is_browser_locked(browser_data_dir):
        _log("浏览器目录被占用（爬虫正在运行），跳过本次刷新")
        cookies_mtime = _get_cookies_mtime(browser_data_dir)
        if cookies_mtime > 0:
            ago = (time.time() - cookies_mtime) / 60
            _log(f"  Cookies 最后更新: {ago:.0f} 分钟前（爬虫在自动续期）")
        return 0  # 爬虫在跑说明 session 是好的
    else:
        # 检查 Cookies 最后更新时间
        cookies_mtime = _get_cookies_mtime(browser_data_dir)
        if cookies_mtime > 0:
            ago_hours = (time.time() - cookies_mtime) / 3600
            _log(f"Cookies 最后更新: {ago_hours:.1f} 小时前")
        else:
            _log("未找到 Cookies 文件")

        # 刷新 session
        success = asyncio.run(refresh_session(browser_data_dir))
        if success:
            _log("Session 刷新成功")
            return 0
        else:
            consecutive_failures += 1
            _log(f"Session 刷新失败 ({consecutive_failures}/{max_retries})")
            if consecutive_failures >= max_retries:
                _log(f"⚠️ 连续 {max_retries} 次失败，session 可能已过期，需要重新扫码登录")
                # 不退出，继续尝试，万一后面恢复了
            return consecutive_failures


def run_keeper(interval_hours: float = 2.0, max_retries: int = 3, midnight_refresh: bool = True, predawn_0318: bool = True):
    """
    主循环：定期刷新 session

    支持三种刷新触发：
    1. 定时刷新：每 interval_hours 小时刷新一次
    2. 午夜刷新：每天 00:00 强制刷新
    3. 凌晨前置刷新：每天 03:18 强制刷新（覆盖服务端 3:30 左右刷新）

    Args:
        interval_hours: 定时刷新间隔（小时），默认 2
        max_retries: 连续失败最大重试次数
        midnight_refresh: 是否开启每日 00:00 强制刷新
        predawn_0318: 是否开启每日 03:18 凌晨前置刷新
    """
    from datetime import timedelta

    browser_data_dir = _get_browser_data_dir()
    _log(f"Session 保活脚本启动")
    _log(f"  浏览器数据目录: {browser_data_dir}")
    _log(f"  定时刷新间隔: {interval_hours} 小时")
    _log(f"  每日午夜(00:00)刷新: {'开启' if midnight_refresh else '关闭'}")
    _log(f"  每日凌晨(03:18)前置刷新: {'开启' if predawn_0318 else '关闭'}")
    _log(f"  连续失败重试上限: {max_retries} 次")

    if not os.path.exists(browser_data_dir):
        _log("错误: 浏览器数据目录不存在，请先运行爬虫或扫码登录")
        return

    consecutive_failures = 0

    while True:
        _log("-" * 50)

        # 执行常规刷新
        consecutive_failures = _do_refresh(
            browser_data_dir, consecutive_failures, max_retries,
            reason="定时刷新"
        )

        # 计算下次唤醒：取 定时间隔、午夜、03:18 中最早的一个
        interval_seconds = interval_hours * 3600
        seconds_to_midnight = _seconds_until_next_midnight() if midnight_refresh else float("inf")
        seconds_to_0318 = _seconds_until_0318() if predawn_0318 else float("inf")

        sleep_seconds = min(interval_seconds, seconds_to_midnight, seconds_to_0318)

        # 加 10 秒缓冲，确保过了整点
        if sleep_seconds == seconds_to_midnight:
            sleep_seconds += 10
            next_time = datetime.now() + timedelta(seconds=sleep_seconds)
            _log(f"下次刷新: {next_time.strftime('%H:%M:%S')}（午夜 00:00 强制刷新，{sleep_seconds/60:.0f} 分钟后）")
        elif sleep_seconds == seconds_to_0318:
            sleep_seconds += 10
            next_time = datetime.now() + timedelta(seconds=sleep_seconds)
            _log(f"下次刷新: {next_time.strftime('%H:%M:%S')}（凌晨 03:18 前置刷新，{sleep_seconds/60:.0f} 分钟后）")
        else:
            next_time = datetime.now() + timedelta(seconds=sleep_seconds)
            _log(f"下次刷新: {next_time.strftime('%H:%M:%S')}（定时 {interval_hours}h 后）")
        _log("")

        try:
            time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            _log("收到中断信号，退出")
            break

        # 若是午夜或 03:18 唤醒，执行对应强制刷新（常规刷新已在循环开头做过，这里只需做"定点"的）
        now = datetime.now()
        if midnight_refresh and 0 <= now.hour < 1 and now.minute < 5:
            _log("-" * 50)
            _log("🕛 每日午夜强制刷新 (00:00)")
            consecutive_failures = _do_refresh(
                browser_data_dir, consecutive_failures, max_retries,
                reason="每日 00:00 午夜强制刷新"
            )
        elif predawn_0318 and now.hour == 3 and 18 <= now.minute < 25:
            _log("-" * 50)
            _log("🌙 每日凌晨前置刷新 (03:18)")
            consecutive_failures = _do_refresh(
                browser_data_dir, consecutive_failures, max_retries,
                reason="每日 03:18 凌晨前置刷新（覆盖服务端约 3:30 刷新）"
            )


def main():
    parser = argparse.ArgumentParser(description="小红书 Session 保活脚本")
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="刷新间隔，单位小时（默认 2）"
    )
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="连续失败最大次数，超过后会持续告警（默认 3）"
    )
    parser.add_argument(
        "--no-midnight", action="store_true",
        help="禁用每日午夜（00:00）强制刷新"
    )
    parser.add_argument(
        "--no-predawn", action="store_true",
        help="禁用每日凌晨（03:18）前置刷新"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="只刷新一次，不循环（用于测试）"
    )
    args = parser.parse_args()

    if args.once:
        browser_data_dir = _get_browser_data_dir()
        _log(f"单次刷新模式，浏览器目录: {browser_data_dir}")
        if _is_browser_locked(browser_data_dir):
            _log("浏览器目录被占用，跳过")
        else:
            success = asyncio.run(refresh_session(browser_data_dir))
            _log(f"结果: {'成功' if success else '失败'}")
    else:
        run_keeper(
            interval_hours=args.interval,
            max_retries=args.max_retries,
            midnight_refresh=not args.no_midnight,
            predawn_0318=not args.no_predawn,
        )


if __name__ == "__main__":
    main()
