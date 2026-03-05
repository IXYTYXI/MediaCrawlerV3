# -*- coding: utf-8 -*-
"""
解析 crawler.log 提取当前爬虫实时进度
"""
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


LOG_PATH = os.path.join("logs", "crawler.log")


@dataclass
class AuthorStatus:
    index: int
    total: int
    name: str
    status: str  # "completed", "skipped", "crawling", "writing"
    notes_count: int = 0
    video_count: int = 0
    reason: str = ""


@dataclass
class CrawlerProgress:
    running: bool = False
    task_id: str = ""
    crawl_mode: str = ""
    total_authors: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    authors: List[AuthorStatus] = field(default_factory=list)
    current_author: str = ""
    current_index: int = 0
    current_detail_progress: str = ""
    total_notes_written: int = 0
    total_videos: int = 0
    scripts_done: int = 0
    scripts_failed: int = 0
    scripts_total: int = 0
    start_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    bitable_url: str = ""
    elapsed_seconds: int = 0


def parse_progress() -> CrawlerProgress:
    """解析日志文件，返回当前爬虫进度"""
    p = CrawlerProgress()

    if not os.path.exists(LOG_PATH):
        return p

    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
    except Exception:
        return p

    if not all_lines:
        return p

    # 只解析最后一次批量爬取的日志（节省内存和时间）
    last_start_idx = 0
    for i, line in enumerate(all_lines):
        if "[BatchCrawler] 批量爬取开始" in line:
            last_start_idx = i
    lines = all_lines[last_start_idx:]

    re_ts = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    re_batch_start = re.compile(
        r"\[BatchCrawler\] 批量爬取开始"
    )
    re_task_id_line = re.compile(
        r"\[BatchCrawler\] 任务ID[：:]\s*(\S+)"
    )
    re_mode = re.compile(
        r"\[BatchCrawler\].*?(全量|增量更新|日期范围)"
    )
    re_completed = re.compile(
        r"\[BatchCrawler\] \[(\d+)/(\d+)\] (?:复用)?完成: (.+)"
    )
    re_skipped = re.compile(
        r"\[BatchCrawler\] \[(\d+)/(\d+)\] (?:跳过[（(](.+?)[)）]|跳过已完成|.*?跳过): (.+)"
    )
    re_failed = re.compile(
        r"\[BatchCrawler\] \[(\d+)/(\d+)\] 失败: (.+)"
    )
    re_crawling = re.compile(
        r"\[BatchCrawler\] \[(\d+)/(\d+)\] 开始爬取: (.+)"
    )
    re_pipeline_done = re.compile(
        r"\[Pipeline\] (.+?): (\d+) 条写入完成(?:.*?(\d+) 条视频入汇总表)?"
    )
    re_detail = re.compile(
        r"\[详情获取\] \((\d+)/(\d+)\) .*?\[累计: (\d+)\]"
    )
    re_script_done = re.compile(r"\[Pipeline\] 脚本完成:")
    re_script_fail = re.compile(r"\[Pipeline\] 脚本失败:")
    re_script_start = re.compile(r"\[Pipeline\] 提取脚本:")
    re_all_done = re.compile(r"\[Pipeline\] 全部完成:")
    re_bitable = re.compile(r"URL: (https://\S+feishu\S+)")

    author_map = {}
    last_ts = None
    current_crawling_idx = 0
    current_crawling_name = ""

    for line in lines:
        ts_match = re_ts.match(line)
        if ts_match:
            try:
                last_ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        m = re_batch_start.search(line)
        if m:
            p.start_time = last_ts
            p.running = True
            author_map.clear()
            continue

        m = re_task_id_line.search(line)
        if m:
            p.task_id = m.group(1)
            continue

        m = re_mode.search(line)
        if m:
            mode_text = m.group(1)
            if "增量" in mode_text:
                p.crawl_mode = "incremental"
            elif "日期范围" in mode_text:
                p.crawl_mode = "date_range"
            else:
                p.crawl_mode = "full"
            continue

        m = re_completed.search(line)
        if m:
            idx, total, name = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            p.total_authors = max(p.total_authors, total)
            author_map[name] = AuthorStatus(
                index=idx, total=total, name=name, status="completed"
            )
            continue

        m = re_skipped.search(line)
        if m:
            idx, total = int(m.group(1)), int(m.group(2))
            reason = (m.group(3) or "").strip()
            name = m.group(4).strip()
            p.total_authors = max(p.total_authors, total)
            author_map[name] = AuthorStatus(
                index=idx, total=total, name=name,
                status="skipped", reason=reason,
            )
            continue

        m = re_failed.search(line)
        if m:
            idx, total = int(m.group(1)), int(m.group(2))
            name = m.group(3).strip().split(" - ")[0]
            p.total_authors = max(p.total_authors, total)
            author_map[name] = AuthorStatus(
                index=idx, total=total, name=name, status="failed",
            )
            continue

        m = re_crawling.search(line)
        if m:
            idx, total, name = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            current_crawling_idx = idx
            current_crawling_name = name
            if name not in author_map:
                author_map[name] = AuthorStatus(
                    index=idx, total=total, name=name, status="crawling"
                )
            continue

        m = re_pipeline_done.search(line)
        if m:
            name = m.group(1).strip()
            notes = int(m.group(2))
            videos = int(m.group(3)) if m.group(3) else 0
            if name in author_map:
                author_map[name].notes_count = notes
                author_map[name].video_count = videos
            continue

        m = re_detail.search(line)
        if m:
            cur, tot, cumulative = m.group(1), m.group(2), m.group(3)
            p.current_detail_progress = f"{cur}/{tot}（累计 {cumulative} 条）"
            continue

        if re_script_done.search(line):
            p.scripts_done += 1
        if re_script_fail.search(line):
            p.scripts_failed += 1
        if re_script_start.search(line):
            p.scripts_total += 1

        m = re_all_done.search(line)
        if m:
            p.running = False

        m = re_bitable.search(line)
        if m:
            p.bitable_url = m.group(1)

    completed_names = set()
    for name, a in author_map.items():
        if a.status == "completed":
            p.completed += 1
            completed_names.add(name)
        elif a.status == "skipped":
            p.skipped += 1
        elif a.status == "failed":
            p.failed += 1
        p.total_notes_written += a.notes_count
        p.total_videos += a.video_count

    p.authors = sorted(author_map.values(), key=lambda a: a.index)

    if current_crawling_name and current_crawling_name not in completed_names:
        p.current_author = current_crawling_name
        p.current_index = current_crawling_idx

    if p.start_time and last_ts:
        p.elapsed_seconds = int((last_ts - p.start_time).total_seconds())
    p.last_activity = last_ts

    # 检查爬虫进程是否真的在运行（通过进程列表确认）
    if p.running:
        p.running = _is_crawler_process_alive()
    elif p.total_authors > 0 and not p.running:
        if _is_crawler_process_alive():
            p.running = True

    return p


