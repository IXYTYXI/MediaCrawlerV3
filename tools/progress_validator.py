# -*- coding: utf-8 -*-
"""
进度文件预检校验模块

在批量爬取开始前自动扫描所有进度文件，检测并清理异常数据：
1. 跨作者 crawled_ids 重复 — session 失效时 progress 被错误复制的典型表现
2. 有进度但无对应有效数据文件
"""

import json
from pathlib import Path
from typing import List, Optional

from tools import utils


def preflight_validate_progress(
    platform: str = "xhs",
    creator_ids: Optional[List[str]] = None,
) -> List[str]:
    """
    批量爬取前预检：扫描进度文件，检测跨作者 crawled_ids 重复并自动清理。

    Args:
        platform: 平台名称
        creator_ids: 可选，只检查这些 user_id

    Returns:
        被清理的 user_id 列表
    """
    progress_dir = Path("data") / platform / "progress"
    if not progress_dir.exists():
        return []

    progress_files = list(progress_dir.glob("creator_*_progress.json"))
    if not progress_files:
        return []

    fingerprints: dict = {}
    cleaned: list = []

    for pf in progress_files:
        uid = pf.stem.replace("creator_", "").replace("_progress", "")
        if creator_ids and uid not in creator_ids:
            continue
        try:
            with open(pf, "r", encoding="utf-8") as f:
                data = json.load(f)
            crawled_ids = data.get("crawled_ids", [])
            count = len(crawled_ids)
            if count == 0:
                continue
            sample = frozenset(sorted(crawled_ids)[:100])
            fp_key = (count, sample)
            if fp_key not in fingerprints:
                fingerprints[fp_key] = []
            fingerprints[fp_key].append({
                "uid": uid,
                "count": count,
                "path": pf,
                "task_id": data.get("task_id", ""),
            })
        except Exception:
            continue

    for _fp_key, group in fingerprints.items():
        if len(group) <= 1:
            continue

        uids = [g["uid"] for g in group]
        utils.logger.warning(
            f"[ProgressValidator] 检测到 {len(group)} 个作者进度 crawled_ids "
            f"完全相同 ({group[0]['count']} 条): {uids}"
        )

        for g in group:
            has_valid_data = _check_valid_data(platform, g["uid"])
            if has_valid_data:
                utils.logger.info(
                    f"[ProgressValidator] {g['uid']}: 有有效数据，保留进度"
                )
            else:
                try:
                    g["path"].unlink()
                    cleaned.append(g["uid"])
                    utils.logger.info(
                        f"[ProgressValidator] 已清理无效进度: {g['uid']}"
                    )
                except Exception as e:
                    utils.logger.warning(
                        f"[ProgressValidator] 清理失败 {g['uid']}: {e}"
                    )

    if cleaned:
        utils.logger.info(
            f"[ProgressValidator] 预检完成: 清理了 {len(cleaned)} 个异常进度文件"
        )
    else:
        utils.logger.info("[ProgressValidator] 预检通过: 所有进度文件正常")

    return cleaned


def _check_valid_data(platform: str, uid: str) -> bool:
    """检查某个作者在所有 task 目录下是否有属于自己的有效数据文件。"""
    json_base = Path("data") / platform / "json"
    if not json_base.exists():
        return False

    for td in json_base.iterdir():
        if not td.is_dir():
            continue
        cf = td / f"creator_{uid}_contents.json"
        if not cf.exists() or cf.stat().st_size < 100:
            continue
        try:
            with open(cf, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list) or not records:
                continue
            own = sum(1 for r in records if r.get("user_id") == uid)
            if own / max(len(records), 1) >= 0.8:
                return True
        except Exception:
            pass

    return False
