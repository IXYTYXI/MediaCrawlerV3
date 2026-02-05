# -*- coding: utf-8 -*-
"""
补爬详情功能
读取已保存的记录，对没有获取到详情的记录重新获取

使用方法:
    cd /Users/yc/Desktop/program/python/MediaCrawler
    uv run python tools/refetch_details.py -i data/xhs/json/creator_contents_2026-02-03_xxx.json

参数:
    -i, --input   : 输入的 JSON 文件路径（必填）
    -o, --output  : 输出文件路径（可选，默认覆盖原文件）
    -w, --wait    : 每次请求等待时间，默认 15 秒
    -m, --max     : 最大补爬数量，0 表示全部
"""
import asyncio
import json
import os
import sys
from typing import Dict, List, Optional
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config.anti_crawl_loader import apply_anti_crawl_config
from tools import utils
from playwright.async_api import async_playwright


def load_json_file(file_path: str) -> List[Dict]:
    """加载 JSON 文件"""
    if not os.path.exists(file_path):
        utils.logger.error(f"文件不存在: {file_path}")
        return []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    if not isinstance(data, list):
        data = [data]
    
    return data


def save_json_file(file_path: str, data: List[Dict]):
    """保存 JSON 文件"""
    # 备份原文件
    if os.path.exists(file_path):
        backup_path = file_path.replace(".json", "_backup.json")
        os.rename(file_path, backup_path)
        utils.logger.info(f"原文件已备份到: {backup_path}")
    
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    utils.logger.info(f"已保存到: {file_path}")


def find_incomplete_records(records: List[Dict]) -> tuple:
    """
    找出没有详情信息的记录
    判断标准：没有 liked_count 或 liked_count 为空/0/None
    
    Returns:
        (incomplete_records, complete_count)
    """
    incomplete = []
    complete_count = 0
    
    for record in records:
        # 检查是否有详情信息
        liked_count = record.get("liked_count")
        # 如果有 liked_count 且不为空，认为是完整的
        if liked_count is not None and liked_count != "" and str(liked_count) != "0":
            complete_count += 1
        else:
            # 确保有 note_id 和 xsec_token
            if record.get("note_id") and record.get("xsec_token"):
                incomplete.append(record)
    
    return incomplete, complete_count


