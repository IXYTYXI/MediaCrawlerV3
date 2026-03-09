# -*- coding: utf-8 -*-
"""
搜索模式关键词池加载器

当 crawler_type=search 且未传入 --keywords 时，从 config/search_keyword_pool.json 加载关键词，
避免每次启动都必须在命令行写关键词。
"""

import json
import os
from typing import Optional

from tools import utils


def load_search_keyword_pool(config_path: Optional[str] = None) -> Optional[str]:
    """
    从关键词池文件加载关键词字符串（逗号分隔）。

    Args:
        config_path: 池文件路径，默认 config/search_keyword_pool.json

    Returns:
        逗号分隔的关键词字符串；文件不存在或 keywords 为空时返回 None。
    """
    if config_path is None:
        config_path = os.path.join("config", "search_keyword_pool.json")
    if not os.path.exists(config_path):
        utils.logger.debug(f"[SearchKeywordPool] 文件不存在: {config_path}")
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        utils.logger.warning(f"[SearchKeywordPool] 读取失败: {e}")
        return None
    keywords = data.get("keywords")
    if not keywords:
        return None
    if isinstance(keywords, list):
        parts = [str(k).strip() for k in keywords if str(k).strip()]
    else:
        parts = [str(keywords).strip()] if str(keywords).strip() else []
    if not parts:
        return None
    result = ",".join(parts)
    utils.logger.info(f"[SearchKeywordPool] 已从关键词池加载 {len(parts)} 个关键词")
    return result
