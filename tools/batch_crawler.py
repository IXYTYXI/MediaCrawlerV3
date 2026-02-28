# -*- coding: utf-8 -*-
"""
批量作者爬取调度器
从 Excel 读取作者列表，逐个爬取，爬取完成后写入飞书多维表格

使用方式:
  uv run python -m tools.batch_crawler
  uv run python -m tools.batch_crawler --excel redbookaccontidandresult.xlsx
  uv run python -m tools.batch_crawler --skip-feishu  # 跳过飞书写入
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from typing import List, Dict, Any, Optional, Set, Tuple

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from tools import utils
from tools.excel_reader import ExcelCreatorReader, load_creators_from_excel
from tools.progress_validator import preflight_validate_progress

import signal


# ==================== 优雅退出 ====================

class _GracefulShutdown:
    """跟踪当前爬取状态，支持信号中断时保存部分数据"""
    shutdown_requested: bool = False
    current_creator_name: str = ""
    current_creator_url: str = ""
    current_user_id: str = ""
    current_session_ts: str = ""
    current_task_dir: str = ""
    current_progress: Any = None

    @classmethod
    def reset_current(cls):
        cls.current_creator_name = ""
        cls.current_creator_url = ""
        cls.current_user_id = ""
        cls.current_session_ts = ""

    @classmethod
    def save_partial_and_exit(cls):
        """信号处理：保存当前作者的部分数据后标记完成"""
        if cls.shutdown_requested:
            return
        cls.shutdown_requested = True
        utils.logger.info("")
        utils.logger.info("=" * 60)
        utils.logger.info("[GracefulShutdown] 收到停止信号，正在保存当前进度...")

        if cls.current_user_id and cls.current_session_ts and cls.current_task_dir:
            try:
                data_dir = os.path.join("data", "xhs", "json")
                partial_notes = collect_crawled_data(
                    data_dir, cls.current_session_ts,
                    cls.current_creator_name, min_interaction=0,
                    user_id=cls.current_user_id,
                )
                if partial_notes:
                    contents_file = os.path.join(
                        cls.current_task_dir,
                        f"creator_{cls.current_user_id}_contents.json",
                    )
                    # 合并已有数据（如果存在）
                    if os.path.exists(contents_file):
                        try:
                            with open(contents_file, "r", encoding="utf-8") as f:
                                old_data = json.load(f)
                            new_ids = {n.get("note_id") for n in partial_notes if n.get("note_id")}
                            extra = [n for n in old_data if n.get("note_id") and n["note_id"] not in new_ids]
                            if extra:
                                partial_notes = partial_notes + extra
                                utils.logger.info(
                                    f"[GracefulShutdown] 合并旧数据 {len(extra)} 条"
                                )
                        except Exception:
                            pass
                    with open(contents_file, "w", encoding="utf-8") as f:
                        json.dump(partial_notes, f, ensure_ascii=False, indent=2)
                    utils.logger.info(
                        f"[GracefulShutdown] 已保存 {cls.current_creator_name} 的 "
                        f"{len(partial_notes)} 条数据到 {contents_file}"
                    )
                    if cls.current_progress and cls.current_creator_url:
                        cls.current_progress.mark_partial(
                            cls.current_creator_url,
                            f"{len(partial_notes)} notes saved at shutdown"
                        )
                        utils.logger.info(
                            f"[GracefulShutdown] 已标记 {cls.current_creator_name} 为部分完成"
                            f"（{len(partial_notes)} 条已保存，下次会重新爬取）"
                        )
                else:
                    utils.logger.info(
                        f"[GracefulShutdown] {cls.current_creator_name} 暂无可保存的数据"
                    )
            except Exception as e:
                utils.logger.error(f"[GracefulShutdown] 保存部分数据失败: {e}")
        else:
            utils.logger.info("[GracefulShutdown] 当前没有正在爬取的作者，无需保存")

        utils.logger.info("[GracefulShutdown] 退出完成")
        utils.logger.info("=" * 60)


def _install_signal_handlers():
    """注册信号处理器，支持 SIGTERM / SIGINT 优雅退出"""
    def _handler(signum, frame):
        sig_name = signal.Signals(signum).name
        utils.logger.info(f"[GracefulShutdown] 收到信号 {sig_name}")
        _GracefulShutdown.save_partial_and_exit()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# ==================== 批量爬取进度管理 ====================

class BatchProgress:
    """批量爬取进度管理（记录已完成的作者）"""

    def __init__(self, progress_file: str = "data/batch_progress.json", task_id: str = ""):
        self.progress_file = progress_file
        self._completed: Set[str] = set()  # 已完成的作者 URL
        self._failed: Dict[str, str] = {}  # 失败记录 {url: error_msg}
        self._partial: Dict[str, str] = {}  # 部分完成 {url: "265 notes saved"}
        self._skipped: Dict[str, str] = {}  # 永久跳过 {url: reason}（如作者隐藏所有作品）
        self._session_id: str = ""
        self._load()

    def _load(self):
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._completed = set(data.get("completed", []))
                raw_failed = data.get("failed", {})
                # 兼容：如果 failed 是 list（旧格式/手动清理残留），转为 dict
                if isinstance(raw_failed, list):
                    self._failed = {}
                elif isinstance(raw_failed, dict):
                    self._failed = raw_failed
                else:
                    self._failed = {}
                self._session_id = data.get("session_id", "")
                raw_partial = data.get("partial", {})
                self._partial = raw_partial if isinstance(raw_partial, dict) else {}
                raw_skipped = data.get("skipped", {})
                self._skipped = raw_skipped if isinstance(raw_skipped, dict) else {}
                utils.logger.info(
                    f"[BatchProgress] 加载进度: {len(self._completed)} 完成, "
                    f"{len(self._partial)} 部分完成, "
                    f"{len(self._failed)} 失败, "
                    f"{len(self._skipped)} 跳过"
                )
            except Exception as e:
                utils.logger.warning(f"[BatchProgress] 加载进度失败: {e}")

    def save(self):
        os.makedirs(os.path.dirname(self.progress_file), exist_ok=True)
        self._ensure_failed_is_dict()
        data = {
            "session_id": self._session_id,
            "completed": list(self._completed),
            "failed": self._failed,
            "partial": self._partial,
            "skipped": self._skipped,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(self.progress_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def is_completed(self, url: str) -> bool:
        return url in self._completed

    def _ensure_failed_is_dict(self):
        """防御性检查：确保 _failed 始终是 dict"""
        if not isinstance(self._failed, dict):
            utils.logger.warning(f"[BatchProgress] _failed 类型异常 ({type(self._failed).__name__})，已重置为 dict")
            self._failed = {}

    def mark_completed(self, url: str):
        self._completed.add(url)
        self._ensure_failed_is_dict()
        self._failed.pop(url, None)
        self._partial.pop(url, None)
        self.save()

    def mark_failed(self, url: str, error: str):
        self._ensure_failed_is_dict()
        self._failed[url] = error
        self.save()

    def mark_skipped(self, url: str, reason: str):
        """永久跳过（如作者隐藏所有作品），后续不再爬取"""
        self._skipped[url] = reason
        self._completed.discard(url)
        self._ensure_failed_is_dict()
        self._failed.pop(url, None)
        self._partial.pop(url, None)
        self.save()

    def is_skipped(self, url: str) -> bool:
        return url in self._skipped

    def mark_partial(self, url: str, info: str):
        """标记为部分完成（优雅中止时使用，下次会重新爬取）"""
        self._partial[url] = info
        self._completed.discard(url)
        self._ensure_failed_is_dict()
        self._failed.pop(url, None)
        self.save()

    def is_partial(self, url: str) -> bool:
        return url in self._partial

    def clear_partial(self, url: str):
        """爬取完成后清除 partial 标记"""
        self._partial.pop(url, None)

    def remove_completed(self, url: str):
        """从已完成列表中移除（校验失败时使用）"""
        self._completed.discard(url)
        self.save()

    def set_session(self, session_id: str):
        self._session_id = session_id

    def reset(self):
        """重置进度（重新开始），skipped 保留（永久跳过不受 reset 影响）"""
        self._completed.clear()
        self._failed.clear()
        self._partial.clear()
        self.save()

    @property
    def completed_count(self) -> int:
        return len(self._completed)


# ==================== 数据收集与过滤 ====================

def _safe_int(value) -> int:
    """安全转换为整数，处理空字符串、None、以及 '3.2万' 等中文数字格式"""
    if value is None or value == "":
        return 0
    import re
    val = str(value).strip()
    # 处理 "3.2万" 格式
    match = re.match(r'^([\d.]+)\s*万$', val)
    if match:
        return int(float(match.group(1)) * 10000)
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def get_interaction_count(note: Dict) -> int:
    """计算笔记的互动量 = 点赞 + 评论 + 收藏"""
    liked = _safe_int(note.get("liked_count", 0))
    comment = _safe_int(note.get("comment_count", 0))
    collected = _safe_int(note.get("collected_count", 0))
    return liked + comment + collected


def _check_date_range(note: Dict, date_start: str, date_end: str) -> bool:
    """检查作品发布时间是否在指定日期范围内"""
    if not date_start and not date_end:
        return True
    time_val = note.get("time", 0)
    if not time_val or not isinstance(time_val, (int, float)):
        return True
    try:
        publish_date = datetime.fromtimestamp(time_val / 1000)
        if date_start:
            start = datetime.strptime(date_start, "%Y-%m-%d")
            if publish_date < start:
                return False
        if date_end:
            end = datetime.strptime(date_end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            if publish_date > end:
                return False
        return True
    except Exception:
        return True


def _note_completeness(n: Dict) -> int:
    """评估记录完整度：有详情的 > 只有基本信息的"""
    score = 0
    if n.get("desc"):
        score += 10
    if n.get("title") and len(str(n.get("title", ""))) > 10:
        score += 5
    score += _safe_int(n.get("liked_count", 0))
    score += _safe_int(n.get("collected_count", 0))
    score += _safe_int(n.get("comment_count", 0))
    if n.get("tag_list"):
        score += 3
    if n.get("image_list") and len(str(n.get("image_list", ""))) > 20:
        score += 2
    return score


def collect_crawled_data(data_dir: str, session_timestamp: str,
                         creator_name: str = "",
                         min_interaction: int = 0,
                         user_id: str = "") -> List[Dict]:
    """
    从爬取结果 JSON 文件中收集数据，支持互动量过滤和作者过滤
    
    Args:
        data_dir: 数据目录 (如 data/xhs/json)
        session_timestamp: 会话时间戳（用于匹配文件）
        creator_name: 作者名称（附加到每条记录）
        min_interaction: 最低互动量阈值，0=不过滤
        user_id: 作者 user_id，非空时仅收集该作者的笔记（防止数据串作者）
        
    Returns:
        笔记数据列表
    """
    notes = []
    filtered_count = 0
    author_filtered_count = 0
    
    if not os.path.exists(data_dir):
        utils.logger.warning(f"[BatchCrawler] 数据目录不存在: {data_dir}")
        return notes

    # 查找匹配的 contents 文件
    for filename in os.listdir(data_dir):
        if not filename.endswith(".json"):
            continue
        if "contents" not in filename:
            continue
        if session_timestamp and session_timestamp not in filename:
            continue

        filepath = os.path.join(data_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    # 按 user_id 过滤，防止数据串到其他作者
                    if user_id and item.get("user_id", "") != user_id:
                        author_filtered_count += 1
                        continue

                    if creator_name:
                        item["_creator_name"] = creator_name
                    
                    # 互动量过滤
                    if min_interaction > 0:
                        interaction = get_interaction_count(item)
                        if interaction < min_interaction:
                            filtered_count += 1
                            continue
                    
                    notes.append(item)
            utils.logger.info(
                f"[BatchCrawler] 从 {filename} 读取 {len(data) if isinstance(data, list) else 0} 条记录"
            )
        except Exception as e:
            utils.logger.error(f"[BatchCrawler] 读取 {filename} 失败: {e}")

    if author_filtered_count > 0:
        utils.logger.info(
            f"[BatchCrawler] 作者过滤: 保留 {len(notes)} 条, "
            f"过滤掉 {author_filtered_count} 条非本作者数据"
        )

    if filtered_count > 0:
        utils.logger.info(
            f"[BatchCrawler] 互动量过滤: 保留 {len(notes)} 条, 过滤掉 {filtered_count} 条 "
            f"(阈值: {min_interaction})"
        )

    return notes


def _merge_note_metadata(existing: Dict, update: Dict) -> Dict:
    """委托给 tools.note_merge.merge_note_metadata"""
    from tools.note_merge import merge_note_metadata
    return merge_note_metadata(existing, update)


def _load_creator_notes_for_pipeline(
    task_dir: str, user_id: str,
    crawl_mode: str = "full", date_start: str = "", date_end: str = "",
    min_interaction: int = 0,
) -> List[Dict]:
    """
    读取单个作者在 task_dir 下的所有数据（contents + reuse），
    合并去重后按日期/互动量过滤，用于 pipeline 模式即时写入飞书。
    """
    notes_by_id: Dict[str, Dict] = {}
    for suffix in ["_contents.json", "_reuse.json"]:
        fp = os.path.join(task_dir, f"creator_{user_id}{suffix}")
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                arr = json.load(f)
            if not isinstance(arr, list):
                continue
            for item in arr:
                nid = item.get("note_id", "")
                if not nid:
                    continue
                if nid not in notes_by_id:
                    notes_by_id[nid] = item
                else:
                    cur = notes_by_id[nid]
                    base = item if _note_completeness(item) > _note_completeness(cur) else cur
                    update = cur if base is item else item
                    notes_by_id[nid] = _merge_note_metadata(base, update)
        except Exception:
            pass

    result = []
    for item in notes_by_id.values():
        if item.get("_reused"):
            result.append(item)
            continue
        if crawl_mode == "date_range" and not _check_date_range(item, date_start, date_end):
            continue
        if min_interaction > 0 and get_interaction_count(item) < min_interaction:
            continue
        result.append(item)
    return result


def _merge_partial_data(task_dir: str, user_id: str, new_notes: list):
    """
    合并 partial 旧数据与新爬取数据（按 note_id 去重）
    确保中止后重爬时，旧数据中未覆盖到的笔记不会丢失
    """
    contents_file = os.path.join(task_dir, f"creator_{user_id}_contents.json")
    if not os.path.exists(contents_file) or not new_notes:
        return

    try:
        with open(contents_file, "r", encoding="utf-8") as f:
            existing = json.load(f)

        new_ids = {n.get("note_id") for n in new_notes if n.get("note_id")}
        merged_extra = [n for n in existing if n.get("note_id") and n["note_id"] not in new_ids]

        if merged_extra:
            combined = new_notes + merged_extra
            with open(contents_file, "w", encoding="utf-8") as f:
                json.dump(combined, f, ensure_ascii=False, indent=2)
            utils.logger.info(
                f"  [合并] 新爬 {len(new_notes)} + 旧数据 {len(merged_extra)} = {len(combined)} 条"
            )
    except Exception as e:
        utils.logger.warning(f"  [合并] 合并 partial 数据失败: {e}")


# ==================== 历史数据复用 ====================

def scan_history_for_creator(user_id: str, creator_name: str,
                              crawl_mode: str, date_start: str, date_end: str,
                              min_interaction: int) -> List[Dict]:
    """
    扫描历史 JSON 文件，提取指定作者符合条件的数据
    
    Returns:
        符合条件的笔记列表
    """
    data_dir = os.path.join("data", "xhs", "json")
    if not os.path.exists(data_dir):
        return []

    notes_by_id: Dict[str, Dict] = {}

    for filename in sorted(os.listdir(data_dir)):
        if not filename.endswith(".json") or not filename.startswith("creator_contents"):
            continue
        # 跳过 task 子目录里的文件
        filepath = os.path.join(data_dir, filename)
        if not os.path.isfile(filepath):
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue
            for item in data:
                if item.get("user_id") != user_id:
                    continue
                nid = item.get("note_id", "")
                if not nid:
                    continue
                if nid not in notes_by_id or _note_completeness(item) > _note_completeness(notes_by_id[nid]):
                    notes_by_id[nid] = item
        except Exception:
            pass

    # 过滤
    result = []
    for item in notes_by_id.values():
        item["_creator_name"] = creator_name
        # 日期过滤
        if crawl_mode == "date_range" and not _check_date_range(item, date_start, date_end):
            continue
        # 互动量过滤
        if min_interaction > 0:
            if get_interaction_count(item) < min_interaction:
                continue
        result.append(item)

    return result


def save_reuse_data(task_dir: str, user_id: str, notes: List[Dict]):
    """保存复用数据到任务目录"""
    os.makedirs(task_dir, exist_ok=True)
    # 标记为已复用数据（导出时不再重复过滤）
    for note in notes:
        note["_reused"] = True
    filepath = os.path.join(task_dir, f"creator_{user_id}_reuse.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)
    utils.logger.info(f"[复用] 保存 {len(notes)} 条到 {filepath}")


def get_last_crawl_date_for_creator(task_dir: str, user_id: str) -> str:
    """
    从任务目录下该作者的 contents/reuse 文件中获取最新笔记日期
    Returns: "YYYY-MM-DD" 或 ""（无数据）
    """
    if not task_dir or not user_id:
        return ""
    max_ts = 0
    for suffix in ["_contents.json", "_reuse.json"]:
        fp = os.path.join(task_dir, f"creator_{user_id}{suffix}")
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue
            for item in data:
                t = item.get("time", 0)
                if t and isinstance(t, (int, float)):
                    if t > 1e12:
                        max_ts = max(max_ts, t)
                    elif t > 0:
                        max_ts = max(max_ts, t * 1000)
        except Exception:
            pass
    if max_ts <= 0:
        return ""
    try:
        dt = datetime.fromtimestamp(max_ts / 1000)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def check_creator_history_status(user_id: str) -> str:
    """
    检查作者在历史爬取中的状态
    
    Returns:
        'completed' - 之前已爬完
        'partial' - 有部分数据但未完成
        'none' - 没有数据
    """
    # 检查所有历史 batch_progress 文件
    data_dir = "data"
    if os.path.exists(data_dir):
        for filename in os.listdir(data_dir):
            if filename.startswith("batch_progress") and filename.endswith(".json"):
                try:
                    with open(os.path.join(data_dir, filename), "r", encoding="utf-8") as f:
                        progress = json.load(f)
                    completed = progress.get("completed", [])
                    for url in completed:
                        if user_id in url:
                            return "completed"
                except Exception:
                    pass

    # 检查是否有该作者的数据
    json_dir = os.path.join("data", "xhs", "json")
    if os.path.exists(json_dir):
        for filename in os.listdir(json_dir):
            if not filename.endswith(".json") or not filename.startswith("creator_contents"):
                continue
            filepath = os.path.join(json_dir, filename)
            if not os.path.isfile(filepath):
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        if item.get("user_id") == user_id:
                            return "partial"
            except Exception:
                pass

    return "none"


# ==================== 格式化导出 ====================

def _parse_image_urls(image_list_raw) -> List[str]:
    """解析图片URL列表"""
    if not image_list_raw:
        return []
    if isinstance(image_list_raw, list):
        return [url for url in image_list_raw if url and str(url).startswith("http")]
    # 逗号分隔的字符串
    return [url.strip() for url in str(image_list_raw).split(",") if url.strip().startswith("http")]


def format_note_for_export(creator_name: str, note: Dict) -> Dict[str, Any]:
    """
    将笔记数据格式化为导出字段格式
    
    输出字段: 账号名称 | 内容类型 | 标题 | 正文 | 标签 | 链接 | 发布时间 |
              点赞数 | 收藏数 | 评论数 | 互动量 | 视频脚本 | 图片1 | 图片2 | ...
    """
    note_type = note.get("type", "")
    content_type = "视频" if note_type == "video" else "图片"

    # 发布时间 → datetime 对象（Excel 可识别）
    time_val = note.get("time", "")
    time_dt = None
    if isinstance(time_val, (int, float)) and time_val > 0:
        try:
            time_dt = datetime.fromtimestamp(time_val / 1000)
        except Exception:
            pass

    # 图片拆分为独立字段
    image_urls = _parse_image_urls(note.get("image_list", ""))

    # 视频脚本：留空（后续通过AI分析视频生成）
    video_script = ""

    # 互动数据
    liked = _safe_int(note.get("liked_count", 0))
    collected = _safe_int(note.get("collected_count", 0))
    comment = _safe_int(note.get("comment_count", 0))
    interaction = liked + collected + comment

    # 热门标记
    is_hot = "🔥 热门" if interaction >= 50 else ""

    # 视频URL
    video_url = note.get("video_url", "")

    result = {
        "账号名称": creator_name or note.get("nickname", ""),
        "内容类型": content_type,
        "标题": note.get("title", ""),
        "正文": note.get("desc", ""),
        "标签": note.get("tag_list", ""),
        "链接": note.get("note_url", ""),
        "发布时间": time_dt if time_dt else "",
        "点赞数": liked,
        "收藏数": collected,
        "评论数": comment,
        "互动量": interaction,
        "热门": is_hot,
        "视频附件": video_url if video_url else "",
        "视频脚本": video_script,
    }

    # 动态图片列：图片1, 图片2, ...
    for i, url in enumerate(image_urls, 1):
        result[f"图片{i}"] = url

    return result


def _get_max_image_count(notes: List[Dict]) -> int:
    """扫描所有笔记，获取最大图片数"""
    max_count = 0
    for note in notes:
        urls = _parse_image_urls(note.get("image_list", ""))
        if len(urls) > max_count:
            max_count = len(urls)
    return max_count


def export_to_local(notes: List[Dict], export_dir: str = "data/export",
                    export_format: str = "excel",
                    filename_prefix: str = "batch_crawl") -> str:
    """
    将数据按指定格式导出到本地文件
    Excel 格式时每个作者一个 Sheet
    
    Args:
        notes: 笔记数据列表
        export_dir: 导出目录
        export_format: 导出格式 (excel/json/csv)
        filename_prefix: 文件名前缀
        
    Returns:
        导出文件路径
    """
    os.makedirs(export_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if not notes:
        utils.logger.warning("[Export] 没有数据可导出")
        return ""

    # 按作者分组
    from collections import OrderedDict
    grouped: OrderedDict[str, List[Dict]] = OrderedDict()
    for note in notes:
        creator_name = note.get("_creator_name", note.get("nickname", "未知"))
        if creator_name not in grouped:
            grouped[creator_name] = []
        grouped[creator_name].append(note)

    # 扫描最大图片数，动态生成列
    max_images = _get_max_image_count(notes)
    image_columns = [f"图片{i}" for i in range(1, max_images + 1)]

    # 字段顺序：热门在互动量后，视频脚本在图片列前面
    columns = ["账号名称", "内容类型", "标题", "正文", "标签", "链接",
               "发布时间", "点赞数", "收藏数", "评论数", "互动量", "热门",
               "视频脚本"] + image_columns

    if export_format == "excel":
        filepath = os.path.join(export_dir, f"{filename_prefix}_{timestamp}.xlsx")
        _export_excel_multi_sheet(grouped, columns, filepath)
    elif export_format == "csv":
        filepath = os.path.join(export_dir, f"{filename_prefix}_{timestamp}.csv")
        # CSV 不支持多 sheet，合并导出
        all_formatted = []
        for creator_name, creator_notes in grouped.items():
            for note in creator_notes:
                all_formatted.append(format_note_for_export(creator_name, note))
        _export_csv(all_formatted, columns, filepath)
    else:  # json
        filepath = os.path.join(export_dir, f"{filename_prefix}_{timestamp}.json")
        all_formatted = []
        for creator_name, creator_notes in grouped.items():
            for note in creator_notes:
                all_formatted.append(format_note_for_export(creator_name, note))
        _export_json(all_formatted, filepath)

    total = sum(len(v) for v in grouped.values())
    utils.logger.info(
        f"[Export] 导出完成: {filepath} "
        f"({total} 条, {len(grouped)} 个作者)"
    )
    return filepath


def _sanitize_for_excel(value) -> str:
    """
    清理字符串中 openpyxl 不支持的非法字符（XML 控制字符等）。
    保留 emoji 和常见 Unicode 字符，仅移除 XML 1.0 不允许的控制字符。
    """
    import re
    if not isinstance(value, str):
        return value
    # XML 1.0 允许的字符范围：
    # #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
    # 移除不在此范围内的字符（如 \x00-\x08, \x0B, \x0C, \x0E-\x1F, \xFFFE, \xFFFF 等）
    illegal_xml_chars = re.compile(
        r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff]'
    )
    return illegal_xml_chars.sub('', value)


def _export_excel_multi_sheet(grouped: Dict[str, List[Dict]],
                               columns: List[str], filepath: str):
    """导出为 Excel，每个作者一个 Sheet"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    wb = openpyxl.Workbook()
    # 删除默认 sheet
    wb.remove(wb.active)

    # 样式
    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )
    wrap_align = Alignment(vertical="top", wrap_text=True)
    # 热门行高亮：浅橙色背景
    hot_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    hot_font = Font(bold=True, color="D35400")

    col_widths = {
        "账号名称": 18, "内容类型": 10, "标题": 30, "正文": 50,
        "标签": 25, "链接": 40, "发布时间": 20,
        "点赞数": 10, "收藏数": 10, "评论数": 10, "互动量": 10,
        "热门": 10, "视频脚本": 30,
    }
    # 图片列统一宽度
    for c in columns:
        if c.startswith("图片"):
            col_widths[c] = 45

    for creator_name, creator_notes in grouped.items():
        # Sheet 名称（Excel 限制 31 字符，不能含特殊字符）
        safe_name = creator_name[:31].replace("/", "-").replace("\\", "-")
        safe_name = safe_name.replace("*", "").replace("?", "").replace("[", "").replace("]", "")
        if not safe_name:
            safe_name = "未知作者"

        # 避免重名
        if safe_name in wb.sheetnames:
            safe_name = f"{safe_name[:28]}_{len(wb.sheetnames)}"

        ws = wb.create_sheet(title=safe_name)

        # 写表头
        for col_idx, col_name in enumerate(columns, 1):
            cell = ws.cell(row=1, column=col_idx, value=col_name)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border

        # 写数据
        from openpyxl.styles.numbers import FORMAT_DATE_DATETIME
        for row_idx, note in enumerate(creator_notes, 2):
            formatted = format_note_for_export(creator_name, note)
            is_hot_row = formatted.get("热门", "") != ""
            for col_idx, col_name in enumerate(columns, 1):
                value = formatted.get(col_name, "")
                if not isinstance(value, datetime):
                    value = _sanitize_for_excel(value)
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.alignment = wrap_align
                cell.border = thin_border
                if isinstance(value, datetime):
                    cell.number_format = "YYYY-MM-DD HH:MM:SS"
                # 热门行高亮
                if is_hot_row:
                    cell.fill = hot_fill
                    if col_name == "热门":
                        cell.font = hot_font

        # 列宽
        for col_idx, col_name in enumerate(columns, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = col_widths.get(col_name, 15)

        # 冻结首行
        ws.freeze_panes = "A2"

        utils.logger.info(f"[Export] Sheet '{safe_name}': {len(creator_notes)} 条")

    # 如果没有 sheet（不应该发生），添加空 sheet 避免报错
    if not wb.sheetnames:
        wb.create_sheet("空数据")

    wb.save(filepath)


def _export_csv(data: List[Dict], columns: List[str], filepath: str):
    """导出为 CSV"""
    import csv
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)


