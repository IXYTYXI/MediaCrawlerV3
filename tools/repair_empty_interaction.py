# -*- coding: utf-8 -*-
"""
修复互动数据缺失的笔记

功能：
  1. 扫描指定作者的 contents JSON，找出 liked_count/collected_count 全为空的笔记
  2. 从 progress JSON 的 crawled_ids 中移除这些笔记 ID
  3. 从 contents JSON 中删除这些不完整记录
  4. 下次爬取时，这些笔记会被当做"新笔记"重新获取完整详情

用法：
  python -m tools.repair_empty_interaction <creator_id> [--dry-run]

示例：
  python -m tools.repair_empty_interaction 62b16791000000001501b2ca --dry-run
  python -m tools.repair_empty_interaction 62b16791000000001501b2ca
"""
import json
import os
import sys
import shutil
from datetime import datetime


def find_files(creator_id: str):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    progress_json = os.path.join(base, "data", "xhs", "progress", f"creator_{creator_id}_progress.json")

    task_dirs = []
    json_dir = os.path.join(base, "data", "xhs", "json")
    for d in os.listdir(json_dir):
        dp = os.path.join(json_dir, d)
        if os.path.isdir(dp):
            task_dirs.append(dp)
    for d in [json_dir] + task_dirs:
        candidate = os.path.join(d, f"creator_{creator_id}_contents.json")
        if os.path.exists(candidate):
            return progress_json, candidate

    candidate = os.path.join(json_dir, f"creator_{creator_id}_contents.json")
    return progress_json, candidate


def find_empty_notes(notes):
    empty = []
    for n in notes:
        liked = str(n.get("liked_count", "")).strip()
        collected = str(n.get("collected_count", "")).strip()
        if liked == "" and collected == "":
            empty.append(n)
    return empty


def main():
    args = sys.argv[1:]
    if not args:
        print("用法: python -m tools.repair_empty_interaction <creator_id> [--dry-run]")
        sys.exit(1)

    creator_id = args[0]
    dry_run = "--dry-run" in args

    progress_path, contents_path = find_files(creator_id)

    print(f"进度文件: {progress_path}")
    print(f"数据文件: {contents_path}")
    print(f"模式: {'预览 (dry-run)' if dry_run else '实际修复'}")
    print()

    if not os.path.exists(contents_path):
        print(f"[错误] 数据文件不存在: {contents_path}")
        sys.exit(1)

    with open(contents_path, "r", encoding="utf-8") as f:
        notes = json.load(f)

    print(f"数据文件中总笔记数: {len(notes)}")

    author_notes = [n for n in notes if n.get("user_id") == creator_id]
    other_notes = [n for n in notes if n.get("user_id") != creator_id]
    print(f"  该作者笔记: {len(author_notes)} 条")
    print(f"  其他作者笔记: {len(other_notes)} 条")

    empty_notes = find_empty_notes(author_notes)
    print(f"  互动数据全空 (liked+collected): {len(empty_notes)} 条")
    print()

    if not empty_notes:
        print("没有需要修复的笔记！")
        return

    print("将要修复的笔记:")
    for n in empty_notes:
        ts = n.get("time", 0)
        date_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else "?"
        print(f"  {n['note_id']} | {date_str} | {n.get('title', '')[:40]}")

    empty_ids = {n["note_id"] for n in empty_notes}
    print(f"\n共 {len(empty_ids)} 个笔记 ID 需要从进度和数据文件中移除")

    if dry_run:
        print("\n[dry-run] 预览结束，未做任何修改。去掉 --dry-run 执行实际修复。")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if os.path.exists(progress_path):
        backup = f"{progress_path}.bak_{timestamp}"
        shutil.copy2(progress_path, backup)
        print(f"\n进度文件备份: {backup}")

        with open(progress_path, "r", encoding="utf-8") as f:
            progress = json.load(f)

        old_count = len(progress.get("crawled_ids", []))
        progress["crawled_ids"] = [
            id_ for id_ in progress.get("crawled_ids", []) if id_ not in empty_ids
        ]
        new_count = len(progress["crawled_ids"])
        progress["crawled_count"] = new_count
        progress["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)

        print(f"进度文件: crawled_ids {old_count} → {new_count} (移除 {old_count - new_count} 个)")
    else:
        print(f"\n[警告] 进度文件不存在: {progress_path}，跳过进度修复")

    backup_c = f"{contents_path}.bak_{timestamp}"
    shutil.copy2(contents_path, backup_c)
    print(f"数据文件备份: {backup_c}")

    cleaned_author = [n for n in author_notes if n["note_id"] not in empty_ids]
    final_notes = other_notes + cleaned_author
    old_total = len(notes)
    new_total = len(final_notes)

    with open(contents_path, "w", encoding="utf-8") as f:
        json.dump(final_notes, f, ensure_ascii=False, indent=2)

    print(f"数据文件: {old_total} → {new_total} 条 (移除 {old_total - new_total} 条空数据)")
    print(f"\n修复完成！下次爬取这个作者时，被移除的 {len(empty_ids)} 条笔记会重新获取完整详情。")


if __name__ == "__main__":
    main()
