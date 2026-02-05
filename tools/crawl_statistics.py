# -*- coding: utf-8 -*-
"""
爬取统计模块
生成爬取结果的统计摘要
"""
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, asdict

from tools import utils


def parse_count(value: Union[str, int, None]) -> int:
    """
    解析数量值，支持中文格式如 "2.3万"、"10万"、"1.5亿" 等
    
    Args:
        value: 数量值，可能是整数、字符串或None
        
    Returns:
        int: 解析后的整数
    """
    if value is None:
        return 0
    
    if isinstance(value, int):
        return value
    
    if isinstance(value, float):
        return int(value)
    
    if not isinstance(value, str):
        return 0
    
    value = value.strip()
    if not value:
        return 0
    
    # 尝试直接转换
    try:
        return int(value)
    except ValueError:
        pass
    
    # 处理中文数字格式
    try:
        # 匹配 "2.3万"、"10万"、"1.5亿" 等格式
        match = re.match(r'^([\d.]+)\s*(万|亿)?$', value)
        if match:
            num = float(match.group(1))
            unit = match.group(2)
            if unit == '万':
                return int(num * 10000)
            elif unit == '亿':
                return int(num * 100000000)
            else:
                return int(num)
    except (ValueError, AttributeError):
        pass
    
    return 0


@dataclass
class NoteStats:
    """单个作品的统计信息"""
    note_id: str
    title: str
    note_url: str
    liked_count: int = 0
    collected_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    publish_time: str = ""
    nickname: str = ""


@dataclass
class CrawlSummary:
    """爬取统计摘要"""
    # 基本信息
    crawl_time: str
    platform: str
    creator_nickname: str
    creator_id: str
    
    # 数量统计
    total_notes: int
    total_comments: int
    
    # 互动数据汇总
    total_likes: int
    total_collects: int
    total_shares: int
    avg_likes: float
    avg_collects: float
    avg_comments: float
    avg_shares: float
    
    # 最高/最低统计
    most_liked: Optional[Dict] = None
    least_liked: Optional[Dict] = None
    most_collected: Optional[Dict] = None
    least_collected: Optional[Dict] = None
    most_commented: Optional[Dict] = None
    least_commented: Optional[Dict] = None
    most_shared: Optional[Dict] = None
    least_shared: Optional[Dict] = None
    
    # 所有作品列表
    all_notes: List[Dict] = None


