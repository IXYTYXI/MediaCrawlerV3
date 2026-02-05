# -*- coding: utf-8 -*-
"""
反反爬策略配置加载器
从 anti_crawl_config.json 读取配置并应用到全局 config
"""
import json
import os
from typing import Dict, Any

from tools import utils

# 配置文件路径
ANTI_CRAWL_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "anti_crawl_config.json")


def load_anti_crawl_config() -> Dict[str, Any]:
    """
    从 JSON 文件加载反反爬配置
    """
    if not os.path.exists(ANTI_CRAWL_CONFIG_PATH):
        utils.logger.warning(f"[AntiCrawlLoader] Config file not found: {ANTI_CRAWL_CONFIG_PATH}")
        return {}
    
    try:
        with open(ANTI_CRAWL_CONFIG_PATH, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        utils.logger.info(f"[AntiCrawlLoader] Loaded anti-crawl config from {ANTI_CRAWL_CONFIG_PATH}")
        return config_data
    except json.JSONDecodeError as e:
        utils.logger.error(f"[AntiCrawlLoader] Failed to parse config file: {e}")
        return {}
    except Exception as e:
        utils.logger.error(f"[AntiCrawlLoader] Failed to load config file: {e}")
        return {}


def apply_anti_crawl_config(target_config) -> None:
    """
    将反反爬配置应用到目标 config 模块
    
    Args:
        target_config: config 模块对象
    """
    config_data = load_anti_crawl_config()
    if not config_data:
        utils.logger.warning("[AntiCrawlLoader] No config loaded, using defaults")
        return
    
    # ==================== 随机等待策略 ====================
    random_sleep = config_data.get("随机等待策略", {})
    if random_sleep:
        target_config.RANDOM_SLEEP_ENABLED = random_sleep.get("enabled", True)
        target_config.RANDOM_SLEEP_MIN_SEC = random_sleep.get("min_sec", 5.0)
        target_config.RANDOM_SLEEP_MAX_SEC = random_sleep.get("max_sec", 10.0)
        target_config.RANDOM_SLEEP_DISTRIBUTION = random_sleep.get("distribution", "lognormal")
    
    # ==================== 评论随机等待 ====================
    comment_sleep = config_data.get("评论随机等待", {})
    if comment_sleep:
        target_config.RANDOM_SLEEP_COMMENTS_MIN_SEC = comment_sleep.get("min_sec", 3.0)
        target_config.RANDOM_SLEEP_COMMENTS_MAX_SEC = comment_sleep.get("max_sec", 6.0)
        target_config.RANDOM_SLEEP_COMMENTS_DISTRIBUTION = comment_sleep.get("distribution", "lognormal")
    
    # ==================== 情境化等待 ====================
    contextual = config_data.get("情境化等待", {})
    if contextual:
        target_config.CONTEXTUAL_WAIT_ENABLED = contextual.get("enabled", True)
        target_config.CONTEXTUAL_PAGE_LOAD_SEC = contextual.get("page_load_sec", [5.0, 10.0])
        target_config.CONTEXTUAL_USER_INTERACTION_SEC = contextual.get("user_interaction_sec", [1.0, 3.0])
        target_config.CONTEXTUAL_API_RETRY_SEC = contextual.get("api_retry_sec", [0.3, 0.8])
    
    # ==================== 批次暂停策略 ====================
    batch_pause = config_data.get("批次暂停策略", {})
    if batch_pause:
        target_config.BATCH_PAUSE_ENABLED = batch_pause.get("enabled", True)
        target_config.BATCH_PAUSE_EVERY_N_MIN = batch_pause.get("every_n_min", 8)
        target_config.BATCH_PAUSE_EVERY_N_MAX = batch_pause.get("every_n_max", 12)
        target_config.BATCH_PAUSE_MIN_SEC = batch_pause.get("min_sec", 30.0)
        target_config.BATCH_PAUSE_MAX_SEC = batch_pause.get("max_sec", 60.0)
    
    # ==================== 动态调整策略 ====================
    dynamic = config_data.get("动态调整策略", {})
    if dynamic:
        target_config.DYNAMIC_ADJUST_ENABLED = dynamic.get("enabled", True)
        target_config.DYNAMIC_SUCCESS_THRESHOLD = dynamic.get("success_threshold", 10)
        target_config.DYNAMIC_SPEED_UP_FACTOR = dynamic.get("speed_up_factor", 0.8)
        target_config.DYNAMIC_SLOW_DOWN_FACTOR = dynamic.get("slow_down_factor", 2.0)
        target_config.DYNAMIC_SUPER_SLEEP_ON_BLOCK = dynamic.get("super_sleep_on_block", True)
        target_config.DYNAMIC_SUPER_SLEEP_MIN_SEC = dynamic.get("super_sleep_min_sec", 300)
        target_config.DYNAMIC_SUPER_SLEEP_MAX_SEC = dynamic.get("super_sleep_max_sec", 600)
    
    # ==================== 假动作策略 ====================
    fake_action = config_data.get("假动作策略", {})
    if fake_action:
        target_config.FAKE_ACTION_ENABLED = fake_action.get("enabled", True)
        target_config.FAKE_ACTION_PROBABILITY = fake_action.get("probability", 0.15)
        target_config.FAKE_ACTION_MIN_SEC = fake_action.get("min_sec", 2.0)
        target_config.FAKE_ACTION_MAX_SEC = fake_action.get("max_sec", 5.0)
        target_config.FAKE_ACTION_KEYWORDS = fake_action.get("keywords", [])
        target_config.FAKE_ACTION_ENABLE_SCROLL = fake_action.get("enable_scroll", True)
        target_config.FAKE_ACTION_ENABLE_MOUSE_MOVE = fake_action.get("enable_mouse_move", True)
        target_config.FAKE_ACTION_ENABLE_CLICK_PROFILE = fake_action.get("enable_click_profile", True)
        target_config.FAKE_ACTION_ENABLE_INPUT_SIMULATION = fake_action.get("enable_input_simulation", True)
    
    # ==================== 并发控制 ====================
    concurrency = config_data.get("并发控制", {})
    if concurrency:
        target_config.MAX_CONCURRENCY_NUM = concurrency.get("max_concurrency", 1)
    
    # ==================== 分批爬取 ====================
    batch_crawl = config_data.get("分批爬取", {})
    if batch_crawl:
        target_config.CRAWLER_MAX_NOTES_COUNT = batch_crawl.get("max_notes_count", 50)
    
    # ==================== 浏览器控制 ====================
    browser = config_data.get("浏览器控制", {})
    if browser:
        target_config.AUTO_CLOSE_BROWSER = browser.get("auto_close_browser", False)
        target_config.BROWSER_LAUNCH_TIMEOUT = browser.get("browser_launch_timeout", 120)
        target_config.BROWSER_REMOVE_WEBDRIVER_FLAG = browser.get("remove_webdriver_flag", True)
        target_config.BROWSER_SPOOF_CANVAS = browser.get("spoof_canvas_fingerprint", True)
        target_config.BROWSER_SPOOF_WEBGL = browser.get("spoof_webgl_fingerprint", True)
    
    # ==================== 容错机制 ====================
    error_handling = config_data.get("容错机制", {})
    if error_handling:
        backoff = error_handling.get("exponential_backoff", {})
        target_config.BACKOFF_ENABLED = backoff.get("enabled", True)
        target_config.BACKOFF_INITIAL_DELAY = backoff.get("initial_delay_sec", 2.0)
        target_config.BACKOFF_MAX_DELAY = backoff.get("max_delay_sec", 120.0)
        target_config.BACKOFF_MULTIPLIER = backoff.get("multiplier", 2.0)
        target_config.BACKOFF_MAX_RETRIES = backoff.get("max_retries", 5)
        target_config.RECOVERABLE_ERRORS = error_handling.get("recoverable_errors", [408, 429, 500, 502, 503, 504])
        target_config.UNRECOVERABLE_ERRORS = error_handling.get("unrecoverable_errors", [401, 403])
    
    # ==================== 爬取设置 ====================
    crawl_settings = config_data.get("crawl_settings", {}) or config_data.get("爬取设置", {})
    if crawl_settings:
        # 平台
        if crawl_settings.get("platform"):
            target_config.PLATFORM = crawl_settings.get("platform")
        # 爬取类型
        if crawl_settings.get("crawler_type"):
            target_config.CRAWLER_TYPE = crawl_settings.get("crawler_type")
        # 关键词
        if crawl_settings.get("keywords"):
            target_config.KEYWORDS = crawl_settings.get("keywords")
        # 作者 ID 列表
        if crawl_settings.get("creator_ids"):
            # 支持字符串或列表格式
            creator_ids = crawl_settings.get("creator_ids")
            if isinstance(creator_ids, str):
                target_config.XHS_CREATOR_ID_LIST = [id.strip() for id in creator_ids.split(",") if id.strip()]
            elif isinstance(creator_ids, list):
                target_config.XHS_CREATOR_ID_LIST = creator_ids
        # 最大作品数
        if crawl_settings.get("max_notes_count"):
            target_config.CRAWLER_MAX_NOTES_COUNT = crawl_settings.get("max_notes_count")
        # 评论设置
        if "enable_get_comments" in crawl_settings:
            target_config.ENABLE_GET_COMMENTS = crawl_settings.get("enable_get_comments")
        if "enable_get_sub_comments" in crawl_settings:
            target_config.ENABLE_GET_SUB_COMMENTS = crawl_settings.get("enable_get_sub_comments")
    
    utils.logger.info("[AntiCrawlLoader] Config applied successfully")
    _log_current_config(target_config)


def _log_current_config(target_config) -> None:
    """
    打印当前反反爬配置
    """
    utils.logger.info("=" * 60)
    utils.logger.info("[AntiCrawlLoader] 当前反反爬配置:")
    utils.logger.info("-" * 60)
    
    # 随机等待
    dist = getattr(target_config, "RANDOM_SLEEP_DISTRIBUTION", "uniform")
    utils.logger.info(f"  随机等待: {target_config.RANDOM_SLEEP_MIN_SEC}s ~ {target_config.RANDOM_SLEEP_MAX_SEC}s ({dist}分布)")
    utils.logger.info(f"  评论等待: {target_config.RANDOM_SLEEP_COMMENTS_MIN_SEC}s ~ {target_config.RANDOM_SLEEP_COMMENTS_MAX_SEC}s")
    
    # 情境化等待
    if getattr(target_config, "CONTEXTUAL_WAIT_ENABLED", False):
        utils.logger.info(f"  情境化等待: 已启用")
    
    # 批次暂停
    min_n = getattr(target_config, "BATCH_PAUSE_EVERY_N_MIN", 8)
    max_n = getattr(target_config, "BATCH_PAUSE_EVERY_N_MAX", 12)
    utils.logger.info(f"  批次暂停: 每 {min_n}~{max_n} 条暂停 {target_config.BATCH_PAUSE_MIN_SEC}s ~ {target_config.BATCH_PAUSE_MAX_SEC}s")
    
    # 动态调整
    if getattr(target_config, "DYNAMIC_ADJUST_ENABLED", False):
        utils.logger.info(f"  动态调整: 已启用 (成功{getattr(target_config, 'DYNAMIC_SUCCESS_THRESHOLD', 10)}次后加速)")
    
    # 假动作
    utils.logger.info(f"  假动作: {target_config.FAKE_ACTION_PROBABILITY * 100}% 概率")
    features = []
    if getattr(target_config, "FAKE_ACTION_ENABLE_SCROLL", False):
        features.append("滚动")
    if getattr(target_config, "FAKE_ACTION_ENABLE_MOUSE_MOVE", False):
        features.append("鼠标移动")
    if getattr(target_config, "FAKE_ACTION_ENABLE_INPUT_SIMULATION", False):
        features.append("模拟输入")
    if features:
        utils.logger.info(f"    行为模拟: {', '.join(features)}")
    
    # 浏览器
    if getattr(target_config, "BROWSER_REMOVE_WEBDRIVER_FLAG", False):
        utils.logger.info(f"  浏览器指纹: 已启用反检测")
    
    # 容错
    if getattr(target_config, "BACKOFF_ENABLED", False):
        utils.logger.info(f"  指数退避: 最多重试 {getattr(target_config, 'BACKOFF_MAX_RETRIES', 5)} 次")
    
    utils.logger.info(f"  并发数: {target_config.MAX_CONCURRENCY_NUM}")
    utils.logger.info(f"  每批数量: {target_config.CRAWLER_MAX_NOTES_COUNT}")
    utils.logger.info("=" * 60)
