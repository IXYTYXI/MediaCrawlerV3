# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/main.py
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

import os
import sys
import io

# Force UTF-8 encoding for stdout/stderr to prevent encoding errors
# when outputting Chinese characters in non-UTF-8 terminals
if sys.stdout and hasattr(sys.stdout, 'buffer'):
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'buffer'):
    if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import asyncio
import time
from typing import Optional, Type, Tuple

import cmd_arg
import config
from config.anti_crawl_loader import apply_anti_crawl_config
from database import db
from tools.crawl_statistics import reset_statistics, get_statistics
from base.base_crawler import AbstractCrawler
from media_platform.bilibili import BilibiliCrawler
from media_platform.douyin import DouYinCrawler
from media_platform.kuaishou import KuaishouCrawler
from media_platform.tieba import TieBaCrawler
from media_platform.weibo import WeiboCrawler
from media_platform.xhs import XiaoHongShuCrawler
from media_platform.xhs.exception import SessionExpiredError
from tenacity import RetryError
from media_platform.zhihu import ZhihuCrawler
from tools.async_file_writer import AsyncFileWriter
from var import crawler_type_var


class CrawlerFactory:
    CRAWLERS: dict[str, Type[AbstractCrawler]] = {
        "xhs": XiaoHongShuCrawler,
        "dy": DouYinCrawler,
        "ks": KuaishouCrawler,
        "bili": BilibiliCrawler,
        "wb": WeiboCrawler,
        "tieba": TieBaCrawler,
        "zhihu": ZhihuCrawler,
    }

    @staticmethod
    def create_crawler(platform: str) -> AbstractCrawler:
        crawler_class = CrawlerFactory.CRAWLERS.get(platform)
        if not crawler_class:
            supported = ", ".join(sorted(CrawlerFactory.CRAWLERS))
            raise ValueError(f"Invalid media platform: {platform!r}. Supported: {supported}")
        return crawler_class()


crawler: Optional[AbstractCrawler] = None


def _flush_excel_if_needed() -> None:
    if config.SAVE_DATA_OPTION != "excel":
        return

    try:
        from store.excel_store_base import ExcelStoreBase

        ExcelStoreBase.flush_all()
        print("[Main] Excel files saved successfully")
    except Exception as e:
        print(f"[Main] Error flushing Excel data: {e}")


async def _generate_wordcloud_if_needed() -> None:
    if config.SAVE_DATA_OPTION != "json" or not config.ENABLE_GET_WORDCLOUD:
        return

    try:
        file_writer = AsyncFileWriter(
            platform=config.PLATFORM,
            crawler_type=crawler_type_var.get(),
        )
        await file_writer.generate_wordcloud_from_comments()
    except Exception as e:
        print(f"[Main] Error generating wordcloud: {e}")


