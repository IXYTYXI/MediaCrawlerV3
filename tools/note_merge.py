# -*- coding: utf-8 -*-
"""
笔记数据合并工具
用于增量爬取时合并新旧笔记数据：保留媒体字段，更新互动量。
"""
from typing import Dict

# 互动量字段：始终取最新值
_STATS_FIELDS = {
    "liked_count", "collected_count", "comment_count", "share_count",
}

# 内容字段：取非空且更完整的值
_CONTENT_FIELDS = {
    "title", "desc", "tag_list", "note_url", "ip_location",
    "xsec_token", "xsec_source",
}

# 媒体字段：优先保留已有的（避免丢失本地已下载的路径信息）
_MEDIA_FIELDS = {
    "image_list", "video_url",
}

# 不可变字段：不参与合并
_IDENTITY_FIELDS = {
    "note_id", "user_id", "type", "time",
}


def _safe_int(v) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def _field_richness(val) -> int:
    """评估字段值的丰富程度"""
    if val is None or val == "" or val == 0:
        return 0
    if isinstance(val, str):
        return len(val)
    if isinstance(val, (list, dict)):
        return len(str(val))
    return 1


def merge_note_metadata(existing: Dict, update: Dict) -> Dict:
    """
    合并两条笔记记录。

    策略：
    - 身份字段(note_id, user_id, type, time)：保留不变
    - 互动量(liked_count等)：取数值更大的（或 update 的，因为更新）
    - 媒体字段(image_list, video_url)：保留更丰富的
    - 内容字段(title, desc等)：保留更丰富的
    - 其他未知字段：update 有则用 update，否则保留 existing
    - 内部标记(_creator_name, _reused等)：保留 existing 的

    Args:
        existing: 已有的笔记数据（历史记录）
        update: 新获取的笔记数据

    Returns:
        合并后的笔记数据（新 dict，不修改原始数据）
    """
    if not existing:
        return dict(update) if update else {}
    if not update:
        return dict(existing)

    merged = dict(existing)

    # 身份字段：以 existing 为准（不可变）
    for key in _IDENTITY_FIELDS:
        if key in update and not merged.get(key):
            merged[key] = update[key]

    # 互动量：取 update 的值（更新鲜），但如果 update 为 0 而 existing 有值则保留
    for key in _STATS_FIELDS:
        new_val = _safe_int(update.get(key, 0))
        old_val = _safe_int(existing.get(key, 0))
        merged[key] = new_val if new_val > 0 else old_val

    # 媒体字段：保留更丰富的
    for key in _MEDIA_FIELDS:
        old_r = _field_richness(existing.get(key))
        new_r = _field_richness(update.get(key))
        if new_r > old_r:
            merged[key] = update[key]

    # 内容字段：保留更丰富的
    for key in _CONTENT_FIELDS:
        old_r = _field_richness(existing.get(key))
        new_r = _field_richness(update.get(key))
        if new_r > old_r:
            merged[key] = update[key]

    # 其他字段：update 覆盖（但不覆盖 _ 开头的内部标记）
    for key, val in update.items():
        if key.startswith("_"):
            continue
        if key in _IDENTITY_FIELDS | _STATS_FIELDS | _MEDIA_FIELDS | _CONTENT_FIELDS:
            continue
        if val is not None and val != "":
            merged[key] = val

    # 保留 existing 的内部标记
    for key, val in existing.items():
        if key.startswith("_") and key not in merged:
            merged[key] = val

    # nickname 特殊处理：取非空的
    if update.get("nickname") and not merged.get("nickname"):
        merged["nickname"] = update["nickname"]

    # last_update_time：取较大的
    old_lut = _safe_int(existing.get("last_update_time", 0))
    new_lut = _safe_int(update.get("last_update_time", 0))
    if new_lut > old_lut:
        merged["last_update_time"] = new_lut

    return merged