def update_record_with_detail(record: Dict, detail: Dict) -> Dict:
    """用详情数据更新记录"""
    if not detail:
        return record
    
    interact_info = detail.get("interact_info", {})
    user_info = detail.get("user", {})
    image_list = detail.get("image_list", [])
    tag_list = detail.get("tag_list", [])
    
    # 更新字段
    record.update({
        "type": detail.get("type", record.get("type")),
        "title": detail.get("title") or record.get("title"),
        "desc": detail.get("desc", ""),
        "time": detail.get("time"),
        "last_update_time": detail.get("last_update_time", 0),
        "user_id": user_info.get("user_id", record.get("user_id")),
        "nickname": user_info.get("nickname", record.get("nickname")),
        "avatar": user_info.get("avatar", ""),
        "liked_count": interact_info.get("liked_count", 0),
        "collected_count": interact_info.get("collected_count", 0),
        "comment_count": interact_info.get("comment_count", 0),
        "share_count": interact_info.get("share_count", 0),
        "ip_location": detail.get("ip_location", ""),
        "image_list": ','.join([img.get('url', '') for img in image_list]),
        "tag_list": ','.join([tag.get('name', '') for tag in tag_list if tag.get('type') == 'topic']),
        "refetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    
    return record


async def refetch_details(
    input_file: str,
    output_file: Optional[str] = None,
    wait_seconds: float = 15.0,
    max_refetch: int = 0
):
    """
    补爬详情主函数
    
    Args:
        input_file: 输入的 JSON 文件路径
        output_file: 输出的 JSON 文件路径（默认覆盖原文件）
        wait_seconds: 每次请求之间的等待时间
        max_refetch: 最大补爬数量（0 表示全部）
    """
    from media_platform.xhs.client import XiaoHongShuClient
    from tools.cdp_browser import CDPBrowserManager
    
    # 加载反反爬配置
    apply_anti_crawl_config(config)
    
    # 加载数据
    utils.logger.info("=" * 60)
    utils.logger.info(f"[补爬] 加载文件: {input_file}")
    records = load_json_file(input_file)
    if not records:
        utils.logger.error("没有找到记录")
        return
    
    # 找出需要补爬的记录
    incomplete, complete_count = find_incomplete_records(records)
    
    utils.logger.info(f"[补爬] 总记录数: {len(records)}")
    utils.logger.info(f"[补爬] 已有详情: {complete_count}")
    utils.logger.info(f"[补爬] 需要补爬: {len(incomplete)}")
    utils.logger.info("=" * 60)
    
    if not incomplete:
        utils.logger.info("所有记录都已有详情，无需补爬")
        return
    
    # 限制补爬数量
    if max_refetch > 0:
        incomplete = incomplete[:max_refetch]
        utils.logger.info(f"本次补爬数量限制: {max_refetch}")
    
    # 启动浏览器
    cdp_manager = CDPBrowserManager()
    
    async with async_playwright() as playwright:
        try:
            # 连接浏览器
            await cdp_manager.launch_and_connect(playwright)
            browser_context = cdp_manager.browser_context
            
            if not browser_context:
                utils.logger.error("无法连接浏览器，请确保浏览器已启动并登录小红书")
                return
            
            # 获取 cookies
            cookie_str, cookie_dict = utils.convert_cookies(await browser_context.cookies())
            
            # 创建客户端
            xhs_client = XiaoHongShuClient(
                proxy=None,
                headers={
                    "accept": "application/json, text/plain, */*",
                    "accept-language": "zh-CN,zh;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/json;charset=UTF-8",
                    "origin": "https://www.xiaohongshu.com",
                    "pragma": "no-cache",
                    "referer": "https://www.xiaohongshu.com/",
                    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                },
                cookie_dict=cookie_dict,
            )
            
            # 补爬
            success_count = 0
            fail_count = 0
            
            for idx, record in enumerate(incomplete, 1):
                note_id = record.get("note_id")
                xsec_token = record.get("xsec_token")
                xsec_source = record.get("xsec_source", "pc_feed")
                title = (record.get("title") or record.get("display_title") or "无标题")[:25]
                
                utils.logger.info(f"[补爬] ({idx}/{len(incomplete)}) {title}...")
                
                try:
                    # 获取详情
                    detail = await xhs_client.get_note_by_id(
                        note_id=note_id,
                        xsec_source=xsec_source,
                        xsec_token=xsec_token
                    )
                    
                    if detail:
                        # 更新记录
                        update_record_with_detail(record, detail)
                        success_count += 1
                        liked = detail.get("interact_info", {}).get("liked_count", "?")
                        utils.logger.info(f"[补爬] ({idx}/{len(incomplete)}) ✓ 点赞数: {liked}")
                    else:
                        fail_count += 1
                        utils.logger.warning(f"[补爬] ({idx}/{len(incomplete)}) ✗ 返回空")
                        
                except Exception as e:
                    fail_count += 1
                    utils.logger.warning(f"[补爬] ({idx}/{len(incomplete)}) ✗ 错误: {e}")
                
                # 等待
                if idx < len(incomplete):
                    utils.logger.info(f"[补爬] 等待 {wait_seconds} 秒...")
                    await asyncio.sleep(wait_seconds)
            
            utils.logger.info("=" * 60)
            utils.logger.info(f"[补爬] 完成！成功: {success_count}, 失败: {fail_count}")
            utils.logger.info("=" * 60)
            
        finally:
            # 保存结果
            output = output_file or input_file
            save_json_file(output, records)
            
            # 不关闭浏览器（保持登录状态）
            utils.logger.info("[补爬] 浏览器保持运行")


async def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="补爬详情功能 - 对没有详情的记录重新获取")
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="输入的 JSON 文件路径"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="输出的 JSON 文件路径（默认覆盖原文件，原文件会备份）"
    )
    parser.add_argument(
        "--wait", "-w",
        type=float,
        default=15.0,
        help="每次请求之间的等待时间（秒），默认 15"
    )
    parser.add_argument(
        "--max", "-m",
        type=int,
        default=0,
        help="最大补爬数量（0 表示全部），默认 0"
    )
    
    args = parser.parse_args()
    
    await refetch_details(
        input_file=args.input,
        output_file=args.output,
        wait_seconds=args.wait,
        max_refetch=args.max
    )


if __name__ == "__main__":
    asyncio.run(main())
