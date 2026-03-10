# -*- coding: utf-8 -*-
"""
将本地已爬取的「搜索模式」数据重新写入飞书多维表格。

用于：爬取已完成但飞书写入失败（如 NumberFieldConvFail）时，
不重新爬取，仅用修好的代码把 data/xhs/json 下的 search_contents_*.json
和 search_comments_*.json 再推一次到飞书。

用法:
  cd /data/vonjan/program/MediaCrawler-stable
  python -m tools.resync_feishu_search                    # 使用最新一份搜索数据
  python -m tools.resync_feishu_search 2026-03-09_231412  # 指定 session 时间戳
"""
import json
import os
import sys

# 避免 tools 链式导入爬虫/图像依赖（playwright、cv2、numpy）
from unittest.mock import MagicMock
_mock = MagicMock()
for _mod in ("playwright", "playwright.async_api", "cv2", "numpy"):
    if _mod not in sys.modules:
        sys.modules[_mod] = _mock


_CONTENT_PREFIXES = ("search_top_contents_", "search_contents_")


def _find_latest_search_session(json_dir: str, platform: str = "xhs") -> str:
    """按修改时间取最新的 search(_top)_contents_*.json 对应的时间戳"""
    best_ts = ""
    best_mtime = 0
    if not os.path.isdir(json_dir):
        return ""
    for name in os.listdir(json_dir):
        if not name.endswith(".json"):
            continue
        matched_prefix = ""
        for pfx in _CONTENT_PREFIXES:
            if name.startswith(pfx):
                matched_prefix = pfx
                break
        if not matched_prefix:
            continue
        ts = name[len(matched_prefix) : -5]
        path = os.path.join(json_dir, name)
        try:
            m = os.path.getmtime(path)
            if m > best_mtime:
                best_mtime = m
                best_ts = ts
        except OSError:
            pass
    return best_ts


def main():
    # 项目根目录
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    sys.path.insert(0, root)

    platform = "xhs"
    json_dir = os.path.join("data", platform, "json")
    if not os.path.isdir(json_dir):
        print(f"[ResyncFeishu] 数据目录不存在: {json_dir}")
        sys.exit(1)

    session_ts = ""
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        session_ts = sys.argv[1].strip()
    else:
        session_ts = _find_latest_search_session(json_dir, platform)
        if not session_ts:
            print("[ResyncFeishu] 未找到任何 search_contents_*.json，请指定 session 时间戳")
            print("  示例: python -m tools.resync_feishu_search 2026-03-09_231412")
            sys.exit(1)
        print(f"[ResyncFeishu] 使用最新数据: session_ts={session_ts}")

    notes_file = ""
    comments_file = ""
    for content_pfx, comment_pfx in [("search_top_contents_", "search_top_comments_"),
                                      ("search_contents_", "search_comments_")]:
        candidate = os.path.join(json_dir, f"{content_pfx}{session_ts}.json")
        if os.path.isfile(candidate):
            notes_file = candidate
            comments_file = os.path.join(json_dir, f"{comment_pfx}{session_ts}.json")
            break
    if not notes_file:
        print(f"[ResyncFeishu] 笔记文件不存在: search(_top)_contents_{session_ts}.json")
        sys.exit(1)

    with open(notes_file, "r", encoding="utf-8") as f:
        notes = json.load(f)
    if not notes:
        print("[ResyncFeishu] 笔记数据为空，跳过")
        sys.exit(0)

    comments_by_note = {}
    if os.path.isfile(comments_file):
        with open(comments_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for group in raw:
            nid = group.get("note_id", "")
            clist = group.get("comments", [])
            if nid and clist:
                comments_by_note[nid] = clist

    cfg_path = os.path.join("config", "anti_crawl_config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            acfg = json.load(f)
    except Exception as e:
        print(f"[ResyncFeishu] 读取配置失败: {e}")
        sys.exit(1)
    feishu_cfg = acfg.get("feishu", {})
    if not feishu_cfg.get("enabled"):
        print("[ResyncFeishu] 飞书未启用，请在 config 中开启")
        sys.exit(1)
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    folder_token = feishu_cfg.get("search_folder_token") or feishu_cfg.get("folder_token", "")
    if not app_id or not app_secret:
        print("[ResyncFeishu] 未配置 feishu.app_id / app_secret")
        sys.exit(1)

    print(f"[ResyncFeishu] 开始写入飞书: {len(notes)} 条笔记, "
          f"{sum(len(v) for v in comments_by_note.values())} 条评论")
    try:
        from tools.pipeline_feishu_writer import PipelineFeishuWriter
        with PipelineFeishuWriter(
            feishu_app_id=app_id,
            feishu_app_secret=app_secret,
            bitable_name=f"搜索爬取_{session_ts}",
            folder_token=folder_token,
        ) as writer:
            writer.write_search_results(
                notes=notes,
                comments_by_note=comments_by_note,
            )
        print("[ResyncFeishu] 飞书写入完成")
        if getattr(writer, "bitable_url", None):
            print(f"[ResyncFeishu] 多维表格链接: {writer.bitable_url}")
    except Exception as e:
        print(f"[ResyncFeishu] 飞书写入失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
