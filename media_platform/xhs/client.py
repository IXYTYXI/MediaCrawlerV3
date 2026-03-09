# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/client.py
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
import random
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union
from urllib.parse import urlencode

import httpx
from playwright.async_api import BrowserContext, Page
from tenacity import retry, stop_after_attempt, wait_fixed

import config
from base.base_crawler import AbstractApiClient
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import DataFetchError, IPBlockError, SessionExpiredError
from .field import SearchNoteType, SearchSortType
from .help import get_search_id
from .extractor import XiaoHongShuExtractor
from .playwright_sign import sign_with_playwright


class XiaoHongShuClient(AbstractApiClient, ProxyRefreshMixin):

    def __init__(
        self,
        timeout=60,  # If media crawling is enabled, Xiaohongshu long videos need longer timeout
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._host = "https://edith.xiaohongshu.com"
        self._domain = "https://www.xiaohongshu.com"
        self.IP_ERROR_STR = "Network connection error, please check network settings or restart"
        self.IP_ERROR_CODE = 300012
        self.NOTE_ABNORMAL_STR = "Note status abnormal, please check later"
        self.NOTE_ABNORMAL_CODE = -510001
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self._extractor = XiaoHongShuExtractor()
        # Initialize proxy pool (from ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)

    async def _pre_headers(self, url: str, params: Optional[Dict] = None, payload: Optional[Dict] = None) -> Dict:
        """Request header parameter signing (using playwright injection method)

        Args:
            url: Request URL
            params: GET request parameters
            payload: POST request parameters

        Returns:
            Dict: Signed request header parameters
        """
        a1_value = self.cookie_dict.get("a1", "")

        # Determine request data, method and URI
        if params is not None:
            data = params
            method = "GET"
        elif payload is not None:
            data = payload
            method = "POST"
        else:
            raise ValueError("params or payload is required")

        # Generate signature using playwright injection method
        signs = await sign_with_playwright(
            page=self.playwright_page,
            uri=url,
            data=data,
            a1=a1_value,
            method=method,
        )

        headers = {
            "X-S": signs["x-s"],
            "X-T": signs["x-t"],
            "x-S-Common": signs["x-s-common"],
            "X-B3-Traceid": signs["x-b3-traceid"],
        }
        self.headers.update(headers)
        return self.headers

    async def _handle_rate_limit(self):
        """处理访问频繁限流：渐进式等待 + 恢复后冷却期"""
        from tools.anti_crawl_utils import get_wait_manager
        import random

        if not hasattr(self, '_rate_limit_count'):
            self._rate_limit_count = 0
        self._rate_limit_count += 1
        self._success_after_rate_limit = 0

        # 通知动态等待管理器（使 get_multiplier 生效）
        get_wait_manager().record_failure(is_block=True)

        wait_minutes_map = {1: 8, 2: 14, 3: 21}
        if self._rate_limit_count <= 3:
            base_minutes = wait_minutes_map[self._rate_limit_count]
        else:
            base_minutes = min(21 + (self._rate_limit_count - 3) * 7, 60)

        jitter = random.uniform(-30, 30)
        wait_sec = base_minutes * 60 + jitter

        utils.logger.warning(
            f"[XiaoHongShuClient] 触发访问频繁限流 (第{self._rate_limit_count}次)，"
            f"等待 {wait_sec:.0f} 秒（约{wait_sec/60:.1f}分钟）后继续..."
        )
        await asyncio.sleep(wait_sec)

        # 设置恢复冷却期：限流后的请求需要显著降速，避免立即再次触发
        if self._rate_limit_count <= 1:
            self._post_recovery_multiplier = 2.5
            self._post_recovery_remaining = 10
        elif self._rate_limit_count <= 3:
            self._post_recovery_multiplier = 3.5
            self._post_recovery_remaining = 15
        else:
            self._post_recovery_multiplier = 4.5
            self._post_recovery_remaining = 20

        utils.logger.info(
            f"[XiaoHongShuClient] 限流等待结束（第{self._rate_limit_count}次），"
            f"进入冷却期：接下来 {self._post_recovery_remaining} 次请求使用 "
            f"{self._post_recovery_multiplier:.1f}x 间隔"
        )

    def _reset_rate_limit(self):
        """请求成功后渐进式降级限流计数 + 冷却期递减"""
        from tools.anti_crawl_utils import get_wait_manager
        get_wait_manager().record_success()

        # 冷却期递减：每次成功请求后逐步降低恢复乘数
        if hasattr(self, '_post_recovery_remaining') and self._post_recovery_remaining > 0:
            self._post_recovery_remaining -= 1
            if self._post_recovery_remaining > 0:
                total = self._post_recovery_remaining
                start_mult = getattr(self, '_post_recovery_multiplier', 1.0)
                self._post_recovery_multiplier = max(1.2, start_mult - 0.15)
            else:
                self._post_recovery_multiplier = 1.0
                utils.logger.info("[XiaoHongShuClient] 冷却期结束，恢复正常爬取速度")

        if not hasattr(self, '_rate_limit_count') or self._rate_limit_count <= 0:
            return
        if not hasattr(self, '_success_after_rate_limit'):
            self._success_after_rate_limit = 0
        self._success_after_rate_limit += 1

        step_down_threshold = 5
        if self._success_after_rate_limit >= step_down_threshold:
            old = self._rate_limit_count
            self._rate_limit_count = max(0, self._rate_limit_count - 1)
            self._success_after_rate_limit = 0
            if self._rate_limit_count > 0:
                utils.logger.info(
                    f"[XiaoHongShuClient] 连续 {step_down_threshold} 次成功，"
                    f"限流等级 {old} → {self._rate_limit_count}"
                )
            else:
                utils.logger.info(
                    f"[XiaoHongShuClient] 限流计数已清零（连续 {step_down_threshold} 次成功）"
                )

    def get_recovery_multiplier(self) -> float:
        """获取限流恢复冷却期的等待时间乘数（1.0 = 正常速度）"""
        if hasattr(self, '_post_recovery_remaining') and self._post_recovery_remaining > 0:
            return getattr(self, '_post_recovery_multiplier', 1.0)
        return 1.0

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    async def request(self, method, url, **kwargs) -> Union[str, Any]:
        """
        Wrapper for httpx common request method, processes request response
        Args:
            method: Request method
            url: Request URL
            **kwargs: Other request parameters, such as headers, body, etc.

        Returns:

        """
        # Check if proxy is expired before each request
        await self._refresh_proxy_if_expired()

        # return response.text
        return_response = kwargs.pop("return_response", False)
        async with httpx.AsyncClient(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)

        if response.status_code == 471 or response.status_code == 461:
            verify_type = response.headers.get("Verifytype", "unknown")
            verify_uuid = response.headers.get("Verifyuuid", "unknown")
            resp_text = response.text[:200]
            msg = f"CAPTCHA appeared, request failed, Verifytype: {verify_type}, Verifyuuid: {verify_uuid}, Response: {resp_text}"
            utils.logger.error(msg)

            # 检测"访问频繁"类限流，递增等待后重试
            if "300013" in resp_text or "访问频繁" in resp_text or "300012" in resp_text:
                await self._handle_rate_limit()

            raise DataFetchError(msg)

        if return_response:
            return response.text
        data: Dict = response.json()

        # 访问频繁 / 反爬限流（code=300013 等）
        rate_limit_codes = {300013, 300012, 300014}
        if data.get("code") in rate_limit_codes:
            msg = data.get("msg", "访问频繁")
            await self._handle_rate_limit()
            raise DataFetchError(f"Rate limited: {msg}")

        if data["success"]:
            self._reset_rate_limit()
            return data.get("data", data.get("success", {}))
        elif data["code"] == self.IP_ERROR_CODE:
            raise IPBlockError(self.IP_ERROR_STR)
        else:
            err_msg = data.get("msg", None) or f"{response.text}"
            raise DataFetchError(err_msg)

    async def get(self, uri: str, params: Optional[Dict] = None) -> Dict:
        """
        GET request, signs request headers
        Args:
            uri: Request route
            params: Request parameters

        Returns:

        """
        headers = await self._pre_headers(uri, params)
        full_url = f"{self._host}{uri}"

        return await self.request(
            method="GET", url=full_url, headers=headers, params=params
        )

    async def post(self, uri: str, data: dict, **kwargs) -> Dict:
        """
        POST request, signs request headers
        Args:
            uri: Request route
            data: Request body parameters

        Returns:

        """
        headers = await self._pre_headers(uri, payload=data)
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return await self.request(
            method="POST",
            url=f"{self._host}{uri}",
            data=json_str,
            headers=headers,
            **kwargs,
        )

    async def get_note_media(self, url: str) -> Union[bytes, None]:
        # Check if proxy is expired before request
        await self._refresh_proxy_if_expired()

        # 小红书 CDN 需要 Referer 头，否则可能 403 Forbidden
        headers = {
            "Referer": "https://www.xiaohongshu.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        async with httpx.AsyncClient(proxy=self.proxy) as client:
            try:
                response = await client.request("GET", url, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                if not response.reason_phrase == "OK":
                    utils.logger.error(
                        f"[XiaoHongShuClient.get_note_media] request {url} err, res:{response.text}"
                    )
                    return None
                else:
                    return response.content
            except (
                httpx.HTTPError
            ) as exc:  # some wrong when call httpx.request method, such as connection error, client error, server error or response status code is not 2xx
                utils.logger.error(
                    f"[XiaoHongShuClient.get_aweme_media] {exc.__class__.__name__} for {exc.request.url} - {exc}"
                )  # Keep original exception type name for developer debugging
                return None

    async def pong(self) -> bool:
        """
        Check if login state is still valid
        Returns:

        """
        """get a note to check if login state is ok"""
        utils.logger.info("[XiaoHongShuClient.pong] Begin to pong xhs...")
        ping_flag = False
        try:
            note_card: Dict = await self.get_note_by_keyword(keyword="Xiaohongshu")
            if note_card.get("items"):
                ping_flag = True
        except Exception as e:
            utils.logger.error(
                f"[XiaoHongShuClient.pong] Ping xhs failed: {e}, and try to login again..."
            )
            ping_flag = False
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext):
        """
        Update cookies method provided by API client, usually called after successful login
        Args:
            browser_context: Browser context object

        Returns:

        """
        cookie_str, cookie_dict = utils.convert_cookies(await browser_context.cookies())
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def get_note_by_keyword(
        self,
        keyword: str,
        search_id: str = get_search_id(),
        page: int = 1,
        page_size: int = 20,
        sort: SearchSortType = SearchSortType.GENERAL,
        note_type: SearchNoteType = SearchNoteType.ALL,
    ) -> Dict:
        """
        Search notes by keyword
        Args:
            keyword: Keyword parameter
            page: Page number
            page_size: Page data length
            sort: Search result sorting specification
            note_type: Type of note to search

        Returns:

        """
        uri = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": search_id,
            "sort": sort.value,
            "note_type": note_type.value,
        }
        return await self.post(uri, data)

    async def get_note_by_id(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
    ) -> Dict:
        """
        Get note detail API
        Args:
            note_id: Note ID
            xsec_source: Channel source
            xsec_token: Token returned from search keyword result list

        Returns:

        """
        if xsec_source == "":
            xsec_source = "pc_search"

        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": xsec_source,
            "xsec_token": xsec_token,
        }
        uri = "/api/sns/web/v1/feed"
        res = await self.post(uri, data)
        if res and res.get("items"):
            res_dict: Dict = res["items"][0]["note_card"]
            return res_dict
        # When crawling frequently, some notes may have results while others don't
        utils.logger.error(
            f"[XiaoHongShuClient.get_note_by_id] get note id:{note_id} empty and res:{res}"
        )
        return dict()

    async def get_note_comments(
        self,
        note_id: str,
        xsec_token: str,
        cursor: str = "",
    ) -> Dict:
        """
        Get first-level comments API
        Args:
            note_id: Note ID
            xsec_token: Verification token
            cursor: Pagination cursor

        Returns:

        """
        uri = "/api/sns/web/v2/comment/page"
        params = {
            "note_id": note_id,
            "cursor": cursor,
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
        }
        return await self.get(uri, params)

    async def get_note_sub_comments(
        self,
        note_id: str,
        root_comment_id: str,
        xsec_token: str,
        num: int = 10,
        cursor: str = "",
    ):
        """
        Get sub-comments under specified parent comment API
        Args:
            note_id: Post ID of sub-comments
            root_comment_id: Root comment ID
            xsec_token: Verification token
            num: Pagination quantity
            cursor: Pagination cursor

        Returns:

        """
        uri = "/api/sns/web/v2/comment/sub/page"
        params = {
            "note_id": note_id,
            "root_comment_id": root_comment_id,
            "num": str(num),
            "cursor": cursor,
            "image_formats": "jpg,webp,avif",
            "top_comment_id": "",
            "xsec_token": xsec_token,
        }
        return await self.get(uri, params)

    async def get_note_all_comments(
        self,
        note_id: str,
        xsec_token: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ) -> List[Dict]:
        """
        Get all first-level comments under specified note, this method will continuously find all comment information under a post
        Args:
            note_id: Note ID
            xsec_token: Verification token
            crawl_interval: Crawl delay per note (seconds)
            callback: Callback after one note crawl ends
            max_count: Maximum number of comments to crawl per note
        Returns:

        """
        result = []
        comments_has_more = True
        comments_cursor = ""
        while comments_has_more and len(result) < max_count:
            comments_res = await self.get_note_comments(
                note_id=note_id, xsec_token=xsec_token, cursor=comments_cursor
            )
            comments_has_more = comments_res.get("has_more", False)
            comments_cursor = comments_res.get("cursor", "")
            if "comments" not in comments_res:
                utils.logger.info(
                    f"[XiaoHongShuClient.get_note_all_comments] No 'comments' key found in response: {comments_res}"
                )
                break
            comments = comments_res["comments"]
            if len(result) + len(comments) > max_count:
                comments = comments[: max_count - len(result)]
            if callback:
                await callback(note_id, comments)
            # Dynamic random sleep for each request
            actual_sleep = crawl_interval + random.uniform(0, crawl_interval * 0.5) if crawl_interval > 0 else random.uniform(0.3, 0.8)
            await asyncio.sleep(actual_sleep)
            result.extend(comments)
            
            # Batch pause strategy for comments (随机化批次阈值)
            if getattr(config, "BATCH_PAUSE_ENABLED", False):
                min_n = getattr(config, "BATCH_PAUSE_EVERY_N_MIN", 8)
                max_n = getattr(config, "BATCH_PAUSE_EVERY_N_MAX", 12)
                batch_n = random.randint(min_n, max_n)
                if len(result) > 0 and len(result) % batch_n == 0:
                    pause_min = getattr(config, "BATCH_PAUSE_MIN_SEC", 30.0)
                    pause_max = getattr(config, "BATCH_PAUSE_MAX_SEC", 60.0)
                    from tools.anti_crawl_utils import generate_random_wait
                    pause_time = generate_random_wait(pause_min, pause_max, "lognormal")
                    utils.logger.info(f"[XiaoHongShuClient] 评论批次暂停: 等待 {pause_time:.1f}s (已爬取 {len(result)} 条评论)")
                    await asyncio.sleep(pause_time)
            
            sub_comments = await self.get_comments_all_sub_comments(
                comments=comments,
                xsec_token=xsec_token,
                crawl_interval=crawl_interval,
                callback=callback,
            )
            result.extend(sub_comments)
        return result

    async def get_comments_all_sub_comments(
        self,
        comments: List[Dict],
        xsec_token: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Get all second-level comments under specified first-level comments, this method will continuously find all second-level comment information under first-level comments
        Args:
            comments: Comment list
            xsec_token: Verification token
            crawl_interval: Crawl delay per comment (seconds)
            callback: Callback after one comment crawl ends

        Returns:

        """
        if not config.ENABLE_GET_SUB_COMMENTS:
            utils.logger.info(
                f"[XiaoHongShuCrawler.get_comments_all_sub_comments] Crawling sub_comment mode is not enabled"
            )
            return []

        max_sub = getattr(config, "CRAWLER_MAX_SUB_COMMENTS_PER_COMMENT", 0)
        result = []
        for comment in comments:
            note_id = comment.get("note_id")
            sub_comments = comment.get("sub_comments")
            if sub_comments and callback:
                await callback(note_id, sub_comments)

            sub_comment_has_more = comment.get("sub_comment_has_more")
            if not sub_comment_has_more:
                continue

            root_comment_id = comment.get("id")
            sub_comment_cursor = comment.get("sub_comment_cursor")
            sub_count_for_this_comment = len(sub_comments) if sub_comments else 0

            while sub_comment_has_more:
                if max_sub > 0 and sub_count_for_this_comment >= max_sub:
                    utils.logger.info(
                        f"[XiaoHongShuClient.get_comments_all_sub_comments] "
                        f"Reached sub-comment limit ({max_sub}) for comment {root_comment_id}"
                    )
                    break

                comments_res = await self.get_note_sub_comments(
                    note_id=note_id,
                    root_comment_id=root_comment_id,
                    xsec_token=xsec_token,
                    num=10,
                    cursor=sub_comment_cursor,
                )

                if comments_res is None:
                    utils.logger.info(
                        f"[XiaoHongShuClient.get_comments_all_sub_comments] No response found for note_id: {note_id}"
                    )
                    continue
                sub_comment_has_more = comments_res.get("has_more", False)
                sub_comment_cursor = comments_res.get("cursor", "")
                if "comments" not in comments_res:
                    utils.logger.info(
                        f"[XiaoHongShuClient.get_comments_all_sub_comments] No 'comments' key found in response: {comments_res}"
                    )
                    break
                fetched = comments_res["comments"]
                if max_sub > 0 and sub_count_for_this_comment + len(fetched) > max_sub:
                    fetched = fetched[: max_sub - sub_count_for_this_comment]
                    sub_comment_has_more = False
                if callback:
                    await callback(note_id, fetched)
                actual_sleep = crawl_interval + random.uniform(0, crawl_interval * 0.5) if crawl_interval > 0 else random.uniform(0.3, 0.8)
                await asyncio.sleep(actual_sleep)
                result.extend(fetched)
                sub_count_for_this_comment += len(fetched)
        return result

    async def get_creator_info(
        self, user_id: str, xsec_token: str = "", xsec_source: str = ""
    ) -> Dict:
        """
        Get user profile brief information by parsing user homepage HTML
        The PC user homepage has window.__INITIAL_STATE__ variable, just parse it

        Args:
            user_id: User ID
            xsec_token: Verification token (optional, pass if included in URL)
            xsec_source: Channel source (optional, pass if included in URL)

        Returns:
            Dict: Creator information
        """
        # Build URI, add xsec parameters to URL if available
        uri = f"/user/profile/{user_id}"
        if xsec_token and xsec_source:
            uri = f"{uri}?xsec_token={xsec_token}&xsec_source={xsec_source}"

        html_content = await self.request(
            "GET", self._domain + uri, return_response=True, headers=self.headers
        )
        return self._extractor.extract_creator_info_from_html(html_content)

    async def get_notes_by_creator(
        self,
        creator: str,
        cursor: str,
        page_size: int = 30,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> Dict:
        """
        Get creator's notes
        Args:
            creator: Creator ID
            cursor: Last note ID from previous page
            page_size: Page data length
            xsec_token: Verification token
            xsec_source: Channel source

        Returns:

        """
        uri = f"/api/sns/web/v1/user_posted"
        params = {
            "num": page_size,
            "cursor": cursor,
            "user_id": creator,
            "xsec_token": xsec_token,
            "xsec_source": xsec_source,
        }
        return await self.get(uri, params)

    async def get_all_notes_by_creator(
        self,
        user_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
        resume_cursor: str = "",
        resume_enumerated: int = 0,
    ) -> List[Dict]:
        """
        Get all posts published by specified user, this method will continuously find all post information under a user
        Args:
            user_id: User ID
            crawl_interval: Crawl delay (seconds)
            callback: Update callback function after one pagination crawl ends
            xsec_token: Verification token
            xsec_source: Channel source
            resume_cursor: Saved pagination cursor to resume from (skip already-enumerated pages)
            resume_enumerated: Number of notes already enumerated in previous runs

        Returns:

        """
        result = []
        notes_has_more = True
        notes_cursor = ""
        consecutive_errors = 0
        max_consecutive_errors = 3
        batch_count = 0          # 批次计数（用于 pong 探活）
        pong_every_n_batches = 3  # 每 N 个批次做 1 次 pong 探活
        total_enumerated = 0     # 已枚举总数（含恢复的）

        if resume_cursor:
            notes_cursor = resume_cursor
            total_enumerated = resume_enumerated
            utils.logger.info(
                f"[XiaoHongShuClient.get_all_notes_by_creator] "
                f"从游标恢复分页，跳过已枚举的 {resume_enumerated} 条"
            )
        
        while notes_has_more and (total_enumerated + len(result)) < config.CRAWLER_MAX_NOTES_COUNT:
            # ========== 第 4 层：每 N 批次 pong() 探活 ==========
            batch_count += 1
            if batch_count > 1 and batch_count % pong_every_n_batches == 0:
                try:
                    pong_ok = await self.pong()
                    if not pong_ok:
                        msg = (
                            f"pong() 探活失败（第 {batch_count} 批次），"
                            f"session 可能已失效，已获取 {len(result)} 条"
                        )
                        utils.logger.error(
                            f"[XiaoHongShuClient.get_all_notes_by_creator] ⚡ {msg}"
                        )
                        raise SessionExpiredError(msg)
                    else:
                        utils.logger.info(
                            f"[XiaoHongShuClient.get_all_notes_by_creator] "
                            f"pong() 探活成功（第 {batch_count} 批次）"
                        )
                except SessionExpiredError:
                    raise
                except Exception as e:
                    utils.logger.warning(
                        f"[XiaoHongShuClient.get_all_notes_by_creator] "
                        f"pong() 探活异常: {e}，继续爬取"
                    )
            
            try:
                notes_res = await self.get_notes_by_creator(
                    user_id, notes_cursor, xsec_token=xsec_token, xsec_source=xsec_source
                )
                consecutive_errors = 0  # 成功后重置错误计数
                
            except SessionExpiredError:
                # SessionExpiredError 直接向上抛，不被吞掉
                raise
            except Exception as e:
                consecutive_errors += 1
                utils.logger.error(
                    f"[XiaoHongShuClient.get_all_notes_by_creator] 获取作品列表出错 ({consecutive_errors}/{max_consecutive_errors}): {e}"
                )
                
                if consecutive_errors >= max_consecutive_errors:
                    utils.logger.warning(
                        f"[XiaoHongShuClient.get_all_notes_by_creator] 连续错误达到上限，停止获取。已获取 {len(result)} 条"
                    )
                    break
                
                # 遇到错误后等待更长时间再重试
                wait_time = 60 * consecutive_errors  # 第1次60秒，第2次120秒
                utils.logger.info(f"[XiaoHongShuClient] 等待 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
                continue
                
            if not notes_res:
                utils.logger.error(
                    f"[XiaoHongShuClient.get_notes_by_creator] The current creator may have been banned by xhs, so they cannot access the data."
                )
                break

            notes_has_more = notes_res.get("has_more", False)
            notes_cursor = notes_res.get("cursor", "")
            self._last_pagination_cursor = notes_cursor
            if "notes" not in notes_res:
                utils.logger.info(
                    f"[XiaoHongShuClient.get_all_notes_by_creator] No 'notes' key found in response: {notes_res}"
                )
                break

            notes = notes_res["notes"]
            utils.logger.info(
                f"[XiaoHongShuClient.get_all_notes_by_creator] got user_id:{user_id} notes len : {len(notes)}"
            )

            remaining = config.CRAWLER_MAX_NOTES_COUNT - total_enumerated - len(result)
            if remaining <= 0:
                break

            notes_to_add = notes[:remaining]
            if callback:
                await callback(notes_to_add)

                # 检查回调是否请求提前停止（日期超出范围）
                cb_self = getattr(callback, '__self__', None)
                if cb_self and getattr(cb_self, '_early_stop_requested', False):
                    utils.logger.info(
                        f"[XiaoHongShuClient.get_all_notes_by_creator] "
                        f"回调请求提前停止（连续批次日期超出范围），已获取 {len(result) + len(notes_to_add)} 条"
                    )
                    result.extend(notes_to_add)
                    break

            result.extend(notes_to_add)
            # Dynamic random sleep for each request
            actual_sleep = crawl_interval + random.uniform(0, crawl_interval * 0.5) if crawl_interval > 0 else random.uniform(0.5, 1.5)
            await asyncio.sleep(actual_sleep)
            
            # Batch pause strategy: pause longer every N items (N is randomized)
            if getattr(config, "BATCH_PAUSE_ENABLED", False):
                # 随机化批次阈值
                min_n = getattr(config, "BATCH_PAUSE_EVERY_N_MIN", 8)
                max_n = getattr(config, "BATCH_PAUSE_EVERY_N_MAX", 12)
                batch_n = random.randint(min_n, max_n)
                if len(result) > 0 and len(result) % batch_n == 0:
                    pause_min = getattr(config, "BATCH_PAUSE_MIN_SEC", 30.0)
                    pause_max = getattr(config, "BATCH_PAUSE_MAX_SEC", 60.0)
                    # 使用对数正态分布
                    from tools.anti_crawl_utils import generate_random_wait
                    pause_time = generate_random_wait(pause_min, pause_max, "lognormal")
                    utils.logger.info(f"[XiaoHongShuClient] 批次暂停: 等待 {pause_time:.1f}s (已爬取 {len(result)} 条)")
                    await asyncio.sleep(pause_time)

            # Big pause strategy: long pause every N items
            if getattr(config, "BIG_PAUSE_ENABLED", False):
                big_n = getattr(config, "BIG_PAUSE_EVERY_N", 300)
                if len(result) > 0 and len(result) % big_n == 0:
                    big_min = getattr(config, "BIG_PAUSE_MIN_SEC", 240.0)
                    big_max = getattr(config, "BIG_PAUSE_MAX_SEC", 360.0)
                    from tools.anti_crawl_utils import generate_random_wait
                    big_pause_time = generate_random_wait(big_min, big_max, "uniform")
                    utils.logger.info(
                        f"[XiaoHongShuClient] 大暂停: 已爬取 {len(result)} 条，"
                        f"休息 {big_pause_time:.0f}s ({big_pause_time/60:.1f}分钟)"
                    )
                    await asyncio.sleep(big_pause_time)

        utils.logger.info(
            f"[XiaoHongShuClient.get_all_notes_by_creator] Finished getting notes for user {user_id}, total: {len(result)}"
        )
        return result

    async def get_note_short_url(self, note_id: str) -> Dict:
        """
        Get note short URL
        Args:
            note_id: Note ID

        Returns:

        """
        uri = f"/api/sns/web/short_url"
        data = {"original_url": f"{self._domain}/discovery/item/{note_id}"}
        return await self.post(uri, data=data, return_response=True)

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    async def get_note_by_id_from_html(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        enable_cookie: bool = False,
    ) -> Optional[Dict]:
        """
        Get note details by parsing note detail page HTML, this interface may fail, retry 3 times here
        copy from https://github.com/ReaJason/xhs/blob/eb1c5a0213f6fbb592f0a2897ee552847c69ea2d/xhs/core.py#L217-L259
        thanks for ReaJason
        Args:
            note_id:
            xsec_source:
            xsec_token:
            enable_cookie:

        Returns:

        """
        url = (
            "https://www.xiaohongshu.com/explore/"
            + note_id
            + f"?xsec_token={xsec_token}&xsec_source={xsec_source}"
        )
        copy_headers = self.headers.copy()
        if not enable_cookie:
            del copy_headers["Cookie"]

        html = await self.request(
            method="GET", url=url, return_response=True, headers=copy_headers
        )

        return self._extractor.extract_note_detail_from_html(note_id, html)