class CrawlStatisticsGenerator:
    """
    爬取统计生成器
    """
    
    def __init__(self, platform: str = "xhs"):
        self.platform = platform
        self.notes_data: List[NoteStats] = []
        self.comments_count = 0
        self.creator_nickname = ""
        self.creator_id = ""
    
    def add_note(self, note_item: Dict) -> None:
        """
        添加一个作品的数据
        支持两种数据结构：
        1. 详情数据：interact_info 嵌套结构
        2. 列表数据：直接在顶层的 liked_count 等字段
        """
        interact_info = note_item.get("interact_info", {})
        user_info = note_item.get("user", {})
        
        # 兼容两种数据结构：优先从 interact_info 获取，否则从顶层获取
        liked = interact_info.get("liked_count") or note_item.get("liked_count")
        collected = interact_info.get("collected_count") or note_item.get("collected_count")
        comment = interact_info.get("comment_count") or note_item.get("comment_count")
        share = interact_info.get("share_count") or note_item.get("share_count")
        
        # 使用 parse_count 处理可能的中文数字格式（如 "2.3万"）
        note_stats = NoteStats(
            note_id=note_item.get("note_id", ""),
            title=note_item.get("title") or note_item.get("display_title") or note_item.get("desc", "")[:50],
            note_url=f"https://www.xiaohongshu.com/explore/{note_item.get('note_id')}",
            liked_count=parse_count(liked),
            collected_count=parse_count(collected),
            comment_count=parse_count(comment),
            share_count=parse_count(share),
            publish_time=note_item.get("time", ""),
            nickname=user_info.get("nickname", ""),
        )
        
        # 记录创作者信息
        if not self.creator_nickname and user_info.get("nickname"):
            self.creator_nickname = user_info.get("nickname", "")
            self.creator_id = user_info.get("user_id", "")
        
        self.notes_data.append(note_stats)
        utils.logger.debug(f"[CrawlStatistics] Added note: {note_stats.title[:20]}...")
    
    def add_comments_count(self, count: int) -> None:
        """
        添加评论数量
        """
        self.comments_count += count
    
    def generate_summary(self) -> CrawlSummary:
        """
        生成统计摘要
        """
        if not self.notes_data:
            utils.logger.warning("[CrawlStatistics] No notes data to generate summary")
            return None
        
        # 计算汇总数据
        total_likes = sum(n.liked_count for n in self.notes_data)
        total_collects = sum(n.collected_count for n in self.notes_data)
        total_comments = sum(n.comment_count for n in self.notes_data)
        total_shares = sum(n.share_count for n in self.notes_data)
        
        count = len(self.notes_data)
        
        # 找出最高/最低
        most_liked = max(self.notes_data, key=lambda x: x.liked_count)
        least_liked = min(self.notes_data, key=lambda x: x.liked_count)
        most_collected = max(self.notes_data, key=lambda x: x.collected_count)
        least_collected = min(self.notes_data, key=lambda x: x.collected_count)
        most_commented = max(self.notes_data, key=lambda x: x.comment_count)
        least_commented = min(self.notes_data, key=lambda x: x.comment_count)
        most_shared = max(self.notes_data, key=lambda x: x.share_count)
        least_shared = min(self.notes_data, key=lambda x: x.share_count)
        
        summary = CrawlSummary(
            crawl_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            platform=self.platform,
            creator_nickname=self.creator_nickname,
            creator_id=self.creator_id,
            total_notes=count,
            total_comments=self.comments_count,
            total_likes=total_likes,
            total_collects=total_collects,
            total_shares=total_shares,
            avg_likes=round(total_likes / count, 2) if count > 0 else 0,
            avg_collects=round(total_collects / count, 2) if count > 0 else 0,
            avg_comments=round(total_comments / count, 2) if count > 0 else 0,
            avg_shares=round(total_shares / count, 2) if count > 0 else 0,
            most_liked=self._note_to_dict(most_liked),
            least_liked=self._note_to_dict(least_liked),
            most_collected=self._note_to_dict(most_collected),
            least_collected=self._note_to_dict(least_collected),
            most_commented=self._note_to_dict(most_commented),
            least_commented=self._note_to_dict(least_commented),
            most_shared=self._note_to_dict(most_shared),
            least_shared=self._note_to_dict(least_shared),
            all_notes=[self._note_to_dict(n) for n in self.notes_data],
        )
        
        return summary
    
    def _note_to_dict(self, note: NoteStats) -> Dict:
        """转换为字典"""
        return {
            "note_id": note.note_id,
            "title": note.title,
            "note_url": note.note_url,
            "liked_count": note.liked_count,
            "collected_count": note.collected_count,
            "comment_count": note.comment_count,
            "share_count": note.share_count,
            "publish_time": note.publish_time,
        }
    
    def save_summary(self, output_dir: str = "data") -> str:
        """
        保存统计摘要到文件
        """
        summary = self.generate_summary()
        if not summary:
            return None
        
        # 确保目录存在
        os.makedirs(output_dir, exist_ok=True)
        
        # 生成文件名
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"crawl_summary_{self.platform}_{date_str}.json"
        filepath = os.path.join(output_dir, filename)
        
        # 保存 JSON
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(asdict(summary), f, ensure_ascii=False, indent=2)
        
        utils.logger.info(f"[CrawlStatistics] Summary saved to {filepath}")
        
        # 同时打印到控制台
        self._print_summary(summary)
        
        return filepath
    
    def _print_summary(self, summary: CrawlSummary) -> None:
        """
        打印统计摘要到控制台
        """
        print("\n")
        print("=" * 70)
        print("                    📊 爬取统计摘要")
        print("=" * 70)
        print(f"  爬取时间: {summary.crawl_time}")
        print(f"  平台: {summary.platform}")
        print(f"  创作者: {summary.creator_nickname} (ID: {summary.creator_id})")
        print("-" * 70)
        
        print("\n📈 数量统计:")
        print(f"  总作品数: {summary.total_notes}")
        print(f"  总评论数: {summary.total_comments}")
        
        print("\n❤️ 互动数据汇总:")
        print(f"  总点赞: {summary.total_likes:,} (平均: {summary.avg_likes:,.1f})")
        print(f"  总收藏: {summary.total_collects:,} (平均: {summary.avg_collects:,.1f})")
        print(f"  总评论: {sum(n['comment_count'] for n in summary.all_notes):,} (平均: {summary.avg_comments:,.1f})")
        print(f"  总分享: {summary.total_shares:,} (平均: {summary.avg_shares:,.1f})")
        
        print("\n🏆 点赞最高:")
        if summary.most_liked:
            print(f"  《{summary.most_liked['title'][:30]}...》")
            print(f"  点赞: {summary.most_liked['liked_count']:,} | 链接: {summary.most_liked['note_url']}")
        
        print("\n📉 点赞最低:")
        if summary.least_liked:
            print(f"  《{summary.least_liked['title'][:30]}...》")
            print(f"  点赞: {summary.least_liked['liked_count']:,} | 链接: {summary.least_liked['note_url']}")
        
        print("\n💬 评论最高:")
        if summary.most_commented:
            print(f"  《{summary.most_commented['title'][:30]}...》")
            print(f"  评论: {summary.most_commented['comment_count']:,} | 链接: {summary.most_commented['note_url']}")
        
        print("\n📝 评论最低:")
        if summary.least_commented:
            print(f"  《{summary.least_commented['title'][:30]}...》")
            print(f"  评论: {summary.least_commented['comment_count']:,} | 链接: {summary.least_commented['note_url']}")
        
        print("\n⭐ 收藏最高:")
        if summary.most_collected:
            print(f"  《{summary.most_collected['title'][:30]}...》")
            print(f"  收藏: {summary.most_collected['collected_count']:,} | 链接: {summary.most_collected['note_url']}")
        
        print("\n🔗 分享最高:")
        if summary.most_shared:
            print(f"  《{summary.most_shared['title'][:30]}...》")
            print(f"  分享: {summary.most_shared['share_count']:,} | 链接: {summary.most_shared['note_url']}")
        
        print("\n" + "=" * 70)
        print(f"  统计报告已保存到 data/ 目录")
        print("=" * 70 + "\n")


# 全局统计实例
_statistics_instance: Optional[CrawlStatisticsGenerator] = None


def get_statistics() -> CrawlStatisticsGenerator:
    """获取全局统计实例"""
    global _statistics_instance
    if _statistics_instance is None:
        _statistics_instance = CrawlStatisticsGenerator()
    return _statistics_instance


def reset_statistics(platform: str = "xhs") -> CrawlStatisticsGenerator:
    """重置统计实例"""
    global _statistics_instance
    _statistics_instance = CrawlStatisticsGenerator(platform)
    return _statistics_instance
