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

from tools import utils
from tools.excel_reader import ExcelCreatorReader, load_creators_from_excel


# ==================== 批量爬取进度管理 ====================

class BatchProgress:
    """批量爬取进度管理（记录已完成的作者）"""

    def __init__(self, progress_file: str = "data/batch_progress.json", task_id: str = ""):
        self.progress_file = progress_file
        self._completed: Set[str] = set()  # 已完成的作者 URL
        self._failed: Dict[str, str] = {}  # 失败记录 {url: error_msg}
        self._session_id: str = ""
        self._load()

    def _load(self):
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._completed = set(data.get("completed", []))
                self._failed = data.get("failed", {})
                self._session_id = data.get("session_id", "")
                utils.logger.info(
                    f"[BatchProgress] 加载进度: {len(self._completed)} 完成, "
                    f"{len(self._failed)} 失败"
                )
            except Exception as e:
                utils.logger.warning(f"[BatchProgress] 加载进度失败: {e}")

    def save(self):
        os.makedirs(os.path.dirname(self.progress_file), exist_ok=True)
        data = {
            "session_id": self._session_id,
            "completed": list(self._completed),
            "failed": self._failed,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(self.progress_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def is_completed(self, url: str) -> bool:
        return url in self._completed

    def mark_completed(self, url: str):
        self._completed.add(url)
        self._failed.pop(url, None)
        self.save()

    def mark_failed(self, url: str, error: str):
        self._failed[url] = error
        self.save()

    def set_session(self, session_id: str):
        self._session_id = session_id

    def reset(self):
        """重置进度（重新开始）"""
        self._completed.clear()
        self._failed.clear()
        self.save()

    @property
    def completed_count(self) -> int:
        return len(self._completed)


# ==================== 数据收集与过滤 ====================

def _safe_int(value) -> int:
    """安全转换为整数，处理空字符串和None"""
    if value is None or value == "":
        return 0
    try:
        return int(value)
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
                         min_interaction: int = 0) -> List[Dict]:
    """
    从爬取结果 JSON 文件中收集数据，支持互动量过滤
    
    Args:
        data_dir: 数据目录 (如 data/xhs/json)
        session_timestamp: 会话时间戳（用于匹配文件）
        creator_name: 作者名称（附加到每条记录）
        min_interaction: 最低互动量阈值，0=不过滤
        
    Returns:
        笔记数据列表
    """
    notes = []
    filtered_count = 0
    
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

    if filtered_count > 0:
        utils.logger.info(
            f"[BatchCrawler] 互动量过滤: 保留 {len(notes)} 条, 过滤掉 {filtered_count} 条 "
            f"(阈值: {min_interaction})"
        )

    return notes


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

    # 发布时间
    time_val = note.get("time", "")
    if isinstance(time_val, (int, float)) and time_val > 0:
        try:
            time_val = datetime.fromtimestamp(time_val / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            time_val = str(time_val)

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

    result = {
        "账号名称": creator_name or note.get("nickname", ""),
        "内容类型": content_type,
        "标题": note.get("title", ""),
        "正文": note.get("desc", ""),
        "标签": note.get("tag_list", ""),
        "链接": note.get("note_url", ""),
        "发布时间": str(time_val),
        "点赞数": liked,
        "收藏数": collected,
        "评论数": comment,
        "互动量": interaction,
        "热门": is_hot,
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
        for row_idx, note in enumerate(creator_notes, 2):
            formatted = format_note_for_export(creator_name, note)
            is_hot_row = formatted.get("热门", "") != ""
            for col_idx, col_name in enumerate(columns, 1):
                value = formatted.get(col_name, "")
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.alignment = wrap_align
                cell.border = thin_border
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

            # 6. 逐作者创建数据表并写入记录
            first_creator = True
            for creator_name, records in all_grouped_records.items():
                safe_name = creator_name[:100]
                utils.logger.info(f"[飞书] 创建数据表: {safe_name} ({len(records)} 条)")

                if first_creator:
                    # 用默认表
                    tables = client.list_tables(app_token)
                    table_id = tables[0]["table_id"] if tables else client.create_table(app_token, safe_name, [])
                    first_creator = False
                else:
                    # 创建新数据表
                    table_id = client.create_table(app_token, safe_name, [])

                # 创建字段
                for field_name in ordered_fields:
                    if field_name == "序号":
                        continue
                    ftype = 15 if field_name in url_fields else 17 if field_name in attachment_fields else 1
                    try:
                        client.add_field(app_token, table_id, field_name, ftype)
                    except Exception:
                        pass

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

            return bitable_url

    except Exception as e:
        utils.logger.error(f"[BatchCrawler] 飞书推送失败: {e}")
        import traceback
        utils.logger.error(f"[BatchCrawler] 详细错误: {traceback.format_exc()}")
        return None


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

    # 任务数据目录
    task_dir = os.path.join("data", "xhs", "json", task_id)
    os.makedirs(task_dir, exist_ok=True)

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

    # 6. 逐个作者处理
    for idx, creator in enumerate(creators, 1):
        creator_name = creator["name"]
        creator_url = creator["url"]

        # 提取 user_id
        user_id = ""
        if "/user/profile/" in creator_url:
            user_id = creator_url.split("/user/profile/")[1].split("?")[0]

        # 断点续爬：跳过当前任务已完成的
        if resume and progress.is_completed(creator_url):
            utils.logger.info(
                f"[BatchCrawler] [{idx}/{total}] 跳过已完成: {creator_name}"
            )
            skipped += 1
            continue

        # ========== 复用历史数据检查 ==========
        if reuse_history and user_id:
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

        # ========== 爬取 ==========
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

            # 设置当前作者
            config.XHS_CREATOR_ID_LIST = [creator_url]

            # 重置统计
            from tools.crawl_statistics import reset_statistics
            reset_statistics(platform="xhs")

            # 创建并运行爬虫
            from media_platform.xhs import XiaoHongShuCrawler
            from var import crawler_type_var
            crawler_type_var.set("creator")

            crawler = XiaoHongShuCrawler()
            await crawler.start()

            # 新爬取的数据保存到任务目录
            data_dir = os.path.join("data", "xhs", "json")
            new_notes = collect_crawled_data(
                data_dir, session_ts, creator_name, min_interaction=0
            )
            if new_notes:
                contents_file = os.path.join(task_dir, f"creator_{user_id}_contents.json")
                with open(contents_file, "w", encoding="utf-8") as f:
                    json.dump(new_notes, f, ensure_ascii=False, indent=2)
                utils.logger.info(f"  新爬 {len(new_notes)} 条保存到 {contents_file}")

            # 标记完成
            progress.mark_completed(creator_url)
            success += 1
            utils.logger.info(
                f"[BatchCrawler] [{idx}/{total}] 完成: {creator_name}"
            )

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
                    if nid not in notes_by_id or _note_completeness(item) > _note_completeness(notes_by_id[nid]):
                        notes_by_id[nid] = item
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
        utils.logger.info(f"[BatchCrawler] 导出到本地 ({export_format})...")
        export_path = export_to_local(
            notes=all_export_notes,
            export_dir=export_dir,
            export_format=export_format,
        )
        if export_path:
            utils.logger.info(f"[BatchCrawler] 本地导出完成: {export_path}")
    else:
        utils.logger.info("[BatchCrawler] 没有符合条件的数据可导出")

    # 8. 推送到飞书
    if not skip_feishu and all_export_notes:
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
        )

    async def _cleanup():
        pass

    run(_run, _cleanup, cleanup_timeout_seconds=15.0)


if __name__ == "__main__":
    main()
