# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
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

import asyncio
import json
import os
import random
from asyncio import Task
from typing import Dict, List, Optional

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)
from tenacity import RetryError

import config
from base.base_crawler import AbstractCrawler
from model.m_xiaohongshu import NoteUrlInfo, CreatorUrlInfo
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import utils
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var
from tools.keyword_filter import (
    parse_multi_keyword_expressions, match_note_multi,
    FilterScope, KeywordExpression,
)

from .client import XiaoHongShuClient
from .exception import DataFetchError, SessionExpiredError
from .field import SearchSortType
from .help import parse_note_info_from_note_url, parse_creator_info_from_url, get_search_id
from .login import XiaoHongShuLogin

# 断点续爬模块
from tools.crawl_progress import get_progress_manager, CrawlProgressManager


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://www.xiaohongshu.com"
        # self.user_agent = utils.get_user_agent()
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh
        self._session_pool = None
        self._current_session_id: Optional[str] = None
        self._page_since_rotate = 0
        self._rotate_every_pages = 0

    @staticmethod
    def _load_shared_cookie() -> str:
        """从共享 cookie 文件读取最新的 web_session（远程扫码登录写入的）"""
        import json as _json
        cookie_file = os.path.join(os.getcwd(), "data", "cookies", "xhs_cookies.json")
        try:
            if not os.path.exists(cookie_file):
                return ""
            with open(cookie_file, "r", encoding="utf-8") as f:
                data = _json.load(f)
            ws = data.get("web_session", "")
            if ws:
                utils.logger.info(
                    f"[XiaoHongShuCrawler] 读取共享 cookie 文件: "
                    f"更新于 {data.get('updated_at', '?')}, 来源: {data.get('source', '?')}"
                )
            return ws
        except Exception as e:
            utils.logger.warning(f"[XiaoHongShuCrawler] 读取共享 cookie 文件失败: {e}")
            return ""

    def _load_manual_web_session(self) -> str:
        """从 config.MANUAL_WEB_SESSION 或 data/cookies/manual_web_session.txt 读取手动配置的 web_session"""
        ws = getattr(config, "MANUAL_WEB_SESSION", "") or ""
        if ws:
            return ws.strip()
        manual_file = os.path.join(os.getcwd(), "data", "cookies", "manual_web_session.txt")
        try:
            if os.path.exists(manual_file):
                with open(manual_file, "r", encoding="utf-8") as f:
                    ws = f.read().strip()
                return ws
        except Exception as e:
            utils.logger.warning(f"[XiaoHongShuCrawler] 读取 manual_web_session.txt 失败: {e}")
        return ""

    def _init_session_pool(self) -> None:
        """初始化 session 池（搜索模式下的 session 轮换）"""
        try:
            cfg_path = os.path.join("config", "anti_crawl_config.json")
            with open(cfg_path, "r", encoding="utf-8") as f:
                pool_cfg = json.load(f).get("session_pool", {})
            if not pool_cfg.get("enabled"):
                return
            self._rotate_every_pages = pool_cfg.get("rotate_every_pages", 3)
            if self._rotate_every_pages <= 0:
                return
            from tools.session_pool import SessionPool
            pool = SessionPool()
            stats = pool.stats()
            if stats.get("active", 0) < 1:
                utils.logger.info("[SessionPool] 池内无可用 session，不启用轮换")
                return
            self._session_pool = pool
            # 取一个初始 session
            first = pool.get_next()
            if first:
                self._current_session_id = first.id
                utils.logger.info(
                    f"[SessionPool] 搜索模式已启用 | "
                    f"{stats.get('active', 0)} 个可用 session | "
                    f"每 {self._rotate_every_pages} 页轮换 | "
                    f"初始: {first.label or first.id} ({first.web_session[:12]}...)"
                )
        except Exception as e:
            utils.logger.debug(f"[SessionPool] 初始化跳过: {e}")
            self._session_pool = None

    async def _rotate_session(self, context: str = "") -> bool:
        """
        轮换 session：从池中取下一个，注入浏览器和 client。
        成功返回 True。
        """
        if not self._session_pool:
            return False
        next_s = self._session_pool.get_next()
        if not next_s:
            utils.logger.warning("[SessionPool] 无可用 session，跳过轮换")
            return False
        self._current_session_id = next_s.id
        try:
            await self.browser_context.add_cookies([{
                "name": "web_session",
                "value": next_s.web_session,
                "domain": ".xiaohongshu.com",
                "path": "/",
            }])
            await self.xhs_client.update_cookies(browser_context=self.browser_context)
            utils.logger.info(
                f"[SessionPool] {context}切换 session → "
                f"{next_s.label or next_s.id} ({next_s.web_session[:12]}...)"
            )
            self._page_since_rotate = 0
            return True
        except Exception as e:
            utils.logger.warning(f"[SessionPool] 切换失败: {e}")
            return False

    async def _maybe_rotate_session(self, context: str = "") -> None:
        """每爬 N 页自动检查是否需要轮换"""
        if not self._session_pool or self._rotate_every_pages <= 0:
            return
        self._page_since_rotate += 1
        if self._page_since_rotate >= self._rotate_every_pages:
            await self._rotate_session(context)

    def _mark_session_success(self) -> None:
        if self._session_pool and self._current_session_id:
            self._session_pool.mark_success(self._current_session_id)

    async def _handle_session_failure(self, context: str = "") -> None:
        """session 失败时标记并尝试切换"""
        if self._session_pool and self._current_session_id:
            self._session_pool.mark_failed(self._current_session_id)
            await self._rotate_session(f"{context}失效自动")

    def _get_sleep_seconds(self, *, for_comments: bool = False) -> float:
        """使用高级随机分布生成等待时间，含限流冷却期加成"""
        from tools.anti_crawl_utils import generate_random_wait, get_wait_manager
        
        if not getattr(config, "RANDOM_SLEEP_ENABLED", False):
            return float(getattr(config, "CRAWLER_MAX_SLEEP_SEC", 0))
        
        # 获取动态调整乘数
        multiplier = get_wait_manager().get_multiplier(config)
        
        if for_comments:
            min_sec = max(0.0, float(getattr(config, "RANDOM_SLEEP_COMMENTS_MIN_SEC", 3.0)))
            max_sec = max(min_sec, float(getattr(config, "RANDOM_SLEEP_COMMENTS_MAX_SEC", 6.0)))
            distribution = getattr(config, "RANDOM_SLEEP_COMMENTS_DISTRIBUTION", "lognormal")
        else:
            min_sec = max(0.0, float(getattr(config, "RANDOM_SLEEP_MIN_SEC", 5.0)))
            max_sec = max(min_sec, float(getattr(config, "RANDOM_SLEEP_MAX_SEC", 10.0)))
            distribution = getattr(config, "RANDOM_SLEEP_DISTRIBUTION", "lognormal")
        
        base_wait = generate_random_wait(min_sec, max_sec, distribution)
        
        # 限流恢复冷却期：显著降低请求速率，避免立即再次触发
        recovery_multiplier = 1.0
        if hasattr(self, 'xhs_client'):
            recovery_multiplier = self.xhs_client.get_recovery_multiplier()
            if recovery_multiplier > 1.0:
                remaining = getattr(self.xhs_client, '_post_recovery_remaining', 0)
                utils.logger.debug(
                    f"[冷却期] 等待 {base_wait * multiplier * recovery_multiplier:.1f}s "
                    f"(基础={base_wait:.1f}s × 冷却={recovery_multiplier:.1f}x, "
                    f"剩余 {remaining} 次)"
                )
        
        return base_wait * multiplier * recovery_multiplier

    async def _maybe_do_fake_action(self) -> bool:
        """
        随机执行假动作：搜索热门关键词、模拟滚动、鼠标移动等，模拟真实用户浏览行为。
        数据不会保存。
        Returns:
            bool: 是否执行了假动作
        """
        from tools.anti_crawl_utils import (
            generate_random_wait, simulate_scroll, 
            simulate_mouse_move, simulate_input
        )
        
        if not getattr(config, "FAKE_ACTION_ENABLED", False):
            return False
        
        probability = getattr(config, "FAKE_ACTION_PROBABILITY", 0.15)
        if random.random() > probability:
            return False
        
        try:
            # 1. 模拟页面滚动
            if hasattr(self, 'context_page') and self.context_page:
                await simulate_scroll(self.context_page, config)
                await simulate_mouse_move(self.context_page, config)
                await simulate_input(self.context_page, config)
            
            # 2. 随机搜索热门关键词
            default_keywords = ["美食", "旅行", "穿搭", "护肤", "健身", "摄影", "宠物", "家居",
                               "数码", "音乐", "电影", "书籍", "咖啡", "甜点", "打卡", "探店"]
            fake_keywords = getattr(config, "FAKE_ACTION_KEYWORDS", None) or default_keywords
            keyword = random.choice(fake_keywords)
            
            utils.logger.info(f"[FakeAction] 执行假动作：搜索关键词 '{keyword}'")
            await self.xhs_client.get_note_by_keyword(
                keyword=keyword,
                search_id=get_search_id(),
                page=1,
                page_size=10
            )
            
            # 假动作后的随机等待（使用高级分布）
            min_sec = getattr(config, "FAKE_ACTION_MIN_SEC", 2.0)
            max_sec = getattr(config, "FAKE_ACTION_MAX_SEC", 5.0)
            wait_time = generate_random_wait(min_sec, max_sec, "lognormal")
            utils.logger.info(f"[FakeAction] 假动作完成，等待 {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            return True
        except Exception as e:
            utils.logger.warning(f"[FakeAction] 假动作执行失败: {e}")
            return False

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            # Choose launch mode based on configuration
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.CDP_HEADLESS,
                )
            else:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.HEADLESS,
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                await self.browser_context.add_init_script(path="libs/stealth.min.js")

            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)

            # Create a client to interact with the Xiaohongshu website.
            self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
            if not await self.xhs_client.pong():
                # Step 1: 检查浏览器持久化目录是否已有有效 session（自动续期的 cookie）
                browser_cookies = await self.browser_context.cookies()
                _, browser_cookie_dict = utils.convert_cookies(browser_cookies)
                saved_web_session = browser_cookie_dict.get("web_session", "")

                if saved_web_session:
                    utils.logger.info(
                        f"[XiaoHongShuCrawler] 发现浏览器持久化 session: {saved_web_session[:16]}...，尝试复用"
                    )
                    # 用浏览器已有的 session 重新创建 client 并验证
                    await self.xhs_client.update_cookies(browser_context=self.browser_context)
                    if await self.xhs_client.pong():
                        utils.logger.info("[XiaoHongShuCrawler] ✅ 浏览器持久化 session 有效，免登录")
                    else:
                        utils.logger.info("[XiaoHongShuCrawler] 持久化 session 已过期，尝试注入 config cookie")
                        saved_web_session = ""  # 标记无效，走下面的注入逻辑

                # Step 2: 尝试从共享 cookie 文件读取（远程扫码登录保存的最新 session）
                if not saved_web_session:
                    shared_cookie = self._load_shared_cookie()
                    if shared_cookie:
                        utils.logger.info(
                            f"[XiaoHongShuCrawler] 发现共享 cookie 文件 (web_session={shared_cookie[:16]}...)，尝试注入"
                        )
                        shared_cookie_str = f"web_session={shared_cookie}"
                        login_obj_shared = XiaoHongShuLogin(
                            login_type="cookie",
                            login_phone="",
                            browser_context=self.browser_context,
                            context_page=self.context_page,
                            cookie_str=shared_cookie_str,
                        )
                        await login_obj_shared.begin()
                        await self.xhs_client.update_cookies(browser_context=self.browser_context)
                        if await self.xhs_client.pong():
                            utils.logger.info("[XiaoHongShuCrawler] ✅ 共享 cookie 文件中的 session 有效，免登录")
                            saved_web_session = shared_cookie  # 标记有效，跳过后续步骤

                # Step 3: 共享文件也没有或无效，注入 config 中的 cookie
                if not saved_web_session:
                    if config.COOKIES:
                        utils.logger.info("[XiaoHongShuCrawler] 注入 config cookie 并验证...")
                        login_obj = XiaoHongShuLogin(
                            login_type="cookie",
                            login_phone="",
                            browser_context=self.browser_context,
                            context_page=self.context_page,
                            cookie_str=config.COOKIES,
                        )
                        await login_obj.begin()
                        await self.xhs_client.update_cookies(browser_context=self.browser_context)

                        if not await self.xhs_client.pong():
                            utils.logger.warning("[XiaoHongShuCrawler] ⚠️ config cookie 也已过期")
                        else:
                            utils.logger.info("[XiaoHongShuCrawler] ✅ config cookie 有效")
                            saved_web_session = "ok"

                # Step 4: 检查手动配置的 web_session
                if not saved_web_session:
                    manual_ws = self._load_manual_web_session()
                    if manual_ws:
                        utils.logger.info(
                            f"[XiaoHongShuCrawler] 尝试注入手动 web_session: {manual_ws[:12]}..."
                        )
                        login_obj_manual = XiaoHongShuLogin(
                            login_type="cookie",
                            login_phone="",
                            browser_context=self.browser_context,
                            context_page=self.context_page,
                            cookie_str=f"web_session={manual_ws}",
                        )
                        await login_obj_manual.begin()
                        await self.xhs_client.update_cookies(browser_context=self.browser_context)
                        if await self.xhs_client.pong():
                            utils.logger.info("[XiaoHongShuCrawler] ✅ 手动 web_session 有效")
                            saved_web_session = manual_ws
                        else:
                            utils.logger.warning("[XiaoHongShuCrawler] ⚠️ 手动 web_session 也无效")

                # Step 5: 所有 cookie 方式都失败，回退到原始登录方式（扫码/手机号）
                if not saved_web_session:
                    utils.logger.warning(
                        f"[XiaoHongShuCrawler] 所有 cookie/session 均无效，"
                        f"回退到原始登录方式 (LOGIN_TYPE={config.LOGIN_TYPE})"
                    )
                    try:
                        login_obj_final = XiaoHongShuLogin(
                            login_type=config.LOGIN_TYPE,
                            login_phone="",
                            browser_context=self.browser_context,
                            context_page=self.context_page,
                            cookie_str=config.COOKIES,
                        )
                        await login_obj_final.begin()
                        await self.xhs_client.update_cookies(browser_context=self.browser_context)
                    except Exception as login_err:
                        utils.logger.error(
                            f"[XiaoHongShuCrawler] ❌ 原始登录方式也失败: {login_err}\n"
                            f"  💡 请手动更新 session：\n"
                            f"     方式1: 编辑 config/base_config.py → MANUAL_WEB_SESSION\n"
                            f"     方式2: 写入 data/cookies/manual_web_session.txt\n"
                            f"     方式3: 更新 config/base_config.py → COOKIES 字段"
                        )
                        raise SessionExpiredError(f"All login methods failed: {login_err}")

            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                await self.search()
            elif config.CRAWLER_TYPE == "search_top":
                await self.search_top()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_notes()
            elif config.CRAWLER_TYPE == "creator":
                # Get creator's information and their notes and comments
                await self.get_creators_and_notes()
            elif config.CRAWLER_TYPE == "creator_keyword":
                await self.get_creators_and_notes_by_keyword()
            else:
                pass

            utils.logger.info("[XiaoHongShuCrawler.start] Xhs Crawler finished ...")

    def _get_search_keywords(self) -> List[str]:
        """根据 KEYWORDS_COMBINE_MODE 返回待搜索关键词列表。
        True=组合模式：逗号分隔的词用空格拼接为一个搜索词（如 A,B,C → "A B C"）；
        False=逐词模式：每个词单独搜索。"""
        parts = [k.strip() for k in config.KEYWORDS.split(",") if k.strip()]
        if not parts:
            return []
        if getattr(config, "KEYWORDS_COMBINE_MODE", False):
            return [" ".join(parts)]
        return parts

    @staticmethod
    def _note_in_date_range(note_detail: dict, date_start: str, date_end: str) -> bool:
        """Check if a note's publish time is within [date_start, date_end]."""
        if not date_start and not date_end:
            return True
        note_time = note_detail.get("time", 0)
        if not note_time or not isinstance(note_time, (int, float)):
            return True
        try:
            from datetime import datetime as _dt
            publish_date = _dt.fromtimestamp(note_time / 1000)
            if date_start:
                if publish_date < _dt.strptime(date_start, "%Y-%m-%d"):
                    return False
            if date_end:
                if publish_date > _dt.strptime(date_end, "%Y-%m-%d").replace(hour=23, minute=59, second=59):
                    return False
            return True
        except Exception:
            return True

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords")
        self._init_session_pool()
        xhs_limit_count = 20  # Xiaohongshu limit page fixed value
        if config.CRAWLER_MAX_NOTES_COUNT < xhs_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = xhs_limit_count
        start_page = config.START_PAGE
        keywords_to_search = self._get_search_keywords()
        if not keywords_to_search:
            utils.logger.warning("[XiaoHongShuCrawler.search] 无有效关键词，跳过搜索")
            return
        combine_mode = getattr(config, "KEYWORDS_COMBINE_MODE", False)
        _date_start = getattr(config, "CRAWL_DATE_START", "")
        _date_end = getattr(config, "CRAWL_DATE_END", "")
        _date_filter_active = bool(_date_start or _date_end)
        if _date_filter_active:
            utils.logger.info(
                f"[XiaoHongShuCrawler.search] 日期过滤已启用: {_date_start or '不限'} ~ {_date_end or '不限'}"
            )
        utils.logger.info(
            f"[XiaoHongShuCrawler.search] 关键词模式: {'组合为一个搜索' if combine_mode else '逐词分别搜索'} | "
            f"共 {len(keywords_to_search)} 个搜索项"
        )
        for keyword in keywords_to_search:
            source_keyword_var.set(keyword)
            utils.logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            page = 1
            search_id = get_search_id()
            while (page - start_page + 1) * xhs_limit_count <= config.CRAWLER_MAX_NOTES_COUNT:
                if page < start_page:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Skip page {page}")
                    page += 1
                    continue

                try:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] search Xiaohongshu keyword: {keyword}, page: {page}")
                    note_ids: List[str] = []
                    xsec_tokens: List[str] = []
                    notes_res = await self.xhs_client.get_note_by_keyword(
                        keyword=keyword,
                        search_id=search_id,
                        page=page,
                        sort=(SearchSortType(config.SORT_TYPE) if config.SORT_TYPE != "" else SearchSortType.GENERAL),
                    )
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Search notes response: {notes_res}")
                    if not notes_res or not notes_res.get("has_more", False):
                        utils.logger.info("[XiaoHongShuCrawler.search] No more content!")
                        break
                    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                    task_list = [
                        self.get_note_detail_async_task(
                            note_id=post_item.get("id"),
                            xsec_source=post_item.get("xsec_source"),
                            xsec_token=post_item.get("xsec_token"),
                            semaphore=semaphore,
                        ) for post_item in notes_res.get("items", {}) if post_item.get("model_type") not in ("rec_query", "hot_query")
                    ]
                    note_details = await asyncio.gather(*task_list)
                    _date_skipped = 0
                    for note_detail in note_details:
                        if note_detail:
                            if _date_filter_active and not self._note_in_date_range(note_detail, _date_start, _date_end):
                                _date_skipped += 1
                                continue
                            await xhs_store.update_xhs_note(note_detail)
                            await self.get_notice_media(note_detail)
                            note_ids.append(note_detail.get("note_id"))
                            xsec_tokens.append(note_detail.get("xsec_token"))
                    if _date_skipped > 0:
                        utils.logger.info(f"[XiaoHongShuCrawler.search] 日期过滤: 本页跳过 {_date_skipped} 条不在范围内的笔记")
                    page += 1
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Note details: {note_details}")
                    await self.batch_get_note_comments(note_ids, xsec_tokens)
                    self._mark_session_success()

                    await self._maybe_rotate_session(f"[search] 第{page-1}页后 ")

                    sleep_seconds = self._get_sleep_seconds()
                    await asyncio.sleep(sleep_seconds)
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Sleeping for {sleep_seconds} seconds after page {page-1}")
                except SessionExpiredError:
                    utils.logger.error("[XiaoHongShuCrawler.search] 登录已过期")
                    await self._handle_session_failure("[search] ")
                    raise
                except DataFetchError:
                    utils.logger.error("[XiaoHongShuCrawler.search] Get note detail error")
                    await self._handle_session_failure("[search] ")
                    break

    # ==================== 关键词高赞搜索 ====================

    async def search_top(self) -> None:
        """
        关键词高赞搜索：
        1. 按热度搜索，收集 top_notes_count 条笔记详情（含赞数）
        2. 全部保存，按点赞数降序排序
        3. 对点赞前 top_comment_notes_count 篇爬取有限页评论
        """
        self._init_session_pool()
        fetch_count = getattr(config, "SEARCH_TOP_NOTES_COUNT", 100)
        comment_top = getattr(config, "SEARCH_TOP_COMMENT_NOTES_COUNT", 20)
        comment_pages = getattr(config, "SEARCH_TOP_COMMENT_PAGE_COUNT", 2)
        comment_max_count = comment_pages * 20
        xhs_page_size = 20

        est_pages = (fetch_count + xhs_page_size - 1) // xhs_page_size
        keywords_to_search = self._get_search_keywords()
        if not keywords_to_search:
            utils.logger.warning("[search_top] 无有效关键词，跳过")
            return
        combine_mode = getattr(config, "KEYWORDS_COMBINE_MODE", False)
        utils.logger.info(
            f"[search_top] 开始关键词高赞搜索 | "
            f"关键词模式: {'组合为一个搜索' if combine_mode else '逐词分别搜索'} | "
            f"共 {len(keywords_to_search)} 个搜索项 | "
            f"搜索{fetch_count}条(约{est_pages}页) → 按赞排序 → "
            f"前{comment_top}篇爬{comment_pages}页评论"
        )

        for keyword in keywords_to_search:
            source_keyword_var.set(keyword)
            utils.logger.info(f"[search_top] 关键词: {keyword}")

            # ---- Phase 1: 按热度搜索，逐页收集笔记详情 ----
            collected: List[Dict] = []
            page = 1
            search_id = get_search_id()

            while len(collected) < fetch_count:
                try:
                    utils.logger.info(
                        f"[search_top] 搜索第 {page} 页, 已收集 {len(collected)}/{fetch_count}"
                    )
                    notes_res = await self.xhs_client.get_note_by_keyword(
                        keyword=keyword,
                        search_id=search_id,
                        page=page,
                        sort=SearchSortType.MOST_POPULAR,
                    )
                    if not notes_res or not notes_res.get("has_more", False):
                        utils.logger.info("[search_top] 搜索结果已到底")
                        break

                    items = [
                        item for item in notes_res.get("items", [])
                        if item.get("model_type") not in ("rec_query", "hot_query")
                    ]
                    if not items:
                        break

                    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                    tasks = [
                        self.get_note_detail_async_task(
                            note_id=item.get("id"),
                            xsec_source=item.get("xsec_source"),
                            xsec_token=item.get("xsec_token"),
                            semaphore=semaphore,
                        )
                        for item in items
                    ]
                    details = await asyncio.gather(*tasks)
                    for detail in details:
                        if detail:
                            collected.append(detail)

                    self._mark_session_success()
                    page += 1
                    await self._maybe_rotate_session(f"[search_top] 第{page-1}页后 ")
                    sleep_seconds = self._get_sleep_seconds()
                    await asyncio.sleep(sleep_seconds)

                except SessionExpiredError:
                    utils.logger.error("[search_top] 登录已过期")
                    await self._handle_session_failure("[search_top] ")
                    raise
                except DataFetchError:
                    utils.logger.error("[search_top] 获取笔记详情出错，停止翻页")
                    break

            utils.logger.info(
                f"[search_top] 关键词「{keyword}」搜索完成，共 {len(collected)} 条笔记"
            )
            if not collected:
                continue

            # ---- Phase 2: 按点赞数排序，全部保存 ----
            def _liked_count(note: Dict) -> int:
                interact = note.get("interact_info", {})
                raw = interact.get("liked_count", 0)
                try:
                    val = str(raw).replace("+", "").replace("万", "0000")
                    return int(float(val))
                except (ValueError, TypeError):
                    return 0

            collected.sort(key=_liked_count, reverse=True)

            utils.logger.info(
                f"[search_top] 按赞排序完成 | "
                f"第1名: {_liked_count(collected[0])}赞, "
                f"末位: {_liked_count(collected[-1])}赞"
            )

            for note_detail in collected:
                await xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)

            # ---- Phase 3: 对点赞前 M 篇笔记爬评论 ----
            comment_notes = collected[:comment_top]
            utils.logger.info(
                f"[search_top] 开始爬取点赞前 {len(comment_notes)} 篇笔记的评论 "
                f"(每篇 {comment_pages} 页, 约 {comment_max_count} 条)"
            )
            crawl_interval = self._get_sleep_seconds(for_comments=True)

            for idx, note_detail in enumerate(comment_notes, 1):
                note_id = note_detail.get("note_id", "")
                xsec_token = note_detail.get("xsec_token", "")
                if not note_id:
                    continue
                utils.logger.info(
                    f"[search_top] [{idx}/{len(comment_notes)}] "
                    f"爬评论 note_id={note_id} ({_liked_count(note_detail)}赞)"
                )
                try:
                    await self.xhs_client.get_note_all_comments(
                        note_id=note_id,
                        xsec_token=xsec_token,
                        crawl_interval=crawl_interval,
                        callback=xhs_store.batch_update_xhs_note_comments,
                        max_count=comment_max_count,
                    )
                    self._mark_session_success()
                    await asyncio.sleep(crawl_interval)
                except SessionExpiredError:
                    utils.logger.error(f"[search_top] 评论爬取登录过期 note_id={note_id}")
                    await self._handle_session_failure("[search_top-comment] ")
                    raise
                except Exception as e:
                    utils.logger.warning(
                        f"[search_top] 评论获取失败 note_id={note_id}: {e}"
                    )
                    await self._handle_session_failure("[search_top-comment] ")

                await self._maybe_rotate_session(
                    f"[search_top] 评论[{idx}/{len(comment_notes)}]后 "
                )

            utils.logger.info(
                f"[search_top] 关键词「{keyword}」全部完成 — "
                f"{len(collected)} 条笔记已保存, "
                f"前 {len(comment_notes)} 篇高赞笔记评论已爬取"
            )

    async def get_creators_and_notes(self) -> None:
        """
        获取作者的作品和评论信息
        流程：
        1. 获取所有作品列表
        2. 获取所有作品详情（保存到作品文件）
        3. 获取所有评论（保存到评论文件）
        
        支持断点续爬：自动跳过已爬取的内容
        """
        utils.logger.info("[XiaoHongShuCrawler.get_creators_and_notes] 开始爬取作者信息")
        
        for creator_url in config.XHS_CREATOR_ID_LIST:
            try:
                # 解析作者URL
                creator_info: CreatorUrlInfo = parse_creator_info_from_url(creator_url)
                utils.logger.info(f"[XiaoHongShuCrawler] 解析作者URL: {creator_info}")
                user_id = creator_info.user_id
                
                # ========== 初始化日期停止标记 ==========
                self._early_stop_requested = False
                self._consecutive_old_batches = 0
                self._consecutive_all_skip_batches = 0

                # ========== 断点续爬：初始化进度管理器 ==========
                self._progress_manager: CrawlProgressManager = get_progress_manager("xhs", "creator")
                self._crawled_note_ids = self._progress_manager.load_progress(user_id)
                self._comment_crawled_ids = self._progress_manager.get_comment_crawled_ids()
                
                if self._crawled_note_ids:
                    utils.logger.info(f"[断点续爬] 作品: 跳过 {len(self._crawled_note_ids)} 条已爬取")
                if self._comment_crawled_ids:
                    utils.logger.info(f"[断点续爬] 评论: 跳过 {len(self._comment_crawled_ids)} 条已获取")

                # 获取作者基本信息
                createor_info: Dict = await self.xhs_client.get_creator_info(
                    user_id=user_id,
                    xsec_token=creator_info.xsec_token,
                    xsec_source=creator_info.xsec_source
                )
                if createor_info:
                    await xhs_store.save_creator(user_id, creator=createor_info)
                    nickname = createor_info.get("basicInfo", {}).get("nickname", user_id)
                    utils.logger.info(f"[作者信息] {nickname}")
            except ValueError as e:
                utils.logger.error(f"[XiaoHongShuCrawler] 解析作者URL失败: {e}")
                continue

            try:
                # ========== 阶段1：获取所有作品列表并保存详情 ==========
                utils.logger.info("=" * 50)
                utils.logger.info("[阶段1] 开始获取作品列表并获取详情（边获取边保存）...")
                
                # 加载保存的分页游标（跳过已枚举的页面）
                saved_cursor, saved_enumerated = self._progress_manager.get_saved_cursor()
                
                crawl_interval = self._get_sleep_seconds()
                all_notes_list = await self.xhs_client.get_all_notes_by_creator(
                    user_id=user_id,
                    crawl_interval=crawl_interval,
                    callback=self._fetch_all_notes_detail,
                    xsec_token=creator_info.xsec_token,
                    xsec_source=creator_info.xsec_source,
                    resume_cursor=saved_cursor,
                    resume_enumerated=saved_enumerated,
                )
                
                utils.logger.info(f"[阶段1] 完成！共获取 {len(all_notes_list)} 条作品")
                
                # ========== 阶段2：补爬失败的详情 ==========
                utils.logger.info("=" * 50)
                utils.logger.info("[阶段2] 开始补爬失败的详情...")
                
                await self._refetch_failed_details(all_notes_list)
                
                utils.logger.info(f"[阶段2] 完成！补爬结束")
                
                # ========== 阶段3：获取所有评论 ==========
                comments_mode = getattr(config, 'COMMENTS_FETCH_MODE', 'parallel')
                
                if config.ENABLE_GET_COMMENTS:
                    if comments_mode == 'parallel':
                        # 并行模式：评论已在阶段1中获取
                        comment_crawled = getattr(self, '_comment_crawled_ids', set())
                        utils.logger.info("=" * 50)
                        utils.logger.info(f"[阶段3] 并行模式 - 评论已在阶段1获取 ({len(comment_crawled)} 条)")
                        
                        # 检查是否有遗漏的评论需要补爬
                        new_crawled = getattr(self, '_new_crawled_ids', set())
                        missing_comments = new_crawled - comment_crawled
                        
                        if missing_comments:
                            utils.logger.info(f"[阶段3] 补爬 {len(missing_comments)} 条遗漏的评论...")
                            for note in all_notes_list:
                                note_id = note.get("note_id")
                                if note_id in missing_comments:
                                    try:
                                        await self.get_comments(note_id, note.get("xsec_token", ""))
                                    except Exception as e:
                                        utils.logger.warning(f"[阶段3] 补爬评论失败: {note_id}, 错误: {e}")
                        
                        utils.logger.info(f"[阶段3] 完成！")
                    else:
                        # 顺序模式：在阶段3获取所有评论
                        utils.logger.info("=" * 50)
                        utils.logger.info("[阶段3] 顺序模式 - 开始获取评论...")
                        
                        # 断点续爬：只获取未爬取评论的作品
                        notes_for_comments = []
                        comment_crawled = getattr(self, '_comment_crawled_ids', set())
                        
                        for note in all_notes_list:
                            note_id = note.get("note_id")
                            # 只有新爬取的作品且评论未爬取才获取评论
                            if note_id in getattr(self, '_new_crawled_ids', set()) and note_id not in comment_crawled:
                                notes_for_comments.append(note)
                        
                        if notes_for_comments:
                            note_ids = [note.get("note_id") for note in notes_for_comments]
                            xsec_tokens = [note.get("xsec_token") for note in notes_for_comments]
                            await self.batch_get_note_comments(note_ids, xsec_tokens)
                            utils.logger.info(f"[阶段3] 完成！评论已保存（{len(notes_for_comments)} 条作品）")
                        else:
                            utils.logger.info("[阶段3] 所有作品评论已获取，跳过")
                else:
                    utils.logger.info("[阶段3] 跳过评论获取（未开启）")
                
                utils.logger.info("=" * 50)
                utils.logger.info(f"[完成] 作者 {user_id} 爬取完成")
                # 正常完成：清除游标，下次从头开始
                if hasattr(self, '_progress_manager'):
                    self._progress_manager.clear_cursor()
                
            except Exception as e:
                utils.logger.error(f"[爬取异常] {e}")
                # 异常中断：保存当前游标，下次从断点恢复
                if hasattr(self, '_progress_manager') and hasattr(self, 'xhs_client'):
                    cur = getattr(self.xhs_client, '_last_pagination_cursor', '')
                    if cur:
                        enumerated = len(getattr(self, '_new_crawled_ids', set())) + len(getattr(self, '_crawled_note_ids', set()))
                        self._progress_manager.update_cursor(cur, enumerated)
                raise
            finally:
                # ========== 断点续爬：保存进度 ==========
                if hasattr(self, '_progress_manager'):
                    self._progress_manager.save_progress()
                    stats = self._progress_manager.get_stats()
                    utils.logger.info(f"[断点续爬] 进度已保存: 成功 {stats['crawled_count']} 条")

    def _parse_filter_scope(self) -> FilterScope:
        """从 config 解析过滤范围配置"""
        scope_str = getattr(config, "CREATOR_KEYWORD_FILTER_SCOPE", "title,desc,tags")
        parts = {s.strip().lower() for s in scope_str.split(",") if s.strip()}
        return FilterScope(
            title="title" in parts,
            desc="desc" in parts,
            tags="tags" in parts,
            comments="comments" in parts,
            author_desc="author_desc" in parts,
        )

    async def get_creators_and_notes_by_keyword(self) -> None:
        """
        作者×关键词 组合搜索模式：
        复用 creator 模式获取作者的全部笔记，在获取详情后按关键词本地过滤，
        只保存匹配的笔记。

        流程：
        1. 解析过滤关键词表达式
        2. 设置关键词过滤器到实例属性（供 _fetch_all_notes_detail 使用）
        3. 调用现有的 get_creators_and_notes 执行爬取（自动过滤）
        4. 清理过滤器状态
        """
        filter_kw = getattr(config, "CREATOR_KEYWORD_FILTER_KEYWORDS", "")
        if not filter_kw.strip():
            utils.logger.warning(
                "[creator_keyword] CREATOR_KEYWORD_FILTER_KEYWORDS 为空，"
                "将退化为普通 creator 模式（不过滤）"
            )

        expressions = parse_multi_keyword_expressions(filter_kw)
        scope = self._parse_filter_scope()

        kw_display = [e.raw for e in expressions] if expressions else ["(无过滤)"]
        scope_parts = []
        if scope.title: scope_parts.append("标题")
        if scope.desc: scope_parts.append("正文")
        if scope.tags: scope_parts.append("标签")
        if scope.comments: scope_parts.append("评论")
        if scope.author_desc: scope_parts.append("作者简介")

        utils.logger.info(
            f"[creator_keyword] 作者×关键词模式启动\n"
            f"  关键词: {kw_display}\n"
            f"  过滤范围: {', '.join(scope_parts)}\n"
            f"  作者数: {len(config.XHS_CREATOR_ID_LIST)}"
        )

        self._keyword_filter_expressions = expressions
        self._keyword_filter_scope = scope
        self._keyword_filter_stats = {"matched": 0, "filtered": 0}

        try:
            await self.get_creators_and_notes()
        finally:
            stats = getattr(self, "_keyword_filter_stats", {})
            utils.logger.info(
                f"[creator_keyword] 完成 | "
                f"匹配: {stats.get('matched', 0)} | "
                f"过滤: {stats.get('filtered', 0)}"
            )
            self._keyword_filter_expressions = None
            self._keyword_filter_scope = None
            self._keyword_filter_stats = None

    async def _fetch_all_notes_detail(self, note_list: List[Dict]):
        """
        获取所有作品详情并保存（支持断点续爬 + 并行评论获取）
        Args:
            note_list: 作品列表
        """
        if not note_list:
            return
        
        # 初始化失败记录集合和新爬取集合
        if not hasattr(self, '_failed_note_ids'):
            self._failed_note_ids = set()
        if not hasattr(self, '_new_crawled_ids'):
            self._new_crawled_ids = set()
        if not hasattr(self, '_comment_crawled_ids'):
            self._comment_crawled_ids = set()  # 评论已爬取的 note_id
        
        # 获取已爬取的 note_id 集合（断点续爬）
        crawled_ids = getattr(self, '_crawled_note_ids', set())
        progress_manager = getattr(self, '_progress_manager', None)
        
        # 获取评论配置
        comments_mode = getattr(config, 'COMMENTS_FETCH_MODE', 'parallel')
        comments_delay = getattr(config, 'COMMENTS_DELAY_SEC', 1.0)
        comments_concurrency = getattr(config, 'COMMENTS_CONCURRENCY', 2)
        enable_comments = getattr(config, 'ENABLE_GET_COMMENTS', True)
        
        # 并行评论任务管理
        comment_tasks: List[Task] = []
        comment_semaphore = asyncio.Semaphore(comments_concurrency)
        
        total = len(note_list)
        success_count = 0
        fail_count = 0
        skip_count = 0
        stats_updated_count = 0  # 老作品只更新互动数据计数
        consecutive_fail_count = 0  # 连续失败计数（用于 session 失效熔断）
        max_consecutive_fails = 5   # 连续失败熔断阈值
        save_interval = 10  # 每10条保存一次进度
        
        # 按日期提前停止
        early_stop_enabled = getattr(config, 'DATE_EARLY_STOP_ENABLED', False)
        early_stop_threshold = getattr(config, 'DATE_EARLY_STOP_THRESHOLD', 5)
        crawl_date_start = getattr(config, 'CRAWL_DATE_START', '')
        consecutive_old_count = 0  # 连续超出日期范围的计数
        early_stopped = False
        batch_has_old_notes = False  # 本批次是否有超期笔记

        # 列表阶段轻量互动量刷新
        list_level_stats = getattr(config, 'LIST_LEVEL_STATS_UPDATE', False)
        list_cutoff_date = getattr(config, 'LIST_LEVEL_STATS_CUTOFF_DATE', '')
        list_date_floor = getattr(config, 'LIST_LEVEL_DATE_FLOOR', '')
        list_stats_updated = 0
        list_fallback_count = 0
        floor_consecutive_old = 0
        _FLOOR_STOP_THRESHOLD = 10

        def _parse_list_publish_date(post_item: dict) -> str:
            """从列表条目解析发布日期，返回 'YYYY-MM-DD' 或 ''
            优先 corner_tag_info（搜索结果），回退到 timestamp（user_posted API）"""
            from datetime import datetime as _dt, date as _date
            note_card = post_item.get("note_card", {})
            for tag in note_card.get("corner_tag_info", []):
                if tag.get("type") == "publish_time":
                    text = tag.get("text", "").strip()
                    if not text:
                        continue
                    try:
                        _dt.strptime(text, "%Y-%m-%d")
                        return text
                    except ValueError:
                        pass
                    try:
                        this_year = _date.today().year
                        d = _dt.strptime(f"{this_year}-{text}", "%Y-%m-%d")
                        return d.strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                    return _date.today().strftime("%Y-%m-%d")
            # 回退：从 timestamp 解析（user_posted API 返回毫秒级时间戳）
            for time_key in ["time", "create_time", "last_update_time"]:
                ts = note_card.get(time_key) or post_item.get(time_key)
                if ts and isinstance(ts, (int, float)) and ts > 0:
                    try:
                        if ts > 1e12:
                            ts = ts / 1000
                        return _dt.fromtimestamp(ts).strftime("%Y-%m-%d")
                    except (ValueError, OSError):
                        pass
            # 字符串类型的时间戳 fallback
            for time_key in ["time", "create_time", "last_update_time"]:
                ts_raw = note_card.get(time_key) or post_item.get(time_key)
                if ts_raw and isinstance(ts_raw, str):
                    try:
                        ts_val = int(ts_raw)
                        if ts_val > 1e12:
                            ts_val = ts_val / 1000
                        return _dt.fromtimestamp(ts_val).strftime("%Y-%m-%d")
                    except (ValueError, OSError):
                        pass
            return ""

        def _extract_list_interact(post_item: dict) -> dict:
            """从列表条目的 note_card.interact_info 提取互动量字段"""
            note_card = post_item.get("note_card", {})
            info = note_card.get("interact_info", {})
            def _i(v):
                try:
                    return int(str(v).replace(",", "").strip()) if v else 0
                except (ValueError, TypeError):
                    return 0
            liked = _i(info.get("liked_count", 0))
            collected = _i(info.get("collected_count", 0))
            comment = _i(info.get("comment_count", 0))
            return {"liked": liked, "collected": collected, "comment": comment,
                    "interaction": liked + collected + comment}
        
        is_parallel_mode = comments_mode == 'parallel' and enable_comments
        
        if is_parallel_mode:
            utils.logger.info(f"[详情获取] 共 {total} 条作品待处理（并行获取评论，延迟 {comments_delay}s，并发 {comments_concurrency}）")
        else:
            utils.logger.info(f"[详情获取] 共 {total} 条作品待处理")
        
        for idx, post_item in enumerate(note_list, 1):
            # ========== 按日期提前停止 ==========
            if early_stopped:
                break
            
            note_id = post_item.get("note_id")
            xsec_token = post_item.get("xsec_token", "")
            display_title = post_item.get("display_title", "")[:20]

            # ========== 列表阶段轻量互动量刷新（含 date_floor + crawled_ids 整合） ==========
            if list_level_stats and note_id:
                pub_date = _parse_list_publish_date(post_item)

                # 日期地板：早于 date_floor 的笔记直接跳过，不更新也不补爬
                if list_date_floor and pub_date and pub_date < list_date_floor:
                    floor_consecutive_old += 1
                    if floor_consecutive_old >= _FLOOR_STOP_THRESHOLD:
                        self._early_stop_requested = True
                        utils.logger.info(
                            f"[详情获取] 连续 {floor_consecutive_old} 条早于 {list_date_floor}，停止翻页"
                        )
                    skip_count += 1
                    continue
                elif pub_date:
                    floor_consecutive_old = 0

                # 已爬取过的笔记：直接从列表取互动量更新，跳过详情接口
                if note_id in crawled_ids:
                    task_dir = getattr(config, 'XHS_TASK_DIR', '') or ''
                    user_id_cur = getattr(config, 'XHS_CURRENT_USER_ID', '') or ''
                    _cache_key = f"_stats_existing_cache_{user_id_cur}"
                    if not getattr(self, _cache_key, None):
                        cache: dict = {}
                        for suffix in ["_contents.json", "_reuse.json"]:
                            fp = os.path.join(task_dir, f"creator_{user_id_cur}{suffix}")
                            if os.path.exists(fp):
                                try:
                                    with open(fp, "r", encoding="utf-8") as _f:
                                        arr = json.load(_f)
                                    if isinstance(arr, list):
                                        for it in arr:
                                            nid = it.get("note_id")
                                            if nid:
                                                cache[nid] = it
                                except Exception:
                                    pass
                        setattr(self, _cache_key, cache)
                    existing_cache = getattr(self, _cache_key, {})
                    existing_note = existing_cache.get(note_id)
                    if existing_note:
                        stats = _extract_list_interact(post_item)
                        existing_note.update({
                            "liked_count": stats["liked"],
                            "collected_count": stats["collected"],
                            "comment_count": stats["comment"],
                        })
                        try:
                            await xhs_store.update_xhs_note(existing_note)
                        except Exception:
                            pass
                        list_stats_updated += 1
                        utils.logger.debug(
                            f"[详情获取] ({idx}/{total}) ⚡ 列表互动量更新: {display_title}... "
                            f"[👍{stats['liked']} 🌟{stats['collected']} 💬{stats['comment']}]"
                        )
                    skip_count += 1
                    continue

                # 不在 crawled_ids 中 + pub_date 为空 → 无法判断日期，标记需要后验
                # 不在 crawled_ids 中 + pub_date 有效 → 真正的新笔记，走详情接口流程

            # ========== 断点续爬：已爬取作品（list_level_stats 关闭时的原逻辑） ==========
            if not list_level_stats and note_id in crawled_ids:
                # 老作品只更新互动数据：获取详情、合并、写入，不下载媒体
                if getattr(config, 'ENABLE_STATS_UPDATE_FOR_CRAWLED', False):
                    task_dir = getattr(config, 'XHS_TASK_DIR', '') or ''
                    user_id = getattr(config, 'XHS_CURRENT_USER_ID', '') or ''
                    if task_dir and user_id:
                        try:
                            # 懒加载已有数据（contents + reuse）
                            _cache_key = f"_stats_existing_cache_{user_id}"
                            if not getattr(self, _cache_key, None):
                                cache = {}
                                for suffix in ["_contents.json", "_reuse.json"]:
                                    fp = os.path.join(task_dir, f"creator_{user_id}{suffix}")
                                    if os.path.exists(fp):
                                        try:
                                            with open(fp, "r", encoding="utf-8") as f:
                                                arr = json.load(f)
                                            if isinstance(arr, list):
                                                for it in arr:
                                                    nid = it.get("note_id")
                                                    if nid:
                                                        cache[nid] = it
                                        except Exception:
                                            pass
                                setattr(self, _cache_key, cache)
                            existing = getattr(self, _cache_key, {}).get(note_id, {})
                            note_detail = await self.xhs_client.get_note_by_id(
                                note_id,
                                post_item.get("xsec_source", "pc_feed"),
                                xsec_token
                            )
                            if note_detail and note_detail.get("note_id"):
                                note_detail.update({
                                    "xsec_token": xsec_token,
                                    "xsec_source": post_item.get("xsec_source", "")
                                })
                                from tools.note_merge import merge_note_metadata
                                merged = merge_note_metadata(existing, note_detail)
                                await xhs_store.update_xhs_note(merged)
                                stats_updated_count += 1
                                utils.logger.info(
                                    f"[详情获取] ({idx}/{total}) 📊 统计更新: {display_title}... [累计: {stats_updated_count}]"
                                )
                            else:
                                skip_count += 1
                                utils.logger.debug(f"[详情获取] ({idx}/{total}) ⏭ 跳过（详情无效）: {display_title}...")
                            # 统计更新也需要等待和假动作，防止限流
                            sleep_seconds = self._get_sleep_seconds()
                            await asyncio.sleep(sleep_seconds)
                            await self._maybe_do_fake_action()
                        except Exception as e:
                            utils.logger.warning(f"[详情获取] 统计更新失败 {note_id}: {e}")
                            skip_count += 1
                    else:
                        skip_count += 1
                        utils.logger.debug(f"[详情获取] ({idx}/{total}) ⏭ 跳过（已爬取）: {display_title}...")
                else:
                    skip_count += 1
                    utils.logger.debug(f"[详情获取] ({idx}/{total}) ⏭ 跳过（已爬取）: {display_title}...")
                continue

            try:
                # 获取详情
                note_detail = await self.xhs_client.get_note_by_id(
                    note_id, 
                    post_item.get("xsec_source", "pc_feed"),
                    xsec_token
                )
                
                # ========== 第 1 层：笔记数据校验门 ==========
                # 校验核心字段，空壳/无效数据不落盘
                note_valid = (
                    note_detail
                    and note_detail.get("note_id")
                    and note_detail.get("user", {}).get("user_id")
                )
                
                if note_valid:
                    note_detail.update({
                        "xsec_token": xsec_token,
                        "xsec_source": post_item.get("xsec_source", "")
                    })

                    # date_floor 后验：列表阶段无法解析日期时，用详情的 time 兜底
                    if list_date_floor:
                        _note_ts = note_detail.get("time", 0)
                        if _note_ts and isinstance(_note_ts, (int, float)):
                            try:
                                from datetime import datetime as _dt
                                _ts = _note_ts / 1000 if _note_ts > 1e12 else _note_ts
                                _note_date = _dt.fromtimestamp(_ts).strftime("%Y-%m-%d")
                                if _note_date < list_date_floor:
                                    floor_consecutive_old += 1
                                    utils.logger.info(
                                        f"[详情获取] ({idx}/{total}) ✗ {display_title}... | {_note_date} "
                                        f"早于地板 {list_date_floor}，丢弃 (连续 {floor_consecutive_old})"
                                    )
                                    if floor_consecutive_old >= _FLOOR_STOP_THRESHOLD:
                                        self._early_stop_requested = True
                                        utils.logger.info(
                                            f"[详情获取] 连续 {floor_consecutive_old} 条早于 {list_date_floor}，停止翻页"
                                        )
                                    skip_count += 1
                                    continue
                                else:
                                    floor_consecutive_old = 0
                            except Exception:
                                pass

                    # ========== 作者×关键词 本地过滤 ==========
                    _kf_expressions = getattr(self, '_keyword_filter_expressions', None)
                    if _kf_expressions is not None and _kf_expressions:
                        _kf_scope = getattr(self, '_keyword_filter_scope', None)
                        _kf_matched = match_note_multi(note_detail, _kf_expressions, _kf_scope)
                        _kf_stats = getattr(self, '_keyword_filter_stats', {})
                        if not _kf_matched:
                            _kf_stats["filtered"] = _kf_stats.get("filtered", 0) + 1
                            skip_count += 1
                            utils.logger.debug(
                                f"[详情获取] ({idx}/{total}) 🔍 关键词未匹配，跳过: {display_title}..."
                            )
                            continue
                        _kf_stats["matched"] = _kf_stats.get("matched", 0) + 1
                        note_detail["source_keyword"] = "|".join(_kf_matched)

                    await xhs_store.update_xhs_note(note_detail)
                    await self.get_notice_media(note_detail)
                    success_count += 1
                    consecutive_fail_count = 0

                    if progress_manager:
                        progress_manager.add_crawled(note_id)
                    self._new_crawled_ids.add(note_id)

                    crawled_so_far = len(getattr(self, '_new_crawled_ids', set())) + len(crawled_ids)
                    note_ts = note_detail.get("time", 0)
                    note_date_str = ""
                    if note_ts and isinstance(note_ts, (int, float)):
                        try:
                            from datetime import datetime as _dt
                            _ts = note_ts / 1000 if note_ts > 1e12 else note_ts
                            note_date_str = _dt.fromtimestamp(_ts).strftime("%Y-%m-%d")
                        except Exception:
                            pass
                    date_tag = f" | {note_date_str}" if note_date_str else ""
                    _kw_tag = ""
                    if getattr(self, '_keyword_filter_expressions', None):
                        _matched_kw = note_detail.get("source_keyword", "")
                        _kw_tag = f" | 🔍{_matched_kw}" if _matched_kw else ""
                    utils.logger.info(f"[详情获取] ({idx}/{total}) ✓ {display_title}...{date_tag}{_kw_tag} [累计: {crawled_so_far}]")
                    
                    # ========== 按日期提前停止检查 ==========
                    if early_stop_enabled and crawl_date_start:
                        note_time = note_detail.get("time", 0)
                        if note_time and isinstance(note_time, (int, float)):
                            try:
                                from datetime import datetime as _dt
                                publish_date = _dt.fromtimestamp(note_time / 1000)
                                start_date = _dt.strptime(crawl_date_start, "%Y-%m-%d")
                                if publish_date < start_date:
                                    consecutive_old_count += 1
                                    batch_has_old_notes = True
                                    utils.logger.info(
                                        f"[详情获取] 作品日期 {publish_date.strftime('%Y-%m-%d')} "
                                        f"早于 {crawl_date_start}，本批次内连续 {consecutive_old_count} 条"
                                    )
                                    if consecutive_old_count >= early_stop_threshold:
                                        utils.logger.info(
                                            f"[详情获取] ⏹ 连续 {early_stop_threshold} 条作品早于 {crawl_date_start}，"
                                            f"提前停止当前批次"
                                        )
                                        early_stopped = True
                                else:
                                    consecutive_old_count = 0  # 重置计数
                            except Exception:
                                pass
                    
                    # ========== 并行模式：启动评论获取任务 ==========
                    if is_parallel_mode and note_id not in self._comment_crawled_ids:
                        task = asyncio.create_task(
                            self._fetch_comments_with_delay(
                                note_id, xsec_token, comments_delay, comment_semaphore, display_title
                            )
                        )
                        comment_tasks.append(task)
                else:
                    # 数据无效（空响应或缺少核心字段），不保存到磁盘
                    consecutive_fail_count += 1
                    self._failed_note_ids.add(note_id)
                    if progress_manager:
                        progress_manager.add_failed(note_id)
                    fail_count += 1
                    utils.logger.warning(
                        f"[详情获取] ({idx}/{total}) ✗ {display_title}... "
                        f"(数据无效，不落盘，连续失败: {consecutive_fail_count}/{max_consecutive_fails})"
                    )
                    
                    # ========== 第 2 层：连续失败熔断器 ==========
                    if consecutive_fail_count >= max_consecutive_fails:
                        msg = (
                            f"连续 {consecutive_fail_count} 次获取笔记详情失败/数据无效，"
                            f"疑似 session 失效，触发熔断"
                        )
                        utils.logger.error(f"[详情获取] ⚡ {msg}")
                        raise SessionExpiredError(msg)
                    
            except SessionExpiredError:
                # SessionExpiredError 不捕获，直接向上抛出
                raise
            except Exception as e:
                # 异常时不保存基本信息（可能是 session 失效导致的）
                consecutive_fail_count += 1
                self._failed_note_ids.add(note_id)
                if progress_manager:
                    progress_manager.add_failed(note_id)
                fail_count += 1
                utils.logger.warning(
                    f"[详情获取] ({idx}/{total}) ✗ {display_title}... 错误: {e} "
                    f"(连续失败: {consecutive_fail_count}/{max_consecutive_fails})"
                )
                
                # ========== 第 2 层：连续失败熔断器（异常路径） ==========
                if consecutive_fail_count >= max_consecutive_fails:
                    msg = (
                        f"连续 {consecutive_fail_count} 次获取笔记详情异常，"
                        f"疑似 session 失效，触发熔断"
                    )
                    utils.logger.error(f"[详情获取] ⚡ {msg}")
                    raise SessionExpiredError(msg)
            
            # 等待
            sleep_seconds = self._get_sleep_seconds()
            await asyncio.sleep(sleep_seconds)
            
            # 随机假动作
            await self._maybe_do_fake_action()
            
            # ========== 断点续爬：定期保存进度 + 分页游标 ==========
            if progress_manager and (success_count + fail_count) % save_interval == 0:
                if hasattr(self, 'xhs_client'):
                    cur = getattr(self.xhs_client, '_last_pagination_cursor', '')
                    if cur:
                        enumerated = len(getattr(self, '_new_crawled_ids', set())) + len(crawled_ids)
                        progress_manager.update_cursor(cur, enumerated)
                progress_manager.save_progress()
        
        # ========== 等待所有评论任务完成 ==========
        if comment_tasks:
            utils.logger.info(f"[评论获取] 等待 {len(comment_tasks)} 个评论任务完成...")
            await asyncio.gather(*comment_tasks, return_exceptions=True)
            utils.logger.info(f"[评论获取] 所有评论任务已完成")
        
        # 最终保存一次进度（含分页游标）
        if progress_manager:
            if hasattr(self, 'xhs_client'):
                cur = getattr(self.xhs_client, '_last_pagination_cursor', '')
                if cur:
                    enumerated = len(getattr(self, '_new_crawled_ids', set())) + len(crawled_ids)
                    progress_manager.update_cursor(cur, enumerated)
            progress_manager.save_progress()
        
        # ========== 批次级别日期停止信号 ==========
        # 设置实例标记，让外层 get_all_notes_by_creator 能感知到
        if batch_has_old_notes:
            if not hasattr(self, '_consecutive_old_batches'):
                self._consecutive_old_batches = 0
            self._consecutive_old_batches += 1
            utils.logger.info(
                f"[日期检测] 本批次包含超期笔记，连续超期批次: {self._consecutive_old_batches}/2"
            )
            if self._consecutive_old_batches >= 2:
                self._early_stop_requested = True
                utils.logger.info(
                    f"[日期检测] ⏹ 连续 2 个批次包含早于 {crawl_date_start} 的笔记，"
                    f"通知外层停止该作者的爬取"
                )
        else:
            self._consecutive_old_batches = 0  # 本批次没有超期，重置

        # ========== 全跳过批次检测：有历史数据时，连续全跳过达阈值则停止翻页 ==========
        # 阈值必须为 3（防止因偶发跳过导致误判），禁止修改！
        # list_stats_updated > 0 表示有互动量更新，不算「无效跳过」
        _ALL_SKIP_THRESHOLD = 3
        _has_history = len(crawled_ids) > 0
        if _has_history and skip_count == total and success_count == 0 and list_stats_updated == 0:
            if not hasattr(self, '_consecutive_all_skip_batches'):
                self._consecutive_all_skip_batches = 0
            self._consecutive_all_skip_batches += 1
            if self._consecutive_all_skip_batches >= _ALL_SKIP_THRESHOLD:
                self._early_stop_requested = True
                utils.logger.info(
                    f"[增量检测] 连续 {self._consecutive_all_skip_batches} 个批次全部跳过"
                    f"（已有 {len(crawled_ids)} 条历史数据，无新内容），停止翻页"
                )
        elif success_count > 0 or list_stats_updated > 0:
            self._consecutive_all_skip_batches = 0

        # ========== 跳过批次：防限流延迟 ==========
        # list_level_stats 模式下只翻了列表页没调详情，不需要长延迟
        if list_level_stats and list_stats_updated > 0 and success_count == 0:
            pass
        elif total > 0 and skip_count > total * 0.5:
            skip_delay = random.uniform(0.3, 0.8) * skip_count
            utils.logger.info(
                f"[详情获取] 本批次大量跳过 ({skip_count}/{total})，"
                f"等待 {skip_delay:.1f}s 防限流"
            )
            await asyncio.sleep(skip_delay)

        early_stop_msg = f", 提前停止(早于{crawl_date_start})" if early_stopped else ""
        stats_msg = f", 统计更新 {stats_updated_count}" if stats_updated_count > 0 else ""
        list_stats_msg = (
            f", 列表互动量更新 {list_stats_updated}"
            + (f"(补爬 {list_fallback_count} 条)" if list_fallback_count else "")
        ) if list_stats_updated > 0 else ""
        utils.logger.info(f"[详情获取] 汇总: 成功 {success_count}, 失败 {fail_count}, 跳过 {skip_count}{stats_msg}{list_stats_msg}, 总计 {total}{early_stop_msg}")
    
    async def _fetch_comments_with_delay(
        self, 
        note_id: str, 
        xsec_token: str, 
        delay_sec: float, 
        semaphore: asyncio.Semaphore,
        display_title: str = ""
    ):
        """
        延迟后获取单个作品的评论（带信号量控制并发）
        """
        async with semaphore:
            try:
                # 延迟执行
                await asyncio.sleep(delay_sec)
                
                utils.logger.info(f"[评论获取] 开始: {display_title}... (note_id: {note_id[:8]}...)")
                
                # 获取评论
                await self.xhs_client.get_note_all_comments(
                    note_id=note_id,
                    xsec_token=xsec_token,
                    callback=xhs_store.batch_update_xhs_note_comments,
                    max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES
                )
                
                # 记录评论已爬取
                self._comment_crawled_ids.add(note_id)
                
                # 更新断点续爬进度（评论）
                progress_manager = getattr(self, '_progress_manager', None)
                if progress_manager:
                    progress_manager.add_comment_crawled(note_id)
                
                utils.logger.info(f"[评论获取] 完成: {display_title}...")
                
            except Exception as e:
                utils.logger.warning(f"[评论获取] 失败: {display_title}... 错误: {e}")

    async def _refetch_failed_details(self, all_notes_list: List[Dict]):
        """
        补爬失败的详情
        找出没有成功获取详情的记录，重新尝试获取
        """
        # 找出需要补爬的记录
        failed_notes = []
        for note in all_notes_list:
            note_id = note.get("note_id")
            # 检查这个 note 是否在失败列表中，或者通过其他方式判断
            if note_id in getattr(self, '_failed_note_ids', set()):
                failed_notes.append(note)
        
        if not failed_notes:
            utils.logger.info("[补爬] 没有需要补爬的记录")
            return
        
        utils.logger.info(f"[补爬] 发现 {len(failed_notes)} 条记录需要补爬")
        utils.logger.info("[补爬] 等待 60 秒后开始补爬（让反爬冷却）...")
        await asyncio.sleep(60)
        
        success_count = 0
        fail_count = 0
        
        for idx, post_item in enumerate(failed_notes, 1):
            note_id = post_item.get("note_id")
            display_title = post_item.get("display_title", "")[:20]
            
            utils.logger.info(f"[补爬] ({idx}/{len(failed_notes)}) {display_title}...")
            
            try:
                note_detail = await self.xhs_client.get_note_by_id(
                    note_id, 
                    post_item.get("xsec_source", "pc_feed"),
                    post_item.get("xsec_token", "")
                )
                
                if note_detail:
                    note_detail.update({
                        "xsec_token": post_item.get("xsec_token", ""),
                        "xsec_source": post_item.get("xsec_source", "")
                    })
                    await xhs_store.update_xhs_note(note_detail)
                    success_count += 1
                    utils.logger.info(f"[补爬] ({idx}/{len(failed_notes)}) ✓ 成功")
                else:
                    fail_count += 1
                    utils.logger.warning(f"[补爬] ({idx}/{len(failed_notes)}) ✗ 返回空")
                    
            except Exception as e:
                fail_count += 1
                utils.logger.warning(f"[补爬] ({idx}/{len(failed_notes)}) ✗ 错误: {e}")
            
            # 补爬时使用更长的等待时间
            sleep_seconds = self._get_sleep_seconds() * 2
            await asyncio.sleep(sleep_seconds)
        
        utils.logger.info(f"[补爬] 汇总: 成功 {success_count}, 失败 {fail_count}")

    async def get_specified_notes(self):
        """Get the information and comments of the specified post

        Note: Must specify note_id, xsec_source, xsec_token
        """
        get_note_detail_task_list = []
        for full_note_url in config.XHS_SPECIFIED_NOTE_URL_LIST:
            note_url_info: NoteUrlInfo = parse_note_info_from_note_url(full_note_url)
            utils.logger.info(f"[XiaoHongShuCrawler.get_specified_notes] Parse note url info: {note_url_info}")
            crawler_task = self.get_note_detail_async_task(
                note_id=note_url_info.note_id,
                xsec_source=note_url_info.xsec_source,
                xsec_token=note_url_info.xsec_token,
                semaphore=asyncio.Semaphore(config.MAX_CONCURRENCY_NUM),
            )
            get_note_detail_task_list.append(crawler_task)

        need_get_comment_note_ids = []
        xsec_tokens = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for note_detail in note_details:
            if note_detail:
                need_get_comment_note_ids.append(note_detail.get("note_id", ""))
                xsec_tokens.append(note_detail.get("xsec_token", ""))
                await xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)
        await self.batch_get_note_comments(need_get_comment_note_ids, xsec_tokens)

    async def get_note_detail_async_task(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        semaphore: asyncio.Semaphore,
    ) -> Optional[Dict]:
        """Get note detail

        Args:
            note_id:
            xsec_source:
            xsec_token:
            semaphore:

        Returns:
            Dict: note detail
        """
        note_detail = None
        utils.logger.info(f"[get_note_detail_async_task] Begin get note detail, note_id: {note_id}")
        async with semaphore:
            try:
                try:
                    note_detail = await self.xhs_client.get_note_by_id(note_id, xsec_source, xsec_token)
                except RetryError:
                    pass

                if not note_detail:
                    note_detail = await self.xhs_client.get_note_by_id_from_html(note_id, xsec_source, xsec_token,
                                                                                 enable_cookie=True)
                    if not note_detail:
                        # 跳过失败的笔记，继续执行其他任务
                        utils.logger.warning(f"[get_note_detail_async_task] Failed to get note detail, skipping Id: {note_id}")
                        return None

                note_detail.update({"xsec_token": xsec_token, "xsec_source": xsec_source})

                # Sleep after fetching note detail
                sleep_seconds = self._get_sleep_seconds()
                await asyncio.sleep(sleep_seconds)
                utils.logger.info(f"[get_note_detail_async_task] Sleeping for {sleep_seconds} seconds after fetching note {note_id}")

                # 随机执行假动作（模拟真实用户浏览其他内容）
                await self._maybe_do_fake_action()

                return note_detail

            except DataFetchError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def batch_get_note_comments(self, note_list: List[str], xsec_tokens: List[str]):
        """Batch get note comments"""
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Begin batch get note comments, note list: {note_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for index, note_id in enumerate(note_list):
            task = asyncio.create_task(
                self.get_comments(note_id=note_id, xsec_token=xsec_tokens[index], semaphore=semaphore),
                name=note_id,
            )
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(self, note_id: str, xsec_token: str, semaphore: asyncio.Semaphore):
        """Get note comments with keyword filtering and quantity limitation"""
        async with semaphore:
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Begin get note id comments {note_id}")
            try:
                # Use fixed crawling interval
                crawl_interval = self._get_sleep_seconds(for_comments=True)
                await self.xhs_client.get_note_all_comments(
                    note_id=note_id,
                    xsec_token=xsec_token,
                    crawl_interval=crawl_interval,
                    callback=xhs_store.batch_update_xhs_note_comments,
                    max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
                )

                # Sleep after fetching comments
                await asyncio.sleep(crawl_interval)
                utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Sleeping for {crawl_interval} seconds after fetching comments for note {note_id}")

                # 随机执行假动作
                await self._maybe_do_fake_action()
                
            except Exception as e:
                utils.logger.warning(f"[XiaoHongShuCrawler.get_comments] 获取评论失败 note_id={note_id}: {e}")

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create Xiaohongshu client"""
        utils.logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create Xiaohongshu API client ...")
        cookie_str, cookie_dict = utils.convert_cookies(await self.browser_context.cookies())
        xhs_client_obj = XiaoHongShuClient(
            proxy=httpx_proxy,
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "cache-control": "no-cache",
                "content-type": "application/json;charset=UTF-8",
                "origin": "https://www.xiaohongshu.com",
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": "https://www.xiaohongshu.com/",
                "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                "Cookie": cookie_str,
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
        )
        return xhs_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={
                    "width": 1920,
                    "height": 1080
                },
                user_agent=user_agent,
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser using CDP mode"""
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[XiaoHongShuCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[XiaoHongShuCrawler] CDP mode launch failed, falling back to standard mode: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self):
        """Close browser context"""
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")

    async def get_notice_media(self, note_detail: Dict):
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.info(f"[XiaoHongShuCrawler.get_notice_media] Crawling image mode is not enabled")
            return
        await self.get_note_images(note_detail)
        await self.get_notice_video(note_detail)

    async def get_note_images(self, note_item: Dict):
        """Get note images. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        note_id = note_item.get("note_id")
        image_list: List[Dict] = note_item.get("image_list", [])

        for img in image_list:
            if img.get("url_default") != "":
                img.update({"url": img.get("url_default")})

        if not image_list:
            return
        picNum = 0
        for pic in image_list:
            url = pic.get("url")
            if not url:
                continue
            content = await self.xhs_client.get_note_media(url)
            await asyncio.sleep(random.random())
            if content is None:
                continue
            extension_file_name = f"{picNum}.jpg"
            picNum += 1
            await xhs_store.update_xhs_note_image(note_id, content, extension_file_name)

    async def get_notice_video(self, note_item: Dict):
        """Get note videos. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        note_id = note_item.get("note_id")

        videos = xhs_store.get_video_url_arr(note_item)

        if not videos:
            return
        videoNum = 0
        for url in videos:
            content = await self.xhs_client.get_note_media(url)
            await asyncio.sleep(random.random())
            if content is None:
                continue
            extension_file_name = f"{videoNum}.mp4"
            videoNum += 1
            await xhs_store.update_xhs_note_video(note_id, content, extension_file_name)
