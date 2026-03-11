# -*- coding: utf-8 -*-
"""
爬取进度快照管理（Checkpoint Manager）

功能：
- 导出当前爬取进度为快照文件（JSON），记录每个作者的状态、最后爬取日期、数据量等
- 导入快照文件恢复进度，作为增量爬取的基线
- 自动在批量爬取完成后生成快照

快照文件保存在 data/checkpoints/ 目录下，格式：checkpoint_{task_id}_{timestamp}.json
"""

import glob
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHECKPOINT_DIR = os.path.join(_BASE_DIR, "data", "checkpoints")


def _extract_user_id(url: str) -> str:
    """从作者 URL 提取 user_id"""
    if "/user/profile/" in url:
        uid = url.split("/user/profile/")[-1].split("?")[0]
        return uid
    return ""


def _get_contents_count(task_dir: str, user_id: str) -> int:
    """获取某作者的笔记数量"""
    path = os.path.join(task_dir, f"creator_{user_id}_contents.json")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return len(json.load(f))
    except Exception:
        return 0


def _get_latest_note_date(task_dir: str, user_id: str) -> str:
    """获取某作者最新笔记的发布日期"""
    path = os.path.join(task_dir, f"creator_{user_id}_contents.json")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            notes = json.load(f)
        latest_ts = 0
        for n in notes:
            ts = n.get("time", 0)
            if ts > latest_ts:
                latest_ts = ts
        if latest_ts > 0:
            return datetime.fromtimestamp(latest_ts / 1000).strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def _get_progress_info(progress_dir: str, user_id: str) -> dict:
    """读取作者的 progress 文件信息"""
    path = os.path.join(progress_dir, f"creator_{user_id}_progress.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            prog = json.load(f)
        return {
            "crawled_count": prog.get("crawled_count", 0),
            "failed_count": len(prog.get("failed_ids", [])),
            "comment_crawled_count": len(prog.get("comment_crawled_ids", [])),
        }
    except Exception:
        return {}


def export_checkpoint(
    task_id: str,
    feishu_url: str = "",
    note: str = "",
) -> str:
    """
    导出当前爬取状态为快照文件。

    读取 batch_progress + 各作者 progress + contents 汇总成一份完整快照。
    Returns: 快照文件路径
    """
    os.makedirs(_CHECKPOINT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    progress_file = os.path.join(_BASE_DIR, "data", f"batch_progress_{task_id}.json")
    if not os.path.exists(progress_file):
        raise FileNotFoundError(f"batch_progress 文件不存在: {progress_file}")

    with open(progress_file, "r", encoding="utf-8") as f:
        bp = json.load(f)

    task_dir = os.path.join(_BASE_DIR, "data", "xhs", "json", task_id)
    progress_dir = os.path.join(_BASE_DIR, "data", "xhs", "progress")

    all_urls = set()
    for bucket in ("completed", "failed", "partial", "skipped"):
        items = bp.get(bucket, {})
        if isinstance(items, dict):
            all_urls.update(items.keys())
        elif isinstance(items, list):
            all_urls.update(items)
    all_urls.update(bp.get("last_crawl_dates", {}).keys())

    authors: Dict[str, dict] = {}
    total_notes = 0

    for url in sorted(all_urls):
        uid = _extract_user_id(url)
        if not uid:
            continue

        last_date = bp.get("last_crawl_dates", {}).get(url, "")
        status = "unknown"
        completed = bp.get("completed", {})
        if isinstance(completed, list):
            if url in completed:
                status = "completed"
        elif isinstance(completed, dict):
            if url in completed:
                status = "completed"
        if url in bp.get("failed", {}):
            status = "failed"
        if url in bp.get("skipped", {}):
            status = "skipped"
        if url in bp.get("partial", {}):
            status = "partial"

        contents_count = _get_contents_count(task_dir, uid)
        latest_note = _get_latest_note_date(task_dir, uid)
        prog_info = _get_progress_info(progress_dir, uid)

        total_notes += contents_count

        authors[uid] = {
            "url": url,
            "status": status,
            "last_crawl_date": last_date,
            "latest_note_date": latest_note,
            "contents_count": contents_count,
            **prog_info,
        }

    completed_count = sum(1 for a in authors.values() if a["status"] == "completed")
    failed_count = sum(1 for a in authors.values() if a["status"] == "failed")
    skipped_count = sum(1 for a in authors.values() if a["status"] == "skipped")

    checkpoint = {
        "checkpoint_id": f"{task_id}_{timestamp}",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "task_id": task_id,
        "authors": authors,
        "summary": {
            "total_authors": len(authors),
            "completed": completed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "total_notes": total_notes,
        },
        "feishu_url": feishu_url,
        "note": note or f"自动快照 {timestamp}",
    }

    filename = f"checkpoint_{task_id}_{timestamp}.json"
    filepath = os.path.join(_CHECKPOINT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False, indent=2)

    return filepath


def import_checkpoint(checkpoint_path: str) -> dict:
    """
    从快照文件恢复 batch_progress。

    将快照中每个作者的 last_crawl_date 和 status 写回 batch_progress 文件，
    作为下次增量爬取的基线。

    Returns: {"task_id": ..., "restored_authors": N, "message": ...}
    """
    with open(checkpoint_path, "r", encoding="utf-8") as f:
        cp = json.load(f)

    task_id = cp.get("task_id", "")
    if not task_id:
        raise ValueError("快照文件中缺少 task_id")

    authors = cp.get("authors", {})

    completed = {}
    failed = {}
    skipped = {}
    last_crawl_dates = {}

    for uid, info in authors.items():
        url = info.get("url", "")
        if not url:
            continue
        status = info.get("status", "unknown")
        last_date = info.get("last_crawl_date", "")

        if status == "completed":
            completed[url] = ""
        elif status == "failed":
            failed[url] = info.get("error", "从快照恢复")
        elif status == "skipped":
            skipped[url] = info.get("reason", "从快照恢复")

        if last_date:
            last_crawl_dates[url] = last_date

    bp_data = {
        "session_id": f"restored_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "completed": completed,
        "failed": failed,
        "partial": {},
        "skipped": skipped,
        "last_crawl_dates": last_crawl_dates,
        "retry_queue": [],
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    progress_file = os.path.join(_BASE_DIR, "data", f"batch_progress_{task_id}.json")
    os.makedirs(os.path.dirname(progress_file), exist_ok=True)

    if os.path.exists(progress_file):
        backup = progress_file + f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            import shutil
            shutil.copy2(progress_file, backup)
        except Exception:
            pass

    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(bp_data, f, ensure_ascii=False, indent=2)

    return {
        "task_id": task_id,
        "restored_authors": len(last_crawl_dates),
        "completed": len(completed),
        "failed": len(failed),
        "skipped": len(skipped),
        "progress_file": progress_file,
        "message": f"已从快照恢复 {len(last_crawl_dates)} 个作者的增量日期",
    }


def list_checkpoints(task_id: str = "") -> List[dict]:
    """
    列出所有快照文件。

    Args:
        task_id: 可选，只列出指定任务的快照

    Returns: 快照列表（按时间倒序）
    """
    if not os.path.isdir(_CHECKPOINT_DIR):
        return []

    pattern = f"checkpoint_{task_id}_*.json" if task_id else "checkpoint_*.json"
    files = glob.glob(os.path.join(_CHECKPOINT_DIR, pattern))

    result = []
    for fp in sorted(files, key=os.path.getmtime, reverse=True):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                cp = json.load(f)
            result.append({
                "filename": os.path.basename(fp),
                "filepath": fp,
                "checkpoint_id": cp.get("checkpoint_id", ""),
                "created_at": cp.get("created_at", ""),
                "task_id": cp.get("task_id", ""),
                "summary": cp.get("summary", {}),
                "feishu_url": cp.get("feishu_url", ""),
                "note": cp.get("note", ""),
            })
        except Exception:
            continue

    return result


def get_checkpoint_detail(filename: str) -> dict:
    """读取某个快照的完整内容"""
    filepath = os.path.join(_CHECKPOINT_DIR, filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"快照文件不存在: {filename}")
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# ==================== CLI 入口 ====================

def main():
    import sys
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print("用法:")
        print("  python -m tools.checkpoint_manager export <task_id> [--note '备注'] [--feishu_url 'URL']")
        print("  python -m tools.checkpoint_manager import <checkpoint_file>")
        print("  python -m tools.checkpoint_manager list [task_id]")
        print("  python -m tools.checkpoint_manager detail <filename>")
        sys.exit(0)

    action = args[0]

    if action == "export":
        if len(args) < 2:
            print("错误: 请指定 task_id")
            sys.exit(1)
        tid = args[1]
        note = ""
        feishu_url = ""
        i = 2
        while i < len(args):
            if args[i] == "--note" and i + 1 < len(args):
                note = args[i + 1]
                i += 2
            elif args[i] == "--feishu_url" and i + 1 < len(args):
                feishu_url = args[i + 1]
                i += 2
            else:
                i += 1
        path = export_checkpoint(tid, feishu_url=feishu_url, note=note)
        print(f"✓ 快照已导出: {path}")

        cp = get_checkpoint_detail(os.path.basename(path))
        s = cp["summary"]
        print(f"  作者: {s['total_authors']} 个 (完成: {s['completed']}, 失败: {s['failed']}, 跳过: {s['skipped']})")
        print(f"  笔记: {s['total_notes']} 条")
        for uid, info in cp["authors"].items():
            ldate = info.get("last_crawl_date", "?")
            cnt = info.get("contents_count", 0)
            st = info.get("status", "?")
            print(f"    {uid[:12]}... | {st:10s} | 日期: {ldate} | {cnt} 条")

    elif action == "import":
        if len(args) < 2:
            print("错误: 请指定快照文件路径")
            sys.exit(1)
        cp_path = args[1]
        if not os.path.isabs(cp_path):
            full = os.path.join(_CHECKPOINT_DIR, cp_path)
            if os.path.exists(full):
                cp_path = full
        result = import_checkpoint(cp_path)
        print(f"✓ {result['message']}")
        print(f"  batch_progress 已写入: {result['progress_file']}")

    elif action == "list":
        tid = args[1] if len(args) > 1 else ""
        cps = list_checkpoints(tid)
        if not cps:
            print("暂无快照")
        else:
            print(f"共 {len(cps)} 个快照:")
            for cp in cps:
                s = cp.get("summary", {})
                print(f"  [{cp['created_at']}] {cp['filename']}")
                print(f"    {s.get('total_authors',0)} 作者 | {s.get('total_notes',0)} 笔记 | {cp.get('note','')}")

    elif action == "detail":
        if len(args) < 2:
            print("错误: 请指定快照文件名")
            sys.exit(1)
        cp = get_checkpoint_detail(args[1])
        print(json.dumps(cp, ensure_ascii=False, indent=2))

    else:
        print(f"未知命令: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