def _is_crawler_process_alive() -> bool:
    """检查 batch_crawler 进程是否存活"""
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "tools.batch_crawler"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def build_progress_card(p: CrawlerProgress) -> dict:
    """将进度信息构建为飞书消息卡片"""
    mode_labels = {
        "full": "全量爬取", "incremental": "增量更新", "date_range": "日期范围",
    }

    if not p.running and p.completed == 0 and p.total_authors == 0:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "grey",
                "title": {"tag": "plain_text", "content": "爬虫状态查询"},
            },
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": "当前没有正在运行的爬虫任务。"},
            }],
        }

    h = p.elapsed_seconds // 3600
    m = (p.elapsed_seconds % 3600) // 60
    duration = f"{h}h {m}m" if h else f"{m}m"

    done = p.completed + p.skipped + p.failed
    remaining = p.total_authors - done
    bar_len = 20
    filled = round(done / p.total_authors * bar_len) if p.total_authors > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    pct = round(done / p.total_authors * 100) if p.total_authors > 0 else 0

    status_emoji = "🟢" if p.running else "✅"
    status_text = "运行中" if p.running else "已完成"
    header_color = "blue" if p.running else "green"

    info_line = f"{status_emoji} **{status_text}**　·　{mode_labels.get(p.crawl_mode, p.crawl_mode)}　·　已运行 {duration}"

    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": info_line},
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**作者进度**　`{bar}`　**{pct}%**（{done}/{p.total_authors}）",
            },
        },
    ]

    # 当前正在处理的作者
    if p.running:
        if p.current_author:
            detail = ""
            if p.current_detail_progress:
                detail = f"　笔记获取: {p.current_detail_progress}"
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**正在爬取**: [{p.current_index}/{p.total_authors}] {p.current_author}{detail}",
                },
            })
        elif p.scripts_total > p.scripts_done + p.scripts_failed:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**当前**: 等待飞书写入和视频脚本提取完成...",
                },
            })

    elements.append({"tag": "hr"})

    # 四列统计
    elements.append({
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "div", "text": {
                    "tag": "lark_md", "content": f"**{p.completed}**\n成功",
                }}],
            },
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "div", "text": {
                    "tag": "lark_md", "content": f"**{p.skipped}**\n跳过",
                }}],
            },
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "div", "text": {
                    "tag": "lark_md",
                    "content": f"**{p.total_notes_written}**\n笔记写入",
                }}],
            },
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "div", "text": {
                    "tag": "lark_md",
                    "content": f"**{p.total_videos}**\n视频汇总",
                }}],
            },
        ],
    })

    # 脚本提取
    if p.scripts_total > 0:
        script_line = f"**视频脚本**: {p.scripts_done} 完成"
        if p.scripts_failed > 0:
            script_line += f" / {p.scripts_failed} 失败"
        pending = p.scripts_total - p.scripts_done - p.scripts_failed
        if pending > 0:
            script_line += f" / {pending} 进行中"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": script_line},
        })

    # 已完成的作者列表
    if p.authors:
        author_lines = []
        for a in p.authors:
            if a.status == "completed":
                suffix = ""
                if a.notes_count > 0:
                    suffix = f"　{a.notes_count} 条"
                    if a.video_count > 0:
                        suffix += f"/{a.video_count} 视频"
                author_lines.append(f"✅ [{a.index}] {a.name}{suffix}")
            elif a.status == "skipped":
                author_lines.append(f"⏭️ [{a.index}] {a.name}（{a.reason}）")
            elif a.status == "failed":
                author_lines.append(f"❌ [{a.index}] {a.name}（失败）")
            elif a.status == "crawling":
                author_lines.append(f"🔄 [{a.index}] {a.name}（进行中）")
        if author_lines:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**作者详情**\n" + "\n".join(author_lines),
                },
            })

    # 剩余估算
    if p.running and remaining > 0 and p.completed > 0 and p.elapsed_seconds > 0:
        avg = p.elapsed_seconds / p.completed
        est_min = int(remaining * avg / 60)
        est_h = est_min // 60
        est_m = est_min % 60
        est_text = f"{est_h}h {est_m}m" if est_h else f"{est_m}m"
        elements.append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"预计剩余 {est_text}（还有 {remaining} 个作者）",
            }],
        })

    if p.bitable_url:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看飞书表格"},
                "type": "primary",
                "url": p.bitable_url,
            }],
        })

    # 运行中时显示"刷新进度"按钮，点击后卡片原地更新
    if p.running:
        _refresh_ts = datetime.now().strftime("%H:%M:%S")
        refresh_actions = [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 刷新进度"},
            "type": "default",
            "value": {"action": "refresh_progress"},
        }]
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": refresh_actions})
        elements.append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"数据截至 {_refresh_ts}　·　再次 @机器人 可原地刷新此卡片",
            }],
        })
    else:
        _done_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elements.append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"查询时间 {_done_ts}",
            }],
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": header_color,
            "title": {"tag": "plain_text", "content": "爬虫实时进度"},
        },
        "elements": elements,
    }