def _push_search_results_to_feishu() -> Tuple[Optional[str], int]:
    """搜索模式爬完后，读取本次生成的 JSON 数据，写入飞书多维表格。返回 (多维表格链接, 笔记数)。"""
    import json
    import os

    cfg_path = os.path.join("config", "anti_crawl_config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            acfg = json.load(f)
    except Exception:
        acfg = {}

    feishu_cfg = acfg.get("feishu", {})
    if not feishu_cfg.get("enabled"):
        print("[Main] 飞书未启用，跳过写入", flush=True)
        return (None, 0)

    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    folder_token = (
        getattr(config, "FEISHU_FOLDER_TOKEN_OVERRIDE", "")
        or feishu_cfg.get("search_folder_token")
        or feishu_cfg.get("folder_token", "")
    )
    if not app_id or not app_secret:
        print("[Main] 飞书 app_id/app_secret 未配置，跳过写入", flush=True)
        return (None, 0)

    from tools.async_file_writer import AsyncFileWriter
    session_ts = AsyncFileWriter.get_session_timestamp()
    json_dir = f"data/{config.PLATFORM}/json"
    prefix = "search_top_contents" if config.CRAWLER_TYPE == "search_top" else "search_contents"
    comments_prefix = "search_top_comments" if config.CRAWLER_TYPE == "search_top" else "search_comments"
    notes_file = os.path.join(json_dir, f"{prefix}_{session_ts}.json")
    comments_file = os.path.join(json_dir, f"{comments_prefix}_{session_ts}.json")

    notes = []
    if os.path.exists(notes_file):
        with open(notes_file, "r", encoding="utf-8") as f:
            notes = json.load(f)
    if not notes:
        print("[Main] 无笔记数据，跳过飞书写入", flush=True)
        return (None, 0)

    comments_by_note = {}
    if os.path.exists(comments_file):
        with open(comments_file, "r", encoding="utf-8") as f:
            raw_comments = json.load(f)
        for group in raw_comments:
            nid = group.get("note_id", "")
            clist = group.get("comments", [])
            if nid and clist:
                comments_by_note[nid] = clist

    print(f"[Main] 开始写入飞书: {len(notes)} 条笔记, "
          f"{sum(len(v) for v in comments_by_note.values())} 条评论", flush=True)

    try:
        from tools.pipeline_feishu_writer import PipelineFeishuWriter
        with PipelineFeishuWriter(
            feishu_app_id=app_id,
            feishu_app_secret=app_secret,
            bitable_name=f"搜索爬取_{session_ts}",
            folder_token=folder_token,
        ) as writer:
            writer.write_search_results(
                notes=notes,
                comments_by_note=comments_by_note,
            )
        bitable_url = getattr(writer, "bitable_url", None) or ""
        print(f"[Main] 飞书写入完成: {bitable_url}", flush=True)
        if bitable_url:
            print(f"多维表格链接: {bitable_url}", flush=True)
        return (bitable_url or None, len(notes))
    except Exception as e:
        print(f"[Main] 飞书写入失败: {e}", flush=True)
        return (None, 0)


async def main() -> None:
    global crawler
    _crawl_start_time = time.time()

    # 先加载配置文件作为默认值，再解析命令行参数（CLI 参数优先级更高）
    apply_anti_crawl_config(config)

    args = await cmd_arg.parse_cmd()
    if args.init_db:
        await db.init_db(args.init_db)
        print(f"Database {args.init_db} initialized successfully.")
        return

    # 搜索模式：若未传关键词（为空或仍是 base 默认），则从关键词池加载
    if config.CRAWLER_TYPE in ("search", "search_top"):
        from tools.search_keyword_pool import load_search_keyword_pool
        pool_keywords = load_search_keyword_pool()
        if pool_keywords:
            base_default = "编程副业,编程兼职"
            current = (config.KEYWORDS or "").strip()
            if not current or current == base_default:
                config.KEYWORDS = pool_keywords

    # 重置文件写入器的会话时间戳，确保新运行生成新文件
    from tools.async_file_writer import AsyncFileWriter
    AsyncFileWriter.reset_session_timestamp()

    # 重置统计（新的爬取任务）
    reset_statistics(platform=config.PLATFORM)

    crawler = CrawlerFactory.create_crawler(platform=config.PLATFORM)
    crawl_exit_code = 0
    try:
        await crawler.start()
    except SessionExpiredError as e:
        print("\n[MediaCrawler] 登录已过期或会话失效，请刷新 session 后点击重试。", file=sys.stderr)
        print(f"  详情: {e}", file=sys.stderr)
        crawl_exit_code = 1
    except RetryError as e:
        last_exc = None
        if getattr(e, "last_attempt", None) is not None:
            att = e.last_attempt
            if hasattr(att, "exception") and callable(getattr(att, "exception", None)):
                last_exc = att.exception()
            if last_exc is None and hasattr(att, "exception_info") and callable(getattr(att, "exception_info", None)):
                info = att.exception_info()
                last_exc = info[1] if len(info) > 1 else info[0]
        if isinstance(last_exc, SessionExpiredError):
            print("\n[MediaCrawler] 登录已过期或会话失效，请刷新 session 后点击重试。", file=sys.stderr)
            print(f"  详情: {last_exc}", file=sys.stderr)
            crawl_exit_code = 1
        else:
            crawl_exit_code = 1
            print(f"\n[MediaCrawler] 重试耗尽: {e}", file=sys.stderr)
    except Exception as e:
        crawl_exit_code = 1
        print(f"\n[MediaCrawler] 爬虫异常: {e}", file=sys.stderr)
    finally:
        _flush_excel_if_needed()
        await _generate_wordcloud_if_needed()

        if config.CRAWLER_TYPE in ("search", "search_top") and config.SAVE_DATA_OPTION == "json":
            feishu_url, notes_count = _push_search_results_to_feishu()
            # 任务完成飞书群通知（与批量爬取一致，读取 notification 配置）
            try:
                import json as _json
                _notify_cfg_path = os.path.join("config", "anti_crawl_config.json")
                if os.path.exists(_notify_cfg_path):
                    with open(_notify_cfg_path, "r", encoding="utf-8") as _nf:
                        _notify_cfg = dict(_json.load(_nf).get("notification", {}))
                    # 任务级覆盖：单个任务可指定通知群聊 ID 或 Webhook，实现分任务管理
                    _chat_override = getattr(config, "NOTIFICATION_CHAT_ID_OVERRIDE", "") or ""
                    _webhook_override = getattr(config, "NOTIFICATION_WEBHOOK_URL_OVERRIDE", "") or ""
                    if _chat_override:
                        _notify_cfg["chat_id"] = _chat_override
                        print(f"[Main] 使用任务指定群聊: {_chat_override[:20]}...", flush=True)
                    if _webhook_override:
                        _notify_cfg["webhook_url"] = _webhook_override
                        print(f"[Main] 使用任务指定 Webhook 发送通知", flush=True)
                    # 任务指定了群聊但未指定 Webhook 时，清空全局 Webhook，确保发到任务群
                    if _chat_override and not _webhook_override:
                        _notify_cfg["webhook_url"] = ""
                    if not _chat_override and not _webhook_override and _notify_cfg.get("chat_id"):
                        print(f"[Main] 使用全局配置群聊: {_notify_cfg.get('chat_id', '')[:20]}...", flush=True)
                    # 任务级填了群聊或 Webhook 时，视为本任务要发通知（即使全局未开启）
                    _will_send = (_notify_cfg.get("webhook_url") or _notify_cfg.get("chat_id")) and (
                        _notify_cfg.get("enabled", False) or _chat_override or _webhook_override
                    )
                    if _will_send:
                        from tools.feishu_notify import send_notification
                        _elapsed = int(time.time() - _crawl_start_time)
                        send_notification(
                            task_id="关键词搜索",
                            crawl_mode="full",
                            total=notes_count,
                            success=notes_count,
                            failed=0,
                            skipped=0,
                            reused=0,
                            elapsed_seconds=_elapsed,
                            bitable_url=feishu_url or "",
                            extra_info=f"平台={config.PLATFORM}，共 {notes_count} 条笔记",
                            notify_config=_notify_cfg,
                        )
            except Exception as _ne:
                print(f"[Main] 飞书通知发送失败: {_ne}", flush=True)

        try:
            stats = get_statistics()
            if stats.notes_data:
                stats.save_summary(output_dir="data")
        except Exception as e:
            print(f"[Main] Error generating statistics: {e}")

    if crawl_exit_code != 0:
        sys.exit(crawl_exit_code)


async def async_cleanup() -> None:
    global crawler
    if crawler:
        if getattr(crawler, "cdp_manager", None):
            try:
                # 使用 force=False，让 AUTO_CLOSE_BROWSER 配置生效
                await crawler.cdp_manager.cleanup(force=False)
            except Exception as e:
                error_msg = str(e).lower()
                if "closed" not in error_msg and "disconnected" not in error_msg:
                    print(f"[Main] Error cleaning up CDP browser: {e}")

        elif getattr(crawler, "browser_context", None):
            try:
                await crawler.browser_context.close()
            except Exception as e:
                error_msg = str(e).lower()
                if "closed" not in error_msg and "disconnected" not in error_msg:
                    print(f"[Main] Error closing browser context: {e}")

    if config.SAVE_DATA_OPTION in ("db", "sqlite"):
        await db.close()

if __name__ == "__main__":
    from tools.app_runner import run

    def _force_stop() -> None:
        c = crawler
        if not c:
            return
        cdp_manager = getattr(c, "cdp_manager", None)
        launcher = getattr(cdp_manager, "launcher", None)
        if not launcher:
            return
        try:
            launcher.cleanup()
        except Exception:
            pass

    run(main, async_cleanup, cleanup_timeout_seconds=15.0, on_first_interrupt=_force_stop)