def _export_json(data: List[Dict], filepath: str):
    """导出为 JSON"""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==================== 本地图片查找 ====================

def _get_local_image_paths(note_id: str, image_dir: str = "data/xhs/images") -> List[str]:
    """
    根据 note_id 查找本地已下载的图片文件
    
    图片由爬虫下载保存在 data/xhs/images/{note_id}/0.jpg, 1.jpg, ...
    
    Args:
        note_id: 笔记ID
        image_dir: 图片存储根目录
        
    Returns:
        按序号排列的图片文件路径列表 ["data/xhs/images/{note_id}/0.jpg", ...]
    """
    note_dir = os.path.join(image_dir, note_id)
    if not os.path.exists(note_dir):
        return []
    
    image_files = []
    for filename in os.listdir(note_dir):
        filepath = os.path.join(note_dir, filename)
        if os.path.isfile(filepath) and filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            image_files.append(filepath)
    
    # 按文件名序号排序: 0.jpg, 1.jpg, 2.jpg, ...
    def sort_key(path):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            return int(name)
        except ValueError:
            return 999
    
    image_files.sort(key=sort_key)
    return image_files


def _get_local_video_path(note_id: str, video_dir: str = "data/xhs/videos") -> Optional[str]:
    """
    根据 note_id 查找本地已下载的视频文件
    
    视频由爬虫下载保存在 data/xhs/videos/{note_id}/0.mp4
    
    Args:
        note_id: 笔记ID
        video_dir: 视频存储根目录
        
    Returns:
        视频文件路径，不存在返回 None
    """
    note_dir = os.path.join(video_dir, note_id)
    if not os.path.exists(note_dir):
        return None

    for filename in os.listdir(note_dir):
        filepath = os.path.join(note_dir, filename)
        if os.path.isfile(filepath) and filename.lower().endswith(('.mp4', '.mov', '.avi', '.webm')):
            return filepath
    return None


