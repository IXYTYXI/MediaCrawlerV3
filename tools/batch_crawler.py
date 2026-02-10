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
from typing import List, Dict, Any, Optional, Set

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import utils
from tools.excel_reader import ExcelCreatorReader, load_creators_from_excel


# ==================== 批量爬取进度管理 ====================

class BatchProgress:
    """批量爬取进度管理（记录已完成的作者）"""

    def __init__(self, progress_file: str = "data/batch_progress.json"):
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

    try:
        with FeishuBitableClient(app_id, app_secret) as client:
            # 1. 创建多维表格
            result = client.create_bitable(bitable_name, folder_token or None)
            app_token = result["app_token"]
            bitable_url = result["url"]
            utils.logger.info(f"[BatchCrawler] 创建多维表格: {bitable_name}")

            # 2. 按作者分组
            from collections import OrderedDict
            grouped: OrderedDict[str, List[Dict]] = OrderedDict()
            for note in notes:
                creator_name = note.get("_creator_name", note.get("nickname", "未知"))
                if creator_name not in grouped:
                    grouped[creator_name] = []
                grouped[creator_name].append(note)

            # 3. 构建所有记录，确定字段列表
            all_field_names = set()
            all_grouped_records: OrderedDict[str, List[Dict]] = OrderedDict()
            for creator_name, creator_notes in grouped.items():
                records = []
                for note in creator_notes:
                    record = map_note_to_feishu_record(creator_name, note)
                    records.append(record)
                    all_field_names.update(record.get("fields", {}).keys())
                all_grouped_records[creator_name] = records

            # 4. 确定字段顺序
            ordered_fields = ["账号名称", "内容类型", "标题", "正文", "标签", "链接",
                              "发布时间", "点赞数", "收藏数", "评论数", "互动量", "热门",
                              "视频附件", "视频脚本"]
            image_fields = sorted(
                [f for f in all_field_names if f.startswith("图片")],
                key=lambda x: int(x.replace("图片", "") or "0")
            )
            ordered_fields.extend(image_fields)
            for f in all_field_names:
                if f not in ordered_fields:
                    ordered_fields.append(f)

            url_fields = {"链接"}
            attachment_fields = {"视频附件"}
            ordered_with_serial = set(ordered_fields) | {"序号"}
            total_inserted = 0

            # 5. 处理默认数据表（改为第一个作者的表）
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
            return bitable_url

    except Exception as e:
        utils.logger.error(f"[BatchCrawler] 飞书推送失败: {e}")
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

    # 2. 初始化进度管理
    progress = BatchProgress()
    if not resume:
        progress.reset()

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    progress.set_session(session_id)

    # 3. 加载基础配置
    apply_anti_crawl_config(config)

    # 覆盖关键配置
    config.PLATFORM = "xhs"
    config.CRAWLER_TYPE = "creator"
    config.CRAWLER_MAX_NOTES_COUNT = max_notes_per_creator
    config.ENABLE_GET_COMMENTS = enable_comments

    # 4. 统计
    total = len(creators)
    skipped = 0
    success = 0
    failed = 0
    all_notes: List[Dict] = []

    # 5. 逐个作者爬取
    for idx, creator in enumerate(creators, 1):
        creator_name = creator["name"]
        creator_url = creator["url"]

        # 断点续爬：跳过已完成
        if resume and progress.is_completed(creator_url):
            utils.logger.info(
                f"[BatchCrawler] [{idx}/{total}] 跳过已完成: {creator_name}"
            )
            skipped += 1
            continue

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

            # 收集数据（带互动量过滤）
            data_dir = os.path.join("data", "xhs", "json")
            notes = collect_crawled_data(
                data_dir, session_ts, creator_name,
                min_interaction=min_interaction
            )
            all_notes.extend(notes)

            # 标记完成
            progress.mark_completed(creator_url)
            success += 1
            utils.logger.info(
                f"[BatchCrawler] [{idx}/{total}] 完成: {creator_name} "
                f"({len(notes)} 条笔记，互动量>={min_interaction})"
            )

        except Exception as e:
            failed += 1
            error_msg = str(e)
            progress.mark_failed(creator_url, error_msg)
            utils.logger.error(
                f"[BatchCrawler] [{idx}/{total}] 失败: {creator_name} - {error_msg}"
            )

        finally:
            # 关闭浏览器，释放端口，防止下一个作者启动时冲突
            if crawler:
                try:
                    # 关闭 CDP 浏览器
                    if getattr(crawler, "cdp_manager", None):
                        await crawler.cdp_manager.cleanup(force=True)
                        crawler.cdp_manager = None
                    elif getattr(crawler, "browser_context", None):
                        await crawler.browser_context.close()
                    utils.logger.info("[BatchCrawler] 浏览器已关闭")
                except Exception as close_err:
                    utils.logger.warning(f"[BatchCrawler] 关闭浏览器异常: {close_err}")

                # 杀掉残留的 Chrome 进程
                try:
                    import subprocess
                    subprocess.run(["pkill", "-f", "Google Chrome Dev"], capture_output=True, timeout=5)
                    await asyncio.sleep(2)
                except Exception:
                    pass

        # 作者之间休息一下
        if idx < total:
            wait = 10
            utils.logger.info(f"[BatchCrawler] 等待 {wait} 秒后继续下一个作者...")
            await asyncio.sleep(wait)

    # 6. 汇总
    utils.logger.info("=" * 60)
    utils.logger.info("[BatchCrawler] 批量爬取完成!")
    utils.logger.info(f"  总计: {total} | 成功: {success} | 失败: {failed} | 跳过: {skipped}")
    utils.logger.info("=" * 60)

    # 7. 导出到本地文件（读取所有历史JSON，合并导出，支持断点续爬多次累积）
    utils.logger.info(f"[BatchCrawler] 汇总所有已爬数据并导出...")
    
    # 构建作者 user_id → 名称映射
    creator_names = {}
    for c in creators:
        url = c["url"]
        if "/user/profile/" in url:
            uid = url.split("/user/profile/")[1].split("?")[0]
            creator_names[uid] = c["name"]

    # 读取所有 creator_contents JSON，只匹配 Excel 中的作者
    # 去重：同一个 note_id 保留最完整的记录（有详情的优先于只有基本信息的）
    data_dir = os.path.join("data", "xhs", "json")
    notes_by_id: Dict[str, Dict] = {}  # note_id → 最完整的记录
    if os.path.exists(data_dir):
        for filename in sorted(os.listdir(data_dir)):
            if not filename.endswith(".json") or not filename.startswith("creator_contents"):
                continue
            filepath = os.path.join(data_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    continue
                for item in data:
                    user_id = item.get("user_id", "")
                    if user_id not in creator_names:
                        continue
                    item["_creator_name"] = creator_names[user_id]
                    
                    note_id = item.get("note_id", "")
                    if not note_id:
                        continue
                    
                    # 保留更完整的记录
                    if note_id not in notes_by_id or _note_completeness(item) > _note_completeness(notes_by_id[note_id]):
                        notes_by_id[note_id] = item
            except Exception:
                pass

    # 读取日期范围配置
    crawl_mode = "full"
    date_start = ""
    date_end = ""
    try:
        config_path_t = os.path.join("config", "anti_crawl_config.json")
        with open(config_path_t, "r", encoding="utf-8") as f:
            cfg_t = json.load(f)
        batch_cfg_t = cfg_t.get("batch_crawl", {})
        crawl_mode = batch_cfg_t.get("crawl_mode", "full")
        date_start = batch_cfg_t.get("date_start", "")
        date_end = batch_cfg_t.get("date_end", "")
    except Exception:
        pass

    # 时间范围+互动量过滤
    all_export_notes = []
    date_filtered = 0
    for item in notes_by_id.values():
        if crawl_mode == "date_range" and not _check_date_range(item, date_start, date_end):
            date_filtered += 1
            continue
        if min_interaction > 0:
            interaction = get_interaction_count(item)
            if interaction < min_interaction:
                continue
        all_export_notes.append(item)

    if date_filtered > 0:
        utils.logger.info(f"  日期范围过滤: {date_filtered} 条不在 {date_start} ~ {date_end} 内")

    utils.logger.info(f"  汇总结果: {len(all_export_notes)} 条符合条件 (互动量>={min_interaction})")

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
        )

    async def _cleanup():
        pass

    run(_run, _cleanup, cleanup_timeout_seconds=15.0)


if __name__ == "__main__":
    main()