def _upload_note_video_to_feishu(
    client, app_token: str, note_id: str,
    video_url: str = ""
) -> Optional[List[Dict]]:
    """
    上传单个笔记的视频到飞书，优先使用本地文件，URL作为回退
    
    Args:
        client: FeishuBitableClient 实例
        app_token: 多维表格 token
        note_id: 笔记ID
        video_url: 视频URL（作为回退）
        
    Returns:
        [{"file_token": "xxx"}] 格式的附件值，失败返回 None
    """
    # 优先本地文件
    local_path = _get_local_video_path(note_id)
    if local_path:
        try:
            file_size_mb = os.path.getsize(local_path) / 1024 / 1024
            # 飞书上传限制 20MB
            if file_size_mb > 20:
                utils.logger.warning(
                    f"[视频上传] {note_id}: 视频 {file_size_mb:.1f}MB 超过 20MB 限制，跳过"
                )
                return None
            file_token = client.upload_media(app_token, local_path)
            if file_token:
                return [{"file_token": file_token}]
        except Exception as e:
            utils.logger.warning(f"[视频上传] {note_id} 本地上传失败: {e}")

    # 回退: 从 URL 下载后上传
    if video_url and video_url.startswith("http"):
        try:
            import hashlib
            temp_dir = "/tmp/feishu_videos"
            os.makedirs(temp_dir, exist_ok=True)

            download_headers = {
                "Referer": "https://www.xiaohongshu.com/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }
            resp = httpx.get(video_url, timeout=60.0, follow_redirects=True, headers=download_headers)
            if resp.status_code != 200:
                utils.logger.warning(f"[视频上传] {note_id} URL下载失败 HTTP {resp.status_code}")
                return None

            # 检查大小
            if len(resp.content) > 20 * 1024 * 1024:
                utils.logger.warning(f"[视频上传] {note_id}: URL视频超过 20MB 限制，跳过")
                return None

            url_hash = hashlib.md5(video_url.encode()).hexdigest()[:12]
            file_path = os.path.join(temp_dir, f"{url_hash}.mp4")
            with open(file_path, "wb") as f:
                f.write(resp.content)

            file_token = client.upload_media(app_token, file_path)

            try:
                os.remove(file_path)
            except Exception:
                pass

            if file_token:
                return [{"file_token": file_token}]
        except Exception as e:
            utils.logger.warning(f"[视频上传] {note_id} URL上传失败: {e}")

    return None


# ==================== 补下载缺失图片 ====================

async def _download_missing_images(notes: List[Dict], max_notes: int = 0) -> int:
    """
    扫描笔记列表，对缺少本地图片的笔记重新获取新鲜URL并下载
    
    不重新爬作者页面，只通过 note_id + xsec_token 调 API 获取新鲜图片链接。
    
    Args:
        notes: 所有待处理的笔记列表（含 note_id, xsec_token, image_list 等字段）
        max_notes: 最多处理多少条笔记（0=不限）
        
    Returns:
        成功下载图片的笔记数
    """
    import random
    import pathlib
    import json as _json
    image_dir = "data/xhs/images"
    
    # 1. 筛选需要补图的笔记
    notes_need_images = []
    existing_ids = set()
    for note in notes:
        note_id = note.get("note_id", "")
        if not note_id:
            continue
        # 有图片URL但本地没有图片文件
        image_list_raw = note.get("image_list", "")
        if not image_list_raw:
            continue
        image_urls = _parse_image_urls(image_list_raw)
        if not image_urls:
            continue
        local_paths = _get_local_image_paths(note_id)
        if len(local_paths) >= len(image_urls):
            continue  # 本地图片已完整
        notes_need_images.append(note)
        existing_ids.add(note_id)
    
    # 1.1 加载历史失败列表（上次运行保存的），合并到待处理队列
    prev_fail_files = sorted(pathlib.Path(image_dir).glob("failed_notes_*.json"))
    loaded_from_prev = 0
    for ff in prev_fail_files:
        try:
            with open(ff, "r", encoding="utf-8") as f:
                prev_fails = _json.load(f)
            for pf in prev_fails:
                nid = pf.get("note_id", "")
                if nid and nid not in existing_ids:
                    # 检查本地是否已有图片（可能上次之后手动处理了）
                    local_paths = _get_local_image_paths(nid)
                    if not local_paths:
                        notes_need_images.append(pf)
                        existing_ids.add(nid)
                        loaded_from_prev += 1
            # 加载成功后删除旧文件，避免重复
            ff.unlink()
            utils.logger.info(f"[补图] 已加载历史失败记录: {ff.name}")
        except Exception as load_err:
            utils.logger.warning(f"[补图] 加载历史失败记录出错 {ff}: {load_err}")
    
    if loaded_from_prev > 0:
        utils.logger.info(f"[补图] 从历史失败记录中合并了 {loaded_from_prev} 条笔记")
    
    if not notes_need_images:
        utils.logger.info("[补图] 所有笔记的本地图片已完整，无需补下载")
        return 0
    
    if max_notes > 0:
        notes_need_images = notes_need_images[:max_notes]
    
    utils.logger.info(f"[补图] 发现 {len(notes_need_images)} 条笔记缺少本地图片，开始补下载...")
    
    # 2. 启动浏览器获取新鲜图片URL
    from playwright.async_api import async_playwright
    import config
    
    downloaded_count = 0
    pw = None
    browser_context = None
    xhs_client = None
    
    try:
        pw = await async_playwright().start()
        chromium = pw.chromium
        
        user_data_dir = os.path.join(
            os.getcwd(), "browser_data",
            config.USER_DATA_DIR % config.PLATFORM
        )
        os.makedirs(user_data_dir, exist_ok=True)
        
        browser_context = await chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            accept_downloads=True,
            headless=True,
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        
        # 添加反检测脚本
        stealth_js_path = os.path.join(os.getcwd(), "libs", "stealth.min.js")
        if os.path.exists(stealth_js_path):
            await browser_context.add_init_script(path=stealth_js_path)
        
        # 打开页面并创建 XHS 客户端
        context_page = await browser_context.new_page()
        await context_page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        
        from media_platform.xhs.client import XiaoHongShuClient
        cookie_str, cookie_dict = utils.convert_cookies(await browser_context.cookies())
        
        xhs_client = XiaoHongShuClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Cookie": cookie_str,
                "Origin": "https://www.xiaohongshu.com",
                "Referer": "https://www.xiaohongshu.com",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=context_page,
            cookie_dict=cookie_dict,
        )
        
        # 3. 准备假动作工具（复用主爬虫的反爬工具）
        from tools.anti_crawl_utils import (
            generate_random_wait, simulate_scroll, 
            simulate_mouse_move, simulate_input
        )
        from media_platform.xhs.help import get_search_id
        
        fake_action_keywords = ["美食", "旅行", "穿搭", "护肤", "健身", "摄影",
                                "宠物", "家居", "数码", "音乐", "电影", "书籍",
                                "咖啡", "甜点", "打卡", "探店", "学习", "考试"]
        fake_action_probability = getattr(config, "FAKE_ACTION_PROBABILITY", 0.15)
        
        async def _maybe_fake_action():
            """补图阶段的假动作：模拟页面浏览 + 随机搜索关键词"""
            if random.random() > fake_action_probability:
                return False
            try:
                # 模拟页面滚动和鼠标移动
                await simulate_scroll(context_page, config)
                await simulate_mouse_move(context_page, config)
                
                # 随机搜索一个热门关键词
                keyword = random.choice(fake_action_keywords)
                utils.logger.info(f"[补图-FakeAction] 搜索关键词 '{keyword}'")
                await xhs_client.get_note_by_keyword(
                    keyword=keyword,
                    search_id=get_search_id(),
                    page=1,
                    page_size=10
                )
                
                wait = generate_random_wait(2.0, 5.0, "lognormal")
                utils.logger.info(f"[补图-FakeAction] 完成，等待 {wait:.1f}s")
                await asyncio.sleep(wait)
                return True
            except Exception as e:
                utils.logger.debug(f"[补图-FakeAction] 执行失败: {e}")
                return False
        
        # 4. 单轮下载逻辑（供首轮和补录轮复用）
        async def _process_one_note(note: Dict, idx: int, total: int, round_name: str) -> bool:
            """处理单条笔记的图片下载，返回是否成功"""
            note_id = note.get("note_id", "")
            xsec_token = note.get("xsec_token", "")
            xsec_source = note.get("xsec_source", "pc_search")
            title = note.get("title", "")[:20]
            
            # 随机执行假动作（混淆 API 调用模式）
            await _maybe_fake_action()
            
            utils.logger.info(
                f"[{round_name}] [{idx+1}/{total}] "
                f"获取新鲜图片URL: {note_id} ({title}...)"
            )
            
            # 调API获取新鲜的笔记详情（含新鲜图片URL）
            note_detail = await xhs_client.get_note_by_id(
                note_id, xsec_source, xsec_token
            )
            
            if not note_detail:
                utils.logger.warning(f"[{round_name}] {note_id} 获取详情失败")
                return False
            
            # 提取新鲜的 image_list
            fresh_image_list = note_detail.get("image_list", [])
            if not fresh_image_list:
                utils.logger.warning(f"[{round_name}] {note_id} 无图片")
                return False
            
            # 对URL做默认URL优先
            for img in fresh_image_list:
                if img.get("url_default"):
                    img["url"] = img["url_default"]
            
            # 下载每张图片
            note_image_dir = os.path.join(image_dir, note_id)
            pathlib.Path(note_image_dir).mkdir(parents=True, exist_ok=True)
            
            pic_downloaded = 0
            for pic_num, pic in enumerate(fresh_image_list):
                url = pic.get("url")
                if not url:
                    continue
                save_path = os.path.join(note_image_dir, f"{pic_num}.jpg")
                if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                    pic_downloaded += 1
                    continue
                
                try:
                    content = await xhs_client.get_note_media(url)
                    if content:
                        with open(save_path, "wb") as f:
                            f.write(content)
                        pic_downloaded += 1
                    else:
                        utils.logger.warning(f"[{round_name}] {note_id} 第{pic_num}张下载返回空")
                except Exception as dl_err:
                    utils.logger.warning(f"[{round_name}] {note_id} 第{pic_num}张下载失败: {dl_err}")
                await asyncio.sleep(random.random() * 0.5)
            
            utils.logger.info(
                f"[{round_name}] [{idx+1}/{total}] "
                f"{note_id}: 下载 {pic_downloaded}/{len(fresh_image_list)} 张图片"
            )
            return True
        
        async def _run_download_round(
            notes_batch: List[Dict], round_name: str, 
            base_wait_min: float = 5.0, base_wait_max: float = 12.0,
            max_consecutive_fails: int = 7
        ) -> Tuple[int, List[Dict]]:
            """
            执行一轮下载，返回 (成功数, 失败笔记列表)
            """
            success_count = 0
            failed_notes: List[Dict] = []
            consecutive_fails = 0
            stopped_early = False
            
            for i, note in enumerate(notes_batch):
                try:
                    ok = await _process_one_note(note, i, len(notes_batch), round_name)
                    if ok:
                        success_count += 1
                        consecutive_fails = 0
                    else:
                        failed_notes.append(note)
                    
                    # 反爬等待
                    wait_time = random.uniform(base_wait_min, base_wait_max)
                    await asyncio.sleep(wait_time)
                    
                except Exception as e:
                    err_str = str(e)
                    note_id = note.get("note_id", "")
                    utils.logger.warning(f"[{round_name}] {note_id} 异常: {err_str}")
                    failed_notes.append(note)
                    consecutive_fails += 1
                    
                    # 检测 CAPTCHA / 限流 / 账号异常
                    is_anti_crawl = any(kw in err_str for kw in [
                        "CAPTCHA", "Verifytype", "300013", "300011",
                        "DataFetchError", "RetryError"
                    ])
                    
                    if is_anti_crawl:
                        if consecutive_fails <= 2:
                            wait = random.uniform(30, 60)
                            utils.logger.warning(
                                f"[{round_name}] 触发反爬(连续{consecutive_fails}次)，"
                                f"等待 {wait:.0f} 秒..."
                            )
                        elif consecutive_fails <= max_consecutive_fails - 1:
                            wait = random.uniform(60, 120)
                            utils.logger.warning(
                                f"[{round_name}] 反爬持续(连续{consecutive_fails}次)，"
                                f"等待 {wait:.0f} 秒..."
                            )
                        else:
                            # 剩余的笔记全部加入失败列表
                            remaining = notes_batch[i+1:]
                            failed_notes.extend(remaining)
                            utils.logger.error(
                                f"[{round_name}] 连续失败{consecutive_fails}次，"
                                f"停止本轮。已成功 {success_count} 条，"
                                f"剩余 {len(remaining)} 条移入失败列表。"
                                f"请重新登录后再运行。"
                            )
                            stopped_early = True
                            break
                        await asyncio.sleep(wait)
                        await _maybe_fake_action()
                    else:
                        await asyncio.sleep(5)
                    continue
            
            return success_count, failed_notes
        
        # ========== 首轮下载 ==========
        utils.logger.info(f"[补图] ===== 首轮开始: {len(notes_need_images)} 条笔记 =====")
        round1_ok, round1_failed = await _run_download_round(
            notes_need_images, "补图", 
            base_wait_min=5.0, base_wait_max=12.0,
            max_consecutive_fails=4
        )
        downloaded_count += round1_ok
        
        # ========== 失败补录（第二轮） ==========
        if round1_failed:
            utils.logger.info(
                f"[补图-补录] ===== 首轮结束: 成功 {round1_ok}, "
                f"失败 {len(round1_failed)} 条，准备补录... ====="
            )
            # 补录前长等待，让反爬冷却
            cooldown = random.uniform(60, 120)
            utils.logger.info(f"[补图-补录] 冷却等待 {cooldown:.0f} 秒...")
            await asyncio.sleep(cooldown)
            await _maybe_fake_action()
            
            # 第二轮使用更长的间隔
            round2_ok, round2_failed = await _run_download_round(
                round1_failed, "补图-补录",
                base_wait_min=10.0, base_wait_max=20.0,
                max_consecutive_fails=5
            )
            downloaded_count += round2_ok
            
            # 持久化最终失败列表
            if round2_failed:
                fail_record_path = os.path.join(
                    image_dir, 
                    f"failed_notes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                )
                fail_ids = [
                    {"note_id": n.get("note_id", ""), 
                     "xsec_token": n.get("xsec_token", ""),
                     "xsec_source": n.get("xsec_source", "pc_search"),
                     "title": n.get("title", "")}
                    for n in round2_failed
                ]
                try:
                    import json as _json
                    os.makedirs(image_dir, exist_ok=True)
                    with open(fail_record_path, "w", encoding="utf-8") as f:
                        _json.dump(fail_ids, f, ensure_ascii=False, indent=2)
                    utils.logger.warning(
                        f"[补图-补录] 仍有 {len(round2_failed)} 条失败，"
                        f"已保存到 {fail_record_path}，下次运行将自动重试。"
                    )
                except Exception as save_err:
                    utils.logger.error(f"[补图-补录] 保存失败列表异常: {save_err}")
            else:
                utils.logger.info("[补图-补录] 补录全部成功！")
        else:
            utils.logger.info("[补图] 首轮全部成功，无需补录。")
    
    except Exception as e:
        utils.logger.error(f"[补图] 浏览器启动/运行异常: {e}")
    
    finally:
        # 清理浏览器
        try:
            if browser_context:
                await browser_context.close()
        except Exception:
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass
    
    utils.logger.info(f"[补图] 完成: {downloaded_count}/{len(notes_need_images)} 条笔记图片下载成功")
    return downloaded_count


def _upload_note_images_to_feishu(
    client, app_token: str, note_id: str,
    image_urls: List[str], num_threads: int = 2
) -> Dict[str, Any]:
    """
    上传单个笔记的图片到飞书，优先使用本地文件，URL作为回退
    
    Args:
        client: FeishuBitableClient 实例
        app_token: 多维表格 token
        note_id: 笔记ID
        image_urls: 图片URL列表（作为回退）
        num_threads: 并发线程数
        
    Returns:
        {field_name: [{"file_token": "xxx"}]} 格式的映射
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    result = {}
    local_paths = _get_local_image_paths(note_id)
    
    # 构建上传任务: [(field_name, local_path_or_url, is_local), ...]
    tasks = []
    for i, url in enumerate(image_urls):
        field_name = f"图片{i + 1}"
        if i < len(local_paths):
            # 优先使用本地文件
            tasks.append((field_name, local_paths[i], True))
        else:
            # 回退到URL下载
            tasks.append((field_name, url, False))
    
    if not tasks:
        return result
    
    def upload_one(field_name, path_or_url, is_local):
        """上传单张图片"""
        try:
            if is_local:
                file_token = client.upload_media(app_token, path_or_url)
                if file_token:
                    return field_name, [{"file_token": file_token}]
            else:
                # URL回退（可能已过期）
                file_token = client.upload_image_from_url(app_token, path_or_url)
                if file_token:
                    return field_name, [{"file_token": file_token}]
        except Exception as e:
            utils.logger.warning(f"[图片上传] {field_name} 失败: {e}")
        finally:
            # 飞书上传API有限流(~100次/分)，每次上传后短暂等待
            time.sleep(0.3)
        return field_name, None
    
    # 多线程上传
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {
            executor.submit(upload_one, fn, p, il): fn
            for fn, p, il in tasks
        }
        for future in as_completed(futures):
            try:
                field_name, attachment = future.result()
                if attachment:
                    result[field_name] = attachment
            except Exception as e:
                fn = futures[future]
                utils.logger.warning(f"[图片上传] {fn} 线程异常: {e}")
    
    return result


# ==================== 飞书图片处理（旧版后处理方式，作为回退） ====================

def process_feishu_images(client, app_token: str, table_id: str,
                          image_field_names: List[str],
                          num_threads: int = 2):
    """
    多线程处理飞书多维表格中的图片字段：
    下载图片 → 上传飞书 → 更新记录（链接替换为附件缩略图）
    
    Args:
        client: FeishuBitableClient 实例
        app_token: 多维表格 token
        table_id: 数据表 ID
        image_field_names: 图片字段名列表 ["图片1", "图片2", ...]
        num_threads: 并发线程数
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not image_field_names:
        return

    # 1. 把图片字段改为附件类型（type=17）
    fields = client.list_fields(app_token, table_id)
    field_id_map = {}  # field_name → field_id
    for f in fields:
        if f and f.get("field_name") in image_field_names:
            fid = f.get("field_id", "")
            fname = f["field_name"]
            field_id_map[fname] = fid
            # 修改字段类型为附件
            try:
                client.update_field(app_token, table_id, fid, fname, 17)
                utils.logger.info(f"[图片处理] 字段 {fname} 改为附件类型")
            except Exception as e:
                utils.logger.warning(f"[图片处理] 修改字段类型失败 {fname}: {e}")

    # 2. 获取所有记录
    all_records = []
    page_token = ""
    while True:
        url = f"{client.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        try:
            data = client._request("GET", url, params=params)
            items = data.get("items", [])
            all_records.extend(items)
            if not data.get("has_more", False):
                break
            page_token = data.get("page_token", "")
        except Exception:
            break

    utils.logger.info(f"[图片处理] 共 {len(all_records)} 条记录, {len(image_field_names)} 个图片字段")

    # 3. 收集需要处理的任务 (record_id, field_name, image_url)
    tasks = []
    for record in all_records:
        record_id = record.get("record_id", "")
        record_fields = record.get("fields", {})
        for fname in image_field_names:
            value = record_fields.get(fname)
            if not value or not isinstance(value, str) or not value.startswith("http"):
                continue
            tasks.append((record_id, fname, value))

    if not tasks:
        utils.logger.info("[图片处理] 没有需要处理的图片")
        return

    utils.logger.info(f"[图片处理] 共 {len(tasks)} 张图片需要处理，使用 {num_threads} 线程")

    # 4. 多线程处理
    processed = 0
    failed = 0

    def process_one(record_id, field_name, image_url):
        """单张图片处理：下载→上传→更新"""
        try:
            file_token = client.upload_image_from_url(app_token, image_url)
            if not file_token:
                return False
            # 更新记录，把文本替换为附件
            client.update_record(app_token, table_id, record_id, {
                field_name: [{"file_token": file_token}]
            })
            return True
        except Exception as e:
            return False

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {}
        for record_id, fname, url in tasks:
            future = executor.submit(process_one, record_id, fname, url)
            futures[future] = (record_id, fname)

        for future in as_completed(futures):
            record_id, fname = futures[future]
            try:
                if future.result():
                    processed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

            # 进度日志
            total_done = processed + failed
            if total_done % 20 == 0 or total_done == len(tasks):
                utils.logger.info(
                    f"[图片处理] 进度: {total_done}/{len(tasks)} "
                    f"(成功 {processed}, 失败 {failed})"
                )

    utils.logger.info(f"[图片处理] 完成: 成功 {processed}, 失败 {failed}, 总计 {len(tasks)}")


# ==================== 飞书推送 ====================

def push_to_feishu(notes: List[Dict], field_defs: List[Dict],
                   app_id: str, app_secret: str,
                   bitable_name: str = "",
                   folder_token: str = "") -> Optional[str]:
    """
    将爬取数据推送到飞书多维表格
    
    Args:
        notes: 笔记数据列表
        field_defs: 字段定义
        app_id: 飞书 App ID
        app_secret: 飞书 App Secret
        bitable_name: 多维表格名称
        folder_token: 飞书文件夹 token
        
    Returns:
        多维表格 URL（成功时）
    """
    from tools.feishu_bitable import (
        FeishuBitableClient, build_feishu_fields,
        map_note_to_feishu_record
    )

    if not app_id or not app_secret:
        utils.logger.warning("[BatchCrawler] 飞书 App ID/Secret 未配置，跳过推送")
        return None

    if not notes:
        utils.logger.warning("[BatchCrawler] 没有数据需要推送")
        return None

    if not bitable_name:
        bitable_name = f"小红书爬取数据_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 提前读取图片模式配置
    feishu_image_mode = "link"
    feishu_image_threads = 2
    try:
        config_path_img = os.path.join("config", "anti_crawl_config.json")
        with open(config_path_img, "r", encoding="utf-8") as f:
            cfg_img = json.load(f)
        feishu_cfg = cfg_img.get("feishu", {})
        feishu_image_mode = feishu_cfg.get("image_mode", "link")
        feishu_image_threads = feishu_cfg.get("image_threads", 2)
    except Exception:
        pass

    is_image_mode = (feishu_image_mode == "image")
    utils.logger.info(f"[BatchCrawler] 飞书图片模式: {feishu_image_mode}")

    try:
        with FeishuBitableClient(app_id, app_secret) as client:
            # 1. 创建多维表格
            result = client.create_bitable(bitable_name, folder_token or None)
            app_token = result["app_token"]
            bitable_url = result["url"]
            utils.logger.info(f"[BatchCrawler] 创建多维表格: {bitable_name}")

            # 2. 按作者分组（保留原始note数据用于提取note_id）
            from collections import OrderedDict
            grouped: OrderedDict[str, List[Dict]] = OrderedDict()
            for note in notes:
                creator_name = note.get("_creator_name", note.get("nickname", "未知"))
                if creator_name not in grouped:
                    grouped[creator_name] = []
                grouped[creator_name].append(note)

            # 3. 构建所有记录，确定字段列表
            all_field_names = set()
            # 同时保存 records 和对应的原始 notes（用于图片上传时提取 note_id）
            all_grouped_records: OrderedDict[str, List[Dict]] = OrderedDict()
            all_grouped_raw_notes: OrderedDict[str, List[Dict]] = OrderedDict()
            for creator_name, creator_notes in grouped.items():
                records = []
                for note in creator_notes:
                    record = map_note_to_feishu_record(creator_name, note)
                    records.append(record)
                    all_field_names.update(record.get("fields", {}).keys())
                all_grouped_records[creator_name] = records
                all_grouped_raw_notes[creator_name] = creator_notes

            # 4. 确定字段顺序
            ordered_fields = ["账号名称", "内容类型", "标题", "正文", "标签", "链接",
                              "发布时间", "点赞数", "收藏数", "评论数", "互动量", "热门",
                              "视频附件", "视频脚本"]
            image_field_names = sorted(
                [f for f in all_field_names if f.startswith("图片")],
                key=lambda x: int(x.replace("图片", "") or "0")
            )
            ordered_fields.extend(image_field_names)
            for f in all_field_names:
                if f not in ordered_fields:
                    ordered_fields.append(f)

            url_fields = {"链接"}
            attachment_fields = {"视频附件"}
            date_fields = {"发布时间"}
            # image 模式：图片字段也创建为附件类型
            if is_image_mode:
                attachment_fields.update(image_field_names)

            ordered_with_serial = set(ordered_fields) | {"序号"}
            total_inserted = 0
            total_images_uploaded = 0
            total_images_failed = 0

            # 5. image 模式：在插入记录前上传图片
            if is_image_mode:
                # 统计总笔记数（有图片的）
                total_notes_with_images = sum(
                    1 for raw_notes in all_grouped_raw_notes.values()
                    for n in raw_notes
                    if _parse_image_urls(n.get("image_list", ""))
                )
                utils.logger.info(
                    f"[图片上传] 开始上传图片到飞书（{feishu_image_threads} 线程），"
                    f"{total_notes_with_images} 条笔记有图片，"
                    f"优先本地文件(data/xhs/images/)，URL作为回退"
                )
                note_progress = 0
                for creator_name, raw_notes in all_grouped_raw_notes.items():
                    records = all_grouped_records[creator_name]
                    for idx, (note, record) in enumerate(zip(raw_notes, records)):
                        note_id = note.get("note_id", "")
                        if not note_id:
                            continue
                        # 提取该笔记的图片URL列表
                        image_urls = _parse_image_urls(note.get("image_list", ""))
                        if not image_urls:
                            continue
                        note_progress += 1
                        local_paths = _get_local_image_paths(note_id)
                        source_desc = f"本地{len(local_paths)}张" if local_paths else "URL回退"
                        # 上传图片并获取 file_tokens
                        image_tokens = _upload_note_images_to_feishu(
                            client, app_token, note_id,
                            image_urls, feishu_image_threads
                        )
                        # 替换记录中的图片字段：URL文本 → 附件格式
                        for field_name, attachment in image_tokens.items():
                            record["fields"][field_name] = attachment
                            total_images_uploaded += 1
                        # 统计失败的图片
                        expected = len(image_urls)
                        uploaded = len(image_tokens)
                        if uploaded < expected:
                            total_images_failed += (expected - uploaded)
                        # 移除没有成功上传的图片字段（附件类型不能写入URL文本）
                        for i in range(1, len(image_urls) + 1):
                            fn = f"图片{i}"
                            if fn not in image_tokens and fn in record["fields"]:
                                # 未成功上传的，清空字段值（附件类型不接受字符串）
                                del record["fields"][fn]
                        # 进度日志（每5条或最后一条）
                        if note_progress % 5 == 0 or note_progress == total_notes_with_images:
                            utils.logger.info(
                                f"[图片上传] 进度: {note_progress}/{total_notes_with_images} 条笔记 "
                                f"({source_desc}, {uploaded}/{expected}张成功)"
                            )

                utils.logger.info(
                    f"[图片上传] 全部完成: {total_images_uploaded} 张成功, "
                    f"{total_images_failed} 张失败"
                )

                # 安全清理：移除所有记录中残留的图片URL文本
                # （附件类型字段不接受字符串，未成功上传的图片必须清空）
                cleaned = 0
                for creator_name, records in all_grouped_records.items():
                    for record in records:
                        fields = record.get("fields", {})
                        for key in list(fields.keys()):
                            if key.startswith("图片") and isinstance(fields[key], str):
                                del fields[key]
                                cleaned += 1
                if cleaned > 0:
                    utils.logger.info(f"[图片上传] 清理 {cleaned} 个未处理的图片字段（避免附件类型写入错误）")

            # 5.5 视频上传（image 模式下同时上传视频）
            if is_image_mode:
                total_videos = 0
                total_videos_uploaded = 0
                total_videos_skipped = 0

                for creator_name, raw_notes in all_grouped_raw_notes.items():
                    records = all_grouped_records[creator_name]
                    for note, record in zip(raw_notes, records):
                        note_id = note.get("note_id", "")
                        video_url = note.get("video_url", "")
                        note_type = note.get("type", "")

                        # 只处理有视频的笔记
                        if note_type != "video" and not video_url:
                            continue
                        if not note_id:
                            continue

                        total_videos += 1
                        attachment = _upload_note_video_to_feishu(
                            client, app_token, note_id, video_url
                        )
                        if attachment:
                            record["fields"]["视频附件"] = attachment
                            total_videos_uploaded += 1
                        else:
                            # 未上传成功，清空字段（附件类型不接受字符串）
                            if "视频附件" in record["fields"]:
                                del record["fields"]["视频附件"]
                            total_videos_skipped += 1

                        # 进度日志
                        done = total_videos_uploaded + total_videos_skipped
                        if done % 5 == 0 or done == total_videos:
                            utils.logger.info(
                                f"[视频上传] 进度: {done}/{total_videos} "
                                f"(成功 {total_videos_uploaded}, 跳过 {total_videos_skipped})"
                            )

                        # 飞书限流
                        time.sleep(0.5)

                if total_videos > 0:
                    utils.logger.info(
                        f"[视频上传] 全部完成: {total_videos_uploaded} 个成功, "
                        f"{total_videos_skipped} 个跳过 (共 {total_videos} 个视频笔记)"
                    )

                # 清理残留的视频URL文本（附件类型不接受字符串）
                video_cleaned = 0
                for creator_name, records in all_grouped_records.items():
                    for record in records:
                        fields = record.get("fields", {})
                        if "视频附件" in fields and isinstance(fields["视频附件"], str):
                            del fields["视频附件"]
                            video_cleaned += 1
                if video_cleaned > 0:
                    utils.logger.info(f"[视频上传] 清理 {video_cleaned} 个未处理的视频字段")

            # 6. 构建完整字段列表（创建新表时一次性传入）
            def _resolve_field_type(name):
                if name in url_fields: return 15
                if name in attachment_fields: return 17
                if name in date_fields: return 5
                return 1

            all_table_fields = []
            for field_name in ordered_fields:
                all_table_fields.append({"field_name": field_name, "type": _resolve_field_type(field_name)})

            # 7. 逐作者创建数据表并写入记录
            first_creator = True
            for creator_name, records in all_grouped_records.items():
                safe_name = creator_name[:100]
                utils.logger.info(f"[飞书] 创建数据表: {safe_name} ({len(records)} 条)")

                if first_creator:
                    # 用默认表（已有默认字段，需要逐个添加自定义字段）
                    tables = client.list_tables(app_token)
                    table_id = tables[0]["table_id"] if tables else client.create_table(app_token, safe_name, all_table_fields)
                    first_creator = False
                    
                    if tables:
                        # 重命名默认表为第一个作者名称
                        try:
                            client.rename_table(app_token, table_id, safe_name)
                            utils.logger.info(f"[飞书] 默认表已重命名为: {safe_name}")
                        except Exception as e:
                            utils.logger.warning(f"[飞书] 重命名默认表失败: {e}")
                        # 默认表需要逐个添加字段
                        for field_name in ordered_fields:
                            if field_name == "序号":
                                continue
                            try:
                                client.add_field(app_token, table_id, field_name, _resolve_field_type(field_name))
                            except Exception:
                                pass
                else:
                    # 创建新数据表，一次性传入所有字段
                    table_id = client.create_table(app_token, safe_name, all_table_fields)

                # 清理默认字段和空记录
                client.cleanup_default_fields_and_records(app_token, table_id, ordered_with_serial)

                # 填充序号
                for i, record in enumerate(records, 1):
                    record["fields"]["序号"] = str(i)

                # 写入
                inserted = client.batch_insert_records(app_token, table_id, records)
                total_inserted += inserted

                # 创建热门视图
                try:
                    fields = client.list_fields(app_token, table_id)
                    hot_field_id = ""
                    for f in fields:
                        if f and f.get("field_name") == "热门":
                            hot_field_id = f.get("field_id", "")
                            break
                    if hot_field_id:
                        client.create_view(
                            app_token, table_id,
                            view_name="🔥 热门作品",
                            filter_conditions=[{
                                "field_id": hot_field_id,
                                "operator": "isNotEmpty",
                            }]
                        )
                except Exception:
                    pass

            utils.logger.info(
                f"[BatchCrawler] 飞书写入完成: {total_inserted}/{len(notes)} 条, "
                f"{len(all_grouped_records)} 个作者, URL: {bitable_url}"
            )
            if is_image_mode:
                utils.logger.info(
                    f"[BatchCrawler] 图片上传: {total_images_uploaded} 张成功, "
                    f"{total_images_failed} 张失败"
                )
            else:
                utils.logger.info("[BatchCrawler] 图片模式: link（链接文本，跳过图片上传）")

            # ========== 8. 创建视频汇总表 ==========
            try:
                # 收集所有作者的视频记录
                video_records = []
                for creator_name, records in all_grouped_records.items():
                    for record in records:
                        fields = record.get("fields", {})
                        note_type = fields.get("内容类型", "")
                        # 视频类型 或 有视频附件的
                        has_video = (
                            note_type == "video"
                            or fields.get("视频附件")
                        )
                        if has_video:
                            # 复制 record，确保有账号名称字段
                            video_record = {"fields": dict(fields)}
                            video_record["fields"]["账号名称"] = creator_name
                            video_records.append(video_record)

                if video_records:
                    utils.logger.info(
                        f"[飞书] 创建视频汇总表: {len(video_records)} 条视频记录"
                    )
                    # 汇总表字段与分表相同
                    summary_table_id = client.create_table(
                        app_token, "视频汇总", all_table_fields
                    )
                    client.cleanup_default_fields_and_records(
                        app_token, summary_table_id, ordered_with_serial
                    )
                    # 填充序号
                    for i, record in enumerate(video_records, 1):
                        record["fields"]["序号"] = str(i)
                    # 写入
                    summary_inserted = client.batch_insert_records(
                        app_token, summary_table_id, video_records
                    )
                    utils.logger.info(
                        f"[飞书] 视频汇总表写入完成: {summary_inserted} 条"
                    )

                    # 添加"视频公网链接"文本字段
                    try:
                        client.add_field(
                            app_token, summary_table_id,
                            "视频公网链接", 15  # 15=超链接
                        )
                    except Exception:
                        pass

                    # 创建热门视图
                    try:
                        summary_fields = client.list_fields(app_token, summary_table_id)
                        hot_fid = ""
                        for sf in summary_fields:
                            if sf and sf.get("field_name") == "热门":
                                hot_fid = sf.get("field_id", "")
                                break
                        if hot_fid:
                            client.create_view(
                                app_token, summary_table_id,
                                view_name="🔥 热门作品",
                                filter_conditions=[{
                                    "field_id": hot_fid,
                                    "operator": "isNotEmpty",
                                }]
                            )
                    except Exception:
                        pass

                    # ========== 9. 生成视频临时公网下载链接 ==========
                    try:
                        utils.logger.info("[飞书] 开始生成视频临时公网下载链接...")
                        # 读回汇总表所有记录，提取视频附件的 file_token
                        summary_records = client.list_all_records(
                            app_token, summary_table_id
                        )
                        utils.logger.info(
                            f"[飞书] 读取汇总表记录: {len(summary_records)} 条"
                        )

                        # 收集所有视频附件的 file_token
                        record_file_map = {}  # {record_id: file_token}
                        all_file_tokens = []
                        for rec in summary_records:
                            record_id = rec.get("record_id", "")
                            fields = rec.get("fields", {})
                            video_attach = fields.get("视频附件")
                            if video_attach and isinstance(video_attach, list):
                                for att in video_attach:
                                    ft = att.get("file_token", "")
                                    if ft:
                                        record_file_map[record_id] = ft
                                        all_file_tokens.append(ft)
                                        break  # 只取第一个视频

                        if all_file_tokens:
                            utils.logger.info(
                                f"[飞书] 找到 {len(all_file_tokens)} 个视频附件，"
                                f"批量获取临时下载链接..."
                            )
                            # 批量获取临时 URL
                            token_url_map = client.batch_get_tmp_download_url(
                                all_file_tokens
                            )

                            # 批量更新记录，写入"视频公网链接"字段
                            update_records = []
                            for record_id, file_token in record_file_map.items():
                                tmp_url = token_url_map.get(file_token, "")
                                if tmp_url:
                                    update_records.append({
                                        "record_id": record_id,
                                        "fields": {
                                            "视频公网链接": {
                                                "link": tmp_url,
                                                "text": tmp_url
                                            }
                                        }
                                    })

                            if update_records:
                                updated = client.batch_update_records(
                                    app_token, summary_table_id, update_records
                                )
                                utils.logger.info(
                                    f"[飞书] 视频公网链接写入完成: "
                                    f"{updated}/{len(update_records)} 条"
                                )
                            else:
                                utils.logger.warning(
                                    "[飞书] 未能获取任何临时下载链接"
                                )
                        else:
                            utils.logger.info(
                                "[飞书] 汇总表中无视频附件，跳过链接生成"
                            )

                    except Exception as e:
                        utils.logger.warning(
                            f"[飞书] 生成视频公网链接失败（不影响已写入数据）: {e}"
                        )

                    # ========== 10. 自动提取视频脚本 ==========
                    try:
                        _run_video_script_extraction(
                            feishu_app_id, feishu_app_secret,
                            app_token, summary_table_id
                        )
                    except Exception as e:
                        utils.logger.warning(
                            f"[飞书] 视频脚本提取失败（不影响已写入数据）: {e}"
                        )

                else:
                    utils.logger.info("[飞书] 没有视频记录，跳过汇总表创建")

            except Exception as e:
                utils.logger.warning(f"[飞书] 创建视频汇总表失败（不影响分表数据）: {e}")

            return bitable_url

    except Exception as e:
        utils.logger.error(f"[BatchCrawler] 飞书推送失败: {e}")
        import traceback
        utils.logger.error(f"[BatchCrawler] 详细错误: {traceback.format_exc()}")
        return None


# ==================== 数据完整性校验 ====================

def _patch_feishu_tables(app_token: str, feishu_app_id: str, feishu_app_secret: str):
    """
    补写已有飞书表格：
    1. 将所有数据表的「发布时间」字段从文本改为日期类型，重写值为时间戳
    2. 对视频汇总表自动提取视频脚本
    """
    from tools.feishu_bitable import FeishuBitableClient

    if not feishu_app_id or not feishu_app_secret:
        print("[补写] 缺少飞书 App ID / App Secret，无法执行")
        return

    client = FeishuBitableClient(feishu_app_id, feishu_app_secret)
    try:
        tables = client.list_tables(app_token)
        print(f"[补写] 找到 {len(tables)} 个数据表")

        summary_table_id = ""

        for table_info in tables:
            table_id = table_info["table_id"]
            table_name = table_info.get("name", "")
            print(f"\n[补写] 处理表: {table_name} ({table_id})")

            if "视频汇总" in table_name:
                summary_table_id = table_id

            # --- 修复日期字段 ---
            fields = client.list_fields(app_token, table_id)
            date_field_id = ""
            for f in fields:
                if f.get("field_name") == "发布时间":
                    date_field_id = f.get("field_id", "")
                    current_type = f.get("type", 0)
                    break

            if not date_field_id:
                print(f"  [跳过] 未找到「发布时间」字段")
                continue

            if current_type == 5:
                print(f"  [跳过] 「发布时间」已是日期类型")
            else:
                # 读取所有记录，解析文本日期 → 时间戳
                records = client.list_all_records(app_token, table_id)
                print(f"  读取 {len(records)} 条记录")

                update_batch = []
                for rec in records:
                    rid = rec.get("record_id")
                    val = rec.get("fields", {}).get("发布时间", "")
                    if not val or not isinstance(val, str):
                        continue
                    try:
                        from datetime import datetime as _dt
                        dt = _dt.strptime(val.strip(), "%Y-%m-%d %H:%M:%S")
                        ms = int(dt.timestamp() * 1000)
                        update_batch.append({
                            "record_id": rid,
                            "fields": {"发布时间": ms}
                        })
                    except Exception:
                        pass

                # 先改字段类型为日期
                try:
                    client.update_field(app_token, table_id, date_field_id, "发布时间", 5)
                    print(f"  ✓ 字段类型已改为日期")
                except Exception as e:
                    print(f"  ✗ 修改字段类型失败: {e}")
                    continue

                # 再批量更新值
                if update_batch:
                    updated = client.batch_update_records(app_token, table_id, update_batch)
                    print(f"  ✓ 日期值已更新: {updated}/{len(update_batch)} 条")
                else:
                    print(f"  无需更新日期值")

        # --- 视频脚本提取 ---
        if summary_table_id:
            print(f"\n[补写] 开始提取视频脚本 (汇总表: {summary_table_id})")
            try:
                _run_video_script_extraction(
                    feishu_app_id, feishu_app_secret,
                    app_token, summary_table_id
                )
            except Exception as e:
                print(f"[补写] 视频脚本提取失败: {e}")
        else:
            print("\n[补写] 未找到「视频汇总」表，跳过脚本提取")

        print("\n[补写] 完成!")

    finally:
        client.close()


def _run_video_script_extraction(
    feishu_app_id: str, feishu_app_secret: str,
    app_token: str, summary_table_id: str,
):
    """读取配置并调用 VideoScriptExtractor 提取视频脚本"""
    config_path = os.path.join("config", "anti_crawl_config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    feishu_cfg = cfg.get("feishu", {})
    if not feishu_cfg.get("video_script_enabled", False):
        utils.logger.info("[飞书] 视频脚本提取未启用，跳过")
        return

    api_key = feishu_cfg.get("video_script_api_key", "")
    if not api_key:
        utils.logger.warning("[飞书] video_script_api_key 未配置，跳过视频脚本提取")
        return

    gateway_url = feishu_cfg.get("video_script_gateway_url", "https://ops-ai-gateway.yc345.tv/v1")
    model = feishu_cfg.get("video_script_model", "gemini-3-pro-preview")
    concurrency = int(feishu_cfg.get("video_script_concurrency", 5))

    utils.logger.info(
        f"[飞书] 开始自动提取视频脚本 (模型: {model}, 并发: {concurrency})..."
    )

    from tools.video_script_extractor import VideoScriptExtractor

    def _on_progress(current, total, title):
        utils.logger.info(f"[视频脚本] [{current}/{total}] {title}")

    with VideoScriptExtractor(
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        gemini_base_url=gateway_url,
        gemini_api_key=api_key,
        gemini_model=model,
        concurrency=concurrency,
    ) as extractor:
        result = extractor.extract_and_write(
            app_token=app_token,
            table_id=summary_table_id,
            skip_existing=True,
            on_progress=_on_progress,
        )

    if result.get("success"):
        utils.logger.info(
            f"[飞书] 视频脚本提取完成: "
            f"处理 {result.get('processed', 0)}, "
            f"跳过 {result.get('skipped', 0)}, "
            f"失败 {result.get('failed', 0)}"
        )
    else:
        utils.logger.warning(f"[飞书] 视频脚本提取失败: {result.get('message', '')}")


def _validate_single_creator(expected_uid: str, notes: list) -> bool:
    """
    校验单个作者的数据是否有效
    
    检查项:
    1. 数据不为空
    2. 数据中的 user_id 与预期的 creator uid 一致（允许少量不匹配）
    """
    if not notes:
        return False

    if not expected_uid:
        return len(notes) > 0

    # 统计 user_id 匹配率
    match_count = sum(1 for n in notes if n.get("user_id", "") == expected_uid)
    match_ratio = match_count / len(notes) if notes else 0

    # 至少 80% 的数据应该属于这个作者（允许少量合集/转发）
    return match_ratio >= 0.8


def _validate_crawled_data(
    task_dir: str,
    creators: list,
    progress,
) -> list:
    """
    批量爬取完成后，校验所有已完成作者的数据完整性
    
    检查项:
    1. user_id 不匹配：数据文件里的 user_id 和目标作者不一致
    2. 数据为空：标记为完成但没有数据文件
    3. 数据重复：多个不同作者的数据文件内容完全相同（session 失效的典型表现）
    
    Returns:
        需要补爬的作者列表
    """
    utils.logger.info("[DataValidator] 开始数据完整性校验...")

    invalid_creators = []
    data_fingerprints = {}  # uid → (note_count, first_note_id_set) 用于检测重复

    for creator in creators:
        creator_name = creator["name"]
        creator_url = creator["url"]
        user_id = ""
        if "/user/profile/" in creator_url:
            user_id = creator_url.split("/user/profile/")[1].split("?")[0]

        if not user_id:
            continue

        # 只校验已标记为"完成"的作者
        if not progress.is_completed(creator_url):
            continue

        contents_file = os.path.join(task_dir, f"creator_{user_id}_contents.json")
        reuse_file = os.path.join(task_dir, f"creator_{user_id}_reuse.json")

        # 收集该作者的所有数据
        notes = []
        for fpath in [contents_file, reuse_file]:
            if os.path.exists(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        notes.extend(data)
                except Exception:
                    pass

        # 检查1: 数据文件是否存在且非空
        if not notes:
            utils.logger.warning(
                f"[DataValidator] ⚠️ {creator_name} ({user_id}): 标记完成但无数据文件"
            )
            invalid_creators.append(creator)
            progress.remove_completed(creator_url)
            continue

        # 检查2: user_id 是否匹配
        if not _validate_single_creator(user_id, notes):
            actual_uids = {}
            for n in notes:
                uid = n.get("user_id", "unknown")
                actual_uids[uid] = actual_uids.get(uid, 0) + 1
            top_uid = max(actual_uids, key=actual_uids.get) if actual_uids else "?"
            utils.logger.warning(
                f"[DataValidator] ⚠️ {creator_name} ({user_id}): "
                f"user_id 不匹配！数据实际属于 {top_uid} ({actual_uids.get(top_uid, 0)}/{len(notes)} 条)"
            )
            invalid_creators.append(creator)
            progress.remove_completed(creator_url)
            # 移除脏数据文件
            for fpath in [contents_file, reuse_file]:
                if os.path.exists(fpath):
                    backup = fpath + ".invalid"
                    try:
                        os.rename(fpath, backup)
                    except Exception:
                        pass
            # 删除断点续爬进度
            progress_file = os.path.join("data", "xhs", "progress", f"creator_{user_id}_progress.json")
            if os.path.exists(progress_file):
                try:
                    os.remove(progress_file)
                except Exception:
                    pass
            continue

        # 检查3: 收集指纹用于检测重复
        note_ids = frozenset(n.get("note_id", "") for n in notes[:20])
        fingerprint = (len(notes), note_ids)
        data_fingerprints[user_id] = {
            "fingerprint": fingerprint,
            "creator": creator,
            "name": creator_name,
        }

    # 检查3: 检测重复数据（多个作者的数据指纹完全相同）
    fp_groups = {}
    for uid, info in data_fingerprints.items():
        fp_key = (info["fingerprint"][0], tuple(sorted(info["fingerprint"][1])))
        if fp_key not in fp_groups:
            fp_groups[fp_key] = []
        fp_groups[fp_key].append(info)

    for fp_key, group in fp_groups.items():
        if len(group) > 1:
            # 多个作者有完全相同的数据 → session 失效时的典型症状
            names = [g["name"] for g in group]
            utils.logger.warning(
                f"[DataValidator] ⚠️ 检测到重复数据！以下 {len(group)} 个作者数据完全相同: {names}"
            )
            for g in group:
                c = g["creator"]
                uid = ""
                if "/user/profile/" in c["url"]:
                    uid = c["url"].split("/user/profile/")[1].split("?")[0]
                if c not in invalid_creators:
                    invalid_creators.append(c)
                    progress.remove_completed(c["url"])
                    # 移除脏数据
                    for suffix in ["_contents.json", "_reuse.json"]:
                        fpath = os.path.join(task_dir, f"creator_{uid}{suffix}")
                        if os.path.exists(fpath):
                            try:
                                os.rename(fpath, fpath + ".invalid")
                            except Exception:
                                pass
                    # 删除断点续爬进度
                    pf = os.path.join("data", "xhs", "progress", f"creator_{uid}_progress.json")
                    if os.path.exists(pf):
                        try:
                            os.remove(pf)
                        except Exception:
                            pass

    if invalid_creators:
        utils.logger.info(
            f"[DataValidator] 校验完成: {len(invalid_creators)} 个作者需要补爬"
        )
    else:
        utils.logger.info("[DataValidator] ✅ 校验完成: 所有数据正常")

    return invalid_creators


# ==================== Session 健康检查 ====================

_last_known_expired_ws: str = ""


def _extract_web_session(cookie_str: str) -> str:
    """从 cookie 字符串中提取 web_session 值"""
    if not cookie_str:
        return ""
    if "web_session=" in cookie_str:
        return cookie_str.split("web_session=")[-1].split(";")[0].strip()
    if "=" not in cookie_str:
        return cookie_str.strip()
    return ""


def mark_session_expired():
    """爬取失败时调用，标记当前 config cookie 为已知过期"""
    global _last_known_expired_ws
    try:
        import config as app_config
        ws = _extract_web_session(getattr(app_config, "COOKIES", "") or "")
        if ws:
            _last_known_expired_ws = ws
    except Exception:
        pass


async def _check_session_health() -> bool:
    """
    检查当前 session 是否有效。
    检查顺序：
      1. config.COOKIES（支持 UI 手动更新的 web_session）
      2. 共享 cookie 文件 (xhs_cookies.json)
      3. 浏览器持久化上下文中的 cookie

    如果 config cookie 与上次失败时相同，视为已过期，跳过。
    """
    global _last_known_expired_ws
    try:
        import config as app_config

        # ===== 1. 检查 config.COOKIES（UI 手动更新写入此处）=====
        config_ws = _extract_web_session(getattr(app_config, "COOKIES", "") or "")
        if config_ws and len(config_ws) > 20:
            if config_ws == _last_known_expired_ws:
                utils.logger.debug(
                    "[SessionCheck] config web_session 与上次失败相同，跳过"
                )
            else:
                utils.logger.info(
                    f"[SessionCheck] config 中检测到新 web_session "
                    f"({config_ws[:8]}...{config_ws[-4:]}) ✓"
                )
                return True

        # ===== 2. 检查共享 cookie 文件 =====
        shared_cookie_file = os.path.join("data", "cookies", "xhs_cookies.json")
        if os.path.exists(shared_cookie_file):
            try:
                with open(shared_cookie_file, "r", encoding="utf-8") as f:
                    shared_data = json.load(f)
                shared_ws = shared_data.get("web_session", "")
                if shared_ws and len(shared_ws) > 20 and shared_ws != _last_known_expired_ws:
                    utils.logger.info(
                        f"[SessionCheck] 共享 cookie 文件中检测到 web_session "
                        f"({shared_ws[:8]}...{shared_ws[-4:]}) ✓"
                    )
                    return True
            except Exception:
                pass

        # ===== 3. 检查浏览器持久化上下文 =====
        from playwright.async_api import async_playwright

        browser_data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "browser_data",
            app_config.USER_DATA_DIR % "xhs",
        )

        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=browser_data_dir,
                headless=True,
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )

            cookies = await ctx.cookies()
            session_cookie = next(
                (c for c in cookies if c.get("name") == "web_session"), None
            )
            await ctx.close()

            if not session_cookie:
                utils.logger.warning(
                    "[SessionCheck] 未检测到有效 web_session"
                    "（config / 共享文件 / 浏览器均无）"
                )
                return False

            browser_ws = session_cookie.get("value", "")
            if browser_ws == _last_known_expired_ws:
                utils.logger.warning(
                    "[SessionCheck] 浏览器 web_session 与上次失败相同"
                )
                return False

            utils.logger.info("[SessionCheck] 浏览器中检测到 web_session ✓")
            return True

        except Exception as e:
            utils.logger.warning(f"[SessionCheck] 浏览器检查失败: {e}")
            return False
        finally:
            try:
                await pw.stop()
            except Exception:
                pass

    except Exception as e:
        utils.logger.warning(f"[SessionCheck] 初始化失败: {e}")
        return False


async def _wait_for_session_recovery(
    max_wait_minutes: int = 60,
    check_interval_seconds: int = 120,
) -> bool:
    """
    等待 session 恢复（由 session_keeper 或手动重新登录修复）

    Args:
        max_wait_minutes: 最大等待时间（分钟）
        check_interval_seconds: 检查间隔（秒）

    Returns:
        True=session 已恢复, False=等待超时
    """
    max_wait_seconds = max_wait_minutes * 60
    elapsed = 0

    utils.logger.info(
        f"[SessionCheck] ⏸ 爬虫暂停，等待 session 恢复（最长等待 {max_wait_minutes} 分钟）..."
    )
    utils.logger.info(
        f"[SessionCheck] 请通过 Web 界面重新扫码登录，或等待 session_keeper 自动恢复"
    )

    while elapsed < max_wait_seconds:
        await asyncio.sleep(check_interval_seconds)
        elapsed += check_interval_seconds

        minutes_elapsed = elapsed / 60
        minutes_remaining = (max_wait_seconds - elapsed) / 60

        utils.logger.info(
            f"[SessionCheck] 第 {int(minutes_elapsed)} 分钟，检查 session..."
        )

        if await _check_session_health():
            utils.logger.info(
                f"[SessionCheck] ✅ Session 已恢复！等待了 {int(minutes_elapsed)} 分钟，继续爬取"
            )
            return True
        else:
            utils.logger.warning(
                f"[SessionCheck] Session 仍然无效，继续等待（剩余 {int(minutes_remaining)} 分钟）..."
            )

    utils.logger.error(
        f"[SessionCheck] ❌ 等待 {max_wait_minutes} 分钟后 session 仍未恢复"
    )
    return False


# ==================== 主流程 ====================

async def run_batch_crawl(
    excel_path: str = "redbookaccontidandresult.xlsx",
    skip_feishu: bool = False,
    feishu_app_id: str = "",
    feishu_app_secret: str = "",
    feishu_folder_token: str = "",
    resume: bool = True,
    max_notes_per_creator: int = 3000,
    enable_comments: bool = False,
    min_interaction: int = 50,
    export_format: str = "excel",
    export_dir: str = "data/export",
    limit: int = 0,
    patch_only: bool = False,
    patch_limit: int = 0,
    force_recrawl: bool = False,
    crawl_mode_override: str = "",
) -> None:
    """
    批量爬取主流程
    
    Args:
        excel_path: Excel 文件路径
        skip_feishu: 是否跳过飞书写入
        feishu_app_id: 飞书 App ID
        feishu_app_secret: 飞书 App Secret
        feishu_folder_token: 飞书文件夹 token
        resume: 是否启用断点续爬（跳过已完成的作者）
        max_notes_per_creator: 每个作者最多爬取多少条
        enable_comments: 是否爬取评论
        min_interaction: 互动量过滤阈值（点赞+评论+收藏），0=不过滤
        export_format: 导出格式 (excel/json/csv)
        export_dir: 导出文件目录
    """
    import config
    from config.anti_crawl_loader import apply_anti_crawl_config
    from tools.async_file_writer import AsyncFileWriter

    utils.logger.info("=" * 60)
    utils.logger.info("[BatchCrawler] 批量爬取开始")
    utils.logger.info(f"  互动量过滤: {'关闭' if min_interaction <= 0 else f'>= {min_interaction}'}")
    utils.logger.info(f"  导出格式: {export_format}")
    utils.logger.info("=" * 60)

    # 1. 读取 Excel
    creators, field_defs = load_creators_from_excel(excel_path)
    if not creators:
        utils.logger.error("[BatchCrawler] Excel 中没有作者数据")
        return

    # 限制作者数量
    if limit > 0:
        creators = creators[:limit]
        utils.logger.info(f"[BatchCrawler] 限制爬取前 {limit} 个作者")

    utils.logger.info(f"[BatchCrawler] 共 {len(creators)} 个作者待爬取")

    # 2. 读取任务ID
    task_id = ""
    try:
        config_path_tid = os.path.join("config", "anti_crawl_config.json")
        with open(config_path_tid, "r", encoding="utf-8") as f:
            cfg_tid = json.load(f)
        task_id = cfg_tid.get("batch_crawl", {}).get("task_id", "")
    except Exception:
        pass
    if not task_id:
        task_id = datetime.now().strftime("task_%Y%m%d")

    utils.logger.info(f"[BatchCrawler] 任务ID: {task_id}")

    # 初始化进度管理（按任务ID区分）
    progress_file = f"data/batch_progress_{task_id}.json"
    progress = BatchProgress(progress_file=progress_file, task_id=task_id)
    if not resume or force_recrawl:
        progress.reset()
        if force_recrawl:
            # 同时清理单个作者的断点续爬进度，确保从头爬取
            creator_progress_dir = os.path.join("data", "xhs", "progress")
            if os.path.exists(creator_progress_dir):
                import glob
                cleared = 0
                for pf in glob.glob(os.path.join(creator_progress_dir, "creator_*_progress.json")):
                    try:
                        os.remove(pf)
                        cleared += 1
                    except Exception:
                        pass
                if cleared:
                    utils.logger.info(f"[BatchCrawler] --force-recrawl: 已清理 {cleared} 个作者的断点续爬进度")
            utils.logger.info("[BatchCrawler] --force-recrawl: 所有进度已重置，将从头爬取每个作者")

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    progress.set_session(session_id)

    # 3. 加载基础配置
    apply_anti_crawl_config(config)

    # 覆盖关键配置
    config.PLATFORM = "xhs"
    config.CRAWLER_TYPE = "creator"
    config.CRAWLER_MAX_NOTES_COUNT = max_notes_per_creator
    config.ENABLE_GET_COMMENTS = enable_comments

    # force_recrawl 模式：强制启用图片下载，无论飞书配置
    if force_recrawl:
        config.ENABLE_GET_MEIDAS = True
        utils.logger.info(
            "[BatchCrawler] --force-recrawl: 强制启用图片下载 + 禁用历史复用"
        )

    # 读取飞书图片模式，image模式时自动启用图片下载
    feishu_image_mode = "link"
    try:
        config_path_img = os.path.join("config", "anti_crawl_config.json")
        with open(config_path_img, "r", encoding="utf-8") as f:
            cfg_img = json.load(f)
        feishu_image_mode = cfg_img.get("feishu", {}).get("image_mode", "link")
    except Exception:
        pass

    if feishu_image_mode == "image":
        config.ENABLE_GET_MEIDAS = True
        utils.logger.info("[BatchCrawler] 飞书图片模式=image，自动启用图片下载(ENABLE_GET_MEIDAS=True)")
    else:
        if not force_recrawl:
            utils.logger.info(f"[BatchCrawler] 飞书图片模式={feishu_image_mode}，不下载图片")

    # 4. 读取复用和过滤配置
    reuse_history = True if not force_recrawl else False
    crawl_mode = "full"
    date_start = ""
    date_end = ""
    try:
        config_path_r = os.path.join("config", "anti_crawl_config.json")
        with open(config_path_r, "r", encoding="utf-8") as f:
            cfg_r = json.load(f)
        batch_r = cfg_r.get("batch_crawl", {})
        if not force_recrawl:
            reuse_history = batch_r.get("reuse_history", True)
        crawl_mode = batch_r.get("crawl_mode", "full")
        date_start = batch_r.get("date_start", "")
        date_end = batch_r.get("date_end", "")
    except Exception:
        pass

    if crawl_mode_override:
        crawl_mode = crawl_mode_override
        utils.logger.info(f"[BatchCrawler] 命令行指定爬取模式: {crawl_mode}")

    _MODE_LABELS = {
        "full": "全量爬取（爬取所有内容）",
        "date_range": f"日期范围爬取（{date_start} ~ {date_end}）",
        "incremental": "增量更新（已完成作者仅爬取新内容+更新互动量）",
    }
    utils.logger.info(f"[BatchCrawler] 当前模式: {_MODE_LABELS.get(crawl_mode, crawl_mode)}")

    # 5. 注入日期提前停止配置到 config（供 core.py 使用）
    if crawl_mode == "date_range" and date_start:
        config.DATE_EARLY_STOP_ENABLED = True
        config.DATE_EARLY_STOP_THRESHOLD = 5  # 连续5条超出范围后停止
        config.CRAWL_DATE_START = date_start
        utils.logger.info(f"[BatchCrawler] 日期提前停止已启用: 连续5条早于 {date_start} 时自动跳过")
    elif crawl_mode == "incremental":
        utils.logger.info(
            "[BatchCrawler] 增量更新模式: 已完成的作者将只爬取上次爬取日期之后的新内容"
        )
        config.DATE_EARLY_STOP_ENABLED = False  # 每个作者的日期由 get_last_crawl_date 动态设置
    else:
        config.DATE_EARLY_STOP_ENABLED = False

    # 任务数据目录
    task_dir = os.path.join("data", "xhs", "json", task_id)
    os.makedirs(task_dir, exist_ok=True)


    # 5a. 进度文件预检：检测跨作者 crawled_ids 重复等异常
    creator_uids = []
    for c in creators:
        u = c.get("url", "")
        if "/user/profile/" in u:
            creator_uids.append(u.split("/user/profile/")[1].split("?")[0])
    preflight_validate_progress(platform="xhs", creator_ids=creator_uids or None)

    # 注册优雅退出信号处理
    _install_signal_handlers()
    _GracefulShutdown.current_task_dir = task_dir
    _GracefulShutdown.current_progress = progress
    _GracefulShutdown.shutdown_requested = False

    # 5. 统计
    total = len(creators)
    skipped = 0
    reused = 0
    success = 0
    failed = 0

    # patch_only 模式：跳过爬取，直接进入汇总+补图
    if patch_only:
        utils.logger.info("[BatchCrawler] --patch-only 模式，跳过爬取，直接补图...")
        total = len(creators)
        skipped = total
        # 跳到汇总阶段（下面的 for 循环不会执行）
        creators = []

    # 保存完整作者列表（校验阶段需要）
    creators_original = list(creators) if not patch_only else []

    # 5b. 流水线模式：每个作者完成后立即写入飞书
    pipeline_writer = None
    pipeline_mode = False
    if not skip_feishu and not patch_only and feishu_app_id and feishu_app_secret:
        try:
            _cfg_path = os.path.join("config", "anti_crawl_config.json")
            with open(_cfg_path, "r", encoding="utf-8") as _f:
                _cfg_pl = json.load(_f)
            pipeline_mode = _cfg_pl.get("feishu", {}).get("pipeline_mode", False)
        except Exception:
            pass
        if pipeline_mode:
            from tools.pipeline_feishu_writer import PipelineFeishuWriter
            pipeline_writer = PipelineFeishuWriter(
                feishu_app_id=feishu_app_id,
                feishu_app_secret=feishu_app_secret,
                folder_token=feishu_folder_token,
            )
            utils.logger.info("[BatchCrawler] 流水线模式已启用: 每完成一个作者立即写入飞书")

    # 6. 逐个作者处理
    for idx, creator in enumerate(creators, 1):
        creator_name = creator["name"]
        creator_url = creator["url"]

        # 提取 user_id
        user_id = ""
        if "/user/profile/" in creator_url:
            user_id = creator_url.split("/user/profile/")[1].split("?")[0]

        # 每作者开始时恢复日期停止配置（增量模式会在下面覆盖）
        if crawl_mode == "date_range" and date_start:
            config.DATE_EARLY_STOP_ENABLED = True
            config.CRAWL_DATE_START = date_start
        else:
            config.DATE_EARLY_STOP_ENABLED = False

        # 永久跳过（作者隐藏所有作品等）
        if progress.is_skipped(creator_url):
            reason = progress._skipped.get(creator_url, "")
            utils.logger.info(
                f"[BatchCrawler] [{idx}/{total}] 跳过（{reason}）: {creator_name}"
            )
            skipped += 1
            continue

        # 断点续爬：跳过当前任务已完成的（增量模式除外）
        incremental_this_creator = False
        if resume and progress.is_completed(creator_url):
            if crawl_mode == "incremental":
                last_date = get_last_crawl_date_for_creator(task_dir, user_id)
                if not last_date:
                    utils.logger.info(
                        f"[BatchCrawler] [{idx}/{total}] [增量] 无历史数据，跳过: {creator_name}"
                    )
                    skipped += 1
                    continue
                incremental_this_creator = True
                config.DATE_EARLY_STOP_ENABLED = True
                config.DATE_EARLY_STOP_THRESHOLD = 5
                config.CRAWL_DATE_START = last_date
                utils.logger.info(
                    f"[BatchCrawler] [{idx}/{total}] [增量更新] {creator_name}，"
                    f"只爬 {last_date} 之后的新内容"
                )
            else:
                utils.logger.info(
                    f"[BatchCrawler] [{idx}/{total}] 跳过已完成: {creator_name}"
                )
                skipped += 1
                continue

        # 部分完成的作者：有部分数据但需要重新完整爬取
        if progress.is_partial(creator_url):
            utils.logger.info(
                f"[BatchCrawler] [{idx}/{total}] 重新爬取（上次中止未完成）: {creator_name}"
            )
            progress.clear_partial(creator_url)

        # 提前记录当前作者（供优雅退出时使用，即使在 session 等待期间也能识别）
        _GracefulShutdown.current_creator_name = creator_name
        _GracefulShutdown.current_creator_url = creator_url
        _GracefulShutdown.current_user_id = user_id

        # ========== 复用历史数据检查 ==========（增量模式不复用，直接爬新内容）
        if reuse_history and user_id and not incremental_this_creator:
            history_status = check_creator_history_status(user_id)

            if history_status == "completed":
                # 之前已爬完，直接复用符合条件的数据
                utils.logger.info(f"[BatchCrawler] [{idx}/{total}] 复用历史数据: {creator_name}")
                reuse_notes = scan_history_for_creator(
                    user_id, creator_name, crawl_mode, date_start, date_end, min_interaction
                )
                save_reuse_data(task_dir, user_id, reuse_notes)
                progress.mark_completed(creator_url)
                reused += 1
                utils.logger.info(
                    f"[BatchCrawler] [{idx}/{total}] 复用完成: {creator_name} "
                    f"({len(reuse_notes)} 条符合条件)"
                )
                if pipeline_writer and reuse_notes:
                    try:
                        pipeline_writer.write_creator(creator_name, reuse_notes)
                    except Exception as _pw_e:
                        utils.logger.warning(f"[Pipeline] 写入失败: {creator_name} - {_pw_e}")
                continue

            elif history_status == "partial":
                # 有部分数据，先复用再续爬
                utils.logger.info(f"[BatchCrawler] [{idx}/{total}] 部分复用+续爬: {creator_name}")
                reuse_notes = scan_history_for_creator(
                    user_id, creator_name, crawl_mode, date_start, date_end, min_interaction
                )
                save_reuse_data(task_dir, user_id, reuse_notes)
                utils.logger.info(
                    f"  已复用 {len(reuse_notes)} 条历史数据，继续爬取剩余..."
                )
                # 不 continue，继续下面的爬取流程

        # ========== Session 健康检查 ==========
        session_ok = await _check_session_health()
        if not session_ok:
            utils.logger.warning(
                f"[BatchCrawler] [{idx}/{total}] Session 失效，暂停爬取并等待恢复..."
            )
            recovered = await _wait_for_session_recovery(
                max_wait_minutes=60,
                check_interval_seconds=120,
            )
            if not recovered:
                utils.logger.error(
                    f"[BatchCrawler] [{idx}/{total}] Session 等待超时（60分钟），"
                    f"跳过 {creator_name}，标记为失败"
                )
                progress.mark_failed(creator_url, "Session 过期且等待恢复超时")
                failed += 1
                continue

        # ========== 爬取 ==========
        if _GracefulShutdown.shutdown_requested:
            utils.logger.info(f"[BatchCrawler] [{idx}/{total}] 收到退出信号，停止处理后续作者")
            break

        utils.logger.info("=" * 60)
        utils.logger.info(
            f"[BatchCrawler] [{idx}/{total}] 开始爬取: {creator_name}"
        )
        utils.logger.info(f"  URL: {creator_url}")
        utils.logger.info("=" * 60)

        crawler = None
        try:
            # 重置会话时间戳（每个作者一个新文件）
            AsyncFileWriter.reset_session_timestamp()
            session_ts = AsyncFileWriter._session_timestamp

            # 记录当前爬取状态（供优雅退出时使用）
            _GracefulShutdown.current_creator_name = creator_name
            _GracefulShutdown.current_creator_url = creator_url
            _GracefulShutdown.current_user_id = user_id
            _GracefulShutdown.current_session_ts = session_ts

            # 设置当前作者
            config.XHS_CREATOR_ID_LIST = [creator_url]

            # 重置统计
            from tools.crawl_statistics import reset_statistics
            reset_statistics(platform="xhs")

            # 供爬虫「老作品只更新互动数据」时加载已有数据
            config.XHS_TASK_DIR = task_dir
            config.XHS_CURRENT_USER_ID = user_id

            # 创建并运行爬虫
            from media_platform.xhs import XiaoHongShuCrawler
            from media_platform.xhs.exception import SessionExpiredError
            from var import crawler_type_var
            crawler_type_var.set("creator")

            crawler = XiaoHongShuCrawler()
            await crawler.start()

            # 新爬取的数据保存到任务目录
            data_dir = os.path.join("data", "xhs", "json")
            new_notes = collect_crawled_data(
                data_dir, session_ts, creator_name, min_interaction=0,
                user_id=user_id
            )
            contents_file = os.path.join(task_dir, f"creator_{user_id}_contents.json")
            if incremental_this_creator:
                # 增量模式：合并新旧数据（无新内容时也保持已完成状态）
                existing: Dict[str, Dict] = {}
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
                                        existing[nid] = it
                        except Exception:
                            pass
                for n in new_notes:
                    nid = n.get("note_id")
                    if nid:
                        if nid in existing:
                            existing[nid] = _merge_note_metadata(existing[nid], n)
                        else:
                            existing[nid] = n
                merged = sorted(existing.values(), key=lambda x: _safe_int(x.get("time", 0)), reverse=True)
                with open(contents_file, "w", encoding="utf-8") as f:
                    json.dump(merged, f, ensure_ascii=False, indent=2)
                if new_notes:
                    utils.logger.info(f"  增量合并: 新增 {len(new_notes)} 条，合计 {len(merged)} 条")
                else:
                    utils.logger.info(f"  增量: 无新内容，保持原有 {len(merged)} 条")
            elif new_notes:
                with open(contents_file, "w", encoding="utf-8") as f:
                    json.dump(new_notes, f, ensure_ascii=False, indent=2)
                utils.logger.info(f"  新爬 {len(new_notes)} 条保存到 {contents_file}")

            # 合并：如果之前有 partial 的旧数据，与新数据合并（按 note_id 去重）
            if not incremental_this_creator:
                _merge_partial_data(task_dir, user_id, new_notes)

            # 校验磁盘上是否有实际数据，防止 0 数据被标记为完成
            _has_data = False
            for _suffix in ["_contents.json", "_reuse.json"]:
                _fp = os.path.join(task_dir, f"creator_{user_id}{_suffix}")
                if os.path.exists(_fp):
                    try:
                        with open(_fp, "r", encoding="utf-8") as _f:
                            _arr = json.load(_f)
                        if isinstance(_arr, list) and len(_arr) > 0:
                            _has_data = True
                            break
                    except Exception:
                        pass

            if _has_data:
                progress.mark_completed(creator_url)
                _GracefulShutdown.reset_current()
                success += 1
                utils.logger.info(
                    f"[BatchCrawler] [{idx}/{total}] 完成: {creator_name}"
                )
                # 流水线模式：立即写入飞书
                if pipeline_writer:
                    try:
                        _pl_notes = _load_creator_notes_for_pipeline(
                            task_dir, user_id, crawl_mode,
                            date_start, date_end, min_interaction,
                        )
                        if _pl_notes:
                            pipeline_writer.write_creator(creator_name, _pl_notes)
                    except Exception as _pw_e:
                        utils.logger.warning(
                            f"[Pipeline] 写入失败: {creator_name} - {_pw_e}"
                        )
            else:
                # API 成功返回但 0 笔记 → 作者隐藏了所有作品，永久跳过
                progress.mark_skipped(creator_url, "作者隐藏所有作品（API返回0笔记）")
                _GracefulShutdown.reset_current()
                skipped += 1
                utils.logger.warning(
                    f"[BatchCrawler] [{idx}/{total}] 作者隐藏所有作品，永久跳过: {creator_name}"
                )

        except SessionExpiredError as se:
            # ========== Session 失效熔断：暂停等待恢复 ==========
            utils.logger.error(
                f"[BatchCrawler] [{idx}/{total}] ⚡ Session 失效熔断: {creator_name} - {se}"
            )
            
            # 保存已爬到的部分数据（如果有的话）
            try:
                data_dir = os.path.join("data", "xhs", "json")
                partial_notes = collect_crawled_data(
                    data_dir, session_ts, creator_name, min_interaction=0,
                    user_id=user_id
                )
                if partial_notes:
                    contents_file = os.path.join(task_dir, f"creator_{user_id}_contents.json")
                    with open(contents_file, "w", encoding="utf-8") as f:
                        json.dump(partial_notes, f, ensure_ascii=False, indent=2)
                    utils.logger.info(
                        f"[BatchCrawler] [{idx}/{total}] 已保存 {len(partial_notes)} 条有效数据"
                        f"（session 断前爬到的部分）"
                    )
            except Exception as save_err:
                utils.logger.warning(f"[BatchCrawler] 保存部分数据失败: {save_err}")
            
            # 标记为失败（需重爬）
            progress.mark_failed(creator_url, f"Session 失效熔断: {se}")
            failed += 1
            
            # 关闭当前浏览器
            if crawler:
                try:
                    if getattr(crawler, "cdp_manager", None):
                        await crawler.cdp_manager.cleanup(force=True)
                        crawler.cdp_manager = None
                    elif getattr(crawler, "browser_context", None):
                        await crawler.browser_context.close()
                except Exception:
                    pass
                try:
                    import subprocess
                    import platform as _platform
                    if _platform.system() == "Linux":
                        subprocess.run(["pkill", "-f", "chromium"], capture_output=True, timeout=5)
                    await asyncio.sleep(2)
                except Exception:
                    pass
                crawler = None
            
            # 等待 session 恢复
            utils.logger.info(
                f"[BatchCrawler] [{idx}/{total}] 等待 session 恢复..."
            )
            recovered = await _wait_for_session_recovery(
                max_wait_minutes=60,
                check_interval_seconds=120,
            )
            if not recovered:
                utils.logger.error(
                    f"[BatchCrawler] [{idx}/{total}] Session 等待超时（60分钟），"
                    f"停止后续爬取"
                )
                break  # 超时直接停止整个批次
            
            utils.logger.info(
                f"[BatchCrawler] [{idx}/{total}] Session 已恢复，继续下一个作者"
            )
            # 继续 for 循环处理下一个作者（当前作者标记为 failed，下次可重爬）
            continue

        except Exception as e:
            failed += 1
            error_msg = str(e)
            progress.mark_failed(creator_url, error_msg)
            utils.logger.error(
                f"[BatchCrawler] [{idx}/{total}] 失败: {creator_name} - {error_msg}"
            )

        finally:
            # 关闭浏览器
            if crawler:
                try:
                    if getattr(crawler, "cdp_manager", None):
                        await crawler.cdp_manager.cleanup(force=True)
                        crawler.cdp_manager = None
                    elif getattr(crawler, "browser_context", None):
                        await crawler.browser_context.close()
                    utils.logger.info("[BatchCrawler] 浏览器已关闭")
                except Exception as close_err:
                    utils.logger.warning(f"[BatchCrawler] 关闭浏览器异常: {close_err}")
                try:
                    import subprocess
                    import platform as _platform
                    if _platform.system() == "Darwin":
                        subprocess.run(["pkill", "-f", "Google Chrome Dev"], capture_output=True, timeout=5)
                    elif _platform.system() == "Linux":
                        subprocess.run(["pkill", "-f", "chromium"], capture_output=True, timeout=5)
                    # Windows: 不使用 pkill
                    await asyncio.sleep(2)
                except Exception as kill_err:
                    utils.logger.debug(f"[BatchCrawler] 清理浏览器进程: {kill_err}")

        # 作者之间休息一下
        if idx < total:
            wait = 10
            utils.logger.info(f"[BatchCrawler] 等待 {wait} 秒后继续下一个作者...")
            await asyncio.sleep(wait)

    # 7. 汇总
    utils.logger.info("=" * 60)
    utils.logger.info("[BatchCrawler] 批量爬取完成!")
    utils.logger.info(f"  总计: {total} | 成功: {success} | 复用: {reused} | 失败: {failed} | 跳过: {skipped}")
    utils.logger.info("=" * 60)

    # 7.5 数据完整性校验 + 自动补爬
    if not patch_only:
        invalid_creators = _validate_crawled_data(task_dir, creators_original, progress)
        if invalid_creators:
            utils.logger.info("=" * 60)
            utils.logger.info(f"[DataValidator] 发现 {len(invalid_creators)} 个作者数据异常，自动补爬...")
            utils.logger.info("=" * 60)

            recrawl_success = 0
            recrawl_failed = 0
            for ridx, rc in enumerate(invalid_creators, 1):
                rc_name = rc["name"]
                rc_url = rc["url"]
                rc_uid = ""
                if "/user/profile/" in rc_url:
                    rc_uid = rc_url.split("/user/profile/")[1].split("?")[0]

                utils.logger.info(f"[DataValidator] 补爬 [{ridx}/{len(invalid_creators)}]: {rc_name}")

                crawler = None
                try:
                    AsyncFileWriter.reset_session_timestamp()
                    session_ts = AsyncFileWriter._session_timestamp
                    config.XHS_CREATOR_ID_LIST = [rc_url]

                    from tools.crawl_statistics import reset_statistics
                    reset_statistics(platform="xhs")

                    from media_platform.xhs import XiaoHongShuCrawler
                    from media_platform.xhs.exception import SessionExpiredError as _SSE
                    from var import crawler_type_var
                    crawler_type_var.set("creator")

                    crawler = XiaoHongShuCrawler()
                    await crawler.start()

                    data_dir = os.path.join("data", "xhs", "json")
                    new_notes = collect_crawled_data(
                        data_dir, session_ts, rc_name, min_interaction=0,
                        user_id=rc_uid
                    )
                    if new_notes:
                        contents_file = os.path.join(task_dir, f"creator_{rc_uid}_contents.json")
                        with open(contents_file, "w", encoding="utf-8") as f:
                            json.dump(new_notes, f, ensure_ascii=False, indent=2)
                        utils.logger.info(f"  补爬完成: {len(new_notes)} 条 → {contents_file}")

                    # 再次校验补爬结果
                    if new_notes and _validate_single_creator(rc_uid, new_notes):
                        progress.mark_completed(rc_url)
                        recrawl_success += 1
                        utils.logger.info(f"[DataValidator] ✅ 补爬校验通过: {rc_name}")
                    else:
                        recrawl_failed += 1
                        progress.mark_failed(rc_url, "补爬后数据仍异常")
                        utils.logger.warning(f"[DataValidator] ⚠️ 补爬后仍异常: {rc_name}")

                except _SSE as se:
                    # Session 失效熔断 — 停止补爬
                    recrawl_failed += 1
                    progress.mark_failed(rc_url, f"Session 失效熔断: {se}")
                    utils.logger.error(
                        f"[DataValidator] ⚡ 补爬 Session 失效: {rc_name} - {se}，停止补爬"
                    )
                    # 关闭浏览器
                    if crawler:
                        try:
                            if getattr(crawler, "cdp_manager", None):
                                await crawler.cdp_manager.cleanup(force=True)
                            elif getattr(crawler, "browser_context", None):
                                await crawler.browser_context.close()
                        except Exception:
                            pass
                        crawler = None
                    break  # 停止补爬循环

                except Exception as e:
                    recrawl_failed += 1
                    progress.mark_failed(rc_url, f"补爬异常: {str(e)}")
                    utils.logger.error(f"[DataValidator] 补爬失败: {rc_name} - {e}")

                finally:
                    if crawler:
                        try:
                            if getattr(crawler, "cdp_manager", None):
                                await crawler.cdp_manager.cleanup(force=True)
                            elif getattr(crawler, "browser_context", None):
                                await crawler.browser_context.close()
                        except Exception:
                            pass
                        try:
                            import subprocess as _sp
                            import platform as _pf
                            if _pf.system() == "Linux":
                                _sp.run(["pkill", "-f", "chromium"], capture_output=True, timeout=5)
                            await asyncio.sleep(2)
                        except Exception:
                            pass

                if ridx < len(invalid_creators):
                    utils.logger.info("[DataValidator] 等待 10 秒后继续...")
                    await asyncio.sleep(10)

            utils.logger.info("=" * 60)
            utils.logger.info(f"[DataValidator] 补爬完成: 成功 {recrawl_success} | 失败 {recrawl_failed}")
            utils.logger.info("=" * 60)

    # 8. 从任务目录读取所有数据，合并导出
    utils.logger.info(f"[BatchCrawler] 从任务目录汇总数据: {task_dir}")

    notes_by_id: Dict[str, Dict] = {}
    if os.path.exists(task_dir):
        for filename in sorted(os.listdir(task_dir)):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(task_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    continue
                for item in data:
                    nid = item.get("note_id", "")
                    if not nid:
                        continue
                    if nid not in notes_by_id:
                        notes_by_id[nid] = item
                    else:
                        # 按字段合并：以更完整的为基底，用另一条的互动数据更新（避免覆盖媒体字段）
                        cur = notes_by_id[nid]
                        base = item if _note_completeness(item) > _note_completeness(cur) else cur
                        update = cur if base is item else item
                        notes_by_id[nid] = _merge_note_metadata(base, update)
            except Exception:
                pass

    # 对新爬取的 _contents 数据做过滤（_reuse 已经过滤过了）
    all_export_notes = []
    for item in notes_by_id.values():
        # reuse 数据已经过滤过，直接保留
        if item.get("_reused"):
            all_export_notes.append(item)
            continue
        # 新爬取的数据做日期+互动量过滤
        if crawl_mode == "date_range" and not _check_date_range(item, date_start, date_end):
            continue
        if min_interaction > 0:
            if get_interaction_count(item) < min_interaction:
                continue
        all_export_notes.append(item)

    utils.logger.info(f"  汇总结果: {len(all_export_notes)} 条符合条件 (互动量>={min_interaction})")

    # 8.5 补下载缺失图片（image 模式下，对复用的历史数据补图）
    if feishu_image_mode == "image" and all_export_notes:
        patch_msg = f"(限制 {patch_limit} 条)" if patch_limit > 0 else "(不限)"
        utils.logger.info(f"[BatchCrawler] 检查是否有笔记缺少本地图片... {patch_msg}")
        try:
            img_count = await _download_missing_images(all_export_notes, max_notes=patch_limit)
            if img_count > 0:
                utils.logger.info(f"[BatchCrawler] 补图完成: {img_count} 条笔记的图片已下载")
        except Exception as e:
            utils.logger.warning(f"[BatchCrawler] 补图阶段异常: {e}")

    if all_export_notes:
        try:
            utils.logger.info(f"[BatchCrawler] 导出到本地 ({export_format})...")
            export_path = export_to_local(
                notes=all_export_notes,
                export_dir=export_dir,
                export_format=export_format,
            )
            if export_path:
                utils.logger.info(f"[BatchCrawler] 本地导出完成: {export_path}")
        except Exception as e:
            utils.logger.error(f"[BatchCrawler] 本地导出失败: {e}，继续执行飞书推送...")
    else:
        utils.logger.info("[BatchCrawler] 没有符合条件的数据可导出")

    # 8. 推送到飞书
    if pipeline_writer:
        try:
            utils.logger.info("[Pipeline] 收尾: 等待脚本提取完成 + 最终校验...")
            pipeline_writer.finalize()
            if pipeline_writer.bitable_url:
                utils.logger.info(f"[Pipeline] 飞书表格链接: {pipeline_writer.bitable_url}")
        except Exception as e:
            utils.logger.error(f"[Pipeline] 收尾失败: {e}")
        finally:
            pipeline_writer.close()
    elif not skip_feishu and all_export_notes:
        try:
            utils.logger.info("[BatchCrawler] 开始推送到飞书多维表格...")
            bitable_url = push_to_feishu(
                notes=all_export_notes,
                field_defs=field_defs,
                app_id=feishu_app_id,
                app_secret=feishu_app_secret,
                folder_token=feishu_folder_token,
            )
            if bitable_url:
                utils.logger.info(f"[BatchCrawler] 飞书表格链接: {bitable_url}")
        except Exception as e:
            utils.logger.error(f"[BatchCrawler] 飞书推送失败: {e}")
    elif skip_feishu:
        utils.logger.info("[BatchCrawler] 跳过飞书写入 (--skip-feishu)")
    elif not all_export_notes:
        utils.logger.info("[BatchCrawler] 没有新数据，跳过飞书写入")


# ==================== 仅导出模式 ====================

def _run_export_only(excel_path: str, min_interaction: int = 50,
                     export_format: str = "excel",
                     export_dir: str = "data/export"):
    """
    仅导出模式：读取已有的 JSON 数据，转换为 Excel
    不执行爬取
    """
    print("=" * 60)
    print("[ExportOnly] 仅导出模式 - 读取已有 JSON 数据")
    print(f"  互动量过滤: >= {min_interaction}")
    print(f"  导出格式: {export_format}")
    print(f"  导出目录: {export_dir}")
    print("=" * 60)

    # 读取 Excel 获取作者名称映射
    creator_names = {}
    try:
        with ExcelCreatorReader(excel_path) as reader:
            creators = reader.get_creators()
        for c in creators:
            # 从 URL 提取 user_id 用于匹配
            url = c["url"]
            if "/user/profile/" in url:
                uid = url.split("/user/profile/")[1].split("?")[0]
                creator_names[uid] = c["name"]
    except Exception as e:
        print(f"[ExportOnly] 读取 Excel 作者列表失败: {e}")

    # 扫描 JSON 数据文件（仅 creator 模式的文件，排除 search）
    data_dir = os.path.join("data", "xhs", "json")
    if not os.path.exists(data_dir):
        print(f"[ExportOnly] 数据目录不存在: {data_dir}")
        return

    # 构建 Excel 中所有作者的 user_id 集合，用于精确匹配
    valid_user_ids = set(creator_names.keys())
    print(f"  Excel 中共 {len(valid_user_ids)} 个作者 user_id")

    # 去重：同一个 note_id 保留最完整的记录
    notes_by_id: Dict[str, Dict] = {}
    filtered_count = 0
    skipped_not_in_excel = 0
    duplicates = 0

    for filename in sorted(os.listdir(data_dir)):
        if not filename.endswith(".json"):
            continue
        if not filename.startswith("creator_contents"):
            continue

        filepath = os.path.join(data_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue

            file_added = 0
            for item in data:
                user_id = item.get("user_id", "")
                if valid_user_ids and user_id not in creator_names:
                    skipped_not_in_excel += 1
                    continue

                item["_creator_name"] = creator_names.get(user_id, item.get("nickname", "未知"))

                note_id = item.get("note_id", "")
                if not note_id:
                    continue

                if note_id in notes_by_id:
                    if _note_completeness(item) > _note_completeness(notes_by_id[note_id]):
                        notes_by_id[note_id] = item
                    duplicates += 1
                else:
                    notes_by_id[note_id] = item
                    file_added += 1

            print(f"  读取: {filename} ({file_added} 条新增)")
        except Exception as e:
            print(f"  读取失败: {filename} - {e}")

    # 读取日期范围配置
    crawl_mode = "full"
    date_start_e = ""
    date_end_e = ""
    try:
        config_path_t = os.path.join("config", "anti_crawl_config.json")
        with open(config_path_t, "r", encoding="utf-8") as f:
            cfg_t = json.load(f)
        batch_cfg_t = cfg_t.get("batch_crawl", {})
        crawl_mode = batch_cfg_t.get("crawl_mode", "full")
        date_start_e = batch_cfg_t.get("date_start", "")
        date_end_e = batch_cfg_t.get("date_end", "")
    except Exception:
        pass

    # 时间范围+互动量过滤
    all_notes = []
    date_filtered = 0
    for item in notes_by_id.values():
        if crawl_mode == "date_range" and not _check_date_range(item, date_start_e, date_end_e):
            date_filtered += 1
            continue
        if min_interaction > 0:
            interaction = get_interaction_count(item)
            if interaction < min_interaction:
                filtered_count += 1
                continue
        all_notes.append(item)

    if date_filtered > 0:
        print(f"  日期范围过滤: {date_filtered} 条不在 {date_start_e} ~ {date_end_e} 内")

    print(f"\n[ExportOnly] 共 {len(all_notes)} 条符合条件 (去重 {duplicates} 条)")
    print(f"  互动量过滤掉: {filtered_count} 条")
    print(f"  非Excel作者跳过: {skipped_not_in_excel} 条")

    if all_notes:
        export_path = export_to_local(
            notes=all_notes,
            export_dir=export_dir,
            export_format=export_format,
        )
        print(f"\n[ExportOnly] 导出完成: {export_path}")
    else:
        print("[ExportOnly] 没有数据可导出")


# ==================== 命令行入口 ====================

def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="批量作者爬取 + 飞书多维表格")
    parser.add_argument("--excel", default="redbookaccontidandresult.xlsx",
                        help="Excel 文件路径")
    parser.add_argument("--skip-feishu", action="store_true",
                        help="跳过飞书写入")
    parser.add_argument("--no-resume", action="store_true",
                        help="不使用断点续爬，从头开始")
    parser.add_argument("--max-notes", type=int, default=3000,
                        help="每个作者最多爬取多少条 (默认 3000)")
    parser.add_argument("--enable-comments", action="store_true",
                        help="启用评论爬取")
    parser.add_argument("--limit", type=int, default=0,
                        help="只爬取前 N 个作者，0=全部")
    parser.add_argument("--export-only", action="store_true",
                        help="仅导出已有JSON数据为Excel，不执行爬取")
    parser.add_argument("--min-interaction", type=int, default=50,
                        help="互动量过滤阈值 (点赞+评论+收藏)，默认50，0=不过滤")
    parser.add_argument("--export-format", default="excel",
                        choices=["excel", "json", "csv"],
                        help="导出格式 (默认 excel)")
    parser.add_argument("--export-dir", default="data/export",
                        help="导出目录 (默认 data/export)")
    parser.add_argument("--feishu-app-id", default="",
                        help="飞书 App ID")
    parser.add_argument("--feishu-app-secret", default="",
                        help="飞书 App Secret")
    parser.add_argument("--feishu-folder", default="",
                        help="飞书文件夹 token")
    parser.add_argument("--reset-progress", action="store_true",
                        help="重置批量爬取进度")
    parser.add_argument("--patch-only", action="store_true",
                        help="仅补图模式：跳过爬取，只对已有数据补下载缺失图片")
    parser.add_argument("--patch-limit", type=int, default=0,
                        help="每次补图最多处理 N 条笔记 (默认0=不限制，建议30~50)")
    parser.add_argument("--force-recrawl", action="store_true",
                        help="强制重新爬取：重置进度+禁用历史复用，爬取时直接下载图片")
    parser.add_argument("--patch-feishu", default="",
                        help="补写飞书表格: 传入 app_token，自动修复日期字段+提取视频脚本")
    parser.add_argument("--mode", default="",
                        choices=["", "full", "date_range", "incremental"],
                        help="爬取模式: full=全量, date_range=日期范围, incremental=增量更新(覆盖配置文件)")

    args = parser.parse_args()

    # 重置进度
    if args.reset_progress:
        progress = BatchProgress()
        progress.reset()
        print("[BatchCrawler] 批量爬取进度已重置")
        return

    # 从配置文件读取飞书凭证（如果命令行未指定）
    feishu_app_id = args.feishu_app_id
    feishu_app_secret = args.feishu_app_secret
    feishu_folder = args.feishu_folder

    if not feishu_app_id:
        try:
            config_path = os.path.join("config", "anti_crawl_config.json")
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            feishu_cfg = cfg.get("feishu", {})
            feishu_app_id = feishu_app_id or feishu_cfg.get("app_id", "")
            feishu_app_secret = feishu_app_secret or feishu_cfg.get("app_secret", "")
            feishu_folder = feishu_folder or feishu_cfg.get("folder_token", "")
        except Exception:
            pass

    from tools.app_runner import run

    # 从配置文件读取过滤/导出设置（命令行优先）
    min_interaction = args.min_interaction
    export_format = args.export_format
    export_dir = args.export_dir
    try:
        config_path = os.path.join("config", "anti_crawl_config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        batch_cfg = cfg.get("batch_crawl", {})
        if min_interaction == 50:  # 使用默认值时从配置文件读取
            min_interaction = batch_cfg.get("min_interaction", 50)
        if export_format == "excel":
            export_format = batch_cfg.get("export_format", "excel")
        if export_dir == "data/export":
            export_dir = batch_cfg.get("export_dir", "data/export")
    except Exception:
        pass

    # 从配置文件读取 Excel 路径（命令行优先）
    excel_path = args.excel
    try:
        if excel_path == "redbookaccontidandresult.xlsx":
            config_path = os.path.join("config", "anti_crawl_config.json")
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
            config_excel = cfg_data.get("batch_crawl", {}).get("excel_path", "")
            if config_excel and os.path.exists(config_excel):
                excel_path = config_excel
    except Exception:
        pass

    # --patch-feishu 模式：补写已有飞书表格（修复日期 + 提取脚本）
    if args.patch_feishu:
        _patch_feishu_tables(args.patch_feishu, feishu_app_id, feishu_app_secret)
        return

    # --export-only 模式：只导出已有数据，不爬取
    if args.export_only:
        _run_export_only(
            excel_path=excel_path,
            min_interaction=min_interaction,
            export_format=export_format,
            export_dir=export_dir,
        )
        return

    async def _run():
        await run_batch_crawl(
            excel_path=excel_path,
            skip_feishu=args.skip_feishu,
            feishu_app_id=feishu_app_id,
            feishu_app_secret=feishu_app_secret,
            feishu_folder_token=feishu_folder,
            resume=not args.no_resume,
            max_notes_per_creator=args.max_notes,
            enable_comments=args.enable_comments,
            min_interaction=min_interaction,
            export_format=export_format,
            export_dir=export_dir,
            limit=args.limit,
            patch_only=args.patch_only,
            patch_limit=args.patch_limit,
            force_recrawl=args.force_recrawl,
            crawl_mode_override=args.mode,
        )

    async def _cleanup():
        pass

    run(_run, _cleanup, cleanup_timeout_seconds=15.0)


if __name__ == "__main__":
    main()
